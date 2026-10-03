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
    waybill_no TEXT NOT NULL UNIQUE,
    customer_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    origin_name TEXT NOT NULL,
    destination_name TEXT NOT NULL,
    cargo_quantity REAL NOT NULL CHECK(cargo_quantity >= 0),
    cargo_unit TEXT NOT NULL,
    declared_value REAL NOT NULL CHECK(declared_value >= 0),
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS waybill_segments (
    segment_id TEXT PRIMARY KEY,
    waybill_id TEXT NOT NULL REFERENCES waybills(waybill_id),
    sequence_no INTEGER NOT NULL CHECK(sequence_no >= 1),
    mode TEXT NOT NULL,
    carrier_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    start_site_id TEXT REFERENCES sites(site_id),
    end_site_id TEXT REFERENCES sites(site_id),
    liability_cap REAL NOT NULL CHECK(liability_cap >= 0),
    contract_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(waybill_id, sequence_no)
);
CREATE TABLE IF NOT EXISTS containers (
    container_id TEXT PRIMARY KEY,
    waybill_id TEXT NOT NULL REFERENCES waybills(waybill_id),
    group_no TEXT NOT NULL,
    container_no TEXT NOT NULL,
    cargo_quantity REAL NOT NULL CHECK(cargo_quantity >= 0),
    cargo_description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(waybill_id, container_no)
);
CREATE TABLE IF NOT EXISTS seals (
    seal_id TEXT PRIMARY KEY,
    container_id TEXT NOT NULL REFERENCES containers(container_id),
    seal_no TEXT NOT NULL,
    status TEXT NOT NULL,
    affixed_by_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    affixed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE(container_id, seal_no)
);
CREATE TABLE IF NOT EXISTS handover_boundaries (
    boundary_id TEXT PRIMARY KEY,
    waybill_id TEXT NOT NULL REFERENCES waybills(waybill_id),
    sequence_no INTEGER NOT NULL CHECK(sequence_no >= 1),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    from_org_id TEXT REFERENCES organizations(organization_id),
    to_org_id TEXT REFERENCES organizations(organization_id),
    created_at TEXT NOT NULL,
    UNIQUE(waybill_id, sequence_no)
);
CREATE TABLE IF NOT EXISTS handover_receipts (
    receipt_id TEXT PRIMARY KEY,
    boundary_id TEXT NOT NULL REFERENCES handover_boundaries(boundary_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    submitter_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    event_time TEXT NOT NULL,
    container_summary_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES actors(actor_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(boundary_id, version_no),
    UNIQUE(boundary_id, submitter_org_id, payload_hash)
);
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    waybill_id TEXT NOT NULL REFERENCES waybills(waybill_id),
    title TEXT NOT NULL,
    boundary_id TEXT REFERENCES handover_boundaries(boundary_id),
    discovered_at TEXT NOT NULL,
    evidence_deadline TEXT NOT NULL,
    status TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    reported_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(waybill_id, dedup_key)
);
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    submitter_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    evidence_kind TEXT NOT NULL,
    title TEXT NOT NULL,
    reference TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    after_cutoff INTEGER NOT NULL CHECK(after_cutoff IN (0, 1)),
    submitted_by TEXT NOT NULL REFERENCES actors(actor_id),
    submitted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claim_items (
    claim_item_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    item_type TEXT NOT NULL,
    container_no TEXT,
    quantity REAL NOT NULL CHECK(quantity >= 0),
    claimed_amount REAL NOT NULL CHECK(claimed_amount >= 0),
    cause_key TEXT,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS liability_drafts (
    draft_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    supersedes_draft_id TEXT REFERENCES liability_drafts(draft_id),
    status TEXT NOT NULL,
    cutoff_snapshot_json TEXT NOT NULL,
    review_evidence_json TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(incident_id, version_no)
);
CREATE TABLE IF NOT EXISTS liability_allocations (
    allocation_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES liability_drafts(draft_id),
    claim_item_id TEXT NOT NULL REFERENCES claim_items(claim_item_id),
    responsible_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    share_amount REAL NOT NULL CHECK(share_amount >= 0),
    handover_receipt_id TEXT NOT NULL REFERENCES handover_receipts(receipt_id),
    evidence_refs_json TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    capped INTEGER NOT NULL CHECK(capped IN (0, 1)),
    UNIQUE(draft_id, claim_item_id, responsible_org_id)
);
CREATE TABLE IF NOT EXISTS party_positions (
    position_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES liability_drafts(draft_id),
    allocation_id TEXT NOT NULL REFERENCES liability_allocations(allocation_id),
    party_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    position TEXT NOT NULL,
    statement TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL REFERENCES actors(actor_id),
    decided_at TEXT NOT NULL,
    UNIQUE(draft_id, allocation_id, party_org_id)
);
CREATE TABLE IF NOT EXISTS liability_decisions (
    decision_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES liability_drafts(draft_id),
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    claim_item_id TEXT NOT NULL REFERENCES claim_items(claim_item_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    outcome TEXT NOT NULL,
    awarded_amount REAL NOT NULL CHECK(awarded_amount >= 0),
    basis_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(incident_id, claim_item_id, version_no)
);
CREATE TABLE IF NOT EXISTS settlement_entries (
    entry_id TEXT PRIMARY KEY,
    waybill_id TEXT NOT NULL REFERENCES waybills(waybill_id),
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    claim_item_id TEXT NOT NULL REFERENCES claim_items(claim_item_id),
    decision_id TEXT REFERENCES liability_decisions(decision_id),
    payer_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    payee_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    amount REAL NOT NULL,
    entry_type TEXT NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
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
