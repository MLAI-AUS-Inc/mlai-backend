"""Synthetic DB regression suite; run only with approval for the disposable schema."""

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from community_chat.onboarding_models import CommunityMemberConsent, CommunityMemberProfile
from core.models import User
from crm.community_optins import sync_community_optins
from crm.models import Blast, BlastDelivery, Contact, ContactConsentEvent, CrmAuditEvent, SenderMailbox
from crm.resend_feedback import record_event
from crm.warmup import _tick, admission_block, audit_state, plan_run, run_state

from .test_resend_warmup_unit import SECRET, SENDERS


@override_settings(CRM_RESEND_MONITORED_SENDERS=SENDERS, CRM_RESEND_WEBHOOK_SECRET=SECRET,
    CRM_WARMUP_ENABLED=True, CRM_WARMUP_RUN_ID="regression", CRM_SENDING_ENABLED=True,
    COMMUNITY_CHAT_CRM_ENABLED=True, LISTMONK_BASE_URL="http://listmonk.test:9000",
    LISTMONK_API_USER="synthetic", LISTMONK_API_TOKEN="synthetic")
class ResendWarmupDatabaseTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.admin = User.objects.create_user("admin@example.net", is_active=True, is_superuser=True)
        self.member = User.objects.create_user("member@example.net", is_active=True,
                                             email_verified_at=self.now, last_login=self.now)
        self.profile = CommunityMemberProfile.objects.create(user=self.member, status="approved", marketing_opt_in=True)
        self.evidence = CommunityMemberConsent.objects.create(user=self.member, purpose="marketing_email",
                                                               granted=True, policy_version="v1", source="test")
        for sender in SENDERS:
            SenderMailbox.objects.create(email=sender, domain=sender.split("@")[1],
                display_name="MLAI", route="resend", enabled=True, daily_limit=20)

    def import_contact(self):
        sync_community_optins(actor=self.admin)
        return Contact.objects.get(email=self.member.email)

    def plan(self):
        plan_run("regression", self.now.date(), actor=self.admin)
        blast = Blast.objects.order_by("stats__warmup__scheduled_at").first()
        blast.stats["warmup"]["scheduled_at"] = (self.now - timedelta(minutes=1)).isoformat()
        blast.save(update_fields=["stats"])
        state = run_state("regression")
        audit_state("regression", "activated", ends_at=state["ends_at"])
        return blast

    def test_import_is_idempotent_uses_exact_evidence_and_preserves_withdrawal(self):
        contact = self.import_contact()
        self.assertEqual(contact.marketing_consent_at, self.evidence.created_at)
        self.assertEqual(ContactConsentEvent.objects.get(contact=contact).detail["consent_event_id"], self.evidence.pk)
        self.import_contact()
        self.assertEqual(ContactConsentEvent.objects.filter(contact=contact).count(), 1)
        contact.marketing_consent = "withdrawn"; contact.save()
        self.import_contact(); contact.refresh_from_db()
        self.assertEqual(contact.marketing_consent, "withdrawn")

    def test_source_withdrawal_is_imported_and_never_resubscribed(self):
        contact = self.import_contact()
        self.profile.marketing_opt_in = False; self.profile.save()
        sync_community_optins(); contact.refresh_from_db()
        self.assertEqual((contact.email_status, contact.marketing_consent), ("unsubscribed", "withdrawn"))
        self.profile.marketing_opt_in = True; self.profile.save()
        sync_community_optins(); contact.refresh_from_db()
        self.assertEqual(contact.email_status, "unsubscribed")

    def test_plan_dry_run_rolls_back_and_repeat_does_not_duplicate(self):
        plan_run("regression", self.now.date(), actor=self.admin, dry_run=True)
        self.assertFalse(Blast.objects.exists()); self.assertFalse(Contact.objects.exists())
        plan_run("regression", self.now.date(), actor=self.admin)
        plan_run("regression", self.now.date(), actor=self.admin)
        self.assertEqual(Blast.objects.count(), 3)

    def test_queue_is_once_and_missing_feedback_pauses_both_senders(self):
        self.plan()
        self.assertEqual(_tick("regression", self.now)["admitted"], 1)
        self.assertEqual(_tick("regression", self.now)["admitted"], 0)
        self.assertEqual(BlastDelivery.objects.count(), 1)
        result = _tick("regression", self.now + timedelta(hours=2))
        self.assertEqual(result["reason"], "delivery_feedback_missing")
        self.assertFalse(SenderMailbox.objects.filter(enabled=True).exists())

    def test_consent_is_rechecked_after_queue(self):
        blast = self.plan()
        _tick("regression", self.now)
        self.profile.marketing_opt_in = False; self.profile.save()
        blast.refresh_from_db()
        self.assertEqual(admission_block(blast, self.now), "consent_not_current")

    def test_provider_replays_and_late_delivery_do_not_erase_complaint(self):
        contact = self.import_contact()
        contact.listmonk_subscriber_id = 42; contact.save()
        blast = Blast.objects.create(mailbox=SenderMailbox.objects.first(), channels=["email"],
                                      purpose="marketing", subject="Test", status="sending")
        delivery = BlastDelivery.objects.create(blast=blast, contact=contact, channel="email",
            address=contact.email, consent_basis="explicit", status="submitting")
        event = {"type": "email.complained", "sender": blast.mailbox.email, "recipient": contact.email,
                 "email_id": str(uuid.uuid4()), "subject": "Test", "test": False}
        with patch("crm.listmonk.blocklist_subscribers") as block:
            self.assertEqual(record_event(event, "message-1")["status"], "recorded")
            self.assertEqual(record_event(event, "message-1")["status"], "duplicate")
            record_event({**event, "type": "email.delivered"}, "message-2")
        self.assertEqual(block.call_count, 2)
        delivery.refresh_from_db(); contact.refresh_from_db()
        self.assertEqual(delivery.error_code, "resend_complained")
        self.assertEqual((contact.email_status, contact.marketing_consent), ("complained", "withdrawn"))
        self.assertFalse(SenderMailbox.objects.filter(enabled=True).exists())
        self.assertEqual(CrmAuditEvent.objects.filter(target_type="resend_event").count(), 2)

    def test_provider_simulator_event_does_not_pause_real_senders(self):
        event = {"type": "email.bounced", "sender": SENDERS[0], "recipient": "bounced+crm@resend.dev",
                 "email_id": str(uuid.uuid4()), "subject": "Test", "test": True}
        record_event(event, "test-message")
        self.assertEqual(SenderMailbox.objects.filter(enabled=True).count(), 2)
