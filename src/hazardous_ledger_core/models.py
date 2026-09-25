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
class Container:
    """具有稳定身份的危废容器及其当前保管状态。"""

    container_id: str
    site_id: str
    waste_category_key: str
    origin_batch: str | None
    status: str
    current_weight: float
    current_seal: str
    custodian_party: str
    custodian_actor: str
    version: int
    created_at: str


@dataclass(frozen=True)
class Manifest:
    """一次交出方发起、接收方确认的危废交接联单。"""

    manifest_id: str
    site_id: str
    from_party: str
    to_party: str
    tolerance_ratio: float
    status: str
    initiated_by: str
    initiated_at: str
    decided_by: str | None
    decided_at: str | None
    closed_by: str | None
    closed_at: str | None
    version: int


@dataclass(frozen=True)
class Discrepancy:
    """同一交接号下重量或封签不一致形成的差异记录。"""

    discrepancy_id: str
    manifest_id: str
    container_id: str
    declared_weight: float
    declared_seal: str
    received_weight: float
    received_seal: str
    level: str
    status: str
    weight_delta: float
    receiver_statement: str | None
    transferor_statement: str | None
    ruling: str | None
    ruled_by: str | None
    ruled_at: str | None
    created_at: str
