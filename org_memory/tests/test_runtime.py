import hashlib
import os
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from integrations.models import ExternalServiceConnection
from organizations.models import Organization
from org_memory.connectors.base import ConnectorHealth, DryRunResult, ScopePage, SourcePreview, SyncPage
from org_memory.connectors.registry import connector_registry
from org_memory.kernel import (
    capture_source_version,
    create_work_item,
    suspend_configuration_runtime,
)
from org_memory.models import (
    MemoryActionStatus,
    MemoryActionType,
    MemoryConnectionConfiguration,
    MemoryConnectionState,
    MemoryChunkEmbedding,
    MemoryDeadLetter,
    MemoryOutboxEvent,
    MemoryOutboxEventType,
    MemoryOutboxStatus,
    MemoryProviderEnablement,
    MemoryRuntimeLane,
    MemoryScopeStatus,
    MemorySource,
    MemorySourceActionRequest,
    MemorySourceScope,
    MemorySyncRun,
    MemorySyncRunStatus,
    MemoryWorkItem,
    MemoryWorkerLease,
    MemoryWorkStatus,
    MemoryWorkTaskType,
    OrganizationMembership,
)
from org_memory.runtime import (
    LEGACY_ACCESS_RESTORED_ERROR,
    claim_memory_work,
    dispatch_outbox_events,
    dispatch_pending_actions,
    execute_claimed_memory_work,
    heartbeat_memory_work,
    memory_queue_snapshot,
    recover_expired_leases,
    reconcile_access_restored_dead_letters,
    requeue_dead_letter,
    schedule_due_connections,
)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FakeLinearRuntimeConnector:
    provider = "linear"

    def __init__(self):
        self.pages = []
        self.error = None
        self.cursors = []

    def discover_scopes(self, configuration, cursor=None):
        return ScopePage(scopes=())

    def preview(self, configuration, selected_scopes, policy):
        return SourcePreview(summary={})

    def dry_run(self, configuration, selected_scopes, policy):
        return DryRunResult(summary={})

    def backfill(self, configuration, selected_scopes, checkpoint):
        return self.incremental_sync(configuration, None)

    def incremental_sync(self, configuration, cursor):
        self.cursors.append(cursor)
        if self.error:
            raise self.error
        return self.pages.pop(0)

    def refresh_permissions(self, configuration, checkpoint):
        return self.incremental_sync(configuration, configuration.sync_cursor or None)

    def fetch_version(self, configuration, external_id):
        raise AssertionError("fetch_version is not used by the generic page runtime")

    def tombstone_missing(self, configuration, sync_run):
        raise AssertionError("tombstone_missing is not used by the generic page runtime")

    def health(self, configuration):
        return ConnectorHealth("active", "connected", None, None)


