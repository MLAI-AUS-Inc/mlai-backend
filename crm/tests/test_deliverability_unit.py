"""Database-free policy, provider contracts, protected identities and owner boundaries."""

import copy
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from community_chat.crm.errors import CrmError
from community_chat.crm.services import save_mailbox
from crm import blast_email
from crm.blast_email import email_block
from crm.blasts import BlastError
from crm.deliverability import guardian, policy
from crm.deliverability import alerts
from crm.deliverability.alerts import alert_text
from crm.deliverability.copy import status_message
from crm.deliverability.guardian import Metrics, evaluate
from crm.deliverability.streams import monitored_sender, protected_domain, stream_definitions
from crm.resend_feedback import next_delivery_state, parse_event
from crm.models import Blast, SenderMailbox, SendingStream


class RateTests(SimpleTestCase):
    def test_one_bounce_in_a_thousand_never_pauses(self):
        self.assertEqual(evaluate(Metrics(attempted=1000, hard_bounces=1)), [])

    def test_six_percent_bounces_pauses_blast(self):
        signal, = evaluate(Metrics(attempted=1000, hard_bounces=60))
        self.assertEqual((signal.key, signal.severity, signal.action), ("hard_bounce_rate", "red", "pause_blast"))

    def test_minimum_sample_and_inclusive_thresholds(self):
        self.assertEqual(evaluate(Metrics(attempted=99, hard_bounces=20)), [])
        signal, = evaluate(Metrics(attempted=100, hard_bounces=2))
        self.assertEqual(signal.severity, "amber")
        self.assertEqual(evaluate(Metrics(attempted=100, hard_bounces=5))[0].severity, "red")

    def test_complaint_minimum_count_and_delivered_denominator(self):
        self.assertEqual(evaluate(Metrics(delivered=100, complaints=1)), [])
        self.assertEqual(evaluate(Metrics(delivered=300, complaints=1)), [])
        self.assertEqual(evaluate(Metrics(attempted=10000, delivered=1000, complaints=2))[0].severity, "amber")
        self.assertEqual(evaluate(Metrics(delivered=3000, complaints=3))[0].severity, "red")
        self.assertEqual(evaluate(Metrics(delivered=500, complaints=2))[0].severity, "red")

    @override_settings(CRM_GUARD_HARD_BOUNCE_MIN_SAMPLE=10, CRM_GUARD_HARD_BOUNCE_RED=0.1)
    def test_thresholds_are_read_from_settings(self):
        self.assertEqual(evaluate(Metrics(attempted=10, hard_bounces=1))[0].severity, "red")

    def test_three_actual_policy_blocks_stop_stream(self):
        self.assertEqual(evaluate(Metrics(policy_blocks=2)), [])
        self.assertEqual(evaluate(Metrics(policy_blocks=3))[0].action, "stop_stream")

    def test_policy_codes_require_the_right_provider_and_context(self):
        def event(provider, code, text=""):
            return SimpleNamespace(recipient_provider=provider, enhanced_code=code, diagnostic=text)
        for provider, code, text in (("google", "5.7.26", ""), ("google", "5.7.1", "likely unsolicited"),
                ("microsoft", "5.7.1", "S3140"), ("yahoo", "", "TSS09"), ("apple", "", "HM08")):
            self.assertTrue(guardian.policy_block(event(provider, code, text)))
        for provider, code, text in (("google", "5.7.1", "Mailbox disabled"), ("other", "5.7.26", ""),
                ("microsoft", "4.7.650", "Try later"), ("google", "", "temporary delay")):
            self.assertFalse(guardian.policy_block(event(provider, code, text)))

    def test_fingerprints_are_stable_and_scope_specific(self):
        first = guardian.fingerprint("news", "first", "hard_bounce_rate")
        self.assertEqual(first, guardian.fingerprint("news", "first", "hard_bounce_rate"))
        self.assertNotEqual(first, guardian.fingerprint("notify", "first", "hard_bounce_rate"))
        self.assertNotEqual(first, guardian.fingerprint("news", "second", "hard_bounce_rate"))

    @override_settings(CRM_GUARDIAN_ENABLED=True)
    def test_runner_never_breaks_shared_scheduler(self):
        with patch("crm.deliverability.guardian.cache.add", side_effect=RuntimeError("synthetic")):
            self.assertEqual(guardian.run_deliverability_guard()["status"], "deferred")

    @override_settings(CRM_GUARDIAN_ENABLED=True, CRM_GUARDIAN_DRY_RUN=True, APP_ENV="production")
    def test_production_cannot_run_a_dry_guardian(self):
        self.assertEqual(guardian.run_deliverability_guard()["reason"], "dry_run_forbidden_in_production")


