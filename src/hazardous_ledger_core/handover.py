"""危废交接账本：容器身份、拆分合并、联单交接与差异裁决。"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import (
    ConflictError,
    DiscrepancyError,
    NotFoundError,
    PermissionDenied,
    StateError,
    ValidationError,
)
from .models import Actor, Container, Discrepancy, Manifest
from .service import DomainService
from .storage import Database

# 拆分/合并允许的默认质量平衡偏差（0.5%）。
DEFAULT_BALANCE_TOLERANCE = 0.005
# 交接复称默认容差。
DEFAULT_HANDOVER_TOLERANCE = 0.005
PARTY_CATEGORIES = ("storage_zone", "custodian_profile", "carrier_profile")
TRANSFER_ROLES = ("admin", "operator")


class HandoverService:
    """在基础台账之上协调容器、联单、差异与溯源。"""

    def __init__(self, database: Database, domain: DomainService | None = None,
                 clock: Clock | None = None) -> None:
        self.database = database
        self.domain = domain or DomainService(database, clock=clock)
        self.clock = clock or self.domain.clock

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _identifier(self, value: str, field: str) -> str:
        return self.domain._identifier(value, field)

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        return self.domain._text(value, field, limit)

    def _weight(self, value: Any, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{field} 必须是正数重量")
        value = float(value)
        if value <= 0:
            raise ValidationError(f"{field} 必须是正数重量")
        return round(value, 6)

    def _ratio(self, value: Any, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{field} 必须是 0 到 1 之间的比例")
        value = float(value)
        if not 0.0 <= value <= 1.0:
            raise ValidationError(f"{field} 必须是 0 到 1 之间的比例")
        return value

    def _actor(self, connection, actor_id: str) -> Actor:
        return self.domain._actor(connection, actor_id)

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]):
        return self.domain._idempotent(connection, request_id=request_id, action=action,
                                       payload=payload, create=create)

    def _site(self, connection, actor: Actor, site_id: str):
        site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if site is None:
            raise NotFoundError("场所不存在")
        if actor.organization_id != site["organization_id"] and actor.role != "admin":
            raise PermissionDenied("不能操作其他组织的场所")
        return site

    def _category_exists(self, connection, site_id: str, external_key: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM domain_records WHERE site_id=? AND category='waste_category' AND external_key=?",
            (site_id, external_key),
        ).fetchone()
        if row is None:
            raise ValidationError("危废类别未在场所台账中登记")

    def _party_exists(self, connection, site_id: str, party: str) -> None:
        placeholders = ",".join("?" for _ in PARTY_CATEGORIES)
        row = connection.execute(
            f"SELECT 1 FROM domain_records WHERE site_id=? AND external_key=? "
            f"AND category IN ({placeholders})",
            (site_id, party, *PARTY_CATEGORIES),
        ).fetchone()
        if row is None:
            raise ValidationError(f"责任主体或场所未登记：{party}")

    def _container_row(self, connection, container_id: str):
        row = connection.execute("SELECT * FROM containers WHERE container_id=?", (container_id,)).fetchone()
        if row is None:
            raise NotFoundError("容器不存在")
        return row

    def _container(self, row) -> Container:
        return Container(row["container_id"], row["site_id"], row["waste_category_key"],
                         row["origin_batch"], row["status"], row["current_weight"],
                         row["current_seal"], row["custodian_party"], row["custodian_actor"],
                         row["version"], row["created_at"])

    def _container_event(self, connection, *, container_id: str, event_type: str, actor_id: str,
                         manifest_id: str | None = None, weight: float | None = None,
                         seal: str | None = None, from_party: str | None = None,
                         to_party: str | None = None, detail: dict[str, Any] | None = None) -> None:
        connection.execute(
            "INSERT INTO container_events(container_id,event_type,manifest_id,weight,seal,"
            "from_party,to_party,actor_id,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (container_id, event_type, manifest_id, weight, seal, from_party, to_party,
             actor_id, canonical_json(detail or {}), self._now()),
        )

    # ----------------------------------------------------------- 容器建档

    def register_container(self, *, request_id: str, actor_id: str, container_id: str,
                           site_id: str, waste_category_key: str, weight: float, seal: str,
                           custodian_party: str, origin_batch: str | None = None) -> Any:
        """为一个物理容器分配稳定身份并记录首次装载、封签与保管责任。"""

        container_id_in = container_id
        payload = {"actor_id": actor_id, "container_id": container_id, "site_id": site_id,
                   "waste_category_key": waste_category_key, "weight": weight, "seal": seal,
                   "custodian_party": custodian_party, "origin_batch": origin_batch}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.domain._require(actor, *TRANSFER_ROLES)
            self._site(connection, actor, site_id)
            container_id = self._identifier(container_id_in, "container_id")
            waste_category_key = self._identifier(waste_category_key, "waste_category_key")
            custodian_party = self._identifier(custodian_party, "custodian_party")
            weight = self._weight(weight, "weight")
            seal = self._text(seal, "seal", 120)
            if origin_batch is not None:
                origin_batch = self._text(origin_batch, "origin_batch", 120)
            self._category_exists(connection, site_id, waste_category_key)
            self._party_exists(connection, site_id, custodian_party)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO containers(container_id,site_id,waste_category_key,origin_batch,"
                        "status,current_weight,current_seal,custodian_party,custodian_actor,version,created_at) "
                        "VALUES(?,?,?,?,'open',?,?,?,?,1,?)",
                        (container_id, site_id, waste_category_key, origin_batch, weight, seal,
                         custodian_party, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("容器编号已经存在") from exc
                self._container_event(
                    connection, container_id=container_id, event_type="registered",
                    actor_id=actor_id, weight=weight, seal=seal, to_party=custodian_party,
                    detail={"waste_category_key": waste_category_key, "origin_batch": origin_batch},
                )
                append_event(connection, actor_id=actor_id, action="container.registered",
                             resource_type="container", resource_id=container_id,
                             detail={"site_id": site_id, "waste_category_key": waste_category_key,
                                     "weight": weight, "seal": seal, "custodian_party": custodian_party},
                             occurred_at=self._now())
                return "container", container_id, {"container_id": container_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_container", payload=payload, create=create)

    # ------------------------------------------------- 拆分/合并授权与执行

    def authorize_transformation(self, *, request_id: str, actor_id: str, authorization_id: str,
                                 operation: str, container_ids: list[str]) -> Any:
        """由有权人员签发一次性的拆分或合并授权。"""

        payload = {"actor_id": actor_id, "authorization_id": authorization_id,
                   "operation": operation, "container_ids": list(container_ids)}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.domain._require(actor, *TRANSFER_ROLES)
            if operation not in ("split", "merge"):
                raise ValidationError("operation 只能是 split 或 merge")
            if not isinstance(container_ids, list) or not container_ids:
                raise ValidationError("container_ids 必须是非空列表")
            ids = [self._identifier(value, "container_id") for value in container_ids]
            if len(set(ids)) != len(ids):
                raise ValidationError("容器编号在授权中重复")
            for container_id in ids:
                row = self._container_row(connection, container_id)
                if row["status"] != "open":
                    raise StateError(f"容器 {container_id} 已被拆分或合并，不能再次授权")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO transformation_authorizations(authorization_id,operation,"
                        "container_ids_json,status,authorized_by,created_at) VALUES(?,?,?,'issued',?,?)",
                        (self._identifier(authorization_id, "authorization_id"), operation,
                         canonical_json(ids), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("授权编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="transformation.authorized",
                             resource_type="transformation_authorization", resource_id=authorization_id,
                             detail={"operation": operation, "container_ids": ids},
                             occurred_at=self._now())
                return "transformation_authorization", authorization_id, {"authorization_id": authorization_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="authorize_transformation", payload=payload, create=create)

    def _load_authorization(self, connection, authorization_id: str, operation: str,
                            expected_ids: set[str]) -> str:
        row = connection.execute(
            "SELECT * FROM transformation_authorizations WHERE authorization_id=?",
            (authorization_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("授权不存在")
        if row["operation"] != operation:
            raise ValidationError("授权操作类型与实际不符")
        if row["status"] == "consumed":
            raise ConflictError("授权已经被使用")
        if row["status"] == "void":
            raise StateError("授权已作废")
        authorized = set(json.loads(row["container_ids_json"]))
        if not expected_ids.issubset(authorized):
            raise PermissionDenied("容器不在本次授权范围内")
        return authorization_id

    def _new_child_container(self, connection, *, container_id: str, site_id: str,
                             waste_category_key: str, origin_batch: str | None,
                             weight: float, seal: str, custodian_party: str,
                             custodian_actor: str) -> None:
        try:
            connection.execute(
                "INSERT INTO containers(container_id,site_id,waste_category_key,origin_batch,"
                "status,current_weight,current_seal,custodian_party,custodian_actor,version,created_at) "
                "VALUES(?,?,?,?,'open',?,?,?,?,1,?)",
                (container_id, site_id, waste_category_key, origin_batch, weight, seal,
                 custodian_party, custodian_actor, self._now()),
            )
        except Exception as exc:
            raise ConflictError(f"容器编号已经存在：{container_id}") from exc

    def split_container(self, *, request_id: str, actor_id: str, authorization_id: str,
                        parent_container_id: str, children: list[dict[str, Any]],
                        tolerance_ratio: float = DEFAULT_BALANCE_TOLERANCE) -> Any:
        """在授权下把一个容器拆分为多个带新身份与封签的子容器。"""

        payload = {"actor_id": actor_id, "authorization_id": authorization_id,
                   "parent_container_id": parent_container_id, "children": children,
                   "tolerance_ratio": tolerance_ratio}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.domain._require(actor, *TRANSFER_ROLES)
            parent_id = self._identifier(parent_container_id, "parent_container_id")
            tolerance_ratio = self._ratio(tolerance_ratio, "tolerance_ratio")
            if not isinstance(children, list) or len(children) < 2:
                raise ValidationError("拆分至少需要两个子容器")
            normalized: list[dict[str, Any]] = []
            child_ids: set[str] = set()
            for child in children:
                if not isinstance(child, dict):
                    raise ValidationError("children 必须是对象列表")
                child_id = self._identifier(child.get("container_id", ""), "container_id")
                if child_id in child_ids:
                    raise ValidationError(f"子容器编号重复：{child_id}")
                child_ids.add(child_id)
                normalized.append({"container_id": child_id,
                                   "weight": self._weight(child.get("weight"), "weight"),
                                   "seal": self._text(child.get("seal", ""), "seal", 120)})
            parent = self._container_row(connection, parent_id)
            if parent_id in child_ids:
                raise ValidationError("子容器不能与父容器使用同一编号")
            self._load_authorization(connection, self._identifier(authorization_id, "authorization_id"),
                                     "split", {parent_id})
            if parent["status"] != "open":
                raise StateError("父容器已经被拆分或合并")
            parent_weight = parent["current_weight"]
            child_total = round(sum(item["weight"] for item in normalized), 6)
            balance_delta = round(child_total - parent_weight, 6)
            if abs(balance_delta) > parent_weight * tolerance_ratio + 1e-9:
                raise ValidationError(
                    f"拆分质量不平衡：子容器合计 {child_total} 与父容器 {parent_weight} 超出容差")

            def create() -> tuple[str, str, dict[str, Any]]:
                for item in normalized:
                    self._new_child_container(
                        connection, container_id=item["container_id"], site_id=parent["site_id"],
                        waste_category_key=parent["waste_category_key"],
                        origin_batch=parent["origin_batch"], weight=item["weight"], seal=item["seal"],
                        custodian_party=parent["custodian_party"],
                        custodian_actor=parent["custodian_actor"])
                    edge_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO container_lineage(edge_id,parent_container_id,child_container_id,"
                        "operation,parent_weight,child_weight,weight_delta,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (edge_id, parent_id, item["container_id"], "split", parent_weight,
                         item["weight"], round(item["weight"] - parent_weight, 6),
                         actor_id, self._now()))
                    self._container_event(
                        connection, container_id=item["container_id"], event_type="split_out",
                        actor_id=actor_id, weight=item["weight"], seal=item["seal"],
                        from_party=parent["custodian_party"], to_party=parent["custodian_party"],
                        detail={"parent_container_id": parent_id, "edge_id": edge_id})
                connection.execute(
                    "UPDATE containers SET status='consumed',version=version+1 WHERE container_id=?",
                    (parent_id,))
                connection.execute(
                    "UPDATE transformation_authorizations SET status='consumed',consumed_at=? "
                    "WHERE authorization_id=?",
                    (self._now(), authorization_id))
                self._container_event(
                    connection, container_id=parent_id, event_type="split_in",
                    actor_id=actor_id, weight=parent_weight, seal=parent["current_seal"],
                    from_party=parent["custodian_party"], to_party=parent["custodian_party"],
                    detail={"children": [item["container_id"] for item in normalized],
                            "parent_weight": parent_weight, "children_total": child_total,
                            "balance_delta": balance_delta})
                append_event(connection, actor_id=actor_id, action="container.split",
                             resource_type="container", resource_id=parent_id,
                             detail={"authorization_id": authorization_id, "parent_weight": parent_weight,
                                     "children": [item["container_id"] for item in normalized],
                                     "children_weights": [item["weight"] for item in normalized],
                                     "balance_delta": balance_delta},
                             occurred_at=self._now())
                return "split", parent_id, {"parent_container_id": parent_id,
                                            "child_container_ids": list(child_ids)}

            return self._idempotent(connection, request_id=request_id,
                                    action="split_container", payload=payload, create=create)

    def merge_containers(self, *, request_id: str, actor_id: str, authorization_id: str,
                         container_ids: list[str], result_container_id: str, seal: str,
                         weight: float, tolerance_ratio: float = DEFAULT_BALANCE_TOLERANCE) -> Any:
        """在授权下把同类危废的多个容器合并为一个新容器，保留全部来源血缘。"""

        payload = {"actor_id": actor_id, "authorization_id": authorization_id,
                   "container_ids": list(container_ids), "result_container_id": result_container_id,
                   "seal": seal, "weight": weight, "tolerance_ratio": tolerance_ratio}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.domain._require(actor, *TRANSFER_ROLES)
            tolerance_ratio = self._ratio(tolerance_ratio, "tolerance_ratio")
            if not isinstance(container_ids, list) or len(container_ids) < 2:
                raise ValidationError("合并至少需要两个源容器")
            parent_ids = [self._identifier(value, "container_ids") for value in container_ids]
            if len(set(parent_ids)) != len(parent_ids):
                raise ValidationError("源容器编号重复")
            result_id = self._identifier(result_container_id, "result_container_id")
            if result_id in parent_ids:
                raise ValidationError("合并结果容器不能与源容器使用同一编号")
            seal = self._text(seal, "seal", 120)
            weight = self._weight(weight, "weight")
            parents = [self._container_row(connection, parent_id) for parent_id in parent_ids]
            categories = {row["waste_category_key"] for row in parents}
            if len(categories) != 1:
                raise ValidationError("只能合并相同危废类别的容器")
            sites = {row["site_id"] for row in parents}
            if len(sites) != 1:
                raise ValidationError("不能跨场所合并容器")
            custodians = {(row["custodian_party"], row["custodian_actor"]) for row in parents}
            if len(custodians) != 1:
                raise StateError("源容器保管责任不一致，不能直接合并")
            if any(row["status"] != "open" for row in parents):
                raise StateError("源容器已经被拆分或合并")
            self._load_authorization(connection, self._identifier(authorization_id, "authorization_id"),
                                     "merge", set(parent_ids))
            parent_total = round(sum(row["current_weight"] for row in parents), 6)
            balance_delta = round(weight - parent_total, 6)
            if abs(balance_delta) > parent_total * tolerance_ratio + 1e-9:
                raise ValidationError(
                    f"合并质量不平衡：结果 {weight} 与源容器合计 {parent_total} 超出容差")
            site_id = parents[0]["site_id"]
            category = next(iter(categories))
            custodian_party, custodian_actor = next(iter(custodians))
            origin_batch = next((row["origin_batch"] for row in parents if row["origin_batch"]), None)

            def create() -> tuple[str, str, dict[str, Any]]:
                self._new_child_container(
                    connection, container_id=result_id, site_id=site_id,
                    waste_category_key=category, origin_batch=origin_batch, weight=weight,
                    seal=seal, custodian_party=custodian_party, custodian_actor=custodian_actor)
                for row in parents:
                    edge_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO container_lineage(edge_id,parent_container_id,child_container_id,"
                        "operation,parent_weight,child_weight,weight_delta,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (edge_id, row["container_id"], result_id, "merge", row["current_weight"],
                         weight, round(weight - row["current_weight"], 6), actor_id, self._now()))
                    connection.execute(
                        "UPDATE containers SET status='consumed',version=version+1 WHERE container_id=?",
                        (row["container_id"],))
                    self._container_event(
                        connection, container_id=row["container_id"], event_type="merge_in",
                        actor_id=actor_id, weight=row["current_weight"], seal=row["current_seal"],
                        from_party=custodian_party, to_party=custodian_party,
                        detail={"result_container_id": result_id, "edge_id": edge_id})
                connection.execute(
                    "UPDATE transformation_authorizations SET status='consumed',consumed_at=? "
                    "WHERE authorization_id=?",
                    (self._now(), authorization_id))
                self._container_event(
                    connection, container_id=result_id, event_type="merge_out",
                    actor_id=actor_id, weight=weight, seal=seal,
                    from_party=custodian_party, to_party=custodian_party,
                    detail={"parents": parent_ids, "parents_total": parent_total,
                            "balance_delta": balance_delta})
                append_event(connection, actor_id=actor_id, action="container.merge",
                             resource_type="container", resource_id=result_id,
                             detail={"authorization_id": authorization_id, "parents": parent_ids,
                                     "parents_total": parent_total, "result_weight": weight,
                                     "balance_delta": balance_delta},
                             occurred_at=self._now())
                return "merge", result_id, {"result_container_id": result_id,
                                            "parent_container_ids": parent_ids}

            return self._idempotent(connection, request_id=request_id,
                                    action="merge_containers", payload=payload, create=create)

    # --------------------------------------------------------------- 联单

    def initiate_manifest(self, *, request_id: str, actor_id: str, manifest_id: str,
                          site_id: str, from_party: str, to_party: str,
                          items: list[dict[str, Any]],
                          tolerance_ratio: float = DEFAULT_HANDOVER_TOLERANCE) -> Any:
        """交出方发起联单，申报容器、重量与封签。"""

        payload = {"actor_id": actor_id, "manifest_id": manifest_id, "site_id": site_id,
                   "from_party": from_party, "to_party": to_party, "items": items,
                   "tolerance_ratio": tolerance_ratio}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.domain._require(actor, *TRANSFER_ROLES)
            self._site(connection, actor, site_id)
            manifest_id = self._identifier(manifest_id, "manifest_id")
            from_party = self._identifier(from_party, "from_party")
            to_party = self._identifier(to_party, "to_party")
            if from_party == to_party:
                raise ValidationError("交出方与接收方不能相同")
            self._party_exists(connection, site_id, from_party)
            self._party_exists(connection, site_id, to_party)
            tolerance_ratio = self._ratio(tolerance_ratio, "tolerance_ratio")
            if not isinstance(items, list) or not items:
                raise ValidationError("items 必须是非空列表")
            normalized: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in items:
                if not isinstance(item, dict):
                    raise ValidationError("items 必须是对象列表")
                container_id = self._identifier(item.get("container_id", ""), "container_id")
                if container_id in seen:
                    raise ValidationError(f"联单内容器重复：{container_id}")
                seen.add(container_id)
                row = self._container_row(connection, container_id)
                if row["site_id"] != site_id:
                    raise ValidationError(f"容器 {container_id} 不属于该场所")
                if row["status"] != "open":
                    raise StateError(f"容器 {container_id} 已被拆分或合并，不能交接")
                if row["custodian_party"] != from_party:
                    raise PermissionDenied(
                        f"容器 {container_id} 当前保管方为 {row['custodian_party']}，交出方不匹配")
                declared_weight = self._weight(item.get("declared_weight"), "declared_weight")
                declared_seal = self._text(item.get("declared_seal", ""), "declared_seal", 120)
                # 申报必须与账本当前装载一致，差异只能产生在接收方复称环节。
                if declared_weight != row["current_weight"]:
                    raise StateError(
                        f"容器 {container_id} 申报重量 {declared_weight} 与台账重量 "
                        f"{row['current_weight']} 不一致")
                if declared_seal != row["current_seal"]:
                    raise StateError(f"容器 {container_id} 申报封签与台账封签不一致")
                normalized.append({"container_id": container_id, "declared_weight": declared_weight,
                                   "declared_seal": declared_seal})

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO manifests(manifest_id,site_id,from_party,to_party,tolerance_ratio,"
                        "status,initiated_by,initiated_at,version) VALUES(?,?,?,?,?,'initiated',?,?,1)",
                        (manifest_id, site_id, from_party, to_party, tolerance_ratio,
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("联单编号已经存在") from exc
                for item in normalized:
                    connection.execute(
                        "INSERT INTO manifest_items(manifest_id,container_id,declared_weight,declared_seal,"
                        "item_status) VALUES(?,?,?,?,'pending')",
                        (manifest_id, item["container_id"], item["declared_weight"],
                         item["declared_seal"]))
                    self._container_event(
                        connection, container_id=item["container_id"], event_type="manifest_initiated",
                        manifest_id=manifest_id, actor_id=actor_id,
                        weight=item["declared_weight"], seal=item["declared_seal"],
                        from_party=from_party, to_party=to_party)
                append_event(connection, actor_id=actor_id, action="manifest.initiated",
                             resource_type="manifest", resource_id=manifest_id,
                             detail={"site_id": site_id, "from_party": from_party, "to_party": to_party,
                                     "containers": [item["container_id"] for item in normalized],
                                     "tolerance_ratio": tolerance_ratio},
                             occurred_at=self._now())
                return "manifest", manifest_id, {"manifest_id": manifest_id, "status": "initiated"}

            return self._idempotent(connection, request_id=request_id,
                                    action="initiate_manifest", payload=payload, create=create)

    def _level(self, *, declared_weight: float, received_weight: float, declared_seal: str,
               received_seal: str, tolerance_ratio: float) -> tuple[str, float]:
        weight_delta = round(received_weight - declared_weight, 6)
        if received_seal != declared_seal:
            return "seal_mismatch", weight_delta
        if weight_delta == 0.0:
            return "none", 0.0
        if abs(weight_delta) <= declared_weight * tolerance_ratio + 1e-9:
            return "within_tolerance", weight_delta
        return "over_tolerance", weight_delta

    def _create_discrepancy(self, connection, *, manifest_row, item_row, level: str,
                            weight_delta: float, received_weight: float,
                            received_seal: str) -> str:
        discrepancy_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO discrepancies(discrepancy_id,manifest_id,container_id,declared_weight,"
            "declared_seal,received_weight,received_seal,level,status,weight_delta,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,'open',?,?)",
            (discrepancy_id, manifest_row["manifest_id"], item_row["container_id"],
             item_row["declared_weight"], item_row["declared_seal"], received_weight,
             received_seal, level, weight_delta, self._now()))
        return discrepancy_id

    def decide_manifest(self, *, request_id: str, actor_id: str, manifest_id: str,
                        decision: str, observations: list[dict[str, Any]] | None = None,
                        statement: str | None = None) -> Any:
        """接收方确认或拒收。重复确认只返回原决定；不同重量或封签进入差异处理。

        差异状态随事务一并落库，事务提交后再以 DiscrepancyError 告知调用方，
        因此差异记录不会因为异常而回滚。
        """

        payload = {"actor_id": actor_id, "manifest_id": manifest_id, "decision": decision,
                   "observations": observations, "statement": statement}
        result_flag = {"discrepancy": False}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.domain._require(actor, *TRANSFER_ROLES, "reviewer")
            manifest_id = self._identifier(manifest_id, "manifest_id")
            if decision not in ("confirm", "reject"):
                raise ValidationError("decision 只能是 confirm 或 reject")
            manifest = connection.execute("SELECT * FROM manifests WHERE manifest_id=?",
                                          (manifest_id,)).fetchone()
            if manifest is None:
                raise NotFoundError("联单不存在")
            # 已关闭联单不能被旧消息重新打开。
            if manifest["status"] == "closed":
                raise StateError("联单已结案，旧消息不能重新打开")
            item_rows = connection.execute(
                "SELECT * FROM manifest_items WHERE manifest_id=?", (manifest_id,)
            ).fetchall()
            observations = observations or []
            observed: dict[str, dict[str, Any]] = {}
            for observation in observations:
                if not isinstance(observation, dict):
                    raise ValidationError("observations 必须是对象列表")
                container_id = self._identifier(observation.get("container_id", ""), "container_id")
                observed[container_id] = {
                    "received_weight": self._weight(observation.get("received_weight"),
                                                     "received_weight"),
                    "received_seal": self._text(observation.get("received_seal", ""),
                                                "received_seal", 120),
                }

            def replay_original() -> tuple[str, str, dict[str, Any]]:
                return "manifest", manifest_id, {"manifest_id": manifest_id,
                                                 "status": manifest["status"], "replayed": True}

            if decision == "reject":
                # 拒收：保留接收方陈述，保管责任不发生变化。重复拒收只返回原决定。
                if manifest["status"] in ("rejected", "confirmed", "discrepancy"):
                    if manifest["status"] == "rejected":
                        return self._idempotent(
                            connection, request_id=request_id, action="decide_manifest",
                            payload=payload, create=replay_original)
                    raise StateError("联单已进入确认或差异处理，不能再改为拒收")
                statement_text = self._text(statement or "", "statement", 1000) if statement else None

                def create_reject() -> tuple[str, str, dict[str, Any]]:
                    connection.execute(
                        "UPDATE manifests SET status='rejected',decided_by=?,decided_at=?,version=version+1 "
                        "WHERE manifest_id=?",
                        (actor_id, self._now(), manifest_id))
                    connection.execute(
                        "UPDATE manifest_items SET item_status='rejected' WHERE manifest_id=?",
                        (manifest_id,))
                    for item in item_rows:
                        self._container_event(
                            connection, container_id=item["container_id"], event_type="manifest_rejected",
                            manifest_id=manifest_id, actor_id=actor_id,
                            weight=item["declared_weight"], seal=item["declared_seal"],
                            from_party=manifest["from_party"], to_party=manifest["to_party"],
                            detail={"statement": statement_text})
                    append_event(connection, actor_id=actor_id, action="manifest.rejected",
                                 resource_type="manifest", resource_id=manifest_id,
                                 detail={"statement": statement_text}, occurred_at=self._now())
                    return "manifest", manifest_id, {"manifest_id": manifest_id, "status": "rejected"}

                return self._idempotent(connection, request_id=request_id, action="decide_manifest",
                                        payload=payload, create=create_reject)

            # decision == confirm
            missing = [row["container_id"] for row in item_rows
                       if row["container_id"] not in observed]
            if missing:
                raise ValidationError(f"缺少容器的复称记录：{', '.join(missing)}")
            extra = [cid for cid in observed if cid not in {row["container_id"] for row in item_rows}]
            if extra:
                raise ValidationError(f"复称记录包含联单外容器：{', '.join(extra)}")

            # 联单已经做出过决定：相同复称结果只返回原决定；不同重量或封签必须进入差异处理。
            if manifest["status"] in ("confirmed", "discrepancy"):
                same = all(
                    observed[row["container_id"]]["received_weight"] == (row["received_weight"] or 0)
                    and observed[row["container_id"]]["received_seal"] == (row["received_seal"] or "")
                    for row in item_rows)
                if same:
                    result_flag["discrepancy"] = manifest["status"] == "discrepancy"
                    receipt = self._idempotent(
                        connection, request_id=request_id, action="decide_manifest",
                        payload=payload, create=replay_original)
                else:
                    receipt = self._confirm(connection, actor_id=actor_id, manifest=manifest,
                                            item_rows=item_rows, observed=observed,
                                            request_id=request_id, payload=payload,
                                            result_flag=result_flag)
            else:
                receipt = self._confirm(connection, actor_id=actor_id, manifest=manifest,
                                        item_rows=item_rows, observed=observed,
                                        request_id=request_id, payload=payload,
                                        result_flag=result_flag)
        # 事务已提交：差异状态已持久化，再在事务外以业务异常告知调用方。
        if result_flag["discrepancy"]:
            raise DiscrepancyError(
                f"联单 {manifest_id} 存在重量或封签差异，容差内可结案，超差或封签不符需质量人员裁决")
        return receipt

    def _confirm(self, connection, *, actor_id: str, manifest, item_rows,
                 observed: dict[str, dict[str, Any]], request_id: str,
                 payload: dict[str, Any], result_flag: dict[str, bool]) -> Any:
        """在事务内落实确认结果：逐项比对、登记差异、转移无差异容器。"""

        manifest_id = manifest["manifest_id"]
        levels: list[tuple[Any, str, float, dict[str, Any]]] = []
        for row in item_rows:
            observation = observed[row["container_id"]]
            level, delta = self._level(
                declared_weight=row["declared_weight"],
                received_weight=observation["received_weight"],
                declared_seal=row["declared_seal"],
                received_seal=observation["received_seal"],
                tolerance_ratio=manifest["tolerance_ratio"])
            levels.append((row, level, delta, observation))

        def create_confirm() -> tuple[str, str, dict[str, Any]]:
            has_discrepancy = any(level != "none" for _, level, _, _ in levels)
            new_status = "discrepancy" if has_discrepancy else "confirmed"
            discrepancy_ids: list[str] = []
            for row, level, delta, observation in levels:
                connection.execute(
                    "UPDATE manifest_items SET received_weight=?,received_seal=?,item_status=?,"
                    "discrepancy_level=? WHERE manifest_id=? AND container_id=?",
                    (observation["received_weight"], observation["received_seal"],
                     "disputed" if level != "none" else "confirmed", level,
                     manifest_id, row["container_id"]))
                if level != "none":
                    existing = connection.execute(
                        "SELECT discrepancy_id,status FROM discrepancies "
                        "WHERE manifest_id=? AND container_id=?",
                        (manifest_id, row["container_id"])).fetchone()
                    if existing is None:
                        discrepancy_id = self._create_discrepancy(
                            connection, manifest_row=manifest, item_row=row, level=level,
                            weight_delta=delta,
                            received_weight=observation["received_weight"],
                            received_seal=observation["received_seal"])
                        discrepancy_ids.append(discrepancy_id)
                    elif existing["status"] == "open":
                        # 同一交接号下再次提交不同复称：以最新复称刷新未决差异。
                        connection.execute(
                            "UPDATE discrepancies SET received_weight=?,received_seal=?,level=?,"
                            "weight_delta=? WHERE discrepancy_id=?",
                            (observation["received_weight"], observation["received_seal"],
                             level, delta, existing["discrepancy_id"]))
                        discrepancy_ids.append(existing["discrepancy_id"])
                    else:
                        # 旧差异已经裁决，新的复称差异另立记录、再次进入裁决。
                        discrepancy_id = self._create_discrepancy(
                            connection, manifest_row=manifest, item_row=row, level=level,
                            weight_delta=delta,
                            received_weight=observation["received_weight"],
                            received_seal=observation["received_seal"])
                        discrepancy_ids.append(discrepancy_id)
                self._container_event(
                    connection, container_id=row["container_id"],
                    event_type="manifest_discrepancy" if level != "none" else "manifest_confirmed",
                    manifest_id=manifest_id, actor_id=actor_id,
                    weight=observation["received_weight"], seal=observation["received_seal"],
                    from_party=manifest["from_party"], to_party=manifest["to_party"],
                    detail={"level": level, "weight_delta": delta,
                            "declared_weight": row["declared_weight"],
                            "declared_seal": row["declared_seal"]})
                if level == "none":
                    # 无差异即转移保管责任；有差异的容器待容差结案或质量裁决后再定。
                    connection.execute(
                        "UPDATE containers SET current_weight=?,current_seal=?,custodian_party=?,"
                        "custodian_actor=?,version=version+1 WHERE container_id=?",
                        (observation["received_weight"], observation["received_seal"],
                         manifest["to_party"], actor_id, row["container_id"]))
            connection.execute(
                "UPDATE manifests SET status=?,decided_by=?,decided_at=?,version=version+1 "
                "WHERE manifest_id=?",
                (new_status, actor_id, self._now(), manifest_id))
            append_event(connection, actor_id=actor_id,
                         action="manifest.discrepancy" if has_discrepancy else "manifest.confirmed",
                         resource_type="manifest", resource_id=manifest_id,
                         detail={"discrepancy_ids": discrepancy_ids,
                                 "items": [{"container_id": row["container_id"], "level": level,
                                            "weight_delta": delta}
                                           for row, level, delta, _ in levels]},
                         occurred_at=self._now())
            result_flag["discrepancy"] = has_discrepancy
            return "manifest", manifest_id, {"manifest_id": manifest_id, "status": new_status}

        return self._idempotent(connection, request_id=request_id, action="decide_manifest",
                                payload=payload, create=create_confirm)

    def rule_discrepancy(self, *, request_id: str, actor_id: str, discrepancy_id: str,
                         ruling: str, receiver_statement: str | None = None,
                         transferor_statement: str | None = None) -> Any:
        """质量人员对超差或封签差异作出裁决，并保留双方陈述。"""

        payload = {"actor_id": actor_id, "discrepancy_id": discrepancy_id, "ruling": ruling,
                   "receiver_statement": receiver_statement,
                   "transferor_statement": transferor_statement}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.domain._require(actor, "reviewer", "admin")
            discrepancy_id = self._identifier(discrepancy_id, "discrepancy_id")
            if ruling not in ("accepted", "rejected"):
                raise ValidationError("ruling 只能是 accepted 或 rejected")
            row = connection.execute("SELECT * FROM discrepancies WHERE discrepancy_id=?",
                                     (discrepancy_id,)).fetchone()
            if row is None:
                raise NotFoundError("差异记录不存在")
            if row["status"] != "open":
                raise StateError("差异已经裁决，不能重复裁决")
            manifest = connection.execute("SELECT * FROM manifests WHERE manifest_id=?",
                                          (row["manifest_id"],)).fetchone()
            if manifest["status"] == "closed":
                raise StateError("联单已结案，旧消息不能重新打开差异")
            receiver_text = (self._text(receiver_statement or "", "receiver_statement", 1000)
                             if receiver_statement else None)
            transferor_text = (self._text(transferor_statement or "", "transferor_statement", 1000)
                               if transferor_statement else None)
            ruling_text = self._text(ruling, "ruling", 120)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE discrepancies SET status=?,ruling=?,receiver_statement=?,"
                    "transferor_statement=?,ruled_by=?,ruled_at=? WHERE discrepancy_id=?",
                    ("accepted" if ruling == "accepted" else "rejected", ruling_text,
                     receiver_text, transferor_text, actor_id, self._now(), discrepancy_id))
                if ruling == "accepted":
                    # 采信接收方复称：容器以复称重量与封签转移给接收方。
                    connection.execute(
                        "UPDATE containers SET current_weight=?,current_seal=?,custodian_party=?,"
                        "custodian_actor=?,version=version+1 WHERE container_id=?",
                        (row["received_weight"], row["received_seal"], manifest["to_party"],
                         manifest["decided_by"] or actor_id, row["container_id"]))
                else:
                    # 不采信差异：容器维持申报重量与封签，保管责任回到交出方。
                    connection.execute(
                        "UPDATE containers SET current_weight=?,current_seal=?,custodian_party=?,"
                        "custodian_actor=?,version=version+1 WHERE container_id=?",
                        (row["declared_weight"], row["declared_seal"], manifest["from_party"],
                         manifest["initiated_by"], row["container_id"]))
                connection.execute(
                    "UPDATE manifest_items SET item_status='confirmed',discrepancy_level=? "
                    "WHERE manifest_id=? AND container_id=?",
                    (row["level"], row["manifest_id"], row["container_id"]))
                self._container_event(
                    connection, container_id=row["container_id"], event_type="discrepancy_ruled",
                    manifest_id=row["manifest_id"], actor_id=actor_id,
                    weight=row["received_weight"] if ruling == "accepted" else row["declared_weight"],
                    seal=row["received_seal"] if ruling == "accepted" else row["declared_seal"],
                    from_party=manifest["from_party"], to_party=manifest["to_party"],
                    detail={"ruling": ruling, "level": row["level"],
                            "receiver_statement": receiver_text,
                            "transferor_statement": transferor_text})
                append_event(connection, actor_id=actor_id, action="discrepancy.ruled",
                             resource_type="discrepancy", resource_id=discrepancy_id,
                             detail={"manifest_id": row["manifest_id"],
                                     "container_id": row["container_id"], "ruling": ruling,
                                     "level": row["level"]},
                             occurred_at=self._now())
                return "discrepancy", discrepancy_id, {"discrepancy_id": discrepancy_id,
                                                       "ruling": ruling}

            return self._idempotent(connection, request_id=request_id, action="rule_discrepancy",
                                    payload=payload, create=create)

    def close_manifest(self, *, request_id: str, actor_id: str, manifest_id: str) -> Any:
        """结案联单：无差异或仅容差差异可直接结案，超差/封签差异须先经质量裁决。"""

        payload = {"actor_id": actor_id, "manifest_id": manifest_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.domain._require(actor, *TRANSFER_ROLES, "reviewer")
            manifest_id = self._identifier(manifest_id, "manifest_id")
            manifest = connection.execute("SELECT * FROM manifests WHERE manifest_id=?",
                                          (manifest_id,)).fetchone()
            if manifest is None:
                raise NotFoundError("联单不存在")
            if manifest["status"] == "closed":
                # 重复结案只返回原决定。
                def replay_closed() -> tuple[str, str, dict[str, Any]]:
                    return "manifest", manifest_id, {"manifest_id": manifest_id,
                                                     "status": "closed", "replayed": True}
                return self._idempotent(connection, request_id=request_id, action="close_manifest",
                                        payload=payload, create=replay_closed)
            if manifest["status"] == "initiated":
                raise StateError("接收方尚未确认，不能结案")
            if manifest["status"] == "rejected":
                allowed = True
                unresolved: list[str] = []
            else:
                unresolved_rows = connection.execute(
                    "SELECT container_id,level FROM discrepancies WHERE manifest_id=? AND status='open'",
                    (manifest_id,),
                ).fetchall()
                unresolved = [row["container_id"] for row in unresolved_rows
                              if row["level"] in ("over_tolerance", "seal_mismatch")]
                allowed = not unresolved

            def create() -> tuple[str, str, dict[str, Any]]:
                if not allowed:
                    raise DiscrepancyError(
                        f"容器 {', '.join(unresolved)} 存在超差或封签差异，需质量人员裁决后才能结案")
                # 容差内差异随结案自动采信接收方复称。
                within_rows = connection.execute(
                    "SELECT * FROM discrepancies WHERE manifest_id=? AND status='open' AND level='within_tolerance'",
                    (manifest_id,),
                ).fetchall()
                for row in within_rows:
                    connection.execute(
                        "UPDATE discrepancies SET status='auto_closed' WHERE discrepancy_id=?",
                        (row["discrepancy_id"],))
                    connection.execute(
                        "UPDATE containers SET current_weight=?,current_seal=?,custodian_party=?,"
                        "custodian_actor=?,version=version+1 WHERE container_id=?",
                        (row["received_weight"], row["received_seal"], manifest["to_party"],
                         manifest["decided_by"] or actor_id, row["container_id"]))
                    connection.execute(
                        "UPDATE manifest_items SET item_status='confirmed' "
                        "WHERE manifest_id=? AND container_id=?",
                        (manifest_id, row["container_id"]))
                    self._container_event(
                        connection, container_id=row["container_id"], event_type="discrepancy_auto_closed",
                        manifest_id=manifest_id, actor_id=actor_id,
                        weight=row["received_weight"], seal=row["received_seal"],
                        from_party=manifest["from_party"], to_party=manifest["to_party"],
                        detail={"level": "within_tolerance", "weight_delta": row["weight_delta"]})
                connection.execute(
                    "UPDATE manifests SET status='closed',closed_by=?,closed_at=?,version=version+1 "
                    "WHERE manifest_id=?",
                    (actor_id, self._now(), manifest_id))
                for row in connection.execute("SELECT container_id FROM manifest_items WHERE manifest_id=?",
                                              (manifest_id,)):
                    self._container_event(
                        connection, container_id=row["container_id"], event_type="manifest_closed",
                        manifest_id=manifest_id, actor_id=actor_id,
                        from_party=manifest["from_party"], to_party=manifest["to_party"])
                append_event(connection, actor_id=actor_id, action="manifest.closed",
                             resource_type="manifest", resource_id=manifest_id,
                             detail={"auto_closed_discrepancies": [row["discrepancy_id"]
                                                                  for row in within_rows]},
                             occurred_at=self._now())
                return "manifest", manifest_id, {"manifest_id": manifest_id, "status": "closed"}

            return self._idempotent(connection, request_id=request_id, action="close_manifest",
                                    payload=payload, create=create)

    # --------------------------------------------------------------- 查询

    def get_container(self, container_id: str) -> Container:
        row = self.database.connection.execute("SELECT * FROM containers WHERE container_id=?",
                                               (container_id,)).fetchone()
        if row is None:
            raise NotFoundError("容器不存在")
        return self._container(row)

    def get_manifest(self, manifest_id: str) -> dict[str, Any]:
        connection = self.database.connection
        row = connection.execute("SELECT * FROM manifests WHERE manifest_id=?", (manifest_id,)).fetchone()
        if row is None:
            raise NotFoundError("联单不存在")
        items = [dict(item) for item in connection.execute(
            "SELECT * FROM manifest_items WHERE manifest_id=?", (manifest_id,))]
        discrepancies = [dict(item) for item in connection.execute(
            "SELECT * FROM discrepancies WHERE manifest_id=? ORDER BY created_at", (manifest_id,))]
        manifest = {key: row[key] for key in row.keys()}
        manifest["items"] = items
        manifest["discrepancies"] = discrepancies
        return manifest

    def list_open_discrepancies(self, site_id: str | None = None) -> list[dict[str, Any]]:
        query = ("SELECT d.* FROM discrepancies d JOIN manifests m ON d.manifest_id=m.manifest_id "
                 "WHERE d.status='open'")
        parameters: list[Any] = []
        if site_id:
            query += " AND m.site_id=?"
            parameters.append(site_id)
        query += " ORDER BY d.created_at"
        return [dict(row) for row in self.database.connection.execute(query, parameters)]

    def trace_container(self, container_id: str) -> dict[str, Any]:
        """从任一子容器反向还原来源、历次责任人、未决差异和最终去向。"""

        connection = self.database.connection
        container = self.get_container(container_id)

        ancestors = connection.execute(
            "WITH RECURSIVE walk(edge_id,parent_container_id,child_container_id,operation,"
            "parent_weight,child_weight,weight_delta,created_by,created_at,depth) AS ("
            "SELECT edge_id,parent_container_id,child_container_id,operation,parent_weight,"
            "child_weight,weight_delta,created_by,created_at,1 FROM container_lineage "
            "WHERE child_container_id=? "
            "UNION ALL "
            "SELECT e.edge_id,e.parent_container_id,e.child_container_id,e.operation,e.parent_weight,"
            "e.child_weight,e.weight_delta,e.created_by,e.created_at,w.depth+1 "
            "FROM container_lineage e JOIN walk w ON e.child_container_id=w.parent_container_id) "
            "SELECT * FROM walk ORDER BY depth, created_at",
            (container_id,),
        ).fetchall()
        descendants = connection.execute(
            "WITH RECURSIVE walk(edge_id,parent_container_id,child_container_id,operation,"
            "parent_weight,child_weight,weight_delta,created_by,created_at,depth) AS ("
            "SELECT edge_id,parent_container_id,child_container_id,operation,parent_weight,"
            "child_weight,weight_delta,created_by,created_at,1 FROM container_lineage "
            "WHERE parent_container_id=? "
            "UNION ALL "
            "SELECT e.edge_id,e.parent_container_id,e.child_container_id,e.operation,e.parent_weight,"
            "e.child_weight,e.weight_delta,e.created_by,e.created_at,w.depth+1 "
            "FROM container_lineage e JOIN walk w ON e.parent_container_id=w.child_container_id) "
            "SELECT * FROM walk ORDER BY depth, created_at",
            (container_id,),
        ).fetchall()
        ancestor_ids = {row["parent_container_id"] for row in ancestors}
        descendant_ids = {row["child_container_id"] for row in descendants}
        family = ancestor_ids | descendant_ids | {container_id}

        events = connection.execute(
            "SELECT * FROM container_events WHERE container_id=? ORDER BY sequence",
            (container_id,),
        ).fetchall()
        custody_history = [
            {"sequence": row["sequence"], "event_type": row["event_type"],
             "manifest_id": row["manifest_id"], "weight": row["weight"], "seal": row["seal"],
             "from_party": row["from_party"], "to_party": row["to_party"],
             "actor_id": row["actor_id"], "detail": json.loads(row["detail_json"]),
             "created_at": row["created_at"]}
            for row in events
        ]

        open_discrepancies = [dict(row) for row in connection.execute(
            "SELECT d.* FROM discrepancies d WHERE d.status='open' AND d.container_id=? "
            "ORDER BY d.created_at",
            (container_id,))]

        placeholders = ",".join("?" for _ in family)
        manifest_rows = connection.execute(
            f"SELECT mi.container_id, m.* FROM manifest_items mi JOIN manifests m "
            f"ON mi.manifest_id=m.manifest_id WHERE mi.container_id IN ({placeholders}) "
            f"ORDER BY m.initiated_at",
            tuple(family),
        ).fetchall()
        handovers = []
        for row in manifest_rows:
            handovers.append({"container_id": row["container_id"], "manifest_id": row["manifest_id"],
                              "from_party": row["from_party"], "to_party": row["to_party"],
                              "status": row["status"], "initiated_at": row["initiated_at"],
                              "closed_at": row["closed_at"]})
        final_destination = None
        for row in reversed(manifest_rows):
            if row["status"] == "closed" and row["container_id"] == container_id:
                final_destination = {"manifest_id": row["manifest_id"], "to_party": row["to_party"],
                                     "closed_at": row["closed_at"], "closed_by": row["closed_by"]}
                break
        if final_destination is None:
            descendant_ends = []
            for cid in descendant_ids:
                end = connection.execute(
                    "SELECT m.manifest_id,m.to_party,m.closed_at,m.closed_by FROM manifest_items mi "
                    "JOIN manifests m ON mi.manifest_id=m.manifest_id "
                    "WHERE mi.container_id=? AND m.status='closed' "
                    "ORDER BY m.closed_at DESC LIMIT 1",
                    (cid,),
                ).fetchone()
                if end:
                    descendant_ends.append({"container_id": cid, "manifest_id": end["manifest_id"],
                                            "to_party": end["to_party"], "closed_at": end["closed_at"],
                                            "closed_by": end["closed_by"]})
            if descendant_ends:
                final_destination = {"via_descendants": descendant_ends}

        return {
            "container": {
                "container_id": container.container_id, "site_id": container.site_id,
                "waste_category_key": container.waste_category_key,
                "origin_batch": container.origin_batch, "status": container.status,
                "current_weight": container.current_weight, "current_seal": container.current_seal,
                "custodian_party": container.custodian_party,
                "custodian_actor": container.custodian_actor, "version": container.version,
                "created_at": container.created_at,
            },
            "ancestors": [dict(row) for row in ancestors],
            "descendants": [dict(row) for row in descendants],
            "custody_history": custody_history,
            "open_discrepancies": open_discrepancies,
            "handovers": handovers,
            "final_destination": final_destination,
        }
