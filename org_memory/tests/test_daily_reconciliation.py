import hashlib
import os
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from integrations.models import ExternalServiceConnection
from organizations.models import Organization
from org_memory.connectors.base import (
    ConnectorHealth,
    DryRunResult,
    ScopePage,
    SourcePreview,
    SyncPage,
)
from org_memory.connectors.registry import connector_registry
from org_memory.kernel import capture_source_version, create_work_item, kernel_health_snapshot
from org_memory.models import (
    MemoryActionStatus,
    MemoryActionType,
    MemoryConnectionConfiguration,
    MemoryConnectionHealthStatus,
    MemoryConnectionState,
    MemoryCostReservation,
    MemoryCostReservationStatus,
    MemoryDailyCostLedger,
    MemoryDailyReconciliationReport,
    MemoryDailyReconciliationStatus,
    MemoryDeadLetter,
    MemoryOutboxEvent,
    MemoryProviderEnablement,
    MemoryScopeStatus,
    MemorySourceActionRequest,
    MemorySourceScope,
    MemoryWorkItem,
    MemoryWorkStatus,
    MemoryWorkTaskType,
)
from org_memory.reconciliation import run_daily_reconciliation
from org_memory.runtime import (
    claim_memory_work,
    dispatch_pending_actions,
    execute_claimed_memory_work,
)


class DailyLinearConnector:
    provider = "linear"

    def __init__(self):
        self.pages = []

    def discover_scopes(self, configuration, cursor=None):
        return ScopePage(scopes=())

    def preview(self, configuration, selected_scopes, policy):
        return SourcePreview(summary={})

    def dry_run(self, configuration, selected_scopes, policy):
        return DryRunResult(summary={})

    def backfill(self, configuration, selected_scopes, checkpoint):
        return self.incremental_sync(configuration, None)

    def incremental_sync(self, configuration, cursor):
        return self.pages.pop(0) if self.pages else SyncPage(records=())

    def refresh_permissions(self, configuration, checkpoint):
        return SyncPage(records=())

    def fetch_version(self, configuration, external_id):
        raise AssertionError("not used")

    def tombstone_missing(self, configuration, sync_run):
        raise AssertionError("not used")

    def health(self, configuration):
        last_sync = configuration.last_successful_sync_at
        lag = (
            max(int((timezone.now() - last_sync).total_seconds()), 0)
            if last_sync
            else None
        )
        return ConnectorHealth(
            status=configuration.lifecycle_state,
            credential_status="connected",
            last_successful_sync_at=last_sync.isoformat() if last_sync else None,
            source_lag_seconds=lag,
            details={"content_free": True},
        )


