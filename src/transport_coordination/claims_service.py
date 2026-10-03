"""实现一单制责任认定与索赔协同服务。

以统一运单串联分段承运合同、箱组、封识、交接回执、异常证据与责任上限；
异常拆成独立索赔项目，证据截止后形成责任草案，相关方确认、申辩或仲裁。
责任决定只追加：迟到证据只能触发复审新版本与追加分录，不能删除原决定；
结算分录按业务去重键幂等，跨系统重放不会重复赔付。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor, WriteReceipt
from .storage import Database
from .claims_models import (
    ClaimItem,
    ContainerRecord,
    Evidence,
    HandoverBoundary,
    HandoverReceipt,
    Incident,
    LiabilityAllocation,
    LiabilityDecision,
    LiabilityDraft,
    PartyPosition,
    Seal,
    Segment,
    SettlementEntry,
    Waybill,
)


ITEM_TYPES = frozenset({"shortage", "damage", "delay", "common_cause"})
POSITIONS = frozenset({"confirmed", "disputed"})
OUTCOMES = frozenset({"partial_settlement", "awarded", "rejected"})

MONEY_TOLERANCE = 0.005
STAFF_ROLES = frozenset({"admin", "operator", "reviewer"})
FULL_VIEW_ROLES = frozenset({"admin", "operator", "reviewer"})


class ClaimsService:
    """协调一单制运单、交接证据、责任认定与赔付守恒规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------
    # 通用辅助
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _money_eq(self, left: float, right: float) -> bool:
        return abs(float(left) - float(right)) <= MONEY_TOLERANCE

    def _iso(self, value: Any, field: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValidationError(f"{field} 不能为空")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 不是合法的 ISO 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _amount(self, value: Any, field: str) -> float:
        try:
            amount = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} 必须是数字") from exc
        if amount != amount or amount in (float("inf"), float("-inf")):
            raise ValidationError(f"{field} 必须是有限数字")
        return amount

    def _non_negative(self, value: Any, field: str) -> float:
        amount = self._amount(value, field)
        if amount < -MONEY_TOLERANCE:
            raise ValidationError(f"{field} 不能为负")
        return max(amount, 0.0)

    def _text(self, value: Any, field: str, limit: int = 200) -> str:
        text = str(value or "").strip()
        if not text or len(text) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return text

    def _id_text(self, value: Any, field: str, limit: int = 120) -> str:
        text = str(value or "").strip()
        if not text or len(text) > limit or any(ch.isspace() for ch in text):
            raise ValidationError(f"{field} 不能为空、不能含空白且不能超过 {limit} 个字符")
        return text

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _org_exists(self, connection, org_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM organizations WHERE organization_id=?", (org_id,)
        ).fetchone() is not None

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        request_id = self._id_text(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action,
                     resource_type=resource_type, resource_id=resource_id,
                     detail=detail, occurred_at=self._now())

    def _waybill_row(self, connection, waybill_id: str):
        row = connection.execute("SELECT * FROM waybills WHERE waybill_id=?", (waybill_id,)).fetchone()
        if row is None:
            raise NotFoundError("统一运单不存在")
        return row

    def _incident_row(self, connection, incident_id: str):
        row = connection.execute("SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        if row is None:
            raise NotFoundError("异常事件不存在")
        return row

    def _draft_row(self, connection, draft_id: str):
        row = connection.execute("SELECT * FROM liability_drafts WHERE draft_id=?", (draft_id,)).fetchone()
        if row is None:
            raise NotFoundError("责任草案不存在")
        return row

    def _latest_draft(self, connection, incident_id: str):
        return connection.execute(
            "SELECT * FROM liability_drafts WHERE incident_id=? ORDER BY version_no DESC LIMIT 1",
            (incident_id,),
        ).fetchone()

    def _participant_orgs(self, connection, waybill_id: str) -> set[str]:
        """参与一单制运输的全部组织：客户、各段承运人、交接双方及港口所在组织。"""

        waybill = self._waybill_row(connection, waybill_id)
        orgs = {waybill["customer_org_id"]}
        for row in connection.execute("SELECT carrier_org_id FROM waybill_segments WHERE waybill_id=?",
                                      (waybill_id,)):
            orgs.add(row["carrier_org_id"])
        for row in connection.execute(
            "SELECT b.from_org_id, b.to_org_id, s.organization_id AS site_org "
            "FROM handover_boundaries b JOIN sites s ON s.site_id=b.site_id WHERE b.waybill_id=?",
            (waybill_id,),
        ):
            if row["from_org_id"]:
                orgs.add(row["from_org_id"])
            if row["to_org_id"]:
                orgs.add(row["to_org_id"])
            orgs.add(row["site_org"])
        return orgs

    def _require_participant(self, actor: Actor, connection, waybill_id: str) -> None:
        if actor.role in STAFF_ROLES:
            return
        if actor.organization_id not in self._participant_orgs(connection, waybill_id):
            raise PermissionDenied("当前组织不是该运单的参与方")

    # ------------------------------------------------------------------
    # 运单、分段合同、箱组、封识
    # ------------------------------------------------------------------

    def register_waybill(self, *, request_id: str, actor_id: str, waybill_id: str,
                         waybill_no: str, customer_org_id: str, origin_name: str,
                         destination_name: str, cargo_quantity: float, cargo_unit: str,
                         declared_value: float, currency: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "waybill_id": waybill_id, "waybill_no": waybill_no,
                   "customer_org_id": customer_org_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor.role not in ("admin", "operator"):
                raise PermissionDenied("只有枢纽运营方可以登记统一运单")
            waybill_id = self._id_text(waybill_id, "waybill_id")
            waybill_no = self._text(waybill_no, "waybill_no")
            customer_org_id = self._id_text(customer_org_id, "customer_org_id")
            if not self._org_exists(connection, customer_org_id):
                raise NotFoundError("客户组织不存在")
            quantity = self._non_negative(cargo_quantity, "cargo_quantity")
            cargo_unit = self._text(cargo_unit, "cargo_unit", 40)
            declared = self._non_negative(declared_value, "declared_value")
            currency = self._text(currency, "currency", 10)
            origin_name = self._text(origin_name, "origin_name")
            destination_name = self._text(destination_name, "destination_name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO waybills(waybill_id,waybill_no,customer_org_id,origin_name,"
                        "destination_name,cargo_quantity,cargo_unit,declared_value,currency,status,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (waybill_id, waybill_no, customer_org_id, origin_name, destination_name,
                         quantity, cargo_unit, declared, currency, "active", actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("运单编号或运单号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="waybill.registered",
                            resource_type="waybill", resource_id=waybill_id,
                            detail={"waybill_no": waybill_no, "customer_org_id": customer_org_id,
                                    "cargo_quantity": quantity, "declared_value": declared})
                return "waybill", waybill_id, {"waybill_id": waybill_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_waybill", payload=payload, create=create)

    def add_segment(self, *, request_id: str, actor_id: str, waybill_id: str, sequence_no: int,
                    mode: str, carrier_org_id: str, liability_cap: float, contract_ref: str,
                    start_site_id: str | None = None, end_site_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "waybill_id": waybill_id, "sequence_no": sequence_no,
                   "carrier_org_id": carrier_org_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor.role not in ("admin", "operator"):
                raise PermissionDenied("只有枢纽运营方可以登记分段承运合同")
            self._waybill_row(connection, waybill_id)
            try:
                sequence_no = int(sequence_no)
            except (TypeError, ValueError) as exc:
                raise ValidationError("sequence_no 必须是整数") from exc
            if sequence_no < 1:
                raise ValidationError("sequence_no 必须从 1 开始")
            mode = self._text(mode, "mode", 40)
            carrier_org_id = self._id_text(carrier_org_id, "carrier_org_id")
            if not self._org_exists(connection, carrier_org_id):
                raise NotFoundError("承运人组织不存在")
            cap = self._non_negative(liability_cap, "liability_cap")
            contract_ref = self._text(contract_ref, "contract_ref")
            for site_id in (start_site_id, end_site_id):
                if site_id and connection.execute(
                    "SELECT 1 FROM sites WHERE site_id=?", (site_id,)
                ).fetchone() is None:
                    raise NotFoundError(f"场所 {site_id} 不存在")
            segment_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO waybill_segments(segment_id,waybill_id,sequence_no,mode,"
                        "carrier_org_id,start_site_id,end_site_id,liability_cap,contract_ref,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (segment_id, waybill_id, sequence_no, mode, carrier_org_id,
                         start_site_id, end_site_id, cap, contract_ref, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("同一运单下分段序号重复") from exc
                self._audit(connection, actor_id=actor_id, action="segment.added",
                            resource_type="segment", resource_id=segment_id,
                            detail={"waybill_id": waybill_id, "sequence_no": sequence_no,
                                    "carrier_org_id": carrier_org_id, "liability_cap": cap})
                return "segment", segment_id, {"segment_id": segment_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_segment", payload=payload, create=create)

    def add_container(self, *, request_id: str, actor_id: str, waybill_id: str, group_no: str,
                      container_no: str, cargo_quantity: float,
                      cargo_description: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "waybill_id": waybill_id, "container_no": container_no}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor.role not in ("admin", "operator"):
                raise PermissionDenied("只有枢纽运营方可以登记箱组")
            self._waybill_row(connection, waybill_id)
            group_no = self._text(group_no, "group_no", 60)
            container_no = self._text(container_no, "container_no", 40)
            quantity = self._non_negative(cargo_quantity, "cargo_quantity")
            cargo_description = str(cargo_description or "")[:500]
            container_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO containers(container_id,waybill_id,group_no,container_no,"
                        "cargo_quantity,cargo_description,created_at) VALUES(?,?,?,?,?,?,?)",
                        (container_id, waybill_id, group_no, container_no, quantity,
                         cargo_description, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("同一运单下箱号重复") from exc
                self._audit(connection, actor_id=actor_id, action="container.added",
                            resource_type="container", resource_id=container_id,
                            detail={"waybill_id": waybill_id, "container_no": container_no,
                                    "group_no": group_no, "cargo_quantity": quantity})
                return "container", container_id, {"container_id": container_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_container", payload=payload, create=create)

    def add_seal(self, *, request_id: str, actor_id: str, waybill_id: str, container_no: str,
                 seal_no: str, status: str, affixed_by_org_id: str, affixed_at: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "waybill_id": waybill_id, "container_no": container_no,
                   "seal_no": seal_no}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor.role not in ("admin", "operator"):
                raise PermissionDenied("只有枢纽运营方可以登记封识")
            row = connection.execute(
                "SELECT * FROM containers WHERE waybill_id=? AND container_no=?",
                (waybill_id, container_no),
            ).fetchone()
            if row is None:
                raise NotFoundError("集装箱不存在")
            seal_no = self._text(seal_no, "seal_no", 60)
            status = self._text(status, "status", 40)
            affixed_by_org_id = self._id_text(affixed_by_org_id, "affixed_by_org_id")
            if not self._org_exists(connection, affixed_by_org_id):
                raise NotFoundError("施封组织不存在")
            affixed_at = self._iso(affixed_at, "affixed_at")
            seal_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO seals(seal_id,container_id,seal_no,status,affixed_by_org_id,"
                        "affixed_at,recorded_at) VALUES(?,?,?,?,?,?,?)",
                        (seal_id, row["container_id"], seal_no, status, affixed_by_org_id,
                         affixed_at, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("同一箱上的封识号重复") from exc
                self._audit(connection, actor_id=actor_id, action="seal.added",
                            resource_type="seal", resource_id=seal_id,
                            detail={"container_no": container_no, "seal_no": seal_no,
                                    "status": status})
                return "seal", seal_id, {"seal_id": seal_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_seal", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 交接节点与多方交接回执（只追加版本）
    # ------------------------------------------------------------------

    def add_boundary(self, *, request_id: str, actor_id: str, waybill_id: str, sequence_no: int,
                     site_id: str, from_org_id: str | None = None,
                     to_org_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "waybill_id": waybill_id, "sequence_no": sequence_no,
                   "site_id": site_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor.role not in ("admin", "operator"):
                raise PermissionDenied("只有枢纽运营方可以登记交接节点")
            self._waybill_row(connection, waybill_id)
            try:
                sequence_no = int(sequence_no)
            except (TypeError, ValueError) as exc:
                raise ValidationError("sequence_no 必须是整数") from exc
            if sequence_no < 1:
                raise ValidationError("sequence_no 必须从 1 开始")
            if connection.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
                raise NotFoundError("交接场所不存在")
            carriers = {
                row["carrier_org_id"]
                for row in connection.execute(
                    "SELECT carrier_org_id FROM waybill_segments WHERE waybill_id=?", (waybill_id,))
            }
            for org_id in (from_org_id, to_org_id):
                if org_id and (not self._org_exists(connection, org_id)):
                    raise NotFoundError(f"组织 {org_id} 不存在")
                if org_id and org_id not in carriers:
                    raise ValidationError("交接双方必须是该运单的分段承运人")
            boundary_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO handover_boundaries(boundary_id,waybill_id,sequence_no,site_id,"
                        "from_org_id,to_org_id,created_at) VALUES(?,?,?,?,?,?,?)",
                        (boundary_id, waybill_id, sequence_no, site_id, from_org_id, to_org_id,
                         self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("同一运单下交接序号重复") from exc
                self._audit(connection, actor_id=actor_id, action="boundary.added",
                            resource_type="handover_boundary", resource_id=boundary_id,
                            detail={"waybill_id": waybill_id, "sequence_no": sequence_no,
                                    "site_id": site_id})
                return "handover_boundary", boundary_id, {"boundary_id": boundary_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_boundary", payload=payload, create=create)

    def submit_handover_receipt(self, *, request_id: str, actor_id: str, boundary_id: str,
                                event_time: str,
                                container_summary: dict[str, Any]) -> WriteReceipt:
        if not isinstance(container_summary, dict) or not container_summary:
            raise ValidationError("container_summary 必须是非空对象")
        payload = {"actor_id": actor_id, "boundary_id": boundary_id,
                    "event_time": event_time, "summary": container_summary}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            boundary = connection.execute(
                "SELECT * FROM handover_boundaries WHERE boundary_id=?", (boundary_id,)
            ).fetchone()
            if boundary is None:
                raise NotFoundError("交接节点不存在")
            site = connection.execute(
                "SELECT organization_id FROM sites WHERE site_id=?", (boundary["site_id"],)
            ).fetchone()
            allowed = {boundary["from_org_id"], boundary["to_org_id"], site["organization_id"]}
            if actor.role not in ("admin", "operator") and actor.organization_id not in allowed:
                raise PermissionDenied("只有交接双方或港口作业方可以提交交接回执")
            event_time = self._iso(event_time, "event_time")
            summary_hash = digest({"event_time": event_time, "container_summary": container_summary})
            version_row = connection.execute(
                "SELECT COALESCE(MAX(version_no), 0) AS v FROM handover_receipts WHERE boundary_id=?",
                (boundary_id,),
            ).fetchone()
            version_no = version_row["v"] + 1
            receipt_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO handover_receipts(receipt_id,boundary_id,version_no,submitter_org_id,"
                        "event_time,container_summary_json,payload_hash,recorded_by,recorded_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (receipt_id, boundary_id, version_no, actor.organization_id, event_time,
                         canonical_json(container_summary), summary_hash, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("相同交接内容已经提交，不能重复建档") from exc
                self._audit(connection, actor_id=actor_id, action="handover.receipt_submitted",
                            resource_type="handover_receipt", resource_id=receipt_id,
                            detail={"boundary_id": boundary_id, "version_no": version_no,
                                    "submitter_org_id": actor.organization_id,
                                    "event_time": event_time, "payload_hash": summary_hash})
                return "handover_receipt", receipt_id, {"receipt_id": receipt_id, "version_no": version_no}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_handover_receipt", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 异常报案、证据、索赔项目
    # ------------------------------------------------------------------

    def report_incident(self, *, request_id: str, actor_id: str, waybill_id: str,
                        incident_id: str, title: str, discovered_at: str,
                        evidence_deadline: str, dedup_key: str,
                        boundary_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "waybill_id": waybill_id, "dedup_key": dedup_key}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            waybill = self._waybill_row(connection, waybill_id)
            if actor.role not in ("admin", "operator") and actor.organization_id != waybill["customer_org_id"]:
                raise PermissionDenied("只有客户或枢纽运营方可以报案")
            incident_id = self._id_text(incident_id, "incident_id")
            title = self._text(title, "title")
            dedup_key = self._id_text(dedup_key, "dedup_key")
            discovered_at = self._iso(discovered_at, "discovered_at")
            evidence_deadline = self._iso(evidence_deadline, "evidence_deadline")
            if evidence_deadline <= discovered_at:
                raise ValidationError("证据截止时间必须晚于发现时间")
            if boundary_id and connection.execute(
                "SELECT 1 FROM handover_boundaries WHERE boundary_id=? AND waybill_id=?",
                (boundary_id, waybill_id),
            ).fetchone() is None:
                raise NotFoundError("交接节点不属于该运单")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT incident_id FROM incidents WHERE waybill_id=? AND dedup_key=?",
                    (waybill_id, dedup_key),
                ).fetchone()
                if existing is not None:
                    raise ConflictError("同一异常已经报案，请勿对整票货物重复报案")
                try:
                    connection.execute(
                        "INSERT INTO incidents(incident_id,waybill_id,title,boundary_id,discovered_at,"
                        "evidence_deadline,status,dedup_key,reported_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (incident_id, waybill_id, title, boundary_id, discovered_at,
                         evidence_deadline, "open", dedup_key, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("异常编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="incident.reported",
                            resource_type="incident", resource_id=incident_id,
                            detail={"waybill_id": waybill_id, "dedup_key": dedup_key,
                                    "boundary_id": boundary_id})
                return "incident", incident_id, {"incident_id": incident_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="report_incident", payload=payload, create=create)

    def submit_evidence(self, *, request_id: str, actor_id: str, incident_id: str,
                        evidence_kind: str, title: str, reference: str,
                        content_hash: str | None = None,
                        submitter_org_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "incident_id": incident_id,
                   "evidence_kind": evidence_kind, "reference": reference,
                   "content_hash": content_hash, "submitter_org_id": submitter_org_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            incident = self._incident_row(connection, incident_id)
            self._require_participant(actor, connection, incident["waybill_id"])
            evidence_kind = self._text(evidence_kind, "evidence_kind", 60)
            title = self._text(title, "title")
            reference = self._text(reference, "reference", 500)
            submitter_org_id = self._id_text(
                submitter_org_id or actor.organization_id, "submitter_org_id"
            )
            if actor.role not in STAFF_ROLES and submitter_org_id != actor.organization_id:
                raise PermissionDenied("不能代表其他组织提交证据")
            if not self._org_exists(connection, submitter_org_id):
                raise NotFoundError("提交组织不存在")
            content_hash = self._id_text(content_hash or digest(
                {"kind": evidence_kind, "reference": reference}
            ), "content_hash", 128)
            now = self._now()
            after_cutoff = now > incident["evidence_deadline"]
            evidence_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO evidence(evidence_id,incident_id,submitter_org_id,evidence_kind,"
                    "title,reference,content_hash,after_cutoff,submitted_by,submitted_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (evidence_id, incident_id, submitter_org_id, evidence_kind, title, reference,
                     content_hash, 1 if after_cutoff else 0, actor_id, now),
                )
                self._audit(connection, actor_id=actor_id, action="evidence.submitted",
                            resource_type="evidence", resource_id=evidence_id,
                            detail={"incident_id": incident_id, "submitter_org_id": submitter_org_id,
                                    "evidence_kind": evidence_kind, "content_hash": content_hash,
                                    "after_cutoff": after_cutoff})
                return "evidence", evidence_id, {"evidence_id": evidence_id,
                                                  "after_cutoff": after_cutoff}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_evidence", payload=payload, create=create)

    def add_claim_item(self, *, request_id: str, actor_id: str, incident_id: str,
                       item_type: str, claimed_amount: float, quantity: float = 0.0,
                       container_no: str | None = None, cause_key: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "incident_id": incident_id, "item_type": item_type,
                   "container_no": container_no, "cause_key": cause_key}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            incident = self._incident_row(connection, incident_id)
            if actor.role not in ("admin", "operator"):
                raise PermissionDenied("由枢纽运营方拆分独立索赔项目")
            if incident["status"] != "open":
                raise ConflictError("证据截止后不能再新增索赔项目")
            if item_type not in ITEM_TYPES:
                raise ValidationError("item_type 必须是 shortage/damage/delay/common_cause")
            claimed = self._non_negative(claimed_amount, "claimed_amount")
            quantity = self._non_negative(quantity, "quantity")
            if item_type in ("shortage", "damage") and not container_no:
                raise ValidationError("短少与破损项目必须指定 container_no")
            if container_no and connection.execute(
                "SELECT 1 FROM containers WHERE waybill_id=? AND container_no=?",
                (incident["waybill_id"], container_no),
            ).fetchone() is None:
                raise NotFoundError("集装箱不属于该运单")
            if item_type == "common_cause" and not cause_key:
                raise ValidationError("共同原因项目必须提供 cause_key")
            cause_key = self._id_text(cause_key, "cause_key", 80) if cause_key else None
            claim_item_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO claim_items(claim_item_id,incident_id,item_type,container_no,"
                    "quantity,claimed_amount,cause_key,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (claim_item_id, incident_id, item_type, container_no, quantity, claimed,
                     cause_key, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="claim_item.added",
                            resource_type="claim_item", resource_id=claim_item_id,
                            detail={"incident_id": incident_id, "item_type": item_type,
                                    "claimed_amount": claimed, "cause_key": cause_key})
                return "claim_item", claim_item_id, {"claim_item_id": claim_item_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_claim_item", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 责任草案、责任分配、相关方立场
    # ------------------------------------------------------------------

    def _load_allocations_input(self, connection, waybill_id: str,
                                raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(raw_items, list) or not raw_items:
            raise ValidationError("allocations 必须是非空数组")
        segments = {
            row["carrier_org_id"]: row
            for row in connection.execute(
                "SELECT * FROM waybill_segments WHERE waybill_id=?", (waybill_id,))
        }
        normalized: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise ValidationError("allocations 条目必须是对象")
            claim_item_id = self._id_text(raw.get("claim_item_id"), "claim_item_id")
            org_id = self._id_text(raw.get("responsible_org_id"), "responsible_org_id")
            if org_id not in segments:
                raise ValidationError(f"责任方 {org_id} 不是该运单的分段承运人")
            share = self._non_negative(raw.get("share_amount"), "share_amount")
            key = (claim_item_id, org_id)
            if key in seen:
                raise ValidationError("同一项目对同一责任方只能有一条分配")
            seen.add(key)
            receipt_id = raw.get("handover_receipt_id")
            boundary_id = raw.get("boundary_id")
            if receipt_id:
                receipt = connection.execute(
                    "SELECT r.*, b.waybill_id FROM handover_receipts r "
                    "JOIN handover_boundaries b ON b.boundary_id=r.boundary_id "
                    "WHERE r.receipt_id=?", (receipt_id,),
                ).fetchone()
                if receipt is None or receipt["waybill_id"] != waybill_id:
                    raise NotFoundError("交接回执不属于该运单")
            elif boundary_id:
                receipt = connection.execute(
                    "SELECT r.*, b.waybill_id FROM handover_receipts r "
                    "JOIN handover_boundaries b ON b.boundary_id=r.boundary_id "
                    "WHERE b.boundary_id=? ORDER BY r.version_no DESC LIMIT 1",
                    (boundary_id,),
                ).fetchone()
                if receipt is None:
                    raise ValidationError("该交接节点尚无回执版本")
                receipt_id = receipt["receipt_id"]
            else:
                raise ValidationError("必须指定 handover_receipt_id 或 boundary_id")
            rationale = str(raw.get("rationale", "") or "")[:1000]
            evidence_refs = raw.get("evidence_refs", [])
            if not isinstance(evidence_refs, list) or any(not isinstance(x, str) for x in evidence_refs):
                raise ValidationError("evidence_refs 必须是字符串数组")
            cap = segments[org_id]["liability_cap"]
            capped = share > cap + MONEY_TOLERANCE
            effective = min(share, cap)
            normalized.append({
                "claim_item_id": claim_item_id,
                "responsible_org_id": org_id,
                "requested_share": share,
                "share_amount": effective,
                "capped": capped,
                "handover_receipt_id": receipt_id,
                "evidence_refs": evidence_refs,
                "rationale": rationale,
            })
        return normalized

    def _form_draft(self, connection, actor: Actor, incident_id: str,
                    allocations_input: list[dict[str, Any]], review_evidence_ids: list[str],
                    action: str, request_kind: str) -> str:
        incident = self._incident_row(connection, incident_id)
        allowed_roles = {"admin", "operator"} | ({"reviewer"} if request_kind == "review" else set())
        if actor.role not in allowed_roles:
            raise PermissionDenied("当前角色不能形成责任草案")
        now = self._now()
        if now < incident["evidence_deadline"]:
            raise ConflictError("证据截止时间未到，不能形成责任草案")
        items = connection.execute(
            "SELECT * FROM claim_items WHERE incident_id=?", (incident_id,)
        ).fetchall()
        if not items:
            raise ConflictError("没有可认定的索赔项目")
        previous = self._latest_draft(connection, incident_id)
        if previous is not None and request_kind == "initial":
            raise ConflictError("责任草案已经形成；补到的证据只能发起复审")
        if request_kind == "review":
            if previous is None:
                raise ConflictError("尚无原决定，不需要复审")
            fresh = {
                row["evidence_id"]
                for row in connection.execute(
                    "SELECT evidence_id FROM evidence WHERE incident_id=? AND after_cutoff=1",
                    (incident_id,))
            }
            used: set[str] = set()
            for draft_row in connection.execute(
                "SELECT review_evidence_json FROM liability_drafts WHERE incident_id=?",
                (incident_id,),
            ):
                used.update(json.loads(draft_row["review_evidence_json"]))
            review_evidence_ids = review_evidence_ids or sorted(fresh - used)
            unknown = [eid for eid in review_evidence_ids if eid not in fresh]
            if unknown:
                raise ValidationError("复审只能引用截止后补到的新证据")
            if not review_evidence_ids:
                raise ConflictError("没有尚未采用的新证据，不能发起复审")
        else:
            review_evidence_ids = []
        item_ids = {row["claim_item_id"] for row in items}
        allocations = self._load_allocations_input(connection, incident["waybill_id"],
                                                   allocations_input)
        covered: dict[str, float] = {}
        for allocation in allocations:
            if allocation["claim_item_id"] not in item_ids:
                raise NotFoundError("分配引用了不属于该异常的索赔项目")
            covered[allocation["claim_item_id"]] = (
                covered.get(allocation["claim_item_id"], 0.0) + allocation["share_amount"]
            )
        for item in items:
            total = covered.get(item["claim_item_id"], 0.0)
            if total > item["claimed_amount"] + MONEY_TOLERANCE:
                raise ConflictError(f"项目 {item['claim_item_id']} 的责任份额超过索赔金额")
        version_no = (previous["version_no"] + 1) if previous else 1
        snapshot = {
            "formed_at": now,
            "evidence_deadline": incident["evidence_deadline"],
            "evidence": [
                {"evidence_id": row["evidence_id"], "submitter_org_id": row["submitter_org_id"],
                 "evidence_kind": row["evidence_kind"], "content_hash": row["content_hash"]}
                for row in connection.execute(
                    "SELECT * FROM evidence WHERE incident_id=? AND after_cutoff=0",
                    (incident_id,))
            ],
            "receipt_versions": {
                row["boundary_id"]: row["max_version"]
                for row in connection.execute(
                    "SELECT r.boundary_id AS boundary_id, MAX(r.version_no) AS max_version "
                    "FROM handover_receipts r "
                    "JOIN handover_boundaries b ON b.boundary_id=r.boundary_id "
                    "WHERE b.waybill_id=? GROUP BY r.boundary_id", (incident["waybill_id"],))
            },
            "claim_item_totals": {
                row["claim_item_id"]: {"claimed": row["claimed_amount"],
                                       "allocated": covered.get(row["claim_item_id"], 0.0)}
                for row in items
            },
        }
        draft_id = uuid.uuid4().hex
        if previous is not None:
            connection.execute(
                "UPDATE liability_drafts SET status='superseded' WHERE draft_id=?",
                (previous["draft_id"],),
            )
        connection.execute(
            "INSERT INTO liability_drafts(draft_id,incident_id,version_no,supersedes_draft_id,"
            "status,cutoff_snapshot_json,review_evidence_json,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (draft_id, incident_id, version_no,
             previous["draft_id"] if previous else None, "draft",
             canonical_json(snapshot), canonical_json(review_evidence_ids),
             actor.actor_id, now),
        )
        for allocation in allocations:
            allocation_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO liability_allocations(allocation_id,draft_id,claim_item_id,"
                "responsible_org_id,share_amount,handover_receipt_id,evidence_refs_json,rationale,"
                "capped) VALUES(?,?,?,?,?,?,?,?,?)",
                (allocation_id, draft_id, allocation["claim_item_id"],
                 allocation["responsible_org_id"], allocation["share_amount"],
                 allocation["handover_receipt_id"],
                 canonical_json(allocation["evidence_refs"]), allocation["rationale"],
                 1 if allocation["capped"] else 0),
            )
        connection.execute(
            "UPDATE incidents SET status=? WHERE incident_id=?",
            ("under_review" if version_no > 1 else "draft_issued", incident_id),
        )
        self._audit(connection, actor_id=actor.actor_id, action=action,
                    resource_type="liability_draft", resource_id=draft_id,
                    detail={"incident_id": incident_id, "version_no": version_no,
                            "supersedes": previous["draft_id"] if previous else None,
                            "review_evidence": review_evidence_ids,
                            "allocation_total": sum(a["share_amount"] for a in allocations)})
        return draft_id

    def form_draft(self, *, request_id: str, actor_id: str, incident_id: str,
                   allocations: list[dict[str, Any]]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "incident_id": incident_id, "allocations": allocations}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                draft_id = self._form_draft(connection, actor, incident_id, allocations, [],
                                            "liability.draft_formed", "initial")
                return "liability_draft", draft_id, {"draft_id": draft_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="form_draft", payload=payload, create=create)

    def record_party_position(self, *, request_id: str, actor_id: str, draft_id: str,
                              allocation_id: str, position: str, statement: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "draft_id": draft_id,
                   "allocation_id": allocation_id, "position": position}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            draft = self._draft_row(connection, draft_id)
            allocation = connection.execute(
                "SELECT * FROM liability_allocations WHERE allocation_id=?", (allocation_id,)
            ).fetchone()
            if allocation is None or allocation["draft_id"] != draft_id:
                raise NotFoundError("责任份额不存在")
            if position not in POSITIONS:
                raise ValidationError("position 必须是 confirmed 或 disputed")
            if actor.role not in ("admin",) and actor.organization_id != allocation["responsible_org_id"]:
                raise PermissionDenied("只有被划责的承运方可以确认或申辩该份额")
            statement = str(statement or "")[:1000]
            position_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO party_positions(position_id,draft_id,allocation_id,party_org_id,"
                        "position,statement,decided_by,decided_at) VALUES(?,?,?,?,?,?,?,?)",
                        (position_id, draft_id, allocation_id, actor.organization_id, position,
                         statement, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该方已经提交过立场") from exc
                self._audit(connection, actor_id=actor_id, action="party.position_recorded",
                            resource_type="party_position", resource_id=position_id,
                            detail={"draft_id": draft_id, "allocation_id": allocation_id,
                                    "party_org_id": actor.organization_id, "position": position})
                return "party_position", position_id, {"position_id": position_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_party_position", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 责任决定（只追加）与结算守恒
    # ------------------------------------------------------------------

    def _allocation_basis(self, connection, draft_id: str, claim_item_id: str) -> list[dict[str, Any]]:
        basis: list[dict[str, Any]] = []
        rows = connection.execute(
            "SELECT a.*, r.version_no AS receipt_version, r.payload_hash AS receipt_hash, "
            "r.boundary_id AS boundary_id FROM liability_allocations a "
            "JOIN handover_receipts r ON r.receipt_id=a.handover_receipt_id "
            "WHERE a.draft_id=? AND a.claim_item_id=?",
            (draft_id, claim_item_id),
        ).fetchall()
        for row in rows:
            position_row = connection.execute(
                "SELECT position FROM party_positions WHERE allocation_id=? ORDER BY decided_at DESC LIMIT 1",
                (row["allocation_id"],),
            ).fetchone()
            basis.append({
                "allocation_id": row["allocation_id"],
                "responsible_org_id": row["responsible_org_id"],
                "share_amount": row["share_amount"],
                "capped": bool(row["capped"]),
                "handover_receipt_id": row["handover_receipt_id"],
                "boundary_id": row["boundary_id"],
                "receipt_version_no": row["receipt_version"],
                "receipt_payload_hash": row["receipt_hash"],
                "evidence_refs": json.loads(row["evidence_refs_json"]),
                "rationale": row["rationale"],
                "party_position": position_row["position"] if position_row else None,
            })
        return basis

    def _settle(self, connection, *, waybill_id: str, incident_id: str, claim_item_id: str,
                decision_id: str, payer_org_id: str, payee_org_id: str, amount: float,
                entry_type: str, dedup_key: str, actor_id: str) -> None:
        if self._money_eq(amount, 0.0):
            return
        try:
            connection.execute(
                "INSERT INTO settlement_entries(entry_id,waybill_id,incident_id,claim_item_id,"
                "decision_id,payer_org_id,payee_org_id,amount,entry_type,dedup_key,created_by,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, waybill_id, incident_id, claim_item_id, decision_id,
                 payer_org_id, payee_org_id, amount, entry_type, dedup_key, actor_id, self._now()),
            )
        except Exception as exc:
            raise ConflictError("结算分录去重键已经存在，拒绝重复赔付") from exc

    def decide_claim(self, *, request_id: str, actor_id: str, draft_id: str, claim_item_id: str,
                     outcome: str, awarded_amount: float, payer_org_id: str,
                     payee_org_id: str | None = None, rationale: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "draft_id": draft_id, "claim_item_id": claim_item_id,
                   "outcome": outcome, "awarded_amount": awarded_amount, "payer_org_id": payer_org_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            draft = self._draft_row(connection, draft_id)
            if draft["status"] != "draft":
                raise ConflictError("该草案版本已被复审取代")
            incident = self._incident_row(connection, draft["incident_id"])
            item = connection.execute(
                "SELECT * FROM claim_items WHERE claim_item_id=? AND incident_id=?",
                (claim_item_id, draft["incident_id"]),
            ).fetchone()
            if item is None:
                raise NotFoundError("索赔项目不存在")
            if outcome not in OUTCOMES:
                raise ValidationError("outcome 必须是 partial_settlement/awarded/rejected")
            awarded = 0.0 if outcome == "rejected" else self._non_negative(awarded_amount, "awarded_amount")
            basis = self._allocation_basis(connection, draft_id, claim_item_id)
            allocated_total = sum(b["share_amount"] for b in basis)
            confirmed_total = sum(b["share_amount"] for b in basis if b["party_position"] == "confirmed")
            if outcome == "partial_settlement":
                if actor.role not in ("admin", "operator"):
                    raise PermissionDenied("部分和解由枢纽运营方办理")
                if awarded > confirmed_total + MONEY_TOLERANCE:
                    raise ConflictError("部分和解只能支付已确认无争议的金额")
            else:
                if actor.role not in ("admin", "reviewer"):
                    raise PermissionDenied("仲裁结论由复核员作出")
            if awarded > allocated_total + MONEY_TOLERANCE:
                raise ConflictError("裁决金额超过责任份额合计")
            if awarded > item["claimed_amount"] + MONEY_TOLERANCE:
                raise ConflictError("裁决金额超过客户索赔金额")
            payer_ids = {b["responsible_org_id"] for b in basis}
            if payer_org_id not in payer_ids:
                raise ValidationError("付款方必须是该项目的责任承运人")
            payee_org_id = payee_org_id or self._waybill_row(
                connection, incident["waybill_id"])["customer_org_id"]
            if not self._org_exists(connection, payee_org_id):
                raise NotFoundError("收款方组织不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                latest_prior = connection.execute(
                    "SELECT version_no, awarded_amount FROM liability_decisions "
                    "WHERE incident_id=? AND claim_item_id=? ORDER BY version_no DESC LIMIT 1",
                    (draft["incident_id"], claim_item_id),
                ).fetchone()
                version_no = (latest_prior["version_no"] + 1) if latest_prior else 1
                decision_id = uuid.uuid4().hex
                delta = round(
                    awarded - (latest_prior["awarded_amount"] if latest_prior else 0.0), 2
                )
                entry_key = f"settle:{draft['incident_id']}:{claim_item_id}:v{version_no}"
                connection.execute(
                    "INSERT INTO liability_decisions(decision_id,draft_id,incident_id,claim_item_id,"
                    "version_no,outcome,awarded_amount,basis_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (decision_id, draft_id, draft["incident_id"], claim_item_id, version_no,
                     outcome, awarded, canonical_json(basis), actor.actor_id, self._now()),
                )
                if delta > 0:
                    self._settle(connection, waybill_id=incident["waybill_id"],
                                 incident_id=draft["incident_id"], claim_item_id=claim_item_id,
                                 decision_id=decision_id, payer_org_id=payer_org_id,
                                 payee_org_id=payee_org_id, amount=delta, entry_type="settlement",
                                 dedup_key=entry_key, actor_id=actor_id)
                elif delta < 0:
                    self._settle(connection, waybill_id=incident["waybill_id"],
                                 incident_id=draft["incident_id"], claim_item_id=claim_item_id,
                                 decision_id=decision_id, payer_org_id=payee_org_id,
                                 payee_org_id=payer_org_id, amount=-delta, entry_type="clawback",
                                 dedup_key=entry_key, actor_id=actor_id)
                self._refresh_incident_status(connection, draft["incident_id"])
                self._audit(connection, actor_id=actor_id, action="liability.decided",
                            resource_type="liability_decision", resource_id=decision_id,
                            detail={"incident_id": draft["incident_id"], "claim_item_id": claim_item_id,
                                    "draft_id": draft_id, "version_no": version_no, "outcome": outcome,
                                    "awarded_amount": awarded, "delta_amount": delta,
                                    "settlement_dedup_key": entry_key})
                return "liability_decision", decision_id, {
                    "decision_id": decision_id, "version_no": version_no,
                    "settlement_delta": delta}

            return self._idempotent(connection, request_id=request_id,
                                    action="decide_claim", payload=payload, create=create)

    def _refresh_incident_status(self, connection, incident_id: str) -> None:
        latest = self._latest_draft(connection, incident_id)
        if latest is None:
            new_status = "open"
        else:
            items = connection.execute(
                "SELECT COUNT(*) AS c FROM claim_items WHERE incident_id=?", (incident_id,)
            ).fetchone()["c"]
            decided = connection.execute(
                "SELECT COUNT(DISTINCT claim_item_id) AS c FROM liability_decisions WHERE draft_id=?",
                (latest["draft_id"],),
            ).fetchone()["c"]
            if items and decided == items:
                new_status = "decided"
            else:
                new_status = "under_review" if latest["version_no"] > 1 else "draft_issued"
        connection.execute("UPDATE incidents SET status=? WHERE incident_id=?",
                           (new_status, incident_id))

    def request_review(self, *, request_id: str, actor_id: str, incident_id: str, reason: str,
                       allocations: list[dict[str, Any]],
                       new_evidence_ids: list[str] | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "incident_id": incident_id, "reason": reason,
                   "allocations": allocations, "new_evidence_ids": new_evidence_ids}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            reason = self._text(reason, "reason", 500)
            if actor.role not in ("admin", "operator", "reviewer"):
                raise PermissionDenied("只有运营方或复核员可以发起复审")

            def create() -> tuple[str, str, dict[str, Any]]:
                draft_id = self._form_draft(connection, actor, incident_id, allocations,
                                            new_evidence_ids or [], "liability.review_requested",
                                            "review")
                return "liability_draft", draft_id, {"draft_id": draft_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="request_review", payload=payload, create=create)

    def add_recovery_entry(self, *, request_id: str, actor_id: str, incident_id: str,
                           claim_item_id: str, amount: float, payer_org_id: str,
                           payee_org_id: str, dedup_key: str,
                           decision_id: str | None = None) -> WriteReceipt:
        """追加补差或追回分录；金额为正表示补赔，为负表示追回。"""

        payload = {"actor_id": actor_id, "incident_id": incident_id,
                   "claim_item_id": claim_item_id, "dedup_key": dedup_key}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor.role not in ("admin", "operator", "reviewer"):
                raise PermissionDenied("只有运营方或复核员可以追加分录")
            incident = self._incident_row(connection, incident_id)
            item = connection.execute(
                "SELECT 1 FROM claim_items WHERE claim_item_id=? AND incident_id=?",
                (claim_item_id, incident_id),
            ).fetchone()
            if item is None:
                raise NotFoundError("索赔项目不存在")
            signed = self._amount(amount, "amount")
            if self._money_eq(signed, 0.0):
                raise ValidationError("调整金额不能为零")
            for org_id in (payer_org_id, payee_org_id):
                if not self._org_exists(connection, org_id):
                    raise NotFoundError(f"组织 {org_id} 不存在")
            dedup_key = self._id_text(dedup_key, "dedup_key")
            if decision_id and connection.execute(
                "SELECT 1 FROM liability_decisions WHERE decision_id=? AND incident_id=?",
                (decision_id, incident_id),
            ).fetchone() is None:
                raise NotFoundError("责任决定不存在")
            entry_id = uuid.uuid4().hex
            actual_amount = abs(signed)
            if signed < 0:
                payer_org_id, payee_org_id = payee_org_id, payer_org_id

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO settlement_entries(entry_id,waybill_id,incident_id,claim_item_id,"
                        "decision_id,payer_org_id,payee_org_id,amount,entry_type,dedup_key,created_by,"
                        "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (entry_id, incident["waybill_id"], incident_id, claim_item_id, decision_id,
                         payer_org_id, payee_org_id, actual_amount, "recovery", dedup_key,
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("调整分录去重键已经存在，拒绝重复记账") from exc
                self._audit(connection, actor_id=actor_id, action="settlement.recovery_added",
                            resource_type="settlement_entry", resource_id=entry_id,
                            detail={"incident_id": incident_id, "claim_item_id": claim_item_id,
                                    "amount": signed, "dedup_key": dedup_key})
                return "settlement_entry", entry_id, {"entry_id": entry_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_recovery_entry", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 查询：最小披露材料包、责任轨迹、监管守恒核对
    # ------------------------------------------------------------------

    def _receipts(self, connection, boundary_id: str) -> list[HandoverReceipt]:
        result = []
        for row in connection.execute(
            "SELECT * FROM handover_receipts WHERE boundary_id=? ORDER BY version_no",
            (boundary_id,),
        ):
            result.append(HandoverReceipt(
                row["receipt_id"], row["boundary_id"], row["version_no"], row["submitter_org_id"],
                row["event_time"], json.loads(row["container_summary_json"]),
                row["payload_hash"], row["recorded_by"], row["recorded_at"]))
        return result

    def _build_bundle(self, connection, waybill_id: str) -> dict[str, Any]:
        w = self._waybill_row(connection, waybill_id)
        waybill = Waybill(w["waybill_id"], w["waybill_no"], w["customer_org_id"], w["origin_name"],
                          w["destination_name"], w["cargo_quantity"], w["cargo_unit"],
                          w["declared_value"], w["currency"], w["status"], w["created_by"],
                          w["created_at"]).__dict__
        segments = []
        for row in connection.execute("SELECT * FROM waybill_segments WHERE waybill_id=? ORDER BY sequence_no",
                                      (waybill_id,)):
            segments.append(Segment(row["segment_id"], row["waybill_id"], row["sequence_no"],
                                    row["mode"], row["carrier_org_id"], row["start_site_id"],
                                    row["end_site_id"], row["liability_cap"], row["contract_ref"],
                                    row["created_at"]).__dict__)
        containers = []
        for row in connection.execute("SELECT * FROM containers WHERE waybill_id=?", (waybill_id,)):
            containers.append(ContainerRecord(
                row["container_id"], row["waybill_id"], row["group_no"], row["container_no"],
                row["cargo_quantity"], row["cargo_description"], row["created_at"]).__dict__)
        seals = []
        container_ids = [c["container_id"] for c in containers]
        if container_ids:
            marks = ",".join("?" for _ in container_ids)
            for row in connection.execute(
                f"SELECT * FROM seals WHERE container_id IN ({marks}) ORDER BY affixed_at",
                container_ids,
            ):
                seals.append(Seal(row["seal_id"], row["container_id"], row["seal_no"],
                                  row["status"], row["affixed_by_org_id"], row["affixed_at"],
                                  row["recorded_at"]).__dict__)
        boundaries = []
        for row in connection.execute(
            "SELECT * FROM handover_boundaries WHERE waybill_id=? ORDER BY sequence_no", (waybill_id,)
        ):
            boundaries.append({
                **HandoverBoundary(row["boundary_id"], row["waybill_id"], row["sequence_no"],
                                   row["site_id"], row["from_org_id"], row["to_org_id"],
                                   row["created_at"]).__dict__,
                "receipts": [r.__dict__ for r in self._receipts(connection, row["boundary_id"])],
            })
        incidents = []
        for irow in connection.execute("SELECT * FROM incidents WHERE waybill_id=? ORDER BY created_at",
                                       (waybill_id,)):
            incident_id = irow["incident_id"]
            evidence_items = []
            for erow in connection.execute("SELECT * FROM evidence WHERE incident_id=?",
                                           (incident_id,)):
                evidence_items.append(Evidence(
                    erow["evidence_id"], erow["incident_id"], erow["submitter_org_id"],
                    erow["evidence_kind"], erow["title"], erow["reference"], erow["content_hash"],
                    bool(erow["after_cutoff"]), erow["submitted_by"], erow["submitted_at"]).__dict__)
            claim_items = []
            for crow in connection.execute("SELECT * FROM claim_items WHERE incident_id=?",
                                           (incident_id,)):
                claim_items.append(ClaimItem(
                    crow["claim_item_id"], crow["incident_id"], crow["item_type"],
                    crow["container_no"], crow["quantity"], crow["claimed_amount"],
                    crow["cause_key"], crow["created_by"], crow["created_at"]).__dict__)
            drafts = []
            for drow in connection.execute(
                "SELECT * FROM liability_drafts WHERE incident_id=? ORDER BY version_no",
                (incident_id,),
            ):
                allocations = []
                for arow in connection.execute(
                    "SELECT * FROM liability_allocations WHERE draft_id=?", (drow["draft_id"],)
                ):
                    positions = []
                    for prow in connection.execute(
                        "SELECT * FROM party_positions WHERE allocation_id=? ORDER BY decided_at",
                        (arow["allocation_id"],),
                    ):
                        positions.append(PartyPosition(
                            prow["position_id"], prow["draft_id"], prow["allocation_id"],
                            prow["party_org_id"], prow["position"], prow["statement"],
                            prow["decided_by"], prow["decided_at"]).__dict__)
                    allocations.append({
                        **LiabilityAllocation(
                            arow["allocation_id"], arow["draft_id"], arow["claim_item_id"],
                            arow["responsible_org_id"], arow["share_amount"],
                            arow["handover_receipt_id"], tuple(json.loads(arow["evidence_refs_json"])),
                            arow["rationale"], bool(arow["capped"])).__dict__,
                        "positions": positions,
                    })
                decisions = []
                for derow in connection.execute(
                    "SELECT * FROM liability_decisions WHERE draft_id=? ORDER BY version_no",
                    (drow["draft_id"],),
                ):
                    decisions.append(LiabilityDecision(
                        derow["decision_id"], derow["draft_id"], derow["incident_id"],
                        derow["claim_item_id"], derow["version_no"], derow["outcome"],
                        derow["awarded_amount"], tuple(json.loads(derow["basis_json"])),
                        derow["created_by"], derow["created_at"]).__dict__)
                drafts.append({
                    **LiabilityDraft(
                        drow["draft_id"], drow["incident_id"], drow["version_no"],
                        drow["supersedes_draft_id"], drow["status"],
                        json.loads(drow["cutoff_snapshot_json"]),
                        tuple(json.loads(drow["review_evidence_json"])),
                        drow["created_by"], drow["created_at"]).__dict__,
                    "allocations": allocations,
                    "decisions": decisions,
                })
            entries = []
            for erow in connection.execute(
                "SELECT * FROM settlement_entries WHERE incident_id=? ORDER BY created_at",
                (incident_id,),
            ):
                entries.append(SettlementEntry(
                    erow["entry_id"], erow["waybill_id"], erow["incident_id"],
                    erow["claim_item_id"], erow["decision_id"], erow["payer_org_id"],
                    erow["payee_org_id"], erow["amount"], erow["entry_type"],
                    erow["dedup_key"], erow["created_by"], erow["created_at"]).__dict__)
            incidents.append({
                **Incident(irow["incident_id"], irow["waybill_id"], irow["title"],
                           irow["boundary_id"], irow["discovered_at"], irow["evidence_deadline"],
                           irow["status"], irow["dedup_key"], irow["reported_by"],
                           irow["created_at"]).__dict__,
                "evidence": evidence_items,
                "claim_items": claim_items,
                "drafts": drafts,
                "settlement_entries": entries,
            })
        return {"waybill": waybill, "segments": segments, "containers": containers,
                "seals": seals, "boundaries": boundaries, "incidents": incidents}

    def get_case_bundle(self, actor_id: str, waybill_id: str) -> dict[str, Any]:
        """按观察者角色与参与方身份返回最小披露材料包。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        bundle = self._build_bundle(connection, waybill_id)
        org_id = actor.organization_id
        customer_id = bundle["waybill"]["customer_org_id"]
        if actor.role in FULL_VIEW_ROLES:
            bundle["view"] = "internal"
            return bundle
        participants = self._participant_orgs(connection, waybill_id)
        if org_id not in participants:
            raise PermissionDenied("当前组织不是该运单的参与方")
        bundle["view"] = "customer" if org_id == customer_id else "party"
        is_customer = org_id == customer_id
        # 分段合同条款只对该段承运人及客户之外的内部方开放
        for segment in bundle["segments"]:
            if is_customer or segment["carrier_org_id"] != org_id:
                segment["contract_ref"] = None
                segment["liability_cap"] = None
        # 证据：只回传本方提交的外部引用，其他方仅可见指纹
        for incident in bundle["incidents"]:
            for evidence in incident["evidence"]:
                if evidence["submitter_org_id"] != org_id:
                    evidence["reference"] = None
            if is_customer:
                self._redact_for_customer(incident, org_id)
            else:
                self._redact_for_party(incident, org_id)
        return bundle

    def _redact_for_customer(self, incident: dict[str, Any], org_id: str) -> None:
        """客户可见结论与交接版本，但看不到承运人之间的内部辩词。"""

        for draft in incident["drafts"]:
            for allocation in draft["allocations"]:
                allocation["positions"] = [
                    {**p, "statement": ""} for p in allocation["positions"]
                ]
            draft["decisions"] = [
                {
                    **decision,
                    "basis": tuple({
                        "responsible_org_id": b["responsible_org_id"],
                        "share_amount": b["share_amount"],
                        "capped": b["capped"],
                        "handover_receipt_id": b["handover_receipt_id"],
                        "boundary_id": b["boundary_id"],
                        "receipt_version_no": b["receipt_version_no"],
                        "receipt_payload_hash": b["receipt_payload_hash"],
                        "party_position": b["party_position"],
                    } for b in decision["basis"]),
                }
                for decision in draft["decisions"]
            ]
        incident["settlement_entries"] = [
            e for e in incident["settlement_entries"]
            if e["payer_org_id"] == org_id or e["payee_org_id"] == org_id
        ]

    def _redact_for_party(self, incident: dict[str, Any], org_id: str) -> None:
        """承运方只见划给本方的份额、相关决定与本方的立场记录。"""

        visible_items: set[str] = set()
        kept_drafts = []
        for draft in incident["drafts"]:
            allocations = []
            for allocation in draft["allocations"]:
                if allocation["responsible_org_id"] != org_id:
                    continue
                allocation["positions"] = [
                    p for p in allocation["positions"] if p["party_org_id"] == org_id
                ]
                allocations.append(allocation)
                visible_items.add(allocation["claim_item_id"])
            decisions = [
                d for d in draft["decisions"]
                if any(b["responsible_org_id"] == org_id for b in d["basis"])
            ]
            if allocations or decisions:
                kept_drafts.append({**draft, "allocations": allocations,
                                    "decisions": decisions})
        incident["drafts"] = kept_drafts
        incident["claim_items"] = [
            item for item in incident["claim_items"]
            if item["claim_item_id"] in visible_items
        ]
        incident["settlement_entries"] = [
            e for e in incident["settlement_entries"]
            if e["payer_org_id"] == org_id or e["payee_org_id"] == org_id
        ]

    def reconcile(self, actor_id: str, waybill_id: str | None = None) -> dict[str, Any]:
        """供监管核对数量与赔款守恒，并列出每个决定采用的交接版本。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        if actor.role not in ("admin", "auditor"):
            raise PermissionDenied("只有监管人员可以执行守恒核对")
        query = "SELECT * FROM incidents"
        parameters: list[Any] = []
        if waybill_id:
            query += " WHERE waybill_id=?"
            parameters.append(waybill_id)
        items_report: list[dict[str, Any]] = []
        quantity_violations: list[dict[str, Any]] = []
        totals = {"claimed": 0.0, "awarded": 0.0, "settled": 0.0}
        conserved = True
        for incident in connection.execute(query, parameters):
            cargo_by_container = {
                row["container_no"]: row["cargo_quantity"]
                for row in connection.execute(
                    "SELECT container_no, cargo_quantity FROM containers WHERE waybill_id=?",
                    (incident["waybill_id"],),
                )
            }
            for item in connection.execute(
                "SELECT * FROM claim_items WHERE incident_id=?", (incident["incident_id"],)
            ):
                latest = connection.execute(
                    "SELECT * FROM liability_decisions WHERE incident_id=? AND claim_item_id=? "
                    "ORDER BY version_no DESC LIMIT 1",
                    (incident["incident_id"], item["claim_item_id"]),
                ).fetchone()
                entries_sum = 0.0
                for entry in connection.execute(
                    "SELECT amount, entry_type FROM settlement_entries WHERE incident_id=? AND claim_item_id=?",
                    (incident["incident_id"], item["claim_item_id"]),
                ):
                    entries_sum += entry["amount"]
                awarded = latest["awarded_amount"] if latest else 0.0
                item_conserved = self._money_eq(entries_sum, awarded)
                conserved = conserved and item_conserved
                pinned: list[dict[str, Any]] = []
                if latest:
                    for basis in json.loads(latest["basis_json"]):
                        pinned.append({
                            "boundary_id": basis["boundary_id"],
                            "handover_receipt_id": basis["handover_receipt_id"],
                            "receipt_version_no": basis["receipt_version_no"],
                            "receipt_payload_hash": basis["receipt_payload_hash"],
                        })
                items_report.append({
                    "waybill_id": incident["waybill_id"],
                    "incident_id": incident["incident_id"],
                    "claim_item_id": item["claim_item_id"],
                    "item_type": item["item_type"],
                    "claimed_amount": item["claimed_amount"],
                    "latest_awarded_amount": awarded,
                    "settlement_entries_sum": round(entries_sum, 2),
                    "conserved": item_conserved,
                    "decision_count": connection.execute(
                        "SELECT COUNT(*) AS c FROM liability_decisions WHERE incident_id=? AND claim_item_id=?",
                        (incident["incident_id"], item["claim_item_id"]),
                    ).fetchone()["c"],
                    "pinned_receipts": pinned,
                })
                totals["claimed"] += item["claimed_amount"]
                totals["awarded"] += awarded
                totals["settled"] += entries_sum
                if item["item_type"] == "shortage" and item["container_no"]:
                    capacity = cargo_by_container.get(item["container_no"])
                    if capacity is not None and item["quantity"] > capacity + MONEY_TOLERANCE:
                        quantity_violations.append({
                            "claim_item_id": item["claim_item_id"],
                            "container_no": item["container_no"],
                            "shortage_quantity": item["quantity"],
                            "loaded_quantity": capacity,
                        })
        totals = {key: round(value, 2) for key, value in totals.items()}
        global_conserved = self._money_eq(totals["awarded"], totals["settled"]) and conserved
        duplicate_entries = connection.execute(
            "SELECT dedup_key, COUNT(*) AS c FROM settlement_entries GROUP BY dedup_key HAVING c > 1"
        ).fetchall()
        return {
            "items": items_report,
            "quantity_violations": quantity_violations,
            "totals": totals,
            "conserved": global_conserved,
            "duplicate_settlement_keys": [row["dedup_key"] for row in duplicate_entries],
        }

    def decision_trace(self, actor_id: str, decision_id: str) -> dict[str, Any]:
        """还原一个责任判断所采用的交接回执版本与证据快照。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        row = connection.execute(
            "SELECT d.*, i.waybill_id FROM liability_decisions d "
            "JOIN incidents i ON i.incident_id=d.incident_id WHERE d.decision_id=?",
            (decision_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("责任决定不存在")
        if actor.role not in FULL_VIEW_ROLES and actor.role != "auditor":
            participants = self._participant_orgs(connection, row["waybill_id"])
            if actor.organization_id not in participants:
                raise PermissionDenied("当前组织不是该运单的参与方")
        basis = json.loads(row["basis_json"])
        receipts = []
        for basis_item in basis:
            receipt = connection.execute(
                "SELECT r.*, b.sequence_no AS boundary_sequence FROM handover_receipts r "
                "JOIN handover_boundaries b ON b.boundary_id=r.boundary_id "
                "WHERE r.receipt_id=?",
                (basis_item["handover_receipt_id"],),
            ).fetchone()
            if actor.role not in FULL_VIEW_ROLES and actor.role != "auditor":
                summary = None
            else:
                summary = json.loads(receipt["container_summary_json"])
            receipts.append({
                "boundary_id": receipt["boundary_id"],
                "boundary_sequence": receipt["boundary_sequence"],
                "receipt_id": receipt["receipt_id"],
                "version_no": receipt["version_no"],
                "submitter_org_id": receipt["submitter_org_id"],
                "event_time": receipt["event_time"],
                "payload_hash": receipt["payload_hash"],
                "container_summary": summary,
            })
        return {
            "decision_id": row["decision_id"],
            "incident_id": row["incident_id"],
            "claim_item_id": row["claim_item_id"],
            "draft_id": row["draft_id"],
            "version_no": row["version_no"],
            "outcome": row["outcome"],
            "awarded_amount": row["awarded_amount"],
            "basis": basis,
            "pinned_receipt_versions": receipts,
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }
