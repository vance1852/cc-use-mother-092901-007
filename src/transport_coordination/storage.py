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
CREATE TABLE IF NOT EXISTS waybills (
    waybill_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    customer_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    status TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS segment_contracts (
    segment_id TEXT PRIMARY KEY,
    waybill_id TEXT NOT NULL REFERENCES waybills(waybill_id),
    seq INTEGER NOT NULL CHECK(seq >= 1),
    mode TEXT NOT NULL,
    carrier_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    liability_limit_minor INTEGER NOT NULL CHECK(liability_limit_minor >= 0),
    UNIQUE(waybill_id, seq)
);
CREATE TABLE IF NOT EXISTS container_groups (
    group_id TEXT PRIMARY KEY,
    waybill_id TEXT NOT NULL REFERENCES waybills(waybill_id),
    group_no TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    description TEXT NOT NULL,
    UNIQUE(waybill_id, group_no)
);
CREATE TABLE IF NOT EXISTS seals (
    seal_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES container_groups(group_id),
    seal_no TEXT NOT NULL,
    UNIQUE(group_id, seal_no)
);
CREATE TABLE IF NOT EXISTS handover_receipts (
    receipt_id TEXT PRIMARY KEY,
    waybill_id TEXT NOT NULL REFERENCES waybills(waybill_id),
    group_id TEXT NOT NULL REFERENCES container_groups(group_id),
    from_seq INTEGER NOT NULL,
    to_seq INTEGER NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    occurred_at TEXT NOT NULL,
    quantity_received INTEGER NOT NULL CHECK(quantity_received >= 0),
    seal_intact INTEGER NOT NULL CHECK(seal_intact IN (0, 1)),
    condition TEXT NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(waybill_id, group_id, from_seq, to_seq, version)
);
CREATE TABLE IF NOT EXISTS waybill_exceptions (
    exception_id TEXT PRIMARY KEY,
    waybill_id TEXT NOT NULL REFERENCES waybills(waybill_id),
    group_id TEXT NOT NULL REFERENCES container_groups(group_id),
    category TEXT NOT NULL,
    exception_key TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    detected_seq INTEGER NOT NULL CHECK(detected_seq >= 0),
    source_system TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(waybill_id, exception_key)
);
CREATE TABLE IF NOT EXISTS claim_items (
    claim_item_id TEXT PRIMARY KEY,
    exception_id TEXT NOT NULL REFERENCES waybill_exceptions(exception_id),
    waybill_id TEXT NOT NULL REFERENCES waybills(waybill_id),
    category TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    claimed_minor INTEGER NOT NULL CHECK(claimed_minor > 0),
    evidence_deadline TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_items (
    evidence_id TEXT PRIMARY KEY,
    claim_item_id TEXT NOT NULL REFERENCES claim_items(claim_item_id),
    submitted_by_org TEXT NOT NULL,
    submitted_by TEXT NOT NULL REFERENCES actors(actor_id),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    late INTEGER NOT NULL CHECK(late IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS liability_drafts (
    draft_id TEXT PRIMARY KEY,
    claim_item_id TEXT NOT NULL REFERENCES claim_items(claim_item_id),
    version INTEGER NOT NULL CHECK(version >= 1),
    awarded_minor INTEGER NOT NULL CHECK(awarded_minor >= 0),
    basis_receipt_ids_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(claim_item_id, version)
);
CREATE TABLE IF NOT EXISTS draft_allocations (
    draft_id TEXT NOT NULL REFERENCES liability_drafts(draft_id),
    segment_id TEXT NOT NULL REFERENCES segment_contracts(segment_id),
    carrier_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    amount_minor INTEGER NOT NULL CHECK(amount_minor >= 0),
    PRIMARY KEY(draft_id, segment_id)
);
CREATE TABLE IF NOT EXISTS draft_responses (
    draft_id TEXT NOT NULL REFERENCES liability_drafts(draft_id),
    org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    stance TEXT NOT NULL,
    note TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(draft_id, org_id)
);
CREATE TABLE IF NOT EXISTS claim_decisions (
    decision_id TEXT PRIMARY KEY,
    claim_item_id TEXT NOT NULL UNIQUE REFERENCES claim_items(claim_item_id),
    draft_id TEXT NOT NULL REFERENCES liability_drafts(draft_id),
    decided_via TEXT NOT NULL,
    awarded_minor INTEGER NOT NULL CHECK(awarded_minor >= 0),
    basis_receipt_ids_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decision_allocations (
    decision_id TEXT NOT NULL REFERENCES claim_decisions(decision_id),
    segment_id TEXT NOT NULL REFERENCES segment_contracts(segment_id),
    carrier_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    amount_minor INTEGER NOT NULL CHECK(amount_minor >= 0),
    PRIMARY KEY(decision_id, segment_id)
);
CREATE TABLE IF NOT EXISTS settlement_entries (
    entry_id TEXT PRIMARY KEY,
    claim_item_id TEXT NOT NULL REFERENCES claim_items(claim_item_id),
    segment_id TEXT NOT NULL REFERENCES segment_contracts(segment_id),
    payer_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    payee_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    amount_minor INTEGER NOT NULL,
    kind TEXT NOT NULL,
    review_id TEXT,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    claim_item_id TEXT NOT NULL REFERENCES claim_items(claim_item_id),
    decision_id TEXT NOT NULL REFERENCES claim_decisions(decision_id),
    trigger_evidence_id TEXT NOT NULL REFERENCES evidence_items(evidence_id),
    status TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    closed_at TEXT
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
