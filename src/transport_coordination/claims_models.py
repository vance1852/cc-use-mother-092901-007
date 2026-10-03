"""定义一单制责任与索赔域在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Waybill:
    """一单制统一运单，串联分段合同、箱组与异常索赔。"""

    waybill_id: str
    waybill_no: str
    customer_org_id: str
    origin_name: str
    destination_name: str
    cargo_quantity: float
    cargo_unit: str
    declared_value: float
    currency: str
    status: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class Segment:
    """一段分段承运合同及其责任上限。"""

    segment_id: str
    waybill_id: str
    sequence_no: int
    mode: str
    carrier_org_id: str
    start_site_id: str | None
    end_site_id: str | None
    liability_cap: float
    contract_ref: str
    created_at: str


@dataclass(frozen=True)
class ContainerRecord:
    """箱组中的一个集装箱及其装载数量。"""

    container_id: str
    waybill_id: str
    group_no: str
    container_no: str
    cargo_quantity: float
    cargo_description: str
    created_at: str


@dataclass(frozen=True)
class Seal:
    """集装箱封识记录。"""

    seal_id: str
    container_id: str
    seal_no: str
    status: str
    affixed_by_org_id: str
    affixed_at: str
    recorded_at: str


@dataclass(frozen=True)
class HandoverBoundary:
    """两个分段承运人之间的一次交接节点。"""

    boundary_id: str
    waybill_id: str
    sequence_no: int
    site_id: str
    from_org_id: str | None
    to_org_id: str | None
    created_at: str


@dataclass(frozen=True)
class HandoverReceipt:
    """某一方保存的交接回执版本；各方交接时间不同时各自成为新版本。"""

    receipt_id: str
    boundary_id: str
    version_no: int
    submitter_org_id: str
    event_time: str
    container_summary: dict[str, Any]
    payload_hash: str
    recorded_by: str
    recorded_at: str


@dataclass(frozen=True)
class Incident:
    """一次异常报案；同一运单内去重键唯一，防止整票重复报案。"""

    incident_id: str
    waybill_id: str
    title: str
    boundary_id: str | None
    discovered_at: str
    evidence_deadline: str
    status: str
    dedup_key: str
    reported_by: str
    created_at: str


@dataclass(frozen=True)
class Evidence:
    """异常证据材料；服务只保存指纹与外部引用，便于最小披露。"""

    evidence_id: str
    incident_id: str
    submitter_org_id: str
    evidence_kind: str
    title: str
    reference: str
    content_hash: str
    after_cutoff: bool
    submitted_by: str
    submitted_at: str


@dataclass(frozen=True)
class ClaimItem:
    """短少、破损、延误或共同原因拆成的独立索赔项目。"""

    claim_item_id: str
    incident_id: str
    item_type: str
    container_no: str | None
    quantity: float
    claimed_amount: float
    cause_key: str | None
    created_by: str
    created_at: str


@dataclass(frozen=True)
class LiabilityDraft:
    """证据截止后形成的责任草案；复审产生新版本，原草案保留。"""

    draft_id: str
    incident_id: str
    version_no: int
    supersedes_draft_id: str | None
    status: str
    cutoff_snapshot: dict[str, Any]
    review_evidence_ids: tuple[str, ...]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class LiabilityAllocation:
    """草案中对某一索赔项目划给某责任方的份额。"""

    allocation_id: str
    draft_id: str
    claim_item_id: str
    responsible_org_id: str
    share_amount: float
    handover_receipt_id: str
    evidence_refs: tuple[str, ...]
    rationale: str
    capped: bool


@dataclass(frozen=True)
class PartyPosition:
    """相关方对责任份额的确认或申辩。"""

    position_id: str
    draft_id: str
    allocation_id: str
    party_org_id: str
    position: str
    statement: str
    decided_by: str
    decided_at: str


@dataclass(frozen=True)
class LiabilityDecision:
    """不可删除的责任决定；复审只会产生新版本。"""

    decision_id: str
    draft_id: str
    incident_id: str
    claim_item_id: str
    version_no: int
    outcome: str
    awarded_amount: float
    basis: tuple[dict[str, Any], ...]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class SettlementEntry:
    """赔付或调整分录；去重键保证同一决定重放不会重复赔付。"""

    entry_id: str
    waybill_id: str
    incident_id: str
    claim_item_id: str
    decision_id: str | None
    payer_org_id: str
    payee_org_id: str
    amount: float
    entry_type: str
    dedup_key: str
    created_by: str
    created_at: str
