"""Private native financial audit remains bound after local website erasure."""

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase

from . import cms_goal_settlement as settlement
from . import cms_goal_billing as billing
from .cms_goal_contract import CmsGoalError
from .channel_artifact_contract import make_envelope, digest
from .tests_cms_inventory_contract_unit import binding
from .dispatch_models import ContentFactoryDispatchOutbox
from .website_models import WebsiteConnectionOperation


class CmsGoalSettlementTests(SimpleTestCase):
    def setUp(self):
        scope = binding()
        self.now = datetime(2026, 10, 17, 5, 30, tzinfo=timezone.utc)
        self.key = "cms-articles-first:" + "d" * 64
        self.goal_id = str(uuid4())
        envelope = make_envelope(channel="cms", platform=scope["platform"], artifact_operation_id=str(uuid4()),
            company_id=scope["company_id"], organization_id=scope["organization_id"], owner_user_id=scope["owner_user_id"],
            connection_id=scope["connection_id"], connection_generation=scope["generation"], public_origin=scope["public_origin"],
            scope_sha256=digest(scope), billing_request_id=self.key, review_policy="auto", policy_sha256="a" * 64)
        request = {"domain": "fixture.test", "client_request_id": self.key, "publishing_channel_artifact": envelope,
            "cms_goal_operation_id": self.goal_id, "cms_goal_id": self.goal_id,
            "roo_points_authorized": True, "roo_points_action": "article_generation", "roo_points_cost": 6,
            "roo_points_billing_status": "charged", "roo_points_billing_user_id": 5, "roo_points_ledger_id": "11",
            "roo_points_requested_by_user_id": scope["owner_user_id"]}
        self.outbox = SimpleNamespace(client_request_id=self.key, organization_id=scope["organization_id"], payload={"request": request})
        self.subject = {"basis": "private_native_handoff", "evidence_digest": "e" * 64}

    def record(self, *, decision="retained", subject=None):
        value = settlement.seal(self.outbox, decision=decision, subject=self.subject if subject is None and decision == "retained" else subject, at=self.now)
        self.outbox.payload[settlement.REFERENCE] = value
        return value

    def test_closed_private_settlement_binds_original_dispatch_payer_amount_and_generated_work(self):
        value = self.record()
        self.assertEqual(settlement.validate(self.outbox), value)
        self.assertEqual((value["payer_user_id"], value["spend_ledger_id"], value["cost_points"]), ("5", "11", 6))
        self.assertEqual(value["retention_subject"], self.subject)
        self.assertNotIn("api_origin", value)
        self.assertNotIn("html", value)
        self.assertNotIn("credential", value)

    def test_source_ledger_payer_price_or_goal_changes_cannot_reuse_the_same_private_stamp(self):
        self.record()
        original = deepcopy(self.outbox.payload["request"])
        for changes in ({"roo_points_cost": 7}, {"roo_points_ledger_id": "12"}, {"roo_points_billing_user_id": 9},
                {"cms_goal_operation_id": str(uuid4())}, {"domain": "foreign.test"}, {"roo_points_authorized": 1},
                {"roo_points_billing_status": "free"}, {"roo_points_cost": True}):
            self.outbox.payload["request"] = {**deepcopy(original), **changes}
            with self.subTest(changes=changes), self.assertRaises(CmsGoalError): settlement.validate(self.outbox)

    def test_rechecksummed_boolean_or_unrelated_financial_metadata_is_not_valid_source_binding(self):
        original = self.record()
        for changes in ({"version": True}, {"cost_points": True}, {"spend_ledger_id": "12"},
                {"payer_user_id": "9"}, {"goal_operation_id": str(uuid4())}, {"approved": True}):
            value = {**deepcopy(original), **changes}
            value["digest"] = digest({key: item for key, item in value.items() if key != "digest"})
            self.outbox.payload[settlement.REFERENCE] = value
            with self.subTest(changes=changes), self.assertRaises(CmsGoalError): settlement.validate(self.outbox)

    def test_an_explicit_original_free_dispatch_can_be_settled_without_inventing_a_paid_ledger(self):
        self.outbox.payload["request"].update(roo_points_cost=0, roo_points_billing_status="free")
        self.outbox.payload["request"].pop("roo_points_ledger_id")
        value = self.record(decision="refunded")
        self.assertIsNone(value["spend_ledger_id"])
        self.assertEqual(settlement.validate(self.outbox)["cost_points"], 0)
        self.outbox.payload["request"]["roo_points_ledger_id"] = "11"
        with self.assertRaises(CmsGoalError): settlement.validate(self.outbox)

    def test_retention_requires_typed_original_work_reference_and_a_refund_has_no_retention_claim(self):
        for decision, subject in (("retained", True), ("refunded", self.subject), ("approved", None),
                ("retained", {"basis": "worker_approved", "evidence_digest": "e" * 64})):
            with self.subTest(decision=decision), self.assertRaises(CmsGoalError):
                settlement.seal(self.outbox, decision=decision, subject=subject, at=self.now)

    def test_private_retained_audit_blocks_late_worker_refund_without_the_erased_goal_or_key(self):
        self.record()
        with patch.object(ContentFactoryDispatchOutbox.objects, "filter") as dispatch, \
                patch.object(WebsiteConnectionOperation.objects, "filter") as private:
            dispatch.return_value.first.return_value = self.outbox
            self.assertTrue(billing.defer_child_refund({"client_request_id": self.key}, resolved_domain="fixture.test"))
            self.assertFalse(billing.defer_child_refund({"client_request_id": self.key}, resolved_domain="foreign.test"))
        private.assert_not_called()

    def test_recorded_original_refund_does_not_authorize_another_retention_or_prevent_idempotent_credit(self):
        self.record(decision="refunded")
        with patch.object(ContentFactoryDispatchOutbox.objects, "filter") as dispatch:
            dispatch.return_value.first.return_value = self.outbox
            self.assertFalse(billing.defer_child_refund({"client_request_id": self.key}, resolved_domain="fixture.test"))

    def test_ordinary_or_missing_dispatch_cannot_create_native_financial_authority(self):
        self.outbox.payload["request"]["client_request_id"] = "ordinary-article"
        with self.assertRaises(CmsGoalError): self.record()
        self.outbox.payload = {"request": {}}
        with self.assertRaises(CmsGoalError): settlement.validate(self.outbox)
