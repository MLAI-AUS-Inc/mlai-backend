from datetime import datetime, timezone
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from crm.luma_guests import event_record, guest_record, one_guest_per_email
from crm.luma_sync import PacedSession, run_luma_guest_sync_scheduler
from integrations.services.luma import LumaAPIError, LumaAttendeeReportService


def guest(**overrides):
    payload = {
        "id": "gst-1",
        "user_email": " Ada@Example.COM ",
        "user_name": "Ada Lovelace",
        "user_first_name": "Ada",
        "user_last_name": "Lovelace",
        "phone_number": "+61 400 000 000",
        "approval_status": "approved",
        "registered_at": "2026-09-01T10:00:00Z",
        "event_tickets": [
            {"name": "General", "checked_in_at": "2026-09-10T08:05:00Z"},
            {"name": "Workshop", "checked_in_at": "2026-09-10T08:01:00Z"},
        ],
        "registration_answers": [
            {
                "label": "Where do you work?",
                "question_type": "company",
                "answer_company": "Analytical Engines",
                "answer_job_title": "Programmer",
            },
            {"label": "Keep me updated about MLAI events", "answer": "Yes"},
        ],
        "utm_source": "newsletter",
    }
    payload.update(overrides)
    return {"guest": payload}


class GuestRecordTests(SimpleTestCase):
    def test_normalises_email_and_reads_profile_check_in_and_answers(self):
        record = guest_record(guest())

        self.assertEqual(record.email, "ada@example.com")
        self.assertEqual(record.display_name, "Ada Lovelace")
        self.assertEqual(record.company, "Analytical Engines")
        self.assertEqual(record.job_title, "Programmer")
        self.assertEqual(record.ticket_names, ("General", "Workshop"))
        self.assertEqual(record.checked_in_at, datetime(2026, 9, 10, 8, 1, tzinfo=timezone.utc))
        self.assertEqual(record.registration_answers[0]["value"], "Analytical Engines - Programmer")
        self.assertFalse(record.consent_granted)

    def test_consent_needs_a_configured_label_and_a_yes(self):
        labels = ["keep me  updated about MLAI events"]
        self.assertTrue(guest_record(guest(), consent_labels=labels).consent_granted)

        no = guest(registration_answers=[{"label": "Keep me updated about MLAI events", "answer": "No"}])
        self.assertFalse(guest_record(no, consent_labels=labels).consent_granted)

    def test_guest_without_a_usable_email_is_dropped(self):
        self.assertIsNone(guest_record(guest(user_email="not-an-email")))
        self.assertIsNone(guest_record(guest(user_email=None)))
        self.assertIsNone(guest_record("garbage"))

    def test_flat_guest_payload_is_accepted(self):
        record = guest_record(guest()["guest"])
        self.assertEqual(record.luma_guest_id, "gst-1")

    def test_one_guest_per_email_prefers_checked_in_then_approved_then_earliest(self):
        waitlisted = guest_record(guest(id="gst-a", approval_status="waitlist", event_tickets=[]))
        approved_late = guest_record(
            guest(id="gst-b", event_tickets=[], registered_at="2026-09-05T00:00:00Z")
        )
        approved_early = guest_record(
            guest(id="gst-c", event_tickets=[], registered_at="2026-09-02T00:00:00Z")
        )
        checked_in = guest_record(guest(id="gst-d", approval_status="waitlist"))

        chosen = one_guest_per_email([waitlisted, approved_late, approved_early])
        self.assertEqual([record.luma_guest_id for record in chosen], ["gst-c"])
        chosen = one_guest_per_email([approved_early, checked_in])
        self.assertEqual([record.luma_guest_id for record in chosen], ["gst-d"])


