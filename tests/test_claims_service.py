import unittest
from datetime import datetime, timezone

from transport_coordination.claims_service import ClaimsService
from transport_coordination.clock import FixedClock
from transport_coordination.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


class ClaimsCase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.base = DomainService(self.database, clock)
        self.svc = ClaimsService(self.database, clock)
        self.base.register_organization(request_id="oh", actor_id="bootstrap",
                                        organization_id="hub", name="枢纽")
        for key, name in (("cust", "客户"), ("rail", "铁路"), ("port", "港口"), ("water", "水运")):
            self.base.register_organization(request_id=f"o-{key}", actor_id="bootstrap",
                                            organization_id=key, name=name)
        self.base.register_actor(request_id="a-admin", actor_id="bootstrap", new_actor_id="admin",
                                 display_name="管理员", role="admin", organization_id="hub")
        for actor_id, name, role, org in (
            ("op", "运营", "operator", "hub"),
            ("rv", "复核", "reviewer", "hub"),
            ("au", "监管", "auditor", "hub"),
            ("cust", "客户", "customer", "cust"),
            ("rail", "铁路", "carrier", "rail"),
            ("port", "港口", "port", "port"),
            ("water", "水运", "carrier", "water"),
        ):
            self.base.register_actor(request_id=f"a-{actor_id}", actor_id="admin",
                                     new_actor_id=actor_id, display_name=name, role=role,
                                     organization_id=org)
        self.base.register_site(request_id="s-port", actor_id="admin", site_id="sport",
                                organization_id="port", name="换装港",
                                timezone_name="Asia/Shanghai")
        self.svc.register_waybill(request_id="wb", actor_id="op", waybill_id="w1",
                                  waybill_no="WB-1", customer_org_id="cust",
                                  origin_name="起", destination_name="终",
                                  cargo_quantity=10, cargo_unit="箱", declared_value=100000,
                                  currency="CNY")
        self.svc.add_segment(request_id="sg1", actor_id="op", waybill_id="w1", sequence_no=1,
                             mode="rail", carrier_org_id="rail", liability_cap=50000,
                             contract_ref="RC", end_site_id="sport")
        self.svc.add_segment(request_id="sg2", actor_id="op", waybill_id="w1", sequence_no=2,
                             mode="water", carrier_org_id="water", liability_cap=8000,
                             contract_ref="WC", start_site_id="sport")
        self.svc.add_container(request_id="c1", actor_id="op", waybill_id="w1", group_no="G",
                               container_no="K1", cargo_quantity=5)
        self.svc.add_boundary(request_id="b1", actor_id="op", waybill_id="w1", sequence_no=1,
                              site_id="sport", from_org_id="rail", to_org_id="water")
        self.boundary_id = self.database.connection.execute(
            "SELECT boundary_id FROM handover_boundaries WHERE waybill_id='w1'"
        ).fetchone()["boundary_id"]

    def tearDown(self):
        self.database.close()

    def _incident(self, deadline="2026-10-05T18:00:00Z"):
        self.svc.report_incident(request_id="inc", actor_id="cust", waybill_id="w1",
                                 incident_id="i1", title="箱损",
                                 discovered_at="2026-10-02T08:00:00Z",
                                 evidence_deadline=deadline, dedup_key="w1:K1:damage",
                                 boundary_id=self.boundary_id)

    def _receipts(self):
        self.svc.submit_handover_receipt(request_id="r1", actor_id="rail",
                                         boundary_id=self.boundary_id,
                                         event_time="2026-10-02T01:00:00Z",
                                         container_summary={"K1": {"seal": "intact"}})
        self.svc.submit_handover_receipt(request_id="r2", actor_id="water",
                                         boundary_id=self.boundary_id,
                                         event_time="2026-10-02T02:00:00Z",
                                         container_summary={"K1": {"seal": "broken"}})

    def _claim(self):
        self.svc.add_claim_item(request_id="ci", actor_id="op", incident_id="i1",
                                item_type="damage", container_no="K1", claimed_amount=30000)
        return self.database.connection.execute(
            "SELECT claim_item_id FROM claim_items WHERE incident_id='i1'"
        ).fetchone()["claim_item_id"]

    def _draft(self, share=30000, org="water"):
        self.svc = ClaimsService(self.database, FixedClock(datetime(2026, 10, 6, tzinfo=timezone.utc)))
        receipt = self.database.connection.execute(
            "SELECT receipt_id FROM handover_receipts WHERE boundary_id=? ORDER BY version_no DESC LIMIT 1",
            (self.boundary_id,)).fetchone()["receipt_id"]
        item = self._claim()
        self.svc.form_draft(request_id="d1", actor_id="op", incident_id="i1", allocations=[
            {"claim_item_id": item, "responsible_org_id": org, "share_amount": share,
             "handover_receipt_id": receipt},
        ])
        draft_id = self.database.connection.execute(
            "SELECT draft_id FROM liability_drafts WHERE incident_id='i1'"
        ).fetchone()["draft_id"]
        allocation_id = self.database.connection.execute(
            "SELECT allocation_id FROM liability_allocations WHERE draft_id=?", (draft_id,)
        ).fetchone()["allocation_id"]
        return item, draft_id, allocation_id


