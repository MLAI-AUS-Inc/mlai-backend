"""Offline source-throughput and exact cache-selection regressions."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase
from integrations.services.external_connectors import ConnectorRateLimitError
from startup_updates import api_views, source_evidence


def page(index, *, more=True):
    return {"connectionId": 1, "connection_id": 1, "provider": "linear",
        "status": "syncing" if more else "synced", "lastSyncedAt": f"synthetic-{index}",
        "projectsSynced": 1, "projects_synced": 1, "issuesSynced": index, "issues_synced": index,
        "updatesSynced": 2, "updates_synced": 2, "membersSynced": 3, "members_synced": 3,
        "projects": [{"projectId": f"project-{index}"}], "has_more": more}


class LinearBackfillSliceTests(SimpleTestCase):
    def call(self):
        return api_views._sync_linear_backfill_slice(SimpleNamespace(id=1), run_id="synthetic",
            project_ids=["one", "two"], include_unselected=True)

    def test_multi_project_counts_and_last_prompt_context_are_preserved(self):
        pages = [page(1), page(2), page(3, more=False)]
        original = copy.deepcopy(pages)
        with patch.object(api_views, "sync_linear_connection_page", side_effect=pages) as sync:
            result, last = self.call()
        self.assertEqual(result["projects_synced"], 3)
        self.assertEqual(result["issues_synced"], 6)
        self.assertEqual(result["updatesSynced"], 6)
        self.assertEqual(result["membersSynced"], 9)
        self.assertFalse(result["has_more"])
        self.assertEqual([row["projectId"] for row in result["projects"]], ["project-1", "project-2", "project-3"])
        self.assertEqual(json.dumps(last), json.dumps(original[-1]))
        self.assertEqual(pages, original)
        self.assertEqual(sync.call_count, 3)
        for call in sync.call_args_list:
            self.assertEqual(call.kwargs, {"run_id": "synthetic", "project_ids": ["one", "two"], "include_unselected": True})

    def test_ten_project_work_limit(self):
        with patch.object(api_views, "sync_linear_connection_page", return_value=page(1)) as sync:
            result, _ = self.call()
        self.assertEqual(sync.call_count, 10)
        self.assertEqual(result["projects_synced"], 10)
        self.assertTrue(result["has_more"])

    def test_twenty_second_deadline_stops_further_project_calls(self):
        now = [0.0]
        def sync(*args, **kwargs):
            now[0] += 11
            return page(int(now[0]))
        with patch.object(api_views.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(api_views, "sync_linear_connection_page", side_effect=sync) as calls:
            result, _ = self.call()
        self.assertEqual(calls.call_count, 2)
        self.assertEqual(result["projects_synced"], 2)
        self.assertTrue(result["has_more"])

    def test_partial_rate_limit_keeps_progress_and_exact_legacy_context(self):
        error = ConnectorRateLimitError(17)
        with patch.object(api_views, "sync_linear_connection_page", side_effect=[page(1), error]):
            result, last = self.call()
        self.assertEqual(result["projects_synced"], 1)
        self.assertEqual(result["issues_synced"], 1)
        self.assertEqual(len(result["projects"]), 1)
        self.assertEqual(result["retry_after_seconds"], 17)
        self.assertTrue(result["has_more"])
        self.assertEqual(last, api_views._linear_rate_limit_page(SimpleNamespace(id=1), error))
        with patch.object(api_views, "sync_linear_connection_page", side_effect=error):
            with self.assertRaises(ConnectorRateLimitError):
                self.call()


class IndexedReceiptSelectionTests(SimpleTestCase):
    def run_fixture(self):
        return SimpleNamespace(pk=9, organization_id=1, workflow="startup_monthly_update", result={},
            run_request={"startup_context": {"notes": "Frozen"}, "backfill_window_end": "2026-10-18"})

    def test_index_hit_bounds_legacy_lookup_to_newer_runs_and_keeps_request_shape(self):
        run = self.run_fixture()
        bundle = {"cleaned_text": "Synthetic original source."}
        with patch.object(source_evidence, "_indexed_cached_receipt", return_value={"run_id": 7, "output": {"events": []}}), \
                patch.object(source_evidence, "_legacy_cached_receipt", return_value=None) as legacy:
            result = source_evidence.stage_source(run, "gmail", "thread", bundle)
        self.assertEqual(result["cached_extraction"], {"events": []})
        self.assertEqual(set(result), {*bundle, "source_fingerprint", "extraction_version", "cached_extraction"})
        self.assertEqual(legacy.call_args.kwargs, {"after_id": 7})

    def test_newer_legacy_receipt_wins_and_index_miss_retains_old_scan(self):
        for indexed, after in (({"run_id": 7, "output": {"events": ["old"]}}, 7), (None, None)):
            with self.subTest(indexed=indexed), \
                    patch.object(source_evidence, "_indexed_cached_receipt", return_value=indexed), \
                    patch.object(source_evidence, "_legacy_cached_receipt", return_value=(8, {"output": {"events": ["new"]}})) as legacy:
                result = source_evidence.stage_source(self.run_fixture(), "gmail", "thread", {})
                self.assertEqual(result["cached_extraction"], {"events": ["new"]})
                self.assertEqual(legacy.call_args.kwargs, {"after_id": after})
