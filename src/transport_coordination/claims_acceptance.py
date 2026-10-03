"""运行一单制责任与索赔服务的离线端到端验收。

场景：制造企业经铁水联运一单制发送设备，集装箱换装后发现损坏。验收覆盖
统一运单串联、交接回执多版本、异常跨系统去重、独立索赔项目、证据截止后的
责任草案、相关方确认与申辩、仲裁、部分和解先行支付、逾期证据触发的复审与
追加分录、防重复赔付，以及监管人员的守恒核对与交接版本还原。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .claims import ClaimsService
from .clock import MutableClock
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整的索赔协同链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "claims_acceptance.sqlite3")
        clock = MutableClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        service = ClaimsService(database, clock)
        service.register_organization(request_id="org-hub", actor_id="bootstrap",
                                      organization_id="org-hub", name="枢纽运营方")
        service.register_actor(request_id="actor-hub-admin", actor_id="bootstrap",
                               new_actor_id="hub-admin", display_name="枢纽管理员",
                               role="admin", organization_id="org-hub")
        for org_id, name in (("org-customer", "制造企业"), ("org-rail", "铁路承运人"),
                             ("org-port", "港口作业方"), ("org-water", "水运公司"),
                             ("org-regulator", "监管机构")):
            service.register_organization(request_id=f"register-{org_id}", actor_id="hub-admin",
                                          organization_id=org_id, name=name)
        for actor_id, name, role, org in (
            ("hub-ops", "枢纽操作员", "operator", "org-hub"),
            ("hub-rev", "枢纽复核员", "reviewer", "org-hub"),
            ("cust-ops", "客户专员", "operator", "org-customer"),
            ("rail-ops", "铁路专员", "operator", "org-rail"),
            ("port-ops", "港口专员", "operator", "org-port"),
            ("regulator", "监管人员", "auditor", "org-regulator"),
        ):
            service.register_actor(request_id=f"register-{actor_id}", actor_id="hub-admin",
                                   new_actor_id=actor_id, display_name=name, role=role,
                                   organization_id=org)
        service.register_site(request_id="site-hub", actor_id="hub-ops", site_id="site-hub",
                              organization_id="org-hub", name="铁水联运枢纽",
                              timezone_name="Asia/Shanghai")
        service.register_waybill(
            request_id="wb-001", actor_id="hub-ops", site_id="site-hub", waybill_id="WB-001",
            customer_org_id="org-customer",
            segments=[{"seq": 1, "mode": "rail", "carrier_org_id": "org-rail",
                       "liability_limit_minor": 300000},
                      {"seq": 2, "mode": "port", "carrier_org_id": "org-port",
                       "liability_limit_minor": 200000},
                      {"seq": 3, "mode": "water", "carrier_org_id": "org-water",
                       "liability_limit_minor": 300000}],
            groups=[{"group_no": "G1", "quantity": 10, "description": "数控设备",
                     "seals": ["SEAL-01", "SEAL-02"]}])
        view = service.get_waybill_view(actor_id="hub-admin", waybill_id="WB-001")
        segments = {item["seq"]: item for item in view["segments"]}
        group_id = view["groups"][0]["group_id"]

        # 交接回执：换装点出现两个版本，责任判断采用更正后的版本。
        service.record_handover(request_id="hov-1-2", actor_id="hub-ops", waybill_id="WB-001",
                                group_id=group_id, from_seq=1, to_seq=2,
                                occurred_at="2026-10-02T09:00:00Z", quantity_received=10,
                                seal_intact=True, condition="good")
        service.record_handover(request_id="hov-2-3-v1", actor_id="hub-ops", waybill_id="WB-001",
                                group_id=group_id, from_seq=2, to_seq=3,
                                occurred_at="2026-10-03T10:00:00Z", quantity_received=10,
                                seal_intact=False, condition="damaged")
        corrected = service.record_handover(request_id="hov-2-3-v2", actor_id="hub-ops",
                                            waybill_id="WB-001", group_id=group_id,
                                            from_seq=2, to_seq=3,
                                            occurred_at="2026-10-03T10:25:00Z",
                                            quantity_received=10, seal_intact=False,
                                            condition="damaged")
        service.record_handover(request_id="hov-3-0", actor_id="hub-ops", waybill_id="WB-001",
                                group_id=group_id, from_seq=3, to_seq=0,
                                occurred_at="2026-10-05T08:00:00Z", quantity_received=10,
                                seal_intact=False, condition="damaged")

        # 同一异常被港口与铁路系统重复上报，只登记一次。
        first = service.register_exception(request_id="exp-port", actor_id="hub-ops",
                                           waybill_id="WB-001", group_id=group_id,
                                           category="damage", exception_key="EXP-DMG-001",
                                           quantity=2, detected_seq=2, source_system="port-tos")
        replay = service.register_exception(request_id="exp-rail", actor_id="hub-ops",
                                            waybill_id="WB-001", group_id=group_id,
                                            category="damage", exception_key="EXP-DMG-001",
                                            quantity=2, detected_seq=2, source_system="rail-ims")
        exception_id = first.resource_id
        exception_deduplicated = replay.resource_id == exception_id

        # 独立索赔项目与截止前证据。
        claim = service.open_claim_item(request_id="claim-1", actor_id="hub-ops",
                                        exception_id=exception_id, category="damage", quantity=2,
                                        claimed_minor=200000,
                                        evidence_deadline="2026-10-08T00:00:00Z")
        claim_id = claim.resource_id
        service.submit_evidence(request_id="ev-1", actor_id="cust-ops", claim_item_id=claim_id,
                                kind="photo", data={"images": ["dmg-1.jpg"], "note": "开箱拍照"})

        # 证据截止后形成责任草案。
        clock.set(datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc))
        draft = service.create_liability_draft(
            request_id="draft-1", actor_id="hub-rev", claim_item_id=claim_id,
            awarded_minor=195000,
            allocations=[{"segment_id": segments[2]["segment_id"], "amount_minor": 115000},
                         {"segment_id": segments[1]["segment_id"], "amount_minor": 80000}],
            basis_receipt_ids=[corrected.resource_id])
        draft_id = draft.resource_id

        # 客户与铁路确认，港口申辩；无争议的铁路分摊先行支付。
        service.respond_to_draft(request_id="resp-cust", actor_id="cust-ops",
                                 draft_id=draft_id, stance="confirm", note="")
        service.respond_to_draft(request_id="resp-rail", actor_id="rail-ops",
                                 draft_id=draft_id, stance="confirm", note="")
        service.respond_to_draft(request_id="resp-port", actor_id="port-ops",
                                 draft_id=draft_id, stance="rebut", note="换装交接时间不一致")
        service.settle_allocation(request_id="settle-rail", actor_id="hub-ops",
                                  claim_item_id=claim_id,
                                  segment_id=segments[1]["segment_id"], amount_minor=80000)

        # 仲裁形成责任决定，港口按决定支付。
        arbitration = service.record_arbitration(
            request_id="arb-1", actor_id="hub-rev", draft_id=draft_id,
            allocations=[{"segment_id": segments[2]["segment_id"], "amount_minor": 115000},
                         {"segment_id": segments[1]["segment_id"], "amount_minor": 80000}])
        decision_id = arbitration.resource_id
        service.settle_allocation(request_id="settle-port", actor_id="hub-ops",
                                  claim_item_id=claim_id,
                                  segment_id=segments[2]["segment_id"], amount_minor=115000)

        # 逾期补交的证据触发复审，复审只产生追加分录。
        clock.set(datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc))
        late = service.submit_evidence(request_id="ev-2", actor_id="cust-ops",
                                       claim_item_id=claim_id, kind="report",
                                       data={"note": "补充的检验报告"})
        review = service.request_review(request_id="review-1", actor_id="cust-ops",
                                        claim_item_id=claim_id, evidence_id=late.resource_id)
        service.close_review(request_id="review-1-close", actor_id="hub-rev",
                             review_id=review.resource_id,
                             additional_entries=[{"segment_id": segments[2]["segment_id"],
                                                  "amount_minor": 5000}])

        # 重放的结算请求返回原回执，不会重复赔付。
        replayed_settle = service.settle_allocation(request_id="settle-port", actor_id="hub-ops",
                                                    claim_item_id=claim_id,
                                                    segment_id=segments[2]["segment_id"],
                                                    amount_minor=115000)

        # 监管核对守恒并还原责任判断采用的交接版本。
        report = service.conservation_report(actor_id="regulator", waybill_id="WB-001")
        basis = service.decision_basis(actor_id="regulator", decision_id=decision_id)
        final_view = service.get_claim_item_view(actor_id="hub-admin", claim_item_id=claim_id)
        valid, event_count = service.verify_audit()
        result = {
            "status": "ok",
            "waybill_id": "WB-001",
            "claim_item_id": claim_id,
            "decision_id": decision_id,
            "decided_via": final_view["decision"]["decided_via"],
            "awarded_minor": final_view["decision"]["awarded_minor"],
            "settled_total_minor": sum(item["amount_minor"]
                                       for item in final_view["settlements"]),
            "exception_deduplicated": exception_deduplicated,
            "settlement_replay_blocked": replayed_settle.replayed,
            "late_evidence_flagged": any(item["late"] for item in final_view["evidence"]),
            "quantity_conserved": report["quantity"]["conserved"],
            "compensation_conserved": report["compensation"]["conserved"],
            "basis_versions": [item["version"] for item in basis["basis_receipts"]],
            "audit_valid": valid,
            "audit_events": event_count,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = result["status"] == "ok" and result["audit_valid"] \
        and result["quantity_conserved"] and result["compensation_conserved"]
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
