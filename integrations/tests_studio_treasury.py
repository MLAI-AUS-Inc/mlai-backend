"""Database regressions. Run only after approval of the schema test migration."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock

from django.test import TestCase
from django.utils import timezone
from organizations.models import Organization
from integrations.models import StudioTreasuryLedger, StudioPaymentReview
from integrations.services.studio_treasury import perform
from integrations.services.studio_payment_rules import ReviewError
from integrations.test_studio_payment_rules import claim, work, fund


class TreasuryDatabaseTests(TestCase):
    def setUp(self):
        self.ledger = StudioTreasuryLedger.objects.create(
            organization=Organization.objects.create(name="Synthetic Studio", domain="studio.example.invalid"),
            workspace_id="T123", configuration={"treasurers": ["USAM"]},
            opening_verified_at=timezone.now(), opening_evidence="Reviewed synthetic opening balance")
        self.snapshot = {"claims": [claim()], "work": [work()], "funding": [fund(1)], "exceptions": []}
        self.sources = Mock()
        self.sources.snapshot.side_effect = lambda selector: deepcopy(self.snapshot)
        self.sources.bank_receipt.return_value = {"id": "a"*8 + "-" + "a"*4 + "-" + "a"*4 + "-" + "a"*4 + "-" + "a"*12,
                                                 "sha256": "verified-receipt"}
        self.factory = lambda scope: self.sources

    def call(self, operation, key="a"*64, **kwargs):
        return perform("T123", "USAM", operation, request_key=key, source_factory=self.factory, **kwargs)

    def test_preview_does_not_reserve_then_authorization_and_receipt_replay_once(self):
        preview = self.call("preview")
        self.ledger.refresh_from_db()
        self.assertEqual(self.ledger.reservations, {})
        handoff = self.call("authorize", review_id=preview["id"])
        self.assertEqual(self.call("authorize", review_id=preview["id"]), handoff)
        self.ledger.refresh_from_db()
        self.assertEqual(len(self.ledger.reservations), 1)
        kwargs = {"review_id": preview["id"], "invoice": "48352", "receipt_id": self.sources.bank_receipt.return_value["id"]}
        paid = self.call("receipt", **kwargs)
        self.assertEqual(paid["status"], "paid")
        self.assertEqual(self.call("receipt", **kwargs), paid)
        later = self.call("preview", key="b"*64)
        self.assertEqual(later["invoices"][0]["status"], "already_paid")

    def test_two_independent_previews_cannot_authorize_the_same_final_unit(self):
        first = self.call("preview")
        second = self.call("preview", key="b"*64)
        self.call("authorize", review_id=first["id"])
        with self.assertRaisesMessage(ReviewError, "coverage_changed"):
            self.call("authorize", review_id=second["id"])

    def test_unknown_bank_outcome_keeps_work_and_customer_funding_reserved(self):
        first = self.call("preview")
        self.call("authorize", review_id=first["id"])
        self.call("unknown", review_id=first["id"], invoice="48352")
        later = self.call("preview", key="b"*64)
        self.assertEqual(later["ready_cents"], 0)
        self.ledger.refresh_from_db()
        self.assertEqual(next(iter(self.ledger.reservations.values()))["status"], "unknown")

    def test_expiry_source_change_and_treasurer_permissions(self):
        first = self.call("preview")
        self.snapshot["funding"][0]["units"] //= 2
        with self.assertRaisesMessage(ReviewError, "sources_changed"):
            self.call("authorize", review_id=first["id"])
        StudioPaymentReview.objects.filter(pk=first["id"]).update(expires_at=timezone.now()-timedelta(seconds=1))
        with self.assertRaisesMessage(ReviewError, "expired"):
            self.call("authorize", review_id=first["id"])
        with self.assertRaises(PermissionError):
            perform("T123", "UOTHER", "preview", request_key="c"*64, source_factory=self.factory)

    def test_unparsed_revision_prevents_old_invoice_becoming_ready(self):
        self.snapshot["exceptions"] = [{"builder": "builder", "source": "mail", "code": "unparsed_revision"}]
        preview = self.call("preview")
        self.assertEqual(preview["ready_cents"], 0)
        with self.assertRaises(ReviewError):
            self.call("authorize", review_id=preview["id"])
