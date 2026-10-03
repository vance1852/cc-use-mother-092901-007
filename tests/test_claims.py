import unittest
from datetime import datetime, timezone

from transport_coordination.claims import ClaimsService
from transport_coordination.clock import MutableClock
from transport_coordination.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from transport_coordination.storage import Database


class ClaimsTestBase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        self.service = ClaimsService(self.database, self.clock)
        self.service.register_organization(request_id="org-hub", actor_id="bootstrap",
                                           organization_id="org-hub", name="枢纽运营方")
        self.service.register_actor(request_id="actor-hub-admin", actor_id="bootstrap",
                                    new_actor_id="hub-admin", display_name="枢纽管理员",
                                    role="admin", organization_id="org-hub")
        for org_id, name in (("org-customer", "制造企业"), ("org-rail", "铁路承运人"),
                             ("org-port", "港口作业方"), ("org-water", "水运公司"),
                             ("org-regulator", "监管机构"), ("org-outsider", "无关企业")):
            self.service.register_organization(request_id=f"register-{org_id}", actor_id="hub-admin",
                                               organization_id=org_id, name=name)
        for actor_id, name, role, org in (
            ("hub-ops", "枢纽操作员", "operator", "org-hub"),
            ("hub-rev", "枢纽复核员", "reviewer", "org-hub"),
            ("cust-ops", "客户专员", "operator", "org-customer"),
            ("rail-ops", "铁路专员", "operator", "org-rail"),
            ("port-ops", "港口专员", "operator", "org-port"),
            ("water-ops", "水运专员", "operator", "org-water"),
            ("regulator", "监管人员", "auditor", "org-regulator"),
            ("outsider", "无关人员", "operator", "org-outsider"),
        ):
            self.service.register_actor(request_id=f"register-{actor_id}", actor_id="hub-admin",
                                        new_actor_id=actor_id, display_name=name, role=role,
                                        organization_id=org)
        self.service.register_site(request_id="site-hub", actor_id="hub-ops", site_id="site-hub",
                                   organization_id="org-hub", name="铁水联运枢纽",
                                   timezone_name="Asia/Shanghai")
        self.service.register_waybill(
            request_id="wb-1", actor_id="hub-ops", site_id="site-hub", waybill_id="WB-1",
            customer_org_id="org-customer",
            segments=[{"seq": 1, "mode": "rail", "carrier_org_id": "org-rail",
                       "liability_limit_minor": 300000},
                      {"seq": 2, "mode": "port", "carrier_org_id": "org-port",
                       "liability_limit_minor": 200000},
                      {"seq": 3, "mode": "water", "carrier_org_id": "org-water",
                       "liability_limit_minor": 300000}],
            groups=[{"group_no": "G1", "quantity": 10, "description": "数控设备",
                     "seals": ["SEAL-1", "SEAL-2"]}])
        view = self.service.get_waybill_view(actor_id="hub-admin", waybill_id="WB-1")
        self.segments = {item["seq"]: item for item in view["segments"]}
        self.group_id = view["groups"][0]["group_id"]

    def tearDown(self):
        self.database.close()

    def _record_handovers(self):
        first = self.service.record_handover(
            request_id="hov-1-2", actor_id="hub-ops", waybill_id="WB-1", group_id=self.group_id,
            from_seq=1, to_seq=2, occurred_at="2026-10-02T09:00:00Z",
            quantity_received=10, seal_intact=True, condition="good")
        second = self.service.record_handover(
            request_id="hov-2-3", actor_id="hub-ops", waybill_id="WB-1", group_id=self.group_id,
            from_seq=2, to_seq=3, occurred_at="2026-10-03T10:00:00Z",
            quantity_received=10, seal_intact=False, condition="damaged")
        self.service.record_handover(
            request_id="hov-3-0", actor_id="hub-ops", waybill_id="WB-1", group_id=self.group_id,
            from_seq=3, to_seq=0, occurred_at="2026-10-05T08:00:00Z",
            quantity_received=10, seal_intact=False, condition="damaged")
        return first.resource_id, second.resource_id

    def _open_claim(self, *, quantity=2, claimed=200000, deadline="2026-10-08T00:00:00Z",
                    request_id="claim-1", exception_key="EXP-DMG-1"):
        self.service.register_exception(
            request_id=f"exp-{exception_key}", actor_id="hub-ops", waybill_id="WB-1",
            group_id=self.group_id, category="damage", exception_key=exception_key,
            quantity=quantity, detected_seq=2, source_system="port-tos")
        exception_id = self.service.register_exception(
            request_id=f"exp2-{exception_key}", actor_id="hub-ops", waybill_id="WB-1",
            group_id=self.group_id, category="damage", exception_key=exception_key,
            quantity=quantity, detected_seq=2, source_system="rail-ims").resource_id
        receipt = self.service.open_claim_item(
            request_id=request_id, actor_id="hub-ops", exception_id=exception_id,
            category="damage", quantity=quantity, claimed_minor=claimed,
            evidence_deadline=deadline)
        return exception_id, receipt.resource_id

    def _draft(self, claim_id, *, awarded=195000, port=115000, rail=80000,
               basis=None, request_id="draft-1"):
        self.clock.set(datetime(2026, 10, 9, tzinfo=timezone.utc))
        return self.service.create_liability_draft(
            request_id=request_id, actor_id="hub-rev", claim_item_id=claim_id,
            awarded_minor=awarded,
            allocations=[{"segment_id": self.segments[2]["segment_id"], "amount_minor": port},
                         {"segment_id": self.segments[1]["segment_id"], "amount_minor": rail}],
            basis_receipt_ids=basis or []).resource_id

    def _decided_claim(self):
        self._record_handovers()
        _, claim_id = self._open_claim()
        draft_id = self._draft(claim_id)
        self.service.respond_to_draft(request_id="resp-c", actor_id="cust-ops",
                                      draft_id=draft_id, stance="confirm", note="")
        self.service.respond_to_draft(request_id="resp-r", actor_id="rail-ops",
                                      draft_id=draft_id, stance="confirm", note="")
        self.service.respond_to_draft(request_id="resp-p", actor_id="port-ops",
                                      draft_id=draft_id, stance="confirm", note="")
        return claim_id


