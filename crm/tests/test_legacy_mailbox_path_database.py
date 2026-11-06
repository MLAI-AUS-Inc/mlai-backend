"""Deploying the stack with its new controls off keeps main's existing email path.

Production today: every enabled staff mailbox is on the root domain, no stream is
configured, the guardian is off and the warm-up run is paused. These checks pin
that the release keeps those mailboxes, keeps the pause, refuses the root domain
on the new stream path and adds no scheduled send of its own. Nothing leaves the
process: listmonk is the in-memory fake from ``test_blasts_database``.
"""

from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from community_chat.crm.errors import CrmError
from community_chat.crm.services import save_blast_draft, save_mailbox
from crm import blast_email, blasts, warmup
from crm.blasts import BlastError, queue_blast
from crm.models import Blast, BlastDelivery, EmailEvent, SenderMailbox, SendingStream

from .api_fixtures import make_contact
from .test_blasts_database import SENDING, BlastFixtures

PRODUCTION_SHAPE = {
    **SENDING,
    # The new controls exactly as a release leaves them: everything off.
    "CRM_GUARDIAN_ENABLED": False,
    "CRM_WAVES_ENABLED": False,
    "CRM_PREFLIGHT_ENABLED": False,
    "CRM_MONITORS_ENABLED": False,
    "CRM_LIFECYCLE_ENABLED": False,
    "CRM_TRANSACTIONAL_MONITOR_ENABLED": False,
    "CRM_SEED_HEARTBEAT_ENABLED": False,
    "CRM_DELIVERABILITY_EMAIL_ALERTS_ENABLED": False,
    # Already enabled in production and preserved.
    "CRM_SENDING_ENABLED": True,
    "CRM_WARMUP_ENABLED": True,
    "CRM_SMS_ENABLED": True,
    "CRM_WHATSAPP_ENABLED": True,
    "CRM_WARMUP_RUN_ID": "legacy-run",
    "CRM_RESEND_MONITORED_SENDERS": ("hi@mlai.au", "tech@mlai.au"),
    # Staff, not deliverability owners.
    "CRM_DELIVERABILITY_OWNERS": ("owner@example.test",),
}
EMAIL_ONLY = {"channels": ["email"], "sms": {"body": ""}, "whatsapp": {"template_id": None, "variables": {}}}


