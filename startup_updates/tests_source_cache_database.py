"""PostgreSQL parity, provenance, deletion and Linear cursor regressions for B3."""
import copy
import json
from datetime import date
from unittest.mock import patch
from django.db import connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from startup_updates.models import SourceExtractionReceipt, StartupProfile, UserStartupBinding, LinearProjectSelection
from startup_updates.source_evidence import source_receipt, stage_source, complete_source, _legacy_cached_receipt
from startup_updates import source_evidence
from integrations.models import ExternalServiceConnection, ExternalServiceProvider, ExternalServiceConnectionStatus
from integrations.services.external_connectors import ConnectorRateLimitError
from integrations.tests_startup_updates import StartupUpdateApiTestCase
from startup_updates.services import create_startup_update_run


class SourceExtractionReceiptParityTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Receipt fixture", domain="receipts.invalid")
        self.request = {"startup_context": {"notes": "Frozen 🦘"}, "backfill_window_end": "2026-10-18",
            "draft_months": ["2026-10-01"]}
        self.bundle = {"cleaned_text": "The synthetic pilot shipped.", "attachments": []}
        self.count = 0

    def make_run(self, *, organization=None, workflow="startup_monthly_update", status="completed"):
        self.count += 1
        return ContentFactoryRun.objects.create(run_id=f"receipt-{self.count}", organization=organization or self.org,
            workflow=workflow, domain=(organization or self.org).domain, status=status,
            run_request=copy.deepcopy(self.request))

    def seed(self, run, label, *, indexed=True):
        receipt = source_receipt(run, "gmail", "thread", self.bundle)
        run.result = {"source_evidence": {"gmail:thread": receipt}}
        output = {"source_fingerprint": receipt["fingerprint"], "events": [{"title": label}]}
        with transaction.atomic():
            if indexed:
                complete_source(run, "gmail", "thread", output)
            else:
                run.result["source_evidence"]["gmail:thread"]["output"] = output
            run.save(update_fields=["result", "updated_at"])
        return output

    def expected(self, run, bundle=None):
        fingerprint = source_receipt(run, "gmail", "thread", self.bundle if bundle is None else bundle)["fingerprint"]
        found = _legacy_cached_receipt(run, "gmail:thread", fingerprint)
        return found[1].get("output") if found is not None else None

    def test_index_hit_matches_original_lookup_and_uses_fingerprint_table(self):
        origin = self.make_run()
        output = self.seed(origin, "indexed")
        target = self.make_run()
        expected = self.expected(target)
        with patch.object(source_evidence, "_legacy_cached_receipt", wraps=source_evidence._legacy_cached_receipt) as legacy, \
                CaptureQueriesContext(connection) as queries:
            result = stage_source(target, "gmail", "thread", self.bundle)
        self.assertEqual(result["cached_extraction"], expected)
        self.assertEqual(result["cached_extraction"], output)
        self.assertEqual(legacy.call_args.kwargs, {"after_id": origin.pk})
        self.assertTrue(any("startup_updates_sourceextractionreceipt" in q["sql"] and "fingerprint" in q["sql"] for q in queries.captured_queries))

    def test_index_miss_and_changed_source_match_original_lookup(self):
        origin = self.make_run()
        self.seed(origin, "legacy", indexed=False)
        target = self.make_run()
        self.assertEqual(stage_source(target, "gmail", "thread", self.bundle)["cached_extraction"], self.expected(target))
        changed = {"cleaned_text": "Changed source."}
        self.assertIsNone(stage_source(self.make_run(), "gmail", "thread", changed)["cached_extraction"])

    def test_cancelled_index_origin_is_never_reused_and_older_valid_run_still_wins(self):
        valid = self.make_run()
        expected = self.seed(valid, "valid")
        cancelled = self.make_run()
        self.seed(cancelled, "cancelled")
        cancelled.status = "cancelled"
        cancelled.save(update_fields=["status", "updated_at"])
        target = self.make_run()
        self.assertEqual(stage_source(target, "gmail", "thread", self.bundle)["cached_extraction"], expected)
        self.assertEqual(self.expected(target), expected)
        valid.status = "cancelled"
        valid.save(update_fields=["status", "updated_at"])
        self.assertIsNone(stage_source(self.make_run(), "gmail", "thread", self.bundle)["cached_extraction"])

    def test_newer_legacy_run_wins_over_an_older_index_hit(self):
        indexed = self.make_run()
        self.seed(indexed, "older indexed")
        newer = self.make_run()
        expected = self.seed(newer, "newer legacy", indexed=False)
        target = self.make_run()
        self.assertEqual(stage_source(target, "gmail", "thread", self.bundle)["cached_extraction"], expected)
        self.assertEqual(self.expected(target), expected)

    def test_out_of_order_completions_keep_newest_origin_and_one_unique_row(self):
        older, newer = self.make_run(), self.make_run()
        expected = self.seed(newer, "newest origin")
        self.seed(older, "older finished later")
        entry = SourceExtractionReceipt.objects.get()
        self.assertEqual(entry.run_id, newer.pk)
        self.assertEqual(entry.output, expected)
        self.assertEqual(stage_source(self.make_run(), "gmail", "thread", self.bundle)["cached_extraction"], expected)

    def test_origin_receipt_removal_or_output_change_invalidates_stale_index(self):
        origin = self.make_run()
        self.seed(origin, "old output")
        origin.result = {}
        origin.save(update_fields=["result", "updated_at"])
        self.assertIsNone(stage_source(self.make_run(), "gmail", "thread", self.bundle)["cached_extraction"])
        updated = self.seed(origin, "replacement output", indexed=False)
        target = self.make_run()
        self.assertEqual(stage_source(target, "gmail", "thread", self.bundle)["cached_extraction"], updated)
        self.assertEqual(self.expected(target), updated)

    def test_wrong_organization_and_workflow_are_not_cache_hits(self):
        origin = self.make_run()
        self.seed(origin, "private original")
        other = Organization.objects.create(name="Other", domain="other-receipts.invalid")
        for target in (self.make_run(organization=other), self.make_run(workflow="different_workflow")):
            self.assertIsNone(stage_source(target, "gmail", "thread", self.bundle)["cached_extraction"])
            self.assertIsNone(self.expected(target))

    def test_completed_run_privacy_scrub_removes_indexed_copy(self):
        from startup_updates.data_deletion import _scrub_runs
        origin = self.make_run(status="completed")
        self.seed(origin, "private")
        with transaction.atomic():
            _scrub_runs([origin], request_id="synthetic-deletion", reason="test")
        self.assertFalse(SourceExtractionReceipt.objects.filter(run=origin).exists())
        origin.refresh_from_db()
        self.assertEqual(origin.status, "completed")
        self.assertTrue(origin.result["data_deleted"])
        self.assertIsNone(stage_source(self.make_run(), "gmail", "thread", self.bundle)["cached_extraction"])

    def test_current_completed_receipt_still_short_circuits_without_index_lookup(self):
        run = self.make_run()
        self.seed(run, "already completed")
        with patch.object(source_evidence, "_indexed_cached_receipt", side_effect=AssertionError("must reuse current checkpoint")):
            self.assertIsNone(stage_source(run, "gmail", "thread", self.bundle))