class IdentityTests(SimpleTestCase):
    def test_root_is_always_protected_but_subdomains_are_distinct(self):
        self.assertTrue(protected_domain("MLAI.au."))
        self.assertFalse(protected_domain("news.mlai.au"))
        with override_settings(CRM_PROTECTED_DOMAINS=()):
            self.assertTrue(protected_domain("mlai.au"))

    def test_default_streams_separate_campaigns_and_notifications(self):
        news, notify = stream_definitions(policy.DEFAULTS["CRM_STREAMS"])
        self.assertEqual(news["purposes"], ["marketing", "event_update"])
        self.assertFalse(news["transactional"])
        self.assertTrue(notify["transactional"])
        self.assertEqual(notify["purposes"], [])

    def test_duplicate_or_protected_streams_are_invalid(self):
        for raw in ("news:mlai.au:hello@mlai.au:marketing", "news:news.mlai.au:other@example.net:marketing",
                "news:news.mlai.au:hello@news.mlai.au:marketing;more:more.mlai.au:hello@more.mlai.au:marketing",
                "news:news.mlai.au:hello@news.mlai.au:transactional|marketing", "not a stream"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                stream_definitions(raw)

    @override_settings(CRM_RESEND_MONITORED_SENDERS=())
    def test_stream_and_system_senders_do_not_need_the_old_allowlist(self):
        for sender in ("events@news.mlai.au", "notifications@mlai.au"):
            self.assertTrue(monitored_sender(sender))
        for sender in ("a@evilnews.mlai.au", "a@mlai.au.example.net", "a@unconfigured.net"):
            self.assertFalse(monitored_sender(sender))

    @override_settings(CRM_RESEND_MONITORED_SENDERS=("notifications@notify.mlai.au",),
                       MLAI_SYSTEM_FROM_EMAIL="MLAI <notifications@notify.mlai.au>")
    def test_system_email_domain_feedback_is_never_crm_feedback(self):
        # system_email owns notify.mlai.au feedback, even when CRM lists the sender.
        self.assertFalse(monitored_sender("notifications@notify.mlai.au"))
        self.assertFalse(monitored_sender("other@notify.mlai.au"))
        self.assertTrue(monitored_sender("events@news.mlai.au"))

    def test_service_refuses_root_mailbox_before_any_provider_or_database_call(self):
        with patch("community_chat.crm.services.require_mlai_admin"):
            with self.assertRaises(CrmError) as caught:
                save_mailbox(None, {"email": "bulk@mlai.au"}, actor=SimpleNamespace())
        self.assertEqual((caught.exception.code, caught.exception.status), ("mailbox_domain_protected", 400))

    def test_non_owner_cannot_acknowledge_system_pause(self):
        with patch("crm.deliverability.guardian.is_owner", return_value=False), self.assertRaises(BlastError) as caught:
            guardian.acknowledge("not-looked-up", actor=SimpleNamespace(), resolution="resume")
        self.assertEqual((caught.exception.code, caught.exception.status), ("owner_required", 403))

    def test_config_types_are_validated_without_credentials(self):
        values = policy.environment_settings({"CRM_GUARDIAN_ENABLED": "true", "CRM_GUARD_HARD_BOUNCE_RED": "0.06"})
        self.assertTrue(values["CRM_GUARDIAN_ENABLED"])
        self.assertEqual(values["CRM_GUARD_HARD_BOUNCE_RED"], 0.06)
        for env in ({"CRM_GUARD_HARD_BOUNCE_RED": "nan"}, {"CRM_GUARD_HARD_BOUNCE_MIN_SAMPLE": "0"}, {"CRM_GUARDIAN_ENABLED": "maybe"}):
            with self.assertRaises(ValueError):
                policy.environment_settings(env)
        for key in ("CRM_GUARD_HARD_BOUNCE_RED", "CRM_GUARD_HARD_BOUNCE_MIN_SAMPLE"):
            with self.assertRaises(ValueError) as caught:
                policy.environment_settings({key: "sensitive-value"})
            self.assertNotIn("sensitive-value", str(caught.exception))


@override_settings(CRM_SENDING_ENABLED=True, CRM_GUARDIAN_ENABLED=True,
    LISTMONK_BASE_URL="http://listmonk.test", LISTMONK_API_USER="synthetic", LISTMONK_API_TOKEN="synthetic")
class AdmissionTests(SimpleTestCase):
    def blast(self):
        mailbox = SenderMailbox(email="hello@news.mlai.au", domain="news.mlai.au", enabled=True)
        stream = SendingStream(key="news", domain="news.mlai.au", mailbox=mailbox,
                               purposes=["marketing", "event_update"])
        return Blast(purpose="marketing", channels=["email"], stream=stream, mailbox=mailbox)

    def test_missing_guardian_heartbeat_holds_new_campaign_starts(self):
        with patch("crm.deliverability.guardian.admission_ready", return_value=False):
            self.assertEqual(email_block(self.blast()), "guardian_unavailable")

    def test_unknown_dns_holds_new_campaign_starts(self):
        with patch("crm.deliverability.guardian.admission_ready", return_value=True), patch(
                "crm.sending_domains.is_verified", return_value=False):
            self.assertEqual(email_block(self.blast()), "sending_system_unverified")

    @override_settings(CRM_STREAMS="")
    def test_removing_a_stream_from_feedback_configuration_holds_its_starts(self):
        self.assertEqual(email_block(self.blast()), "stream_not_configured")

    def test_transactional_stream_cannot_admit_a_crm_blast(self):
        blast = self.blast()
        blast.stream.key, blast.stream.domain, blast.stream.transactional = "notify", "notify.mlai.au", True
        self.assertEqual(email_block(blast), "stream_not_for_blasts")


class FeedbackTests(SimpleTestCase):
    def payload(self, event_type="email.bounced", **data):
        return {"type": event_type, "created_at": "2026-11-05T00:00:00Z", "data": {
            "from": "MLAI <other@news.mlai.au>", "to": ["member@gmail.com"], "subject": "Community news",
            "email_id": "e72632e8-7372-4d4b-9ec2-6fb517bb9929", **data}}

    @override_settings(CRM_RESEND_MONITORED_SENDERS=())
    def test_official_bounce_fields_and_masked_diagnostics(self):
        event = parse_event(self.payload(bounce={"type": "Permanent", "subType": "General",
            "message": "550 5.7.1 member@gmail.com: likely unsolicited"}))
        self.assertEqual((event["bounce_type"], event["bounce_subtype"], event["smtp_code"], event["enhanced_code"]),
                         ("Permanent", "General", "550", "5.7.1"))
        self.assertNotIn("member@gmail.com", event["diagnostic"])
        self.assertEqual(event["occurred_at"], datetime(2026, 11, 5, tzinfo=timezone.utc))

    def test_failed_suppressed_and_delay_payloads_have_distinct_diagnostics(self):
        self.assertEqual(parse_event(self.payload("email.failed", failed={"reason": "reached_daily_quota"}))["diagnostic"], "reached_daily_quota")
        suppressed = parse_event(self.payload("email.suppressed", suppressed={"type": "OnAccountSuppressionList", "message": "Previously suppressed"}))
        self.assertEqual(suppressed["bounce_subtype"], "OnAccountSuppressionList")
        delayed = parse_event(self.payload("email.delivery_delayed"))
        self.assertEqual((delayed["diagnostic"], delayed["enhanced_code"]), ("", ""))

    def test_bad_nested_diagnostics_and_timestamps_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_event(self.payload(bounce="invalid"))
        for timestamp in ("not-a-time", "2026-11-05T00:00:00"):
            payload = self.payload(); payload["created_at"] = timestamp
            with self.assertRaises(ValueError):
                parse_event(payload)

    def test_soft_bounce_is_recoverable_and_never_overwrites_a_hard_bounce(self):
        self.assertEqual(next_delivery_state("sent", "", "email.bounced", bounce_type="Transient"), ("undelivered", "soft_bounce"))
        self.assertEqual(next_delivery_state("undelivered", "soft_bounce", "email.delivered"), ("delivered", ""))
        self.assertEqual(next_delivery_state("bounced", "resend_bounced", "email.bounced", bounce_type="Transient"), ("bounced", "resend_bounced"))

    def test_staff_copy_has_no_technical_terms(self):
        pending = status_message(SimpleNamespace(pause_state="", status="sending", stats={"email": {"resume_pending": True}}))
        self.assertEqual(pending["tone"], "info")
        self.assertIn("next email system check", pending["text"])
        for signal in ("hard_bounce_rate", "complaint_rate", "policy_block"):
            message = status_message(SimpleNamespace(pause_state="system", pause_reason=signal))
            self.assertEqual(message["tone"], "stop")
            self.assertNotRegex(message["text"], r"(?i)\b(DKIM|DMARC|SPF|MX|listmonk|Resend|IP|SMTP|webhook|subdomain)\b")

    def test_owner_alert_contains_aggregate_evidence_without_recipient_data(self):
        incident = SimpleNamespace(pk="synthetic", signal="hard_bounce_rate", value=0.06, sample=1000, action="pause_blast")
        text = alert_text(incident)
        self.assertIn("6.00%", text)
        self.assertIn("1000", text)
        self.assertNotIn("@", text)


@override_settings(CRM_SENDING_ENABLED=True)
class RecoveryTests(SimpleTestCase):
    now = datetime(2026, 11, 5, tzinfo=timezone.utc)

    @contextmanager
    def recovery(self, *, provider_status="paused", block=None):
        mailbox = SenderMailbox(email="hello@news.mlai.au", domain="news.mlai.au", enabled=True)
        stream = SendingStream(key="news", domain="news.mlai.au", mailbox=mailbox)
        blast = Blast(status="sending", stream=stream, mailbox=mailbox, listmonk_campaign_id=42,
                      stats={"email": {"started_at": self.now.isoformat(), "resume_pending": True}})
        with ExitStack() as stack:
            stack.enter_context(patch.object(blast_email.transaction, "atomic"))
            manager = stack.enter_context(patch.object(Blast, "objects"))
            manager.values_list.return_value.get.return_value = stream.pk
            manager.select_for_update.return_value.get.return_value = blast
            stack.enter_context(patch.object(SendingStream, "objects"))
            save = stack.enter_context(patch.object(blast, "save"))
            stack.enter_context(patch.object(blast_email, "email_block", return_value=block))
            get = stack.enter_context(patch.object(blast_email.listmonk, "get_campaign", return_value={"status": provider_status}))
            restart = stack.enter_context(patch.object(blast_email.listmonk, "set_campaign_status"))
            create = stack.enter_context(patch.object(blast_email.listmonk, "create_campaign"))
            stack.enter_context(patch("crm.models.CrmAuditEvent.objects"))
            yield blast, save, get, restart, create

    def test_dispatcher_resumes_the_existing_campaign_and_clears_durable_intent(self):
        with self.recovery() as (blast, save, get, restart, create):
            self.assertEqual(blast_email.resume_campaign(blast.pk, now=self.now), {"campaign": "running"})
            get.assert_called_once_with(42)
            restart.assert_called_once_with(42, "running")
            create.assert_not_called()
            self.assertFalse(blast.stats["email"]["resume_pending"])
            save.assert_called_once()

    def test_a_lost_database_ack_reconciles_running_status_without_restart(self):
        with self.recovery() as (blast, save, get, restart, create):
            original = copy.deepcopy(blast.stats)
            save.side_effect = [RuntimeError("synthetic database failure"), None]
            get.side_effect = [{"status": "paused"}, {"status": "running"}]
            with self.assertRaises(RuntimeError):
                blast_email.resume_campaign(blast.pk, now=self.now)
            # The database rollback retains the original pending intent.
            blast.stats = original
            self.assertEqual(blast_email.resume_campaign(blast.pk, now=self.now), {"campaign": "running"})
            restart.assert_called_once_with(42, "running")
            create.assert_not_called()
            self.assertFalse(blast.stats["email"]["resume_pending"])

    def test_a_new_guardian_hold_blocks_a_pending_recovery(self):
        with self.recovery(block="stream_held") as (blast, save, get, restart, create):
            self.assertEqual(blast_email.resume_campaign(blast.pk, now=self.now), {"blocked": "stream_held"})
            self.assertTrue(blast.stats["email"]["resume_pending"])
            for call in (save, get, restart, create):
                call.assert_not_called()

    def test_a_started_campaign_reset_to_draft_is_not_restarted(self):
        with self.recovery(provider_status="draft") as (blast, save, get, restart, create):
            self.assertEqual(blast_email.resume_campaign(blast.pk, now=self.now), {"blocked": "campaign_state_unknown"})
            self.assertTrue(blast.stats["email"]["resume_pending"])
            for call in (save, restart, create):
                call.assert_not_called()

    def test_stopping_a_blast_also_stops_a_pending_resume(self):
        with self.recovery() as (blast, save, get, restart, create):
            blast.status = "cancelled"
            self.assertEqual(blast_email.resume_campaign(blast.pk, now=self.now), {"blocked": "blast_not_live"})
            for call in (save, get, restart, create):
                call.assert_not_called()

    def test_owner_acknowledgement_records_intent_without_any_provider_mutation(self):
        mailbox = SimpleNamespace(pk="mailbox", enabled=True, email="hello@news.mlai.au", domain="news.mlai.au")
        stream = SimpleNamespace(pk="stream", domain="news.mlai.au", transactional=False, mailbox=mailbox,
                                 ramp_stage=5, incidents=Mock(), save=Mock())
        stream.incidents.filter.return_value.exists.return_value = False
        stream.incidents.filter.return_value.exclude.return_value.exists.return_value = False
        incident = SimpleNamespace(pk="incident", state="open", resolution="", save=Mock())
        targets = [SimpleNamespace(pk=str(index), listmonk_campaign_id=index, mailbox_id=mailbox.pk,
            mailbox=mailbox, stats={}, pause_state="system", save=Mock()) for index in (42, 43)]
        with ExitStack() as stack:
            stack.enter_context(patch.object(guardian.transaction, "atomic"))
            stack.enter_context(patch.object(guardian, "is_owner", return_value=True))
            stack.enter_context(patch.object(guardian, "stream_configured", return_value=True))
            stack.enter_context(patch("crm.sending_domains.is_verified", return_value=True))
            streams = stack.enter_context(patch.object(guardian.SendingStream, "objects"))
            streams.select_for_update.return_value.get.return_value = stream
            incidents = stack.enter_context(patch.object(guardian.DeliverabilityIncident, "objects"))
            incidents.values_list.return_value.get.return_value = stream.pk
            incidents.select_for_update.return_value.get.return_value = incident
            blasts = stack.enter_context(patch.object(guardian.Blast, "objects"))
            blasts.select_for_update.return_value.filter.return_value.order_by.return_value = targets
            stack.enter_context(patch.object(guardian, "_audit"))
            get = stack.enter_context(patch.object(guardian.listmonk, "get_campaign"))
            restart = stack.enter_context(patch.object(guardian.listmonk, "set_campaign_status"))
            guardian.acknowledge(incident.pk, actor=SimpleNamespace(), resolution="resume_slow", now=self.now)
            get.assert_not_called()
            restart.assert_not_called()
        self.assertEqual(stream.ramp_stage, 3)
        for target in targets:
            self.assertEqual(target.pause_state, "")
            self.assertTrue(target.stats["email"]["resume_pending"])


class AlertRaceTests(SimpleTestCase):
    @override_settings(CRM_DELIVERABILITY_SLACK_CHANNEL="C_SYNTHETIC")
    def test_acknowledged_incident_is_not_alerted_from_a_stale_open_snapshot(self):
        with patch.object(alerts.transaction, "atomic"), patch.object(alerts.DeliverabilityIncident, "objects") as manager, patch(
                "jobs.services.slack.post_slack_message") as send:
            manager.filter.return_value.values_list.return_value = ["incident"]
            manager.select_for_update.return_value.get.return_value = SimpleNamespace(state="acknowledged")
            alerts.alert_open_incidents(now=datetime(2026, 11, 5, tzinfo=timezone.utc))
            send.assert_not_called()
