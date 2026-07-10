"""
Reconciliation sweep guards.

content-factory's callback outbox gives up after ~2 hours of retries, so a
terminal event can be permanently lost while the local run stays
queued/running forever; dispatch failures also leave local vibe-marketing-*
placeholder runs content-factory never accepted. The sweep must adopt the
remote truth for stuck runs, fail honestly what content-factory has no
record of, never probe placeholder ids (probing unknown ids creates ghost
artifact dirs on content-factory), and throttle itself so the every-minute
scheduler tick stays cheap.
"""
from datetime import timedelta
from unittest.mock import patch

import requests

from django.test import TestCase, override_settings
from django.utils import timezone

from content_factory.reconciliation import (
    MISSING_REMOTE_FAILURE_ERROR,
    PLACEHOLDER_FAILURE_ERROR,
    _finalize_run_failure,
    run_content_factory_reconciliation_sweep,
)
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus


def _make_run(run_id, *, workflow="direct_generate", status=ContentFactoryRunStatus.RUNNING, age_minutes=90):
    run = ContentFactoryRun.objects.create(
        run_id=run_id,
        workflow=workflow,
        domain="example.com",
        status=status,
    )
    # updated_at is auto_now; push it into the past via queryset update.
    ContentFactoryRun.objects.filter(pk=run.pk).update(
        updated_at=timezone.now() - timedelta(minutes=age_minutes)
    )
    run.refresh_from_db()
    return run


