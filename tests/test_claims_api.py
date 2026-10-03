import unittest
from datetime import datetime, timezone

from transport_coordination.api import route
from transport_coordination.claims import ClaimsService
from transport_coordination.clock import MutableClock
from transport_coordination.storage import Database


class ClaimsApiTest(unittest.TestCase):
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
                             ("org-port", "港口作业方"), ("org-regulator", "监管机构")):
            self.service.register_organization(request_id=f"register-{org_id}",
                                               actor_id="hub-admin", organization_id=org_id,
                                               name=name)
        for actor_id, name, role, org in (
            ("hub-ops", "枢纽操作员", "operator", "org-hub"),
            ("cust-ops", "客户专员", "operator", "org-customer"),
            ("rail-ops", "铁路专员", "operator", "org-rail"),
            ("regulator", "监管人员", "auditor", "org-regulator"),
        ):
            self.service.register_actor(request_id=f"register-{actor_id}", actor_id="hub-admin",
                                        new_actor_id=actor_id, display_name=name, role=role,
                                        organization_id=org)
        self.service.register_site(request_id="site-hub", actor_id="hub-ops", site_id="site-hub",
                                   organization_id="org-hub", name="铁水联运枢纽",
                                   timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def _waybill_body(self, request_id="wb-1"):
        return {"request_id": request_id, "site_id": "site-hub", "waybill_id": "WB-1",
                "customer_org_id": "org-customer",
                "segments": [{"seq": 1, "mode": "rail", "carrier_org_id": "org-rail",
                              "liability_limit_minor": 300000},
                             {"seq": 2, "mode": "port", "carrier_org_id": "org-port",
                              "liability_limit_minor": 200000}],
                "groups": [{"group_no": "G1", "quantity": 10, "description": "数控设备",
                            "seals": ["SEAL-1"]}]}

    def _register_waybill(self):
        status, payload = route(self.service, "POST", "/waybills", self._waybill_body(),
                                {"X-Actor-Id": "hub-ops"})
        self.assertEqual(201, status)
        return payload

    def test_register_waybill_and_replay(self):
        first = self._register_waybill()
        self.assertFalse(first["replayed"])
        status, second = route(self.service, "POST", "/waybills", self._waybill_body(),
                               {"X-Actor-Id": "hub-ops"})
        self.assertEqual(200, status)
        self.assertTrue(second["replayed"])

    def test_waybill_view_scoped_by_actor(self):
        self._register_waybill()
        status, customer_view = route(self.service, "GET", "/waybill-view?waybill_id=WB-1",
                                      None, {"X-Actor-Id": "cust-ops"})
        self.assertEqual(200, status)
        self.assertEqual("customer", customer_view["perspective"])
        self.assertNotIn("liability_limit_minor", customer_view["segments"][0])
        status, carrier_view = route(self.service, "GET", "/waybill-view?waybill_id=WB-1",
                                     None, {"X-Actor-Id": "rail-ops"})
        self.assertEqual(200, status)
        self.assertEqual("carrier", carrier_view["perspective"])
        self.assertEqual(1, len(carrier_view["segments"]))
        status, payload = route(self.service, "GET", "/waybill-view?waybill_id=WB-1", None)
        self.assertEqual(404, status)

    def test_exception_replay_across_systems_via_api(self):
        self._register_waybill()
        view = self.service.get_waybill_view(actor_id="hub-admin", waybill_id="WB-1")
        group_id = view["groups"][0]["group_id"]
        body = {"waybill_id": "WB-1", "group_id": group_id,
                "category": "damage", "exception_key": "EXP-DMG-1", "quantity": 2,
                "detected_seq": 2, "source_system": "port-tos"}
        status, first = route(self.service, "POST", "/exceptions",
                              {"request_id": "exp-1", **body}, {"X-Actor-Id": "hub-ops"})
        self.assertEqual(201, status)
        status, replay = route(self.service, "POST", "/exceptions",
                               {"request_id": "exp-2", **body, "source_system": "rail-ims"},
                               {"X-Actor-Id": "hub-ops"})
        self.assertEqual(201, status)
        self.assertEqual(first["resource_id"], replay["resource_id"])

    def test_conservation_report_requires_regulator(self):
        self._register_waybill()
        status, payload = route(self.service, "GET", "/conservation-report?waybill_id=WB-1",
                                None, {"X-Actor-Id": "cust-ops"})
        self.assertEqual(403, status)
        status, report = route(self.service, "GET", "/conservation-report?waybill_id=WB-1",
                               None, {"X-Actor-Id": "regulator"})
        self.assertEqual(200, status)
        self.assertTrue(report["quantity"]["conserved"])
        self.assertTrue(report["compensation"]["conserved"])

    def test_unknown_claim_route_returns_404(self):
        status, payload = route(self.service, "GET", "/missing-claims", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