class HandoverTest(ClaimsCase):
    def test_parties_append_distinct_versions(self):
        self._receipts()
        versions = self.database.connection.execute(
            "SELECT version_no, submitter_org_id FROM handover_receipts ORDER BY version_no"
        ).fetchall()
        self.assertEqual([(1, "rail"), (2, "water")],
                         [(row["version_no"], row["submitter_org_id"]) for row in versions])

    def test_identical_receipt_rejected_and_request_replay_is_safe(self):
        self._receipts()
        with self.assertRaises(ConflictError):
            self.svc.submit_handover_receipt(request_id="r3", actor_id="water",
                                             boundary_id=self.boundary_id,
                                             event_time="2026-10-02T02:00:00Z",
                                             container_summary={"K1": {"seal": "broken"}})
        replay = self.svc.submit_handover_receipt(request_id="r2", actor_id="water",
                                                  boundary_id=self.boundary_id,
                                                  event_time="2026-10-02T02:00:00Z",
                                                  container_summary={"K1": {"seal": "broken"}})
        self.assertTrue(replay.replayed)
        self.assertEqual(2, self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM handover_receipts").fetchone()["c"])

    def test_outside_party_cannot_submit(self):
        with self.assertRaises(PermissionDenied):
            self.svc.submit_handover_receipt(request_id="rx", actor_id="cust",
                                             boundary_id=self.boundary_id,
                                             event_time="2026-10-02T02:00:00Z",
                                             container_summary={"K1": {}})


class IncidentTest(ClaimsCase):
    def test_duplicate_report_blocked(self):
        self._incident()
        with self.assertRaises(ConflictError):
            self.svc.report_incident(request_id="inc2", actor_id="cust", waybill_id="w1",
                                     incident_id="i2", title="重复",
                                     discovered_at="2026-10-02T09:00:00Z",
                                     evidence_deadline="2026-10-06T18:00:00Z",
                                     dedup_key="w1:K1:damage", boundary_id=self.boundary_id)

    def test_same_request_id_replays(self):
        self._incident()
        receipt = self.svc.report_incident(request_id="inc", actor_id="cust", waybill_id="w1",
                                           incident_id="i1", title="箱损",
                                           discovered_at="2026-10-02T08:00:00Z",
                                           evidence_deadline="2026-10-05T18:00:00Z",
                                           dedup_key="w1:K1:damage",
                                           boundary_id=self.boundary_id)
        self.assertTrue(receipt.replayed)

    def test_deadline_before_discovery_rejected(self):
        with self.assertRaises(ValidationError):
            self.svc.report_incident(request_id="bad", actor_id="cust", waybill_id="w1",
                                     incident_id="i9", title="时间错误",
                                     discovered_at="2026-10-05T08:00:00Z",
                                     evidence_deadline="2026-10-02T08:00:00Z",
                                     dedup_key="w1:K1:x", boundary_id=self.boundary_id)


class DraftAndDecisionTest(ClaimsCase):
    def test_draft_before_cutoff_rejected(self):
        self._incident()
        self._receipts()
        item = self._claim()
        receipt = self.database.connection.execute(
            "SELECT receipt_id FROM handover_receipts WHERE boundary_id=? LIMIT 1",
            (self.boundary_id,)).fetchone()["receipt_id"]
        with self.assertRaises(ConflictError):
            self.svc.form_draft(request_id="early", actor_id="op", incident_id="i1",
                                allocations=[{"claim_item_id": item, "responsible_org_id": "water",
                                              "share_amount": 100, "handover_receipt_id": receipt}])

    def test_liability_cap_is_applied(self):
        self._incident()
        self._receipts()
        _, _, allocation_id = self._draft(share=30000, org="water")
        row = self.database.connection.execute(
            "SELECT share_amount, capped FROM liability_allocations WHERE allocation_id=?",
            (allocation_id,)).fetchone()
        self.assertEqual(8000, row["share_amount"])
        self.assertEqual(1, row["capped"])

    def test_allocation_cannot_exceed_claim(self):
        self._incident()
        self._receipts()
        self.svc = ClaimsService(self.database, FixedClock(datetime(2026, 10, 6, tzinfo=timezone.utc)))
        item = self._claim()
        receipt = self.database.connection.execute(
            "SELECT receipt_id FROM handover_receipts WHERE boundary_id=? LIMIT 1",
            (self.boundary_id,)).fetchone()["receipt_id"]
        with self.assertRaises(ConflictError):
            self.svc.form_draft(request_id="over", actor_id="op", incident_id="i1", allocations=[
                {"claim_item_id": item, "responsible_org_id": "rail", "share_amount": 40000,
                 "handover_receipt_id": receipt},
                {"claim_item_id": item, "responsible_org_id": "water", "share_amount": 40000,
                 "handover_receipt_id": receipt},
            ])

    def test_second_initial_draft_rejected_late_evidence_needs_review(self):
        self._incident()
        self._receipts()
        self._draft()
        with self.assertRaises(ConflictError):
            self.svc.form_draft(request_id="d2", actor_id="op", incident_id="i1",
                                allocations=[])
        with self.assertRaises(ConflictError):
            self.svc.request_review(request_id="rv-empty", actor_id="rv", incident_id="i1",
                                    reason="无新证据", allocations=[])

    def test_partial_settlement_limited_to_confirmed_share(self):
        self._incident()
        self._receipts()
        item, draft_id, allocation_id = self._draft(share=8000, org="water")
        # 水运申辩而非确认：无争议金额为 0
        self.svc.record_party_position(request_id="p1", actor_id="water", draft_id=draft_id,
                                       allocation_id=allocation_id, position="disputed",
                                       statement="不认可")
        with self.assertRaises(ConflictError):
            self.svc.decide_claim(request_id="pay", actor_id="op", draft_id=draft_id,
                                  claim_item_id=item, outcome="partial_settlement",
                                  awarded_amount=8000, payer_org_id="water")

    def test_carrier_can_only_position_on_own_allocation(self):
        self._incident()
        self._receipts()
        _, draft_id, allocation_id = self._draft(share=8000, org="water")
        with self.assertRaises(PermissionDenied):
            self.svc.record_party_position(request_id="p-x", actor_id="rail", draft_id=draft_id,
                                           allocation_id=allocation_id, position="confirmed")

    def test_decisions_are_append_only_and_review_adds_version(self):
        self._incident()
        self._receipts()
        item, draft_id, _ = self._draft(share=8000, org="water")
        first = self.svc.decide_claim(request_id="dec1", actor_id="rv", draft_id=draft_id,
                                      claim_item_id=item, outcome="awarded",
                                      awarded_amount=8000, payer_org_id="water")
        # 迟到证据触发复审
        self.svc = ClaimsService(self.database, FixedClock(datetime(2026, 10, 8, tzinfo=timezone.utc)))
        self.svc.submit_evidence(request_id="late", actor_id="cust", incident_id="i1",
                                 evidence_kind="report", title="补充报告",
                                 reference="doc://late")
        late_id = self.database.connection.execute(
            "SELECT evidence_id FROM evidence WHERE after_cutoff=1").fetchone()["evidence_id"]
        self.svc = ClaimsService(self.database, FixedClock(datetime(2026, 10, 9, tzinfo=timezone.utc)))
        receipt = self.database.connection.execute(
            "SELECT receipt_id FROM handover_receipts WHERE boundary_id=? ORDER BY version_no DESC LIMIT 1",
            (self.boundary_id,)).fetchone()["receipt_id"]
        self.svc.request_review(request_id="rev1", actor_id="rv", incident_id="i1",
                                reason="新证据", new_evidence_ids=[late_id],
                                allocations=[{"claim_item_id": item, "responsible_org_id": "water",
                                              "share_amount": 6000, "handover_receipt_id": receipt}])
        v2 = self.database.connection.execute(
            "SELECT draft_id FROM liability_drafts WHERE incident_id='i1' AND version_no=2"
        ).fetchone()["draft_id"]
        second = self.svc.decide_claim(request_id="dec2", actor_id="rv", draft_id=v2,
                                       claim_item_id=item, outcome="awarded",
                                       awarded_amount=6000, payer_org_id="water")
        self.assertFalse(first.replayed)
        self.assertFalse(second.replayed)
        versions = [row["version_no"] for row in self.database.connection.execute(
            "SELECT version_no FROM liability_decisions WHERE incident_id='i1' ORDER BY version_no")]
        self.assertEqual([1, 2], versions)
        # 原草案被标记取代但记录仍在
        statuses = {row["version_no"]: row["status"] for row in self.database.connection.execute(
            "SELECT version_no, status FROM liability_drafts WHERE incident_id='i1'")}
        self.assertEqual("superseded", statuses[1])
        self.assertEqual("draft", statuses[2])
        # 6000 < 8000：第二笔为追回分录，分录合计守恒于最新裁决
        entries = self.database.connection.execute(
            "SELECT amount, entry_type FROM settlement_entries WHERE claim_item_id=?", (item,)
        ).fetchall()
        self.assertEqual([(8000, "settlement"), (2000, "clawback")],
                         [(row["amount"], row["entry_type"]) for row in entries])

    def test_replayed_decision_does_not_pay_twice(self):
        self._incident()
        self._receipts()
        item, draft_id, allocation_id = self._draft(share=8000, org="water")
        self.svc.record_party_position(request_id="p1", actor_id="water", draft_id=draft_id,
                                       allocation_id=allocation_id, position="confirmed")
        first = self.svc.decide_claim(request_id="dec", actor_id="op", draft_id=draft_id,
                                      claim_item_id=item, outcome="partial_settlement",
                                      awarded_amount=8000, payer_org_id="water")
        replay = self.svc.decide_claim(request_id="dec", actor_id="op", draft_id=draft_id,
                                       claim_item_id=item, outcome="partial_settlement",
                                       awarded_amount=8000, payer_org_id="water")
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        count = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM settlement_entries WHERE claim_item_id=?", (item,)
        ).fetchone()["c"]
        self.assertEqual(1, count)

    def test_awarded_above_cap_share_rejected(self):
        self._incident()
        self._receipts()
        item, draft_id, _ = self._draft(share=8000, org="water")
        with self.assertRaises(ConflictError):
            self.svc.decide_claim(request_id="too-much", actor_id="rv", draft_id=draft_id,
                                  claim_item_id=item, outcome="awarded",
                                  awarded_amount=9000, payer_org_id="water")


