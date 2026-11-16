"""Audit sec 4: a push that leaves MLAI's articles files alone keeps the site Ready."""
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from . import website_live_status as live_status
from . import website_reconciliation as reconciliation
from . import website_source_carry as carry
from .activation import durable_integration_evidence
from .founder_journey_testing import make_history_free_org
from .tests_live_url_verification import (
    MERGED,
    LiveUrlFixture,
    _connect,
    _next_js_target,
)
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnectionOperation

PUSHED = "e" * 40
SETUP = {
    "registry_path": "src/content/articles/registry.ts",
    "route_shell_path": "src/app/blog/page.tsx",
    "detail_route_shell_path": "src/app/blog/[slug]/page.tsx",
    "managed_files": ["src/lib/articles.ts"],
    "content_path": "src/content/articles",
}


def _compare(*files, status="ahead"):
    return {"status": status, "total_commits": 1,
            "files": [{"filename": name, "status": "modified"} if isinstance(name, str) else name for name in files]}


class DecideTests(SimpleTestCase):
    def test_a_push_that_leaves_the_setup_files_alone_carries(self):
        decision = carry.decide(SETUP, _compare("src/app/page.tsx", "README.md"), owned_merge=False)
        self.assertTrue(decision["carry"])
        self.assertEqual(decision["changed_files"], 2)

    def test_a_push_that_changes_a_setup_file_does_not(self):
        decision = carry.decide(SETUP, _compare("src/app/blog/page.tsx"), owned_merge=False)
        self.assertEqual((decision["carry"], decision["reason"]), (False, "setup_files_changed"))
        self.assertEqual(decision["changed_setup_files"], ["src/app/blog/page.tsx"])

    def test_renaming_a_setup_file_away_does_not_carry(self):
        renamed = {"filename": "src/lib/old-articles.ts", "previous_filename": "src/lib/articles.ts", "status": "renamed"}
        self.assertFalse(carry.decide(SETUP, _compare(renamed), owned_merge=False)["carry"])

    def test_an_mlai_article_merge_may_add_to_the_registry_and_content_folder(self):
        files = ("src/content/articles/registry.ts", "src/content/articles/new-post.md")
        self.assertFalse(carry.decide(SETUP, _compare(*files), owned_merge=False)["carry"])
        self.assertTrue(carry.decide(SETUP, _compare(*files), owned_merge=True)["carry"])

    def test_an_mlai_merge_still_may_not_change_the_route_shells(self):
        decision = carry.decide(SETUP, _compare("src/app/blog/[slug]/page.tsx"), owned_merge=True)
        self.assertFalse(decision["carry"])

    def test_anything_unclear_falls_back_to_the_old_lock(self):
        self.assertEqual(carry.decide({}, _compare("a.ts"), owned_merge=False)["reason"], "no_setup_record")
        self.assertEqual(carry.decide(SETUP, _compare("a.ts", status="diverged"), owned_merge=False)["reason"], "not_fast_forward")
        self.assertEqual(carry.decide(SETUP, {"status": "ahead"}, owned_merge=False)["reason"], "compare_unreadable")
        many = [f"src/file{index}.ts" for index in range(carry.COMPARE_FILE_LIMIT)]
        self.assertEqual(carry.decide(SETUP, _compare(*many), owned_merge=False)["reason"], "compare_truncated")


