"""Database-free contract tests for founder island removal (no migrations)."""
import sys
import unittest
from contextlib import nullcontext
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from django.conf import settings
if not settings.configured:
    settings.configure(SECRET_KEY="island-removal-unit-only", USE_TZ=True, USE_I18N=False,
        DATABASES={"default": {"ENGINE": "django.db.backends.dummy"}},
        REST_FRAMEWORK={"UNAUTHENTICATED_USER": None, "DEFAULT_AUTHENTICATION_CLASSES": []})
from rest_framework.test import APIRequestFactory, force_authenticate

from . import island_removal
from .island_removal import (
    release_from_selections, remove_island, removed_island_centroids, suppressed_by_removed_island,
)


def module(name, **attrs):
    value = ModuleType(name)
    value.__dict__.update(attrs)
    return value


def selection_run(state):
    run = SimpleNamespace(result={"island_research_selection": state, "suggested_islands": []}, save=Mock())
    return run


class SuppressionTests(unittest.TestCase):
    def test_same_theme_is_suppressed_and_distinct_theme_is_not(self):
        removed = [[1.0, 0.0, 0.0]]
        self.assertTrue(suppressed_by_removed_island([0.99, 0.05, 0.0], removed))
        self.assertFalse(suppressed_by_removed_island([0.6, 0.8, 0.0], removed))
        self.assertFalse(suppressed_by_removed_island([0.0, 1.0, 0.0], removed))

    def test_missing_vectors_never_suppress(self):
        self.assertFalse(suppressed_by_removed_island([], [[1.0, 0.0]]))
        self.assertFalse(suppressed_by_removed_island([1.0, 0.0], []))

    def test_only_removed_islands_with_centroids_count(self):
        islands = [
            SimpleNamespace(removed_at="2026-10-20", centroid_embedding=[1.0, 0.0]),
            SimpleNamespace(removed_at=None, centroid_embedding=[0.0, 1.0]),
            SimpleNamespace(removed_at="2026-10-20", centroid_embedding=[]),
        ]
        self.assertEqual(removed_island_centroids(islands), [[1.0, 0.0]])


class ReleaseFromSelectionsTests(unittest.TestCase):
    def release(self, runs, slug="balcony-gardens"):
        queryset = Mock()
        queryset.select_for_update.return_value = runs
        with patch("content_factory.island_selection.selection_runs", return_value=queryset):
            release_from_selections(SimpleNamespace(pk="org"), slug)

    def test_removed_theme_can_be_picked_again_and_stops_evolving(self):
        run = selection_run({
            "selected_ids": ["a", "b", "c"], "managed_proposal_ids": ["a", "b", "c"],
            "managed_slugs": ["balcony-gardens", "herbs"], "revision": 3, "pending": {"signature": "x"},
            "groups": [{"slug": "old-balcony", "proposal_ids": ["a", "b"]},
                       {"slug": "herbs", "proposal_ids": ["c"]}],
            "redirects": {"old-balcony": "balcony-gardens"},
        })
        self.release([run])
        state = run.result["island_research_selection"]
        self.assertEqual(state["selected_ids"], ["c"])
        self.assertEqual(state["managed_proposal_ids"], ["c"])
        self.assertEqual(state["managed_slugs"], ["herbs"])
        self.assertEqual(state["groups"], [{"slug": "herbs", "proposal_ids": ["c"]}])
        self.assertEqual(state["revision"], 4)
        self.assertNotIn("pending", state)
        run.save.assert_called_once_with(update_fields=["result", "updated_at"])

    def test_unrelated_selection_is_left_alone(self):
        state = {"selected_ids": ["c"], "managed_slugs": ["herbs"], "revision": 1,
                 "groups": [{"slug": "herbs", "proposal_ids": ["c"]}]}
        run = selection_run(dict(state))
        self.release([run])
        self.assertEqual(run.result["island_research_selection"], state)
        run.save.assert_not_called()


class RemoveIslandTests(unittest.TestCase):
    def setUp(self):
        self.island = SimpleNamespace(slug="balcony-gardens", status="visible", archived_at=None,
                                      removed_at=None, save=Mock())
        self.islands = Mock()
        self.islands.filter.return_value.first.return_value = self.island
        self.org = SimpleNamespace(pk="org")
        organizations = Mock()
        organizations.select_for_update.return_value.get.return_value = self.org
        self.rebuild = Mock()
        self.modules = {
            "organizations.models": module("organizations.models",
                Organization=SimpleNamespace(objects=organizations)),
            "content_factory.models": module("content_factory.models",
                ContentIsland=SimpleNamespace(objects=self.islands),
                ContentIslandStatus=SimpleNamespace(ARCHIVED="archived")),
            "content_factory.content_islands": module("content_factory.content_islands",
                rebuild_island_edges=self.rebuild),
        }

    def remove(self, slug="balcony-gardens"):
        with patch.dict(sys.modules, self.modules), \
                patch.object(island_removal.transaction, "atomic", nullcontext), \
                patch.object(island_removal, "release_from_selections") as release:
            return remove_island(self.org, slug, now="now"), release

    def test_removal_archives_marks_and_releases_the_island(self):
        island, release = self.remove()
        self.assertIs(island, self.island)
        self.assertEqual((island.status, island.archived_at, island.removed_at), ("archived", "now", "now"))
        island.save.assert_called_once_with(update_fields=["status", "archived_at", "removed_at", "updated_at"])
        release.assert_called_once_with(self.org, "balcony-gardens")
        self.rebuild.assert_called_once_with(self.org)
        self.islands.filter.assert_called_once_with(organization=self.org, slug="balcony-gardens")

    def test_repeating_a_removal_changes_nothing(self):
        self.island.removed_at = "earlier"
        island, release = self.remove()
        self.assertEqual(island.removed_at, "earlier")
        island.save.assert_not_called()
        release.assert_not_called()

    def test_unknown_island_is_reported_missing(self):
        self.islands.filter.return_value.first.return_value = None
        island, release = self.remove("nope")
        self.assertIsNone(island)
        release.assert_not_called()


class RemoveViewTests(unittest.TestCase):
    def post(self, removed):
        from .custom_island_views import ContentIslandRemoveView
        context = SimpleNamespace(organization=SimpleNamespace(pk="org"))
        views = module("content_factory.vibe_marketing_views",
            _resolve_context_or_response=Mock(return_value=(context, None)))
        request = APIRequestFactory().post("/islands/balcony-gardens/remove", {}, format="json")
        force_authenticate(request, user=SimpleNamespace(pk="user", is_authenticated=True))
        with patch.dict(sys.modules, {"content_factory.vibe_marketing_views": views}), \
                patch.object(island_removal, "remove_island", return_value=removed) as remove:
            response = ContentIslandRemoveView.as_view()(request, slug="balcony-gardens")
        remove.assert_called_once_with(context.organization, "balcony-gardens")
        return response

    def test_removed_island_is_confirmed(self):
        response = self.post(SimpleNamespace(slug="balcony-gardens"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"removed": "balcony-gardens"})

    def test_missing_island_is_404(self):
        self.assertEqual(self.post(None).status_code, 404)


if __name__ == "__main__":
    unittest.main()