class _FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@override_settings(CONTENT_FACTORY_URL="http://cf.test", CONTENT_FACTORY_API_KEY="test-key")
class ReconciliationSweepTests(TestCase):
    def test_placeholder_run_fails_without_probing_content_factory(self):
        _make_run("vibe-marketing-direct_generate-abc123", status=ContentFactoryRunStatus.QUEUED)

        with patch("content_factory.reconciliation.requests.get") as mock_get:
            summary = run_content_factory_reconciliation_sweep()

        mock_get.assert_not_called()
        self.assertEqual(summary["failed_placeholder"], 1)
        run = ContentFactoryRun.objects.get(run_id="vibe-marketing-direct_generate-abc123")
        self.assertEqual(run.status, ContentFactoryRunStatus.FAILED)
        self.assertEqual(run.error, PLACEHOLDER_FAILURE_ERROR)
        self.assertFalse(run.resume_available)
        self.assertIsNotNone(run.reconciled_at)
        self.assertEqual(run.result["reconciliation"]["outcome"], "placeholder_never_dispatched")

    def test_stuck_run_adopts_remote_terminal_state(self):
        _make_run("real-run-1", workflow="article_system_setup")
        remote = {
            "run_id": "real-run-1",
            "workflow": "article_system_setup",
            "status": "completed",
            "current_step": "create_pull_request",
            "step_states": {},
        }

        with patch(
            "content_factory.reconciliation.requests.get",
            return_value=_FakeResponse(200, remote),
        ) as mock_get:
            summary = run_content_factory_reconciliation_sweep()

        mock_get.assert_called_once()
        self.assertIn("/api/runs/real-run-1", mock_get.call_args.args[0])
        self.assertEqual(summary["adopted"], 1)
        run = ContentFactoryRun.objects.get(run_id="real-run-1")
        self.assertEqual(run.status, ContentFactoryRunStatus.COMPLETED)
        self.assertEqual(run.current_step, "create_pull_request")
        self.assertIsNotNone(run.reconciled_at)

    def test_remote_external_status_labels_are_normalized(self):
        _make_run("real-run-blocked", workflow="direct_generate")
        remote = {
            "run_id": "real-run-blocked",
            "workflow": "direct_generate",
            "status": "blocked_verification",
        }

        with patch(
            "content_factory.reconciliation.requests.get",
            return_value=_FakeResponse(200, remote),
        ):
            summary = run_content_factory_reconciliation_sweep()

        self.assertEqual(summary["adopted"], 1)
        run = ContentFactoryRun.objects.get(run_id="real-run-blocked")
        self.assertEqual(run.status, ContentFactoryRunStatus.BLOCKED)

    def test_run_missing_on_remote_fails_honestly(self):
        _make_run("real-run-2", workflow="repo_scan", status=ContentFactoryRunStatus.QUEUED)

        with patch(
            "content_factory.reconciliation.requests.get",
            return_value=_FakeResponse(404, {"detail": "Run not found"}),
        ):
            summary = run_content_factory_reconciliation_sweep()

        self.assertEqual(summary["failed_missing"], 1)
        run = ContentFactoryRun.objects.get(run_id="real-run-2")
        self.assertEqual(run.status, ContentFactoryRunStatus.FAILED)
        self.assertEqual(run.error, MISSING_REMOTE_FAILURE_ERROR)
        self.assertEqual(run.result["reconciliation"]["outcome"], "missing_on_remote")

    def test_remote_still_active_leaves_run_alone_and_throttles_reprobe(self):
        _make_run("real-run-3", workflow="direct_generate")
        remote = {"run_id": "real-run-3", "workflow": "direct_generate", "status": "processing"}

        with patch(
            "content_factory.reconciliation.requests.get",
            return_value=_FakeResponse(200, remote),
        ) as mock_get:
            first = run_content_factory_reconciliation_sweep()
            second = run_content_factory_reconciliation_sweep()

        # Second sweep must not re-probe within the probe interval.
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(first["remote_active"], 1)
        self.assertEqual(second["checked"], 0)
        run = ContentFactoryRun.objects.get(run_id="real-run-3")
        self.assertEqual(run.status, ContentFactoryRunStatus.RUNNING)

    def test_fresh_runs_are_not_probed(self):
        _make_run("real-run-fresh", age_minutes=5)

        with patch("content_factory.reconciliation.requests.get") as mock_get:
            summary = run_content_factory_reconciliation_sweep()

        mock_get.assert_not_called()
        self.assertEqual(summary["checked"], 0)

    def test_terminal_runs_are_not_probed(self):
        _make_run("real-run-done", status=ContentFactoryRunStatus.COMPLETED)

        with patch("content_factory.reconciliation.requests.get") as mock_get:
            summary = run_content_factory_reconciliation_sweep()

        mock_get.assert_not_called()
        self.assertEqual(summary["checked"], 0)

    def test_workflows_outside_durable_read_are_skipped(self):
        # content-factory's GET /api/runs cannot see these workflows' stores;
        # a 404 would prove nothing, so the sweep must not touch them.
        _make_run("real-run-autofill", workflow="startup_autofill")

        with patch("content_factory.reconciliation.requests.get") as mock_get:
            summary = run_content_factory_reconciliation_sweep()

        mock_get.assert_not_called()
        self.assertEqual(summary["checked"], 0)
        run = ContentFactoryRun.objects.get(run_id="real-run-autofill")
        self.assertEqual(run.status, ContentFactoryRunStatus.RUNNING)

    def test_network_error_leaves_run_untouched_but_stamped(self):
        _make_run("real-run-4", workflow="direct_generate")

        with patch(
            "content_factory.reconciliation.requests.get",
            side_effect=requests.ConnectionError("boom"),
        ):
            summary = run_content_factory_reconciliation_sweep()

        self.assertEqual(summary["errors"], 1)
        run = ContentFactoryRun.objects.get(run_id="real-run-4")
        self.assertEqual(run.status, ContentFactoryRunStatus.RUNNING)
        self.assertIsNotNone(run.reconciled_at)

    def test_non_2xx_probe_leaves_run_untouched(self):
        _make_run("real-run-5", workflow="direct_generate")

        with patch(
            "content_factory.reconciliation.requests.get",
            return_value=_FakeResponse(503, {"detail": "maintenance"}),
        ):
            summary = run_content_factory_reconciliation_sweep()

        self.assertEqual(summary["errors"], 1)
        run = ContentFactoryRun.objects.get(run_id="real-run-5")
        self.assertEqual(run.status, ContentFactoryRunStatus.RUNNING)

    def test_finalize_loses_race_against_fresh_callback(self):
        # A callback may complete the run between candidate selection and
        # the failure write; the guarded finalize must yield.
        run = _make_run("real-run-6", workflow="direct_generate")
        ContentFactoryRun.objects.filter(pk=run.pk).update(
            status=ContentFactoryRunStatus.COMPLETED
        )

        changed = _finalize_run_failure(
            "real-run-6",
            error="should not apply",
            outcome="missing_on_remote",
            now=timezone.now(),
        )

        self.assertFalse(changed)
        run.refresh_from_db()
        self.assertEqual(run.status, ContentFactoryRunStatus.COMPLETED)
        self.assertEqual(run.error, "")

    def test_batch_limit_bounds_probes_per_tick(self):
        for index in range(4):
            _make_run(f"real-run-batch-{index}", workflow="direct_generate")
        remote = {"workflow": "direct_generate", "status": "processing"}

        with override_settings(
            CONTENT_FACTORY_URL="http://cf.test",
            CONTENT_FACTORY_RECONCILIATION_BATCH_LIMIT=2,
        ):
            with patch(
                "content_factory.reconciliation.requests.get",
                return_value=_FakeResponse(200, remote),
            ) as mock_get:
                summary = run_content_factory_reconciliation_sweep()

        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(summary["checked"], 2)


class ReconciliationUnconfiguredTests(TestCase):
    @override_settings(CONTENT_FACTORY_URL="", IS_LOCAL_ENV=False)
    def test_placeholders_still_fail_without_remote_config(self):
        _make_run("vibe-marketing-repo_scan-def456", status=ContentFactoryRunStatus.QUEUED)
        _make_run("real-run-7", workflow="direct_generate")

        with patch("content_factory.reconciliation.requests.get") as mock_get:
            summary = run_content_factory_reconciliation_sweep()

        mock_get.assert_not_called()
        self.assertEqual(summary["failed_placeholder"], 1)
        self.assertEqual(summary["errors"], 1)
        placeholder = ContentFactoryRun.objects.get(run_id="vibe-marketing-repo_scan-def456")
        self.assertEqual(placeholder.status, ContentFactoryRunStatus.FAILED)
        real = ContentFactoryRun.objects.get(run_id="real-run-7")
        self.assertEqual(real.status, ContentFactoryRunStatus.RUNNING)
