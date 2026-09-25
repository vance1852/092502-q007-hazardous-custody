import unittest

from hazardous_ledger_core.api import route
from hazardous_ledger_core.service import DomainService
from hazardous_ledger_core.storage import Database


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)

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

    def _seed(self):
        route(self.service, "POST", "/organizations",
              {"request_id": "r-org", "organization_id": "o1", "name": "厂"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "r-admin", "new_actor_id": "a1", "display_name": "管",
               "role": "admin", "organization_id": "o1"}, {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "r-op", "new_actor_id": "op1", "display_name": "员",
               "role": "operator", "organization_id": "o1"}, {"X-Actor-Id": "a1"})
        route(self.service, "POST", "/sites",
              {"request_id": "r-site", "site_id": "s1", "organization_id": "o1",
               "name": "场所", "timezone_name": "Asia/Shanghai"}, {"X-Actor-Id": "a1"})
        route(self.service, "POST", "/domain-records",
              {"request_id": "r-cat", "site_id": "s1", "category": "waste_category",
               "external_key": "hw", "data": {"name": "渣"}}, {"X-Actor-Id": "op1"})
        route(self.service, "POST", "/domain-records",
              {"request_id": "r-zone", "site_id": "s1", "category": "storage_zone",
               "external_key": "zn", "data": {"name": "存"}}, {"X-Actor-Id": "op1"})
        route(self.service, "POST", "/domain-records",
              {"request_id": "r-car", "site_id": "s1", "category": "carrier_profile",
               "external_key": "tr", "data": {"name": "运"}}, {"X-Actor-Id": "op1"})

    def test_handover_routes_end_to_end(self):
        self._seed()
        status, payload = route(self.service, "POST", "/containers",
                                {"request_id": "c1", "container_id": "bin-1", "site_id": "s1",
                                 "waste_category_key": "hw", "weight": 100.0, "seal": "s1",
                                 "custodian_party": "zn"}, {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        status, payload = route(self.service, "POST", "/manifests",
                                {"request_id": "m1", "manifest_id": "mf-1", "site_id": "s1",
                                 "from_party": "zn", "to_party": "tr",
                                 "items": [{"container_id": "bin-1", "declared_weight": 100.0,
                                            "declared_seal": "s1"}]}, {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        status, payload = route(self.service, "POST", "/manifests/decide",
                                {"request_id": "d1", "manifest_id": "mf-1", "decision": "confirm",
                                 "observations": [{"container_id": "bin-1",
                                                   "received_weight": 100.0,
                                                   "received_seal": "s1"}]},
                                {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        # 重复确认返回原决定（幂等重放）。
        status, payload = route(self.service, "POST", "/manifests/decide",
                                {"request_id": "d1", "manifest_id": "mf-1", "decision": "confirm",
                                 "observations": [{"container_id": "bin-1",
                                                   "received_weight": 100.0,
                                                   "received_seal": "s1"}]},
                                {"X-Actor-Id": "op1"})
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])
        status, payload = route(self.service, "POST", "/manifests/close",
                                {"request_id": "cl1", "manifest_id": "mf-1"},
                                {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        # 已结案联单不能被旧消息重开。
        status, payload = route(self.service, "POST", "/manifests/decide",
                                {"request_id": "d2", "manifest_id": "mf-1", "decision": "confirm",
                                 "observations": [{"container_id": "bin-1",
                                                   "received_weight": 90.0,
                                                   "received_seal": "x"}]},
                                {"X-Actor-Id": "op1"})
        self.assertEqual(409, status)
        self.assertEqual("state_error", payload["error"])
        status, payload = route(self.service, "GET", "/containers/bin-1/trace", None)
        self.assertEqual(200, status)
        self.assertEqual("tr", payload["final_destination"]["to_party"])
        status, payload = route(self.service, "GET", "/manifests/mf-1", None)
        self.assertEqual(200, status)
        self.assertEqual("closed", payload["status"])

    def test_discrepancy_returns_409_with_code(self):
        self._seed()
        route(self.service, "POST", "/containers",
              {"request_id": "c1", "container_id": "bin-1", "site_id": "s1",
               "waste_category_key": "hw", "weight": 100.0, "seal": "s1",
               "custodian_party": "zn"}, {"X-Actor-Id": "op1"})
        route(self.service, "POST", "/manifests",
              {"request_id": "m1", "manifest_id": "mf-1", "site_id": "s1",
               "from_party": "zn", "to_party": "tr",
               "items": [{"container_id": "bin-1", "declared_weight": 100.0,
                          "declared_seal": "s1"}]}, {"X-Actor-Id": "op1"})
        status, payload = route(self.service, "POST", "/manifests/decide",
                                {"request_id": "d1", "manifest_id": "mf-1", "decision": "confirm",
                                 "observations": [{"container_id": "bin-1",
                                                   "received_weight": 120.0,
                                                   "received_seal": "s1"}]},
                                {"X-Actor-Id": "op1"})
        self.assertEqual(409, status)
        self.assertEqual("discrepancy", payload["error"])
        status, payload = route(self.service, "GET", "/discrepancies?site_id=s1", None)
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))


if __name__ == "__main__":
    unittest.main()
