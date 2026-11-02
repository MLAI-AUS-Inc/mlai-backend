"""Real PostgreSQL ledger/rights tests with all outbound delivery mocked."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from core.models import User
from . import founder_journey as journeys, founder_journey_p2 as projection, founder_transitions as transitions
from . import founder_notification_outbox as drafts, website_notification_transport as transport
REAL_POST_EMAIL = transport.post_email
from .models import ContentFactoryCallbackEvent, OrganizationContentConfig
from .self_heal_testing import ALL_FLAGS_ON, ALL_P1_FLAGS_ON, make_p2_org, make_named_legacy_org, make_stamped_run, fake_member_context, fake_provider
from .website_models import WebsiteConnection


FLAGS = {**ALL_FLAGS_ON, "RESEND_API_KEY": "synthetic", "COMMUNITY_CHAT_FRONTEND_URL": "https://chat.mlai.au",
    "ARTICLES_FREE_FIRST_ARTICLE_DAILY_CAP": 0, "ARTICLES_FREE_FIRST_ARTICLE_SPIKE_THRESHOLD": 0}


@override_settings(**FLAGS)
class NotificationTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        cache.clear()
        self.fixture = make_p2_org()
        self.org, self.owner, self.company = self.fixture.organization, self.fixture.owner, self.fixture.company
        self.owner.email_verified_at = timezone.now()
        self.owner.first_name = "Pat"
        self.owner.save(update_fields=["email_verified_at", "first_name"])
        self.now = timezone.now()
        self.sent = []
        self.enterContext(patch.object(transport, "post_email", side_effect=self.send))
        self.enterContext(patch.object(drafts, "_devices", return_value=[]))
        self.enterContext(patch.object(journeys, "terms_out_of_date", return_value=False))
        self.enterContext(patch.object(journeys, "_website_journey_facts", return_value={}))
        self.enterContext(patch.object(journeys, "article_price_and_balance", return_value=(12, 20)))

    def send(self, payload, key, **kwargs):
        self.assertFalse(connection.in_atomic_block)
        self.assertTrue(kwargs.get("exact_key"))
        self.assertLessEqual(len(key), 256)
        self.sent.append((deepcopy(payload), key))
        return "synthetic-message"

    def make_run(self, result=None, *, status="blocked", workflow="article_generation"):
        return make_stamped_run(self.org, status=status, workflow=workflow, result=result or {}, run_request={"delivery_mode": "review_draft"})

    def reserve(self, run, *, event=None, ask="review_draft", item=None):
        from organizations.models import Organization
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=self.org.pk)
            config = OrganizationContentConfig.objects.select_for_update().get(pk=self.fixture.config.pk)
            config.organization = self.org
            state = {"state": "your_turn", "ask": ask, "run_id": run.run_id, "run": run}
            if event:return transitions._reserve(config, item or "article:" + run.run_id, event, state, now=self.now)
            return transitions.observe(config, item or "article:" + run.run_id, state, now=self.now)

    def ready(self):
        return self.make_run({"draft_ready_v1": {"version": 1, "at": self.now.isoformat(), "revision": "review-1"}}, status="running")

    def test_two_members_get_individual_keys_and_no_cross_recipient_email(self):
        a = User.objects.create_user(email="member-a@synthetic.example", email_verified_at=self.now)
        b = User.objects.create_user(email="member-b@synthetic.example", email_verified_at=self.now)
        run = self.ready()
        with fake_member_context(members=(a.pk, b.pk), owner_ids=(self.owner.pk,)):
            self.assertEqual(self.reserve(run), 6)
            self.assertEqual(self.reserve(run), 0)
            self.assertEqual(transitions.drain(self.now)["sent"], 3)
            self.assertEqual(transitions.drain(self.now)["sent"], 0)
        self.assertEqual(len({key for _, key in self.sent}), 3)
        self.assertEqual({p["to"][0] for p, _ in self.sent}, {self.owner.email, a.email, b.email})
        for payload, key in self.sent:
            self.assertEqual(len(payload["to"]), 1)
            self.assertNotIn("cc", payload)
            self.assertNotIn("bcc", payload)
            for email in {self.owner.email, a.email, b.email} - set(payload["to"]):
                self.assertNotIn(email, json.dumps(payload))

    def test_removed_member_is_dropped_at_send_time_and_owner_ask_is_owner_only(self):
        member = User.objects.create_user(email="removed@synthetic.example", email_verified_at=self.now)
        run = self.ready()
        with fake_member_context(members=(member.pk,), owner_ids=(self.owner.pk,)):
            self.reserve(run)
        with fake_member_context(owner_ids=(self.owner.pk,)):
            transitions.drain(self.now)
        self.assertEqual([payload["to"] for payload, _ in self.sent], [[self.owner.email]])
        self.sent.clear()
        with fake_member_context(members=(member.pk,), owner_ids=(self.owner.pk,)):
            self.assertEqual(self.reserve(run, ask="approve_setup", item="website:" + str(self.org.pk)), 2)

    def test_unverified_email_is_skipped_and_p1_requester_does_not_fall_back_after_removal(self):
        run = self.ready()
        self.reserve(run)
        self.owner.email_verified_at = None
        self.owner.save(update_fields=["email_verified_at"])
        transitions.drain(self.now)
        self.assertEqual(self.sent, [])
        member = User.objects.create_user(email="former@synthetic.example", email_verified_at=self.now)
        run.run_request["roo_points_requested_by_user_id"] = member.pk
        self.assertEqual(drafts.recipient(run, self.org), (None, None))

    def test_reminders_are_once_at_24_and_72_hours_and_reproject_before_send(self):
        run = self.ready()
        self.reserve(run)
        transitions.drain(self.now)
        self.fixture.config.refresh_from_db()
        with transaction.atomic():
            config = OrganizationContentConfig.objects.select_for_update().get(pk=self.fixture.config.pk)
            config.organization = self.org
            state = {"state": "your_turn", "ask": "review_draft", "run_id": run.run_id, "run": run}
            self.assertEqual(transitions.observe(config, "article:" + run.run_id, state, now=self.now + timedelta(hours=24)), 2)
            self.assertEqual(transitions.observe(config, "article:" + run.run_id, state, now=self.now + timedelta(hours=72)), 2)
            self.assertEqual(transitions.observe(config, "article:" + run.run_id, state, now=self.now + timedelta(hours=73)), 0)
        run.status = "cancelled"
        run.save(update_fields=["status"])
        for row in ContentFactoryCallbackEvent.objects.filter(event_type=transitions.EVENT_TYPE, processed_at__isnull=True):
            ContentFactoryCallbackEvent.objects.filter(pk=row.pk).update(created_at=row.emitted_at)
        transitions.drain(self.now + timedelta(hours=73))
        self.assertEqual(len(self.sent), 1)

    def test_lost_provider_ack_retries_exact_frozen_payload_and_key(self):
        run = self.ready()
        self.reserve(run)
        attempts = []
        def lost(payload, key, **kwargs):
            attempts.append((deepcopy(payload), key))
            if len(attempts) == 1:raise transport.NotificationTransportError("email_response_unavailable")
        with patch.object(transport, "post_email", side_effect=lost):
            self.assertEqual(transitions.drain(self.now)["retry"], 1)
            self.assertEqual(transitions.drain(self.now + timedelta(minutes=3))["sent"], 1)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(ContentFactoryCallbackEvent.objects.filter(event_type=transitions.EVENT_TYPE, processed_at__isnull=True).count(), 0)

    def test_lost_dm_ack_replays_the_same_saved_signed_event_and_rechecks_devices(self):
        from agent_runtime import native_crypto
        from uuid import uuid4
        run = self.ready()
        self.reserve(run)
        key = native_crypto.signing_key("01" * 32)
        device = native_crypto.public_key(native_crypto.signing_key("02" * 32))
        events = []
        def post(event):
            self.assertFalse(connection.in_atomic_block)
            events.append(deepcopy(event))
            if len(events) == 1:raise transport.NotificationTransportError("chat_response_unavailable")
        with patch.object(drafts, "_devices", return_value=[device]), patch.object(transport, "notifier_key", return_value=key), patch.object(transport, "open_dm", return_value=str(uuid4())), patch.object(transport, "post_event", side_effect=post):
            self.assertEqual(transitions.drain(self.now)["retry"], 1)
            self.assertEqual(transitions.drain(self.now + timedelta(minutes=3))["sent"], 1)
        self.assertEqual(events[0], events[1])

    def test_exact_provider_key_bounded_representations_and_frozen_email_change(self):
        full = transitions.logical_key(10**19, "article:" + "r" * 100, "your_turn_reminder_72h", "approve_on_github", 10**19, "email")
        self.assertGreater(len(full), 100)
        self.assertEqual(transitions.bounded_key(full, 100), transitions.bounded_key(full, 100))
        self.assertLessEqual(len(transitions.bounded_key(full + "x" * 200, 256)), 256)
        run = self.ready()
        self.reserve(run)
        with patch.object(transport, "post_email", side_effect=transport.NotificationTransportError("email_response_unavailable")):
            transitions.drain(self.now)
        self.owner.email = "changed@synthetic.example"
        self.owner.save(update_fields=["email"])
        transitions.drain(self.now + timedelta(minutes=3))
        self.assertEqual(self.sent, [])

    def test_journey_precedence_rules_a_to_j_and_completed_channel_evidence(self):
        cases = [
            ({"error_code": "UNREGISTERED_EXAMPLE"}, "failed", "working"),
            ({"strict_pass_v1": {"claims": 1}}, "running", "ask_check_highlighted"),
            ({"draft_ready_v1": {"version": 1, "at": self.now.isoformat(), "revision": "r1"}}, "running", "ask_review_draft"),
            ({"error_code": "SITE_TEST_CHANGE_REQUIRED"}, "blocked", "ask_approve_on_github_site_test"),
            ({"error_code": "BRANCH_PROTECTION_APPROVAL_REQUIRED"}, "blocked", "ask_approve_on_github_protected"),
            ({"error_code": "DEPLOYMENT_NOT_DETECTED"}, "completed", "working_website_update"),
            ({"publish_pr_v1": {"kind": "article"}}, "completed", "working_publishing"),
            ({"error_code": "VALIDATION_ERROR", "self_heal_v1": {"capped": True}, "points_v1": {"state": "refunded", "points": 12}}, "failed", "stopped"),
        ]
        for result, status, copy_id in cases:
            run = self.make_run(result, status=status)
            decision = projection.run_decision(self.org, run, config=self.fixture.config, user=self.owner, now=self.now)
            self.assertEqual(decision["copyId"], copy_id, result)
        run = self.make_run(status="queued")
        decision = projection.run_decision(self.org, run, config=self.fixture.config, user=self.owner, now=self.now, queue={"queuePosition": 2, "etaSeconds": 180})
        self.assertEqual(decision["copyId"], "working_queue")
        self.assertIn("2nd", decision["body"])
        run = self.make_run({"channel_v1": {"version": 1, "channel": "copy_ready", "waiting_to_publish": True, "dispatch_state": "accepted"}}, status="completed")
        self.assertIsNone(projection.run_decision(self.org, run, config=self.fixture.config, user=self.owner, now=self.now))
        run.result.update(channel_switch_cf_v1={"version": 1, "run_id": run.run_id, "key": run.run_id + ":copy-ready", "delivery_mode": "content_only", "state": "completed"}, content_package={"article_markdown": "Exact saved body"})
        self.assertEqual(projection.run_decision(self.org, run, config=self.fixture.config, user=self.owner, now=self.now)["copyId"], "done_copy_ready_strategy")

    def test_revocation_live_deploy_claims_and_developer_note_are_safe(self):
        from .website_failure_contract import load_registry
        revoked = next(code for code, value in load_registry()["by_code_v2"].items() if value.get("founder_copy_id") == "ask_authorize_github_revoked")
        run = self.make_run({"error_code": revoked})
        self.assertEqual(projection.run_decision(self.org, run, config=self.fixture.config, user=self.owner, now=self.now)["copyId"], "ask_authorize_github_revoked")
        run.result = {"deploy_v1": {"live_at": self.now.isoformat(), "url": "https://public.example/blog/article", "provider": "vercel"}}
        self.assertEqual(projection.run_decision(self.org, run, config=self.fixture.config, user=self.owner, now=self.now)["state"], "done")
        run.result = {"failing_check": "https://internal.example/token=secret", "failure_fingerprint": "member@example.test", "pr_url": "https://github.com/org/repo/pull/1?token=secret"}
        note = projection.developer_note(self.org, run)
        self.assertNotIn("secret", json.dumps(note))
        self.assertNotIn("@", json.dumps(note))
        self.assertIsNone(projection.claims_count(run))

    def test_team_pending_owner_member_fields_connected_by_and_staff_legacy_exception(self):
        self.owner.is_staff = True
        self.owner.save(update_fields=["is_staff"])
        website = WebsiteConnection.objects.create(organization=self.org, repository_id=42, github_repo="fixture/site", installation_id="99", authorized_by=self.owner)
        self.fixture.config.website_connection = website
        self.fixture.config.save(update_fields=["website_connection"])
        with fake_provider("founder_tools.startup_team", viewer_role=lambda user, org: "owner", pending_request_count=lambda org: 2):
            body = journeys.project_founder_journey(self.org, user=self.owner, company=self.company, now=self.now)
        self.assertEqual(body["copyId"], "ask_approve_join_request")
        self.assertEqual(body["team"], {"pendingRequests": 2})
        self.assertEqual(body["website"]["connectedBy"], {"firstName": "Pat"})
        self.assertIs(body["staffConsole"], True)
        with fake_member_context(members=(self.owner.pk,)):
            body = journeys.project_founder_journey(self.org, user=self.owner, company=self.company, now=self.now)
        self.assertEqual(body["viewerRole"], "member")
        self.assertNotIn("team", body)

    def test_legacy_payload_is_byte_identical_with_all_p2_flags_and_no_sends(self):
        for name in ("la_vie", "talathrive", "vyavos", "pawshare", "mlai"):
            legacy = make_named_legacy_org(name)
            with override_settings(**{key: False for key in ALL_FLAGS_ON if key not in ALL_P1_FLAGS_ON}):
                before = journeys.project_founder_journey(legacy.organization, user=legacy.owner, company=legacy.company)
            after = journeys.project_founder_journey(legacy.organization, user=legacy.owner, company=legacy.company)
            self.assertEqual(before, after)
            self.assertFalse(transitions.owns_notifications(legacy.organization))

    def test_flagoff_and_two_actual_claims_cannot_send_twice(self):
        with override_settings(MY_STARTUP_TRANSITION_NOTIFICATIONS_ENABLED=False):
            self.assertEqual(transitions.run_transition_sweep(), {"skipped": "flag_off"})
        run = self.ready()
        self.reserve(run)
        row = ContentFactoryCallbackEvent.objects.filter(event_type=transitions.EVENT_TYPE, event_id__endswith=":email").first()
        ContentFactoryCallbackEvent.objects.filter(pk=row.pk).update(claimed_at=self.now)
        self.assertEqual(transitions.drain(self.now)["sent"], 0)
        self.assertEqual(transitions.drain(self.now + timedelta(minutes=11))["sent"], 1)

    def test_p2_ready_receipt_is_announceable_and_draft_export_can_precede_preview(self):
        from .founder_draft_views import founder_draft_review
        run = self.ready()
        self.assertTrue(drafts._announceable(run, self.now))
        kwargs, founder = founder_draft_review(context=SimpleNamespace(organization=self.org), run=run, user=self.owner)
        self.assertTrue(kwargs["allow_preview_pending"])
        self.assertTrue(founder["copyWhilePreviewPending"])
        run.result["draft_ready_v1"]["at"] = "not-a-time"
        self.assertFalse(drafts._announceable(run, self.now))

    def test_website_without_cf_run_uses_current_revocation_and_verified_target(self):
        from .website_models import WebsiteConnectionTarget
        website = WebsiteConnection.objects.create(organization=self.org, repository_id=42, github_repo="fixture/site", installation_id="99", authorized_by=self.owner, state="revoked")
        self.fixture.config.website_connection = website
        self.fixture.config.save(update_fields=["website_connection"])
        transitions.run_transition_sweep(now=timezone.now(), limit=500)
        self.assertEqual(self.sent[-1][0]["subject"], "MLAI needs access to your website on GitHub")
        website.state = "connected"
        website.save(update_fields=["state"])
        WebsiteConnectionTarget.objects.create(connection=website, generation=website.generation,
            target_key="blog", verified_at=timezone.now())
        transitions.run_transition_sweep(now=timezone.now(), limit=500)
        self.assertEqual(self.sent[-1][0]["subject"], "Your blog page is live")

    def test_actual_transport_uses_exact_key_without_legacy_prefix(self):
        from agent_runtime.webhook_transport import WebhookResponse
        with patch.object(transport, "post_webhook", return_value=WebhookResponse(200, b'{"id":"12345678-1234-1234-1234-123456789012"}')) as post:
            REAL_POST_EMAIL({"to": [self.owner.email]}, "founder-transition:exact", exact_key=True)
        self.assertEqual(post.call_args.args[2]["Idempotency-Key"], "founder-transition:exact")

    def test_reviewed_child_live_projection_keeps_original_article_identity(self):
        source = self.ready()
        digest = "a" * 64
        child = self.make_run({"article_approval_v1": {"source": "publish_reviewed", "approved_body_sha256": digest},
            "publish_reviewed_v1": {"source_run_id": source.run_id, "approved_body_sha256": digest}}, status="running")
        child.run_request.update(source_run_id=source.run_id, approved_body_sha256=digest)
        child.save(update_fields=["run_request"])
        source.result["publish_offer_v1"] = {"run_id": child.run_id}
        decision = projection.run_decision(self.org, source, config=self.fixture.config, user=self.owner, now=self.now)
        self.assertEqual(decision["copyId"], "working_publishing")
        child.result["deploy_v1"] = {"live_at": self.now.isoformat(), "url": "https://" + self.org.domain + "/blog/article"}
        child.save(update_fields=["result"])
        decision = projection.run_decision(self.org, source, config=self.fixture.config, user=self.owner, now=self.now)
        self.assertEqual((decision["headline"], decision["runId"]), ("Your article is live!", source.run_id))
        child.run_request["source_run_id"] = "foreign"
        child.save(update_fields=["run_request"])
        self.assertEqual(projection.run_decision(self.org, source, config=self.fixture.config, user=self.owner, now=self.now)["copyId"], "ask_review_draft")

    def test_actual_concurrent_drains_claim_each_recipient_once(self):
        self.reserve(self.ready())
        def drain():
            close_old_connections()
            try:return transitions.drain(self.now)
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: drain(), range(2)))
        self.assertEqual(sum(result["sent"] for result in results), 1)
        self.assertEqual(len(self.sent), 1)

    def test_copy_ready_done_never_sends_article_live_and_real_deploy_does(self):
        run = self.ready()
        with transaction.atomic():
            config = OrganizationContentConfig.objects.select_for_update().get(pk=self.fixture.config.pk)
            config.organization = self.org
            state = {"state": "done", "ask": None, "copyId": "done_copy_ready_strategy", "run": run, "run_id": run.run_id}
            self.assertEqual(transitions.observe(config, "article:" + run.run_id, state, now=self.now), 0)
            run.result["deploy_v1"] = {"url": "https://" + self.org.domain + "/blog/article", "live_at": self.now.isoformat()}
            self.assertEqual(transitions.observe(config, "article:" + run.run_id, state, now=self.now), 2)

    def test_join_seams_send_only_current_member_or_owner_and_reminders_use_request_identity(self):
        member = User.objects.create_user(email="joined@synthetic.example", first_name="Jo", email_verified_at=self.now)
        with fake_member_context(members=(member.pk,), owner_ids=(self.owner.pk,)):
            self.assertEqual(transitions.notify_join_approved(self.org, member_user=member), 2)
            self.assertEqual(transitions.notify_member_joined(self.org, member_user=member), 2)
            transitions.drain(timezone.now())
        self.assertEqual({payload["to"][0] for payload, _ in self.sent}, {member.email, self.owner.email})

    def test_five_hundred_dormant_orgs_add_no_per_org_scan_cost(self):
        from organizations.models import Organization
        orgs = Organization.objects.bulk_create([Organization(name="Synthetic dormant", domain=f"dormant-{index}.synthetic.test") for index in range(500)])
        OrganizationContentConfig.objects.bulk_create([OrganizationContentConfig(organization=org) for org in orgs])
        started = time.monotonic()
        result = transitions.run_transition_sweep(now=self.now, limit=500)
        elapsed = time.monotonic() - started
        self.assertEqual(result["checked"], 0)
        self.assertLess(elapsed, 2.0)
        from .self_heal_sweeps import run_self_heal_sweeps
        started = time.monotonic()
        combined = run_self_heal_sweeps(now=self.now)
        elapsed = time.monotonic() - started
        self.assertNotIn("missing", combined.values())
        self.assertNotIn("error", combined.values())
        self.assertLess(elapsed, 2.0)

    def test_full_tick_at_five_hundred_pending_items_is_bounded(self):
        from organizations.models import Organization
        from workflow_runs.models import ContentFactoryRun
        from .self_heal_sweeps import run_self_heal_sweeps
        orgs = Organization.objects.bulk_create([Organization(name="Synthetic pending", domain=f"pending-{index}.synthetic.test") for index in range(500)])
        self.fixture.config.refresh_from_db()
        OrganizationContentConfig.objects.bulk_create([OrganizationContentConfig(organization=org,
            pillar_strategy=deepcopy(self.fixture.config.pillar_strategy)) for org in orgs])
        ContentFactoryRun.objects.bulk_create([ContentFactoryRun(organization=org, domain=org.domain,
            run_id=f"pending-{org.pk}", workflow="article_generation", status="blocked",
            run_request={"self_heal_policy_v1": {"version": 1, "cohort": "p2", "failure_policy": "v2"}},
            result={"error_code": "UNKNOWN_SYNTHETIC_FAILURE"}) for org in orgs])
        started = time.monotonic()
        from . import self_heal_sweeps
        timings = {}
        original = self_heal_sweeps._runner
        def measured(module, function):
            runner = original(module, function)
            if runner is None:return None
            def invoke(*args, **kwargs):
                before = time.monotonic()
                value = runner(*args, **kwargs)
                timings[module + "." + function] = time.monotonic() - before
                return value
            return invoke
        with patch.object(self_heal_sweeps, "_runner", side_effect=measured):
            result = run_self_heal_sweeps(now=self.now)
        elapsed = time.monotonic() - started
        print("p2_tick_benchmark " + json.dumps({"seconds": elapsed, "timings": timings}, sort_keys=True))
        self.assertNotIn("missing", result.values())
        self.assertNotIn("error", result.values())
        self.assertLess(elapsed, 2.0, timings)

    @override_settings(MY_STARTUP_TEAM_DOMAIN_AUTO_JOIN_ENABLED=False)
    def test_real_team_request_identity_approval_and_removed_recipient_composition(self):
        from founder_tools import startup_team as team
        member = User.objects.create_user(email="actual@member.synthetic.test", first_name="Jo", email_verified_at=self.now)
        reference = team.issue_join_reference(user=member, organization=self.org)
        team.request_join(user=member, join_reference=reference)
        identifiers = team.pending_request_ids(self.org)
        self.assertEqual(len(identifiers), 1)
        transitions.run_transition_sweep(now=timezone.now(), limit=500)
        self.assertEqual([p["to"] for p, _ in self.sent], [[self.owner.email]])
        team.decide_request(organization=self.org, user=self.owner, request_id=identifiers[0], decision="approve")
        transitions.drain(timezone.now())
        self.assertEqual(self.sent[-1][0]["to"], [member.email])
        self.sent.clear()
        run = self.ready()
        self.assertEqual(self.reserve(run), 4)
        config = OrganizationContentConfig.objects.get(organization=self.org)
        row = next(row for row in config.pillar_strategy["startup_team"]["members"] if row["user_id"] == member.pk)
        team.update_member(organization=self.org, user=self.owner, member_id=row["memberId"], remove=True)
        transitions.drain(timezone.now())
        self.assertEqual([p["to"] for p, _ in self.sent], [[self.owner.email]])
