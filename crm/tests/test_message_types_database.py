"""PostgreSQL-backed checks for invites, automatic reminders and Ask Roo to draft."""

from datetime import timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch

from django.test import TestCase, override_settings

from crm import blast_email, reminders
from crm.blasts import resolve_audience
from crm.models import Blast, BlastDelivery, Contact

from .api_fixtures import PREFIX, make_contact, register
from .test_blasts_database import SENDING, BlastFixtures

EVERYONE = {"match": "all", "rules": [], "exclude": []}


@override_settings(**SENDING)
class InviteAudienceDatabaseTests(BlastFixtures, TestCase):
    def setUp(self):
        super().setUp()
        Contact.objects.update(marketing_consent=Contact.MarketingConsent.GRANTED)
        self.gus = make_contact("gus@example.com", first_name="Gus", marketing_consent="granted")
        self.hal = make_contact("hal@example.com", first_name="Hal", marketing_consent="granted")

    def invite(self, **values):
        return Blast.objects.create(purpose="marketing", message_type="invite", event=self.event,
                                    segment_definition=EVERYONE, channels=["email"], mailbox=self.mailbox, **values)

    def emails(self, blast):
        audience = resolve_audience(blast, now=self.now)
        return set(audience.querysets["email"].values_list("email", flat=True)), audience.excluded["email"]

    def test_an_invite_skips_everyone_on_the_guest_list_and_anyone_already_invited(self):
        earlier = self.invite(status=Blast.Status.SENT)
        BlastDelivery.objects.create(blast=earlier, contact=self.hal, channel="email", address=self.hal.email,
                                     status=BlastDelivery.Status.SENT)
        emails, excluded = self.emails(self.invite())
        # Ada, Bob, Dan and Eve are going, Fay is waitlisted, Cat unsubscribed, Hal was invited.
        self.assertEqual(emails, {"gus@example.com"})
        self.assertEqual(excluded["already_registered"], 5)
        self.assertEqual(excluded["already_invited"], 1)

    def test_a_cancelled_registration_can_be_invited_again(self):
        registration = register(self.event, self.gus)
        self.assertNotIn("gus@example.com", self.emails(self.invite())[0])
        registration.removed_at = self.now
        registration.save(update_fields=["removed_at"])
        self.assertIn("gus@example.com", self.emails(self.invite())[0])

    def test_someone_who_registers_mid_send_is_dropped_before_listmonk(self):
        blast = self.invite()
        self.assertIsNone(blast_email._drop_reason(blast, self.gus, False))
        register(self.event, self.gus)
        self.assertEqual(blast_email._drop_reason(blast, self.gus, False), "already_registered")

    def test_the_api_refuses_an_invite_aimed_at_the_guest_list(self):
        response = self.client.post(PREFIX + "blasts/", self.draft_body(
            purpose="marketing", message_type="invite", channels=["email"], sms={}, whatsapp={}), format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"], "invite_cannot_target_guests")

    def test_an_invite_to_everyone_opted_in_keeps_its_event(self):
        response = self.client.post(PREFIX + "blasts/", {
            "purpose": "marketing", "message_type": "invite", "event_id": str(self.event.pk),
            "audience_preset": "opted_in", "channels": ["email"]}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        blast = Blast.objects.get(pk=response.json()["id"])
        self.assertEqual((blast.event_id, blast.purpose), (self.event.pk, "marketing"))
        # The guest list is still left out of the opted-in audience.
        self.assertEqual(self.emails(blast)[1]["already_registered"], 5)

    def test_the_api_saves_and_returns_the_message_type(self):
        response = self.client.post(PREFIX + "blasts/", self.draft_body(
            message_type="blast", channels=["email"], sms={}, whatsapp={}), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["message_type"], "blast")


@override_settings(**SENDING, CRM_AUTO_REMINDERS_ENABLED=True)
class ReminderDatabaseTests(BlastFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.event.approved_count = 5
        self.event.raw_payload = {"platform": "luma", "access": "manage"}
        # An evening start a week out, so both reminders fall inside sending
        # hours whatever time of day the suite runs.
        zone = ZoneInfo(self.event.timezone or "Australia/Melbourne")
        local = (self.now + timedelta(days=7)).astimezone(zone)
        self.event.start_at = local.replace(hour=18, minute=30, second=0, microsecond=0)
        self.event.save()

    def reminders_for(self):
        return {row.reminder_offset: row for row in Blast.objects.filter(
            event=self.event, message_type="reminder").exclude(status="cancelled")}

    def test_each_event_gets_two_reminder_drafts_once(self):
        reminders.ensure_reminders(self.event, now=self.now, actor=None)
        reminders.ensure_reminders(self.event, now=self.now, actor=None)
        rows = self.reminders_for()
        self.assertEqual(set(rows), {"day_before", "hour_before"})
        self.assertEqual(Blast.objects.filter(event=self.event, message_type="reminder").count(), 2)
        hour = rows["hour_before"]
        self.assertEqual((hour.purpose, hour.quick_segment, hour.channels), ("event_update", "going", ["email"]))
        self.assertTrue(hour.subject.endswith("starts in 1 hour"))
        self.assertLess(hour.scheduled_for, self.event.start_at)

    def test_moving_the_event_moves_its_reminders(self):
        reminders.ensure_reminders(self.event, now=self.now, actor=None)
        before = self.reminders_for()["day_before"].scheduled_for
        self.event.start_at += timedelta(days=1)
        self.event.save()
        reminders.ensure_reminders(self.event, now=self.now, actor=None)
        self.assertEqual(self.reminders_for()["day_before"].scheduled_for, before + timedelta(days=1))

    def test_switching_a_reminder_off_cancels_it(self):
        reminders.ensure_reminders(self.event, now=self.now, actor=None)
        response = self.client.patch(PREFIX + f"events/{self.event.pk}/reminders/", {"day_before": False}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertFalse(body["reminders"][0]["on"])
        self.assertEqual(set(self.reminders_for()), {"hour_before"})

    def test_a_reminder_never_sends_after_the_event_starts(self):
        reminders.ensure_reminders(self.event, now=self.now, actor=None)
        later = self.event.start_at + timedelta(minutes=1)
        with patch("crm.reminders.system_actor", return_value=self.admin), \
                patch("crm.reminders.refresh_event_guests"), patch("crm.reminders._slack"):
            reminders.run_reminder_scheduler(now=later)
        self.assertEqual(self.reminders_for(), {})
        self.assertFalse(BlastDelivery.objects.filter(blast__message_type="reminder").exists())

    def test_staff_see_both_reminders_on_the_event(self):
        body = self.client.get(PREFIX + f"events/{self.event.pk}/reminders/").json()
        self.assertTrue(body["enabled"])
        self.assertEqual([row["offset"] for row in body["reminders"]], ["day_before", "hour_before"])

    def test_at_its_time_a_reminder_queues_to_guests_who_are_going(self):
        reminders.ensure_reminders(self.event, now=self.now, actor=self.admin)
        hour = self.reminders_for()["hour_before"]
        hour.mailbox = self.mailbox
        hour.save(update_fields=["mailbox"])
        slack = []
        with patch("crm.reminders.system_actor", return_value=self.admin), \
                patch("crm.reminders.refresh_event_guests") as refresh, \
                patch("crm.reminders._slack", side_effect=slack.append):
            result = reminders.run_reminder_scheduler(now=hour.scheduled_for)
        hour.refresh_from_db()
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(hour.status, Blast.Status.QUEUED, (hour.last_error, hour.stats))
        self.assertEqual(hour.recipient_count, 4)  # Five going, one of them unsubscribed.
        refresh.assert_called_with(self.event)
        self.assertIn("goes to 4 guests", slack[0])
