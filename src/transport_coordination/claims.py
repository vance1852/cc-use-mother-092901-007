"""铁水联运一单制责任与索赔领域服务。

在基础服务的权限、幂等、事务和审计边界上，实现以统一运单串联分段承运合同、
箱组、封识、交接回执、异常证据与责任上限的索赔协同：

- 短少、破损、延误和共同原因拆成独立索赔项目，各自推进生命周期；
- 证据截止后形成责任草案，相关方确认、申辩或由枢纽仲裁；
- 部分和解先行支付无争议金额，补交证据只能触发复审或追加分录，原决定不可删除；
- 同一异常跨系统重放只登记一次，结算额度封顶，不会重复赔付；
- 客户与各承运方按最小披露查看材料，监管人员可核对数量与赔款守恒，
  并还原每个责任判断采用的交接回执版本。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor, WriteReceipt
from .service import DomainService
from .storage import Database
from .clock import Clock


CLAIM_CATEGORIES = frozenset({"shortage", "damage", "delay", "common"})
QUANTITY_BEARING_CATEGORIES = frozenset({"shortage", "damage"})
SEGMENT_MODES = frozenset({"rail", "port", "water", "road"})
HANDOVER_CONDITIONS = frozenset({"good", "damaged", "short"})
DRAFT_STANCES = frozenset({"confirm", "rebut"})

CLAIM_COLLECTING = "collecting"
CLAIM_DRAFTED = "drafted"
CLAIM_DECIDED = "decided"

DRAFT_OPEN = "open"
DRAFT_ARBITRATING = "arbitrating"
DRAFT_CONFIRMED = "confirmed"
DRAFT_ARBITRATED = "arbitrated"
DRAFT_SUPERSEDED = "superseded"

ENTRY_PARTIAL = "partial"
ENTRY_DECISION = "decision"
ENTRY_ADDITIONAL = "additional"
CAPPED_ENTRY_KINDS = (ENTRY_PARTIAL, ENTRY_DECISION)

REVIEW_OPEN = "open"
REVIEW_CLOSED = "closed"


class ClaimsService(DomainService):
    """在基础服务边界上实现一单制责任与索赔协同。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        super().__init__(database, clock)

    # ---------- 基础校验与装载 ----------

    def _money(self, value: Any, field: str, *, allow_zero: bool = False) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{field} 必须是最小货币单位的整数")
        if allow_zero:
            if value < 0:
                raise ValidationError(f"{field} 不能为负数")
        elif value <= 0:
            raise ValidationError(f"{field} 必须大于零")
        return value

    def _natural(self, value: Any, field: str, *, allow_zero: bool = False) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{field} 必须是整数")
        if allow_zero:
            if value < 0:
                raise ValidationError(f"{field} 不能为负数")
        elif value <= 0:
            raise ValidationError(f"{field} 必须大于零")
        return value

    def _moment(self, value: Any, field: str) -> datetime:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 时间格式无效") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return parsed.astimezone(timezone.utc)

    def _stored_moment(self, value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _iso(self, moment: datetime) -> str:
        return moment.isoformat().replace("+00:00", "Z")

    def _note(self, value: Any, field: str, limit: int = 500) -> str:
        text = str(value or "").strip()
        if len(text) > limit:
            raise ValidationError(f"{field} 不能超过 {limit} 个字符")
        return text

    def _waybill_row(self, connection, waybill_id: str):
        row = connection.execute("SELECT * FROM waybills WHERE waybill_id=?", (waybill_id,)).fetchone()
        if row is None:
            raise NotFoundError("运单不存在")
        return row

    def _claim_row(self, connection, claim_item_id: str):
        row = connection.execute("SELECT * FROM claim_items WHERE claim_item_id=?", (claim_item_id,)).fetchone()
        if row is None:
            raise NotFoundError("索赔项目不存在")
        return row

    def _draft_row(self, connection, draft_id: str):
        row = connection.execute("SELECT * FROM liability_drafts WHERE draft_id=?", (draft_id,)).fetchone()
        if row is None:
            raise NotFoundError("责任草案不存在")
        return row

    def _decision_row(self, connection, decision_id: str):
        row = connection.execute("SELECT * FROM claim_decisions WHERE decision_id=?", (decision_id,)).fetchone()
        if row is None:
            raise NotFoundError("责任决定不存在")
        return row

    def _review_row(self, connection, review_id: str):
        row = connection.execute("SELECT * FROM reviews WHERE review_id=?", (review_id,)).fetchone()
        if row is None:
            raise NotFoundError("复审不存在")
        return row

    def _hub_org_id(self, connection, waybill) -> str:
        site = connection.execute(
            "SELECT organization_id FROM sites WHERE site_id=?", (waybill["site_id"],)
        ).fetchone()
        return site["organization_id"]

    def _hub_actor(self, connection, actor_id: str, hub_org_id: str, *roles: str) -> Actor:
        actor = self._actor(connection, actor_id)
        self._require(actor, *roles)
        if actor.organization_id != hub_org_id:
            raise PermissionDenied("只能由枢纽运营方执行该动作")
        return actor

    def _waybill_party_orgs(self, connection, waybill) -> set[str]:
        carriers = {row["carrier_org_id"] for row in connection.execute(
            "SELECT DISTINCT carrier_org_id FROM segment_contracts WHERE waybill_id=?",
            (waybill["waybill_id"],))}
        return {waybill["customer_org_id"]} | carriers

    def _segment_rows(self, connection, waybill_id: str):
        return connection.execute(
            "SELECT * FROM segment_contracts WHERE waybill_id=? ORDER BY seq", (waybill_id,)
        ).fetchall()

    def _settled_by_segment(self, connection, claim_item_id: str) -> dict[str, int]:
        rows = connection.execute(
            "SELECT segment_id, COALESCE(SUM(amount_minor),0) AS paid FROM settlement_entries "
            "WHERE claim_item_id=? GROUP BY segment_id",
            (claim_item_id,),
        ).fetchall()
        return {row["segment_id"]: row["paid"] for row in rows}

    # ---------- 统一运单与串联资料 ----------

    def register_waybill(self, *, request_id: str, actor_id: str, site_id: str,
                         waybill_id: str, customer_org_id: str,
                         segments: list[dict[str, Any]], groups: list[dict[str, Any]]) -> WriteReceipt:
        """登记统一运单，串联分段承运合同（含责任上限）、箱组与封识。"""

        payload = {"actor_id": actor_id, "site_id": site_id, "waybill_id": waybill_id,
                   "customer_org_id": customer_org_id, "segments": segments, "groups": groups}
        with self.database.transaction(immediate=True) as connection:
            site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")
            self._hub_actor(connection, actor_id, site["organization_id"], "admin", "operator")
            waybill_id = self._identifier(waybill_id, "waybill_id")
            if connection.execute(
                "SELECT 1 FROM organizations WHERE organization_id=?", (customer_org_id,)
            ).fetchone() is None:
                raise NotFoundError("客户组织不存在")
            segment_rows = self._validate_segments(connection, segments)
            group_rows = self._validate_groups(groups)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO waybills(waybill_id,site_id,customer_org_id,status,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (waybill_id, site_id, customer_org_id, "open", actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("运单编号已经存在") from exc
                for segment in segment_rows:
                    connection.execute(
                        "INSERT INTO segment_contracts(segment_id,waybill_id,seq,mode,carrier_org_id,"
                        "liability_limit_minor) VALUES(?,?,?,?,?,?)",
                        (segment["segment_id"], waybill_id, segment["seq"], segment["mode"],
                         segment["carrier_org_id"], segment["liability_limit_minor"]),
                    )
                for group in group_rows:
                    connection.execute(
                        "INSERT INTO container_groups(group_id,waybill_id,group_no,quantity,description) "
                        "VALUES(?,?,?,?,?)",
                        (group["group_id"], waybill_id, group["group_no"], group["quantity"],
                         group["description"]),
                    )
                    for seal in group["seals"]:
                        connection.execute(
                            "INSERT INTO seals(seal_id,group_id,seal_no) VALUES(?,?,?)",
                            (seal["seal_id"], group["group_id"], seal["seal_no"]),
                        )
                append_event(connection, actor_id=actor_id, action="waybill.registered",
                             resource_type="waybill", resource_id=waybill_id,
                             detail={"site_id": site_id, "customer_org_id": customer_org_id,
                                     "segments": len(segment_rows), "groups": len(group_rows)},
                             occurred_at=self._now())
                return "waybill", waybill_id, {"waybill_id": waybill_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_waybill", payload=payload, create=create)

    def _validate_segments(self, connection, segments: Any) -> list[dict[str, Any]]:
        if not isinstance(segments, list) or not segments:
            raise ValidationError("segments 必须是非空数组")
        seen: set[int] = set()
        rows: list[dict[str, Any]] = []
        for item in segments:
            if not isinstance(item, dict):
                raise ValidationError("segments 元素必须是对象")
            seq = self._natural(item.get("seq"), "seq")
            if seq in seen:
                raise ValidationError("segments 的 seq 不能重复")
            seen.add(seq)
            mode = str(item.get("mode", "")).strip()
            if mode not in SEGMENT_MODES:
                raise ValidationError("mode 不在允许范围内")
            carrier_org_id = self._identifier(str(item.get("carrier_org_id", "")), "carrier_org_id")
            if connection.execute(
                "SELECT 1 FROM organizations WHERE organization_id=?", (carrier_org_id,)
            ).fetchone() is None:
                raise NotFoundError("承运组织不存在")
            limit = self._money(item.get("liability_limit_minor"), "liability_limit_minor", allow_zero=True)
            rows.append({"segment_id": uuid.uuid4().hex, "seq": seq, "mode": mode,
                         "carrier_org_id": carrier_org_id, "liability_limit_minor": limit})
        rows.sort(key=lambda row: row["seq"])
        return rows

    def _validate_groups(self, groups: Any) -> list[dict[str, Any]]:
        if not isinstance(groups, list) or not groups:
            raise ValidationError("groups 必须是非空数组")
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        for item in groups:
            if not isinstance(item, dict):
                raise ValidationError("groups 元素必须是对象")
            group_no = self._identifier(str(item.get("group_no", "")), "group_no")
            if group_no in seen:
                raise ValidationError("group_no 不能重复")
            seen.add(group_no)
            quantity = self._natural(item.get("quantity"), "quantity")
            description = self._text(str(item.get("description", "")), "description")
            seal_values = item.get("seals", [])
            if not isinstance(seal_values, list):
                raise ValidationError("seals 必须是数组")
            seal_seen: set[str] = set()
            seal_rows: list[dict[str, Any]] = []
            for seal_value in seal_values:
                seal_no = self._identifier(str(seal_value), "seal_no")
                if seal_no in seal_seen:
                    raise ValidationError("seal_no 不能重复")
                seal_seen.add(seal_no)
                seal_rows.append({"seal_id": uuid.uuid4().hex, "seal_no": seal_no})
            rows.append({"group_id": uuid.uuid4().hex, "group_no": group_no, "quantity": quantity,
                         "description": description, "seals": seal_rows})
        return rows

    def record_handover(self, *, request_id: str, actor_id: str, waybill_id: str, group_id: str,
                        from_seq: int, to_seq: int, occurred_at: str, quantity_received: int,
                        seal_intact: bool, condition: str) -> WriteReceipt:
        """登记一个交接点的回执新版本，旧版本全部保留。"""

        payload = {"actor_id": actor_id, "waybill_id": waybill_id, "group_id": group_id,
                   "from_seq": from_seq, "to_seq": to_seq, "occurred_at": occurred_at,
                   "quantity_received": quantity_received, "seal_intact": seal_intact,
                   "condition": condition}
        with self.database.transaction(immediate=True) as connection:
            waybill = self._waybill_row(connection, waybill_id)
            self._hub_actor(connection, actor_id, self._hub_org_id(connection, waybill), "admin", "operator")
            group = connection.execute(
                "SELECT * FROM container_groups WHERE group_id=? AND waybill_id=?", (group_id, waybill_id)
            ).fetchone()
            if group is None:
                raise NotFoundError("箱组不存在")
            from_seq = self._natural(from_seq, "from_seq")
            to_seq = self._natural(to_seq, "to_seq", allow_zero=True)
            seqs = {row["seq"] for row in self._segment_rows(connection, waybill_id)}
            if from_seq not in seqs:
                raise ValidationError("from_seq 不属于运单区段")
            if to_seq == 0:
                if from_seq != max(seqs):
                    raise ValidationError("只有末区段可以登记交付回执")
            elif to_seq not in seqs or to_seq <= from_seq:
                raise ValidationError("to_seq 必须是后续区段或 0（交付收货人）")
            moment = self._moment(occurred_at, "occurred_at")
            quantity_received = self._natural(quantity_received, "quantity_received", allow_zero=True)
            if quantity_received > group["quantity"]:
                raise ValidationError("接收数量不能大于箱组发运数量")
            if not isinstance(seal_intact, bool):
                raise ValidationError("seal_intact 必须是布尔值")
            condition = str(condition).strip()
            if condition not in HANDOVER_CONDITIONS:
                raise ValidationError("condition 不在允许范围内")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT COALESCE(MAX(version),0) AS version FROM handover_receipts "
                    "WHERE waybill_id=? AND group_id=? AND from_seq=? AND to_seq=?",
                    (waybill_id, group_id, from_seq, to_seq),
                ).fetchone()
                version = row["version"] + 1
                receipt_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO handover_receipts(receipt_id,waybill_id,group_id,from_seq,to_seq,version,"
                    "occurred_at,quantity_received,seal_intact,condition,recorded_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (receipt_id, waybill_id, group_id, from_seq, to_seq, version, self._iso(moment),
                     quantity_received, 1 if seal_intact else 0, condition, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="handover.recorded",
                             resource_type="handover_receipt", resource_id=receipt_id,
                             detail={"waybill_id": waybill_id, "group_id": group_id,
                                     "from_seq": from_seq, "to_seq": to_seq, "version": version,
                                     "quantity_received": quantity_received, "seal_intact": seal_intact,
                                     "condition": condition},
                             occurred_at=self._now())
                return "handover_receipt", receipt_id, {"receipt_id": receipt_id, "version": version}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_handover", payload=payload, create=create)

    # ---------- 异常与独立索赔项目 ----------

    def register_exception(self, *, request_id: str, actor_id: str, waybill_id: str, group_id: str,
                           category: str, exception_key: str, quantity: int, detected_seq: int,
                           source_system: str) -> WriteReceipt:
        """登记异常；同一异常键跨系统重放只保留首次登记。"""

        payload = {"actor_id": actor_id, "waybill_id": waybill_id, "group_id": group_id,
                   "category": category, "exception_key": exception_key, "quantity": quantity,
                   "detected_seq": detected_seq, "source_system": source_system}
        with self.database.transaction(immediate=True) as connection:
            waybill = self._waybill_row(connection, waybill_id)
            self._hub_actor(connection, actor_id, self._hub_org_id(connection, waybill), "admin", "operator")
            group = connection.execute(
                "SELECT * FROM container_groups WHERE group_id=? AND waybill_id=?", (group_id, waybill_id)
            ).fetchone()
            if group is None:
                raise NotFoundError("箱组不存在")
            category = str(category).strip()
            if category not in CLAIM_CATEGORIES:
                raise ValidationError("category 不在允许范围内")
            exception_key = self._identifier(exception_key, "exception_key")
            quantity = self._natural(quantity, "quantity")
            detected_seq = self._natural(detected_seq, "detected_seq", allow_zero=True)
            seqs = {row["seq"] for row in self._segment_rows(connection, waybill_id)}
            if detected_seq != 0 and detected_seq not in seqs:
                raise ValidationError("detected_seq 必须是运单区段或 0（交付环节）")
            source_system = self._text(source_system, "source_system", 80)

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM waybill_exceptions WHERE waybill_id=? AND exception_key=?",
                    (waybill_id, exception_key),
                ).fetchone()
                if existing:
                    if (existing["group_id"] != group_id or existing["category"] != category
                            or existing["quantity"] != quantity):
                        raise ConflictError("同一异常键已经登记不同内容")
                    return "waybill_exception", existing["exception_id"], {
                        "exception_id": existing["exception_id"], "deduplicated": True}
                if category in QUANTITY_BEARING_CATEGORIES:
                    row = connection.execute(
                        "SELECT COALESCE(SUM(quantity),0) AS total FROM waybill_exceptions "
                        "WHERE group_id=? AND category IN ('shortage','damage')",
                        (group_id,),
                    ).fetchone()
                    if row["total"] + quantity > group["quantity"]:
                        raise ValidationError("异常数量超出箱组发运数量")
                exception_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO waybill_exceptions(exception_id,waybill_id,group_id,category,exception_key,"
                    "quantity,detected_seq,source_system,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (exception_id, waybill_id, group_id, category, exception_key, quantity,
                     detected_seq, source_system, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="waybill_exception.registered",
                             resource_type="waybill_exception", resource_id=exception_id,
                             detail={"waybill_id": waybill_id, "group_id": group_id, "category": category,
                                     "exception_key": exception_key, "quantity": quantity,
                                     "detected_seq": detected_seq, "source_system": source_system},
                             occurred_at=self._now())
                return "waybill_exception", exception_id, {
                    "exception_id": exception_id, "deduplicated": False}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_exception", payload=payload, create=create)

    def open_claim_item(self, *, request_id: str, actor_id: str, exception_id: str, category: str,
                        quantity: int, claimed_minor: int, evidence_deadline: str) -> WriteReceipt:
        """把异常拆成独立索赔项目，各自约定证据截止时间。"""

        payload = {"actor_id": actor_id, "exception_id": exception_id, "category": category,
                   "quantity": quantity, "claimed_minor": claimed_minor,
                   "evidence_deadline": evidence_deadline}
        with self.database.transaction(immediate=True) as connection:
            exception = connection.execute(
                "SELECT * FROM waybill_exceptions WHERE exception_id=?", (exception_id,)
            ).fetchone()
            if exception is None:
                raise NotFoundError("异常不存在")
            waybill = self._waybill_row(connection, exception["waybill_id"])
            self._hub_actor(connection, actor_id, self._hub_org_id(connection, waybill), "admin", "operator")
            category = str(category).strip()
            if category != exception["category"]:
                raise ValidationError("索赔项目类别必须与异常类别一致")
            quantity = self._natural(quantity, "quantity")
            claimed_minor = self._money(claimed_minor, "claimed_minor")
            deadline = self._moment(evidence_deadline, "evidence_deadline")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT COALESCE(SUM(quantity),0) AS total FROM claim_items WHERE exception_id=?",
                    (exception_id,),
                ).fetchone()
                if row["total"] + quantity > exception["quantity"]:
                    raise ValidationError("索赔数量超出异常数量")
                claim_item_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO claim_items(claim_item_id,exception_id,waybill_id,category,quantity,"
                    "claimed_minor,evidence_deadline,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (claim_item_id, exception_id, exception["waybill_id"], category, quantity,
                     claimed_minor, self._iso(deadline), CLAIM_COLLECTING, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="claim_item.opened",
                             resource_type="claim_item", resource_id=claim_item_id,
                             detail={"exception_id": exception_id, "waybill_id": exception["waybill_id"],
                                     "category": category, "quantity": quantity,
                                     "claimed_minor": claimed_minor,
                                     "evidence_deadline": self._iso(deadline)},
                             occurred_at=self._now())
                return "claim_item", claim_item_id, {"claim_item_id": claim_item_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="open_claim_item", payload=payload, create=create)

    def submit_evidence(self, *, request_id: str, actor_id: str, claim_item_id: str,
                        kind: str, data: dict[str, Any]) -> WriteReceipt:
        """相关方提交证据；超过截止时间提交的证据标记为逾期。"""

        if not isinstance(data, dict) or not data:
            raise ValidationError("data 必须是非空对象")
        payload = {"actor_id": actor_id, "claim_item_id": claim_item_id, "kind": kind, "data": data}
        with self.database.transaction(immediate=True) as connection:
            claim = self._claim_row(connection, claim_item_id)
            waybill = self._waybill_row(connection, claim["waybill_id"])
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            hub_org_id = self._hub_org_id(connection, waybill)
            if actor.organization_id != hub_org_id and \
                    actor.organization_id not in self._waybill_party_orgs(connection, waybill):
                raise PermissionDenied("只有运单相关方可以提交证据")
            kind = self._text(kind, "kind", 40)
            late = 1 if self.clock.now() > self._stored_moment(claim["evidence_deadline"]) else 0

            def create() -> tuple[str, str, dict[str, Any]]:
                evidence_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO evidence_items(evidence_id,claim_item_id,submitted_by_org,submitted_by,"
                    "kind,payload_json,payload_hash,late,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (evidence_id, claim_item_id, actor.organization_id, actor_id, kind,
                     canonical_json(data), digest(data), late, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="evidence.submitted",
                             resource_type="evidence", resource_id=evidence_id,
                             detail={"claim_item_id": claim_item_id, "kind": kind,
                                     "late": bool(late), "payload_hash": digest(data)},
                             occurred_at=self._now())
                return "evidence", evidence_id, {"evidence_id": evidence_id, "late": bool(late)}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_evidence", payload=payload, create=create)

    # ---------- 责任草案、确认、申辩与仲裁 ----------

    def create_liability_draft(self, *, request_id: str, actor_id: str, claim_item_id: str,
                               awarded_minor: int, allocations: list[dict[str, Any]],
                               basis_receipt_ids: list[str]) -> WriteReceipt:
        """证据截止后形成责任草案，并记录所采用的交接回执版本。"""

        payload = {"actor_id": actor_id, "claim_item_id": claim_item_id,
                   "awarded_minor": awarded_minor, "allocations": allocations,
                   "basis_receipt_ids": basis_receipt_ids}
        with self.database.transaction(immediate=True) as connection:
            claim = self._claim_row(connection, claim_item_id)
            waybill = self._waybill_row(connection, claim["waybill_id"])
            self._hub_actor(connection, actor_id, self._hub_org_id(connection, waybill),
                            "admin", "reviewer")
            if self.clock.now() < self._stored_moment(claim["evidence_deadline"]):
                raise ConflictError("证据尚未截止，不能形成责任草案")
            if connection.execute(
                "SELECT 1 FROM claim_decisions WHERE claim_item_id=?", (claim_item_id,)
            ).fetchone():
                raise ConflictError("已存在责任决定，只能通过复审调整")
            awarded_minor = self._money(awarded_minor, "awarded_minor", allow_zero=True)
            if awarded_minor > claim["claimed_minor"]:
                raise ValidationError("认定金额不能超过索赔金额")
            allocation_rows = self._check_allocation_rows(connection, waybill["waybill_id"], allocations)
            if sum(row["amount_minor"] for row in allocation_rows) != awarded_minor:
                raise ValidationError("分摊合计必须等于认定金额")
            basis_ids = self._check_basis_receipts(connection, waybill["waybill_id"], basis_receipt_ids)
            settled = self._settled_by_segment(connection, claim_item_id)
            allocated_segments = {row["segment_id"] for row in allocation_rows}
            for segment_id, paid in settled.items():
                if paid <= 0:
                    continue
                if segment_id not in allocated_segments:
                    raise ValidationError("新的责任草案不能遗漏已结算的区段")
                if next(row["amount_minor"] for row in allocation_rows
                        if row["segment_id"] == segment_id) < paid:
                    raise ValidationError("新的责任草案不能低于已结算金额")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE liability_drafts SET status=? WHERE claim_item_id=? AND status IN (?,?)",
                    (DRAFT_SUPERSEDED, claim_item_id, DRAFT_OPEN, DRAFT_ARBITRATING),
                )
                row = connection.execute(
                    "SELECT COALESCE(MAX(version),0) AS version FROM liability_drafts WHERE claim_item_id=?",
                    (claim_item_id,),
                ).fetchone()
                version = row["version"] + 1
                draft_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO liability_drafts(draft_id,claim_item_id,version,awarded_minor,"
                    "basis_receipt_ids_json,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (draft_id, claim_item_id, version, awarded_minor, canonical_json(basis_ids),
                     DRAFT_OPEN, actor_id, self._now()),
                )
                for allocation in allocation_rows:
                    connection.execute(
                        "INSERT INTO draft_allocations(draft_id,segment_id,carrier_org_id,amount_minor) "
                        "VALUES(?,?,?,?)",
                        (draft_id, allocation["segment_id"], allocation["carrier_org_id"],
                         allocation["amount_minor"]),
                    )
                connection.execute("UPDATE claim_items SET status=? WHERE claim_item_id=?",
                                   (CLAIM_DRAFTED, claim_item_id))
                append_event(connection, actor_id=actor_id, action="liability_draft.created",
                             resource_type="liability_draft", resource_id=draft_id,
                             detail={"claim_item_id": claim_item_id, "version": version,
                                     "awarded_minor": awarded_minor, "basis_receipt_ids": basis_ids},
                             occurred_at=self._now())
                return "liability_draft", draft_id, {"draft_id": draft_id, "version": version}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_liability_draft", payload=payload, create=create)

    def _check_allocation_rows(self, connection, waybill_id: str,
                               allocations: Any) -> list[dict[str, Any]]:
        if not isinstance(allocations, list):
            raise ValidationError("allocations 必须是数组")
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        for item in allocations:
            if not isinstance(item, dict):
                raise ValidationError("allocations 元素必须是对象")
            segment_id = str(item.get("segment_id", ""))
            segment = connection.execute(
                "SELECT * FROM segment_contracts WHERE segment_id=? AND waybill_id=?",
                (segment_id, waybill_id),
            ).fetchone()
            if segment is None:
                raise ValidationError("分摊引用了不属于运单的区段")
            if segment_id in seen:
                raise ValidationError("同一区段不能重复分摊")
            seen.add(segment_id)
            amount = self._money(item.get("amount_minor"), "amount_minor", allow_zero=True)
            if amount > segment["liability_limit_minor"]:
                raise ValidationError("分摊金额超过区段责任上限")
            rows.append({"segment_id": segment_id, "carrier_org_id": segment["carrier_org_id"],
                         "amount_minor": amount})
        return rows

    def _check_basis_receipts(self, connection, waybill_id: str, basis_receipt_ids: Any) -> list[str]:
        if not isinstance(basis_receipt_ids, list):
            raise ValidationError("basis_receipt_ids 必须是数组")
        ids: list[str] = []
        for value in basis_receipt_ids:
            receipt_id = str(value)
            if connection.execute(
                "SELECT 1 FROM handover_receipts WHERE receipt_id=? AND waybill_id=?",
                (receipt_id, waybill_id),
            ).fetchone() is None:
                raise ValidationError("责任依据引用了不属于运单的交接回执")
            if receipt_id not in ids:
                ids.append(receipt_id)
        return sorted(ids)

    def respond_to_draft(self, *, request_id: str, actor_id: str, draft_id: str,
                         stance: str, note: str = "") -> WriteReceipt:
        """相关方对责任草案确认或申辩；全部确认时自动生成责任决定。"""

        payload = {"actor_id": actor_id, "draft_id": draft_id, "stance": stance, "note": note}
        with self.database.transaction(immediate=True) as connection:
            draft = self._draft_row(connection, draft_id)
            claim = self._claim_row(connection, draft["claim_item_id"])
            waybill = self._waybill_row(connection, claim["waybill_id"])
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            if draft["status"] != DRAFT_OPEN:
                raise ConflictError("责任草案当前不在可回应状态")
            allocations = connection.execute(
                "SELECT * FROM draft_allocations WHERE draft_id=?", (draft_id,)
            ).fetchall()
            parties = {waybill["customer_org_id"]} | {row["carrier_org_id"] for row in allocations}
            if actor.organization_id not in parties:
                raise PermissionDenied("只有索赔相关方可以回应草案")
            stance = str(stance).strip()
            if stance not in DRAFT_STANCES:
                raise ValidationError("stance 必须是 confirm 或 rebut")
            note = self._note(note, "note")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO draft_responses(draft_id,org_id,stance,note,actor_id,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (draft_id, actor.organization_id, stance, note, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该相关方已经回应过此草案") from exc
                append_event(connection, actor_id=actor_id, action="draft_response.recorded",
                             resource_type="liability_draft", resource_id=draft_id,
                             detail={"org_id": actor.organization_id, "stance": stance},
                             occurred_at=self._now())
                decision_id = None
                draft_status = DRAFT_OPEN
                if stance == "rebut":
                    draft_status = DRAFT_ARBITRATING
                    connection.execute("UPDATE liability_drafts SET status=? WHERE draft_id=?",
                                       (DRAFT_ARBITRATING, draft_id))
                else:
                    responses = connection.execute(
                        "SELECT org_id, stance FROM draft_responses WHERE draft_id=?", (draft_id,)
                    ).fetchall()
                    confirmed = {row["org_id"] for row in responses if row["stance"] == "confirm"}
                    if parties <= confirmed:
                        draft_status = DRAFT_CONFIRMED
                        connection.execute("UPDATE liability_drafts SET status=? WHERE draft_id=?",
                                           (DRAFT_CONFIRMED, draft_id))
                        decision_id = self._create_decision(
                            connection, actor_id=actor_id, claim=claim, draft=draft,
                            allocations=[{"segment_id": row["segment_id"],
                                          "carrier_org_id": row["carrier_org_id"],
                                          "amount_minor": row["amount_minor"]} for row in allocations],
                            decided_via="confirmed")
                return "draft_response", f"{draft_id}:{actor.organization_id}", {
                    "draft_id": draft_id, "org_id": actor.organization_id,
                    "draft_status": draft_status, "decision_id": decision_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="respond_to_draft", payload=payload, create=create)

    def record_arbitration(self, *, request_id: str, actor_id: str, draft_id: str,
                           allocations: list[dict[str, Any]]) -> WriteReceipt:
        """枢纽对被申辩的草案记录仲裁结果，形成不可删除的责任决定。"""

        payload = {"actor_id": actor_id, "draft_id": draft_id, "allocations": allocations}
        with self.database.transaction(immediate=True) as connection:
            draft = self._draft_row(connection, draft_id)
            claim = self._claim_row(connection, draft["claim_item_id"])
            waybill = self._waybill_row(connection, claim["waybill_id"])
            self._hub_actor(connection, actor_id, self._hub_org_id(connection, waybill),
                            "admin", "reviewer")
            if draft["status"] != DRAFT_ARBITRATING:
                raise ConflictError("只有被申辩的草案可以仲裁")
            allocation_rows = self._check_allocation_rows(connection, waybill["waybill_id"], allocations)
            awarded = sum(row["amount_minor"] for row in allocation_rows)
            if awarded > claim["claimed_minor"]:
                raise ValidationError("仲裁认定金额不能超过索赔金额")
            settled = self._settled_by_segment(connection, claim["claim_item_id"])
            allocated = {row["segment_id"]: row["amount_minor"] for row in allocation_rows}
            for segment_id, paid in settled.items():
                if paid > 0 and allocated.get(segment_id, 0) < paid:
                    raise ValidationError("仲裁结果不能低于已结算金额")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute("UPDATE liability_drafts SET status=? WHERE draft_id=?",
                                   (DRAFT_ARBITRATED, draft_id))
                decision_id = self._create_decision(
                    connection, actor_id=actor_id, claim=claim, draft=draft,
                    allocations=allocation_rows, decided_via="arbitrated")
                return "claim_decision", decision_id, {"decision_id": decision_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_arbitration", payload=payload, create=create)

    def _create_decision(self, connection, *, actor_id: str, claim, draft,
                         allocations: list[dict[str, Any]], decided_via: str) -> str:
        decision_id = uuid.uuid4().hex
        awarded = sum(row["amount_minor"] for row in allocations)
        connection.execute(
            "INSERT INTO claim_decisions(decision_id,claim_item_id,draft_id,decided_via,awarded_minor,"
            "basis_receipt_ids_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (decision_id, claim["claim_item_id"], draft["draft_id"], decided_via, awarded,
             draft["basis_receipt_ids_json"], actor_id, self._now()),
        )
        for allocation in allocations:
            connection.execute(
                "INSERT INTO decision_allocations(decision_id,segment_id,carrier_org_id,amount_minor) "
                "VALUES(?,?,?,?)",
                (decision_id, allocation["segment_id"], allocation["carrier_org_id"],
                 allocation["amount_minor"]),
            )
        connection.execute("UPDATE claim_items SET status=? WHERE claim_item_id=?",
                           (CLAIM_DECIDED, claim["claim_item_id"]))
        append_event(connection, actor_id=actor_id, action="claim_decision.created",
                     resource_type="claim_decision", resource_id=decision_id,
                     detail={"claim_item_id": claim["claim_item_id"], "decided_via": decided_via,
                             "awarded_minor": awarded,
                             "basis_receipt_ids": json.loads(draft["basis_receipt_ids_json"])},
                     occurred_at=self._now())
        return decision_id

    # ---------- 部分和解、复审与追加分录 ----------

    def settle_allocation(self, *, request_id: str, actor_id: str, claim_item_id: str,
                          segment_id: str, amount_minor: int) -> WriteReceipt:
        """按责任认定结算；无争议分摊可在决定作出前先行支付。"""

        payload = {"actor_id": actor_id, "claim_item_id": claim_item_id,
                   "segment_id": segment_id, "amount_minor": amount_minor}
        with self.database.transaction(immediate=True) as connection:
            claim = self._claim_row(connection, claim_item_id)
            waybill = self._waybill_row(connection, claim["waybill_id"])
            self._hub_actor(connection, actor_id, self._hub_org_id(connection, waybill),
                            "admin", "operator")
            amount_minor = self._money(amount_minor, "amount_minor")
            segment = connection.execute(
                "SELECT * FROM segment_contracts WHERE segment_id=? AND waybill_id=?",
                (segment_id, waybill["waybill_id"]),
            ).fetchone()
            if segment is None:
                raise ValidationError("结算引用了不属于运单的区段")
            decision = connection.execute(
                "SELECT * FROM claim_decisions WHERE claim_item_id=?", (claim_item_id,)
            ).fetchone()
            if decision:
                allocation = connection.execute(
                    "SELECT amount_minor FROM decision_allocations WHERE decision_id=? AND segment_id=?",
                    (decision["decision_id"], segment_id),
                ).fetchone()
                if allocation is None:
                    raise ValidationError("责任决定未涉及该区段")
                cap = allocation["amount_minor"]
                kind = ENTRY_DECISION
            else:
                draft = connection.execute(
                    "SELECT * FROM liability_drafts WHERE claim_item_id=? AND status IN (?,?) "
                    "ORDER BY version DESC LIMIT 1",
                    (claim_item_id, DRAFT_OPEN, DRAFT_ARBITRATING),
                ).fetchone()
                if draft is None:
                    raise ConflictError("没有可结算的责任基础")
                allocation = connection.execute(
                    "SELECT * FROM draft_allocations WHERE draft_id=? AND segment_id=?",
                    (draft["draft_id"], segment_id),
                ).fetchone()
                if allocation is None:
                    raise ValidationError("责任草案未涉及该区段")
                responses = {row["org_id"]: row["stance"] for row in connection.execute(
                    "SELECT org_id, stance FROM draft_responses WHERE draft_id=?", (draft["draft_id"],))}
                if responses.get(waybill["customer_org_id"]) != "confirm" or \
                        responses.get(allocation["carrier_org_id"]) != "confirm":
                    raise ConflictError("该分摊仍存在争议，不能先行结算")
                cap = allocation["amount_minor"]
                kind = ENTRY_PARTIAL

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT COALESCE(SUM(amount_minor),0) AS paid FROM settlement_entries "
                    "WHERE claim_item_id=? AND segment_id=? AND kind IN (?,?)",
                    (claim_item_id, segment_id, ENTRY_PARTIAL, ENTRY_DECISION),
                ).fetchone()
                if row["paid"] + amount_minor > cap:
                    raise ConflictError("结算金额超过责任认定额度")
                total = connection.execute(
                    "SELECT COALESCE(SUM(amount_minor),0) AS paid FROM settlement_entries "
                    "WHERE claim_item_id=? AND segment_id=?",
                    (claim_item_id, segment_id),
                ).fetchone()
                if total["paid"] + amount_minor > segment["liability_limit_minor"]:
                    raise ValidationError("结算金额超过区段责任上限")
                entry_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO settlement_entries(entry_id,claim_item_id,segment_id,payer_org_id,"
                    "payee_org_id,amount_minor,kind,review_id,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (entry_id, claim_item_id, segment_id, segment["carrier_org_id"],
                     waybill["customer_org_id"], amount_minor, kind, None, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="settlement.recorded",
                             resource_type="settlement_entry", resource_id=entry_id,
                             detail={"claim_item_id": claim_item_id, "segment_id": segment_id,
                                     "amount_minor": amount_minor, "kind": kind},
                             occurred_at=self._now())
                return "settlement_entry", entry_id, {"entry_id": entry_id, "kind": kind}

            return self._idempotent(connection, request_id=request_id,
                                    action="settle_allocation", payload=payload, create=create)

    def request_review(self, *, request_id: str, actor_id: str, claim_item_id: str,
                       evidence_id: str) -> WriteReceipt:
        """逾期补交的证据只能触发复审，不能改动原责任决定。"""

        payload = {"actor_id": actor_id, "claim_item_id": claim_item_id, "evidence_id": evidence_id}
        with self.database.transaction(immediate=True) as connection:
            claim = self._claim_row(connection, claim_item_id)
            waybill = self._waybill_row(connection, claim["waybill_id"])
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            hub_org_id = self._hub_org_id(connection, waybill)
            if actor.organization_id != hub_org_id and \
                    actor.organization_id not in self._waybill_party_orgs(connection, waybill):
                raise PermissionDenied("只有运单相关方可以申请复审")
            decision = connection.execute(
                "SELECT * FROM claim_decisions WHERE claim_item_id=?", (claim_item_id,)
            ).fetchone()
            if decision is None:
                raise ConflictError("尚未形成责任决定，不能复审")
            evidence = connection.execute(
                "SELECT * FROM evidence_items WHERE evidence_id=? AND claim_item_id=?",
                (evidence_id, claim_item_id),
            ).fetchone()
            if evidence is None:
                raise NotFoundError("证据不存在")
            if not evidence["late"]:
                raise ValidationError("只有逾期补交的证据可以触发复审")

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute(
                    "SELECT 1 FROM reviews WHERE claim_item_id=? AND status=?",
                    (claim_item_id, REVIEW_OPEN),
                ).fetchone():
                    raise ConflictError("已存在未关闭的复审")
                review_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO reviews(review_id,claim_item_id,decision_id,trigger_evidence_id,status,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (review_id, claim_item_id, decision["decision_id"], evidence_id, REVIEW_OPEN,
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="review.opened",
                             resource_type="review", resource_id=review_id,
                             detail={"claim_item_id": claim_item_id,
                                     "decision_id": decision["decision_id"],
                                     "trigger_evidence_id": evidence_id},
                             occurred_at=self._now())
                return "review", review_id, {"review_id": review_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="request_review", payload=payload, create=create)

    def close_review(self, *, request_id: str, actor_id: str, review_id: str,
                     additional_entries: list[dict[str, Any]]) -> WriteReceipt:
        """复审只能产生追加分录，原责任决定与既有分录保持不变。"""

        payload = {"actor_id": actor_id, "review_id": review_id,
                   "additional_entries": additional_entries}
        with self.database.transaction(immediate=True) as connection:
            review = self._review_row(connection, review_id)
            claim = self._claim_row(connection, review["claim_item_id"])
            waybill = self._waybill_row(connection, claim["waybill_id"])
            self._hub_actor(connection, actor_id, self._hub_org_id(connection, waybill),
                            "admin", "reviewer")
            if review["status"] != REVIEW_OPEN:
                raise ConflictError("复审已关闭")
            if not isinstance(additional_entries, list):
                raise ValidationError("additional_entries 必须是数组")
            entries: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in additional_entries:
                if not isinstance(item, dict):
                    raise ValidationError("additional_entries 元素必须是对象")
                segment_id = str(item.get("segment_id", ""))
                segment = connection.execute(
                    "SELECT * FROM segment_contracts WHERE segment_id=? AND waybill_id=?",
                    (segment_id, waybill["waybill_id"]),
                ).fetchone()
                if segment is None:
                    raise ValidationError("追加分录引用了不属于运单的区段")
                if segment_id in seen:
                    raise ValidationError("同一区段不能重复追加")
                seen.add(segment_id)
                amount = item.get("amount_minor")
                if isinstance(amount, bool) or not isinstance(amount, int) or amount == 0:
                    raise ValidationError("amount_minor 必须是非零整数")
                entries.append({"segment": segment, "amount_minor": amount})

            def create() -> tuple[str, str, dict[str, Any]]:
                for entry in entries:
                    segment = entry["segment"]
                    row = connection.execute(
                        "SELECT COALESCE(SUM(amount_minor),0) AS paid FROM settlement_entries "
                        "WHERE claim_item_id=? AND segment_id=?",
                        (claim["claim_item_id"], segment["segment_id"]),
                    ).fetchone()
                    new_total = row["paid"] + entry["amount_minor"]
                    if new_total < 0:
                        raise ValidationError("追加分录导致累计结算为负")
                    if new_total > segment["liability_limit_minor"]:
                        raise ValidationError("追加分录超过区段责任上限")
                    entry_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO settlement_entries(entry_id,claim_item_id,segment_id,payer_org_id,"
                        "payee_org_id,amount_minor,kind,review_id,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (entry_id, claim["claim_item_id"], segment["segment_id"],
                         segment["carrier_org_id"], waybill["customer_org_id"],
                         entry["amount_minor"], ENTRY_ADDITIONAL, review_id, actor_id, self._now()),
                    )
                connection.execute("UPDATE reviews SET status=?, closed_at=? WHERE review_id=?",
                                   (REVIEW_CLOSED, self._now(), review_id))
                append_event(connection, actor_id=actor_id, action="review.closed",
                             resource_type="review", resource_id=review_id,
                             detail={"claim_item_id": claim["claim_item_id"],
                                     "additional_entries": [
                                         {"segment_id": entry["segment"]["segment_id"],
                                          "amount_minor": entry["amount_minor"]} for entry in entries]},
                             occurred_at=self._now())
                return "review", review_id, {"review_id": review_id, "entries": len(entries)}

            return self._idempotent(connection, request_id=request_id,
                                    action="close_review", payload=payload, create=create)

    # ---------- 最小披露视图 ----------

    def _segment_dict(self, row, *, include_limit: bool) -> dict[str, Any]:
        item = {"segment_id": row["segment_id"], "seq": row["seq"], "mode": row["mode"],
                "carrier_org_id": row["carrier_org_id"]}
        if include_limit:
            item["liability_limit_minor"] = row["liability_limit_minor"]
        return item

    def _group_dict(self, connection, row) -> dict[str, Any]:
        seals = connection.execute(
            "SELECT seal_id, seal_no FROM seals WHERE group_id=? ORDER BY seal_no", (row["group_id"],)
        ).fetchall()
        return {"group_id": row["group_id"], "group_no": row["group_no"],
                "quantity": row["quantity"], "description": row["description"],
                "seals": [{"seal_id": seal["seal_id"], "seal_no": seal["seal_no"]} for seal in seals]}

    def _handover_dict(self, row) -> dict[str, Any]:
        return {"receipt_id": row["receipt_id"], "waybill_id": row["waybill_id"],
                "group_id": row["group_id"], "from_seq": row["from_seq"], "to_seq": row["to_seq"],
                "version": row["version"], "occurred_at": row["occurred_at"],
                "quantity_received": row["quantity_received"],
                "seal_intact": bool(row["seal_intact"]), "condition": row["condition"],
                "recorded_by": row["recorded_by"], "created_at": row["created_at"]}

    def _exception_dict(self, row) -> dict[str, Any]:
        return {"exception_id": row["exception_id"], "group_id": row["group_id"],
                "category": row["category"], "exception_key": row["exception_key"],
                "quantity": row["quantity"], "detected_seq": row["detected_seq"],
                "source_system": row["source_system"], "created_at": row["created_at"]}

    def _claim_summary(self, row, *, include_amount: bool) -> dict[str, Any]:
        item = {"claim_item_id": row["claim_item_id"], "exception_id": row["exception_id"],
                "category": row["category"], "quantity": row["quantity"],
                "status": row["status"], "evidence_deadline": row["evidence_deadline"]}
        if include_amount:
            item["claimed_minor"] = row["claimed_minor"]
        return item

    def _evidence_dict(self, row, *, include_payload: bool) -> dict[str, Any]:
        item = {"evidence_id": row["evidence_id"], "kind": row["kind"],
                "submitted_by_org": row["submitted_by_org"], "submitted_by": row["submitted_by"],
                "late": bool(row["late"]), "payload_hash": row["payload_hash"],
                "created_at": row["created_at"]}
        if include_payload:
            item["payload"] = json.loads(row["payload_json"])
        return item

    def _draft_allocation_dicts(self, connection, draft_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT a.segment_id, a.carrier_org_id, a.amount_minor, s.seq FROM draft_allocations a "
            "JOIN segment_contracts s ON s.segment_id=a.segment_id WHERE a.draft_id=? ORDER BY s.seq",
            (draft_id,),
        ).fetchall()
        return [{"segment_id": row["segment_id"], "seq": row["seq"],
                 "carrier_org_id": row["carrier_org_id"], "amount_minor": row["amount_minor"]}
                for row in rows]

    def _decision_allocation_dicts(self, connection, decision_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT a.segment_id, a.carrier_org_id, a.amount_minor, s.seq FROM decision_allocations a "
            "JOIN segment_contracts s ON s.segment_id=a.segment_id WHERE a.decision_id=? ORDER BY s.seq",
            (decision_id,),
        ).fetchall()
        return [{"segment_id": row["segment_id"], "seq": row["seq"],
                 "carrier_org_id": row["carrier_org_id"], "amount_minor": row["amount_minor"]}
                for row in rows]

    def _draft_dict(self, connection, row) -> dict[str, Any]:
        responses = connection.execute(
            "SELECT * FROM draft_responses WHERE draft_id=? ORDER BY created_at", (row["draft_id"],)
        ).fetchall()
        return {"draft_id": row["draft_id"], "version": row["version"],
                "awarded_minor": row["awarded_minor"], "status": row["status"],
                "basis_receipt_ids": json.loads(row["basis_receipt_ids_json"]),
                "created_at": row["created_at"],
                "allocations": self._draft_allocation_dicts(connection, row["draft_id"]),
                "responses": [{"org_id": response["org_id"], "stance": response["stance"],
                               "note": response["note"], "created_at": response["created_at"]}
                              for response in responses]}

    def _decision_dict(self, connection, row) -> dict[str, Any]:
        return {"decision_id": row["decision_id"], "decided_via": row["decided_via"],
                "awarded_minor": row["awarded_minor"],
                "basis_receipt_ids": json.loads(row["basis_receipt_ids_json"]),
                "created_at": row["created_at"],
                "allocations": self._decision_allocation_dicts(connection, row["decision_id"])}

    def _settlement_dict(self, row) -> dict[str, Any]:
        return {"entry_id": row["entry_id"], "segment_id": row["segment_id"],
                "payer_org_id": row["payer_org_id"], "payee_org_id": row["payee_org_id"],
                "amount_minor": row["amount_minor"], "kind": row["kind"],
                "review_id": row["review_id"], "created_at": row["created_at"]}

    def _review_dict(self, row) -> dict[str, Any]:
        return {"review_id": row["review_id"], "decision_id": row["decision_id"],
                "trigger_evidence_id": row["trigger_evidence_id"], "status": row["status"],
                "created_at": row["created_at"], "closed_at": row["closed_at"]}

    def _latest_handovers(self, connection, waybill_id: str):
        return connection.execute(
            "SELECT h.* FROM handover_receipts h "
            "JOIN (SELECT group_id, from_seq, to_seq, MAX(version) AS version FROM handover_receipts "
            "WHERE waybill_id=? GROUP BY group_id, from_seq, to_seq) latest "
            "ON h.group_id=latest.group_id AND h.from_seq=latest.from_seq AND h.to_seq=latest.to_seq "
            "AND h.version=latest.version WHERE h.waybill_id=? "
            "ORDER BY h.group_id, h.from_seq, h.to_seq",
            (waybill_id, waybill_id),
        ).fetchall()

    def _view_scope(self, connection, actor_id: str, waybill) -> tuple[str, list[Any]]:
        actor = self._actor(connection, actor_id)
        hub_org_id = self._hub_org_id(connection, waybill)
        segments = self._segment_rows(connection, waybill["waybill_id"])
        carrier_orgs = {row["carrier_org_id"] for row in segments}
        if actor.organization_id == hub_org_id and actor.role in ("admin", "operator", "reviewer"):
            return "full", segments
        if actor.role == "auditor":
            return "full", segments
        if actor.organization_id == waybill["customer_org_id"]:
            return "customer", segments
        if actor.organization_id in carrier_orgs:
            return "carrier", segments
        raise PermissionDenied("无权查看该运单材料")

    def _carrier_claim_ids(self, connection, waybill_id: str, org_id: str,
                           own_seqs: set[int]) -> set[str]:
        ids: set[str] = set()
        if own_seqs:
            placeholders = ",".join("?" * len(own_seqs))
            for row in connection.execute(
                "SELECT c.claim_item_id FROM claim_items c "
                "JOIN waybill_exceptions e ON e.exception_id=c.exception_id "
                f"WHERE c.waybill_id=? AND e.detected_seq IN ({placeholders})",
                (waybill_id, *own_seqs),
            ).fetchall():
                ids.add(row["claim_item_id"])
        for row in connection.execute(
            "SELECT DISTINCT d.claim_item_id FROM liability_drafts d "
            "JOIN draft_allocations a ON a.draft_id=d.draft_id "
            "JOIN claim_items c ON c.claim_item_id=d.claim_item_id "
            "WHERE c.waybill_id=? AND a.carrier_org_id=?",
            (waybill_id, org_id),
        ).fetchall():
            ids.add(row["claim_item_id"])
        for row in connection.execute(
            "SELECT DISTINCT c.claim_item_id FROM claim_decisions d "
            "JOIN decision_allocations a ON a.decision_id=d.decision_id "
            "JOIN claim_items c ON c.claim_item_id=d.claim_item_id "
            "WHERE c.waybill_id=? AND a.carrier_org_id=?",
            (waybill_id, org_id),
        ).fetchall():
            ids.add(row["claim_item_id"])
        return ids

    def get_waybill_view(self, *, actor_id: str, waybill_id: str) -> dict[str, Any]:
        """按操作者身份返回最小披露的运单视图。"""

        connection = self.database.connection
        waybill = self._waybill_row(connection, waybill_id)
        perspective, segments = self._view_scope(connection, actor_id, waybill)
        actor = self._actor(connection, actor_id)
        groups = [self._group_dict(connection, row) for row in connection.execute(
            "SELECT * FROM container_groups WHERE waybill_id=? ORDER BY group_no", (waybill_id,))]
        handovers = [self._handover_dict(row) for row in self._latest_handovers(connection, waybill_id)]
        exceptions = [self._exception_dict(row) for row in connection.execute(
            "SELECT * FROM waybill_exceptions WHERE waybill_id=? ORDER BY created_at", (waybill_id,))]
        claims = connection.execute(
            "SELECT * FROM claim_items WHERE waybill_id=? ORDER BY created_at", (waybill_id,)
        ).fetchall()
        base = {"waybill_id": waybill["waybill_id"], "status": waybill["status"],
                "customer_org_id": waybill["customer_org_id"], "created_at": waybill["created_at"]}
        if perspective == "full":
            return {**base, "perspective": "full",
                    "segments": [self._segment_dict(row, include_limit=True) for row in segments],
                    "groups": groups, "handovers": handovers, "exceptions": exceptions,
                    "claim_items": [self._claim_summary(row, include_amount=True) for row in claims]}
        if perspective == "customer":
            return {**base, "perspective": "customer",
                    "segments": [self._segment_dict(row, include_limit=False) for row in segments],
                    "groups": groups, "handovers": handovers, "exceptions": exceptions,
                    "claim_items": [self._claim_summary(row, include_amount=True) for row in claims]}
        own_seqs = {row["seq"] for row in segments if row["carrier_org_id"] == actor.organization_id}
        own_handovers = [item for item in handovers
                         if item["from_seq"] in own_seqs
                         or (item["to_seq"] != 0 and item["to_seq"] in own_seqs)]
        related_claim_ids = self._carrier_claim_ids(connection, waybill_id,
                                                    actor.organization_id, own_seqs)
        visible_claims = [row for row in claims if row["claim_item_id"] in related_claim_ids]
        visible_exception_ids = {row["exception_id"] for row in visible_claims}
        visible_exceptions = [item for item in exceptions
                              if item["detected_seq"] in own_seqs
                              or item["exception_id"] in visible_exception_ids]
        return {**base, "perspective": "carrier",
                "segments": [self._segment_dict(row, include_limit=True) for row in segments
                             if row["carrier_org_id"] == actor.organization_id],
                "groups": groups, "handovers": own_handovers, "exceptions": visible_exceptions,
                "claim_items": [self._claim_summary(row, include_amount=False)
                                for row in visible_claims]}

    def get_claim_item_view(self, *, actor_id: str, claim_item_id: str) -> dict[str, Any]:
        """按操作者身份返回最小披露的索赔项目视图。"""

        connection = self.database.connection
        claim = self._claim_row(connection, claim_item_id)
        waybill = self._waybill_row(connection, claim["waybill_id"])
        perspective, segments = self._view_scope(connection, actor_id, waybill)
        actor = self._actor(connection, actor_id)
        exception = connection.execute(
            "SELECT * FROM waybill_exceptions WHERE exception_id=?", (claim["exception_id"],)
        ).fetchone()
        if perspective == "carrier":
            own_seqs = {row["seq"] for row in segments
                        if row["carrier_org_id"] == actor.organization_id}
            related = self._carrier_claim_ids(connection, waybill["waybill_id"],
                                              actor.organization_id, own_seqs)
            if claim_item_id not in related:
                raise PermissionDenied("该索赔项目与当前承运方无关")
        evidence_rows = connection.execute(
            "SELECT * FROM evidence_items WHERE claim_item_id=? ORDER BY created_at", (claim_item_id,)
        ).fetchall()
        draft_rows = connection.execute(
            "SELECT * FROM liability_drafts WHERE claim_item_id=? ORDER BY version", (claim_item_id,)
        ).fetchall()
        decision = connection.execute(
            "SELECT * FROM claim_decisions WHERE claim_item_id=?", (claim_item_id,)
        ).fetchone()
        settlement_rows = connection.execute(
            "SELECT * FROM settlement_entries WHERE claim_item_id=? ORDER BY created_at",
            (claim_item_id,),
        ).fetchall()
        review_rows = connection.execute(
            "SELECT * FROM reviews WHERE claim_item_id=? ORDER BY created_at", (claim_item_id,)
        ).fetchall()
        if perspective in ("full", "customer"):
            include_all = perspective == "full"
            return {"perspective": perspective,
                    "claim_item": self._claim_summary(claim, include_amount=True),
                    "exception": self._exception_dict(exception),
                    "evidence": [self._evidence_dict(
                        row, include_payload=include_all
                        or row["submitted_by_org"] == actor.organization_id)
                        for row in evidence_rows],
                    "drafts": [self._draft_dict(connection, row) for row in draft_rows],
                    "decision": self._decision_dict(connection, decision) if decision else None,
                    "settlements": [self._settlement_dict(row) for row in settlement_rows],
                    "reviews": [self._review_dict(row) for row in review_rows]}
        org_id = actor.organization_id
        drafts = []
        for row in draft_rows:
            allocations = [item for item in self._draft_allocation_dicts(connection, row["draft_id"])
                           if item["carrier_org_id"] == org_id]
            responses = connection.execute(
                "SELECT org_id, stance, created_at FROM draft_responses WHERE draft_id=? AND org_id=?",
                (row["draft_id"], org_id),
            ).fetchall()
            drafts.append({"draft_id": row["draft_id"], "version": row["version"],
                           "status": row["status"], "allocations": allocations,
                           "responses": [{"org_id": response["org_id"], "stance": response["stance"],
                                          "created_at": response["created_at"]}
                                         for response in responses]})
        decision_view = None
        if decision:
            decision_view = {
                "decision_id": decision["decision_id"], "decided_via": decision["decided_via"],
                "created_at": decision["created_at"],
                "allocations": [item for item in
                                self._decision_allocation_dicts(connection, decision["decision_id"])
                                if item["carrier_org_id"] == org_id]}
        return {"perspective": "carrier",
                "claim_item": self._claim_summary(claim, include_amount=False),
                "exception": {"exception_id": exception["exception_id"],
                              "category": exception["category"],
                              "quantity": exception["quantity"],
                              "detected_seq": exception["detected_seq"]},
                "evidence": [self._evidence_dict(
                    row, include_payload=row["submitted_by_org"] == org_id)
                    for row in evidence_rows],
                "drafts": drafts,
                "decision": decision_view,
                "settlements": [self._settlement_dict(row) for row in settlement_rows
                                if row["payer_org_id"] == org_id],
                "reviews": [{"review_id": row["review_id"], "status": row["status"],
                             "created_at": row["created_at"], "closed_at": row["closed_at"]}
                            for row in review_rows]}

    def list_handover_history(self, *, actor_id: str, waybill_id: str, group_id: str,
                              from_seq: int, to_seq: int) -> list[dict[str, Any]]:
        """返回一个交接点的全部回执版本，便于还原责任判断依据。"""

        connection = self.database.connection
        waybill = self._waybill_row(connection, waybill_id)
        if isinstance(from_seq, bool) or not isinstance(from_seq, int) \
                or isinstance(to_seq, bool) or not isinstance(to_seq, int):
            raise ValidationError("from_seq 与 to_seq 必须是整数")
        perspective, segments = self._view_scope(connection, actor_id, waybill)
        if perspective == "carrier":
            actor = self._actor(connection, actor_id)
            own_seqs = {row["seq"] for row in segments
                        if row["carrier_org_id"] == actor.organization_id}
            if from_seq not in own_seqs and not (to_seq != 0 and to_seq in own_seqs):
                raise PermissionDenied("无权查看该交接点的历史版本")
        rows = connection.execute(
            "SELECT * FROM handover_receipts WHERE waybill_id=? AND group_id=? AND from_seq=? "
            "AND to_seq=? ORDER BY version",
            (waybill_id, group_id, from_seq, to_seq),
        ).fetchall()
        if not rows:
            raise NotFoundError("交接回执不存在")
        return [self._handover_dict(row) for row in rows]

    # ---------- 监管核对 ----------

    def conservation_report(self, *, actor_id: str, waybill_id: str) -> dict[str, Any]:
        """核对运单的数量守恒与赔款守恒，并标注责任判断采用的交接版本。"""

        connection = self.database.connection
        waybill = self._waybill_row(connection, waybill_id)
        actor = self._actor(connection, actor_id)
        hub_org_id = self._hub_org_id(connection, waybill)
        is_hub_manager = actor.organization_id == hub_org_id and actor.role in ("admin", "reviewer")
        if not (actor.role == "auditor" or is_hub_manager):
            raise PermissionDenied("只有监管人员或枢纽管理方可以核对守恒")
        segments = self._segment_rows(connection, waybill_id)
        segment_by_id = {row["segment_id"]: row for row in segments}
        carrier_orgs = {row["carrier_org_id"] for row in segments}
        customer_org = waybill["customer_org_id"]

        quantity_violations: list[str] = []
        group_reports: list[dict[str, Any]] = []
        for group in connection.execute(
            "SELECT * FROM container_groups WHERE waybill_id=? ORDER BY group_no", (waybill_id,)
        ).fetchall():
            handovers = [self._handover_dict(row) for row in self._latest_handovers(connection, waybill_id)
                         if row["group_id"] == group["group_id"]]
            handovers.sort(key=lambda item: (item["from_seq"], item["to_seq"]))
            chain: list[dict[str, Any]] = []
            previous = group["quantity"]
            for handover in handovers:
                within_previous = handover["quantity_received"] <= previous
                if not within_previous:
                    quantity_violations.append(
                        f"箱组 {group['group_no']} 在交接点 {handover['from_seq']}->"
                        f"{handover['to_seq']} 的接收数量超过上一环节")
                chain.append({"from_seq": handover["from_seq"], "to_seq": handover["to_seq"],
                              "version": handover["version"],
                              "quantity_received": handover["quantity_received"],
                              "within_previous": within_previous})
                previous = handover["quantity_received"]
            exception_reports: list[dict[str, Any]] = []
            bearing_total = 0
            shortage_total = 0
            for exception in connection.execute(
                "SELECT * FROM waybill_exceptions WHERE group_id=? ORDER BY created_at",
                (group["group_id"],)
            ).fetchall():
                claims_quantity = connection.execute(
                    "SELECT COALESCE(SUM(quantity),0) AS total FROM claim_items WHERE exception_id=?",
                    (exception["exception_id"],),
                ).fetchone()["total"]
                claims_within = claims_quantity <= exception["quantity"]
                if not claims_within:
                    quantity_violations.append(
                        f"异常 {exception['exception_key']} 的索赔数量超过异常数量")
                if exception["category"] in QUANTITY_BEARING_CATEGORIES:
                    bearing_total += exception["quantity"]
                if exception["category"] == "shortage":
                    shortage_total += exception["quantity"]
                exception_reports.append({"exception_id": exception["exception_id"],
                                          "category": exception["category"],
                                          "quantity": exception["quantity"],
                                          "claims_quantity": claims_quantity,
                                          "claims_within_exception": claims_within})
            bearing_within = bearing_total <= group["quantity"]
            if not bearing_within:
                quantity_violations.append(
                    f"箱组 {group['group_no']} 的短少与破损异常总量超过发运数量")
            final = next((item for item in handovers if item["to_seq"] == 0), None)
            delivery_conserved = None
            if final is not None:
                delivery_conserved = final["quantity_received"] + shortage_total == group["quantity"]
                if not delivery_conserved:
                    quantity_violations.append(
                        f"箱组 {group['group_no']} 的交付数量与短少数量之和不等于发运数量")
            group_reports.append({"group_id": group["group_id"], "group_no": group["group_no"],
                                  "shipped": group["quantity"], "handovers": chain,
                                  "exceptions": exception_reports,
                                  "bearing_exceptions_quantity": bearing_total,
                                  "bearing_within_shipped": bearing_within,
                                  "delivery_conserved": delivery_conserved})

        compensation_violations: list[str] = []
        claim_reports: list[dict[str, Any]] = []
        total_claimed = 0
        total_awarded = 0
        total_settled = 0
        for claim in connection.execute(
            "SELECT * FROM claim_items WHERE waybill_id=? ORDER BY created_at", (waybill_id,)
        ).fetchall():
            decision = connection.execute(
                "SELECT * FROM claim_decisions WHERE claim_item_id=?", (claim["claim_item_id"],)
            ).fetchone()
            if decision:
                basis = "decision"
                awarded = decision["awarded_minor"]
                allocations = {row["segment_id"]: row["amount_minor"] for row in connection.execute(
                    "SELECT * FROM decision_allocations WHERE decision_id=?",
                    (decision["decision_id"],))}
                basis_receipt_ids = json.loads(decision["basis_receipt_ids_json"])
            else:
                draft = connection.execute(
                    "SELECT * FROM liability_drafts WHERE claim_item_id=? "
                    "ORDER BY version DESC LIMIT 1",
                    (claim["claim_item_id"],),
                ).fetchone()
                basis = "draft" if draft else "none"
                awarded = draft["awarded_minor"] if draft else 0
                allocations = ({row["segment_id"]: row["amount_minor"] for row in connection.execute(
                    "SELECT * FROM draft_allocations WHERE draft_id=?", (draft["draft_id"],))}
                    if draft else {})
                basis_receipt_ids = json.loads(draft["basis_receipt_ids_json"]) if draft else []
            if sum(allocations.values()) != awarded:
                compensation_violations.append(
                    f"索赔项目 {claim['claim_item_id']} 的分摊合计与认定金额不一致")
            segment_reports: list[dict[str, Any]] = []
            for segment_id, segment in segment_by_id.items():
                entries = connection.execute(
                    "SELECT * FROM settlement_entries WHERE claim_item_id=? AND segment_id=?",
                    (claim["claim_item_id"], segment_id),
                ).fetchall()
                if not entries and segment_id not in allocations:
                    continue
                capped = sum(entry["amount_minor"] for entry in entries
                             if entry["kind"] in CAPPED_ENTRY_KINDS)
                additional = sum(entry["amount_minor"] for entry in entries
                                 if entry["kind"] == ENTRY_ADDITIONAL)
                total = capped + additional
                allocation = allocations.get(segment_id, 0)
                if capped > allocation:
                    compensation_violations.append(
                        f"索赔项目 {claim['claim_item_id']} 区段 {segment['seq']} 的结算超过责任认定")
                if total < 0 or total > segment["liability_limit_minor"]:
                    compensation_violations.append(
                        f"索赔项目 {claim['claim_item_id']} 区段 {segment['seq']} "
                        "的累计结算超出责任上限或为负")
                for entry in entries:
                    if entry["payer_org_id"] != segment["carrier_org_id"] \
                            or entry["payee_org_id"] != customer_org:
                        compensation_violations.append(
                            f"结算分录 {entry['entry_id']} 的收付双方与运单当事人不一致")
                segment_reports.append({
                    "segment_id": segment_id, "seq": segment["seq"],
                    "carrier_org_id": segment["carrier_org_id"],
                    "allocated_minor": allocation, "settled_minor": capped,
                    "additional_minor": additional, "total_settled_minor": total,
                    "liability_limit_minor": segment["liability_limit_minor"],
                    "within_allocation": capped <= allocation,
                    "within_limit": 0 <= total <= segment["liability_limit_minor"]})
            basis_versions = []
            for receipt_id in basis_receipt_ids:
                receipt = connection.execute(
                    "SELECT receipt_id, group_id, from_seq, to_seq, version FROM handover_receipts "
                    "WHERE receipt_id=?",
                    (receipt_id,),
                ).fetchone()
                if receipt:
                    basis_versions.append({"receipt_id": receipt["receipt_id"],
                                           "group_id": receipt["group_id"],
                                           "from_seq": receipt["from_seq"],
                                           "to_seq": receipt["to_seq"],
                                           "version": receipt["version"]})
            settled_total = sum(report["total_settled_minor"] for report in segment_reports)
            total_claimed += claim["claimed_minor"]
            total_awarded += awarded
            total_settled += settled_total
            claim_reports.append({"claim_item_id": claim["claim_item_id"],
                                  "category": claim["category"], "status": claim["status"],
                                  "claimed_minor": claim["claimed_minor"], "basis": basis,
                                  "awarded_minor": awarded, "settled_minor": settled_total,
                                  "segments": segment_reports,
                                  "basis_receipt_versions": basis_versions})
        entry_rows = connection.execute(
            "SELECT payer_org_id, payee_org_id, amount_minor FROM settlement_entries "
            "WHERE claim_item_id IN (SELECT claim_item_id FROM claim_items WHERE waybill_id=?)",
            (waybill_id,),
        ).fetchall()
        carrier_paid = sum(row["amount_minor"] for row in entry_rows
                           if row["payer_org_id"] in carrier_orgs)
        customer_received = sum(row["amount_minor"] for row in entry_rows
                                if row["payee_org_id"] == customer_org)
        return {"waybill_id": waybill_id,
                "quantity": {"groups": group_reports, "violations": quantity_violations,
                             "conserved": not quantity_violations},
                "compensation": {
                    "claim_items": claim_reports,
                    "totals": {"claimed_minor": total_claimed, "awarded_minor": total_awarded,
                               "settled_minor": total_settled,
                               "carrier_paid_minor": carrier_paid,
                               "customer_received_minor": customer_received,
                               "payment_balanced": carrier_paid == customer_received},
                    "violations": compensation_violations,
                    "conserved": not compensation_violations}}

    def decision_basis(self, *, actor_id: str, decision_id: str) -> dict[str, Any]:
        """还原责任决定采用的交接回执版本及其完整内容。"""

        connection = self.database.connection
        decision = self._decision_row(connection, decision_id)
        claim = self._claim_row(connection, decision["claim_item_id"])
        waybill = self._waybill_row(connection, claim["waybill_id"])
        actor = self._actor(connection, actor_id)
        hub_org_id = self._hub_org_id(connection, waybill)
        is_hub_manager = actor.organization_id == hub_org_id and actor.role in ("admin", "reviewer")
        if not (actor.role == "auditor" or is_hub_manager):
            raise PermissionDenied("只有监管人员或枢纽管理方可以还原责任依据")
        receipts = []
        for receipt_id in json.loads(decision["basis_receipt_ids_json"]):
            row = connection.execute(
                "SELECT * FROM handover_receipts WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
            if row:
                receipts.append(self._handover_dict(row))
        return {"decision": self._decision_dict(connection, decision), "basis_receipts": receipts}