class WaybillTest(ClaimsTestBase):
    def test_register_waybill_replays_same_request(self):
        replay = self.service.register_waybill(
            request_id="wb-1", actor_id="hub-ops", site_id="site-hub", waybill_id="WB-1",
            customer_org_id="org-customer",
            segments=[{"seq": 1, "mode": "rail", "carrier_org_id": "org-rail",
                       "liability_limit_minor": 300000},
                      {"seq": 2, "mode": "port", "carrier_org_id": "org-port",
                       "liability_limit_minor": 200000},
                      {"seq": 3, "mode": "water", "carrier_org_id": "org-water",
                       "liability_limit_minor": 300000}],
            groups=[{"group_no": "G1", "quantity": 10, "description": "数控设备",
                     "seals": ["SEAL-1", "SEAL-2"]}])
        self.assertTrue(replay.replayed)
        self.assertEqual("WB-1", replay.resource_id)

    def test_register_waybill_rejects_changed_payload(self):
        with self.assertRaises(ConflictError):
            self.service.register_waybill(
                request_id="wb-1", actor_id="hub-ops", site_id="site-hub", waybill_id="WB-2",
                customer_org_id="org-customer",
                segments=[{"seq": 1, "mode": "rail", "carrier_org_id": "org-rail",
                           "liability_limit_minor": 300000}],
                groups=[{"group_no": "G1", "quantity": 10, "description": "数控设备", "seals": []}])

    def test_waybill_validates_segments_and_groups(self):
        with self.assertRaises(ValidationError):
            self.service.register_waybill(
                request_id="wb-bad-1", actor_id="hub-ops", site_id="site-hub", waybill_id="WB-BAD",
                customer_org_id="org-customer",
                segments=[{"seq": 1, "mode": "rail", "carrier_org_id": "org-rail",
                           "liability_limit_minor": 300000},
                          {"seq": 1, "mode": "port", "carrier_org_id": "org-port",
                           "liability_limit_minor": 200000}],
                groups=[{"group_no": "G1", "quantity": 10, "description": "设备", "seals": []}])
        with self.assertRaises(ValidationError):
            self.service.register_waybill(
                request_id="wb-bad-2", actor_id="hub-ops", site_id="site-hub", waybill_id="WB-BAD",
                customer_org_id="org-customer",
                segments=[{"seq": 1, "mode": "rail", "carrier_org_id": "org-rail",
                           "liability_limit_minor": -1}],
                groups=[{"group_no": "G1", "quantity": 10, "description": "设备", "seals": []}])
        with self.assertRaises(NotFoundError):
            self.service.register_waybill(
                request_id="wb-bad-3", actor_id="hub-ops", site_id="site-hub", waybill_id="WB-BAD",
                customer_org_id="org-customer",
                segments=[{"seq": 1, "mode": "rail", "carrier_org_id": "org-ghost",
                           "liability_limit_minor": 100}],
                groups=[{"group_no": "G1", "quantity": 10, "description": "设备", "seals": []}])

    def test_non_hub_cannot_register_waybill(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_waybill(
                request_id="wb-x", actor_id="cust-ops", site_id="site-hub", waybill_id="WB-X",
                customer_org_id="org-customer",
                segments=[{"seq": 1, "mode": "rail", "carrier_org_id": "org-rail",
                           "liability_limit_minor": 100}],
                groups=[{"group_no": "G1", "quantity": 1, "description": "设备", "seals": []}])


class HandoverTest(ClaimsTestBase):
    def test_handover_versions_accumulate(self):
        self.service.record_handover(
            request_id="h-1", actor_id="hub-ops", waybill_id="WB-1", group_id=self.group_id,
            from_seq=2, to_seq=3, occurred_at="2026-10-03T10:00:00Z",
            quantity_received=10, seal_intact=False, condition="damaged")
        second = self.service.record_handover(
            request_id="h-2", actor_id="hub-ops", waybill_id="WB-1", group_id=self.group_id,
            from_seq=2, to_seq=3, occurred_at="2026-10-03T10:25:00Z",
            quantity_received=10, seal_intact=False, condition="damaged")
        history = self.service.list_handover_history(
            actor_id="regulator", waybill_id="WB-1", group_id=self.group_id, from_seq=2, to_seq=3)
        self.assertEqual([1, 2], [item["version"] for item in history])
        self.assertEqual(second.resource_id, history[1]["receipt_id"])
        view = self.service.get_waybill_view(actor_id="hub-admin", waybill_id="WB-1")
        self.assertEqual(2, view["handovers"][0]["version"])

    def test_handover_validates_point_and_quantity(self):
        with self.assertRaises(ValidationError):
            self.service.record_handover(
                request_id="h-bad-1", actor_id="hub-ops", waybill_id="WB-1", group_id=self.group_id,
                from_seq=1, to_seq=0, occurred_at="2026-10-02T09:00:00Z",
                quantity_received=10, seal_intact=True, condition="good")
        with self.assertRaises(ValidationError):
            self.service.record_handover(
                request_id="h-bad-2", actor_id="hub-ops", waybill_id="WB-1", group_id=self.group_id,
                from_seq=2, to_seq=1, occurred_at="2026-10-02T09:00:00Z",
                quantity_received=10, seal_intact=True, condition="good")
        with self.assertRaises(ValidationError):
            self.service.record_handover(
                request_id="h-bad-3", actor_id="hub-ops", waybill_id="WB-1", group_id=self.group_id,
                from_seq=1, to_seq=2, occurred_at="2026-10-02T09:00:00Z",
                quantity_received=11, seal_intact=True, condition="good")

    def test_carrier_sees_only_own_handover_history(self):
        self._record_handovers()
        with self.assertRaises(PermissionDenied):
            self.service.list_handover_history(
                actor_id="rail-ops", waybill_id="WB-1", group_id=self.group_id,
                from_seq=2, to_seq=3)
        history = self.service.list_handover_history(
            actor_id="rail-ops", waybill_id="WB-1", group_id=self.group_id, from_seq=1, to_seq=2)
        self.assertEqual(1, len(history))


class ExceptionAndClaimTest(ClaimsTestBase):
    def test_exception_deduplicated_across_systems(self):
        first = self.service.register_exception(
            request_id="exp-1", actor_id="hub-ops", waybill_id="WB-1", group_id=self.group_id,
            category="damage", exception_key="EXP-DMG-1", quantity=2, detected_seq=2,
            source_system="port-tos")
        replay = self.service.register_exception(
            request_id="exp-2", actor_id="hub-ops", waybill_id="WB-1", group_id=self.group_id,
            category="damage", exception_key="EXP-DMG-1", quantity=2, detected_seq=2,
            source_system="rail-ims")
        self.assertEqual(first.resource_id, replay.resource_id)
        view = self.service.get_waybill_view(actor_id="hub-admin", waybill_id="WB-1")
        self.assertEqual(1, len(view["exceptions"]))

    def test_exception_replay_with_different_content_conflicts(self):
        self.service.register_exception(
            request_id="exp-1", actor_id="hub-ops", waybill_id="WB-1", group_id=self.group_id,
            category="damage", exception_key="EXP-DMG-1", quantity=2, detected_seq=2,
            source_system="port-tos")
        with self.assertRaises(ConflictError):
            self.service.register_exception(
                request_id="exp-2", actor_id="hub-ops", waybill_id="WB-1", group_id=self.group_id,
                category="damage", exception_key="EXP-DMG-1", quantity=3, detected_seq=2,
                source_system="rail-ims")

    def test_exception_quantity_cannot_exceed_group(self):
        with self.assertRaises(ValidationError):
            self.service.register_exception(
                request_id="exp-1", actor_id="hub-ops", waybill_id="WB-1", group_id=self.group_id,
                category="damage", exception_key="EXP-DMG-1", quantity=11, detected_seq=2,
                source_system="port-tos")
        self.service.register_exception(
            request_id="exp-2", actor_id="hub-ops", waybill_id="WB-1", group_id=self.group_id,
            category="damage", exception_key="EXP-DMG-2", quantity=6, detected_seq=2,
            source_system="port-tos")
        with self.assertRaises(ValidationError):
            self.service.register_exception(
                request_id="exp-3", actor_id="hub-ops", waybill_id="WB-1", group_id=self.group_id,
                category="shortage", exception_key="EXP-SHT-1", quantity=6, detected_seq=2,
                source_system="port-tos")

    def test_claim_items_split_and_conserve_quantity(self):
        self.service.register_exception(
            request_id="exp-1", actor_id="hub-ops", waybill_id="WB-1", group_id=self.group_id,
            category="damage", exception_key="EXP-DMG-1", quantity=4, detected_seq=2,
            source_system="port-tos")
        exception_id = self.service.get_waybill_view(
            actor_id="hub-admin", waybill_id="WB-1")["exceptions"][0]["exception_id"]
        self.service.open_claim_item(request_id="c-1", actor_id="hub-ops", exception_id=exception_id,
                                     category="damage", quantity=2, claimed_minor=100000,
                                     evidence_deadline="2026-10-08T00:00:00Z")
        self.service.open_claim_item(request_id="c-2", actor_id="hub-ops", exception_id=exception_id,
                                     category="damage", quantity=2, claimed_minor=80000,
                                     evidence_deadline="2026-10-08T00:00:00Z")
        with self.assertRaises(ValidationError):
            self.service.open_claim_item(request_id="c-3", actor_id="hub-ops",
                                         exception_id=exception_id, category="damage", quantity=1,
                                         claimed_minor=1000,
                                         evidence_deadline="2026-10-08T00:00:00Z")

    def test_claim_category_must_match_exception(self):
        self.service.register_exception(
            request_id="exp-1", actor_id="hub-ops", waybill_id="WB-1", group_id=self.group_id,
            category="damage", exception_key="EXP-DMG-1", quantity=2, detected_seq=2,
            source_system="port-tos")
        exception_id = self.service.get_waybill_view(
            actor_id="hub-admin", waybill_id="WB-1")["exceptions"][0]["exception_id"]
        with self.assertRaises(ValidationError):
            self.service.open_claim_item(request_id="c-1", actor_id="hub-ops",
                                         exception_id=exception_id, category="shortage", quantity=1,
                                         claimed_minor=1000,
                                         evidence_deadline="2026-10-08T00:00:00Z")

    def test_evidence_late_flag_follows_deadline(self):
        _, claim_id = self._open_claim()
        early = self.service.submit_evidence(request_id="ev-1", actor_id="cust-ops",
                                             claim_item_id=claim_id, kind="photo",
                                             data={"images": ["a.jpg"]})
        self.clock.set(datetime(2026, 10, 9, tzinfo=timezone.utc))
        late = self.service.submit_evidence(request_id="ev-2", actor_id="cust-ops",
                                            claim_item_id=claim_id, kind="report",
                                            data={"note": "补充报告"})
        view = self.service.get_claim_item_view(actor_id="hub-admin", claim_item_id=claim_id)
        flags = {item["evidence_id"]: item["late"] for item in view["evidence"]}
        self.assertFalse(flags[early.resource_id])
        self.assertTrue(flags[late.resource_id])

    def test_outsider_cannot_submit_evidence(self):
        _, claim_id = self._open_claim()
        with self.assertRaises(PermissionDenied):
            self.service.submit_evidence(request_id="ev-x", actor_id="outsider",
                                         claim_item_id=claim_id, kind="photo",
                                         data={"images": ["a.jpg"]})


class DraftAndDecisionTest(ClaimsTestBase):
    def test_draft_requires_evidence_deadline(self):
        _, claim_id = self._open_claim()
        with self.assertRaises(ConflictError):
            self.service.create_liability_draft(
                request_id="draft-1", actor_id="hub-rev", claim_item_id=claim_id,
                awarded_minor=100000,
                allocations=[{"segment_id": self.segments[2]["segment_id"], "amount_minor": 100000}],
                basis_receipt_ids=[])

    def test_draft_validates_allocations_and_basis(self):
        receipt_ids = self._record_handovers()
        _, claim_id = self._open_claim()
        self.clock.set(datetime(2026, 10, 9, tzinfo=timezone.utc))
        with self.assertRaises(ValidationError):
            self.service.create_liability_draft(
                request_id="draft-1", actor_id="hub-rev", claim_item_id=claim_id,
                awarded_minor=100000,
                allocations=[{"segment_id": self.segments[2]["segment_id"], "amount_minor": 90000}],
                basis_receipt_ids=[])
        with self.assertRaises(ValidationError):
            self.service.create_liability_draft(
                request_id="draft-2", actor_id="hub-rev", claim_item_id=claim_id,
                awarded_minor=250000,
                allocations=[{"segment_id": self.segments[2]["segment_id"], "amount_minor": 250000}],
                basis_receipt_ids=[])
        with self.assertRaises(ValidationError):
            self.service.create_liability_draft(
                request_id="draft-3", actor_id="hub-rev", claim_item_id=claim_id,
                awarded_minor=100000,
                allocations=[{"segment_id": self.segments[2]["segment_id"], "amount_minor": 100000}],
                basis_receipt_ids=["missing-receipt"])
        draft_id = self.service.create_liability_draft(
            request_id="draft-4", actor_id="hub-rev", claim_item_id=claim_id,
            awarded_minor=195000,
            allocations=[{"segment_id": self.segments[2]["segment_id"], "amount_minor": 115000},
                         {"segment_id": self.segments[1]["segment_id"], "amount_minor": 80000}],
            basis_receipt_ids=[receipt_ids[1]]).resource_id
        view = self.service.get_claim_item_view(actor_id="hub-admin", claim_item_id=claim_id)
        self.assertEqual([receipt_ids[1]], view["drafts"][0]["basis_receipt_ids"])
        self.assertEqual(draft_id, view["drafts"][0]["draft_id"])

    def test_full_confirmation_creates_decision(self):
        claim_id = self._decided_claim()
        view = self.service.get_claim_item_view(actor_id="hub-admin", claim_item_id=claim_id)
        self.assertEqual("decided", view["claim_item"]["status"])
        self.assertEqual("confirmed", view["decision"]["decided_via"])
        self.assertEqual(195000, view["decision"]["awarded_minor"])
        self.assertEqual("confirmed", view["drafts"][0]["status"])

    def test_rebut_moves_to_arbitration(self):
        self._record_handovers()
        _, claim_id = self._open_claim()
        draft_id = self._draft(claim_id)
        self.service.respond_to_draft(request_id="resp-c", actor_id="cust-ops",
                                      draft_id=draft_id, stance="confirm", note="")
        self.service.respond_to_draft(request_id="resp-p", actor_id="port-ops",
                                      draft_id=draft_id, stance="rebut", note="交接时间不一致")
        view = self.service.get_claim_item_view(actor_id="hub-admin", claim_item_id=claim_id)
        self.assertEqual("arbitrating", view["drafts"][0]["status"])
        self.assertIsNone(view["decision"])
        with self.assertRaises(ConflictError):
            self.service.respond_to_draft(request_id="resp-r", actor_id="rail-ops",
                                          draft_id=draft_id, stance="confirm", note="")

    def test_duplicate_response_conflicts(self):
        self._record_handovers()
        _, claim_id = self._open_claim()
        draft_id = self._draft(claim_id)
        self.service.respond_to_draft(request_id="resp-c", actor_id="cust-ops",
                                      draft_id=draft_id, stance="confirm", note="")
        with self.assertRaises(ConflictError):
            self.service.respond_to_draft(request_id="resp-c2", actor_id="cust-ops",
                                          draft_id=draft_id, stance="rebut", note="反悔")

    def test_outsider_cannot_respond(self):
        self._record_handovers()
        _, claim_id = self._open_claim()
        draft_id = self._draft(claim_id)
        with self.assertRaises(PermissionDenied):
            self.service.respond_to_draft(request_id="resp-x", actor_id="outsider",
                                          draft_id=draft_id, stance="confirm", note="")
        with self.assertRaises(PermissionDenied):
            self.service.respond_to_draft(request_id="resp-w", actor_id="water-ops",
                                          draft_id=draft_id, stance="confirm", note="")

    def test_arbitration_creates_decision_and_blocks_new_drafts(self):
        self._record_handovers()
        _, claim_id = self._open_claim()
        draft_id = self._draft(claim_id)
        # 草案未被申辩时不能仲裁。
        with self.assertRaises(ConflictError):
            self.service.record_arbitration(
                request_id="arb-0", actor_id="hub-rev", draft_id=draft_id,
                allocations=[{"segment_id": self.segments[2]["segment_id"], "amount_minor": 115000},
                             {"segment_id": self.segments[1]["segment_id"], "amount_minor": 80000}])
        self.service.respond_to_draft(request_id="resp-c", actor_id="cust-ops",
                                      draft_id=draft_id, stance="confirm", note="")
        self.service.respond_to_draft(request_id="resp-r", actor_id="rail-ops",
                                      draft_id=draft_id, stance="confirm", note="")
        self.service.respond_to_draft(request_id="resp-p", actor_id="port-ops",
                                      draft_id=draft_id, stance="rebut", note="异议")
        decision = self.service.record_arbitration(
            request_id="arb-1", actor_id="hub-rev", draft_id=draft_id,
            allocations=[{"segment_id": self.segments[2]["segment_id"], "amount_minor": 115000},
                         {"segment_id": self.segments[1]["segment_id"], "amount_minor": 80000}])
        view = self.service.get_claim_item_view(actor_id="hub-admin", claim_item_id=claim_id)
        self.assertEqual("arbitrated", view["decision"]["decided_via"])
        self.assertEqual(decision.resource_id, view["decision"]["decision_id"])
        with self.assertRaises(ConflictError):
            self.service.create_liability_draft(
                request_id="draft-9", actor_id="hub-rev", claim_item_id=claim_id,
                awarded_minor=1000,
                allocations=[{"segment_id": self.segments[1]["segment_id"], "amount_minor": 1000}],
                basis_receipt_ids=[])

    def test_arbitration_cannot_undercut_settled_amount(self):
        self._record_handovers()
        _, claim_id = self._open_claim()
        draft_id = self._draft(claim_id)
        self.service.respond_to_draft(request_id="resp-c", actor_id="cust-ops",
                                      draft_id=draft_id, stance="confirm", note="")
        self.service.respond_to_draft(request_id="resp-r", actor_id="rail-ops",
                                      draft_id=draft_id, stance="confirm", note="")
        self.service.respond_to_draft(request_id="resp-p", actor_id="port-ops",
                                      draft_id=draft_id, stance="rebut", note="异议")
        self.service.settle_allocation(request_id="settle-rail", actor_id="hub-ops",
                                       claim_item_id=claim_id,
                                       segment_id=self.segments[1]["segment_id"],
                                       amount_minor=80000)
        with self.assertRaises(ValidationError):
            self.service.record_arbitration(
                request_id="arb-1", actor_id="hub-rev", draft_id=draft_id,
                allocations=[{"segment_id": self.segments[2]["segment_id"], "amount_minor": 115000},
                             {"segment_id": self.segments[1]["segment_id"], "amount_minor": 70000}])


class SettlementTest(ClaimsTestBase):
    def _arbitrating_claim(self):
        self._record_handovers()
        _, claim_id = self._open_claim()
        draft_id = self._draft(claim_id)
        self.service.respond_to_draft(request_id="resp-c", actor_id="cust-ops",
                                      draft_id=draft_id, stance="confirm", note="")
        self.service.respond_to_draft(request_id="resp-r", actor_id="rail-ops",
                                      draft_id=draft_id, stance="confirm", note="")
        self.service.respond_to_draft(request_id="resp-p", actor_id="port-ops",
                                      draft_id=draft_id, stance="rebut", note="异议")
        return claim_id, draft_id

    def test_partial_settlement_pays_undisputed_amount(self):
        claim_id, draft_id = self._arbitrating_claim()
        receipt = self.service.settle_allocation(
            request_id="settle-rail", actor_id="hub-ops", claim_item_id=claim_id,
            segment_id=self.segments[1]["segment_id"], amount_minor=80000)
        self.assertFalse(receipt.replayed)
        with self.assertRaises(ConflictError):
            self.service.settle_allocation(
                request_id="settle-port", actor_id="hub-ops", claim_item_id=claim_id,
                segment_id=self.segments[2]["segment_id"], amount_minor=115000)
        with self.assertRaises(ConflictError):
            self.service.settle_allocation(
                request_id="settle-rail-2", actor_id="hub-ops", claim_item_id=claim_id,
                segment_id=self.segments[1]["segment_id"], amount_minor=1)
        view = self.service.get_claim_item_view(actor_id="hub-admin", claim_item_id=claim_id)
        self.assertEqual(["partial"], [item["kind"] for item in view["settlements"]])

    def test_settlement_requires_double_confirmation(self):
        self._record_handovers()
        _, claim_id = self._open_claim()
        draft_id = self._draft(claim_id)
        self.service.respond_to_draft(request_id="resp-r", actor_id="rail-ops",
                                      draft_id=draft_id, stance="confirm", note="")
        with self.assertRaises(ConflictError):
            self.service.settle_allocation(
                request_id="settle-rail", actor_id="hub-ops", claim_item_id=claim_id,
                segment_id=self.segments[1]["segment_id"], amount_minor=80000)

    def test_settlement_caps_prevent_double_payment(self):
        claim_id = self._decided_claim()
        self.service.settle_allocation(
            request_id="settle-port", actor_id="hub-ops", claim_item_id=claim_id,
            segment_id=self.segments[2]["segment_id"], amount_minor=115000)
        with self.assertRaises(ConflictError):
            self.service.settle_allocation(
                request_id="settle-port-2", actor_id="hub-ops", claim_item_id=claim_id,
                segment_id=self.segments[2]["segment_id"], amount_minor=1)
        replay = self.service.settle_allocation(
            request_id="settle-port", actor_id="hub-ops", claim_item_id=claim_id,
            segment_id=self.segments[2]["segment_id"], amount_minor=115000)
        self.assertTrue(replay.replayed)
        view = self.service.get_claim_item_view(actor_id="hub-admin", claim_item_id=claim_id)
        self.assertEqual(1, len(view["settlements"]))

    def test_settlement_without_basis_rejected(self):
        _, claim_id = self._open_claim()
        with self.assertRaises(ConflictError):
            self.service.settle_allocation(
                request_id="settle-1", actor_id="hub-ops", claim_item_id=claim_id,
                segment_id=self.segments[1]["segment_id"], amount_minor=1000)


class ReviewTest(ClaimsTestBase):
    def _decided_with_late_evidence(self):
        claim_id = self._decided_claim()
        self.clock.set(datetime(2026, 10, 12, tzinfo=timezone.utc))
        evidence = self.service.submit_evidence(request_id="ev-late", actor_id="cust-ops",
                                                claim_item_id=claim_id, kind="report",
                                                data={"note": "补充的检验报告"})
        return claim_id, evidence.resource_id

    def test_late_evidence_triggers_review_and_additional_entry(self):
        claim_id, evidence_id = self._decided_with_late_evidence()
        review = self.service.request_review(request_id="review-1", actor_id="cust-ops",
                                             claim_item_id=claim_id, evidence_id=evidence_id)
        self.service.close_review(
            request_id="review-1-close", actor_id="hub-rev", review_id=review.resource_id,
            additional_entries=[{"segment_id": self.segments[2]["segment_id"],
                                 "amount_minor": 5000}])
        view = self.service.get_claim_item_view(actor_id="hub-admin", claim_item_id=claim_id)
        self.assertEqual(195000, view["decision"]["awarded_minor"])
        kinds = sorted(item["kind"] for item in view["settlements"])
        self.assertEqual(["additional"], kinds)
        self.assertEqual("closed", view["reviews"][0]["status"])

    def test_review_requires_late_evidence_and_decision(self):
        self._record_handovers()
        _, claim_id = self._open_claim()
        evidence = self.service.submit_evidence(request_id="ev-1", actor_id="cust-ops",
                                                claim_item_id=claim_id, kind="photo",
                                                data={"images": ["a.jpg"]})
        # 尚未形成责任决定，不能复审。
        with self.assertRaises(ConflictError):
            self.service.request_review(request_id="review-1", actor_id="cust-ops",
                                        claim_item_id=claim_id, evidence_id=evidence.resource_id)
        draft_id = self._draft(claim_id)
        self.service.respond_to_draft(request_id="resp-c", actor_id="cust-ops",
                                      draft_id=draft_id, stance="confirm", note="")
        self.service.respond_to_draft(request_id="resp-r", actor_id="rail-ops",
                                      draft_id=draft_id, stance="confirm", note="")
        self.service.respond_to_draft(request_id="resp-p", actor_id="port-ops",
                                      draft_id=draft_id, stance="confirm", note="")
        # 截止前提交的证据不能触发复审。
        with self.assertRaises(ValidationError):
            self.service.request_review(request_id="review-2", actor_id="cust-ops",
                                        claim_item_id=claim_id, evidence_id=evidence.resource_id)

    def test_only_one_open_review_per_claim(self):
        claim_id, evidence_id = self._decided_with_late_evidence()
        self.service.request_review(request_id="review-1", actor_id="cust-ops",
                                    claim_item_id=claim_id, evidence_id=evidence_id)
        with self.assertRaises(ConflictError):
            self.service.request_review(request_id="review-2", actor_id="hub-rev",
                                        claim_item_id=claim_id, evidence_id=evidence_id)

    def test_additional_entries_respect_limits(self):
        claim_id, evidence_id = self._decided_with_late_evidence()
        self.service.settle_allocation(
            request_id="settle-port", actor_id="hub-ops", claim_item_id=claim_id,
            segment_id=self.segments[2]["segment_id"], amount_minor=115000)
        review = self.service.request_review(request_id="review-1", actor_id="cust-ops",
                                             claim_item_id=claim_id, evidence_id=evidence_id)
        with self.assertRaises(ValidationError):
            self.service.close_review(
                request_id="review-1-close", actor_id="hub-rev", review_id=review.resource_id,
                additional_entries=[{"segment_id": self.segments[2]["segment_id"],
                                     "amount_minor": 90000}])
        with self.assertRaises(ValidationError):
            self.service.close_review(
                request_id="review-1-close-2", actor_id="hub-rev", review_id=review.resource_id,
                additional_entries=[{"segment_id": self.segments[2]["segment_id"],
                                     "amount_minor": -200000}])
        self.service.close_review(
            request_id="review-1-close-3", actor_id="hub-rev", review_id=review.resource_id,
            additional_entries=[{"segment_id": self.segments[2]["segment_id"],
                                 "amount_minor": 85000}])
        report = self.service.conservation_report(actor_id="regulator", waybill_id="WB-1")
        self.assertTrue(report["compensation"]["conserved"])

    def test_original_decision_survives_review(self):
        claim_id, evidence_id = self._decided_with_late_evidence()
        before = self.service.get_claim_item_view(actor_id="hub-admin", claim_item_id=claim_id)
        review = self.service.request_review(request_id="review-1", actor_id="cust-ops",
                                             claim_item_id=claim_id, evidence_id=evidence_id)
        self.service.close_review(
            request_id="review-1-close", actor_id="hub-rev", review_id=review.resource_id,
            additional_entries=[{"segment_id": self.segments[1]["segment_id"],
                                 "amount_minor": 3000}])
        after = self.service.get_claim_item_view(actor_id="hub-admin", claim_item_id=claim_id)
        self.assertEqual(before["decision"], after["decision"])
        self.assertEqual(len(before["settlements"]) + 1, len(after["settlements"]))


class DisclosureTest(ClaimsTestBase):
    def test_customer_view_hides_contract_limits(self):
        view = self.service.get_waybill_view(actor_id="cust-ops", waybill_id="WB-1")
        self.assertEqual("customer", view["perspective"])
        self.assertEqual(3, len(view["segments"]))
        for segment in view["segments"]:
            self.assertNotIn("liability_limit_minor", segment)

    def test_carrier_view_is_scoped_to_own_segment(self):
        self._record_handovers()
        _, claim_id = self._open_claim()
        self.service.submit_evidence(request_id="ev-p", actor_id="port-ops",
                                     claim_item_id=claim_id, kind="report",
                                     data={"note": "港口内部记录"})
        self._draft(claim_id)
        view = self.service.get_waybill_view(actor_id="rail-ops", waybill_id="WB-1")
        self.assertEqual("carrier", view["perspective"])
        self.assertEqual([1], [item["seq"] for item in view["segments"]])
        self.assertEqual("org-rail", view["segments"][0]["carrier_org_id"])
        self.assertIn("liability_limit_minor", view["segments"][0])
        self.assertEqual([(1, 2)], [(item["from_seq"], item["to_seq"])
                                    for item in view["handovers"]])
        claim_view = self.service.get_claim_item_view(actor_id="rail-ops", claim_item_id=claim_id)
        self.assertNotIn("claimed_minor", claim_view["claim_item"])
        allocations = claim_view["drafts"][0]["allocations"]
        self.assertEqual(["org-rail"], [item["carrier_org_id"] for item in allocations])
        self.assertEqual(1, len(claim_view["evidence"]))
        self.assertNotIn("payload", claim_view["evidence"][0])

    def test_unrelated_carrier_cannot_open_claim_view(self):
        self._record_handovers()
        _, claim_id = self._open_claim()
        self._draft(claim_id)
        with self.assertRaises(PermissionDenied):
            self.service.get_claim_item_view(actor_id="water-ops", claim_item_id=claim_id)

    def test_outsider_cannot_view_waybill(self):
        with self.assertRaises(PermissionDenied):
            self.service.get_waybill_view(actor_id="outsider", waybill_id="WB-1")

    def test_evidence_payload_visible_only_to_submitter_and_hub(self):
        _, claim_id = self._open_claim()
        self.service.submit_evidence(request_id="ev-c", actor_id="cust-ops",
                                     claim_item_id=claim_id, kind="photo",
                                     data={"images": ["secret.jpg"]})
        self.service.submit_evidence(request_id="ev-p", actor_id="port-ops",
                                     claim_item_id=claim_id, kind="report",
                                     data={"note": "港口内部记录"})
        customer_view = self.service.get_claim_item_view(actor_id="cust-ops", claim_item_id=claim_id)
        payloads = {item["kind"]: item.get("payload") for item in customer_view["evidence"]}
        self.assertEqual({"images": ["secret.jpg"]}, payloads["photo"])
        self.assertIsNone(payloads["report"])
        hub_view = self.service.get_claim_item_view(actor_id="hub-admin", claim_item_id=claim_id)
        self.assertTrue(all("payload" in item for item in hub_view["evidence"]))

    def test_auditor_is_read_only(self):
        _, claim_id = self._open_claim()
        with self.assertRaises(PermissionDenied):
            self.service.submit_evidence(request_id="ev-x", actor_id="regulator",
                                         claim_item_id=claim_id, kind="photo",
                                         data={"images": ["a.jpg"]})
        view = self.service.get_waybill_view(actor_id="regulator", waybill_id="WB-1")
        self.assertEqual("full", view["perspective"])


class ConservationTest(ClaimsTestBase):
    def test_conservation_report_and_basis_restore(self):
        self._record_handovers()
        _, claim_id = self._open_claim()
        basis = self.service.list_handover_history(
            actor_id="regulator", waybill_id="WB-1", group_id=self.group_id,
            from_seq=2, to_seq=3)[0]["receipt_id"]
        draft_id = self._draft(claim_id, basis=[basis])
        self.service.respond_to_draft(request_id="resp-c", actor_id="cust-ops",
                                      draft_id=draft_id, stance="confirm", note="")
        self.service.respond_to_draft(request_id="resp-r", actor_id="rail-ops",
                                      draft_id=draft_id, stance="confirm", note="")
        self.service.respond_to_draft(request_id="resp-p", actor_id="port-ops",
                                      draft_id=draft_id, stance="confirm", note="")
        self.service.settle_allocation(
            request_id="settle-port", actor_id="hub-ops", claim_item_id=claim_id,
            segment_id=self.segments[2]["segment_id"], amount_minor=115000)
        report = self.service.conservation_report(actor_id="regulator", waybill_id="WB-1")
        self.assertTrue(report["quantity"]["conserved"])
        self.assertTrue(report["compensation"]["conserved"])
        totals = report["compensation"]["totals"]
        self.assertEqual(200000, totals["claimed_minor"])
        self.assertEqual(195000, totals["awarded_minor"])
        self.assertEqual(115000, totals["carrier_paid_minor"])
        self.assertEqual(totals["carrier_paid_minor"], totals["customer_received_minor"])
        claim_report = report["compensation"]["claim_items"][0]
        self.assertEqual([basis], [item["receipt_id"]
                                   for item in claim_report["basis_receipt_versions"]])
        decision_id = self.service.get_claim_item_view(
            actor_id="hub-admin", claim_item_id=claim_id)["decision"]["decision_id"]
        restored = self.service.decision_basis(actor_id="regulator", decision_id=decision_id)
        self.assertEqual([basis], [item["receipt_id"] for item in restored["basis_receipts"]])
        self.assertEqual(1, restored["basis_receipts"][0]["version"])

    def test_conservation_report_detects_tampering(self):
        claim_id = self._decided_claim()
        self.database.connection.execute(
            "INSERT INTO settlement_entries(entry_id,claim_item_id,segment_id,payer_org_id,"
            "payee_org_id,amount_minor,kind,review_id,created_by,created_at) "
            "VALUES('tampered',?,?,?,?,?,?,NULL,'hub-ops','2026-10-10T00:00:00Z')",
            (claim_id, self.segments[2]["segment_id"], "org-port", "org-customer",
             999999, "decision"))
        report = self.service.conservation_report(actor_id="regulator", waybill_id="WB-1")
        self.assertFalse(report["compensation"]["conserved"])
        self.assertTrue(any("超过责任认定" in item
                            for item in report["compensation"]["violations"]))

    def test_conservation_report_requires_regulator(self):
        with self.assertRaises(PermissionDenied):
            self.service.conservation_report(actor_id="cust-ops", waybill_id="WB-1")
        claim_id = self._decided_claim()
        decision_id = self.service.get_claim_item_view(
            actor_id="hub-admin", claim_item_id=claim_id)["decision"]["decision_id"]
        with self.assertRaises(PermissionDenied):
            self.service.decision_basis(actor_id="rail-ops", decision_id=decision_id)

    def test_audit_chain_remains_valid(self):
        claim_id = self._decided_claim()
        self.clock.set(datetime(2026, 10, 12, tzinfo=timezone.utc))
        evidence_id = self.service.submit_evidence(
            request_id="ev-late", actor_id="cust-ops", claim_item_id=claim_id, kind="report",
            data={"note": "补充"}).resource_id
        review = self.service.request_review(request_id="review-1", actor_id="cust-ops",
                                             claim_item_id=claim_id, evidence_id=evidence_id)
        self.service.close_review(
            request_id="review-1-close", actor_id="hub-rev", review_id=review.resource_id,
            additional_entries=[{"segment_id": self.segments[2]["segment_id"],
                                 "amount_minor": 5000}])
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
