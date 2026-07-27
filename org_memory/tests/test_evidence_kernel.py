import hashlib
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from integrations.models import GoogleConnection
from organizations.models import Organization
from org_memory.kernel import (
    EvidenceKernelError,
    capture_source_version,
    create_work_item,
    kernel_health_snapshot,
    open_review_item,
    revoke_source_access,
    tombstone_source,
    validate_work_item_for_execution,
)
from org_memory.models import (
    MemoryChunk,
    MemoryConnectionConfiguration,
    MemoryDeadLetter,
    MemoryDeletionStatus,
    MemoryOutboxEvent,
    MemoryOutboxEventType,
    MemoryReviewType,
    MemorySource,
    MemorySourceLifecycle,
    MemoryWorkItem,
    MemoryWorkerLease,
    MemoryWorkStatus,
    MemoryWorkTaskType,
)
from startup_updates.data_deletion import disconnect_gmail_for_user
from startup_updates.models import UserStartupBinding


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class EvidenceKernelTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="MLAI",
            domain="kernel.mlai.test",
        )
        self.other_organization = Organization.objects.create(
            name="Other",
            domain="kernel.other.test",
        )
        self.user = get_user_model().objects.create_user(email="kernel@mlai.test")

    def _capture(
        self,
        *,
        version_key="v1",
        text="The committee approved the transcript pilot.",
        classification="committee",
        acl=None,
        metadata=None,
        restore_access=False,
        external_id="file-123",
        chunks=None,
    ):
        return capture_source_version(
            organization=self.organization,
            provider="google_drive",
            external_account_id="drive-account-1",
            source_type="meeting_transcript",
            external_id=external_id,
            version_key=version_key,
            content_hash=digest(text),
            classification=classification,
            acl=acl
            or {
                "is_accessible": True,
                "provider_revision": f"acl-{version_key}",
                "principal_refs": ["user:operator@mlai.test"],
                "group_refs": ["group:committee"],
                "link_sharing": {"mode": "restricted"},
            },
            chunks=chunks
            or [
                {
                    "ordinal": 0,
                    "text": text,
                    "token_count": 7,
                    "source_locator": {
                        "transcript_id": external_id,
                        "section": "Decisions",
                    },
                }
            ],
            canonical_url="https://drive.google.com/file/d/file-123",
            title="Committee meeting",
            bounded_excerpt=text[:200],
            metadata=metadata or {"mime_type": "text/plain"},
            restore_access=restore_access,
        )

    def test_edits_create_immutable_versions_and_retire_old_chunks(self):
        source, first, created = self._capture()

        self.assertTrue(created)
        self.assertEqual(source.current_version_id, first.pk)
        self.assertTrue(first.is_current)
        self.assertTrue(first.chunks.get().active_for_retrieval)
        self.assertEqual(MemoryOutboxEvent.objects.count(), 1)

        replay_source, replay_version, replay_created = self._capture()
        self.assertFalse(replay_created)
        self.assertEqual(replay_source.pk, source.pk)
        self.assertEqual(replay_version.pk, first.pk)
        self.assertEqual(MemoryOutboxEvent.objects.count(), 1)

        with self.assertRaises(EvidenceKernelError):
            self._capture(
                acl={
                    "is_accessible": True,
                    "provider_revision": "different-acl",
                    "principal_refs": ["user:someone-else"],
                }
            )

        source, second, second_created = self._capture(
            version_key="v2",
            text="The committee approved the transcript and Linear pilot.",
        )
        first.refresh_from_db()
        source.refresh_from_db()
        self.assertTrue(second_created)
        self.assertFalse(first.is_current)
        self.assertIsNotNone(first.retired_at)
        self.assertFalse(first.chunks.get().active_for_retrieval)
        self.assertTrue(second.chunks.get().active_for_retrieval)
        self.assertEqual(source.current_version_id, second.pk)
        self.assertEqual(MemoryOutboxEvent.objects.count(), 2)

        first.content_hash = digest("mutated evidence")
        with self.assertRaises(ValidationError):
            first.save()

    def test_access_revocation_deactivates_chunks_and_blocks_stale_work(self):
        source, version, _created = self._capture()
        work, _created = create_work_item(
            organization=self.organization,
            provider="google_drive",
            task_type=MemoryWorkTaskType.EXTRACT,
            idempotency_key="extract:file-123:v1",
            source=source,
            source_version=version,
            payload={"parser_version": "v1"},
        )

        result = revoke_source_access(source, reason="provider_permission_removed")
        source.refresh_from_db()
        version.acl_snapshot.refresh_from_db()
        self.assertEqual(result["chunks_deactivated"], 1)
        self.assertEqual(source.lifecycle_state, MemorySourceLifecycle.ACCESS_REVOKED)
        self.assertFalse(version.acl_snapshot.is_accessible)
        self.assertFalse(version.chunks.get().active_for_retrieval)
        self.assertTrue(
            MemoryOutboxEvent.objects.filter(
                event_type=MemoryOutboxEventType.SOURCE_ACCESS_REVOKED
            ).exists()
        )
        with self.assertRaises(EvidenceKernelError):
            validate_work_item_for_execution(work)
        self.assertEqual(kernel_health_snapshot()["status"], "ok")

        source, restored, _created = self._capture(
            version_key="v2",
            text="Access was restored with a newer provider revision.",
            restore_access=True,
        )
        source.refresh_from_db()
        self.assertEqual(source.lifecycle_state, MemorySourceLifecycle.ACTIVE)
        self.assertIsNone(source.access_revoked_at)
        self.assertTrue(restored.chunks.get().active_for_retrieval)

    def test_tombstone_is_idempotent_and_leaves_no_current_retrievable_chunk(self):
        source, version, _created = self._capture()
        work, _created = create_work_item(
            organization=self.organization,
            provider="google_drive",
            task_type=MemoryWorkTaskType.EXTRACT,
            idempotency_key="extract-before-delete",
            source=source,
            source_version=version,
        )

        deletion, result = tombstone_source(
            source,
            reason="provider_source_deleted",
            requested_by=self.user,
            request_id="delete-source-1",
        )
        replay, replay_result = tombstone_source(
            source,
            reason="provider_source_deleted",
            requested_by=self.user,
            request_id="delete-source-1",
        )
        source.refresh_from_db()
        version.refresh_from_db()
        work.refresh_from_db()
        self.assertEqual(deletion.pk, replay.pk)
        self.assertEqual(result, replay_result)
        self.assertEqual(deletion.status, MemoryDeletionStatus.COMPLETED)
        self.assertEqual(source.lifecycle_state, MemorySourceLifecycle.TOMBSTONED)
        self.assertIsNone(source.current_version_id)
        self.assertFalse(version.is_current)
        self.assertIsNotNone(version.tombstoned_at)
        self.assertFalse(MemoryChunk.objects.filter(active_for_retrieval=True).exists())
        self.assertEqual(work.status, MemoryWorkStatus.CANCELLED)
        with self.assertRaises(EvidenceKernelError):
            self._capture(version_key="v2", text="Silent resurrection is forbidden.")

    def test_no_agent_and_inaccessible_acl_never_activate_chunks(self):
        source, version, _created = self._capture(classification="no_agent")
        self.assertEqual(source.lifecycle_state, MemorySourceLifecycle.ACTIVE)
        self.assertFalse(version.chunks.get().active_for_retrieval)

        other_text = "Provider access was already removed."
        other_source, other_version, _created = capture_source_version(
            organization=self.organization,
            provider="slack",
            external_account_id="T123",
            source_type="thread",
            external_id="C123:1700000000.001",
            version_key="v1",
            content_hash=digest(other_text),
            classification="committee",
            acl={"is_accessible": False, "principal_refs": [], "group_refs": []},
            chunks=[{"ordinal": 0, "text": other_text}],
        )
        self.assertEqual(
            other_source.lifecycle_state,
            MemorySourceLifecycle.ACCESS_REVOKED,
        )
        self.assertFalse(other_version.chunks.get().active_for_retrieval)
        self.assertEqual(kernel_health_snapshot()["status"], "ok")

    def test_unsafe_metadata_and_mixed_visibility_fail_atomically(self):
        with self.assertRaises(EvidenceKernelError):
            self._capture(metadata={"access_token": "must-not-persist"})
        self.assertFalse(MemorySource.objects.exists())

        text = "Mixed visibility must fail."
        with self.assertRaises(EvidenceKernelError):
            capture_source_version(
                organization=self.organization,
                provider="google_drive",
                external_account_id="drive-account-1",
                source_type="document",
                external_id="mixed-file",
                version_key="v1",
                content_hash=digest(text),
                classification="committee",
                acl={"is_accessible": True, "principal_refs": ["user:one"]},
                chunks=[
                    {
                        "ordinal": 0,
                        "text": text,
                        "classification": "executive",
                    }
                ],
            )
        self.assertFalse(MemorySource.objects.filter(external_id="mixed-file").exists())

        with self.assertRaises(EvidenceKernelError):
            self._capture(
                external_id="bad-chunk-shape",
                chunks=[{"ordinal": "first", "text": "Invalid ordinal."}],
            )
        self.assertFalse(
            MemorySource.objects.filter(external_id="bad-chunk-shape").exists()
        )

        with self.assertRaises(EvidenceKernelError):
            self._capture(acl={"principal_refs": ["user:one"]})

    def test_review_work_lease_and_dead_letter_primitives_are_idempotent(self):
        source, version, _created = self._capture()
        chunk = version.chunks.get()
        review, created = open_review_item(
            organization=self.organization,
            target=chunk,
            review_type=MemoryReviewType.SENSITIVITY,
            reason="Check whether this excerpt contains people-sensitive information.",
            idempotency_key="sensitivity:file-123:v1:0",
        )
        replay, replay_created = open_review_item(
            organization=self.organization,
            target=chunk,
            review_type=MemoryReviewType.SENSITIVITY,
            reason="Check whether this excerpt contains people-sensitive information.",
            idempotency_key="sensitivity:file-123:v1:0",
        )
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(review.pk, replay.pk)

        work, created = create_work_item(
            organization=self.organization,
            provider="google_drive",
            task_type=MemoryWorkTaskType.EXTRACT,
            idempotency_key="extract:file-123:v1:kernel",
            source=source,
            source_version=version,
        )
        replay_work, replay_created = create_work_item(
            organization=self.organization,
            provider="google_drive",
            task_type=MemoryWorkTaskType.EXTRACT,
            idempotency_key="extract:file-123:v1:kernel",
            source=source,
            source_version=version,
        )
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(work.pk, replay_work.pk)
        with self.assertRaises(EvidenceKernelError):
            create_work_item(
                organization=self.other_organization,
                provider="google_drive",
                task_type=MemoryWorkTaskType.EXTRACT,
                idempotency_key="extract:file-123:v1:kernel",
            )

        expires_at = timezone.now() + timedelta(minutes=5)
        MemoryWorkerLease.objects.create(
            work_item=work,
            worker_id="worker-one",
            expires_at=expires_at,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MemoryWorkerLease.objects.create(
                    work_item=work,
                    worker_id="worker-two",
                    expires_at=expires_at,
                )

        work.status = MemoryWorkStatus.DEAD
        work.attempts = work.max_attempts
        work.save(update_fields=("status", "attempts", "updated_at"))
        MemoryDeadLetter.objects.create(
            work_item=work,
            organization=self.organization,
            task_type=work.task_type,
            payload_snapshot={},
            attempts=work.attempts,
            last_error="unsupported file",
        )
        health = kernel_health_snapshot(organization=self.organization)
        self.assertEqual(health["status"], "degraded")
        self.assertEqual(health["counts"]["open_dead_letters"], 1)

    def test_health_detects_illegal_active_historical_chunk(self):
        _source, first, _created = self._capture()
        self._capture(version_key="v2", text="A newer version exists.")
        MemoryChunk.objects.filter(source_version=first).update(active_for_retrieval=True)

        health = kernel_health_snapshot(organization=self.organization)

        self.assertEqual(health["status"], "error")
        self.assertEqual(
            health["invariant_violations"]["active_chunk_noncurrent_version"],
            1,
        )

    def test_gmail_disconnect_tombstones_memory_before_removing_protected_config(self):
        google_connection = GoogleConnection.objects.create(
            user=self.user,
            google_email="kernel@mlai.test",
            refresh_token="",
            scope="gmail.readonly",
        )
        UserStartupBinding.objects.create(
            user=self.user,
            organization=self.organization,
            google_connection=google_connection,
        )
        configuration = MemoryConnectionConfiguration.objects.create(
            organization=self.organization,
            provider="gmail",
            google_connection=google_connection,
            created_by=self.user,
        )
        text = "A labelled executive email supplied approved context."
        source, version, _created = capture_source_version(
            organization=self.organization,
            provider="gmail",
            external_account_id="kernel@mlai.test",
            source_type="email_thread",
            external_id="thread-1",
            version_key="history-1",
            content_hash=digest(text),
            classification="executive",
            acl={"is_accessible": True, "principal_refs": ["user:kernel@mlai.test"]},
            chunks=[{"ordinal": 0, "text": text}],
            configuration=configuration,
        )

        result = disconnect_gmail_for_user(self.user, delete_derived_data=True)

        source.refresh_from_db()
        version.refresh_from_db()
        self.assertEqual(result["status"], "disconnected")
        self.assertFalse(GoogleConnection.objects.filter(pk=google_connection.pk).exists())
        self.assertFalse(
            MemoryConnectionConfiguration.objects.filter(pk=configuration.pk).exists()
        )
        self.assertEqual(source.lifecycle_state, MemorySourceLifecycle.TOMBSTONED)
        self.assertFalse(version.is_current)
        self.assertFalse(version.chunks.filter(active_for_retrieval=True).exists())
        self.assertEqual(result["deleted"]["orgMemorySourcesTombstoned"], 1)