@override_settings(
    IS_PRODUCTION_ENV=False,
    ORG_MEMORY_RETRY_BASE_SECONDS=1,
    ORG_MEMORY_RETRY_MAX_SECONDS=4,
    ORG_MEMORY_WORKER_MAX_ATTEMPTS=2,
    ORG_MEMORY_WORKER_HEARTBEAT_SECONDS=1,
    ORG_MEMORY_WORKER_LEASE_SECONDS=5,
)
class MemoryRuntimeTests(TestCase):
    def setUp(self):
        self.enabled_patch = patch.dict(
            os.environ,
            {"ORG_MEMORY_ENABLED_PROVIDERS": "linear"},
        )
        self.enabled_patch.start()
        self.organization = Organization.objects.create(
            name="Runtime",
            domain="runtime.mlai.test",
        )
        self.user = get_user_model().objects.create_user(email="runtime@mlai.test")
        self.connection = ExternalServiceConnection.objects.create(
            provider="linear",
            user=self.user,
            organization=self.organization,
            external_account_id="linear-runtime",
            account_label="Runtime Linear",
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
            created_by=self.user,
        )
        self.scope = MemorySourceScope.objects.create(
            configuration=self.configuration,
            scope_type="project",
            external_id="project-runtime",
            name="Runtime project",
            selected=True,
            status=MemoryScopeStatus.SELECTED,
            default_classification="committee",
        )
        self.original_connector = connector_registry.get("linear")
        self.connector = FakeLinearRuntimeConnector()
        connector_registry.register(self.connector, replace=True)

    def tearDown(self):
        connector_registry.register(self.original_connector, replace=True)
        self.enabled_patch.stop()
        super().tearDown()

    def _record(self, *, version="v1", text="A durable runtime record.", external_id="LIN-1"):
        return {
            "source_scope_id": self.scope.pk,
            "external_account_id": "linear-runtime",
            "source_type": "issue",
            "external_id": external_id,
            "version_key": version,
            "content_hash": digest(text),
            "classification": "committee",
            "acl": {
                "is_accessible": True,
                "provider_revision": f"acl-{version}",
                "principal_refs": ["team:runtime"],
            },
            "chunks": [{"ordinal": 0, "text": text, "token_count": 5}],
            "title": external_id,
            "metadata": {"record_type": "linear_issue"},
        }

    def _action(self, *, max_attempts=2, action_type=MemoryActionType.SYNC):
        action = MemorySourceActionRequest.objects.create(
            configuration=self.configuration,
            action=action_type,
            idempotency_key=(
                f"manual-{action_type}-{MemorySourceActionRequest.objects.count()}"
            ),
        )
        result = dispatch_pending_actions(limit=10)
        self.assertEqual(result["dispatched"], 1)
        work = action.work_items.get()
        if work.max_attempts != max_attempts:
            work.max_attempts = max_attempts
            work.save(update_fields=("max_attempts", "updated_at"))
        return action, work

    def _captured_source(self):
        text = "Evidence for the queue."
        return capture_source_version(
            organization=self.organization,
            provider="linear",
            external_account_id="linear-runtime",
            source_type="issue",
            external_id="LIN-OUTBOX",
            version_key="v1",
            content_hash=digest(text),
            classification="committee",
            acl={"is_accessible": True, "principal_refs": ["team:runtime"]},
            chunks=[{"ordinal": 0, "text": text}],
            configuration=self.configuration,
            source_scope=self.scope,
        )

    def test_outbox_dispatch_is_idempotent_and_one_work_item_has_one_owner(self):
        source, version, _created = self._captured_source()

        self.assertEqual(dispatch_outbox_events(limit=10)["published"], 1)
        self.assertEqual(dispatch_outbox_events(limit=10)["published"], 0)
        self.assertEqual(MemoryWorkItem.objects.count(), 1)
        self.assertEqual(MemoryOutboxEvent.objects.get().status, MemoryOutboxStatus.PUBLISHED)

        first = claim_memory_work(worker_id="worker-one")
        second = claim_memory_work(worker_id="worker-two")

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(MemoryWorkerLease.objects.filter(released_at__isnull=True).count(), 1)
        self.assertEqual(execute_claimed_memory_work(first)["status"], "completed")
        version.refresh_from_db()
        self.assertTrue(version.chunks.get().active_for_retrieval)
        self.assertEqual(source.lifecycle_state, "active")

    def test_access_restored_outbox_work_is_version_pinned(self):
        source, version, _created = self._captured_source()
        MemoryOutboxEvent.objects.all().delete()
        MemoryOutboxEvent.objects.create(
            organization=self.organization,
            source=source,
            source_version=version,
            event_type=MemoryOutboxEventType.SOURCE_ACCESS_RESTORED,
            idempotency_key=f"restored:{source.pk}:{version.pk}",
        )

        self.assertEqual(dispatch_outbox_events(limit=10)["published"], 1)
        work = MemoryWorkItem.objects.get(task_type=MemoryWorkTaskType.RECONCILE)

        self.assertEqual(work.source_version, version)
        claim = claim_memory_work(worker_id="access-restored-worker")
        self.assertEqual(execute_claimed_memory_work(claim)["status"], "completed")

    def test_legacy_access_restored_dead_letters_get_version_pinned_replacements(self):
        source, version, _created = self._captured_source()
        MemoryOutboxEvent.objects.all().delete()
        OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.user,
        )
        payload = {
            "event_type": MemoryOutboxEventType.SOURCE_ACCESS_RESTORED,
            "outbox_event_id": "legacy-restored-event",
        }
        work = MemoryWorkItem.objects.create(
            organization=self.organization,
            provider="linear",
            task_type=MemoryWorkTaskType.RECONCILE,
            source=source,
            configuration=self.configuration,
            idempotency_key="legacy-versionless-access-restored",
            payload=payload,
            status=MemoryWorkStatus.DEAD,
            attempts=5,
            max_attempts=5,
            last_error=LEGACY_ACCESS_RESTORED_ERROR,
        )
        dead_letter = MemoryDeadLetter.objects.create(
            work_item=work,
            organization=self.organization,
            task_type=MemoryWorkTaskType.RECONCILE,
            payload_snapshot=payload,
            attempts=5,
            last_error=LEGACY_ACCESS_RESTORED_ERROR,
        )

        preview = reconcile_access_restored_dead_letters(
            organization=self.organization,
            provider="linear",
        )
        self.assertEqual(preview["candidates"], 1)
        self.assertEqual(MemoryWorkItem.objects.count(), 1)

        applied = reconcile_access_restored_dead_letters(
            organization=self.organization,
            provider="linear",
            apply=True,
            resolved_by=self.user,
        )
        dead_letter.refresh_from_db()
        replacement = MemoryWorkItem.objects.get(pk=dead_letter.requeued_work_item_id)

        self.assertEqual(applied["scheduled"], 1)
        self.assertEqual(applied["resolved"], 1)
        self.assertEqual(replacement.source_version, version)
        self.assertEqual(replacement.status, MemoryWorkStatus.PENDING)
        self.assertIsNotNone(dead_letter.resolved_at)
        self.assertEqual(
            reconcile_access_restored_dead_letters(
                organization=self.organization,
                provider="linear",
                apply=True,
                resolved_by=self.user,
            )["candidates"],
            0,
        )

    def test_expired_worker_lease_is_recovered_and_reclaimed(self):
        source, version, _created = self._captured_source()
        MemoryOutboxEvent.objects.all().delete()
        work, _created = create_work_item(
            organization=self.organization,
            provider="linear",
            task_type=MemoryWorkTaskType.RECONCILE,
            idempotency_key="recover-expired",
            source=source,
            source_version=version,
        )
        first = claim_memory_work(worker_id="worker-killed", lease_seconds=5)
        original_expiry = MemoryWorkerLease.objects.get(
            lease_token=first.lease_token
        ).expires_at
        extended_expiry = heartbeat_memory_work(first, lease_seconds=10)
        self.assertGreater(extended_expiry, original_expiry)
        now = timezone.now()
        MemoryWorkerLease.objects.filter(lease_token=first.lease_token).update(
            acquired_at=now - timedelta(seconds=10),
            heartbeat_at=now - timedelta(seconds=10),
            expires_at=now - timedelta(seconds=1),
        )

        recovery = recover_expired_leases()
        work.refresh_from_db()

        self.assertEqual(recovery["recovered"], 1)
        self.assertEqual(work.status, MemoryWorkStatus.PENDING)
        second = claim_memory_work(worker_id="worker-replacement", lease_seconds=5)
        self.assertIsNotNone(second)
        self.assertNotEqual(first.lease_token, second.lease_token)

    def test_failed_page_rolls_back_evidence_and_does_not_advance_cursor(self):
        self.configuration.sync_cursor = "cursor-before"
        self.configuration.save(update_fields=("sync_cursor", "updated_at"))
        invalid = self._record(external_id="LIN-BAD")
        invalid["chunks"] = [{"ordinal": 0, "text": ""}]
        self.connector.pages = [
            SyncPage(
                records=(self._record(external_id="LIN-GOOD"), invalid),
                next_cursor="cursor-must-not-commit",
            )
        ]
        action, work = self._action()
        claim = claim_memory_work(worker_id="worker-page")

        result = execute_claimed_memory_work(claim)
        self.configuration.refresh_from_db()
        action.refresh_from_db()
        work.refresh_from_db()

        self.assertEqual(result["status"], "dead")
        self.assertEqual(self.configuration.sync_cursor, "cursor-before")
        self.assertFalse(MemorySource.objects.filter(external_id="LIN-GOOD").exists())
        self.assertEqual(action.status, MemoryActionStatus.FAILED)
        self.assertEqual(work.status, MemoryWorkStatus.DEAD)

    def test_multi_page_sync_commits_each_page_and_completes_run(self):
        self.connector.pages = [
            SyncPage(
                records=(self._record(version="v1"),),
                next_cursor="cursor-1",
                checkpoint={"page": 1},
                has_more=True,
            ),
            SyncPage(
                records=(self._record(version="v2", text="Updated durable record."),),
                next_cursor="cursor-2",
                checkpoint={"page": 2},
            ),
        ]
        action, first_work = self._action()

        first_claim = claim_memory_work(worker_id="worker-pages")
        first_result = execute_claimed_memory_work(first_claim)
        self.configuration.refresh_from_db()
        action.refresh_from_db()

        self.assertEqual(first_result["status"], "continued")
        self.assertEqual(self.configuration.sync_cursor, "cursor-1")
        self.assertEqual(action.status, MemoryActionStatus.RUNNING)
        second_claim = claim_memory_work(worker_id="worker-pages")
        second_result = execute_claimed_memory_work(second_claim)
        self.configuration.refresh_from_db()
        action.refresh_from_db()
        run = MemorySyncRun.objects.get(action_request=action)

        self.assertEqual(second_result["status"], "completed")
        self.assertEqual(self.connector.cursors, [None, "cursor-1"])
        self.assertEqual(self.configuration.sync_cursor, "cursor-2")
        self.assertEqual(self.configuration.sync_checkpoint, {"page": 2})
        self.assertIsNotNone(self.configuration.last_successful_sync_at)
        self.assertEqual(action.status, MemoryActionStatus.COMPLETED)
        self.assertEqual(run.status, MemorySyncRunStatus.COMPLETED)
        self.assertEqual(run.pages_completed, 2)
        self.assertEqual(run.records_processed, 2)
        source = MemorySource.objects.get(external_id="LIN-1")
        self.assertEqual(source.versions.count(), 2)
        self.assertEqual(source.current_version.version_key, "v2")
        first_work.refresh_from_db()
        self.assertEqual(first_work.status, MemoryWorkStatus.COMPLETED)

    def test_reprocess_schedules_current_source_for_new_extraction_target(self):
        source, _version, _created = self._captured_source()
        MemoryOutboxEvent.objects.all().delete()
        unchanged_record = self._record(
            external_id=source.external_id,
            text="Evidence for the queue.",
        )
        unchanged_record["acl"] = {
            "is_accessible": True,
            "principal_refs": ["team:runtime"],
        }
        self.connector.pages = [
            SyncPage(
                records=(unchanged_record,),
                next_cursor="reprocessed",
            )
        ]
        self._action(action_type=MemoryActionType.REPROCESS)

        claimed = claim_memory_work(worker_id="worker-reprocess-extraction")
        result = execute_claimed_memory_work(claimed)

        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["reextraction"]["scheduled"], 1)
        extraction_work = MemoryWorkItem.objects.get(task_type=MemoryWorkTaskType.EXTRACT)
        self.assertEqual(extraction_work.source, source)

    def test_transient_failures_back_off_then_dead_letter_without_cursor_change(self):
        self.configuration.sync_cursor = "stable-cursor"
        self.configuration.save(update_fields=("sync_cursor", "updated_at"))
        self.connector.error = TimeoutError("provider timeout")
        action, work = self._action(max_attempts=2)

        first = claim_memory_work(worker_id="worker-retry")
        first_result = execute_claimed_memory_work(first)
        work.refresh_from_db()
        self.assertEqual(first_result["status"], "retry")
        self.assertEqual(work.status, MemoryWorkStatus.PENDING)
        self.assertGreater(work.available_at, timezone.now())

        MemoryWorkItem.objects.filter(pk=work.pk).update(available_at=timezone.now())
        second = claim_memory_work(worker_id="worker-retry")
        second_result = execute_claimed_memory_work(second)
        work.refresh_from_db()
        action.refresh_from_db()
        self.configuration.refresh_from_db()

        self.assertEqual(second_result["status"], "dead")
        self.assertEqual(work.status, MemoryWorkStatus.DEAD)
        self.assertEqual(action.status, MemoryActionStatus.FAILED)
        self.assertEqual(self.configuration.sync_cursor, "stable-cursor")
        self.assertTrue(MemoryDeadLetter.objects.filter(work_item=work).exists())

    def test_scheduler_is_due_and_idempotent_with_one_active_run_per_connection(self):
        first = schedule_due_connections(limit=10)
        second = schedule_due_connections(limit=10)
        dispatched = dispatch_pending_actions(limit=10)
        self.configuration.refresh_from_db()

        self.assertEqual(first["scheduled"], 1)
        self.assertEqual(second["scheduled"], 0)
        self.assertEqual(dispatched["dispatched"], 1)
        self.assertIsNotNone(self.configuration.next_scheduled_sync_at)
        self.assertEqual(self.configuration.action_requests.count(), 1)
        self.assertEqual(self.configuration.sync_runs.count(), 1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                duplicate_action = MemorySourceActionRequest.objects.create(
                    configuration=self.configuration,
                    action=MemoryActionType.SYNC,
                )
                MemorySyncRun.objects.create(
                    organization=self.organization,
                    configuration=self.configuration,
                    action_request=duplicate_action,
                    provider="linear",
                    action_type=MemoryActionType.SYNC,
                )

    def test_provider_throttle_and_organization_limit_block_additional_claims(self):
        source, version, _created = self._captured_source()
        MemoryOutboxEvent.objects.all().delete()
        for ordinal in range(2):
            create_work_item(
                organization=self.organization,
                provider="linear",
                task_type=MemoryWorkTaskType.RECONCILE,
                idempotency_key=f"concurrency-{ordinal}",
                source=source,
                source_version=version,
            )
        first = claim_memory_work(
            worker_id="worker-concurrency-one",
            organization_concurrency=1,
            provider_concurrency=4,
        )
        blocked = claim_memory_work(
            worker_id="worker-concurrency-two",
            organization_concurrency=1,
            provider_concurrency=4,
        )
        self.assertIsNotNone(first)
        self.assertIsNone(blocked)
        execute_claimed_memory_work(first)
        MemoryRuntimeLane.objects.filter(key="provider:linear").update(
            blocked_until=timezone.now() + timedelta(minutes=1),
            block_reason="provider_retry_after",
        )
        throttled = claim_memory_work(
            worker_id="worker-concurrency-two",
            organization_concurrency=1,
            provider_concurrency=4,
        )
        self.assertIsNone(throttled)
        MemoryRuntimeLane.objects.filter(key="provider:linear").update(
            blocked_until=timezone.now() - timedelta(seconds=1),
        )
        second = claim_memory_work(
            worker_id="worker-concurrency-two",
            organization_concurrency=1,
            provider_concurrency=4,
        )
        self.assertIsNotNone(second)
        self.assertEqual(MemoryRuntimeLane.objects.count(), 2)

    def test_pause_releases_inflight_lease_and_work_resumes_only_after_connection(self):
        self.connector.pages = [SyncPage(records=(), next_cursor="after-resume")]
        action, work = self._action()
        first = claim_memory_work(worker_id="worker-before-pause")

        suspended = suspend_configuration_runtime(self.configuration)
        self.configuration.lifecycle_state = MemoryConnectionState.PAUSED
        self.configuration.save(update_fields=("lifecycle_state", "updated_at"))
        lost = execute_claimed_memory_work(first)
        work.refresh_from_db()

        self.assertEqual(suspended["work_suspended"], 1)
        self.assertEqual(lost["status"], "lost_lease")
        self.assertEqual(work.status, MemoryWorkStatus.PENDING)
        self.assertIsNone(claim_memory_work(worker_id="worker-while-paused"))

        self.configuration.lifecycle_state = MemoryConnectionState.ACTIVE
        self.configuration.save(update_fields=("lifecycle_state", "updated_at"))
        resumed = claim_memory_work(worker_id="worker-after-resume")
        self.assertIsNotNone(resumed)
        self.assertEqual(execute_claimed_memory_work(resumed)["status"], "completed")
        action.refresh_from_db()
        self.assertEqual(action.status, MemoryActionStatus.COMPLETED)

    def test_source_dead_letter_can_be_requeued_once(self):
        source, version, _created = self._captured_source()
        MemoryOutboxEvent.objects.all().delete()
        work, _created = create_work_item(
            organization=self.organization,
            provider="linear",
            task_type=MemoryWorkTaskType.EMBED,
            idempotency_key="unsupported-embed",
            source=source,
            source_version=version,
        )
        claim = claim_memory_work(worker_id="worker-dead-letter")
        self.assertEqual(execute_claimed_memory_work(claim)["status"], "dead")
        dead_letter = MemoryDeadLetter.objects.get(work_item=work)

        requeued = requeue_dead_letter(dead_letter.pk, resolved_by=self.user)
        dead_letter.refresh_from_db()

        self.assertEqual(requeued.status, MemoryWorkStatus.PENDING)
        self.assertEqual(requeued.source_id, source.pk)
        self.assertEqual(dead_letter.requeued_work_item_id, requeued.pk)
        self.assertIsNotNone(dead_letter.resolved_at)
        self.assertEqual(memory_queue_snapshot()["dead"], 1)

    def test_embedding_work_uses_ids_only_and_persists_a_versioned_vector(self):
        source, version, _created = self._captured_source()
        MemoryOutboxEvent.objects.all().delete()
        chunk = version.chunks.get()
        work, _created = create_work_item(
            organization=self.organization,
            provider="linear",
            task_type=MemoryWorkTaskType.EMBED,
            idempotency_key=f"embed-runtime:{chunk.pk}:v1",
            source=source,
            source_version=version,
            payload={
                "chunk_id": str(chunk.pk),
                "model": "text-embedding-3-small",
                "version": "openai-text-embedding-3-small-v1",
                "dimensions": 1536,
            },
        )

        with patch(
            "org_memory.embeddings.OpenAIEmbeddingProvider.embed",
            return_value=[1.0] + [0.0] * 1535,
        ):
            claim = claim_memory_work(worker_id="embedding-worker")
            result = execute_claimed_memory_work(claim)

        self.assertEqual(result["status"], "completed")
        embedding = MemoryChunkEmbedding.objects.get(chunk=chunk)
        self.assertTrue(embedding.is_current)
        self.assertEqual(embedding.version, "openai-text-embedding-3-small-v1")
        self.assertNotIn(chunk.text, str(work.payload))
