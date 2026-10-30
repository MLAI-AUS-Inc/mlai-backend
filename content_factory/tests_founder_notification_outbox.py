"""P1.9: the draft-ready outbox (IF-P1-11).

A content_only draft of a new-journey org with no website connection gets
exactly one "Your draft is ready" email (and one Chat DM when the founder has
a verified device), however often the sweeps run and even when a send is
replayed. Legacy orgs, the flag off and old runs get no rows at all (G9).
"""
import json
from datetime import timedelta
from unittest import mock
from uuid import UUID, uuid4

from django.test import TestCase, override_settings
from django.utils import timezone

from agent_runtime import native_crypto
from agent_runtime.webhook_transport import WebhookResponse
from community_chat.models import CommunityChatDevice, DeviceBindingStatus
from workflow_runs.models import ContentFactoryRun

from . import founder_notification_outbox as outbox
from . import website_notification_transport as transport
from .founder_journey_sweeps import run_founder_journey_sweeps
from .founder_journey_testing import ALL_P1_FLAGS_ON, NAMED_LEGACY_ORGS, make_named_legacy_org, make_new_journey_org
from .models import ContentFactoryCallbackEvent, OrganizationContentConfig
from .website_models import WebsiteConnection

KEY = "01" * 32  # Synthetic local signing fixture, never a deployed key.
DEVICE = native_crypto.public_key(native_crypto.signing_key("02" * 32))
DELIVERY = {"ARTICLES_GOAL_NOTIFIER_PRIVATE_KEY": KEY, "COMMUNITY_CHAT_RELAY_URL": "wss://chat.mlai.au",
            "RESEND_API_KEY": "synthetic", "RESEND_FROM_EMAIL": "Roo <notifications@example.test>",
            "COMMUNITY_CHAT_FRONTEND_URL": "https://chat.staging.example/"}
FLAGS = {**ALL_P1_FLAGS_ON, **DELIVERY}
FLAG_OFF = {**FLAGS, "ARTICLES_DRAFT_READY_NOTIFICATIONS_ENABLED": False}
V2_OFF = {**FLAGS, "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED": False}


class FakeProviders:
    """Resend deduplicates on Idempotency-Key; the relay deduplicates on event id."""

    def __init__(self):
        self.emails, self.email_keys, self.events, self.channel = [], {}, [], str(uuid4())

    def __call__(self, url, body, headers, **options):
        if url == "https://api.resend.com/emails":
            key = headers["Idempotency-Key"]
            self.emails.append((json.loads(body), key))
            self.email_keys.setdefault(key, str(uuid4()))
            return WebhookResponse(200, json.dumps({"id": self.email_keys[key]}).encode())
        event = json.loads(body)
        self.events.append(event)
        if event["kind"] == 41010:
            message = "response:" + json.dumps({"channel_id": self.channel, "created": False})
            return WebhookResponse(200, json.dumps({"accepted": True, "event_id": event["id"], "message": message}).encode())
        return WebhookResponse(200, json.dumps({"accepted": True, "event_id": event["id"], "message": "stored"}).encode())

    def messages(self):
        return [event for event in self.events if event["kind"] == 9]


def _draft(org, *, created_at=None, status="completed", requested_by=None, workflow="article_generation"):
    run = ContentFactoryRun.objects.create(
        run_id=f"run-{uuid4().hex[:12]}", workflow=workflow, domain=org.domain, organization=org, status=status,
        run_request={"delivery_mode": "content_only", "delivery_mode_confirmed": True,
                     **({"roo_points_requested_by_user_id": requested_by} if requested_by else {})},
        result={"delivery_mode": "content_only"})
    if created_at is not None:
        ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=created_at)
        run.refresh_from_db()
    return run


def _rows():
    return ContentFactoryCallbackEvent.objects.filter(event_type="founder_draft_ready")


def _sweep(now=None, times=1):
    for _ in range(times):
        outbox.enqueue_ready_drafts(now)
        outbox.drain(now)