@override_settings(
    IS_PRODUCTION_ENV=False,
    ORG_MEMORY_DAILY_RECONCILIATION_TIME_ZONE="UTC",
    ORG_MEMORY_DAILY_RECONCILIATION_HOUR=0,
    ORG_MEMORY_PROVIDER_SYNC_INTERVAL_SECONDS={"linear": 7200},
    ORG_MEMORY_PROVIDER_FRESHNESS_SLO_SECONDS={"linear": 3600},
    ORG_MEMORY_DAILY_MODEL_COST_CEILING_AUD="0",
)
class DailyReconciliationTests(TestCase):
    def setUp(self):
        self.enabled_patch = patch.dict(
            os.environ,
            {"ORG_MEMORY_ENABLED_PROVIDERS": "linear"},
        )
        self.enabled_patch.start()
        self.organization = Organization.objects.create(
            name="Daily Brain",
            domain="daily-brain.test",
        )
        self.user = get_user_model().objects.create_user(email="daily@brain.test")
        self.connection = ExternalServiceConnection.objects.create(
            provider="linear",
            user=self.user,
            organization=self.organization,
            external_account_id="daily-linear",
            account_label="Daily Linear",
        )
        MemoryProviderEnablement.objects.create(
            organization=self.organization,
            provider="linear",
            is_enabled=True,
            approved_by=self.user,
            approved_at=timezone.now(),
        )
        self.configuration = MemoryConnectionConfiguration.objects.create(
            organization=self.organization,
            provider="linear",
            external_connection=self.connection,
            lifecycle_state=MemoryConnectionState.ACTIVE,
            last_successful_sync_at=timezone.now() - timedelta(hours=3),
            next_scheduled_sync_at=timezone.now() - timedelta(hours=2),
            created_by=self.user,
        )
        self.scope = MemorySourceScope.objects.create(
            configuration=self.configuration,
            scope_type="project",
            external_id="daily-project",
            name="Daily project",
            selected=True,
            status=MemoryScopeStatus.SELECTED,
            default_classification="committee",
        )
        self.original_connector = connector_registry.get("linear")
        self.connector = DailyLinearConnector()
        connector_registry.register(self.connector, replace=True)

    def tearDown(self):
        connector_registry.register(self.original_connector, replace=True)
        self.enabled_patch.stop()
        super().tearDown()

    def test_daily_catch_up_runs_once_and_finishes_as_a_noop(self):
        now = timezone.now()
        first = run_daily_reconciliation(now=now, force=True)
        report = MemoryDailyReconciliationReport.objects.get(
            organization=self.organization
        )
        snapshot = report.connection_snapshots.get(configuration=self.configuration)

        self.assertEqual(first["status"], "processed")
        self.assertEqual(report.status, MemoryDailyReconciliationStatus.RUNNING)
        self.assertEqual(snapshot.schedule_status, "reconciling")
        self.assertEqual(snapshot.health_status, MemoryConnectionHealthStatus.SYNCING)
        self.assertTrue(snapshot.catch_up)
        self.assertEqual(snapshot.provider_interval_seconds, 7200)
        self.assertEqual(snapshot.freshness_slo_seconds, 3600)
        self.assertEqual(self.configuration.action_requests.count(), 1)

        self.assertEqual(dispatch_pending_actions(limit=10)["dispatched"], 1)
        claim = claim_memory_work(worker_id="daily-worker")
        self.assertEqual(execute_claimed_memory_work(claim)["status"], "completed")
        second = run_daily_reconciliation(now=now + timedelta(minutes=1), force=True)
        report.refresh_from_db()
        snapshot.refresh_from_db()

        self.assertEqual(second["reports"][0]["status"], "completed")
        self.assertEqual(report.status, MemoryDailyReconciliationStatus.COMPLETED)
        self.assertEqual(snapshot.schedule_status, "noop")
        self.assertEqual(snapshot.health_status, MemoryConnectionHealthStatus.HEALTHY)
        self.assertEqual(snapshot.freshness_status, "current")
        self.configuration.refresh_from_db()
        self.assertAlmostEqual(
            (
                self.configuration.next_scheduled_sync_at
                - self.configuration.last_successful_sync_at
            ).total_seconds(),
            7200,
            delta=2,
        )

        run_daily_reconciliation(now=now + timedelta(minutes=2))
        self.assertEqual(self.configuration.action_requests.count(), 1)
        self.assertEqual(MemoryDailyReconciliationReport.objects.count(), 1)

    def test_recent_completed_sync_is_reused_and_stale_source_degrades_report(self):
        now = timezone.now()
        action = MemorySourceActionRequest.objects.create(
            configuration=self.configuration,
            action=MemoryActionType.SYNC,
            status=MemoryActionStatus.COMPLETED,
            idempotency_key="manual-current-window",
            completed_at=now,
            result_summary={"records": 0, "removals": 0},
        )
        self.configuration.last_successful_sync_at = now - timedelta(hours=2)
        self.configuration.next_scheduled_sync_at = now + timedelta(hours=1)
        self.configuration.save(
            update_fields=(
                "last_successful_sync_at",
                "next_scheduled_sync_at",
                "updated_at",
            )
        )

        result = run_daily_reconciliation(now=now, force=True)
        snapshot = self.configuration.daily_health_snapshots.get()

        self.assertEqual(self.configuration.action_requests.count(), 1)
        self.assertEqual(snapshot.action_request_id, action.pk)
        self.assertEqual(snapshot.freshness_status, "stale")
        self.assertEqual(snapshot.health_status, MemoryConnectionHealthStatus.STALE)
        self.assertEqual(result["reports"][0]["status"], "degraded")
        self.assertIn(
            "freshness_slo_missed",
            {row["code"] for row in result["reports"][0]["alerts"]},
        )

    def test_resolved_dead_letter_does_not_degrade_current_connection_health(self):
        now = timezone.now()
        MemorySourceActionRequest.objects.create(
            configuration=self.configuration,
            action=MemoryActionType.SYNC,
            status=MemoryActionStatus.COMPLETED,
            idempotency_key="manual-current-window-resolved-dead-letter",
            completed_at=now,
            result_summary={"records": 0, "removals": 0},
        )
        self.configuration.last_successful_sync_at = now
        self.configuration.next_scheduled_sync_at = now + timedelta(hours=2)
        self.configuration.save(
            update_fields=(
                "last_successful_sync_at",
                "next_scheduled_sync_at",
                "updated_at",
            )
        )
        work = MemoryWorkItem.objects.create(
            organization=self.organization,
            provider="linear",
            task_type=MemoryWorkTaskType.EXTRACT,
            configuration=self.configuration,
            idempotency_key="resolved-historical-dead-work",
            payload={},
            status=MemoryWorkStatus.DEAD,
            attempts=1,
            max_attempts=1,
            completed_at=now,
            last_error="Historical repaired failure",
        )
        MemoryDeadLetter.objects.create(
            work_item=work,
            organization=self.organization,
            task_type=MemoryWorkTaskType.EXTRACT,
            payload_snapshot={},
            attempts=1,
            last_error="Historical repaired failure",
            resolved_at=now,
            resolved_by=self.user,
        )

        result = run_daily_reconciliation(now=now, force=True)
        snapshot = self.configuration.daily_health_snapshots.get()

        self.assertEqual(result["reports"][0]["status"], "completed")
        self.assertEqual(snapshot.health_status, MemoryConnectionHealthStatus.HEALTHY)
        self.assertEqual(snapshot.counts["work_dead"], 0)

    def test_active_connection_without_selected_scope_is_reported_as_error(self):
        self.scope.delete()

        result = run_daily_reconciliation(now=timezone.now(), force=True)
        snapshot = self.configuration.daily_health_snapshots.get()

        self.assertEqual(snapshot.schedule_status, "error")
        self.assertEqual(snapshot.health_status, MemoryConnectionHealthStatus.ERROR)
        self.assertEqual(self.configuration.action_requests.count(), 0)
        self.assertEqual(result["reports"][0]["status"], "degraded")

    def test_disabled_provider_never_attempts_watch_renewal_or_sync(self):
        with patch.dict(
            os.environ,
            {"ORG_MEMORY_ENABLED_PROVIDERS": ""},
        ), patch(
            "org_memory.reconciliation._renew_watch_for_configuration"
        ) as renew:
            result = run_daily_reconciliation(now=timezone.now(), force=True)

        snapshot = self.configuration.daily_health_snapshots.get()
        renew.assert_not_called()
        self.assertEqual(snapshot.schedule_status, "error")
        self.assertEqual(snapshot.watch_status, "disabled")
        self.assertEqual(self.configuration.action_requests.count(), 0)
        self.assertEqual(result["reports"][0]["status"], "degraded")

    def test_organization_filter_does_not_create_other_reports(self):
        other = Organization.objects.create(name="Other", domain="other-daily.test")
        other_connection = ExternalServiceConnection.objects.create(
            provider="linear",
            user=self.user,
            organization=other,
            external_account_id="other-linear",
        )
        MemoryConnectionConfiguration.objects.create(
            organization=other,
            provider="linear",
            external_connection=other_connection,
            lifecycle_state=MemoryConnectionState.ACTIVE,
        )

        run_daily_reconciliation(
            now=timezone.now(),
            organization_id=self.organization.pk,
            force=True,
        )

        self.assertTrue(
            MemoryDailyReconciliationReport.objects.filter(
                organization=self.organization
            ).exists()
        )
        self.assertFalse(
            MemoryDailyReconciliationReport.objects.filter(organization=other).exists()
        )

    def test_kernel_health_exposes_latest_daily_report_without_source_content(self):
        run_daily_reconciliation(now=timezone.now(), force=True)

        health = kernel_health_snapshot(organization=self.organization)

        self.assertEqual(
            health["daily_reconciliation"]["report_date"],
            MemoryDailyReconciliationReport.objects.get().report_date,
        )
        self.assertNotIn("connections", health["daily_reconciliation"])

    @override_settings(
        ORG_MEMORY_DAILY_MODEL_COST_CEILING_AUD="0.010000",
        ORG_MEMORY_EMBEDDING_COST_AUD_PER_MILLION_TOKENS="1.000000",
    )
    def test_model_cost_ceiling_defers_expensive_work_without_blocking_sync(self):
        text = "budgeted evidence"
        source, version, _created = capture_source_version(
            organization=self.organization,
            provider="linear",
            external_account_id="daily-linear",
            source_type="issue",
            external_id="LIN-COST",
            version_key="v1",
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            classification="committee",
            acl={"is_accessible": True, "principal_refs": ["team:daily"]},
            chunks=[{"ordinal": 0, "text": text, "token_count": 20000}],
            configuration=self.configuration,
            source_scope=self.scope,
        )
        MemoryOutboxEvent.objects.all().delete()
        chunk = version.chunks.get()
        work, _created = create_work_item(
            organization=self.organization,
            provider="linear",
            task_type=MemoryWorkTaskType.EMBED,
            idempotency_key="daily-cost-block",
            source=source,
            source_version=version,
            configuration=self.configuration,
            payload={"chunk_id": str(chunk.pk)},
        )

        self.assertIsNone(claim_memory_work(worker_id="budget-worker"))
        work.refresh_from_db()
        ledger = MemoryDailyCostLedger.objects.get(organization=self.organization)

        self.assertEqual(work.status, MemoryWorkStatus.PENDING)
        self.assertGreater(work.available_at, timezone.now())
        self.assertIn("Daily model cost ceiling reached", work.last_error)
        self.assertEqual(ledger.reserved_aud, 0)
        self.assertFalse(MemoryCostReservation.objects.exists())

        run_daily_reconciliation(now=timezone.now(), force=True)
        self.assertEqual(self.configuration.action_requests.count(), 1)

    @override_settings(
        ORG_MEMORY_DAILY_MODEL_COST_CEILING_AUD="1.000000",
        ORG_MEMORY_EMBEDDING_COST_AUD_PER_MILLION_TOKENS="1.000000",
        ORG_MEMORY_EMBEDDING_DIMENSIONS=1536,
    )
    def test_successful_metered_work_consumes_one_atomic_reservation(self):
        text = "small budgeted evidence"
        source, version, _created = capture_source_version(
            organization=self.organization,
            provider="linear",
            external_account_id="daily-linear",
            source_type="issue",
            external_id="LIN-COST-OK",
            version_key="v1",
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            classification="committee",
            acl={"is_accessible": True, "principal_refs": ["team:daily"]},
            chunks=[{"ordinal": 0, "text": text, "token_count": 100}],
            configuration=self.configuration,
            source_scope=self.scope,
        )
        MemoryOutboxEvent.objects.all().delete()
        chunk = version.chunks.get()
        work, _created = create_work_item(
            organization=self.organization,
            provider="linear",
            task_type=MemoryWorkTaskType.EMBED,
            idempotency_key="daily-cost-consume",
            source=source,
            source_version=version,
            configuration=self.configuration,
            payload={
                "chunk_id": str(chunk.pk),
                "model": "text-embedding-3-small",
                "version": "openai-text-embedding-3-small-v1",
                "dimensions": 1536,
            },
        )

        claim = claim_memory_work(worker_id="budget-worker")
        reservation = MemoryCostReservation.objects.get(work_item=work)
        self.assertEqual(reservation.status, MemoryCostReservationStatus.RESERVED)
        with patch(
            "org_memory.embeddings.OpenAIEmbeddingProvider.embed",
            return_value=[1.0] + [0.0] * 1535,
        ):
            self.assertEqual(execute_claimed_memory_work(claim)["status"], "completed")
        reservation.refresh_from_db()
        ledger = reservation.ledger
        ledger.refresh_from_db()

        self.assertEqual(reservation.status, MemoryCostReservationStatus.CONSUMED)
        self.assertEqual(ledger.reserved_aud, 0)
        self.assertEqual(ledger.consumed_aud, reservation.estimated_cost_aud)