class LinearBackfillMultiProjectTests(StartupUpdateApiTestCase):
    def setUp(self):
        super().setUp()
        StartupProfile.objects.create(organization=self.organization)
        binding = UserStartupBinding.objects.create(user=self.user, organization=self.organization,
            google_connection=self.google_connection)
        self.run = create_startup_update_run(organization=self.organization, binding=binding, target_month=date(2026, 3, 1))
        self.run.run_request.update(input_sources=["linear"], linear_project_ids=["one", "two", "three"])
        self.run.save(update_fields=["run_request", "updated_at"])
        self.linear = ExternalServiceConnection.objects.create(provider=ExternalServiceProvider.LINEAR,
            user=self.user, organization=self.organization, access_token="synthetic-linear-token",
            status=ExternalServiceConnectionStatus.CONNECTED)
        for index, project_id in enumerate(("one", "two", "three")):
            LinearProjectSelection.objects.create(organization=self.organization, connection=self.linear, user=self.user,
                linear_project_id=project_id, project_name=f"{index} {project_id}", selected=True)

    def post(self):
        with self._with_key():
            return self.client.post(reverse("startup_updates_linear_backfill", args=[self.run.run_id]),
                {}, format="json", **self.headers)

    def test_one_call_consumes_multiple_project_cursors(self):
        with patch("integrations.services.external_connectors._linear_graphql_request", return_value={"project": None}) as request:
            response = self.post()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["projects_synced"], 3)
        self.assertFalse(response.data["has_more"])
        self.assertEqual(request.call_count, 3)
        self.assertTrue(all(row.sync_cursor["run_backfill_complete"] for row in LinearProjectSelection.objects.filter(connection=self.linear)))
        self.run.refresh_from_db()
        saved = self.run.run_request["external_context"]["linear"]["last_sync"]
        self.assertEqual(saved["projects_synced"], 1)
        self.assertEqual(len(saved["projects"]), 1)
        self.assertEqual(saved["projects"][0]["project_id"], "three")

    def test_partial_rate_limit_retains_progress_and_resume_only_fetches_remaining_project(self):
        with patch("integrations.services.external_connectors._linear_graphql_request",
                side_effect=[{"project": None}, {"project": None}, ConnectorRateLimitError(19)]) as first:
            response = self.post()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["projects_synced"], 2)
        self.assertTrue(response.data["has_more"])
        self.assertEqual(response.data["retry_after_seconds"], 19)
        self.assertEqual(first.call_count, 3)
        self.assertEqual(LinearProjectSelection.objects.filter(connection=self.linear,
            sync_cursor__run_backfill_complete=True).count(), 2)
        with patch("integrations.services.external_connectors._linear_graphql_request", return_value={"project": None}) as retry:
            completed = self.post()
        self.assertEqual(completed.status_code, 200, completed.data)
        self.assertFalse(completed.data["has_more"])
        self.assertEqual(retry.call_count, 1)
        self.assertEqual(retry.call_args.args[2]["id"], "three")
