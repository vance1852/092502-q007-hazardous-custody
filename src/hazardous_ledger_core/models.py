"""定义基础服务在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Actor:
    """表示具有明确角色的后台操作者。"""

    actor_id: str
    display_name: str
    role: str
    organization_id: str
    active: bool


@dataclass(frozen=True)
class Site:
    """表示企业或监管组织下的业务场所。"""

    site_id: str
    organization_id: str
    name: str
    timezone_name: str
    version: int


@dataclass(frozen=True)
class DomainRecord:
    """表示已经持久化的领域资料记录。"""

    record_id: str
    site_id: str
    category: str
    external_key: str
    payload: dict[str, Any]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class WriteReceipt:
    """描述一次幂等写入的稳定结果。"""

    request_id: str
    resource_type: str
    resource_id: str
    replayed: bool


@dataclass(frozen=True)
class LedgerReceipt:
    """描述一次账本写入及其响应载荷。"""

    request_id: str
    resource_type: str
    resource_id: str
    replayed: bool
    response: dict[str, Any]


@dataclass(frozen=True)
class Container:
    """表示具有稳定身份的危废容器。"""

    container_id: str
    site_id: str
    zone_key: str | None
    waste_category: str
    waste_name: str | None
    tare_g: int | None
    net_weight_g: int
    seal_id: str
    custodian_actor_id: str
    status: str
    origin_kind: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class Weighing:
    """描述容器的一次称重记录。"""

    weighing_id: str
    container_id: str
    context: str
    weight_g: int
    reference_id: str | None
    actor_id: str
    created_at: str


@dataclass(frozen=True)
class CustodyEventView:
    """描述一次保管责任变更。"""

    event_id: str
    container_id: str
    action: str
    site_id: str
    zone_key: str | None
    custodian_actor_id: str
    reference_id: str | None
    actor_id: str
    created_at: str


@dataclass(frozen=True)
class HandoverItemView:
    """描述交接联单中的一个容器条目。"""

    container_id: str
    position: int
    declared_weight_g: int
    declared_seal: str
    received_weight_g: int | None
    received_seal: str | None
    final_weight_g: int | None
    final_custodian_actor_id: str | None


@dataclass(frozen=True)
class DiscrepancyView:
    """描述一条重量或封签差异及其裁决状态。"""

    discrepancy_id: str
    handover_id: str
    container_id: str
    kind: str
    declared_weight_g: int
    received_weight_g: int
    declared_seal: str
    received_seal: str
    diff_g: int
    within_tolerance: bool
    blocking: bool
    late: bool
    status: str
    ruling: str | None
    final_weight_g: int | None
    resolved_by: str | None
    resolved_at: str | None
    created_at: str


@dataclass(frozen=True)
class HandoverView:
    """描述一份危废交接联单及其条目与差异。"""

    handover_id: str
    from_site_id: str
    to_site_id: str
    from_zone_key: str | None
    to_zone_key: str | None
    status: str
    basis: str | None
    note: str | None
    initiated_by: str
    responded_by: str | None
    declared_at: str
    decided_at: str | None
    closed_at: str | None
    items: tuple[HandoverItemView, ...]
    discrepancies: tuple[DiscrepancyView, ...]


@dataclass(frozen=True)
class LineageLink:
    """描述容器之间的一次拆分或合并血缘边。"""

    remix_id: str
    kind: str
    peer_container_id: str
    weight_g: int
    created_at: str


@dataclass(frozen=True)
class TraceReport:
    """汇总任一容器的来源、责任人、差异和最终去向。"""

    container: Container
    origin: tuple[dict[str, Any], ...]
    descendants: tuple[dict[str, Any], ...]
    custody: tuple[CustodyEventView, ...]
    weighings: tuple[Weighing, ...]
    open_discrepancies: tuple[DiscrepancyView, ...]
    final_destination: dict[str, Any] | None