class DraftReadyOutboxTests(TestCase):
    def setUp(self):
        self.built = make_new_journey_org()
        self.org, self.owner = self.built.organization, self.built.owner
        self.providers = FakeProviders()
        patcher = mock.patch.object(transport, "post_webhook", side_effect=self.providers)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _device(self, user=None):
        return CommunityChatDevice.objects.create(user=user or self.owner, public_key=DEVICE,
                                                  status=DeviceBindingStatus.VERIFIED, verified_at=timezone.now())

    @override_settings(**FLAGS)
    def test_one_email_for_a_content_only_draft_with_no_connection_even_when_replayed(self):
        self.assertFalse(WebsiteConnection.objects.filter(organization=self.org).exists())
        run = _draft(self.org, requested_by=self.owner.pk)
        with self.assertLogs("content_factory.founder_notification_outbox", "INFO") as logs:
            _sweep(times=2)
            # A worker that died after sending and before marking the row done.
            _rows().update(processed_at=None, claimed_at=None)
            _sweep()
        self.assertEqual(set(_rows().values_list("event_id", flat=True)),
                         {f"draft-ready:{self.org.pk}:{run.run_id}:email", f"draft-ready:{self.org.pk}:{run.run_id}:dm"})
        self.assertFalse(_rows().filter(processed_at__isnull=True).exists())
        keys = {key for _payload, key in self.providers.emails}
        self.assertEqual(keys, {f"articles-draft-ready/{run.run_id}"})
        self.assertEqual(len(self.providers.email_keys), 1)
        payload = self.providers.emails[0][0]
        self.assertEqual(payload["to"], [self.owner.email])
        self.assertEqual(payload["subject"], "Your draft is ready")
        self.assertTrue(payload["text"].startswith("Tap to review it and copy it to your website.\n\n"))
        self.assertEqual(payload["from"], "MLAI <notifications@example.test>")
        self.assertEqual(self.providers.messages(), [])  # no verified device: the DM is skipped
        self.assertTrue(any(f"founder_draft_ready_sent org={self.org.pk} run={run.run_id} channel=email" in line
                            for line in logs.output))

    @override_settings(**FLAGS)
    def test_the_link_holds_only_ids_and_starts_with_the_chat_frontend(self):
        run = _draft(self.org)
        _sweep()
        link = self.providers.emails[0][0]["text"].split("\n\n")[-1]
        self.assertEqual(link, f"https://chat.staging.example/my-startup/runs/{run.run_id}/draft?company_id={self.built.company.pk}")
        self.assertNotIn(self.owner.email, link)
        self.assertNotIn("@", link)
        self.assertNotIn("cohort", link)
        UUID(link.rsplit("=", 1)[1])

    @override_settings(**FLAGS)
    def test_the_dm_is_sent_once_to_verified_devices(self):
        self._device()
        run = _draft(self.org)
        _sweep(times=2)
        _rows().filter(event_id__endswith=":dm").update(processed_at=None, claimed_at=None)
        _sweep()
        messages = self.providers.messages()
        self.assertEqual(len({event["id"] for event in messages}), 1)
        self.assertIn(["h", self.providers.channel], messages[0]["tags"])
        self.assertIn(["p", DEVICE], messages[0]["tags"])
        self.assertIn(f"/my-startup/runs/{run.run_id}/draft?company_id=", messages[0]["content"])
        self.assertTrue(messages[0]["content"].startswith("Your draft is ready\n\nTap to review it"))

    @override_settings(**FLAGS)
    def test_the_run_starter_gets_it_and_the_owner_is_the_fallback(self):
        from core.models import User
        from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
        member = User.objects.create_user(email=f"member-{uuid4().hex[:6]}@synthetic.example.test")
        profile = VibeRaisingProfile.objects.create(user=member, role=VibeRaisingProfile.ROLE_FOUNDER)
        VibeRaisingCompany.objects.create(profile=profile, organization=self.org, name="Member", domain=self.org.domain)
        stranger = User.objects.create_user(email=f"stranger-{uuid4().hex[:6]}@synthetic.example.test")
        _draft(self.org, requested_by=member.pk)
        _draft(self.org, requested_by=stranger.pk)
        _sweep()
        self.assertEqual(sorted(payload["to"][0] for payload, _ in self.providers.emails), sorted([member.email, self.owner.email]))

    @override_settings(**FLAGS)
    def test_copied_posted_old_discarded_and_revision_runs_are_not_announced(self):
        copied, posted = _draft(self.org), _draft(self.org)
        _draft(self.org, created_at=timezone.now() - timedelta(hours=49))
        _draft(self.org, workflow="article_revision")
        _draft(self.org, status="running")
        discarded = _draft(self.org)
        ContentFactoryRun.objects.filter(pk=discarded.pk).update(result={"draft_deleted_at": timezone.now().isoformat()})
        config = OrganizationContentConfig.objects.get(organization=self.org)
        config.pillar_strategy = {**config.pillar_strategy, "founder_posted_articles": {
            copied.run_id: {"copiedAt": "2026-10-30T00:00:00+00:00", "copiedRevision": "r"},
            posted.run_id: {"url": "https://medium.com/@me/x", "check": "skipped"}}}
        config.save(update_fields=["pillar_strategy", "updated_at"])
        _sweep()
        self.assertFalse(_rows().exists())
        self.assertEqual(self.providers.emails, [])

    @override_settings(**FLAGS)
    def test_a_draft_copied_after_enqueue_is_not_sent(self):
        run = _draft(self.org)
        outbox.enqueue_ready_drafts()
        config = OrganizationContentConfig.objects.get(organization=self.org)
        config.pillar_strategy = {**config.pillar_strategy, "founder_posted_articles": {
            run.run_id: {"copiedAt": "2026-10-30T00:00:00+00:00", "copiedRevision": "r"}}}
        config.save(update_fields=["pillar_strategy", "updated_at"])
        outbox.drain()
        self.assertEqual(self.providers.emails, [])
        self.assertFalse(_rows().filter(processed_at__isnull=True).exists())

    @override_settings(**FLAGS)
    def test_a_transient_provider_failure_backs_off_and_a_permanent_one_stops(self):
        run = _draft(self.org)
        outbox.enqueue_ready_drafts()
        start = timezone.now()
        with mock.patch.object(transport, "post_webhook", return_value=WebhookResponse(503, b"{}")) as failing:
            self.assertEqual(outbox.drain(start)["retry"], 1)
            failing.reset_mock()
            # The next minute's tick does not pick the row again.
            outbox.drain(start + timedelta(minutes=1))
            failing.assert_not_called()
        email = _rows().get(event_id__endswith=":email")
        self.assertIsNone(email.processed_at)
        self.assertIsNotNone(email.claimed_at)
        outbox.drain(start + timedelta(minutes=1))
        self.assertEqual(self.providers.emails, [])
        outbox.drain(start + outbox.RETRY_MIN_DELAY + timedelta(seconds=1))
        self.assertEqual(len(self.providers.emails), 1)
        self.assertEqual(self.providers.emails[0][1], f"articles-draft-ready/{run.run_id}")
        other = _draft(self.org)
        outbox.enqueue_ready_drafts()
        with mock.patch.object(transport, "post_webhook", return_value=WebhookResponse(422, b"{}")):
            outbox.drain()
        self.assertIsNotNone(_rows().get(event_id=f"draft-ready:{self.org.pk}:{other.run_id}:email").processed_at)

    @override_settings(**FLAGS)
    def test_an_unexpected_error_also_backs_off(self):
        _draft(self.org)
        outbox.enqueue_ready_drafts()
        start = timezone.now()
        with mock.patch.object(outbox, "_send", side_effect=RuntimeError("boom")) as send:
            outbox.drain(start)
            calls = send.call_count
            outbox.drain(start + timedelta(minutes=1))
            self.assertEqual(send.call_count, calls)
        self.assertFalse(_rows().filter(processed_at__isnull=False).exists())

    def test_the_retry_delay_grows_with_the_row_age_between_its_bounds(self):
        now = timezone.now()
        row = ContentFactoryCallbackEvent(event_id="x", created_at=now)
        self.assertEqual(outbox.retry_delay(row, now), outbox.RETRY_MIN_DELAY)
        row.created_at = now - timedelta(minutes=40)
        self.assertEqual(outbox.retry_delay(row, now), timedelta(minutes=20))
        row.created_at = now - timedelta(hours=10)
        self.assertEqual(outbox.retry_delay(row, now), outbox.RETRY_MAX_DELAY)

    @override_settings(**FLAGS)
    def test_a_slow_drain_stops_claiming_rows_after_its_time_budget(self):
        _draft(self.org)
        _draft(self.org)
        outbox.enqueue_ready_drafts()
        ticks = iter([0, 0, outbox.DRAIN_SECONDS + 1] + [outbox.DRAIN_SECONDS + 1] * 10)
        result = outbox.drain(clock=lambda: next(ticks))
        self.assertEqual(result["deferred"], 3)
        self.assertEqual(_rows().filter(processed_at__isnull=True, claimed_at__isnull=True).count(), 3)
        outbox.drain()
        self.assertFalse(_rows().filter(processed_at__isnull=True).exists())
        self.assertEqual(len(self.providers.emails), 2)

    @override_settings(**FLAGS)
    def test_a_launch_window_marker_written_mid_run_does_not_hide_the_draft(self):
        from . import founder_journey_cohort as cohort
        built = make_new_journey_org(marker=False)
        self.assertTrue(cohort.is_new_journey(built.organization))
        run = _draft(built.organization, status="running", created_at=timezone.now() - timedelta(minutes=20))
        # The founder connects GitHub while the article writes: the first sticky key writes the marker.
        marker = cohort.update_marker(built.organization, github_return="connected")
        self.assertEqual(marker["assignedBy"], "launch_window")
        ContentFactoryRun.objects.filter(pk=run.pk).update(status="completed")
        _sweep()
        self.assertEqual({row.job_id for row in _rows()}, {run.run_id})
        self.assertEqual([payload["to"] for payload, _ in self.providers.emails], [[built.owner.email]])

    def test_nothing_is_enqueued_for_legacy_orgs_with_every_flag_on(self):
        for name in NAMED_LEGACY_ORGS:
            for marker in (True, False):
                with self.subTest(org=name, marker=marker), override_settings(**FLAGS):
                    built = make_named_legacy_org(name, marker=marker, domain=f"{name}-{uuid4().hex[:6]}.example.test",
                                                  owner_email=f"{uuid4().hex[:8]}@synthetic.example.test")
                    self._device(built.owner) if not CommunityChatDevice.objects.filter(public_key=DEVICE).exists() else None
                    _draft(built.organization, requested_by=built.owner.pk)
                    _draft(built.organization, status="awaiting_approval")
                    _sweep()
                    self.assertFalse(_rows().exists())
                    self.assertFalse(outbox.can_notify(built.organization, built.owner))
        self.assertEqual(self.providers.emails, [])
        self.assertEqual(self.providers.events, [])

    def test_nothing_is_enqueued_with_the_flag_or_v2_off(self):
        _draft(self.org)
        for flags in (FLAG_OFF, V2_OFF):
            with self.subTest(v2=flags["MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED"]), override_settings(**flags):
                self.assertEqual(outbox.enqueue_ready_drafts(), {"status": "disabled"})
                self.assertEqual(outbox.drain(), {"status": "disabled"})
                run_founder_journey_sweeps()
        self.assertFalse(_rows().exists())
        self.assertEqual(self.providers.emails, [])

    @override_settings(**FLAGS)
    def test_drafts_ready_before_the_org_joined_are_not_announced(self):
        built = make_named_legacy_org("talathrive", marker=True, domain=f"tt-{uuid4().hex[:6]}.example.test")
        before = _draft(built.organization, created_at=timezone.now() - timedelta(hours=2))
        from . import founder_journey_cohort as cohort
        cohort.opt_in(built.organization, built.owner)
        after = _draft(built.organization)
        _sweep()
        self.assertEqual({row.job_id for row in _rows()}, {after.run_id})
        self.assertNotIn(before.run_id, {row.job_id for row in _rows()})

    @override_settings(**FLAGS)
    def test_the_minute_sweeps_run_both_functions(self):
        _draft(self.org)
        result = run_founder_journey_sweeps()
        self.assertEqual(result["sweeps"]["content_factory.founder_notification_outbox:enqueue_ready_drafts"], "completed")
        self.assertEqual(result["sweeps"]["content_factory.founder_notification_outbox:drain"], "completed")
        self.assertEqual(len(self.providers.emails), 1)

    def test_can_notify_truth_table(self):
        legacy = make_named_legacy_org("vyavos", marker=True, domain=f"vy-{uuid4().hex[:6]}.example.test")
        no_email = type("NoEmail", (), {"email": ""})()
        cases = (
            (FLAGS, self.org, self.owner, True),
            (FLAG_OFF, self.org, self.owner, False),
            (V2_OFF, self.org, self.owner, False),
            ({**FLAGS, "RESEND_API_KEY": ""}, self.org, self.owner, False),
            (FLAGS, self.org, no_email, False),
            (FLAGS, self.org, None, False),
            (FLAGS, legacy.organization, legacy.owner, False),
            (FLAGS, None, self.owner, False),
        )
        for flags, org, user, expected in cases:
            with self.subTest(org=getattr(org, "domain", None), user=getattr(user, "email", None), flags=flags is FLAGS), \
                    override_settings(**flags):
                self.assertIs(outbox.can_notify(org, user), expected)

    def test_every_event_id_is_under_100_characters(self):
        for run_id in ("run-abc", str(uuid4()), "r" * 100):
            for channel in outbox.CHANNELS:
                with self.subTest(run_id=run_id[:10], channel=channel):
                    value = outbox.event_id(2_147_483_647, run_id, channel)
                    self.assertLess(len(value), 100)
                    self.assertTrue(value.startswith("draft-ready:") and value.endswith(":" + channel))
        self.assertEqual(outbox.event_id(7, "run-abc", "email"), "draft-ready:7:run-abc:email")
