"""Readiness recheck rules against a test database: the TalaThrive push scenario end to end."""
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from . import website_readiness as readiness
from . import website_readiness_rules as rules
from . import website_reconciliation as reconciliation
from . import website_source_carry as carry
from .tests_website_source_carry import CarriedSite, _compare
from .website_models import WebsiteCheckEvent, WebsiteConnectionOperation, WebsiteReadiness
from .website_prepare import wake_prepare_for_source as REAL_WAKE  # before the fixture patches it

VERIFIED = "e204852e" + "0" * 32
PUSHED = "14507e59" + "0" * 32
TALA_SETUP = {"setup_run_id": "e3f55f7e", "route_path": "/stories", "route_shell_path": "stories/index.html",
              "data_store_path": "data/articles.json",
              "managed_files": ["scripts/publish_articles.py", "assets/js/articles.js", "scripts/templates/publication.html"]}
RULES = {"ARTICLES_READINESS_RECHECK_RULES_ENABLED": True, "ARTICLES_READINESS_DOMAINS": "carry.example.test",
         "ARTICLES_READY_ON_MERGE_ENABLED": True, "ARTICLES_PROOF_CARRY_FORWARD_ENABLED": True,
         "ARTICLES_READINESS_SHADOW_ENABLED": True}
PASSED = {key: {"status": "passed", "detail": "ok"} for key in ("blog_files", "build_render", "live_page")}


class TalaThriveSite(CarriedSite):
    """A static site verified at e204852e with its pinned footprint and a ready readiness record."""

    def setUp(self):
        super().setUp()
        cache.delete(rules.LOOK_THROTTLE_KEY)
        self.connection.verified_sha = VERIFIED
        self.connection.save(update_fields=["verified_sha"])
        self.target.source_sha = VERIFIED
        self.target.save(update_fields=["source_sha"])
        self.config.article_system_setup_cache = TALA_SETUP
        self.config.save(update_fields=["article_system_setup_cache"])
        with override_settings(**RULES):
            readiness.record(self.connection, trigger="backfill")
        row = WebsiteReadiness.objects.get(connection=self.connection)
        # The 17 Oct setup run is the pin (no ContentFactoryRun row in this test database).
        row.pinned_contract = {**row.pinned_contract, "setupRunId": "e3f55f7e", "adapterMode": "model_synthesized"}
        row.save(update_fields=["pinned_contract"])

    def push_to(self, comparison, sha=PUSHED):
        with patch.object(carry, "compare_sources", return_value=comparison) as compare, \
                patch("content_factory.website_prepare.wake_prepare_for_source") as wake, \
                patch("content_factory.vibe_marketing_views._queue_content_factory_run") as dispatch:
            result = reconciliation.handle_website_github_event("push", {
                "repository": {"id": 777}, "ref": "refs/heads/main", "after": sha})
        self.connection.refresh_from_db()
        self.target.refresh_from_db()
        return result, compare, wake, dispatch

    def row(self):
        return WebsiteReadiness.objects.get(connection=self.connection)

    def checks_op(self, sha=PUSHED):
        return WebsiteConnectionOperation.objects.get(connection=self.connection, action="source-reverify", payload__source_sha=sha)

    def process(self, op, checks):
        with patch.object(rules, "run_checks", return_value=checks):
            return reconciliation._process_source_reverification(op.pk, timezone.now() + timedelta(minutes=2))


