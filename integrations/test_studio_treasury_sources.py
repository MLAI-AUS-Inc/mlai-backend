"""Provider adapters under synthetic inputs. SimpleTestCase never uses a DB."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from integrations.api_views_studio_treasury import StudioTreasuryView
from integrations.services.studio_meetings import occurrence_key, enrich
from integrations.services.studio_treasury_sources import StudioSources


class StudioSourceTests(SimpleTestCase):
    def scope(self, basis="actual"):
        return SimpleNamespace(organization_id=1, configuration={"projects": {"project": {
            "basis": basis, "pool": "shared", "version": "v1", "effective_from": "2026-01-01"}},
            "builders": {"builder": {"slack_id": "UBUILDER"}}})

    def issue(self, body="", labels=None):
        return {"id": "issue", "identifier": "STU-1", "description": body, "updatedAt": "2026-10-12T00:00:00Z",
                "completedAt": "2026-10-12T00:00:00Z", "state": {"type": "completed"},
                "project": {"id": "project"}, "assignee": {"id": "builder"},
                "labels": {"nodes": [{"name": name} for name in labels or []]}}

    @patch("integrations.services.studio_treasury_sources.StudioMeetingOccurrence")
    def test_actual_time_is_independent_of_an_estimate_or_invoice(self, meetings):
        meetings.objects.filter.return_value = []
        source = StudioSources(self.scope())
        self.assertEqual(source.approved_work([self.issue("Time budget: 1 hour")]), [])
        result = source.approved_work([self.issue("Actual time: 20 minutes\nWork date: 12 October 2026")])
        self.assertEqual(result[0]["units"], 1200)
        self.assertEqual(result[0]["key"], "issue")

    @patch("integrations.services.studio_treasury_sources.StudioMeetingOccurrence")
    def test_size_credit_uses_the_selected_agreement_and_done_ticket(self, meetings):
        meetings.objects.filter.return_value = []
        result = StudioSources(self.scope("size")).approved_work([self.issue(labels=["Small (S)"])])
        self.assertEqual(result[0]["units"], 3600)
        self.assertEqual(result[0]["basis"], "size")

    @patch("integrations.services.studio_treasury_sources._list_linear_issue_comments")
    @patch("integrations.services.studio_treasury_sources.StudioMeetingOccurrence")
    def test_cancelled_invite_preserves_approved_participant_and_no_double_generic_work(self, meetings, comments):
        from integrations.services.studio_payment_rules import digest
        comments.return_value = ([{"id": "comment", "body": "approved attendance"}], False)
        meetings.objects.filter.return_value = [SimpleNamespace(
            linear_issue={"id": "issue"}, cancelled=True, project_id="project", occurrence_key="key",
            starts_at=datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc),
            participation={"builder": {"approved": True, "minutes": 20, "comment_pending": False,
                "comment_id": "comment", "note_hash": digest("approved attendance"), "work_date": "2026-10-12"}})]
        result = StudioSources(self.scope()).approved_work([self.issue("Actual time: 20 minutes\nWork date: 12 October 2026")])
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["units"], result[0]["key"]), (1200, "meeting:key:builder"))

    @patch("integrations.services.studio_treasury_sources._list_linear_issue_comments")
    @patch("integrations.services.studio_treasury_sources.StudioMeetingOccurrence")
    def test_multi_date_meeting_ticket_earns_its_size_credit_once(self, meetings, comments):
        from integrations.services.studio_payment_rules import digest
        comments.return_value = ([{"id": "c1", "body": "one"}, {"id": "c2", "body": "two"}], False)
        meetings.objects.filter.return_value = [SimpleNamespace(
            linear_issue={"id": "issue"}, cancelled=False, project_id="project", occurrence_key=key,
            participation={"builder": {"approved": True, "minutes": 20, "comment_pending": False,
                "comment_id": comment, "note_hash": digest(body), "work_date": "2026-10-12"}})
            for key, comment, body in (("meeting1", "c1", "one"), ("meeting2", "c2", "two"))]
        result = StudioSources(self.scope("size")).approved_work([self.issue(labels=["Small (S)"])])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["units"], 3600)
        self.assertEqual(result[0]["key"], "issue")

    def test_calendar_occurrence_identity_collapses_copies_but_keeps_recurrences(self):
        event = {"id": "copy1", "iCalUID": "shared", "recurringEventId": "recurring",
                 "originalStartTime": {"dateTime": "2026-10-11T10:00:00+11:00"}}
        one = occurrence_key("calendar1", event)
        event["id"] = "copy2"
        self.assertEqual(one, occurrence_key("calendar2", event))
        event["originalStartTime"]["dateTime"] = "2026-10-18T10:00:00+11:00"
        self.assertNotEqual(one, occurrence_key("calendar1", event))

    @patch("integrations.services.studio_meetings.StudioMeetingOccurrence")
    @patch("integrations.services.studio_meetings.scope_for")
    def test_preview_collapses_calendar_copies_and_skips_private_bookings(self, scope_for, meetings):
        scope = self.scope()
        scope.configuration["projects"]["project"]["calendars"] = [
            {"calendar_id": "one", "title_patterns": ["Acquire check-in"]},
            {"calendar_id": "two", "title_patterns": ["Acquire check-in"]}]
        scope_for.return_value = scope
        meetings.objects.filter.return_value.first.return_value = None
        source = Mock()
        source.issues.return_value = []
        event = {"id": "e", "iCalUID": "u", "summary": "Acquire check-in", "status": "confirmed",
                 "start": {"dateTime": "2026-11-16T11:00:00+11:00"}, "end": {"dateTime": "2026-11-16T11:30:00+11:00"}}
        reader = lambda *args: [dict(event), dict(event, id="private", iCalUID="private", visibility="private")]
        result = enrich("T123", "USAM", ["project"], now=datetime(2026,11,15,tzinfo=timezone.utc),
                        source_factory=lambda scope: source, event_reader=reader)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["action"], "planned")
        self.assertFalse(result[0]["confirmation_needed"])
        meetings.objects.get_or_create.assert_not_called()

    @override_settings(STUDIO_TREASURY_ENABLED=True, STUDIO_TREASURY_KEY="private-" + "x"*32,
                       ROO_API_KEY="public-key", INTERNAL_API_KEY="internal-key")
    def test_public_key_cannot_review_money_or_supply_model_parameters(self):
        request = APIRequestFactory().post("/", {"workspace": "T1", "actor": "USAM", "operation": "preview"},
                                          format="json", HTTP_X_API_KEY="public-key")
        with patch("integrations.services.studio_treasury.perform") as perform:
            self.assertIn(StudioTreasuryView.as_view()(request).status_code, (401, 403))
            perform.assert_not_called()

    @override_settings(STUDIO_TREASURY_ENABLED=True, STUDIO_TREASURY_KEY="same-" + "x"*32,
                       ROO_API_KEY="same-" + "x"*32)
    def test_shared_public_and_treasury_credentials_are_rejected(self):
        request = APIRequestFactory().post("/", {}, format="json", HTTP_X_STUDIO_TREASURY_KEY="same-" + "x"*32)
        self.assertIn(StudioTreasuryView.as_view()(request).status_code, (401, 403))

    @patch("integrations.services.studio_treasury_sources.all_invoices")
    @patch("integrations.services.studio_treasury_sources.ExternalServiceConnection")
    def test_customer_status_transition_and_gst_never_add_capacity(self, connections, all_invoices):
        scope = self.scope()
        scope.configuration.update(xero_connection_id="conn", customer_contacts=["contact"], funding_lines=[{
            "invoice_id": "inv", "line_id": "line", "currency": "AUD", "item_code": "LABOUR", "net_cents": 1000000,
            "pool": "shared", "basis": "actual", "hours": 80, "start": "2026-10-11", "end": "2026-10-25"}])
        invoice = {"InvoiceID": "inv", "Type": "ACCREC", "Status": "AUTHORISED", "AmountCredited": 0,
                   "CurrencyCode": "AUD", "LineAmountTypes": "Inclusive", "LineItems": [{
                       "LineItemID": "line", "ItemCode": "LABOUR", "LineAmount": 11000, "TaxAmount": 1000}]}
        all_invoices.return_value = [invoice]
        source = StudioSources(scope)
        first = source.funding()
        self.assertEqual(first[0]["cents"], 1000000)
        invoice["Status"] = "PAID"
        self.assertEqual(source.funding(), first)
        invoice["AmountCredited"] = 100
        self.assertEqual(source.funding(), [])