class CarriedSite(LiveUrlFixture):
    """A merged site Ready on MLAI's build proof, with its setup files recorded."""

    def setUp(self):
        super().setUp()
        cache.delete(live_status.THROTTLE_KEY)
        self.built = make_history_free_org(domain="carry.example.test")
        self.connection = _connect(self.built, verified_sha=MERGED)
        self.connection.capabilities = {**self.connection.capabilities, "publishingReady": True, "generationReady": True}
        self.connection.last_verified_at = timezone.now() - timedelta(minutes=5)
        self.connection.save(update_fields=["capabilities", "last_verified_at"])
        self.target = _next_js_target(self.connection)
        self.config = self.built.config
        self.config.refresh_from_db()
        self.config.article_system_setup_cache = SETUP
        self.config.save(update_fields=["article_system_setup_cache"])
        for name, value in (("_find_owned_merge", None),):
            patcher = patch(f"content_factory.website_reconciliation.{name}", return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        wake = patch("content_factory.website_prepare.wake_prepare_for_source")
        wake.start()
        self.addCleanup(wake.stop)

    def push(self, comparison):
        with patch.object(carry, "compare_sources", return_value=comparison) as compare:
            result = reconciliation.handle_website_github_event("push", {
                "repository": {"id": 777}, "ref": "refs/heads/main", "after": PUSHED})
        self.connection.refresh_from_db()
        self.target.refresh_from_db()
        return result, compare

    def operation(self):
        return WebsiteConnectionOperation.objects.get(connection=self.connection, action="source-reverify",
            payload__source_sha=PUSHED)

    def evidence(self):
        self.config.refresh_from_db()
        self.connection.refresh_from_db()
        return durable_integration_evidence(self.config, self.connection)


@override_settings(ARTICLES_READY_ON_MERGE_ENABLED=True, ARTICLES_PROOF_CARRY_FORWARD_ENABLED=True)
class CarryPushTests(CarriedSite, TestCase):
    def test_a_push_outside_the_setup_files_stays_ready(self):
        self.assertTrue(self.evidence()["verified"])
        result, compare = self.push(_compare("src/app/page.tsx"))
        compare.assert_called_once()
        self.assertEqual(result["carried"], 1)
        self.assertEqual(self.connection.verified_sha, PUSHED)
        self.assertEqual(self.target.source_sha, PUSHED)
        self.assertEqual(self.target.capabilities["proofCarriedFrom"], MERGED)
        self.assertEqual(self.target.contract["verification"]["carried_from"], MERGED)
        self.assertTrue(self.connection.capabilities["publishingReady"])
        self.assertFalse([item for item in self.connection.blockers if item.get("code") == "repository_source_changed"])
        evidence = self.evidence()
        self.assertTrue(evidence["verified"], evidence)
        self.assertTrue(evidence["publishingVerified"])
        op = self.operation()
        self.assertFalse(op.payload["scan_required"])
        self.assertEqual(op.payload["carried_from"], MERGED)

    def test_a_push_that_changes_the_setup_files_locks_as_before(self):
        result, _ = self.push(_compare("src/app/blog/page.tsx"))
        self.assertNotIn("carried", result)
        self.assertEqual(self.connection.verified_sha, MERGED)
        self.assertFalse(self.connection.capabilities["publishingReady"])
        self.assertTrue([item for item in self.connection.blockers if item.get("code") == "repository_source_changed"])
        op = self.operation()
        self.assertTrue(op.payload["scan_required"])
        self.assertEqual(op.payload["carried_from"], "")

    def test_an_unreadable_comparison_locks_as_before(self):
        self.push(None)
        self.assertEqual(self.connection.verified_sha, MERGED)
        self.assertFalse(self.connection.capabilities["publishingReady"])

    def test_the_kill_switch_restores_the_old_lock_without_a_github_read(self):
        with override_settings(ARTICLES_PROOF_CARRY_FORWARD_ENABLED=False):
            _, compare = self.push(_compare("src/app/page.tsx"))
        compare.assert_not_called()
        self.assertEqual(self.connection.verified_sha, MERGED)
        self.assertFalse(self.connection.capabilities["publishingReady"])

    def test_a_target_that_needs_sealed_ci_proof_is_never_carried(self):
        with patch("content_factory.website_sealed_workflow.required", return_value=True):
            _, compare = self.push(_compare("src/app/page.tsx"))
        compare.assert_not_called()
        self.assertEqual(self.connection.verified_sha, MERGED)

    def test_the_background_recheck_completes_for_free(self):
        self.push(_compare("src/app/page.tsx"))
        op = self.operation()
        with patch("content_factory.website_reconciliation._renews_by_live_url", return_value=False):
            state = reconciliation._process_source_reverification(op.pk, timezone.now())
        op.refresh_from_db()
        self.assertEqual(state, "completed", op.receipt)
        self.assertEqual(op.receipt["method"], "carried_build_proof")
        self.assertNotIn("scan_run_id", op.receipt)
        self.assertTrue(self.evidence()["verified"])

    def test_a_failed_recheck_withdraws_the_carried_proof(self):
        self.push(_compare("src/app/page.tsx"))
        op = self.operation()
        failure = WebsiteAuthorityError("github_source_unavailable", "GitHub is down.", retryable=False)
        with patch("content_factory.website_reconciliation._renews_by_live_url", return_value=False), \
                patch("content_factory.website_connections.verify_repository_head", side_effect=failure):
            state = reconciliation._process_source_reverification(op.pk, timezone.now())
        self.assertEqual(state, "needs_attention")
        self.connection.refresh_from_db()
        self.assertFalse(self.connection.capabilities["publishingReady"])
        self.assertEqual(self.evidence()["reasonCode"], "repository_source_changed")

    def test_a_retryable_recheck_keeps_ready_inside_the_window(self):
        self.push(_compare("src/app/page.tsx"))
        op = self.operation()
        failure = WebsiteAuthorityError("github_source_unavailable", "GitHub is busy.", retryable=True)
        with patch("content_factory.website_reconciliation._renews_by_live_url", return_value=False), \
                patch("content_factory.website_connections.verify_repository_head", side_effect=failure):
            state = reconciliation._process_source_reverification(op.pk, timezone.now())
        self.assertEqual(state, "pending")
        self.assertTrue(self.evidence()["verified"])
        late = op.created_at + timedelta(hours=carry.RECHECK_WINDOW_HOURS, minutes=1)
        WebsiteConnectionOperation.objects.filter(pk=op.pk).update(next_attempt_at=None)
        with patch("content_factory.website_reconciliation._renews_by_live_url", return_value=False), \
                patch("content_factory.website_connections.verify_repository_head", side_effect=failure):
            state = reconciliation._process_source_reverification(op.pk, late)
        self.assertEqual(state, "needs_attention")
        self.assertFalse(self.evidence()["verified"])

    def test_a_newer_push_owns_the_connection_so_the_old_lock_is_skipped(self):
        self.push(_compare("src/app/page.tsx"))
        op = self.operation()
        self.connection.verified_sha = "f" * 40
        self.connection.save(update_fields=["verified_sha"])
        self.assertFalse(carry.lock_carried_source(op))