@override_settings(**RULES)
class TalaThrivePushTests(TalaThriveSite, TestCase):
    def test_a_push_touching_only_non_footprint_files_stays_ready(self):
        self.assertEqual(self.row().state, "ready")
        result, compare, wake, dispatch = self.push_to(_compare("index.html", "about/index.html"))
        compare.assert_called_once()
        self.assertEqual(result["push_no_footprint"], 1)
        self.assertEqual((self.connection.verified_sha, self.target.source_sha), (PUSHED, PUSHED))
        self.assertTrue(self.connection.capabilities["publishingReady"])
        self.assertFalse([item for item in self.connection.blockers if item.get("code") == "repository_source_changed"])
        self.assertFalse(WebsiteConnectionOperation.objects.filter(connection=self.connection, action__in=["source-reverify", "prepare"]).exists())
        wake.assert_not_called()
        dispatch.assert_not_called()
        row = self.row()
        self.assertEqual((row.state, row.can_publish, row.verified_sha), ("ready", True, PUSHED))
        event = WebsiteCheckEvent.objects.filter(connection=self.connection).order_by("-created_at", "-pk").first()
        self.assertEqual((event.trigger, event.state_after), ("push_no_footprint", "ready"))

    def test_no_footprint_carries_even_without_mlai_build_proof(self):
        with override_settings(ARTICLES_READY_ON_MERGE_ENABLED=False):
            self.assertFalse(carry.carriable(self.connection, self.target))
            result, *_ = self.push_to(_compare("index.html"))
        self.assertEqual(result["push_no_footprint"], 1)
        self.assertEqual(self.connection.verified_sha, PUSHED)

    def test_touching_the_listing_page_checks_without_a_setup(self):
        result, _, wake, dispatch = self.push_to(_compare("index.html", "stories/index.html"))
        self.assertEqual(result["push_footprint"], 1)
        wake.assert_not_called()
        dispatch.assert_not_called()
        self.assertEqual(self.connection.verified_sha, VERIFIED)
        self.assertFalse(self.connection.capabilities["publishingReady"])
        op = self.checks_op()
        self.assertTrue(op.payload["verify_only"])
        self.assertFalse(op.payload["scan_required"])
        self.assertEqual(op.payload["changed_files"], ["stories/index.html"])
        row = self.row()
        self.assertEqual((row.state, row.can_draft, row.can_publish), ("checking", True, False))
        self.assertEqual(row.recheck["sourceSha"], PUSHED)
        event = WebsiteCheckEvent.objects.filter(connection=self.connection).order_by("-created_at", "-pk").first()
        self.assertEqual((event.trigger, event.changed_files), ("push_footprint", ["stories/index.html"]))
        self.assertFalse(WebsiteConnectionOperation.objects.filter(connection=self.connection, action="prepare").exists())

    def test_a_passed_verify_only_check_moves_ready_forward(self):
        self.push_to(_compare("stories/index.html"))
        with patch("content_factory.vibe_marketing_views._queue_content_factory_run") as dispatch:
            self.assertEqual(self.process(self.checks_op(), PASSED), "completed")
        dispatch.assert_not_called()
        self.connection.refresh_from_db()
        self.target.refresh_from_db()
        self.assertEqual((self.connection.verified_sha, self.target.source_sha), (PUSHED, PUSHED))
        self.assertTrue(self.connection.capabilities["publishingReady"])
        self.assertFalse([item for item in self.connection.blockers if item.get("code") == "repository_source_changed"])
        row = self.row()
        self.assertEqual((row.state, row.can_publish), ("ready", True))
        self.assertEqual(row.checks["build_render"]["status"], "passed")
        self.assertEqual(self.checks_op().receipt["method"], "verify_only")

    def test_a_failed_verify_only_check_is_fixing_with_drafts_open_and_no_setup(self):
        self.push_to(_compare("stories/index.html"))
        failed = {**PASSED, "blog_files": {"status": "failed", "detail": "missing"}}
        with patch("content_factory.vibe_marketing_views._queue_content_factory_run") as dispatch:
            self.assertEqual(self.process(self.checks_op(), failed), "needs_attention")
        dispatch.assert_not_called()
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.verified_sha, VERIFIED)
        self.assertFalse(self.connection.capabilities["publishingReady"])
        row = self.row()
        self.assertEqual((row.state, row.can_draft, row.can_publish), ("fixing", True, False))

    def test_an_unproven_check_retries(self):
        self.push_to(_compare("stories/index.html"))
        unproven = {key: {"status": "not_run"} for key in PASSED}
        self.assertEqual(self.process(self.checks_op(), unproven), "pending")
        self.assertEqual(self.row().state, "checking")

    def test_prepare_does_not_adopt_a_verify_only_check(self):
        from .website_prepare import _adopt_source_checks
        self.push_to(_compare("stories/index.html"))
        legacy = WebsiteConnectionOperation.objects.create(connection=self.connection, generation=self.connection.generation,
            action="source-reverify", idempotency_key="legacy-check", payload={"source_sha": "c" * 40})
        _adopt_source_checks(self.connection, legacy)
        self.assertEqual(self.checks_op().state, "pending")
        legacy.refresh_from_db()
        self.assertEqual(legacy.receipt["status"], "managed_by_prepare")
        self.assertTrue(rules.holds_prepare(self.connection))

    def test_rules_off_keeps_todays_lock(self):
        with override_settings(ARTICLES_READINESS_RECHECK_RULES_ENABLED=False):
            result, _, wake, _ = self.push_to(_compare("stories/index.html"))
        self.assertNotIn("push_footprint", result)
        wake.assert_called_once()
        self.assertFalse(self.checks_op().payload.get("verify_only"))


