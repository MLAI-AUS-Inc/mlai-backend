"""PostgreSQL-backed checks for the Luma guest sync and the CSV import."""

import io
from datetime import datetime, timezone

from django.test import TestCase

from core.models import AccountBan, User
from crm.contact_import import apply_import, read_csv
from crm.luma_sync import sync_event_catalog, sync_event_guests
from crm.models import Contact, ContactConsentEvent, ContactImport, CrmEvent, EventRegistration

NOW = datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc)


class FakeLuma:
    def __init__(self, events=(), guests=None):
        self.events = list(events)
        self.guests = guests or {}
        self.guest_calls = []

    def list_all_events(self):
        return self.events

    def list_guests(self, *, event_id, approval_status="approved"):
        self.guest_calls.append((event_id, approval_status))
        return self.guests.get(event_id, [])


def luma_event(event_id="evt-1", **extra):
    return {
        "id": event_id,
        "name": "MLAI Meetup",
        "url": f"https://luma.com/{event_id}",
        "start_at": "2026-10-01T08:00:00Z",
        "end_at": "2026-10-01T10:00:00Z",
        "access": "manage",
        "platform": "luma",
        **extra,
    }


def luma_guest(email, guest_id, **extra):
    return {
        "guest": {
            "id": guest_id,
            "user_email": email,
            "user_first_name": extra.pop("first_name", ""),
            "approval_status": extra.pop("approval_status", "approved"),
            "registered_at": "2026-09-20T00:00:00Z",
            **extra,
        }
    }


class LumaGuestSyncTests(TestCase):
    def setUp(self):
        self.luma = FakeLuma(events=[luma_event(), luma_event("evt-ext", platform="external")])
        [self.event] = sync_event_catalog(self.luma)

    def test_catalog_keeps_only_managed_events(self):
        self.assertEqual(list(CrmEvent.objects.values_list("luma_event_id", flat=True)), ["evt-1"])

    def test_guests_become_contacts_and_registrations(self):
        member = User.objects.create_user("grace@example.com")
        self.luma.guests["evt-1"] = [
            luma_guest("Ada@Example.com", "gst-1", first_name="Ada", checked_in_at="2026-10-01T08:10:00Z"),
            luma_guest("grace@example.com", "gst-2", approval_status="waitlist"),
            luma_guest("broken", "gst-3"),
        ]

        result = sync_event_guests(self.luma, self.event, now=NOW)

        self.assertEqual(self.luma.guest_calls, [("evt-1", None)])
        self.assertEqual(result["guests"], 2)
        self.assertEqual(result["skipped_without_email"], 1)
        self.assertEqual(result["contacts_created"], 2)
        ada = Contact.objects.get(email="ada@example.com")
        self.assertEqual(ada.source, Contact.Source.LUMA)
        self.assertEqual(ada.first_name, "Ada")
        self.assertEqual(ada.events_registered_count, 1)
        self.assertEqual(ada.events_attended_count, 1)
        self.assertEqual(ada.last_attended_at, datetime(2026, 10, 1, 8, 10, tzinfo=timezone.utc))
        grace = Contact.objects.get(email="grace@example.com")
        self.assertEqual(grace.user, member)
        self.assertEqual(grace.events_registered_count, 0)
        self.event.refresh_from_db()
        self.assertEqual(
            (self.event.guest_count, self.event.approved_count, self.event.waitlist_count, self.event.checked_in_count),
            (2, 1, 1, 1),
        )
        self.assertEqual(self.event.guests_synced_at, NOW)

    def test_resync_fills_blanks_only_and_marks_removed_guests(self):
        self.luma.guests["evt-1"] = [
            luma_guest("ada@example.com", "gst-1", first_name="Ada"),
            luma_guest("bob@example.com", "gst-2"),
        ]
        sync_event_guests(self.luma, self.event, now=NOW)
        Contact.objects.filter(email="ada@example.com").update(first_name="Augusta")

        self.luma.guests["evt-1"] = [luma_guest("ada@example.com", "gst-1", first_name="Ada", phone_number="123")]
        result = sync_event_guests(self.luma, self.event, now=NOW)

        ada = Contact.objects.get(email="ada@example.com")
        self.assertEqual((ada.first_name, ada.phone), ("Augusta", "123"))
        self.assertEqual(result["registrations_removed"], 1)
        bob = EventRegistration.objects.get(contact__email="bob@example.com")
        self.assertEqual(bob.removed_at, NOW)
        self.assertEqual(Contact.objects.get(email="bob@example.com").events_registered_count, 0)

    def test_configured_consent_answer_grants_once_and_never_overrides_withdrawal(self):
        answers = [{"label": "Email me about MLAI events", "answer": "Yes"}]
        Contact.objects.create(
            email="withdrawn@example.com",
            source=Contact.Source.MANUAL,
            marketing_consent=Contact.MarketingConsent.WITHDRAWN,
        )
        self.luma.guests["evt-1"] = [
            luma_guest("ada@example.com", "gst-1", registration_answers=answers),
            luma_guest("withdrawn@example.com", "gst-2", registration_answers=answers),
        ]

        sync_event_guests(self.luma, self.event, now=NOW, consent_labels=["Email me about MLAI events"])
        sync_event_guests(self.luma, self.event, now=NOW, consent_labels=["Email me about MLAI events"])

        ada = Contact.objects.get(email="ada@example.com")
        self.assertEqual(ada.marketing_consent, Contact.MarketingConsent.GRANTED)
        self.assertEqual(ada.marketing_consent_source, "luma:evt-1")
        self.assertEqual(ContactConsentEvent.objects.filter(contact=ada).count(), 1)
        withdrawn = Contact.objects.get(email="withdrawn@example.com")
        self.assertEqual(withdrawn.marketing_consent, Contact.MarketingConsent.WITHDRAWN)


