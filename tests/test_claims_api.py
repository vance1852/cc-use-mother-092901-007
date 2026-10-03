import unittest
from datetime import datetime, timezone

from transport_coordination.api import route
from transport_coordination.claims_service import ClaimsService
from transport_coordination.clock import FixedClock
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


class ClaimsApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
        self.base = DomainService(self.database, clock)
        self.claims = ClaimsService(self.database, clock)
        self.base.register_organization(request_id="oh", actor_id="bootstrap",
                                        organization_id="hub", name="枢纽")
        self.base.register_organization(request_id="oc", actor_id="bootstrap",
                                        organization_id="cust", name="客户")
        self.base.register_actor(request_id="aa", actor_id="bootstrap", new_actor_id="admin",
                                 display_name="管理员", role="admin", organization_id="hub")
        self.base.register_actor(request_id="ao", actor_id="admin", new_actor_id="op",
                                 display_name="运营", role="operator", organization_id="hub")
        self.base.register_actor(request_id="ac", actor_id="admin", new_actor_id="cust",
                                 display_name="客户", role="customer", organization_id="cust")

    def tearDown(self):
        self.database.close()

    def _route(self, method, path, body=None, actor="op"):
        return route(self.base, method, path, body, {"X-Actor-Id": actor}, self.claims)

    def test_register_waybill_route(self):
        status, payload = self._route("POST", "/waybills", {
            "request_id": "wb1", "waybill_id": "w1", "waybill_no": "WB-1",
            "customer_org_id": "cust", "origin_name": "起", "destination_name": "终",
            "cargo_quantity": 1, "cargo_unit": "件", "declared_value": 100,
            "currency": "CNY",
        })
        self.assertEqual(201, status)
        self.assertEqual("w1", payload["resource_id"])

    def test_customer_can_report_incident_route(self):
        self._route("POST", "/waybills", {
            "request_id": "wb1", "waybill_id": "w1", "waybill_no": "WB-1",
            "customer_org_id": "cust", "origin_name": "起", "destination_name": "终",
            "cargo_quantity": 1, "cargo_unit": "件", "declared_value": 100,
            "currency": "CNY",
        })
        status, payload = self._route("POST", "/incidents", {
            "request_id": "i1", "waybill_id": "w1", "incident_id": "i1",
            "title": "破损", "discovered_at": "2026-10-02T08:00:00Z",
            "evidence_deadline": "2026-10-05T18:00:00Z", "dedup_key": "w1:x",
        }, actor="cust")
        self.assertEqual(201, status)

    def test_case_bundle_requires_participant(self):
        self._route("POST", "/waybills", {
            "request_id": "wb1", "waybill_id": "w1", "waybill_no": "WB-1",
            "customer_org_id": "cust", "origin_name": "起", "destination_name": "终",
            "cargo_quantity": 1, "cargo_unit": "件", "declared_value": 100,
            "currency": "CNY",
        })
        status, payload = self._route("GET", "/cases/w1", actor="op")
        self.assertEqual(200, status)
        self.assertEqual("internal", payload["view"])

    def test_missing_waybill_is_404(self):
        status, payload = self._route("GET", "/cases/missing", actor="op")
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