@override_settings(**RULES)
class TalaThriveRecheckTriggerTests(TalaThriveSite, TestCase):
    def test_age_alone_is_not_stale_under_the_rules(self):
        from .activation import durable_integration_evidence
        old = timezone.now() - timedelta(days=12)
        self.connection.last_verified_at = old
        self.connection.save(update_fields=["last_verified_at"])
        self.target.verified_at = old
        self.target.save(update_fields=["verified_at"])
        self.assertTrue(durable_integration_evidence(self.config, self.connection)["verified"])
        with override_settings(ARTICLES_READINESS_RECHECK_RULES_ENABLED=False):
            self.assertEqual(durable_integration_evidence(self.config, self.connection)["reasonCode"], "verification_stale")

    def test_manual_check_is_one_per_commit_per_hour(self):
        self.config.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            first = rules.request_manual_check(self.config)
            second = rules.request_manual_check(self.config)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual((first.payload["trigger"], first.payload["source_sha"]), ("manual", VERIFIED))
        self.assertTrue(WebsiteCheckEvent.objects.filter(connection=self.connection, trigger="manual").exists())

    def test_delivery_failure_queues_once_per_commit(self):
        from .website_contract import WebsiteAuthorityError
        refused = WebsiteAuthorityError("website_source_changed", "moved")
        for _ in range(2):
            with self.captureOnCommitCallbacks(execute=True):
                rules.queue_after_delivery_failure(self.connection, refused)
        ops = WebsiteConnectionOperation.objects.filter(connection=self.connection, payload__trigger="delivery_failure")
        self.assertEqual(ops.count(), 1)
        self.assertEqual(WebsiteCheckEvent.objects.filter(connection=self.connection, trigger="delivery_failure").count(), 1)

    def test_two_failed_weekly_looks_queue_a_verify_only_check(self):
        now = timezone.now()
        with patch.object(rules, "look", return_value=(False, {"live_page": {"status": "failed"}})):
            for offset in (0, 8):
                cache.delete(rules.LOOK_THROTTLE_KEY)
                WebsiteReadiness.objects.filter(connection=self.connection).update(state="ready")
                counts = rules.run_weekly_look_sweep(now=now + timedelta(days=7 + offset))
        self.assertEqual(counts["queued"], 1)
        op = WebsiteConnectionOperation.objects.get(connection=self.connection, payload__trigger="weekly_look")
        self.assertTrue(op.payload["verify_only"])
        self.assertEqual(self.row().last_look["failures"], 2)

    def test_a_passing_weekly_look_resets_the_count(self):
        with patch.object(rules, "look", return_value=(True, {"live_page": {"status": "passed"}})):
            counts = rules.run_weekly_look_sweep(now=timezone.now() + timedelta(days=8))
        self.assertEqual((counts["looked"], counts["queued"]), (1, 0))
        self.assertEqual(self.row().last_look["failures"], 0)

    def test_nightly_compare_reports_a_flip_without_changing_the_record(self):
        WebsiteReadiness.objects.filter(connection=self.connection).update(state="fixing")
        cache.clear()
        counts = rules.run_nightly_compare_sweep(now=timezone.now())
        self.assertEqual(counts["flipped"], 1)
        self.assertEqual(self.row().state, "fixing")
        event = WebsiteCheckEvent.objects.get(connection=self.connection, trigger="nightly_compare")
        self.assertEqual((event.result, event.detail["from"], event.detail["to"]), ("failed", "fixing", "ready"))
        self.assertEqual(rules.run_nightly_compare_sweep(now=timezone.now()), {"skipped": "already_ran_today"})


