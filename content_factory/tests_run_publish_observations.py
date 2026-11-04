"""Late publication recovery never competes with an admitted execution."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from . import vibe_marketing_views as views
from .run_backend_facts import BackendFactConflict
from .run_ownership import bind_admitted_run, reserve_admitted_run, user_principal
from .tests_run_event_authority import FLAGS


@override_settings(**FLAGS)
class PublishObservationTests(TransactionTestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic publication recovery", domain="publish-facts.example")
        payload = {"domain": self.org.domain, "client_request_id": "publish-observation-original",
            "topic": "Original article"}
        self.run = bind_admitted_run(reserve_admitted_run(organization=self.org,
            principal=user_principal(123, "user:123"), workflow="article_generation", payload=payload),
            "publish-observation-worker")
        self.context = SimpleNamespace(organization=self.org)

    def snapshot(self):
        return ContentFactoryRun.objects.values().get(pk=self.run.pk)

    def assert_execution_unchanged(self, before):
        after = self.snapshot()
        for key in ("created_at", "updated_at", "last_event_emitted_at", "reconciled_at", "status",
                    "current_step", "run_request", "approval_state", "error", "resume_available"):
            self.assertEqual(after[key], before[key], key)
        return after

    def test_stale_handoff_and_retry_annotations_keep_execution_clocks_and_unrelated_facts(self):
        baseline = deepcopy(self.run.result)
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(result={**baseline, "concurrent_fact": {"kept": True}})
        self.run.result = {**baseline, "publish_handoff_pending": True,
            "publish_handoff_status": "pending", "publish_handoff_started_at": "2020-01-01T00:00:00+00:00"}
        # Save a real prior observation; its unrelated concurrent fact survives.
        views._save_publish_observation(self.run, self.run.result, baseline=baseline)
        before = self.snapshot()
        views._annotate_publish_handoff_staleness(self.run)
        after = self.assert_execution_unchanged(before)
        self.assertTrue(after["result"]["publish_handoff_stale"])
        self.assertEqual(after["result"]["concurrent_fact"], {"kept": True})
        self.run.result["publish_auto_merge_state"] = "blocked"
        views._save_publish_observation(self.run, self.run.result, baseline=after["result"])
        before = self.snapshot()
        views._reset_publish_auto_merge_block(self.run)
        self.assertNotIn("publish_auto_merge_state", self.assert_execution_unchanged(before)["result"])

    def test_same_observation_race_refuses_instead_of_overwriting_newer_value(self):
        baseline = deepcopy(self.run.result)
        views._save_publish_observation(self.run, {**baseline, "publish_handoff_status": "queued"}, baseline=baseline)
        stale = ContentFactoryRun.objects.get(pk=self.run.pk)
        with self.assertRaises(BackendFactConflict):
            views._save_publish_observation(stale, {**baseline, "publish_handoff_status": "stale"}, baseline=baseline)
        self.assertEqual(self.snapshot()["result"]["publish_handoff_status"], "queued")

    def test_legacy_pending_hint_cannot_rewrite_owned_recipe_or_metadata(self):
        before = self.snapshot()
        views._mark_publish_handoff_pending(run=self.run, remote_run=self.run,
            auto_merge=True, remote_data={"run_id": "foreign-child", "status": "completed"})
        self.assertEqual(self.snapshot(), before)

    def test_unknown_child_recovery_never_creates_run_or_calls_worker(self):
        before = self.snapshot()
        self.run.result = {**self.run.result, "publish_child_run_id": "unadmitted-remote-child"}
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(result=self.run.result)
        before = self.snapshot()
        count = ContentFactoryRun.objects.count()
        with patch.object(views, "_call_content_factory_run_status") as remote:
            self.assertIsNone(views._recover_publish_child_for_run(self.run,
                request=SimpleNamespace(user=None), context=self.context))
            self.assertIsNone(views._ensure_local_publish_child_from_known_id(child_run_id="another-unknown",
                source_run=self.run, request=SimpleNamespace(user=None), context=self.context,
                remote_data={"status": "completed"}))
        remote.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.count(), count)
        self.assertEqual(self.snapshot(), before)

    def test_unsealed_child_annotations_and_legacy_control_response_have_no_effect(self):
        before = self.snapshot()
        foreign = ContentFactoryRun.objects.create(run_id="unsealed-child", organization=self.org,
            domain=self.org.domain, workflow="article_generation", status="running")
        foreign_before = ContentFactoryRun.objects.values().get(pk=foreign.pk)
        views._attach_publish_child_to_run(self.run, foreign.run_id)
        views._mark_publish_child_missing_remote_on_run(self.run, foreign.run_id)
        child = views._sync_publish_child_from_control_response(run=self.run, remote_run=self.run,
            request=SimpleNamespace(user=None), context=self.context, payload={},
            remote_data={"run_id": foreign.run_id, "status": "completed"}, mark_source_approved=True)
        self.assertIsNone(child)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=foreign.pk), foreign_before)

    def test_owned_refresh_uses_only_committed_event_history(self):
        before = self.snapshot()
        with patch("content_factory.run_reconciliation.reconcile_authoritative_run") as events, \
                patch.object(views, "_call_content_factory_run_status") as snapshot:
            result = views._refresh_publish_child_remote_state(self.run, context=self.context)
        events.assert_called_once_with(self.run.run_id, refresh=True)
        snapshot.assert_not_called()
        self.assertEqual(result.pk, self.run.pk)
        self.assertEqual(self.snapshot(), before)

    def test_legacy_pending_hint_retains_existing_contract(self):
        legacy = ContentFactoryRun.objects.create(run_id="legacy-publish-source", organization=self.org,
            domain=self.org.domain, workflow="article_generation", status="running", run_request={"topic": "Legacy"})
        views._mark_publish_handoff_pending(run=legacy, auto_merge=True)
        legacy.refresh_from_db()
        self.assertTrue(legacy.result["publish_handoff_pending"])
        self.assertTrue(legacy.run_request["publish_auto_merge"])