@override_settings(**PRODUCTION_SHAPE)
class LegacyMailboxPathTests(BlastFixtures, TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        # Production has no stream rows yet; the fixture's news stream is removed.
        SendingStream.objects.all().delete()
        self.root = SenderMailbox.objects.create(email="hi@mlai.au", display_name="MLAI", domain="mlai.au",
            route=SenderMailbox.Route.RESEND, reply_to="hi@mlai.au", daily_limit=30, enabled=True)

    def legacy_draft(self, **overrides):
        body = {**EMAIL_ONLY, **overrides}
        body["email"] = {**self.draft_body()["email"], "mailbox_id": str(self.root.pk)}
        return self.create_draft(**body)

    def test_staff_draft_keeps_the_root_mailbox_without_a_stream(self):
        blast = self.legacy_draft()
        self.assertEqual((blast.mailbox_id, blast.stream_id), (self.root.pk, None))
        self.assertIsNone(blast_email.email_block(blast))

    def test_legacy_queue_start_and_dispatch_behave_as_on_main(self):
        blast = self.legacy_draft()
        response = self.send(blast, confirm={"email": 4})
        self.assertEqual(response.status_code, 202, response.content)
        blast.refresh_from_db()
        self.assertEqual((blast.status, blast.stream_id, blast.mailbox_id), ("queued", None, self.root.pk))
        self.assertEqual(BlastDelivery.objects.filter(blast=blast, channel="email").count(), 4)
        result = blasts.dispatch_blast(blast.pk)
        self.assertEqual(result["email"]["campaign"], "running")
        blast.refresh_from_db()
        campaign = self.fake.campaigns[blast.listmonk_campaign_id]
        self.assertEqual(campaign["status"], "running")
        self.assertIn("hi@mlai.au", campaign["from_email"])
        self.assertEqual(self.fake.count("POST", r"/api/campaigns"), 1)

    def test_legacy_resume_reconciles_the_same_campaign(self):
        blast = self.legacy_draft()
        self.assertEqual(self.send(blast, confirm={"email": 4}).status_code, 202)
        blasts.dispatch_blast(blast.pk)
        blast.refresh_from_db()
        self.fake.campaigns[blast.listmonk_campaign_id]["status"] = "paused"
        Blast.objects.filter(pk=blast.pk).update(stats=blast_email.with_email_stats(blast.stats, resume_pending=True))
        self.assertEqual(blast_email.resume_campaign(blast.pk, now=self.now), {"campaign": "running"})
        self.assertEqual(self.fake.campaigns[blast.listmonk_campaign_id]["status"], "running")
        self.assertEqual(self.fake.count("POST", r"/api/campaigns"), 1)

    def test_staff_can_still_edit_and_switch_off_an_existing_root_mailbox(self):
        mailbox = save_mailbox(self.root, {"daily_limit": 20, "enabled": False}, actor=self.admin)
        self.assertEqual((mailbox.daily_limit, mailbox.enabled), (20, False))
        with self.assertRaises(CrmError) as caught:
            save_mailbox(None, {"email": "bulk@mlai.au", "display_name": "MLAI"}, actor=self.admin)
        self.assertEqual(caught.exception.code, "mailbox_domain_protected")

    def test_sms_and_whatsapp_paths_are_unchanged(self):
        blast = self.create_draft(channels=["sms", "whatsapp"], email={})
        self.assertIsNone(blast.stream_id)
        response = self.send(blast, confirm={"sms": 1, "whatsapp": 1})
        self.assertEqual(response.status_code, 202, response.content)


@override_settings(**PRODUCTION_SHAPE)
class StreamPathStaysStrictTests(BlastFixtures, TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.root = SenderMailbox.objects.create(email="hi@mlai.au", display_name="MLAI", domain="mlai.au",
            route=SenderMailbox.Route.RESEND, daily_limit=30, enabled=True)

    def root_blast(self, **fields):
        return Blast.objects.create(created_by=self.admin, purpose="marketing", channels=["email"], status="draft",
            mailbox=self.root, subject="Community news", body_markdown="MLAI community news", **fields)

    def assert_refused(self, blast, key):
        with self.assertRaises(BlastError) as caught:
            queue_blast(blast.pk, actor=self.admin, confirm={"email": 0}, idempotency_key=key)
        self.assertEqual((caught.exception.code, caught.exception.status), ("protected_domain", 409))
        self.assertFalse(blast.deliveries.exists())

    def test_a_stream_assigned_blast_on_the_root_domain_is_refused(self):
        blast = self.root_blast(stream=self.stream)
        self.assertEqual(blast_email.email_block(blast), "protected_domain")
        self.assert_refused(blast, "stream-root")

    def test_turning_the_legacy_path_off_refuses_the_root_domain_everywhere(self):
        blast = self.root_blast()
        with override_settings(CRM_LEGACY_MAILBOX_PATH_ENABLED=False):
            self.assertEqual(blast_email.email_block(blast), "protected_domain")
            self.assert_refused(blast, "cutover-root")

    def test_waves_or_preflight_never_use_the_legacy_path(self):
        blast = self.root_blast()
        for flag in ("CRM_WAVES_ENABLED", "CRM_PREFLIGHT_ENABLED"):
            with self.subTest(flag=flag), override_settings(**{flag: True}):
                self.assertEqual(blast_email.email_block(blast), "protected_domain")

    def test_a_stream_domain_mailbox_without_a_stream_still_needs_the_stream_path(self):
        blast = self.root_blast(); blast.mailbox = self.mailbox; blast.save()
        save_blast_draft(blast, {"channels": ["email"]}, actor=self.admin, now=self.now)
        blast.refresh_from_db()
        self.assertEqual(blast.stream_id, self.stream.pk)
        self.assertEqual(blast_email.email_block(blast), "guardian_disabled")

    def test_staff_cannot_pick_a_stream_identity_mailbox(self):
        other = SenderMailbox.objects.create(email="other@news.mlai.au", domain="news.mlai.au",
            route=SenderMailbox.Route.RESEND, daily_limit=30, enabled=True)
        blast = self.create_draft(**EMAIL_ONLY)
        save_blast_draft(blast, {"email": {"mailbox_id": other.pk}}, actor=self.admin, now=self.now)
        blast.refresh_from_db()
        self.assertEqual((blast.stream_id, blast.mailbox_id), (self.stream.pk, self.mailbox.pk))


@override_settings(**PRODUCTION_SHAPE)
class PausedWarmupAndSchedulerTests(BlastFixtures, TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        SendingStream.objects.all().delete()
        self.hi = SenderMailbox.objects.create(email="hi@mlai.au", domain="mlai.au",
            route=SenderMailbox.Route.RESEND, daily_limit=30, enabled=True)
        self.tech = SenderMailbox.objects.create(email="tech@mlai.au", domain="mlai.au",
            route=SenderMailbox.Route.RESEND, daily_limit=30, enabled=True)
        self.person = make_contact("warm@example.com", marketing_consent="granted", marketing_consent_at=self.now)
        warmup.audit_state("legacy-run", "planned")
        warmup.audit_state("legacy-run", "activated")
        warmup.audit_state("legacy-run", "paused", reason="staff")
        self.warm = Blast.objects.create(created_by=self.admin, purpose="marketing", channels=["email"],
            status="queued", queued_at=self.now, mailbox=self.hi, subject="Hello", body_markdown="Hello from MLAI",
            stats={"warmup": {"run_id": "legacy-run", "contact_id": str(self.person.pk),
                "scheduled_at": self.now.isoformat(), "ends_at": (self.now + timedelta(days=11)).isoformat()}})
        BlastDelivery.objects.create(blast=self.warm, contact=self.person, address=self.person.email,
            channel="email", consent_basis="explicit", status="queued")

    def test_paused_warmup_stays_paused_not_root_blocked(self):
        self.assertEqual(blast_email.email_block(self.warm), "warmup_inactive")
        self.assertEqual(warmup.run_state("legacy-run")["operation"], "paused")
        blasts_before = Blast.objects.count()
        result = warmup.run_warmup_scheduler(now=self.now)
        self.assertEqual((result["status"], result.get("state")), ("skipped", "paused"))
        blasts.dispatch_blast(self.warm.pk, now=self.now)
        self.warm.refresh_from_db()
        self.assertIsNone(self.warm.listmonk_campaign_id)
        self.assertEqual(self.fake.count("POST", r"/api/campaigns"), 0)
        self.assertEqual(Blast.objects.count(), blasts_before)
        self.assertEqual(warmup.run_state("legacy-run")["operation"], "paused")

    def test_new_scheduled_jobs_send_nothing_with_their_controls_off(self):
        from core.management.commands import run_scheduled_discovery as scheduler
        jobs = (scheduler.run_deliverability_monitors, scheduler.run_transactional_monitor,
            scheduler.run_deliverability_guard, scheduler.run_daily_digest, scheduler.run_seed_checks,
            scheduler.run_stream_day_close, scheduler.run_lifecycle_refresh, scheduler.run_domain_hygiene)
        counts = (Blast.objects.count(), BlastDelivery.objects.count(), EmailEvent.objects.count())
        calls = len(self.fake.calls)
        with patch("integrations.http_client.post", side_effect=AssertionError("network forbidden")):
            for job in jobs:
                with self.subTest(job=job.__name__):
                    self.assertIn(job()["status"], ("disabled", "inert"))
        self.assertEqual((Blast.objects.count(), BlastDelivery.objects.count(), EmailEvent.objects.count()), counts)
        self.assertEqual(len(self.fake.calls), calls)
        self.assertEqual(warmup.run_state("legacy-run")["operation"], "paused")
