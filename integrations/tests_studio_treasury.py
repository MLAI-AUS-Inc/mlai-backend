"""Database regressions. Run only after approval of the schema test migration."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock, patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone
from organizations.models import Organization
from integrations.models import StudioTreasuryLedger, StudioPaymentReview, StudioMeetingOccurrence
from integrations.services.studio_treasury import perform
from integrations.services.studio_payment_rules import ReviewError
from integrations.test_studio_payment_rules import claim, work, fund
from integrations.services.studio_meetings import enrich, confirm, pending_times


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


@skipUnlessDBFeature("has_select_for_update")
class TreasuryConcurrencyTests(TransactionTestCase):
    def test_two_builders_cannot_reserve_the_last_customer_hour_together(self):
        ledger = StudioTreasuryLedger.objects.create(
            organization=Organization.objects.create(name="Concurrency Studio", domain="race.example.invalid"),
            workspace_id="T123", configuration={"treasurers": ["USAM"]},
            opening_verified_at=timezone.now(), opening_evidence="Reviewed opening")
        claims = {"first": claim(number="first"), "second": claim(builder="second", number="second")}
        works = [work(), work(builder="second", key="work-2")]

        class Sources:
            def snapshot(self, selector):
                return {"claims": [claims[selector]], "work": works, "funding": [fund(1)], "exceptions": []}

        factory = lambda scope: Sources()
        reviews = [perform("T123", "USAM", "preview", request_key=key*64, selector=selector, source_factory=factory)
                   for key, selector in (("a", "first"), ("b", "second"))]
        self.assertTrue(all(row["ready_cents"] == 6500 for row in reviews))
        barrier = Barrier(2)

        def authorize(review_id):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return perform("T123", "USAM", "authorize", request_key="c"*64,
                               review_id=review_id, source_factory=factory)["status"]
            except ReviewError as exc:
                return str(exc)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(authorize, [row["id"] for row in reviews]))
        self.assertCountEqual(outcomes, ["reserved", "coverage_changed_request_new_review"])
        ledger.refresh_from_db()
        self.assertEqual(len(ledger.reservations), 1)
        allocations = next(iter(ledger.reservations.values()))["allocations"]
        self.assertEqual(sum(row["units"] for row in allocations), 3600)


class MeetingDatabaseTests(TestCase):
    def setUp(self):
        self.ledger = StudioTreasuryLedger.objects.create(
            organization=Organization.objects.create(name="Meeting Studio", domain="meeting.example.invalid"),
            workspace_id="T123", configuration={"members": ["UONE", "UTWO"], "treasurers": ["USAM"],
                "builders": {"one": {"slack_id": "UONE", "senders": ["one@example.invalid"]},
                             "two": {"slack_id": "UTWO", "senders": ["two@example.invalid"]}},
                "projects": {"project": {"team_id": "team", "calendars": [
                    {"calendar_id": "one", "title_patterns": ["Studio check-in"]},
                    {"calendar_id": "two", "title_patterns": ["Studio check-in"]}]}}})
        self.start = timezone.now() - timedelta(hours=1)
        self.event = {"id": "event", "iCalUID": "shared-uid", "summary": "Studio check-in", "status": "confirmed",
            "start": {"dateTime": self.start.isoformat()}, "end": {"dateTime": (self.start+timedelta(minutes=30)).isoformat()},
            "attendees": [{"email": "one@example.invalid"}, {"email": "two@example.invalid"}]}
        self.source = Mock()
        self.source.issues.return_value = []

    def scan(self):
        return enrich("T123", "UONE", ["project"], mode="create", source_factory=lambda scope: self.source,
                      event_reader=lambda binding, start, end: [deepcopy(self.event)])

    @patch("integrations.services.studio_meetings.create_linear_meeting_issue")
    def test_calendar_copies_and_reruns_reuse_one_occurrence_and_issue(self, create):
        create.return_value = {"id": "issue", "identifier": "STU-900", "title": "Meeting"}
        first, second = self.scan(), self.scan()
        create.assert_called_once()
        self.assertEqual(first[0]["key"], second[0]["key"])
        self.assertEqual(StudioMeetingOccurrence.objects.count(), 1)
        row = StudioMeetingOccurrence.objects.get()
        self.assertEqual(row.participation, {})
        self.assertEqual(pending_times("T123", "UONE")[0]["minutes"], 30)

    @patch("integrations.services.studio_meetings._create_linear_channel_issue_comment")
    @patch("integrations.services.studio_meetings.create_linear_meeting_issue")
    def test_each_person_confirms_and_only_treasurer_approves_actual_time(self, create, comment):
        create.return_value = {"id": "issue", "identifier": "STU-900", "title": "Meeting"}
        comment.return_value = {"comment": {"id": "comment"}}
        key = self.scan()[0]["key"]
        recorded = confirm("T123", "UONE", key, 20)
        self.assertFalse(recorded["approved"])
        self.assertEqual(confirm("T123", "UONE", key, 20), recorded)
        comment.assert_called_once()
        with self.assertRaises(PermissionError):
            confirm("T123", "UONE", key, 20, builder="two", approved=True)
        self.assertTrue(confirm("T123", "USAM", key, 20, builder="one", approved=True)["approved"])
        self.assertEqual(StudioMeetingOccurrence.objects.get().participation.keys(), {"one"})
        with self.assertRaises(ReviewError):
            confirm("T123", "USAM", key, 30, builder="one", approved=True)

    @patch("integrations.services.studio_meetings._create_linear_channel_issue_comment", side_effect=TimeoutError)
    @patch("integrations.services.studio_meetings.create_linear_meeting_issue")
    def test_uncertain_linear_note_keeps_time_unapproved_without_resending(self, create, comment):
        create.return_value = {"id": "issue", "identifier": "STU-900", "title": "Meeting"}
        key = self.scan()[0]["key"]
        with self.assertRaises(TimeoutError):
            confirm("T123", "UONE", key, 20)
        with self.assertRaisesMessage(ReviewError, "note_needs_review"):
            confirm("T123", "UONE", key, 20)
        comment.assert_called_once()
        self.assertFalse(StudioMeetingOccurrence.objects.get().participation["one"]["approved"])