class DisclosureAndReconcileTest(ClaimsCase):
    def _decided_case(self):
        self._incident()
        self._receipts()
        self.svc.submit_evidence(request_id="e1", actor_id="rail", incident_id="i1",
                                 evidence_kind="log", title="铁路日志",
                                 reference="doc://rail/log")
        item, draft_id, allocation_id = self._draft(share=8000, org="water")
        self.svc.record_party_position(request_id="p1", actor_id="water", draft_id=draft_id,
                                       allocation_id=allocation_id, position="confirmed",
                                       statement="内部辩词")
        self.svc.decide_claim(request_id="dec", actor_id="rv", draft_id=draft_id,
                              claim_item_id=item, outcome="awarded", awarded_amount=8000,
                              payer_org_id="water")
        return item

    def test_minimal_disclosure_differs_by_viewer(self):
        self._decided_case()
        customer = self.svc.get_case_bundle("cust", "w1")
        water = self.svc.get_case_bundle("water", "w1")
        rail = self.svc.get_case_bundle("rail", "w1")
        # 客户看不到任何分段合同条款与责任上限
        self.assertTrue(all(s["contract_ref"] is None for s in customer["segments"]))
        # 客户看不到承运人内部辩词，但能看到结论
        positions = [p for inc in customer["incidents"] for d in inc["drafts"]
                     for a in d["allocations"] for p in a["positions"]]
        self.assertTrue(positions)
        self.assertTrue(all(p["statement"] == "" for p in positions))
        # 铁路未被划责：看不到该项目的分配
        rail_allocations = [a for inc in rail["incidents"] for d in inc["drafts"]
                            for a in d["allocations"]]
        self.assertEqual([], rail_allocations)
        # 外部引用只回传给提交方：铁路日志对水运不可见、水运没有自己的证据可取
        self.assertTrue(all(e["reference"] is None for inc in water["incidents"]
                            for e in inc["evidence"] if e["submitter_org_id"] != "water"))
        # 提交方自己仍能取回引用
        rail_refs = [e["reference"] for inc in rail["incidents"] for e in inc["evidence"]
                     if e["submitter_org_id"] == "rail"]
        self.assertEqual(["doc://rail/log"], rail_refs)
        # 水运只能看到本方的合同条款
        self.assertEqual({(s["carrier_org_id"], s["contract_ref"]) for s in water["segments"]},
                         {("rail", None), ("water", "WC")})

    def test_non_participant_denied(self):
        # 另建一个与本运单无关的组织并赋予承运角色
        self.base.register_organization(request_id="o-stranger", actor_id="admin",
                                        organization_id="stranger", name="无关方")
        self.base.register_actor(request_id="a-stranger", actor_id="admin",
                                 new_actor_id="stranger", display_name="无关",
                                 role="carrier", organization_id="stranger")
        with self.assertRaises(PermissionDenied):
            self.svc.get_case_bundle("stranger", "w1")

    def test_reconcile_conservation_and_trace(self):
        item = self._decided_case()
        report = self.svc.reconcile("au", "w1")
        self.assertTrue(report["conserved"])
        row = next(r for r in report["items"] if r["claim_item_id"] == item)
        self.assertEqual(8000, row["latest_awarded_amount"])
        self.assertEqual(8000, row["settlement_entries_sum"])
        self.assertTrue(row["pinned_receipts"])
        decision_id = self.database.connection.execute(
            "SELECT decision_id FROM liability_decisions WHERE incident_id='i1'"
        ).fetchone()["decision_id"]
        trace = self.svc.decision_trace("au", decision_id)
        self.assertEqual(2, trace["pinned_receipt_versions"][0]["version_no"])
        self.assertEqual("water", trace["pinned_receipt_versions"][0]["submitter_org_id"])

    def test_auditor_is_required_for_reconcile(self):
        with self.assertRaises(PermissionDenied):
            self.svc.reconcile("cust", "w1")

    def test_missing_trace_returns_not_found(self):
        with self.assertRaises(NotFoundError):
            self.svc.decision_trace("au", "nope")


if __name__ == "__main__":
    unittest.main()
