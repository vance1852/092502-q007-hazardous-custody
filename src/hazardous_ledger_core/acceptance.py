"""运行基础服务与交接账本的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .errors import DiscrepancyError, StateError
from .handover import HandoverService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整登记链与交接链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = DomainService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范企业")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="环保负责人", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-reviewer", actor_id="admin-001", new_actor_id="reviewer-001",
                               display_name="质量工程师", role="reviewer", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号生产场所", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="waste_category", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="waste_category", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})
        records = service.list_domain_data("site-001")

        # ------------------------------------------------ 危废交接账本
        handover = HandoverService(database, service)
        service.record_domain_data(request_id="req-cat", actor_id="operator-001", site_id="site-001",
                                   category="waste_category", external_key="hw-paint",
                                   data={"name": "喷漆漆渣", "code": "HW12"})
        service.record_domain_data(request_id="req-zone", actor_id="operator-001", site_id="site-001",
                                   category="storage_zone", external_key="zone-temp",
                                   data={"name": "危废暂存区"})
        service.record_domain_data(request_id="req-carrier", actor_id="operator-001", site_id="site-001",
                                   category="carrier_profile", external_key="carrier-001",
                                   data={"name": "示范运输公司"})
        # 车间产生 100kg 漆渣，建档入暂存区。
        handover.register_container(request_id="req-c1", actor_id="operator-001",
                                    container_id="bin-001", site_id="site-001",
                                    waste_category_key="hw-paint", weight=100.0, seal="seal-001",
                                    custodian_party="zone-temp", origin_batch="batch-0901")
        # 授权拆分为两个 50kg 子容器。
        handover.authorize_transformation(request_id="req-auth", actor_id="operator-001",
                                          authorization_id="auth-001", operation="split",
                                          container_ids=["bin-001"])
        handover.split_container(request_id="req-split", actor_id="operator-001",
                                 authorization_id="auth-001", parent_container_id="bin-001",
                                 children=[{"container_id": "bin-001-a", "weight": 50.0,
                                            "seal": "seal-001a"},
                                           {"container_id": "bin-001-b", "weight": 50.0,
                                            "seal": "seal-001b"}])
        # 子容器 a 移交运输方，复称 50.4kg 超差，质量裁决后结案。
        handover.initiate_manifest(request_id="req-mf", actor_id="operator-001",
                                   manifest_id="mf-001", site_id="site-001",
                                   from_party="zone-temp", to_party="carrier-001",
                                   items=[{"container_id": "bin-001-a", "declared_weight": 50.0,
                                           "declared_seal": "seal-001a"}])
        discrepancy_raised = False
        try:
            handover.decide_manifest(request_id="req-dc", actor_id="operator-001",
                                     manifest_id="mf-001", decision="confirm",
                                     observations=[{"container_id": "bin-001-a",
                                                    "received_weight": 50.4,
                                                    "received_seal": "seal-001a"}])
        except DiscrepancyError:
            discrepancy_raised = True
        discrepancy_id = handover.get_manifest("mf-001")["discrepancies"][0]["discrepancy_id"]
        handover.rule_discrepancy(request_id="req-rd", actor_id="reviewer-001",
                                  discrepancy_id=discrepancy_id, ruling="accepted",
                                  receiver_statement="复称 50.4kg，封签完好",
                                  transferor_statement="出厂申报 50.0kg")
        handover.close_manifest(request_id="req-cl", actor_id="operator-001", manifest_id="mf-001")
        # 已结案联单不能被旧消息重新打开。
        reopen_blocked = False
        try:
            handover.decide_manifest(request_id="req-dc2", actor_id="operator-001",
                                     manifest_id="mf-001", decision="confirm",
                                     observations=[{"container_id": "bin-001-a",
                                                    "received_weight": 49.0,
                                                    "received_seal": "seal-x"}])
        except StateError:
            reopen_blocked = True
        trace = handover.trace_container("bin-001-a")
        valid, event_count = service.verify_audit()
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "discrepancy_raised": discrepancy_raised,
                  "reopen_blocked": reopen_blocked,
                  "trace_source": trace["ancestors"][0]["parent_container_id"],
                  "trace_destination": trace["final_destination"]["to_party"],
                  "final_weight": trace["container"]["current_weight"]}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"]
          and result["discrepancy_raised"] and result["reopen_blocked"]
          and result["trace_source"] == "bin-001"
          and result["trace_destination"] == "carrier-001")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
