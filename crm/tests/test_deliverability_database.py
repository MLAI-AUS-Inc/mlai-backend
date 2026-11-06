"""Synthetic schema-backed acceptance tests; migration approval required before execution."""

import hashlib
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from community_chat.crm.services import save_blast_draft
from crm.blast_email import resume_campaign
from crm.blasts import BlastError, queue_blast
from crm.deliverability.guardian import acknowledge, blast_metrics, detect, enforce, evaluate, run_deliverability_guard
from crm.deliverability.streams import sync_streams
from crm.models import Blast, BlastDelivery, Contact, ContactConsentEvent, CrmAuditEvent, DeliverabilityIncident, EmailEvent, SenderMailbox, SendingStream
from crm.resend_feedback import record_event

from .api_fixtures import PREFIX, CrmApiMixin
from .test_blasts_database import SENDING


@override_settings(**{**SENDING, "CRM_GUARDIAN_ENABLED": True,
    "CRM_DELIVERABILITY_OWNERS": ("admin@example.test",), "CRM_DELIVERABILITY_SLACK_CHANNEL": "",
    "CRM_RESEND_MONITORED_SENDERS": (), "CRM_SENDING_DOMAINS": ["news.mlai.au:resend"]})
class DeliverabilityDatabaseTests(CrmApiMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        # All provider effects in these tests must be faked explicitly.
        self.enterContext(patch("integrations.http_client.request", side_effect=AssertionError("Network forbidden")))
        self.enterContext(patch("crm.sending_domains.is_verified", return_value=True))
        sync_streams()
        self.news = SendingStream.objects.get(key="news")
        self.notify = SendingStream.objects.get(key="notify")
        self.news.mailbox.enabled = True
        self.news.mailbox.save()

    def blast(self, **fields):
        return Blast.objects.create(stream=self.news, mailbox=self.news.mailbox, created_by=self.admin,
            purpose="marketing", channels=["email"], subject="Community news", body_markdown="MLAI community news",
            **{"status": "sending", **fields})

    def audience(self, blast, count):
        contacts = Contact.objects.bulk_create([Contact(email=f"person-{index}@example.net", source="manual",
            marketing_consent="granted", marketing_consent_at=self.now) for index in range(count)])
        BlastDelivery.objects.bulk_create([BlastDelivery(blast=blast, contact=contact, address=contact.email,
            channel="email", consent_basis="explicit", status="sent") for contact in contacts])
        return list(blast.deliveries.order_by("pk"))

    def event(self, delivery, event_type="email.bounced", **fields):
        return {"type": event_type, "sender": delivery.blast.mailbox.email, "recipient": delivery.address,
            "email_id": delivery.provider_id or str(uuid.uuid4()), "subject": delivery.blast.subject,
            "test": False, "bounce_type": "Permanent" if event_type == "email.bounced" else "", **fields}

    def red_incident(self, blast, count=1000):
        rows = self.audience(blast, count)
        BlastDelivery.objects.filter(pk__in=[row.pk for row in rows[:60]]).update(status="bounced", bounce_kind="hard")
        signal, = evaluate(blast_metrics(blast, now=self.now))
        return detect(blast, signal, now=self.now), signal

    def test_stream_sync_is_idempotent_and_never_enables_or_resets_state(self):
        self.notify.state = "stopped"; self.notify.save()
        sync_streams()
        self.notify.refresh_from_db()
        self.assertEqual(SendingStream.objects.count(), 2)
        self.assertEqual(SenderMailbox.objects.count(), 2)
        self.assertFalse(self.notify.mailbox.enabled)
        self.assertEqual(self.notify.state, "stopped")

    def test_old_client_mailbox_choice_is_ignored_for_staff(self):
        with override_settings(CRM_DELIVERABILITY_OWNERS=()):
            blast = save_blast_draft(None, {"purpose": "marketing", "channels": ["email"],
                "email": {"mailbox_id": uuid.uuid4(), "subject": "Community news"}}, actor=self.admin, now=self.now)
        self.assertEqual((blast.stream_id, blast.mailbox_id), (self.news.pk, self.news.mailbox_id))

    def test_root_mailbox_and_direct_root_blast_are_refused(self):
        response = self.client.post(PREFIX + "mailboxes/", {"email": "bulk@mlai.au", "display_name": "MLAI"}, format="json")
        self.assertEqual((response.status_code, response.json()["error"]), (400, "mailbox_domain_protected"))
        root = SenderMailbox.objects.create(email="legacy@mlai.au", domain="mlai.au", route="resend", enabled=True)
        blast = self.blast(status="draft"); blast.mailbox = root; blast.save()
        with self.assertRaises(BlastError) as caught:
            queue_blast(blast.pk, actor=self.admin, confirm={"email": 0}, idempotency_key="root-attempt")
        self.assertEqual((caught.exception.code, caught.exception.status), ("protected_domain", 409))
        self.assertFalse(blast.deliveries.exists())

    def test_one_hard_bounce_suppresses_one_address_without_pausing_anything(self):
        blast = self.blast()
        row = self.audience(blast, 1000)[0]
        record_event(self.event(row), "one-bounce", now=self.now)
        row.refresh_from_db(); row.contact.refresh_from_db(); blast.refresh_from_db(); self.news.refresh_from_db()
        self.assertEqual((row.status, row.bounce_kind, row.contact.email_status), ("bounced", "hard", "bounced"))
        self.assertEqual(evaluate(blast_metrics(blast, now=self.now)), [])
        self.assertEqual((blast.pause_state, self.news.state), ("", "warming"))
        self.assertTrue(self.news.mailbox.enabled)
        self.assertFalse(DeliverabilityIncident.objects.exists())

    def test_six_percent_pauses_and_repeated_detection_is_one_incident(self):
        blast = self.blast()
        incident, signal = self.red_incident(blast)
        enforce(incident, now=self.now)
        repeated = detect(blast, signal, now=self.now + timedelta(minutes=1))
        enforce(repeated, now=self.now)
        self.assertEqual(incident.pk, repeated.pk)
        self.assertEqual(DeliverabilityIncident.objects.count(), 1)
        blast.refresh_from_db(); self.news.refresh_from_db(); self.notify.refresh_from_db()
        self.assertEqual((blast.pause_state, self.news.state, self.notify.state), ("system", "held", "warming"))
        self.assertEqual(CrmAuditEvent.objects.filter(target_type="deliverability_incident").count(), 2)

    def test_provider_pause_failure_preserves_admission_stop_and_is_retried(self):
        blast = self.blast(listmonk_campaign_id=42)
        incident, _ = self.red_incident(blast)
        with patch("crm.listmonk.get_campaign", return_value={"status": "running"}), patch(
                "crm.listmonk.set_campaign_status", side_effect=RuntimeError("synthetic outage")):
            with self.assertRaises(RuntimeError):
                enforce(incident, now=self.now)
        incident.refresh_from_db(); blast.refresh_from_db()
        self.assertEqual(blast.pause_state, "system")
        self.assertIsNone(incident.action_applied_at)
        with patch("crm.listmonk.get_campaign", return_value={"status": "running"}), patch("crm.listmonk.set_campaign_status") as pause:
            enforce(incident, now=self.now)
        pause.assert_called_once_with(42, "paused")
        incident.refresh_from_db(); self.assertIsNotNone(incident.action_applied_at)

    def test_non_allowlisted_stream_and_root_events_are_durable(self):
        for index, sender in enumerate(("events@news.mlai.au", "notifications@notify.mlai.au", "notifications@mlai.au")):
            event = {"type": "email.sent", "sender": sender, "recipient": "person@example.net", "email_id": str(uuid.uuid4()), "subject": "System mail", "test": False}
            record_event(event, f"unlisted-{index}", now=self.now)
        # notify.mlai.au is the system_email domain, whose feedback CRM never records.
        self.assertEqual(EmailEvent.objects.count(), 2)
        self.assertEqual(EmailEvent.objects.filter(stream=self.news).count(), 1)
        self.assertEqual(EmailEvent.objects.filter(stream=self.notify).count(), 0)
        self.assertEqual(EmailEvent.objects.filter(stream=None).count(), 1)

    def test_soft_bounce_is_once_per_blast_and_delivery_resets_streak(self):
        blast = self.blast(); row = self.audience(blast, 1)[0]
        event = self.event(row, bounce_type="Transient")
        record_event(event, "soft-1", now=self.now)
        record_event(event, "soft-1", now=self.now)
        record_event(event, "soft-same-blast", now=self.now)
        row.refresh_from_db(); row.contact.refresh_from_db()
        self.assertEqual((row.status, row.contact.soft_bounce_streak, row.contact.email_status), ("undelivered", 1, "active"))
        record_event({**event, "type": "email.delivered", "occurred_at": self.now + timedelta(minutes=1)}, "soft-recovered", now=self.now)
        row.refresh_from_db(); row.contact.refresh_from_db()
        self.assertEqual((row.status, row.contact.soft_bounce_streak), ("delivered", 0))

    def test_three_soft_bounces_in_different_blasts_suppress_address(self):
        first = self.blast(); row = self.audience(first, 1)[0]
        for index, blast in enumerate((first, self.blast(), self.blast())):
            if index:
                row = BlastDelivery.objects.create(blast=blast, contact=row.contact, address=row.address,
                    channel="email", consent_basis="explicit", status="sent")
            record_event(self.event(row, bounce_type="Transient"), f"soft-{index}", now=self.now)
        row.refresh_from_db(); row.contact.refresh_from_db()
        self.assertEqual((row.contact.soft_bounce_streak, row.contact.email_status, row.status, row.bounce_kind), (3, "bounced", "bounced", "hard"))

    def test_replay_and_late_success_never_restore_a_complaint(self):
        blast = self.blast(); row = self.audience(blast, 1)[0]
        event = self.event(row, "email.complained")
        self.assertEqual(record_event(event, "complaint-1", now=self.now)["status"], "recorded")
        self.assertEqual(record_event(event, "complaint-1", now=self.now)["status"], "duplicate")
        record_event({**event, "type": "email.delivered"}, "late-success", now=self.now)
        row.refresh_from_db(); row.contact.refresh_from_db()
        self.assertEqual((row.error_code, row.contact.email_status, row.contact.marketing_consent), ("resend_complained", "complained", "withdrawn"))
        self.assertEqual(ContactConsentEvent.objects.filter(contact=row.contact).count(), 1)
        self.assertEqual(EmailEvent.objects.count(), 2)

    def test_legacy_receipt_does_not_apply_feedback_twice(self):
        blast = self.blast(); row = self.audience(blast, 1)[0]
        event = self.event(row, bounce_type="Transient")
        CrmAuditEvent.objects.create(action="sync", target_type="resend_event", target_id=hashlib.sha256(b"legacy").hexdigest())
        self.assertEqual(record_event(event, "legacy", now=self.now)["status"], "duplicate")
        row.contact.refresh_from_db()
        self.assertEqual(row.contact.soft_bounce_streak, 0)
        self.assertEqual(EmailEvent.objects.count(), 1)

    def test_owner_resume_demotes_and_unchanged_history_does_not_stop_again(self):
        blast = self.blast(); incident, signal = self.red_incident(blast)
        self.news.ramp_stage = 5; self.news.save(update_fields=["ramp_stage"])
        acknowledge(incident.pk, actor=self.admin, resolution="resume_slow", now=self.now)
        self.news.refresh_from_db(); blast.refresh_from_db()
        self.assertEqual((self.news.state, self.news.ramp_stage, blast.pause_state), ("slowed", 3, ""))
        self.assertIsNone(detect(blast, signal, now=self.now + timedelta(minutes=1)))
        row = blast.deliveries.filter(status="sent").first()
        row.status, row.bounce_kind = "bounced", "hard"; row.save()
        updated, = evaluate(blast_metrics(blast, now=self.now))
        self.assertIsNotNone(detect(blast, updated, now=self.now + timedelta(minutes=2)))

    def test_owner_gate_keeps_admin_installation_and_no_store_requirements(self):
        self.assertEqual(self.member_client.get(PREFIX + "deliverability/").status_code, 403)
        with override_settings(CRM_DELIVERABILITY_OWNERS=()):
            self.assertEqual(self.client.get(PREFIX + "deliverability/").json()["error"], "owner_required")
        response = self.client.get(PREFIX + "deliverability/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertTrue(self.client.get(PREFIX + "access/").json()["can_manage_deliverability"])

    def test_owner_can_keep_stopped_then_resume_later_without_demoting_twice(self):
        blast = self.blast(); incident, _ = self.red_incident(blast)
        self.news.ramp_stage = 5; self.news.save(update_fields=["ramp_stage"])
        acknowledge(incident.pk, actor=self.admin, resolution="keep_stopped", now=self.now)
        self.news.refresh_from_db(); self.assertEqual(self.news.state, "held")
        acknowledge(incident.pk, actor=self.admin, resolution="resume_slow", now=self.now)
        acknowledge(incident.pk, actor=self.admin, resolution="resume_slow", now=self.now)
        self.news.refresh_from_db(); self.assertEqual(self.news.ramp_stage, 3)

    def test_failed_owner_validation_cannot_partially_restart_provider_campaigns(self):
        first = self.blast(listmonk_campaign_id=42)
        incident, _ = self.red_incident(first)
        second = self.blast(listmonk_campaign_id=43, pause_state="system")
        second.mailbox = SenderMailbox.objects.create(email="other@other.example", domain="other.example", enabled=True)
        second.save()
        with patch("crm.listmonk.get_campaign") as get, patch("crm.listmonk.set_campaign_status") as restart:
            with self.assertRaises(BlastError):
                acknowledge(incident.pk, actor=self.admin, resolution="resume_slow", now=self.now)
        get.assert_not_called(); restart.assert_not_called()
        first.refresh_from_db(); second.refresh_from_db(); incident.refresh_from_db()
        self.assertEqual((first.pause_state, second.pause_state, incident.state), ("system", "system", "open"))
        self.assertFalse((first.stats.get("email") or {}).get("resume_pending"))

    def test_resume_reconciles_provider_success_after_a_database_rollback(self):
        blast = self.blast(listmonk_campaign_id=42)
        incident, _ = self.red_incident(blast)
        acknowledge(incident.pk, actor=self.admin, resolution="resume_slow", now=self.now)
        blast.refresh_from_db()
        self.assertTrue(blast.stats["email"]["resume_pending"])
        provider, failure = {"status": "paused"}, {"raised": False}
        original_save = Blast.save

        def interrupted_save(instance, *args, **kwargs):
            if not failure["raised"] and not instance.stats["email"]["resume_pending"]:
                failure["raised"] = True
                raise RuntimeError("synthetic database write failure")
            return original_save(instance, *args, **kwargs)

        def restart_provider(campaign_id, status):
            self.assertEqual(campaign_id, 42)
            provider["status"] = status

        with patch("crm.deliverability.guardian.admission_ready", return_value=True), patch(
                "crm.listmonk.get_campaign", side_effect=lambda _id: dict(provider)), patch(
                "crm.listmonk.set_campaign_status", side_effect=restart_provider) as restart, patch(
                "crm.listmonk.create_campaign") as create, patch.object(Blast, "save", autospec=True, side_effect=interrupted_save):
            with self.assertRaises(RuntimeError):
                resume_campaign(blast.pk, now=self.now)
            blast.refresh_from_db()
            self.assertTrue(blast.stats["email"]["resume_pending"])
            self.assertEqual(blast.pause_state, "")
            self.assertEqual(resume_campaign(blast.pk, now=self.now), {"campaign": "running"})
        restart.assert_called_once_with(42, "running"); create.assert_not_called()
        blast.refresh_from_db(); self.assertFalse(blast.stats["email"]["resume_pending"])

    def test_guardian_never_applies_campaign_rules_to_transactional_stream(self):
        blast = self.blast(); blast.stream, blast.mailbox = self.notify, self.notify.mailbox; blast.save()
        rows = self.audience(blast, 100)
        BlastDelivery.objects.filter(pk__in=[row.pk for row in rows[:10]]).update(status="bounced", bounce_kind="hard")
        cache.clear()
        self.assertEqual(run_deliverability_guard(now=self.now)["status"], "completed")
        self.notify.refresh_from_db()
        self.assertEqual(self.notify.state, "warming")
        self.assertFalse(DeliverabilityIncident.objects.exists())