class EventRecordTests(SimpleTestCase):
    def test_managed_event_is_kept_and_external_listing_is_skipped(self):
        managed = event_record(
            {
                "id": "evt-1",
                "name": "MLAI Meetup",
                "url": "https://luma.com/abc",
                "cover_url": "javascript:alert(1)",
                "start_at": "2026-10-01T08:00:00Z",
                "access": "manage",
                "platform": "luma",
            }
        )
        self.assertEqual(managed.luma_event_id, "evt-1")
        self.assertEqual(managed.cover_url, "")
        self.assertEqual(managed.start_at, datetime(2026, 10, 1, 8, tzinfo=timezone.utc))
        self.assertIsNone(event_record({"id": "evt-2", "platform": "external"}))
        self.assertIsNone(event_record({"id": "evt-3", "access": "view"}))
        self.assertIsNone(event_record({"name": "no id"}))


class LumaGuestRequestTests(SimpleTestCase):
    def test_all_statuses_omits_the_approval_filter(self):
        calls = []

        class Session:
            def get(self, url, headers, params, timeout):
                calls.append(params)

                class Response:
                    status_code = 200

                    def raise_for_status(self):
                        return None

                    def json(self):
                        return {"entries": [], "has_more": False}

                return Response()

        service = LumaAttendeeReportService(api_key="key", session=Session())
        service.list_guests(event_id="evt-1", approval_status=None)
        service.list_guests(event_id="evt-1")

        self.assertIsNone(calls[0]["approval_status"])
        self.assertEqual(calls[1]["approval_status"], "approved")


class PacedSessionTests(SimpleTestCase):
    def test_requests_are_spaced_to_the_per_minute_budget(self):
        clock = [0.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(round(seconds, 3))
            clock[0] += seconds

        session = PacedSession(120, clock=lambda: clock[0], sleep=sleep)
        with patch("requests.Session.request", return_value="ok"):
            session.request("GET", "https://example.test")
            session.request("GET", "https://example.test")
            clock[0] += 2
            session.request("GET", "https://example.test")

        self.assertEqual(sleeps, [0.5])


class RunnerGateTests(SimpleTestCase):
    @override_settings(CRM_LUMA_SYNC_ENABLED=False)
    def test_disabled_runner_does_nothing(self):
        with patch("crm.luma_sync.cache") as cache:
            self.assertEqual(run_luma_guest_sync_scheduler(), {"status": "skipped", "reason": "disabled"})
        cache.add.assert_not_called()

    @override_settings(CRM_LUMA_SYNC_ENABLED=True, LUMA_API_KEY="")
    def test_missing_key_is_skipped_not_failed(self):
        self.assertEqual(run_luma_guest_sync_scheduler()["reason"], "luma_not_configured")

    @override_settings(CRM_LUMA_SYNC_ENABLED=True, LUMA_API_KEY="key")
    def test_throttle_skips_until_the_interval_passes(self):
        with patch("crm.luma_sync.cache") as cache, patch("crm.luma_sync.sync_recent_luma_guests") as sync:
            cache.add.return_value = False
            self.assertEqual(run_luma_guest_sync_scheduler()["reason"], "throttled")
        sync.assert_not_called()

    @override_settings(CRM_LUMA_SYNC_ENABLED=True, LUMA_API_KEY="key")
    def test_outage_defers_and_rejected_key_fails(self):
        with patch("crm.luma_sync.cache") as cache, patch("crm.luma_sync.sync_recent_luma_guests") as sync:
            cache.add.return_value = True
            sync.side_effect = LumaAPIError("down", status_code=503)
            self.assertEqual(run_luma_guest_sync_scheduler()["status"], "deferred")
            sync.side_effect = LumaAPIError("no", status_code=401)
            self.assertEqual(run_luma_guest_sync_scheduler()["status"], "failed")

    @override_settings(CRM_LUMA_SYNC_ENABLED=True, LUMA_API_KEY="key")
    def test_failed_events_report_partial(self):
        summary = {"failed_events": ["evt-1"], "rate_limited": False, "events_synced": 3}
        with patch("crm.luma_sync.cache") as cache, patch(
            "crm.luma_sync.sync_recent_luma_guests", return_value=summary
        ):
            cache.add.return_value = True
            result = run_luma_guest_sync_scheduler()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["events_synced"], 3)
