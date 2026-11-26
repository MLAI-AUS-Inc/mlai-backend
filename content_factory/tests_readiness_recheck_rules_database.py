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


@override_settings(**RULES)
class ReadinessGapTests(TalaThriveSite, TestCase):
    """Completeness gaps: unsupported stacks, CF delivery failures, the owner action and direct setup pins."""

    def verify_with_cf(self, cf_build, *, live="passed"):
        """Run the real verify-only checks with Content Factory answering ``cf_build`` for build_render."""
        from unittest.mock import MagicMock
        response = MagicMock(status_code=200)
        response.json.return_value = {"status": "unavailable", "durationMs": 12,
                                      "checks": {"blog_files": {"status": "passed"}, "build_render": cf_build}}
        files = {"status": "passed", "detail": "All blog files are in place."}
        page = {"status": live, "detail": "The live blog page answered."}
        with patch.object(rules, "check_blog_files", return_value=files), \
                patch.object(rules, "check_live_page", return_value=page), \
                patch("content_factory.website_connections.require_unlocked_remote_call"), \
                patch("content_factory.vibe_marketing_views._content_factory_remote_config",
                      return_value={"enabled": True, "base_url": "https://cf.test"}), \
                patch("integrations.http_client.post", return_value=response) as post, \
                patch("content_factory.vibe_marketing_views._queue_content_factory_run") as dispatch:
            state = reconciliation._process_source_reverification(self.checks_op().pk, timezone.now() + timedelta(minutes=2))
        dispatch.assert_not_called()
        post.assert_called_once()
        return state

    def test_an_unsupported_stack_promotes_on_blog_files_and_the_live_page(self):
        self.push_to(_compare("app/blog/page.tsx", "stories/index.html"))
        self.assertEqual(self.row().state, "checking")
        unsupported = {"status": "not_run", "detail": "This site's build can't be checked in place; its preview build checks it instead.",
                       "evidence": {"reason": "render_unsupported_for_stack", "unsupported": True,
                                    "cause": "no_static_materialization_contract", "framework": "nextjs",
                                    "runtime_family": "node"}}
        self.assertEqual(self.verify_with_cf(unsupported), "completed")
        self.connection.refresh_from_db()
        self.target.refresh_from_db()
        self.assertEqual((self.connection.verified_sha, self.target.source_sha), (PUSHED, PUSHED))
        self.assertTrue(self.connection.capabilities["publishingReady"])
        row = self.row()
        self.assertEqual((row.state, row.can_publish), ("ready", True))
        self.assertEqual(row.checks["build_render"]["status"], "not_run")
        self.assertEqual(row.checks["build_render"]["detail"], rules.UNSUPPORTED_BUILD_DETAIL)
        event = WebsiteCheckEvent.objects.filter(connection=self.connection, trigger="push_footprint", result="passed").get()
        self.assertTrue(event.detail["build_unsupported"])

    def test_an_unsupported_stack_still_waits_for_the_live_page(self):
        self.push_to(_compare("stories/index.html"))
        unsupported = {"status": "not_run", "evidence": {"reason": "render_unsupported_for_stack", "unsupported": True,
                                                         "cause": "witness_not_required"}}
        self.assertEqual(self.verify_with_cf(unsupported, live="not_run"), "pending")
        self.assertEqual(self.row().state, "checking")

    def test_an_old_unsupported_reason_alone_keeps_retrying(self):
        self.push_to(_compare("stories/index.html"))
        old = {"status": "not_run", "evidence": {"reason": "no_static_materialization_contract"}}
        self.assertEqual(self.verify_with_cf(old), "pending")
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.verified_sha, VERIFIED)
        self.assertEqual(self.row().state, "checking")

    def test_a_transient_build_answer_keeps_retrying(self):
        self.push_to(_compare("stories/index.html"))
        transient = {"status": "unavailable", "evidence": {"codes": ["BUILD_TIMEOUT"]}}
        self.assertEqual(self.verify_with_cf(transient), "pending")
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.verified_sha, VERIFIED)
        self.assertEqual(self.row().state, "checking")

    def failed_callback(self, code, *, workflow="direct_generate", run_id="article-run-1"):
        from workflow_runs.models import ContentFactoryRun
        from .service_views import ContentFactoryCallbackView
        ContentFactoryRun.objects.get_or_create(run_id=run_id, defaults={
            "domain": self.connection.organization.domain, "workflow": workflow, "status": "running",
            "run_request": {"website_connection_id": str(self.connection.pk), "workflow": workflow}})
        data = {"event": "generation_failed", "event_type": "generation_failed", "job_id": run_id, "run_id": run_id,
                "workflow": workflow, "status": "error", "error": "The site changed.", "error_code": code,
                "domain": self.connection.organization.domain}
        with self.captureOnCommitCallbacks(execute=True), \
                patch("content_factory.vibe_marketing_views._queue_content_factory_run") as dispatch:
            response = ContentFactoryCallbackView()._handle_generation_failed(data)
        dispatch.assert_not_called()
        self.assertLess(response.status_code, 300)

    def delivery_checks(self):
        return WebsiteConnectionOperation.objects.filter(connection=self.connection, payload__trigger="delivery_failure")

    def test_a_cf_delivery_failure_queues_one_verify_only_check_per_commit(self):
        self.failed_callback("SOURCE_CHANGED")
        self.failed_callback("PUBLISH_TARGET_UNVERIFIED", run_id="article-run-2")
        op = self.delivery_checks().get()
        self.assertEqual((op.action, op.payload["verify_only"], op.payload["source_sha"]), ("source-reverify", True, VERIFIED))
        self.assertFalse(WebsiteConnectionOperation.objects.filter(connection=self.connection, action="prepare").exists())
        event = WebsiteCheckEvent.objects.get(connection=self.connection, trigger="delivery_failure")
        self.assertEqual((event.detail["run_id"], event.detail["refusal"]), ("article-run-1", "website_source_changed"))

    def test_other_cf_failures_and_setup_runs_queue_nothing(self):
        self.failed_callback("INTERNAL_ERROR")
        self.failed_callback("SOURCE_CHANGED", workflow="article_system_setup", run_id="setup-run-1")
        with override_settings(ARTICLES_READINESS_RECHECK_RULES_ENABLED=False):
            self.failed_callback("SOURCE_CHANGED", run_id="article-run-3")
        with override_settings(ARTICLES_READINESS_DOMAINS="other.example.test"):
            self.failed_callback("SOURCE_CHANGED", run_id="article-run-4")
        self.assertFalse(self.delivery_checks().exists())

    def owner_post(self, action, data=None):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from .website_views import WebsiteConnectionActionView
        company_id = str(self.built.company.pk)
        request = APIRequestFactory().post(f"/website-connection/{action}?company_id={company_id}",
                                           {"company_id": company_id, **(data or {})}, format="json")
        force_authenticate(request, user=self.built.owner)
        return WebsiteConnectionActionView.as_view()(request, action=action)

    def test_the_owner_readiness_check_action_queues_one_verify_only_check(self):
        with self.captureOnCommitCallbacks(execute=True):
            first = self.owner_post("readiness-check")
            second = self.owner_post("readiness-check")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        ops = WebsiteConnectionOperation.objects.filter(connection=self.connection, payload__trigger="manual")
        self.assertEqual(ops.count(), 1)
        op = ops.get()
        self.assertEqual((op.action, op.payload["verify_only"], op.payload["source_sha"]), ("source-reverify", True, VERIFIED))
        self.assertEqual(first.data["operation"]["id"], str(op.pk))
        self.assertEqual(second.data["operation"]["id"], str(op.pk))
        self.assertIn("websiteReadiness", first.data)
        self.assertFalse(WebsiteConnectionOperation.objects.filter(connection=self.connection, action="prepare").exists())
        self.assertEqual(WebsiteCheckEvent.objects.filter(connection=self.connection, trigger="manual").count(), 1)

    def test_the_readiness_check_action_is_refused_without_the_rules(self):
        with override_settings(ARTICLES_READINESS_RECHECK_RULES_ENABLED=False):
            response = self.owner_post("readiness-check")
        self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "source_reverification_unavailable")
        self.assertFalse(WebsiteConnectionOperation.objects.filter(connection=self.connection, payload__trigger="manual").exists())

    def direct_setup(self, data=None, *, route="/stories"):
        """A founder's own POST to the setup view; returns the payload sent to Content Factory."""
        from types import SimpleNamespace
        from rest_framework.test import APIRequestFactory, force_authenticate
        from . import vibe_marketing_views as views
        from .website_connections import contract_for
        WebsiteReadiness.objects.filter(connection=self.connection).update(
            pinned_contract={**self.row().pinned_contract, "routePath": "/stories", "generation": self.connection.generation})
        sent = []
        run = SimpleNamespace(run_id="direct-setup-1", status="queued", result={})

        def queue(**kwargs):
            sent.append(kwargs["payload"])
            return run

        company_id = str(self.built.company.pk)
        request = APIRequestFactory().post(f"/article-system-setup?company_id={company_id}", {
            **contract_for(self.connection), "company_id": company_id, "github_repo": self.connection.github_repo,
            "articleSurfaceMode": "existing", "articleSurfaceUrl": route, **(data or {})}, format="json")
        force_authenticate(request, user=self.built.owner)
        from unittest.mock import MagicMock
        provider = MagicMock(status_code=200)
        provider.json.return_value = {"id": self.connection.repository_id, "full_name": self.connection.github_repo,
                                      "default_branch": self.connection.branch, "sha": VERIFIED}
        with patch("integrations.services.github_connections.read_bound_repository", return_value=provider), \
                patch.object(views, "_queue_content_factory_run", side_effect=queue), \
                patch.object(views, "_require_roo_points_for_ai_agent", return_value=(None, 0)), \
                patch.object(views, "analytics_config_for_content_factory", return_value={}), \
                patch.object(views, "_article_system_setup_run_is_dispatch_blocked", return_value=False), \
                patch("content_factory.run_provider_facts.save_provider_result"), \
                patch("content_factory.website_prepare.attach_selected_setup_run"):
            response = views.VibeMarketingArticleSystemSetupView.as_view()(request)
        self.assertEqual(response.status_code, 202, getattr(response, "data", None))
        self.assertEqual(len(sent), 1)
        return sent[0]

    @override_settings(ARTICLES_READINESS_PIN_ENABLED=True)
    def test_a_direct_setup_post_forwards_the_pin(self):
        payload = self.direct_setup()
        self.assertEqual((payload["pinned_setup_run_id"], payload["strategy_pin"]), ("e3f55f7e", "model_adapter"))

    @override_settings(ARTICLES_READINESS_PIN_ENABLED=True)
    def test_set_up_again_or_another_route_sends_no_pin(self):
        for payload in (self.direct_setup({"set_up_again": True}), self.direct_setup(route="/blog")):
            self.assertNotIn("pinned_setup_run_id", payload)
            self.assertNotIn("strategy_pin", payload)

    def test_a_direct_setup_post_sends_no_pin_with_the_flag_off(self):
        payload = self.direct_setup()
        self.assertNotIn("pinned_setup_run_id", payload)
