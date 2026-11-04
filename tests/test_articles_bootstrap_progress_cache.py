"""Database/network-free isolation and invalidation of workflow presentation."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import django
from django.apps import apps
from django.core.cache import cache
from django.test import SimpleTestCase

if not apps.ready:
    django.setup()

from content_factory import bootstrap_progress_cache as progress_cache
from content_factory import vibe_marketing_views as views


class BootstrapProgressCacheTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.now = datetime(2026, 10, 16, tzinfo=timezone.utc)
        self.context = SimpleNamespace(organization=SimpleNamespace(pk=901, domain="fixture.test"),
            profile=SimpleNamespace(user=SimpleNamespace(pk=902)))
        self.website = SimpleNamespace(pk="website-1", generation=1, repository_id=1, installation_id="2",
            branch="main", state="connected", configuration_version=1, verified_sha="a" * 40,
            authorized_by_id=902, updated_at=self.now)
        self.config = SimpleNamespace(updated_at=self.now, website_connection=self.website)
        self.runs = [SimpleNamespace(run_id="run-1", updated_at=self.now)]
        self.checks = {"scaffold": {"generationReady": True}}
        self.capabilities = {"canGenerateArticle": True, "canPublishArticle": True}
        self.candidates = []
        self.serialized = [{"runId": "run-1"}]
        self.versions = {"runs": {"count": 1, "updated": self.now}, "comments": {"count": 0, "updated": None}}
        self.enterContext(patch.object(views, "_bootstrap_cache_seconds", return_value=300))
        self._versions_reader = progress_cache._versions
        self.version_reader = self.enterContext(patch.object(progress_cache, "_versions", side_effect=lambda org: deepcopy(self.versions)))
        self.projector = self.enterContext(patch.object(views, "_workflow_progress",
            side_effect=lambda **kwargs: {"steps": [{"id": "review", "run": getattr(kwargs.get("run"), "run_id", None)}]}))

    def project(self):
        return progress_cache.project_workflow_progress(context=self.context, config=self.config, runs=self.runs,
            checks=self.checks, candidates=self.candidates, serialized_runs=self.serialized, capabilities=self.capabilities)

    def test_warm_reuse_copies_presentation_and_caps_ttl(self):
        with patch.object(cache, "set", wraps=cache.set) as stored:
            first = self.project()
        self.assertEqual(stored.call_args.args[2], 20)
        self.assertEqual(self.projector.call_count, 2)  # root + viewed run
        first["root"]["steps"].clear()
        first["runs"]["run-1"]["steps"].clear()
        second = self.project()
        second["root"]["steps"].clear()
        third = self.project()
        self.assertEqual(self.projector.call_count, 2)
        self.assertEqual(len(third["root"]["steps"]), 1)
        self.assertEqual(len(third["runs"]["run-1"]["steps"]), 1)
        self.assertNotIn("capabilities", third)
        self.assertNotIn("websiteJourney", third)

    def test_actor_org_config_connection_and_current_permissions_partition_cache(self):
        self.project()
        cases = [(self.context.profile.user, "pk", 903), (self.context.organization, "pk", 904),
            (self.context.organization, "domain", "other.test"), (self.config, "updated_at", self.now + timedelta(seconds=1)),
            (self.website, "state", "revoked"), (self.website, "generation", 2), (self.website, "repository_id", 2),
            (self.website, "installation_id", "3"), (self.website, "branch", "next"),
            (self.website, "configuration_version", 2), (self.website, "verified_sha", "b" * 40),
            (self.website, "authorized_by_id", 903), (self.website, "updated_at", self.now + timedelta(seconds=1))]
        for target, field, changed in cases:
            with self.subTest(field=field, value=changed):
                original = getattr(target, field)
                setattr(target, field, changed)
                self.projector.reset_mock()
                self.project()
                self.assertEqual(self.projector.call_count, 2)
                setattr(target, field, original)
        self.capabilities["canPublishArticle"] = False
        self.projector.reset_mock()
        self.project()
        self.assertEqual(self.projector.call_count, 2)
        self.checks["scaffold"]["generationReady"] = False
        self.projector.reset_mock()
        self.project()
        self.assertEqual(self.projector.call_count, 2)

    def test_run_and_comment_versions_outside_visible_window_invalidate(self):
        self.project()
        for category, field, value in (("runs", "count", 2), ("runs", "updated", self.now + timedelta(seconds=1)),
                ("comments", "count", 1), ("comments", "updated", self.now + timedelta(seconds=1))):
            with self.subTest(category=category, field=field):
                self.versions[category][field] = value
                self.projector.reset_mock()
                self.project()
                self.assertEqual(self.projector.call_count, 2)

    def test_disabled_cache_never_reads_versions_or_cache(self):
        with patch.object(views, "_bootstrap_cache_seconds", return_value=0), patch.object(cache, "get") as read, patch.object(cache, "set") as write:
            self.project()
            self.project()
        self.assertEqual(self.projector.call_count, 4)
        self.version_reader.assert_not_called()
        read.assert_not_called()
        write.assert_not_called()

    def test_displayed_runs_and_topic_availability_invalidate(self):
        self.project()
        self.runs[0].updated_at = self.now + timedelta(seconds=1)
        self.projector.reset_mock()
        self.project()
        self.assertEqual(self.projector.call_count, 2)
        self.candidates.append({"keyword": "new topic"})
        self.projector.reset_mock()
        self.project()
        self.assertEqual(self.projector.call_count, 2)
        self.runs.append(SimpleNamespace(run_id="run-2", updated_at=self.now))
        self.serialized.append({"runId": "run-2"})
        self.projector.reset_mock()
        result = self.project()
        self.assertEqual(self.projector.call_count, 3)
        self.assertEqual(set(result["runs"]), {"run-1", "run-2"})

    def test_cache_errors_compute_current_progress_without_logging_error_payload(self):
        with patch.object(cache, "get", side_effect=RuntimeError("private-cache-credential")), patch.object(
            cache, "set", side_effect=RuntimeError("private-cache-credential")
        ), self.assertLogs(progress_cache.logger, level="WARNING") as logs:
            result = self.project()
        self.assertEqual(self.projector.call_count, 2)
        self.assertEqual(result["root"]["steps"][0]["id"], "review")
        self.assertNotIn("private-cache-credential", " ".join(logs.output))

    def test_version_query_scope_covers_all_current_org_runs_and_comments(self):
        with patch.object(progress_cache.ContentFactoryRun.objects, "filter") as runs, patch.object(
            progress_cache.VibeMarketingComponentComment.objects, "filter"
        ) as comments:
            # Call the real reader, bypassing this fixture's patched wrapper.
            self._versions_reader(self.context.organization)
        runs.assert_called_once_with(domain="fixture.test")
        comments.assert_called_once_with(run__domain="fixture.test")
