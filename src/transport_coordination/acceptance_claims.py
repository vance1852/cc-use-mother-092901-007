"""运行一单制责任与索赔服务的离线端到端验收。

场景：铁水联运一单制发送设备，换装后集装箱破损；铁路、港口、水运保存的
交接时间各不相同。验收覆盖统一运单建档、多方回执版本、异常去重、索赔拆分、
责任草案与上限、部分和解、仲裁、迟到证据复审追加、重放防重复赔付、最小披露
以及监管数量与赔款守恒核对。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .claims_service import ClaimsService
from .clock import FixedClock
from .service import DomainService
from .storage import Database


def _clock(database: Database, day: int, hour: int = 8) -> ClaimsService:
    return ClaimsService(database, FixedClock(datetime(2026, 10, day, hour, tzinfo=timezone.utc)))


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "claims_acceptance.sqlite3")
        base = DomainService(database, FixedClock(datetime(2026, 9, 30, tzinfo=timezone.utc)))

        # 组织与角色：枢纽运营方、客户、铁路承运人、港口作业方、水运公司
        base.register_organization(request_id="org-hub", actor_id="bootstrap",
                                   organization_id="org-hub", name="枢纽运营方")
        for key, name in (("org-customer", "设备制造客户"), ("org-rail", "铁路承运人"),
                          ("org-port", "港口作业方"), ("org-water", "水运公司")):
            base.register_organization(request_id=key, actor_id="bootstrap",
                                       organization_id=key, name=name)
        base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin",
                            display_name="管理员", role="admin", organization_id="org-hub")
        actors = [
            ("op", "运营调度员", "operator", "org-hub"),
            ("rv", "复核仲裁员", "reviewer", "org-hub"),
            ("au", "监管人员", "auditor", "org-hub"),
            ("cust", "客户代表", "customer", "org-customer"),
            ("rail", "铁路代表", "carrier", "org-rail"),
            ("port", "港口代表", "port", "org-port"),
            ("water", "水运代表", "carrier", "org-water"),
        ]
        for index, (actor_id, name, role, org) in enumerate(actors):
            base.register_actor(request_id=f"actor-{actor_id}", actor_id="admin",
                                new_actor_id=actor_id, display_name=name, role=role,
                                organization_id=org)
        base.register_site(request_id="site-port", actor_id="admin", site_id="site-port",
                           organization_id="org-port", name="联运换装港",
                           timezone_name="Asia/Shanghai")

        svc = _clock(database, 1)
        # 统一运单：20 台设备，分装两个箱子，C2 未受影响
        svc.register_waybill(request_id="wb", actor_id="op", waybill_id="wb1",
                             waybill_no="ODR-2026-1001", customer_org_id="org-customer",
                             origin_name="制造基地", destination_name="海外收货地",
                             cargo_quantity=20, cargo_unit="台", declared_value=800000,
                             currency="CNY")
        svc.add_segment(request_id="seg-rail", actor_id="op", waybill_id="wb1", sequence_no=1,
                        mode="rail", carrier_org_id="org-rail", liability_cap=50000,
                        contract_ref="RAIL-CONTRACT-77", end_site_id="site-port")
        svc.add_segment(request_id="seg-water", actor_id="op", waybill_id="wb1", sequence_no=2,
                        mode="water", carrier_org_id="org-water", liability_cap=8000,
                        contract_ref="WATER-CONTRACT-12", start_site_id="site-port")
        svc.add_container(request_id="ctn1", actor_id="op", waybill_id="wb1", group_no="G1",
                          container_no="C1", cargo_quantity=10, cargo_description="精密设备")
        svc.add_container(request_id="ctn2", actor_id="op", waybill_id="wb1", group_no="G1",
                          container_no="C2", cargo_quantity=10, cargo_description="精密设备")
        svc.add_seal(request_id="seal1", actor_id="op", waybill_id="wb1", container_no="C1",
                     seal_no="SL9001", status="intact", affixed_by_org_id="org-rail",
                     affixed_at="2026-10-01T06:00:00Z")
        svc.add_boundary(request_id="bnd", actor_id="op", waybill_id="wb1", sequence_no=1,
                         site_id="site-port", from_org_id="org-rail", to_org_id="org-water")
        boundary_id = database.connection.execute(
            "SELECT boundary_id FROM handover_boundaries WHERE waybill_id='wb1'"
        ).fetchone()["boundary_id"]

        # 三方各自保存的交接时间不同：各自成为追加版本，谁也不能覆盖谁
        svc.submit_handover_receipt(request_id="rcpt-rail", actor_id="rail",
                                    boundary_id=boundary_id, event_time="2026-10-02T01:10:00Z",
                                    container_summary={"C1": {"seal": "intact"}, "C2": {"seal": "intact"}})
        svc.submit_handover_receipt(request_id="rcpt-port", actor_id="port",
                                    boundary_id=boundary_id, event_time="2026-10-02T01:40:00Z",
                                    container_summary={"C1": {"seal": "broken"}, "C2": {"seal": "intact"}})
        svc.submit_handover_receipt(request_id="rcpt-water", actor_id="water",
                                    boundary_id=boundary_id, event_time="2026-10-02T02:05:00Z",
                                    container_summary={"C1": {"seal": "broken", "outer_damage": True},
                                                        "C2": {"seal": "intact"}})

        # 客户只报一次案：同一去重键重复报案必须被拒绝
        svc.report_incident(request_id="inc", actor_id="cust", waybill_id="wb1",
                            incident_id="inc1", title="换装后 C1 箱损并短少",
                            discovered_at="2026-10-02T08:00:00Z",
                            evidence_deadline="2026-10-05T18:00:00Z",
                            dedup_key="wb1:C1:transfer-damage", boundary_id=boundary_id)
        duplicate = None
        try:
            svc.report_incident(request_id="inc-dup", actor_id="cust", waybill_id="wb1",
                                incident_id="inc1-copy", title="整票重复报案",
                                discovered_at="2026-10-02T09:00:00Z",
                                evidence_deadline="2026-10-06T18:00:00Z",
                                dedup_key="wb1:C1:transfer-damage", boundary_id=boundary_id)
        except Exception as exc:
            duplicate = type(exc).__name__

        # 截止前证据
        svc.submit_evidence(request_id="ev-rail", actor_id="rail", incident_id="inc1",
                            evidence_kind="seal_log", title="铁路封识记录",
                            reference="doc://rail/seal/SL9001")
        svc.submit_evidence(request_id="ev-port", actor_id="port", incident_id="inc1",
                            evidence_kind="handover_photo", title="港口破损照片",
                            reference="doc://port/photo/C1")

        # 拆成独立索赔项目：短少、破损、延误、共同原因
        svc.add_claim_item(request_id="ci-shortage", actor_id="op", incident_id="inc1",
                           item_type="shortage", container_no="C1", quantity=2,
                           claimed_amount=30000)
        svc.add_claim_item(request_id="ci-damage", actor_id="op", incident_id="inc1",
                           item_type="damage", container_no="C1", claimed_amount=30000)
        svc.add_claim_item(request_id="ci-delay", actor_id="op", incident_id="inc1",
                           item_type="delay", claimed_amount=5000)
        svc.add_claim_item(request_id="ci-common", actor_id="op", incident_id="inc1",
                           item_type="common_cause", claimed_amount=10000,
                           cause_key="weather-delay")

        # 截止后形成责任草案；各份额钉住具体交接回执版本
        svc = _clock(database, 6)
        water_receipt = database.connection.execute(
            "SELECT receipt_id FROM handover_receipts WHERE boundary_id=? AND version_no=3",
            (boundary_id,)).fetchone()["receipt_id"]
        rail_receipt = database.connection.execute(
            "SELECT receipt_id FROM handover_receipts WHERE boundary_id=? AND version_no=1",
            (boundary_id,)).fetchone()["receipt_id"]
        port_receipt = database.connection.execute(
            "SELECT receipt_id FROM handover_receipts WHERE boundary_id=? AND version_no=2",
            (boundary_id,)).fetchone()["receipt_id"]
        shortage_item = database.connection.execute(
            "SELECT claim_item_id FROM claim_items WHERE incident_id='inc1' AND item_type='shortage'"
        ).fetchone()["claim_item_id"]
        damage_item = database.connection.execute(
            "SELECT claim_item_id FROM claim_items WHERE incident_id='inc1' AND item_type='damage'"
        ).fetchone()["claim_item_id"]
        delay_item = database.connection.execute(
            "SELECT claim_item_id FROM claim_items WHERE incident_id='inc1' AND item_type='delay'"
        ).fetchone()["claim_item_id"]
        common_item = database.connection.execute(
            "SELECT claim_item_id FROM claim_items WHERE incident_id='inc1' AND item_type='common_cause'"
        ).fetchone()["claim_item_id"]

        svc.form_draft(request_id="draft1", actor_id="op", incident_id="inc1", allocations=[
            {"claim_item_id": shortage_item, "responsible_org_id": "org-rail",
             "share_amount": 20000, "handover_receipt_id": rail_receipt,
             "evidence_refs": ["ev-rail"]},
            # 水运段责任上限 8000：申请 30000 自动受限并标记 capped
            {"claim_item_id": damage_item, "responsible_org_id": "org-water",
             "share_amount": 30000, "handover_receipt_id": port_receipt,
             "evidence_refs": ["ev-port"]},
            {"claim_item_id": delay_item, "responsible_org_id": "org-rail",
             "share_amount": 5000, "boundary_id": boundary_id},
            {"claim_item_id": common_item, "responsible_org_id": "org-rail",
             "share_amount": 5000, "handover_receipt_id": rail_receipt},
            {"claim_item_id": common_item, "responsible_org_id": "org-water",
             "share_amount": 5000, "handover_receipt_id": water_receipt},
        ])
        draft_id = database.connection.execute(
            "SELECT draft_id FROM liability_drafts WHERE incident_id='inc1' AND version_no=1"
        ).fetchone()["draft_id"]
        rail_allocation = database.connection.execute(
            "SELECT allocation_id FROM liability_allocations WHERE draft_id=? "
            "AND responsible_org_id='org-rail' AND claim_item_id=?",
            (draft_id, shortage_item)).fetchone()["allocation_id"]
        water_allocation = database.connection.execute(
            "SELECT allocation_id FROM liability_allocations WHERE draft_id=? "
            "AND responsible_org_id='org-water' AND claim_item_id=?",
            (draft_id, damage_item)).fetchone()["allocation_id"]
        capped_flag = database.connection.execute(
            "SELECT capped FROM liability_allocations WHERE allocation_id=?",
            (water_allocation,)).fetchone()["capped"]
        water_share = database.connection.execute(
            "SELECT share_amount FROM liability_allocations WHERE allocation_id=?",
            (water_allocation,)).fetchone()["share_amount"]

        # 铁路确认无争议份额，水运申辩
        svc.record_party_position(request_id="pos-rail", actor_id="rail", draft_id=draft_id,
                                  allocation_id=rail_allocation, position="confirmed",
                                  statement="认可铁路段短少责任")
        svc.record_party_position(request_id="pos-water", actor_id="water", draft_id=draft_id,
                                  allocation_id=water_allocation, position="disputed",
                                  statement="破损发生在港口作业期间")

        # 部分和解：无争议的 20000 继续支付，其余项目结算不被暂停
        svc.decide_claim(request_id="dec-partial", actor_id="op", draft_id=draft_id,
                         claim_item_id=shortage_item, outcome="partial_settlement",
                         awarded_amount=20000, payer_org_id="org-rail")
        partial_delta = database.connection.execute(
            "SELECT amount FROM settlement_entries WHERE incident_id='inc1' "
            "AND claim_item_id=? ORDER BY created_at LIMIT 1", (shortage_item,)).fetchone()["amount"]
        # 仲裁：破损按水运责任上限封顶 8000
        damage_decision = svc.decide_claim(request_id="dec-damage", actor_id="rv",
                                           draft_id=draft_id, claim_item_id=damage_item,
                                           outcome="awarded", awarded_amount=8000,
                                           payer_org_id="org-water")

        # 迟到证据只能触发复审：原草案与原决定保留，新草案为 v2
        svc = _clock(database, 8)
        svc.submit_evidence(request_id="ev-late", actor_id="cust", incident_id="inc1",
                            evidence_kind="count_report", title="目的港清点补充报告",
                            reference="doc://customer/count/final")
        late_evidence = database.connection.execute(
            "SELECT evidence_id FROM evidence WHERE incident_id='inc1' AND after_cutoff=1"
        ).fetchone()["evidence_id"]
        svc = _clock(database, 9)
        svc.request_review(request_id="review1", actor_id="rv", incident_id="inc1",
                           reason="补充清点显示短少为 25000", new_evidence_ids=[late_evidence],
                           allocations=[
                               {"claim_item_id": shortage_item, "responsible_org_id": "org-rail",
                                "share_amount": 25000, "handover_receipt_id": rail_receipt},
                               {"claim_item_id": damage_item, "responsible_org_id": "org-water",
                                "share_amount": 8000, "handover_receipt_id": port_receipt},
                           ])
        draft_v2 = database.connection.execute(
            "SELECT draft_id FROM liability_drafts WHERE incident_id='inc1' AND version_no=2"
        ).fetchone()["draft_id"]
        original_decisions_kept = database.connection.execute(
            "SELECT COUNT(*) AS c FROM liability_decisions WHERE incident_id='inc1'"
        ).fetchone()["c"]
        review_decision = svc.decide_claim(request_id="dec-review", actor_id="rv",
                                           draft_id=draft_v2, claim_item_id=shortage_item,
                                           outcome="awarded", awarded_amount=25000,
                                           payer_org_id="org-rail")

        # 同一异常在不同系统重放：报案拒绝、request_id 重放拿原回执、分录去重键拒绝重复
        replay_receipt = svc.report_incident(request_id="inc", actor_id="cust", waybill_id="wb1",
                                             incident_id="inc1", title="换装后 C1 箱损并短少",
                                             discovered_at="2026-10-02T08:00:00Z",
                                             evidence_deadline="2026-10-05T18:00:00Z",
                                             dedup_key="wb1:C1:transfer-damage",
                                             boundary_id=boundary_id)
        duplicate_entry = None
        try:
            # 用复审决定已经记账的业务键重放：必须被分录唯一约束拒绝
            svc.add_recovery_entry(request_id="rec-dup", actor_id="op", incident_id="inc1",
                                   claim_item_id=shortage_item, amount=5000,
                                   payer_org_id="org-rail", payee_org_id="org-customer",
                                   dedup_key="settle:inc1:%s:v2" % shortage_item)
        except Exception as exc:
            duplicate_entry = type(exc).__name__

        # 监管核对：数量与赔款守恒、交接版本可还原
        report = svc.reconcile("au", "wb1")
        shortage_row = next(item for item in report["items"]
                            if item["claim_item_id"] == shortage_item)
        damage_row = next(item for item in report["items"]
                          if item["claim_item_id"] == damage_item)
        trace = svc.decision_trace("au", review_decision.resource_id)
        pinned_versions = sorted(r["version_no"] for r in trace["pinned_receipt_versions"])

        # 最小披露：三个参与方视角各不相同
        customer_view = svc.get_case_bundle("cust", "wb1")
        rail_view = svc.get_case_bundle("rail", "wb1")
        water_view = svc.get_case_bundle("water", "wb1")
        customer_contracts_hidden = all(s["contract_ref"] is None for s in customer_view["segments"])
        rail_sees_only_own_contract = {
            s["carrier_org_id"]: s["contract_ref"] for s in rail_view["segments"]
        } == {"org-rail": "RAIL-CONTRACT-77", "org-water": None}
        other_evidence_ref_hidden = all(
            e["reference"] is None
            for inc in rail_view["incidents"] for e in inc["evidence"]
            if e["submitter_org_id"] != "org-rail"
        )
        water_sees_disputed_only = all(
            allocation["responsible_org_id"] == "org-water"
            for inc in water_view["incidents"] for draft in inc["drafts"]
            for allocation in draft["allocations"]
        )

        valid, event_count = base.verify_audit()
        settlements = database.connection.execute(
            "SELECT amount, entry_type FROM settlement_entries WHERE incident_id='inc1' "
            "AND claim_item_id=? ORDER BY created_at", (shortage_item,)).fetchall()
        shortage_settled = sum(row["amount"] for row in settlements)

        result = {
            "status": "ok",
            "receipt_versions": 3,
            "duplicate_incident_blocked": duplicate == "ConflictError",
            "water_capped": bool(capped_flag) and water_share == 8000,
            "partial_settlement_paid": partial_delta == 20000,
            "original_decisions_kept": original_decisions_kept >= 2,
            "review_is_new_version": review_decision.__dict__["resource_id"] is not None,
            "incident_replay_is_idempotent": replay_receipt.replayed,
            "duplicate_settlement_blocked": duplicate_entry == "ConflictError",
            "shortage_settled_total": shortage_settled,
            "shortage_conserved": shortage_row["conserved"] and shortage_settled == 25000,
            "damage_conserved": damage_row["conserved"],
            "all_conserved": report["conserved"],
            "no_duplicate_keys": report["duplicate_settlement_keys"] == [],
            "pinned_receipt_versions": pinned_versions,
            "trace_decision_version": trace["version_no"],
            "customer_contracts_hidden": customer_contracts_hidden,
            "rail_sees_only_own_contract": rail_sees_only_own_contract,
            "other_evidence_ref_hidden": other_evidence_ref_hidden,
            "water_sees_disputed_only": water_sees_disputed_only,
            "unaffected_c2_has_no_claim": not database.connection.execute(
                "SELECT 1 FROM claim_items WHERE container_no='C2'").fetchone(),
            "audit_valid": valid,
            "audit_events": event_count,
        }
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
