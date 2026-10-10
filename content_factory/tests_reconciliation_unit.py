"""Split-run recovery policy with synthetic timestamps and no database."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from contextlib import ExitStack, contextmanager, nullcontext
from copy import deepcopy
from uuid import uuid4

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import reconciliation


class SplitRunRecoveryTests(SimpleTestCase):
    def test_older_worker_sweep_cannot_claim_recovery_or_exhaust_refund(self):
        run, now = self.fake_run(), timezone.now()
        run.domain, run.workflow = "fixture.test", "article_generation"
        query = Mock()
        query.filter.return_value = query
        query.order_by.return_value = query
        query.__getitem__ = Mock(return_value=[run])
        with patch.object(reconciliation.ContentFactoryRun.objects, "filter", return_value=query), \
                patch.object(reconciliation, "_reconcile_orphan_operations", return_value={}), \
                patch.object(reconciliation, "_reconcile_restart_receipts", return_value=0), \
                patch.object(reconciliation, "_redrive_fixed_runs", return_value=0), \
                patch.object(reconciliation, "report_repeated_refusals", return_value=0), \
                patch.object(reconciliation, "_settle_sweep_refunds"), \
                patch.object(reconciliation, "_remote_base_url", return_value="https://fixture.test"), \
                patch.object(reconciliation, "_remote_headers", return_value={}), \
                patch.object(reconciliation, "_stamp_reconciled"), \
                patch.object(reconciliation.requests, "get", return_value=Mock(status_code=200,
                    json=Mock(return_value={"status": "running"}))), \
                patch.object(reconciliation, "worker_recovery_supported", return_value=False), \
                patch.object(reconciliation, "recover_stalled_run") as recover, \
                patch.object(reconciliation, "_finalize_run_failure") as failure:
            self.assertEqual(reconciliation.run_content_factory_reconciliation_sweep(now=now)["remote_active"], 1)
        recover.assert_not_called()
        failure.assert_not_called()

    def test_failed_recovery_cancels_the_workflow_write_authority(self):
        run, now = self.fake_run(), timezone.now()
        with self.failure_scope(run) as (events, operation, refund):
            self.assertTrue(reconciliation._finalize_run_failure(run.run_id, error="No progress",
                outcome="worker_recovery_exhausted", now=now))
        self.assertEqual(events[:4], ["organization", "connection", "operation", "run"])
        self.assertEqual(operation.state, "cancelled")
        self.assertEqual(operation.receipt["reason_code"], "worker_recovery_exhausted")
        refund.assert_called_once()

    @contextmanager
    def failure_scope(self, run, *, operation_state="running", changed_request=False, portable=False):
        from organizations.models import Organization
        from .website_models import WebsiteConnection, WebsiteConnectionOperation
        from .website_connections import contract_for
        events = []
        website = SimpleNamespace(pk=uuid4(), generation=3, repository_id=42, organization_id=run.organization_id,
            state="connected", github_repo="fixture/site", branch="main", app_root="",
            organization=SimpleNamespace(domain="fixture.test"))
        operation = SimpleNamespace(pk=uuid4(), state=operation_state, receipt={}, payload={"run_id": run.run_id}, save=Mock())
        run.run_request = {} if portable else {**contract_for(website), "operation_id": str(operation.pk), "operation_attempt": 1}
        original = {"organization_id": run.organization_id, "run_request": deepcopy(run.run_request)}
        if changed_request:
            run.run_request["connection_generation"] = 4
        def locked_row(name, row):
            selected = Mock()
            def first():
                events.append(name)
                return row
            selected.filter.return_value.first.side_effect = first
            return selected
        with ExitStack() as stack:
            stack.enter_context(patch.object(reconciliation.transaction, "atomic", side_effect=lambda: nullcontext()))
            read = stack.enter_context(patch.object(reconciliation.ContentFactoryRun.objects, "filter"))
            read.return_value.values.return_value.first.return_value = original
            for model, name, row in [(Organization, "organization", SimpleNamespace(pk=run.organization_id)),
                    (WebsiteConnection, "connection", website), (WebsiteConnectionOperation, "operation", operation),
                    (reconciliation.ContentFactoryRun, "run", run)]:
                stack.enter_context(patch.object(model.objects, "select_for_update", return_value=locked_row(name, row)))
            refund = stack.enter_context(patch.object(reconciliation, "_refund_failed_run"))
            yield events, operation, refund

    def test_failure_loses_to_current_terminal_scope_and_changed_binding(self):
        for status, operation_state, changed in [("completed", "running", False), ("running", "cancelled", False),
                ("running", "completed", False), ("running", "running", True)]:
            with self.subTest(status=status, operation=operation_state, changed=changed):
                run = self.fake_run()
                run.status = status
                with self.failure_scope(run, operation_state=operation_state, changed_request=changed) as (_, _, refund):
                    self.assertFalse(reconciliation._finalize_run_failure(run.run_id, error="No progress",
                        outcome="worker_recovery_exhausted", now=timezone.now()))
                run.save.assert_not_called()
                refund.assert_not_called()

    def test_portable_failure_locks_only_its_company_then_run(self):
        run = self.fake_run()
        with self.failure_scope(run, portable=True) as (events, _, refund):
            self.assertTrue(reconciliation._finalize_run_failure(run.run_id, error="No progress",
                outcome="worker_recovery_exhausted", now=timezone.now()))
        self.assertEqual(events, ["organization", "run"])
        refund.assert_called_once()

    def test_recovery_failure_cannot_cancel_a_newer_operation_attempt(self):
        run = self.fake_run()
        with self.failure_scope(run) as (_, operation, refund):
            operation.payload["attempt"] = 2
            self.assertFalse(reconciliation._finalize_run_failure(run.run_id, error="No progress",
                outcome="worker_recovery_exhausted", now=timezone.now()))
        operation.save.assert_not_called()
        run.save.assert_not_called()
        refund.assert_not_called()

    def test_recovery_requires_confirmed_worker_endpoint_version(self):
        for version, allowed in [("2.0.9", False), ("2.1.0", True), (None, False)]:
            with self.subTest(version=version), patch.object(reconciliation.requests, "get",
                    return_value=Mock(status_code=200, json=Mock(return_value={"runtime": {"version": version}}))):
                self.assertIs(reconciliation.worker_recovery_supported(base_url="https://fixture.test", headers={}), allowed)
        with patch.object(reconciliation.requests, "get", side_effect=reconciliation.requests.ConnectionError):
            self.assertFalse(reconciliation.worker_recovery_supported(base_url="https://fixture.test", headers={}))

    def fake_run(self):
        return SimpleNamespace(run_id="fixture-run", result={}, run_request={}, status="running",
            updated_at=timezone.now() - timedelta(minutes=12), organization_id=7, save=Mock())

    def test_fresh_worker_heartbeat_never_recovers(self):
        now = timezone.now()
        self.assertFalse(reconciliation.remote_heartbeat_stale({"activity_heartbeat_at": now.isoformat()}, now=now))
        self.assertTrue(reconciliation.remote_heartbeat_stale({"activity_heartbeat_at": (now - timedelta(minutes=11)).isoformat()}, now=now))

    def test_recovery_acceptance_is_explicit_not_http_success(self):
        self.assertFalse(reconciliation.recovery_was_accepted({"status": "running", "already_queued": True}))
        self.assertTrue(reconciliation.recovery_was_accepted({"recovery_requested": True}))

    def test_recovery_attempt_is_bounded_and_persisted(self):
        run = self.fake_run()
        now = timezone.now()
        with patch.object(reconciliation, "_claim_run_recovery", return_value=True), \
                patch.object(reconciliation, "_record_recovery_result") as record, \
                patch.object(reconciliation.requests, "post", return_value=Mock(status_code=202,
                    json=Mock(return_value={"recovery_requested": True}))) as post:
            self.assertEqual(reconciliation.recover_stalled_run(run, base_url="https://fixture.test", headers={}, now=now), "recovery_requested")
        self.assertTrue(post.call_args.args[0].endswith("/recover"))
        self.assertTrue(record.call_args.kwargs["accepted"])

    def test_blocked_code_requires_confirmed_fixed_version(self):
        self.assertFalse(reconciliation.fixed_failure_can_redrive("website_configuration_changed", "2.0.0"))
        self.assertTrue(reconciliation.fixed_failure_can_redrive("website_configuration_changed", "2.1.0"))
        self.assertFalse(reconciliation.fixed_failure_can_redrive("unknown_failure", "99.0.0"))

    def test_refund_completion_merges_latest_result_after_io(self):
        from roo.models import Ledger
        run = self.fake_run()
        run.pk, run.domain = 1, "fixture.test"
        run.result = {"reconciliation_refund_pending": True}
        latest = SimpleNamespace(result={"reconciliation_refund_pending": True, "worker_receipt": "later"}, save=Mock())
        with patch.object(reconciliation.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(Ledger.objects, "filter") as ledger, \
                patch.object(reconciliation.ContentFactoryRun.objects, "select_for_update") as runs:
            ledger.return_value.select_related.return_value.first.return_value = None
            runs.return_value.filter.return_value.first.return_value = latest
            reconciliation._refund_failed_run(run, reason="test")
        self.assertEqual(latest.result["worker_receipt"], "later")
        self.assertFalse(latest.result["reconciliation_refund_pending"])

    @override_settings(CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID="")
    def test_unconfigured_ops_alert_makes_no_external_call(self):
        with patch("integrations.services.slack.SlackService.get_client") as client:
            self.assertEqual(reconciliation._deliver_pending_refusal_alerts(limit=20, now=timezone.now()), 0)
        client.assert_not_called()

    @override_settings(CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID="C0000000000")
    def test_ops_alert_acknowledgement_is_saved_and_not_sent_twice(self):
        run = self.fake_run()
        run.pk, run.workflow = 1, "article_generation"
        now = timezone.now()
        run.result = {"refusal_alert_reported": now.isoformat(), "authority_refusals": [{"code": "website_connection_changed"}] * 4}
        query = Mock()
        query.get.return_value = run
        query.filter.return_value.first.return_value = run
        with patch.object(reconciliation.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(reconciliation.ContentFactoryRun.objects, "filter", return_value=[run]), \
                patch.object(reconciliation.ContentFactoryRun.objects, "select_for_update", return_value=query), \
                patch("integrations.services.slack.SlackService.get_client") as client:
            client.return_value.chat_postMessage.return_value = {"ok": True, "ts": "fixture-ts"}
            self.assertEqual(reconciliation._deliver_pending_refusal_alerts(limit=20, now=now), 1)
            self.assertEqual(reconciliation._deliver_pending_refusal_alerts(limit=20, now=now), 0)
        self.assertEqual(client.return_value.chat_postMessage.call_count, 1)
        self.assertEqual(run.result["refusal_alert_delivery"]["message_id"], "fixture-ts")

    def test_orphan_receipt_uses_dispatch_key_lookup(self):
        op = SimpleNamespace(payload={"client_request_id": "saved-key"})
        with patch("content_factory.vibe_marketing_views._content_factory_remote_config", return_value={"enabled": True}) as config, \
                patch("content_factory.vibe_marketing_views._lookup_content_factory_dispatch_by_key", return_value=("dispatched", {"run_id": "actual-child"})) as lookup:
            self.assertEqual(reconciliation._lookup_orphan_child(op), ("dispatched", {"run_id": "actual-child"}))
        lookup.assert_called_once_with(config.return_value, "saved-key")

    def test_restart_no_child_releases_only_confirmed_rejected_reuse(self):
        self.assertEqual(reconciliation.restart_receipt_resolution("absent"), "failed")
        self.assertEqual(reconciliation.restart_receipt_resolution("rejected"), "failed")
        self.assertEqual(reconciliation.restart_receipt_resolution("dispatched"), "completed")
        self.assertEqual(reconciliation.restart_receipt_resolution("unknown"), "pending")
