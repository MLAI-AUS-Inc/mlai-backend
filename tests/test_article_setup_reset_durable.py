"""Durable article-setup reset (P0).

The reset used to be a fragile timestamp watermark: a re-scan's persist dropped
the marker (un-resetting), and a run's `updated_at` could move past it. These
tests cover the durable version — tombstoning the exact run ids at reset time,
carrying the markers across a re-scan, and honoring the reset in the scaffold
gate (which `_profile_checks` calls with unfiltered runs).
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from content_factory.article_setup_reset import (
    article_setup_reset_excluded_run_ids,
    article_setup_reset_ignores_run,
    carry_reset_markers,
    reset_article_setup_config,
)
from content_factory.article_system import resolve_article_system
from content_factory.models import OrganizationContentConfig
from content_factory.vibe_marketing_views import (
    _article_setup_state_for_config,
    _article_system_setup_gate,
)
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun

REPO = "The-Product-Bus/tpbnewsite"


class DurableResetTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(domain="theproductbus.com", name="TPB")
        self.config = OrganizationContentConfig.objects.create(organization=self.org, github_repo=REPO)

    def _setup_run(self, run_id, status="blocked"):
        return ContentFactoryRun.objects.create(
            domain=self.org.domain,
            run_id=run_id,
            workflow="article_system_setup",
            github_repo=REPO,
            status=status,
            result={"article_system_setup": {"setup_run_id": run_id, "status": "preview_failed"}},
        )

    def _bump_updated_at(self, run_id, when):
        # .update() bypasses auto_now, so we can simulate a later touch.
        ContentFactoryRun.objects.filter(run_id=run_id).update(updated_at=when)

    def test_reset_tombstones_existing_setup_runs(self):
        self._setup_run("old-1")
        self._setup_run("old-2")
        result = reset_article_setup_config(self.config, github_repo=REPO)
        self.assertEqual(set(result["excludedRunIds"]), {"old-1", "old-2"})
        self.config.refresh_from_db()
        self.assertEqual(article_setup_reset_excluded_run_ids(self.config), {"old-1", "old-2"})

    def test_tombstoned_run_ignored_even_when_touched_after_reset(self):
        run = self._setup_run("old-1")
        reset_article_setup_config(self.config)
        self._bump_updated_at("old-1", timezone.now() + timedelta(hours=1))  # watermark-defeating touch
        run.refresh_from_db()
        self.assertTrue(article_setup_reset_ignores_run(self.config, run))

    def test_new_run_after_reset_is_not_ignored(self):
        reset_article_setup_config(self.config)  # nothing to tombstone yet
        new_run = self._setup_run("new-1")
        self._bump_updated_at("new-1", timezone.now() + timedelta(hours=1))
        new_run.refresh_from_db()
        self.assertFalse(article_setup_reset_ignores_run(self.config, new_run))

    def test_article_setup_state_clean_after_reset(self):
        self._setup_run("old-1")
        reset_article_setup_config(self.config)
        self._bump_updated_at("old-1", timezone.now() + timedelta(hours=1))
        state = _article_setup_state_for_config(self.config, organization=self.org)
        self.assertIsNone(state.get("setupRunId"))
        self.assertFalse(state.get("setupBlocked"))

    def test_reset_restores_default_path_patterns(self):
        # A stale pattern (e.g. from a prior repo layout) must not survive the reset, or a
        # re-scaffold inherits the wrong content/registry location.
        self.config.article_path_pattern = "src/app/articles/content/{category}/{slug}.tsx"
        self.config.registry_path = "src/app/articles/registry.ts"
        self.config.save(update_fields=["article_path_pattern", "registry_path"])

        result = reset_article_setup_config(self.config)
        self.config.refresh_from_db()

        default_pattern = OrganizationContentConfig._meta.get_field("article_path_pattern").get_default()
        default_registry = OrganizationContentConfig._meta.get_field("registry_path").get_default()
        self.assertEqual(self.config.article_path_pattern, default_pattern)
        self.assertEqual(self.config.registry_path, default_registry)
        self.assertIn("article_path_pattern", result["cleared_fields"])
        self.assertIn("registry_path", result["cleared_fields"])

    def test_gate_honors_reset_with_unfiltered_runs(self):
        old = self._setup_run("old-1")
        reset_article_setup_config(self.config)
        self._bump_updated_at("old-1", timezone.now() + timedelta(hours=1))
        old.refresh_from_db()
        # _profile_checks hands the gate UNFILTERED runs; it must still drop tombstoned ones.
        gate = _article_system_setup_gate(self.config, [old], resolve_article_system(self.config))
        self.assertIsNone(gate.get("setupRunId"))
        self.assertFalse(gate.get("setupBlocked"))

    def test_carry_reset_markers_survives_a_renormalized_article_system(self):
        self._setup_run("old-1")
        reset_article_setup_config(self.config)
        raw = dict(self.config.article_system)
        rescanned = {"state": "missing", "source": "scan"}  # a fresh normalized scan result, markers dropped
        carry_reset_markers(raw, rescanned)
        self.assertIn("article_setup_reset", rescanned)
        self.assertEqual(
            set((rescanned.get("article_setup_reset") or {}).get("excludedRunIds") or []),
            {"old-1"},
        )

    def test_dropping_markers_resurfaces_the_run(self):
        # Negative control: the old behavior (markers wiped by a scan) un-tombstones the run.
        run = self._setup_run("old-1")
        reset_article_setup_config(self.config)
        self.config.article_system = {"state": "missing", "source": "scan"}  # no carry_reset_markers
        self.config.save(update_fields=["article_system", "updated_at"])
        run.refresh_from_db()
        self.assertFalse(article_setup_reset_ignores_run(self.config, run))
