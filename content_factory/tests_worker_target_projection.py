"""B13: the worker's targets are projected from the current-generation target row.

``config.publish_targets`` is a cache that verification writes beside the
target row. A push that carries the proof forward moves only the row, so the
worker read the old commit's proof and refused a website the founder was told
is Ready. Property: wherever the founder summary says Ready, the worker
projection validates.
"""
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from . import website_live_status as live_status
from . import website_reconciliation as reconciliation
from . import website_source_carry as carry
from .activation import durable_integration_evidence
from .founder_journey_testing import make_history_free_org
from .tests_live_url_verification import CONTRACT_DIGEST, MERGED, LiveUrlFixture, _connect, _next_js_target
from .tests_setup_merge_proof import _worker_accepts
from .website_connections import contract_for
from .website_models import WebsiteConnectionOperation
from .website_operations import deletion_epoch
from .website_setup_merge_proof import worker_publish_targets

PUSHED = "e" * 40
SETUP = {"registry_path": "src/content/articles/registry.ts", "route_shell_path": "src/app/blog/page.tsx",
         "managed_files": ["src/lib/articles.ts"], "content_path": "src/content/articles"}


def _compare(*files):
    return {"status": "ahead", "total_commits": 1, "files": [{"filename": name, "status": "modified"} for name in files]}


@override_settings(ARTICLES_READY_ON_MERGE_ENABLED=True, ARTICLES_PROOF_CARRY_FORWARD_ENABLED=True,
                   ARTICLES_PREPARE_STICKY_STOP_ENABLED=True)
