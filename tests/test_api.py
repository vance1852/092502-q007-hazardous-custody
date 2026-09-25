import unittest

from hazardous_ledger_core.api import route
from hazardous_ledger_core.ledger import LedgerService
from hazardous_ledger_core.storage import Database


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = LedgerService(self.database)

    def tearDown(self):
        self.database.close()

    def test_health_is_available_without_actor(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_route_returns_404(self):
        status, payload = route(self.service, "GET", "/missing", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_invalid_json_shape_returns_400(self):
        status, payload = route(self.service, "POST", "/organizations", {"request_id": "x"},
                                {"X-Actor-Id": "bootstrap"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def _bootstrap_ledger(self):
        route(self.service, "POST", "/organizations",
              {"request_id": "org-1", "organization_id": "o1", "name": "企业"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "act-1", "new_actor_id": "a1", "display_name": "管理员",
               "role": "admin", "organization_id": "o1"}, {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "act-2", "new_actor_id": "op1", "display_name": "操作员一",
               "role": "operator", "organization_id": "o1"}, {"X-Actor-Id": "a1"})
        route(self.service, "POST", "/actors",
              {"request_id": "act-3", "new_actor_id": "op2", "display_name": "操作员二",
               "role": "operator", "organization_id": "o1"}, {"X-Actor-Id": "a1"})
        route(self.service, "POST", "/sites",
              {"request_id": "s1", "site_id": "ws", "organization_id": "o1",
               "name": "车间", "timezone_name": "Asia/Shanghai"}, {"X-Actor-Id": "op1"})
        route(self.service, "POST", "/sites",
              {"request_id": "s2", "site_id": "ys", "organization_id": "o1",
               "name": "暂存", "timezone_name": "Asia/Shanghai"}, {"X-Actor-Id": "op1"})
        route(self.service, "POST", "/domain-records",
              {"request_id": "d1", "site_id": "ws", "category": "waste_category",
               "external_key": "HW06", "data": {"name": "废溶剂"}}, {"X-Actor-Id": "op1"})

    def test_container_handover_round_trip_over_http(self):
        self._bootstrap_ledger()
        status, payload = route(self.service, "POST", "/containers",
                                {"request_id": "c1", "site_id": "ws", "container_id": "c1",
                                 "waste_category": "HW06", "net_weight_g": 100000,
                                 "seal_id": "S1"}, {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        status, payload = route(self.service, "POST", "/handovers",
                                {"request_id": "h1", "from_site_id": "ws", "to_site_id": "ys",
                                 "items": [{"container_id": "c1", "declared_weight_g": 100000,
                                            "declared_seal": "S1"}]}, {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        handover_id = payload["handover_id"]
        status, payload = route(self.service, "POST", "/handover-responses",
                                {"request_id": "r1", "handover_id": handover_id,
                                 "decision": "accept",
                                 "items": [{"container_id": "c1", "received_weight_g": 100000,
                                            "received_seal": "S1"}]}, {"X-Actor-Id": "op2"})
        self.assertEqual(201, status)
        self.assertEqual("closed", payload["status"])
        status, payload = route(self.service, "GET", f"/handovers/{handover_id}", None)
        self.assertEqual(200, status)
        self.assertEqual("closed", payload["status"])
        status, payload = route(self.service, "GET", "/containers/c1/trace", None)
        self.assertEqual(200, status)
        self.assertEqual("custody", payload["final_destination"]["type"])
        self.assertEqual("ys", payload["final_destination"]["site_id"])

    def test_late_conflicting_message_returns_409(self):
        self._bootstrap_ledger()
        route(self.service, "POST", "/containers",
              {"request_id": "c1", "site_id": "ws", "container_id": "c1",
               "waste_category": "HW06", "net_weight_g": 100000, "seal_id": "S1"},
              {"X-Actor-Id": "op1"})
        _, payload = route(self.service, "POST", "/handovers",
                           {"request_id": "h1", "from_site_id": "ws", "to_site_id": "ys",
                            "items": [{"container_id": "c1", "declared_weight_g": 100000,
                                       "declared_seal": "S1"}]}, {"X-Actor-Id": "op1"})
        handover_id = payload["handover_id"]
        route(self.service, "POST", "/handover-responses",
              {"request_id": "r1", "handover_id": handover_id, "decision": "accept",
               "items": [{"container_id": "c1", "received_weight_g": 100000,
                          "received_seal": "S1"}]}, {"X-Actor-Id": "op2"})
        status, payload = route(self.service, "POST", "/handover-responses",
                                {"request_id": "r2", "handover_id": handover_id,
                                 "decision": "accept",
                                 "items": [{"container_id": "c1", "received_weight_g": 90000,
                                            "received_seal": "S1"}]}, {"X-Actor-Id": "op2"})
        self.assertEqual(409, status)
        status, payload = route(self.service, "GET", "/discrepancies?status=late_open", None)
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))


if __name__ == "__main__":
    unittest.main()