@override_settings(**RULES)
class ReadinessReviewFixTests(TalaThriveSite, TestCase):
    """Review fixes: no promotion on the old live page, sealed targets, draft starts, access, fixing, sweeps."""

    def test_a_footprint_push_never_passes_on_the_old_live_page_alone(self):
        self.push_to(_compare("stories/index.html"))
        live_only = {"blog_files": {"status": "passed"}, "build_render": {"status": "not_run"}, "live_page": {"status": "passed"}}
        op = self.checks_op()
        self.assertEqual(self.process(op, live_only), "pending")
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.verified_sha, VERIFIED)
        self.assertEqual(self.row().state, "checking")
        WebsiteConnectionOperation.objects.filter(pk=op.pk).update(attempts=rules.VERIFY_ONLY_MAX_ATTEMPTS,
                                                                    next_attempt_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.process(op, live_only), "needs_attention")
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.verified_sha, VERIFIED)
        self.assertEqual((self.row().state, self.row().can_publish), ("fixing", False))

    def test_a_sealed_target_is_never_promoted_by_a_verify_only_check(self):
        self.push_to(_compare("stories/index.html"))
        with patch.object(rules, "sealed_for", return_value=True):
            self.assertEqual(self.process(self.checks_op(), PASSED), "cancelled")
            self.assertEqual(rules.queue_verify_only(self.connection, trigger="manual", bucket="x"), (None, False))
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.verified_sha, VERIFIED)
        self.assertEqual(self.checks_op().receipt["reason"], "sealed_workflow_required")
        self.assertNotEqual(self.row().state, "ready")

    def test_a_draft_starts_in_checking_with_reads_on(self):
        from types import SimpleNamespace
        from django.db import transaction
        from .vibe_marketing_views import _validate_article_start_reservation
        from .website_contract import WebsiteAuthorityError
        self.push_to(_compare("stories/index.html"))
        self.assertEqual(self.row().state, "checking")
        payload = {"delivery_mode": "review_draft", "website_connection_id": str(self.connection.pk),
                   "connection_target_id": self.config.default_publish_target_id, "expected_source_sha": VERIFIED}
        context = SimpleNamespace(organization=self.config.organization)

        def reserve(data):
            with transaction.atomic(), patch("content_factory.website_contract.validate_authority"), \
                    patch("content_factory.website_operations.validate_operation"):
                _validate_article_start_reservation(context, data)

        with override_settings(ARTICLES_READINESS_READS_ENABLED=True):
            reserve(payload)
            with self.assertRaises(WebsiteAuthorityError) as publish:
                reserve({**payload, "delivery_mode": "publish_code"})
            self.assertEqual(publish.exception.code, "website_publish_verification_required")
        with self.assertRaises(WebsiteAuthorityError):
            reserve(payload)  # reads off: today's proof requirement

    def test_an_access_failure_stays_needs_user_on_the_next_shadow_write(self):
        from .website_contract import WebsiteAuthorityError
        self.push_to(_compare("stories/index.html"))
        lost = WebsiteAuthorityError("github_access_required", "Access was removed.")
        with patch.object(rules, "run_checks", side_effect=lost):
            reconciliation._process_source_reverification(self.checks_op().pk, timezone.now() + timedelta(minutes=2))
        self.assertEqual(self.row().state, "needs_user")
        readiness.record(self.connection, trigger="verification_recorded")
        self.assertEqual((self.row().state, self.row().reason_code), ("needs_user", "github_access_required"))

    def test_a_push_to_a_fixing_site_checks_without_reopening_preparation(self):
        WebsiteReadiness.objects.filter(connection=self.connection).update(state="fixing", can_publish=False)
        result, compare, wake, _ = self.push_to(_compare("README.md"))
        self.assertEqual(result["push_footprint"], 1)
        compare.assert_not_called()
        self.assertTrue(self.checks_op().payload["verify_only"])
        self.assertTrue(rules.holds_prepare(self.connection))

    def test_wake_prepare_stays_shut_for_a_held_site(self):
        self.connection.authorized_by = self.built.owner
        self.connection.save(update_fields=["authorized_by"])
        with patch("content_factory.website_prepare.start_prepare") as start:
            self.assertIsNone(REAL_WAKE(self.connection.pk, organization_id=self.connection.organization_id, source_sha=PUSHED))
            start.assert_not_called()
            with patch.object(rules, "sealed_for", return_value=True):
                # Sealed-CI targets keep today's behaviour: the guard does not hold them.
                self.assertFalse(rules.holds_prepare(self.connection))

    def test_the_weekly_look_reaches_enabled_sites_past_other_domains(self):
        from .founder_journey_testing import make_history_free_org
        from .tests_live_url_verification import _connect
        long_ago = timezone.now() - timedelta(days=60)
        for index in range(5):
            other = _connect(make_history_free_org(domain=f"other{index}.example.test"))
            WebsiteReadiness.objects.create(connection=other, generation=other.generation, state="ready",
                                            can_draft=True, can_publish=True)
        WebsiteReadiness.objects.exclude(connection=self.connection).update(updated_at=long_ago)
        looked = []
        with patch.object(rules, "look", side_effect=lambda site, row, now: looked.append(site.pk) or (True, {})):
            counts = rules.run_weekly_look_sweep(now=timezone.now() + timedelta(days=8), limit=1)
        self.assertEqual(looked, [self.connection.pk])
        self.assertEqual((counts["looked"], counts["skipped"]), (1, 0))

    def test_an_archived_repository_records_needs_user(self):
        from . import website_repository_identity as identity
        with self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(identity.set_blocker(self.connection, identity.ARCHIVED_ENTRY, True))
        self.assertEqual((self.row().state, self.row().reason_code), ("needs_user", "CONNECTION_READ_ONLY"))
        event = WebsiteCheckEvent.objects.filter(connection=self.connection).order_by("-created_at", "-pk").first()
        self.assertEqual((event.trigger, event.detail["identity"]), ("access_change", "added:CONNECTION_READ_ONLY"))

    def test_a_reset_drops_the_pin(self):
        self.assertEqual(self.row().pinned_contract["setupRunId"], "e3f55f7e")
        self.connection.generation += 1
        self.connection.save(update_fields=["generation"])
        readiness.record(self.connection, trigger="access_change", detail={"action": "reset"})
        self.assertEqual(self.row().pinned_contract.get("setupRunId"), "")
        with override_settings(ARTICLES_READINESS_PIN_ENABLED=True):
            self.assertEqual(rules.setup_pin(self.connection), {})