class WorkerProjectionTests(LiveUrlFixture, TestCase):
    def setUp(self):
        super().setUp()
        cache.delete(live_status.THROTTLE_KEY)
        self.built = make_history_free_org(domain="projection.example.test")
        self.connection = _connect(self.built, verified_sha=MERGED)
        self.connection.capabilities = {**self.connection.capabilities, "publishingReady": True, "generationReady": True}
        self.connection.last_verified_at = timezone.now() - timedelta(minutes=5)
        self.connection.save(update_fields=["capabilities", "last_verified_at"])
        self.target = _next_js_target(self.connection)
        # The verified, operation-bound proof the worker checks, as verification writes it.
        proof = {**contract_for(self.connection), "status": "passed", "source_sha": MERGED, "target_id": "articles",
                 "adapter_id": "next_app_router", "operation_id": "verify-op", "operation_attempt": 1,
                 "deletion_epoch": deletion_epoch(self.connection), "evidence_digest": "f" * 64,
                 "contract_digest": CONTRACT_DIGEST, "preview_capable": True}
        self.target.contract = {**self.target.contract, "verification": proof}
        self.target.save(update_fields=["contract"])
        self.config = self.built.config
        self.config.refresh_from_db()
        self.config.article_system_setup_cache = SETUP
        self.config.publish_targets = [self.target.contract]
        self.config.save(update_fields=["article_system_setup_cache", "publish_targets"])
        for name, value in (("_find_owned_merge", None),):
            patcher = patch(f"content_factory.website_reconciliation.{name}", return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        wake = patch("content_factory.website_prepare.wake_prepare_for_source")
        wake.start()
        self.addCleanup(wake.stop)

    def refresh(self):
        self.config.refresh_from_db()
        self.connection.refresh_from_db()
        self.target.refresh_from_db()

    def summary_ready(self):
        self.refresh()
        evidence = durable_integration_evidence(self.config, self.connection)
        return evidence.get("verified") is True and evidence.get("publishingVerified") is True

    def worker_accepts(self):
        self.refresh()
        return bool(_worker_accepts(worker_publish_targets(self.config), self.config, self.connection))

    def push(self, *files):
        with patch.object(carry, "compare_sources", return_value=_compare(*files)):
            return reconciliation.handle_website_github_event("push", {
                "repository": {"id": 777}, "ref": "refs/heads/main", "after": PUSHED})

    def recheck(self):
        op = WebsiteConnectionOperation.objects.get(connection=self.connection, action="source-reverify",
                                                    payload__source_sha=PUSHED)
        with patch("content_factory.website_reconciliation._renews_by_live_url", return_value=False):
            return reconciliation._process_source_reverification(op.pk, timezone.now())

    def assert_ready_implies_worker_accepts(self, label):
        if self.summary_ready():
            self.assertTrue(self.worker_accepts(), f"{label}: the summary says Ready but the worker refuses")

    def test_a_ready_site_validates(self):
        self.assertTrue(self.summary_ready())
        self.assertTrue(self.worker_accepts())

    def test_a_carried_push_keeps_the_worker_at_the_new_head(self):
        self.assertEqual(self.push("src/app/page.tsx")["carried"], 1)
        self.refresh()
        # The cache still names the old commit; the row moved with the carry.
        self.assertEqual(self.config.publish_targets[0]["verification"]["source_sha"], MERGED)
        self.assertEqual(self.target.source_sha, PUSHED)
        self.assertTrue(self.summary_ready())
        projected = worker_publish_targets(self.config)
        self.assertEqual(projected[0]["verification"]["source_sha"], PUSHED)
        self.assertEqual(projected[0]["verification"]["carried_from"], MERGED)
        self.assertTrue(self.worker_accepts())
        self.assertEqual(self.recheck(), "completed")
        self.assertTrue(self.worker_accepts())

    def test_a_missing_cache_entry_is_filled_from_the_row(self):
        self.config.publish_targets = []
        self.config.save(update_fields=["publish_targets"])
        self.assertTrue(self.worker_accepts())

    def test_a_withdrawn_carry_is_not_projected(self):
        self.push("src/app/page.tsx")
        op = WebsiteConnectionOperation.objects.get(connection=self.connection, action="source-reverify",
                                                    payload__source_sha=PUSHED)
        self.assertTrue(carry.lock_carried_source(op))
        self.assertFalse(self.summary_ready())
        self.refresh()
        self.assertIs(worker_publish_targets(self.config), self.config.publish_targets)
        self.assertFalse(self.worker_accepts())

    def test_a_push_that_changes_setup_files_is_not_projected(self):
        self.push("src/app/blog/page.tsx")
        self.assertFalse(self.summary_ready())
        self.refresh()
        # The old commit's cached proof is served as before; the worker binds it to the new head and refuses.
        self.assertIs(worker_publish_targets(self.config), self.config.publish_targets)
        self.assertEqual(self.config.publish_targets[0]["verification"]["source_sha"], MERGED)

    def test_sealed_custom_and_unverified_rows_keep_the_cache(self):
        self.push("src/app/page.tsx")
        self.refresh()
        with patch("content_factory.website_sealed_workflow.required", return_value=True):
            self.assertIs(worker_publish_targets(self.config), self.config.publish_targets)
        self.target.contract = {**self.target.contract, "delivery_adapter": "custom_contract_v1"}
        self.target.save(update_fields=["contract"])
        self.assertIs(worker_publish_targets(self.config), self.config.publish_targets)
        self.target.capabilities = {**self.target.capabilities, "publishingReady": False}
        self.target.save(update_fields=["capabilities"])
        self.assertIs(worker_publish_targets(self.config), self.config.publish_targets)

    def test_another_generation_or_a_disconnected_site_keeps_the_cache(self):
        self.push("src/app/page.tsx")
        self.connection.state = "paused"
        self.connection.save(update_fields=["state"])
        self.refresh()
        self.assertIs(worker_publish_targets(self.config), self.config.publish_targets)

    def test_property_ready_implies_the_worker_projection_validates(self):
        self.assert_ready_implies_worker_accepts("verified")
        self.push("src/app/page.tsx")
        self.assert_ready_implies_worker_accepts("carried, before the free re-check")
        self.recheck()
        self.assert_ready_implies_worker_accepts("carried, re-checked")
        self.config.publish_targets = []
        self.config.save(update_fields=["publish_targets"])
        self.assert_ready_implies_worker_accepts("cache emptied by a config write")
