"""Real Django query construction with only row evaluation replaced; no database."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.db.models.query import QuerySet
from django.test import SimpleTestCase
from django.utils import timezone

from content_factory import vibe_marketing_views as views
from workflow_runs.models import ContentFactoryRun


class BootstrapDraftReuseTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.organization = SimpleNamespace(domain="draft-fixture.test")
        self.loaded = [self.make_run(1), self.make_run(2)]
        self.rows = {run.pk: run for run in self.loaded}
        self.reads = []
        def evaluate(query):
            if query._result_cache is not None:
                return
            self.assertIs(query.model, ContentFactoryRun)
            ordered = sorted(self.rows.values(), key=lambda row: row.updated_at, reverse=True)
            if query._fields is not None:
                self.assertEqual(query._fields, ("pk", "updated_at"))
                self.reads.append("versions")
                query._result_cache = [(run.pk, run.updated_at) for run in ordered][query.query.low_mark:query.query.high_mark]
            else:
                identifiers = next((node.rhs for node in query.query.where.children
                    if getattr(node, "lookup_name", None) == "in" and getattr(getattr(node, "lhs", None), "target", None).name == "id"), None)
                self.reads.append(tuple(identifiers) if identifiers is not None else "full")
                query._result_cache = [run for run in ordered if identifiers is None or run.pk in identifiers]
                query._result_cache = query._result_cache[query.query.low_mark:query.query.high_mark]
            query._prefetch_done = True
        self.enterContext(patch.object(QuerySet, "_fetch_all", evaluate))

    def make_run(self, identifier):
        row = ContentFactoryRun(id=identifier, run_id=f"draft-{identifier}", domain="draft-fixture.test",
            workflow="article_generation", status="completed", updated_at=self.now, created_at=self.now,
            result={"title": f"Draft {identifier}", "review_draft_html": "synthetic-large-review" * 1000},
            run_request={"keyword": f"topic-{identifier}"}, acceptance_summary={}, step_order=[])
        row._prefetched_objects_cache = {"steps": []}
        return row

    def test_identical_loaded_versions_do_not_fetch_another_result_blob(self):
        selected, owned = views._article_draft_run_window(self.organization, scan_limit=20, loaded_runs=self.loaded)
        self.assertEqual(self.reads, ["versions"])
        self.assertEqual(owned, [])
        self.assertTrue(all(any(run is original for original in self.loaded) for run in selected))

    def test_changed_version_and_rows_outside_display_window_are_loaded(self):
        changed = self.make_run(1)
        changed.updated_at += timedelta(seconds=1)
        changed.result["title"] = "Changed current draft"
        self.rows[1] = changed
        outside = self.make_run(3)
        self.rows[3] = outside
        selected, owned = views._article_draft_run_window(self.organization, scan_limit=20, loaded_runs=self.loaded)
        self.assertEqual(self.reads, ["versions", (1, 3)])
        self.assertEqual(selected, [changed, self.loaded[1], outside])
        self.assertEqual(owned, [changed, outside])
        self.assertEqual(selected[0].result["title"], "Changed current draft")

    def test_foreign_cached_domain_cannot_supply_a_draft_row(self):
        foreign = self.make_run(1)
        foreign.domain = "foreign-fixture.test"
        selected, owned = views._article_draft_run_window(self.organization, scan_limit=20, loaded_runs=[foreign, self.loaded[1]])
        self.assertIs(selected[0], self.rows[1])
        self.assertIsNot(selected[0], foreign)
        self.assertEqual(owned, [self.rows[1]])

    def test_draft_serialization_releases_only_owned_prefetch_graphs(self):
        outside = self.make_run(3)
        self.rows[3] = outside
        with patch.object(views, "_written_article_identity_keys", return_value={}):
            drafts = views._recent_article_drafts(self.organization, loaded_runs=self.loaded)
        self.assertEqual([card["runId"] for card in drafts], ["draft-1", "draft-2", "draft-3"])
        self.assertIn("steps", self.loaded[0]._prefetched_objects_cache)
        self.assertNotIn("steps", outside._prefetched_objects_cache)
        self.assertNotIn("synthetic-large-review", str(drafts))
