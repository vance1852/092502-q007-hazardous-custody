"""危废交接账本的领域规则测试。"""

import unittest
from datetime import datetime, timezone

from hazardous_ledger_core.clock import FixedClock
from hazardous_ledger_core.errors import (
    ConflictError,
    DiscrepancyError,
    NotFoundError,
    PermissionDenied,
    StateError,
    ValidationError,
)
from hazardous_ledger_core.handover import HandoverService
from hazardous_ledger_core.service import DomainService
from hazardous_ledger_core.storage import Database


class HandoverTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database,
                                     FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc)))
        self.handover = HandoverService(self.database, self.service)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="喷涂厂")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                    display_name="车间员", role="operator", organization_id="o1")
        self.service.register_actor(request_id="op2", actor_id="a1", new_actor_id="op2",
                                    display_name="接收员", role="operator", organization_id="o1")
        self.service.register_actor(request_id="rv", actor_id="a1", new_actor_id="rv1",
                                    display_name="质量员", role="reviewer", organization_id="o1")
        self.service.register_actor(request_id="au", actor_id="a1", new_actor_id="au1",
                                    display_name="审计员", role="auditor", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="op1", site_id="s1",
                                   organization_id="o1", name="场所", timezone_name="Asia/Shanghai")
        self.service.record_domain_data(request_id="cat", actor_id="op1", site_id="s1",
                                        category="waste_category", external_key="hw-paint",
                                        data={"name": "漆渣"})
        self.service.record_domain_data(request_id="cat2", actor_id="op1", site_id="s1",
                                        category="waste_category", external_key="hw-sludge",
                                        data={"name": "污泥"})
        self.service.record_domain_data(request_id="zone", actor_id="op1", site_id="s1",
                                        category="storage_zone", external_key="zone-temp",
                                        data={"name": "暂存区"})
        self.service.record_domain_data(request_id="carrier", actor_id="op1", site_id="s1",
                                        category="carrier_profile", external_key="carrier-1",
                                        data={"name": "运输方"})

    def tearDown(self):
        self.database.close()

    def _container(self, cid="c-001", weight=100.0, seal="seal-1",
                   party="zone-temp", request_id="reg"):
        return self.handover.register_container(
            request_id=request_id, actor_id="op1", container_id=cid, site_id="s1",
            waste_category_key="hw-paint", weight=weight, seal=seal, custodian_party=party)

    def _manifest(self, manifest_id="mf-1", container_id="c-001", weight=100.0, seal="seal-1",
                  request_id="mf", frm="zone-temp", to="carrier-1"):
        return self.handover.initiate_manifest(
            request_id=request_id, actor_id="op1", manifest_id=manifest_id, site_id="s1",
            from_party=frm, to_party=to,
            items=[{"container_id": container_id, "declared_weight": weight,
                    "declared_seal": seal}])

    # ------------------------------------------------------------- 容器建档

    def test_container_gets_stable_identity_and_custody(self):
        self._container()
        container = self.handover.get_container("c-001")
        self.assertEqual("hw-paint", container.waste_category_key)
        self.assertEqual(100.0, container.current_weight)
        self.assertEqual("seal-1", container.current_seal)
        self.assertEqual("zone-temp", container.custodian_party)
        self.assertEqual("op1", container.custodian_actor)
        self.assertEqual(1, container.version)

    def test_container_requires_registered_category_and_party(self):
        with self.assertRaises(ValidationError):
            self.handover.register_container(
                request_id="x", actor_id="op1", container_id="c-x", site_id="s1",
                waste_category_key="missing", weight=1.0, seal="s", custodian_party="zone-temp")
        with self.assertRaises(ValidationError):
            self.handover.register_container(
                request_id="y", actor_id="op1", container_id="c-y", site_id="s1",
                waste_category_key="hw-paint", weight=1.0, seal="s", custodian_party="missing")

    def test_auditor_cannot_register_container(self):
        with self.assertRaises(PermissionDenied):
            self.handover.register_container(
                request_id="z", actor_id="au1", container_id="c-z", site_id="s1",
                waste_category_key="hw-paint", weight=1.0, seal="s", custodian_party="zone-temp")

    # ------------------------------------------------------------- 拆分合并

    def test_split_requires_authorization_and_is_one_time(self):
        self._container(weight=100.0)
        with self.assertRaises(NotFoundError):
            self.handover.split_container(
                request_id="sp0", actor_id="op1", authorization_id="no-auth",
                parent_container_id="c-001",
                children=[{"container_id": "ca", "weight": 50.0, "seal": "sa"},
                          {"container_id": "cb", "weight": 50.0, "seal": "sb"}])
        self.handover.authorize_transformation(
            request_id="au1", actor_id="op1", authorization_id="auth-1",
            operation="split", container_ids=["c-001"])
        self.handover.split_container(
            request_id="sp1", actor_id="op1", authorization_id="auth-1",
            parent_container_id="c-001",
            children=[{"container_id": "ca", "weight": 50.0, "seal": "sa"},
                      {"container_id": "cb", "weight": 50.1, "seal": "sb"}])
        with self.assertRaises(ConflictError):
            self.handover.split_container(
                request_id="sp2", actor_id="op1", authorization_id="auth-1",
                parent_container_id="c-001",
                children=[{"container_id": "cc", "weight": 50.0, "seal": "sc"},
                          {"container_id": "cd", "weight": 50.1, "seal": "sd"}])
        # 父容器已消耗，子容器继承类别与保管方。
        self.assertEqual("consumed", self.handover.get_container("c-001").status)
        self.assertEqual("zone-temp", self.handover.get_container("ca").custodian_party)
        self.assertEqual("hw-paint", self.handover.get_container("ca").waste_category_key)

    def test_split_rejects_mass_imbalance(self):
        self._container(weight=100.0)
        self.handover.authorize_transformation(
            request_id="au1", actor_id="op1", authorization_id="auth-1",
            operation="split", container_ids=["c-001"])
        with self.assertRaises(ValidationError):
            self.handover.split_container(
                request_id="sp", actor_id="op1", authorization_id="auth-1",
                parent_container_id="c-001",
                children=[{"container_id": "ca", "weight": 40.0, "seal": "sa"},
                          {"container_id": "cb", "weight": 40.0, "seal": "sb"}])
        # 失败后授权仍然可用、容器未被消耗。
        self.assertEqual("open", self.handover.get_container("c-001").status)

    def test_merge_requires_same_category_and_custodian(self):
        self.handover.register_container(
            request_id="m1", actor_id="op1", container_id="p1", site_id="s1",
            waste_category_key="hw-paint", weight=40.0, seal="s1", custodian_party="zone-temp")
        self.handover.register_container(
            request_id="m2", actor_id="op1", container_id="p2", site_id="s1",
            waste_category_key="hw-sludge", weight=40.0, seal="s2", custodian_party="zone-temp")
        self.handover.authorize_transformation(
            request_id="aum1", actor_id="op1", authorization_id="auth-m",
            operation="merge", container_ids=["p1", "p2"])
        with self.assertRaises(ValidationError):
            self.handover.merge_containers(
                request_id="mg", actor_id="op1", authorization_id="auth-m",
                container_ids=["p1", "p2"], result_container_id="pr", seal="sr", weight=80.0)

    def test_merge_preserves_all_source_lineage(self):
        self.handover.register_container(
            request_id="m1", actor_id="op1", container_id="p1", site_id="s1",
            waste_category_key="hw-paint", weight=40.0, seal="s1", custodian_party="zone-temp")
        self.handover.register_container(
            request_id="m2", actor_id="op1", container_id="p2", site_id="s1",
            waste_category_key="hw-paint", weight=60.0, seal="s2", custodian_party="zone-temp")
        self.handover.authorize_transformation(
            request_id="aum2", actor_id="op1", authorization_id="auth-m",
            operation="merge", container_ids=["p1", "p2"])
        self.handover.merge_containers(
            request_id="mg", actor_id="op1", authorization_id="auth-m",
            container_ids=["p1", "p2"], result_container_id="pr", seal="sr", weight=100.0)
        trace = self.handover.trace_container("pr")
        parents = {edge["parent_container_id"] for edge in trace["ancestors"]}
        self.assertEqual({"p1", "p2"}, parents)
        self.assertEqual("consumed", self.handover.get_container("p1").status)
        self.assertEqual("open", self.handover.get_container("pr").status)

    # ------------------------------------------------------------- 联单流程

    def test_initiation_requires_transferor_custody(self):
        self._container()
        with self.assertRaises(PermissionDenied):
            self._manifest(frm="carrier-1", to="zone-temp", request_id="wrong")

    def test_confirm_transfers_custody_and_closes(self):
        self._container()
        self._manifest()
        receipt = self.handover.decide_manifest(
            request_id="dc", actor_id="op1", manifest_id="mf-1", decision="confirm",
            observations=[{"container_id": "c-001", "received_weight": 100.0,
                           "received_seal": "seal-1"}])
        self.assertFalse(receipt.replayed)
        self.assertEqual("confirmed", self.handover.get_manifest("mf-1")["status"])
        self.assertEqual("carrier-1", self.handover.get_container("c-001").custodian_party)
        self.handover.close_manifest(request_id="cl", actor_id="op1", manifest_id="mf-1")
        self.assertEqual("closed", self.handover.get_manifest("mf-1")["status"])

    def test_repeat_confirmation_returns_original_decision(self):
        self._container()
        self._manifest()
        observations = [{"container_id": "c-001", "received_weight": 100.0,
                         "received_seal": "seal-1"}]
        first = self.handover.decide_manifest(request_id="dc", actor_id="op1",
                                              manifest_id="mf-1", decision="confirm",
                                              observations=observations)
        second = self.handover.decide_manifest(request_id="dc", actor_id="op1",
                                               manifest_id="mf-1", decision="confirm",
                                               observations=observations)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)

    def test_different_weight_within_tolerance_enters_discrepancy_and_auto_closes(self):
        self._container(weight=100.0)
        self._manifest()
        # 0.3% 偏差，默认 0.5% 容差内。
        with self.assertRaises(DiscrepancyError):
            self.handover.decide_manifest(
                request_id="dc", actor_id="op1", manifest_id="mf-1", decision="confirm",
                observations=[{"container_id": "c-001", "received_weight": 100.3,
                               "received_seal": "seal-1"}])
        manifest = self.handover.get_manifest("mf-1")
        self.assertEqual("discrepancy", manifest["status"])
        self.assertEqual("within_tolerance", manifest["discrepancies"][0]["level"])
        # 容差内可直接结案，自动采信复称并转移保管。
        self.handover.close_manifest(request_id="cl", actor_id="op1", manifest_id="mf-1")
        self.assertEqual(100.3, self.handover.get_container("c-001").current_weight)
        self.assertEqual("carrier-1", self.handover.get_container("c-001").custodian_party)

    def test_over_tolerance_requires_quality_ruling_with_statements(self):
        self._container(weight=100.0)
        self._manifest()
        with self.assertRaises(DiscrepancyError):
            self.handover.decide_manifest(
                request_id="dc", actor_id="op1", manifest_id="mf-1", decision="confirm",
                observations=[{"container_id": "c-001", "received_weight": 110.0,
                               "received_seal": "seal-1"}])
        discrepancy_id = self.handover.get_manifest("mf-1")["discrepancies"][0]["discrepancy_id"]
        # 超差不能直接结案。
        with self.assertRaises(DiscrepancyError):
            self.handover.close_manifest(request_id="cl", actor_id="op1", manifest_id="mf-1")
        # 操作员不能裁决。
        with self.assertRaises(PermissionDenied):
            self.handover.rule_discrepancy(
                request_id="rd", actor_id="op1", discrepancy_id=discrepancy_id,
                ruling="accepted")
        self.handover.rule_discrepancy(
            request_id="rd", actor_id="rv1", discrepancy_id=discrepancy_id,
            ruling="accepted", receiver_statement="复称确为110",
            transferor_statement="申报时为100")
        ruled = self.handover.get_manifest("mf-1")["discrepancies"][0]
        self.assertEqual("accepted", ruled["status"])
        self.assertEqual("复称确为110", ruled["receiver_statement"])
        self.assertEqual("申报时为100", ruled["transferor_statement"])
        self.assertEqual("rv1", ruled["ruled_by"])
        self.handover.close_manifest(request_id="cl", actor_id="op1", manifest_id="mf-1")
        self.assertEqual(110.0, self.handover.get_container("c-001").current_weight)

    def test_seal_mismatch_requires_ruling_and_reject_keeps_declared(self):
        self._container(weight=100.0)
        self._manifest()
        with self.assertRaises(DiscrepancyError):
            self.handover.decide_manifest(
                request_id="dc", actor_id="op1", manifest_id="mf-1", decision="confirm",
                observations=[{"container_id": "c-001", "received_weight": 100.0,
                               "received_seal": "broken"}])
        discrepancy_id = self.handover.get_manifest("mf-1")["discrepancies"][0]["discrepancy_id"]
        self.assertEqual("seal_mismatch",
                         self.handover.get_manifest("mf-1")["discrepancies"][0]["level"])
        self.handover.rule_discrepancy(
            request_id="rd", actor_id="rv1", discrepancy_id=discrepancy_id, ruling="rejected")
        # 不采信差异：保管责任回到交出方，维持申报封签。
        container = self.handover.get_container("c-001")
        self.assertEqual("zone-temp", container.custodian_party)
        self.assertEqual("seal-1", container.current_seal)
        self.handover.close_manifest(request_id="cl", actor_id="op1", manifest_id="mf-1")

    def test_reject_keeps_custody_and_repeated_reject_replays(self):
        self._container()
        self._manifest()
        self.handover.decide_manifest(request_id="rj", actor_id="op1", manifest_id="mf-1",
                                      decision="reject", statement="容器破损")
        self.assertEqual("rejected", self.handover.get_manifest("mf-1")["status"])
        self.assertEqual("zone-temp", self.handover.get_container("c-001").custodian_party)
        replay = self.handover.decide_manifest(request_id="rj", actor_id="op1",
                                               manifest_id="mf-1", decision="reject",
                                               statement="容器破损")
        self.assertTrue(replay.replayed)

    def test_closed_manifest_cannot_be_reopened_by_old_message(self):
        self._container()
        self._manifest()
        self.handover.decide_manifest(
            request_id="dc", actor_id="op1", manifest_id="mf-1", decision="confirm",
            observations=[{"container_id": "c-001", "received_weight": 100.0,
                           "received_seal": "seal-1"}])
        self.handover.close_manifest(request_id="cl", actor_id="op1", manifest_id="mf-1")
        # 旧的确认/拒收消息都不能重开已结案联单。
        with self.assertRaises(StateError):
            self.handover.decide_manifest(
                request_id="dc2", actor_id="op1", manifest_id="mf-1", decision="confirm",
                observations=[{"container_id": "c-001", "received_weight": 90.0,
                               "received_seal": "other"}])
        with self.assertRaises(StateError):
            self.handover.decide_manifest(request_id="rj", actor_id="op1",
                                          manifest_id="mf-1", decision="reject")

    def test_different_reweigh_on_same_manifest_refreshes_open_discrepancy(self):
        self._container(weight=100.0)
        self._manifest()
        for weight in (110.0, 112.0):
            with self.assertRaises(DiscrepancyError):
                self.handover.decide_manifest(
                    request_id=f"dc-{weight}", actor_id="op1", manifest_id="mf-1",
                    decision="confirm",
                    observations=[{"container_id": "c-001", "received_weight": weight,
                                   "received_seal": "seal-1"}])
        discrepancies = self.handover.get_manifest("mf-1")["discrepancies"]
        self.assertEqual(1, len(discrepancies))
        self.assertEqual(112.0, discrepancies[0]["received_weight"])
        self.assertEqual("open", discrepancies[0]["status"])

    # ------------------------------------------------------------- 溯源

    def test_trace_reconstructs_source_custody_and_destination(self):
        self._container(cid="root", weight=100.0)
        self.handover.authorize_transformation(
            request_id="aus1", actor_id="op1", authorization_id="auth-s",
            operation="split", container_ids=["root"])
        self.handover.split_container(
            request_id="sp", actor_id="op1", authorization_id="auth-s",
            parent_container_id="root",
            children=[{"container_id": "kid-1", "weight": 50.0, "seal": "k1"},
                      {"container_id": "kid-2", "weight": 50.0, "seal": "k2"}])
        self._manifest(manifest_id="mf-k", container_id="kid-1", weight=50.0, seal="k1",
                       request_id="mfk")
        self.handover.decide_manifest(
            request_id="dck", actor_id="op1", manifest_id="mf-k", decision="confirm",
            observations=[{"container_id": "kid-1", "received_weight": 50.0,
                           "received_seal": "k1"}])
        self.handover.close_manifest(request_id="clk", actor_id="op1", manifest_id="mf-k")

        trace = self.handover.trace_container("kid-1")
        self.assertEqual(["root"], [edge["parent_container_id"] for edge in trace["ancestors"]])
        event_types = [event["event_type"] for event in trace["custody_history"]]
        self.assertIn("manifest_closed", event_types)
        self.assertEqual("carrier-1", trace["final_destination"]["to_party"])
        self.assertEqual([], trace["open_discrepancies"])
        # 从父容器也能经子容器还原最终去向。
        root_trace = self.handover.trace_container("root")
        self.assertEqual("kid-1",
                         root_trace["final_destination"]["via_descendants"][0]["container_id"])

    def test_open_discrepancies_listed_per_site(self):
        self._container(weight=100.0)
        self._manifest()
        with self.assertRaises(DiscrepancyError):
            self.handover.decide_manifest(
                request_id="dc", actor_id="op1", manifest_id="mf-1", decision="confirm",
                observations=[{"container_id": "c-001", "received_weight": 120.0,
                               "received_seal": "seal-1"}])
        open_items = self.handover.list_open_discrepancies("s1")
        self.assertEqual(1, len(open_items))
        self.assertEqual("over_tolerance", open_items[0]["level"])


if __name__ == "__main__":
    unittest.main()
