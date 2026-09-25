"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS containers (
    container_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    waste_category_key TEXT NOT NULL,
    origin_batch TEXT,
    status TEXT NOT NULL CHECK(status IN ('open', 'consumed')),
    current_weight REAL NOT NULL CHECK(current_weight > 0),
    current_seal TEXT NOT NULL,
    custodian_party TEXT NOT NULL,
    custodian_actor TEXT NOT NULL REFERENCES actors(actor_id),
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS container_lineage (
    edge_id TEXT PRIMARY KEY,
    parent_container_id TEXT NOT NULL REFERENCES containers(container_id),
    child_container_id TEXT NOT NULL REFERENCES containers(container_id),
    operation TEXT NOT NULL CHECK(operation IN ('split', 'merge')),
    parent_weight REAL NOT NULL,
    child_weight REAL NOT NULL,
    weight_delta REAL NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lineage_child ON container_lineage(child_container_id);
CREATE INDEX IF NOT EXISTS idx_lineage_parent ON container_lineage(parent_container_id);
CREATE TABLE IF NOT EXISTS container_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    container_id TEXT NOT NULL REFERENCES containers(container_id),
    event_type TEXT NOT NULL,
    manifest_id TEXT,
    weight REAL,
    seal TEXT,
    from_party TEXT,
    to_party TEXT,
    actor_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_container ON container_events(container_id);
CREATE TABLE IF NOT EXISTS manifests (
    manifest_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    from_party TEXT NOT NULL,
    to_party TEXT NOT NULL,
    tolerance_ratio REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('initiated', 'confirmed', 'rejected', 'discrepancy', 'closed')),
    initiated_by TEXT NOT NULL REFERENCES actors(actor_id),
    initiated_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    closed_by TEXT,
    closed_at TEXT,
    version INTEGER NOT NULL CHECK(version >= 1)
);
CREATE TABLE IF NOT EXISTS manifest_items (
    manifest_id TEXT NOT NULL REFERENCES manifests(manifest_id),
    container_id TEXT NOT NULL REFERENCES containers(container_id),
    declared_weight REAL NOT NULL CHECK(declared_weight > 0),
    declared_seal TEXT NOT NULL,
    received_weight REAL,
    received_seal TEXT,
    item_status TEXT NOT NULL CHECK(item_status IN ('pending', 'confirmed', 'rejected', 'disputed')),
    discrepancy_level TEXT NOT NULL DEFAULT 'none'
        CHECK(discrepancy_level IN ('none', 'within_tolerance', 'over_tolerance', 'seal_mismatch')),
    PRIMARY KEY(manifest_id, container_id)
);
CREATE INDEX IF NOT EXISTS idx_manifest_items_container ON manifest_items(container_id);
CREATE TABLE IF NOT EXISTS discrepancies (
    discrepancy_id TEXT PRIMARY KEY,
    manifest_id TEXT NOT NULL REFERENCES manifests(manifest_id),
    container_id TEXT NOT NULL REFERENCES containers(container_id),
    declared_weight REAL NOT NULL,
    declared_seal TEXT NOT NULL,
    received_weight REAL NOT NULL,
    received_seal TEXT NOT NULL,
    level TEXT NOT NULL CHECK(level IN ('within_tolerance', 'over_tolerance', 'seal_mismatch')),
    status TEXT NOT NULL CHECK(status IN ('open', 'accepted', 'rejected', 'auto_closed')),
    weight_delta REAL NOT NULL,
    receiver_statement TEXT,
    transferor_statement TEXT,
    ruling TEXT,
    ruled_by TEXT,
    ruled_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_discrepancies_container ON discrepancies(container_id);
CREATE INDEX IF NOT EXISTS idx_discrepancies_status ON discrepancies(status);
CREATE TABLE IF NOT EXISTS transformation_authorizations (
    authorization_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL CHECK(operation IN ('split', 'merge')),
    container_ids_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('issued', 'consumed', 'void')),
    authorized_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    consumed_at TEXT
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
