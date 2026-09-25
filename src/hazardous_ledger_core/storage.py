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
    zone_key TEXT,
    waste_category TEXT NOT NULL,
    waste_name TEXT,
    tare_g INTEGER,
    net_weight_g INTEGER NOT NULL CHECK(net_weight_g >= 0),
    seal_id TEXT NOT NULL,
    custodian_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    status TEXT NOT NULL CHECK(status IN ('in_custody', 'in_transit', 'split', 'merged')),
    origin_kind TEXT NOT NULL CHECK(origin_kind IN ('loading', 'split', 'merge')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS container_weighings (
    weighing_id TEXT PRIMARY KEY,
    container_id TEXT NOT NULL REFERENCES containers(container_id),
    context TEXT NOT NULL,
    weight_g INTEGER NOT NULL CHECK(weight_g >= 0),
    reference_id TEXT,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS custody_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    container_id TEXT NOT NULL REFERENCES containers(container_id),
    action TEXT NOT NULL,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    zone_key TEXT,
    custodian_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    reference_id TEXT,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS remix_grants (
    authorization_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    kind TEXT NOT NULL CHECK(kind IN ('split', 'merge')),
    spec_json TEXT NOT NULL,
    spec_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('issued', 'consumed', 'void')),
    issued_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    consumed_at TEXT
);
CREATE TABLE IF NOT EXISTS container_links (
    link_id TEXT PRIMARY KEY,
    remix_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('split', 'merge')),
    parent_container_id TEXT NOT NULL REFERENCES containers(container_id),
    child_container_id TEXT NOT NULL REFERENCES containers(container_id),
    weight_g INTEGER NOT NULL CHECK(weight_g >= 0),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_links_parent ON container_links(parent_container_id);
CREATE INDEX IF NOT EXISTS idx_links_child ON container_links(child_container_id);
CREATE TABLE IF NOT EXISTS handovers (
    handover_id TEXT PRIMARY KEY,
    from_site_id TEXT NOT NULL REFERENCES sites(site_id),
    to_site_id TEXT NOT NULL REFERENCES sites(site_id),
    from_zone_key TEXT,
    to_zone_key TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending', 'discrepancy', 'closed', 'rejected')),
    basis TEXT,
    note TEXT,
    tolerance_ratio REAL NOT NULL DEFAULT 0,
    tolerance_g INTEGER NOT NULL DEFAULT 0,
    initiated_by TEXT NOT NULL REFERENCES actors(actor_id),
    responded_by TEXT REFERENCES actors(actor_id),
    decision_hash TEXT,
    declared_at TEXT NOT NULL,
    decided_at TEXT,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS handover_items (
    handover_id TEXT NOT NULL REFERENCES handovers(handover_id),
    container_id TEXT NOT NULL REFERENCES containers(container_id),
    position INTEGER NOT NULL,
    declared_weight_g INTEGER NOT NULL,
    declared_seal TEXT NOT NULL,
    received_weight_g INTEGER,
    received_seal TEXT,
    final_weight_g INTEGER,
    final_custodian_actor_id TEXT REFERENCES actors(actor_id),
    PRIMARY KEY(handover_id, container_id)
);
CREATE INDEX IF NOT EXISTS idx_items_container ON handover_items(container_id);
CREATE TABLE IF NOT EXISTS discrepancies (
    discrepancy_id TEXT PRIMARY KEY,
    handover_id TEXT NOT NULL REFERENCES handovers(handover_id),
    container_id TEXT NOT NULL REFERENCES containers(container_id),
    kind TEXT NOT NULL CHECK(kind IN ('weight', 'seal', 'weight_seal')),
    declared_weight_g INTEGER NOT NULL,
    received_weight_g INTEGER NOT NULL,
    declared_seal TEXT NOT NULL,
    received_seal TEXT NOT NULL,
    diff_g INTEGER NOT NULL,
    within_tolerance INTEGER NOT NULL,
    blocking INTEGER NOT NULL,
    late INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('open', 'late_open', 'tolerance_closed',
                                         'arbitrated', 'rejected_dismissed')),
    ruling TEXT,
    final_weight_g INTEGER,
    resolved_by TEXT REFERENCES actors(actor_id),
    resolved_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_discrepancies_container ON discrepancies(container_id);
CREATE INDEX IF NOT EXISTS idx_discrepancies_status ON discrepancies(status);
CREATE TABLE IF NOT EXISTS party_statements (
    statement_id TEXT PRIMARY KEY,
    handover_id TEXT NOT NULL REFERENCES handovers(handover_id),
    party TEXT NOT NULL CHECK(party IN ('giver', 'receiver')),
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    statement TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_statements_handover ON party_statements(handover_id);
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
