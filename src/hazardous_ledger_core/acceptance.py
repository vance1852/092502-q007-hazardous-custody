"""运行基础服务与危废交接账本的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .ledger import LedgerService
from .storage import Database


def run() -> dict[str, object]:
    """执行登记、容器、拆分、交接、差异裁决与追溯的完整链路。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = LedgerService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范企业")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="环保负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号生产场所", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="waste_category", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="waste_category", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        # ---- 交接账本链路：车间 -> 暂存区 -> 运输方 ----
        service.register_actor(request_id="req-actor-keeper", actor_id="admin-001",
                               new_actor_id="keeper-001", display_name="暂存区管理员",
                               role="operator", organization_id="org-001")
        service.register_actor(request_id="req-actor-reviewer", actor_id="admin-001",
                               new_actor_id="reviewer-001", display_name="质量工程师",
                               role="reviewer", organization_id="org-001")
        service.register_site(request_id="req-site-workshop", actor_id="operator-001", site_id="site-ws",
                              organization_id="org-001", name="喷涂车间", timezone_name="Asia/Shanghai")
        service.register_site(request_id="req-site-yard", actor_id="keeper-001", site_id="site-yard",
                              organization_id="org-001", name="危废暂存区", timezone_name="Asia/Shanghai")
        service.record_domain_data(request_id="req-wc-ws", actor_id="operator-001", site_id="site-ws",
                                   category="waste_category", external_key="HW06",
                                   data={"name": "废有机溶剂"})
        service.record_domain_data(request_id="req-zone-ws", actor_id="operator-001", site_id="site-ws",
                                   category="storage_zone", external_key="zone-ws-1",
                                   data={"name": "车间收集点"})
        service.record_domain_data(request_id="req-wc-yard", actor_id="keeper-001", site_id="site-yard",
                                   category="waste_category", external_key="HW06",
                                   data={"name": "废有机溶剂"})
        service.record_domain_data(request_id="req-zone-yard", actor_id="keeper-001", site_id="site-yard",
                                   category="storage_zone", external_key="zone-yard-1",
                                   data={"name": "暂存间一号位"})

        service.register_container(request_id="req-container", actor_id="operator-001", site_id="site-ws",
                                   container_id="drum-A", waste_category="HW06", net_weight_g=100000,
                                   seal_id="seal-A", zone_key="zone-ws-1", waste_name="废有机溶剂")

        # 授权拆分：A 拆成 A1/A2，执行后 A 关闭，子容器血缘可追溯
        grant = service.authorize_remix(request_id="req-grant-split", actor_id="reviewer-001",
                                        site_id="site-ws", kind="split",
                                        spec={"parent_container_id": "drum-A", "children": [
                                            {"container_id": "drum-A1", "net_weight_g": 59900,
                                             "seal_id": "seal-A1"},
                                            {"container_id": "drum-A2", "net_weight_g": 40000,
                                             "seal_id": "seal-A2"}]})
        service.execute_remix(request_id="req-split", actor_id="operator-001",
                              authorization_id=grant.resource_id)

        # A1 车间 -> 暂存区，复称差异在容差内：自动结案
        handover_one = service.initiate_handover(
            request_id="req-ho-1", actor_id="operator-001", from_site_id="site-ws",
            to_site_id="site-yard", from_zone_key="zone-ws-1", to_zone_key="zone-yard-1",
            items=[{"container_id": "drum-A1", "declared_weight_g": 59900, "declared_seal": "seal-A1"}])
        ho1 = handover_one.resource_id
        service.respond_handover(
            request_id="req-ho-1-ok", actor_id="keeper-001", handover_id=ho1, decision="accept",
            items=[{"container_id": "drum-A1", "received_weight_g": 59850,
                    "received_seal": "seal-A1"}])
        ho1_view = service.get_handover(ho1)

        # A2 车间 -> 暂存区，超差：进入差异，双方陈述后由质量人员裁决
        handover_two = service.initiate_handover(
            request_id="req-ho-2", actor_id="operator-001", from_site_id="site-ws",
            to_site_id="site-yard", from_zone_key="zone-ws-1", to_zone_key="zone-yard-1",
            items=[{"container_id": "drum-A2", "declared_weight_g": 40000, "declared_seal": "seal-A2"}])
        ho2 = handover_two.resource_id
        service.respond_handover(
            request_id="req-ho-2-diff", actor_id="keeper-001", handover_id=ho2, decision="accept",
            items=[{"container_id": "drum-A2", "received_weight_g": 34000,
                    "received_seal": "seal-A2"}])
        open_diff = service.list_discrepancies(handover_id=ho2, status="open")[0]
        # 重复确认只能返回原决定
        repeat = service.respond_handover(
            request_id="req-ho-2-diff", actor_id="keeper-001", handover_id=ho2, decision="accept",
            items=[{"container_id": "drum-A2", "received_weight_g": 34000,
                    "received_seal": "seal-A2"}])
        service.submit_statement(request_id="req-stmt-giver", actor_id="operator-001",
                                 handover_id=ho2, party="giver", statement="交出前复称40.0kg，封签完好")
        service.submit_statement(request_id="req-stmt-receiver", actor_id="keeper-001",
                                 handover_id=ho2, party="receiver", statement="到场复称34.0kg，封签完好")
        service.arbitrate_discrepancy(request_id="req-arb", actor_id="reviewer-001",
                                      discrepancy_id=open_diff.discrepancy_id,
                                      ruling="accept_received", note="按接收复称重量结案")
        ho2_view = service.get_handover(ho2)
        # 已结案联单收到旧的不同重量消息：登记晚期差异但联单保持关闭
        late_blocked = False
        try:
            service.respond_handover(
                request_id="req-ho-2-late", actor_id="keeper-001", handover_id=ho2, decision="accept",
                items=[{"container_id": "drum-A2", "received_weight_g": 32000,
                        "received_seal": "seal-A2"}])
        except Exception:
            late_blocked = True
        late_open = service.list_discrepancies(handover_id=ho2, status="late_open")

        # 从任一子容器反向还原来源、责任人、差异与去向
        trace = service.trace_container("drum-A2")
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "tolerance_handover_closed": ho1_view.status == "closed",
                  "tolerance_diff_auto_closed": ho1_view.discrepancies[0].status == "tolerance_closed",
                  "discrepancy_handover_closed": ho2_view.status == "closed",
                  "repeat_confirmation_replayed": repeat.replayed,
                  "late_message_blocked": late_blocked,
                  "late_discrepancy_recorded": len(late_open) == 1,
                  "trace_origin": [node["container_id"] for node in trace.origin],
                  "trace_custody_steps": len(trace.custody),
                  "trace_open_discrepancies": len(trace.open_discrepancies),
                  "trace_destination_site": trace.final_destination["site_id"]}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    required = ["status", "audit_valid", "tolerance_handover_closed",
                "tolerance_diff_auto_closed", "discrepancy_handover_closed",
                "repeat_confirmation_replayed", "late_message_blocked",
                "late_discrepancy_recorded"]
    ok = result["status"] == "ok" and all(result[key] for key in required) \
        and result["trace_origin"] == ["drum-A"] and result["trace_destination_site"] == "site-yard"
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