class ContactImportTests(TestCase):
    CSV = (
        "Email,First name,Company,Tags,Opt-in date\n"
        "ada@example.com,Ada,Engines,founder,2025-03-04\n"
        "new@example.com,Newt,,,\n"
        "gone@example.com,Gone,,,\n"
        "banned@example.com,Banned,,,\n"
    )

    def setUp(self):
        Contact.objects.create(email="ada@example.com", source=Contact.Source.LUMA, first_name="Augusta")
        Contact.objects.create(
            email="gone@example.com",
            source=Contact.Source.LUMA,
            email_status=Contact.EmailStatus.UNSUBSCRIBED,
        )
        banned_user = User.objects.create_user("banned@example.com")
        AccountBan.objects.create(user=banned_user, email="banned@example.com")

    def run_import(self, **options):
        _, parsed = read_csv(io.StringIO(self.CSV))
        return apply_import(
            parsed,
            file_name="optins.csv",
            consent=options.pop("consent", ContactImport.Consent.GRANTED),
            tags=["imported"],
            now=datetime(2026, 10, 23, tzinfo=timezone.utc),
            **options,
        )

    def test_import_creates_fills_and_suppresses(self):
        record = self.run_import()

        self.assertEqual(record.status, ContactImport.Status.COMPLETED)
        self.assertEqual(record.counts["created"], 1)
        self.assertEqual(record.counts["updated"], 1)
        self.assertEqual(record.counts["suppressed"], 2)
        self.assertEqual(record.counts["consent_granted"], 2)
        ada = Contact.objects.get(email="ada@example.com")
        self.assertEqual((ada.first_name, ada.company), ("Augusta", "Engines"))
        self.assertEqual(ada.tags, ["founder", "imported"])
        self.assertEqual(ada.marketing_consent_source, "csv:optins.csv")
        self.assertEqual(ada.marketing_consent_at, datetime(2025, 3, 4, tzinfo=timezone.utc))
        self.assertEqual(Contact.objects.get(email="new@example.com").source, Contact.Source.IMPORT)
        gone = Contact.objects.get(email="gone@example.com")
        self.assertEqual((gone.first_name, gone.marketing_consent), ("", Contact.MarketingConsent.UNKNOWN))
        self.assertFalse(Contact.objects.filter(email="banned@example.com").exists())

    def test_rerunning_the_same_file_changes_nothing(self):
        self.run_import()
        record = self.run_import()

        self.assertEqual(record.counts.get("created", 0), 0)
        self.assertEqual(record.counts.get("updated", 0), 0)
        self.assertEqual(record.counts["unchanged"], 2)
        self.assertEqual(ContactConsentEvent.objects.count(), 2)

    def test_dry_run_rolls_back_but_keeps_the_report(self):
        record = self.run_import(dry_run=True)

        self.assertTrue(record.dry_run)
        self.assertEqual(record.counts["created"], 1)
        self.assertFalse(Contact.objects.filter(email="new@example.com").exists())
        self.assertEqual(Contact.objects.get(email="ada@example.com").company, "")

    def test_no_consent_import_records_people_without_permission(self):
        self.run_import(consent=ContactImport.Consent.NONE)

        new = Contact.objects.get(email="new@example.com")
        self.assertEqual(new.marketing_consent, Contact.MarketingConsent.UNKNOWN)
        self.assertFalse(ContactConsentEvent.objects.exists())
