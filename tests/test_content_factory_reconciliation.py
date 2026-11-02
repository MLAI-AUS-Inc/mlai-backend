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
from io import StringIO
import json
from unittest.mock import patch

import requests

from django.test import TestCase, override_settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from content_factory.reconciliation import (
    MISSING_REMOTE_FAILURE_ERROR,
    PLACEHOLDER_FAILURE_ERROR,
    _finalize_run_failure,
    preview_discovery_reconciliation,
    run_content_factory_reconciliation_sweep,
)
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus


def _make_run(run_id, *, workflow="direct_generate", status=ContentFactoryRunStatus.RUNNING, age_minutes=90):
    from content_factory.models import OrganizationContentConfig
    from organizations.models import Organization
    from tests.website_fixtures import bind_config_fixture
    org, _ = Organization.objects.get_or_create(domain='example.com', defaults={'name': 'Example'})
    config, _ = OrganizationContentConfig.objects.get_or_create(organization=org)
    from content_factory.website_connections import contract_for
    binding = contract_for(config.website_connection) if config.website_connection_id else bind_config_fixture(config)
    run = ContentFactoryRun.objects.create(
        organization=org, run_request=binding,
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
        remote = {"run_id": "real-run-3", "workflow": "direct_generate", "status": "processing",
                  "activity_heartbeat_at": timezone.now().isoformat()}

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
        # Unreviewed workflow recovery contracts stay outside this sweep.
        _make_run("real-run-autofill", workflow="startup_autofill")

        with patch("content_factory.reconciliation.requests.get") as mock_get:
            summary = run_content_factory_reconciliation_sweep()

        mock_get.assert_not_called()
        self.assertEqual(summary["checked"], 0)
        run = ContentFactoryRun.objects.get(run_id="real-run-autofill")
        self.assertEqual(run.status, ContentFactoryRunStatus.RUNNING)

    def test_discovery_requires_an_aware_explicit_activation_boundary(self):
        _make_run("discovery-disabled", workflow="auto_discovery")
        for boundary in ("", None, "disabled", "invalid", "2026-11-02", "2026-11-02T10:00:00", "2026-99-99T00:00:00Z"):
            with self.subTest(boundary=boundary), override_settings(CONTENT_FACTORY_DISCOVERY_RECONCILIATION_SINCE=boundary), \
                    patch("content_factory.reconciliation.requests.get") as get:
                self.assertEqual(run_content_factory_reconciliation_sweep()["checked"], 0)
                get.assert_not_called()

    def test_discovery_boundary_preserves_history_and_adopts_new_terminal_result(self):
        since = timezone.now() - timedelta(hours=2)
        historical = _make_run("discovery-history", workflow="auto_discovery")
        eligible = _make_run("discovery-new", workflow="auto_discovery")
        ContentFactoryRun.objects.filter(pk=eligible.pk).update(slack_user_id="U_DISCOVERY",
            run_request={"domain": "example.com", "requested_topic_count": 2, "client_request_id": "original-dispatch"})
        eligible.refresh_from_db()
        identity = (eligible.domain, eligible.github_repo, eligible.slack_user_id, eligible.organization_id, eligible.run_request)
        ContentFactoryRun.objects.filter(pk=historical.pk).update(created_at=since - timedelta(seconds=1))
        ContentFactoryRun.objects.filter(pk=eligible.pk).update(created_at=since)
        before = ContentFactoryRun.objects.filter(pk=historical.pk).values().get()
        response = _FakeResponse(200, {"run_id": eligible.run_id, "workflow": "auto_discovery",
                                       "status": "awaiting_confirmation", "step_states": {}})
        with override_settings(CONTENT_FACTORY_DISCOVERY_RECONCILIATION_SINCE=since.isoformat()), \
                patch("content_factory.reconciliation.requests.get", return_value=response) as get:
            summary = run_content_factory_reconciliation_sweep()
        self.assertEqual(summary["checked"], 1)
        self.assertEqual(summary["adopted"], 1)
        get.assert_called_once()
        self.assertIn(eligible.run_id, get.call_args.args[0])
        self.assertEqual(ContentFactoryRun.objects.filter(pk=historical.pk).values().get(), before)
        eligible.refresh_from_db()
        self.assertEqual(eligible.status, "awaiting_confirmation")
        self.assertEqual((eligible.domain, eligible.github_repo, eligible.slack_user_id, eligible.organization_id, eligible.run_request), identity)

    def test_discovery_adoption_rejects_foreign_identity_and_preserves_current_owner_stop(self):
        from content_factory.reconciliation import _adopt_remote_payload
        run = _make_run("discovery-scope", workflow="auto_discovery")
        before = ContentFactoryRun.objects.filter(pk=run.pk).values().get()
        for changes in ({"run_id": "foreign"}, {"workflow": "direct_generate"}, {"domain": "foreign.test"},
                        {"run_request": {"domain": "foreign.test"}}, {"slack_user_id": "foreign"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                _adopt_remote_payload(run, {"status": "completed", **changes})
        self.assertEqual(ContentFactoryRun.objects.filter(pk=run.pk).values().get(), before)
        ContentFactoryRun.objects.filter(pk=run.pk).update(status="cancelled")
        self.assertEqual(_adopt_remote_payload(run, {"status": "completed"}), "cancelled")
        run.refresh_from_db()
        self.assertEqual(run.status, "cancelled")

    def test_discovery_worker_recovery_stays_with_cf_orphan_sweeper(self):
        now = timezone.now()
        run = _make_run("discovery-stale-heartbeat", workflow="auto_discovery")
        response = _FakeResponse(200, {"run_id": run.run_id, "workflow": "auto_discovery", "status": "running",
                                      "last_progress_at": (now - timedelta(hours=3)).isoformat()})
        with override_settings(CONTENT_FACTORY_DISCOVERY_RECONCILIATION_SINCE=(now - timedelta(days=1)).isoformat()), \
                patch("content_factory.reconciliation.requests.get", return_value=response), \
                patch("content_factory.reconciliation.requests.post", side_effect=AssertionError("Unexpected worker control")):
            summary = run_content_factory_reconciliation_sweep(now=now)
        self.assertEqual(summary["remote_active"], 1)
        self.assertEqual(summary["recovery_requested"], 0)
        run.refresh_from_db()
        self.assertEqual(run.status, "running")
        self.assertNotIn("reconciliation_recovery", run.result)

    def test_discovery_preview_is_bounded_and_never_runs_provider_or_mutating_sweep(self):
        since = timezone.now() - timedelta(days=1)
        for index in range(3):
            _make_run(f"discovery-preview-{index}", workflow="auto_discovery")
        _make_run("discovery-awaiting", workflow="auto_discovery", status="awaiting_confirmation")
        _make_run("discovery-fresh", workflow="auto_discovery", age_minutes=1)
        _make_run("other-workflow", workflow="direct_generate")
        recent_probe = _make_run("discovery-recent-probe", workflow="auto_discovery")
        ContentFactoryRun.objects.filter(pk=recent_probe.pk).update(reconciled_at=timezone.now())
        history = _make_run("discovery-old", workflow="auto_discovery")
        ContentFactoryRun.objects.filter(pk=history.pk).update(created_at=since - timedelta(seconds=1))
        before = list(ContentFactoryRun.objects.order_by("pk").values())
        with patch("content_factory.reconciliation.run_content_factory_reconciliation_sweep", side_effect=AssertionError("Mutation")), \
                patch("requests.sessions.Session.request", side_effect=AssertionError("Network")):
            result = preview_discovery_reconciliation(created_since=since.isoformat(), limit=2)
            out = StringIO()
            call_command("preview_discovery_reconciliation", "--created-since", since.isoformat(), "--limit", "2", stdout=out)
        self.assertEqual(json.loads(out.getvalue()), result)
        self.assertEqual(result["eligible"], 3)
        self.assertEqual(result["selected"], 2)
        self.assertEqual(result["selected_by_status"], {"running": 2})
        self.assertEqual(result["remote_actions"], "not_probed")
        self.assertEqual(list(ContentFactoryRun.objects.order_by("pk").values()), before)
        self.assertNotIn("example.com", out.getvalue())
        self.assertNotIn("discovery-preview-", out.getvalue())

    def test_discovery_preview_rejects_unbounded_or_naive_scope(self):
        for boundary, limit in (("2026-11-02", 10), ("bad", 10), ("2026-11-02T00:00:00Z", 0),
                                ("2026-11-02T00:00:00Z", 101)):
            with self.subTest(boundary=boundary, limit=limit), self.assertNumQueries(0), self.assertRaises(CommandError):
                call_command("preview_discovery_reconciliation", "--created-since", boundary, "--limit", str(limit))

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
        remote = {"workflow": "direct_generate", "status": "processing",
                  "activity_heartbeat_at": timezone.now().isoformat()}

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
