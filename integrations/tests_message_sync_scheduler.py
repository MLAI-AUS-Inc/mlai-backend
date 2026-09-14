"""Synthetic PostgreSQL transaction, fairness and crash-recovery regressions."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.db import IntegrityError, connection, connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from integrations.models import BridgeApiBudget, BridgeSyncInbox, BridgeSyncJob, BridgeSyncState, CommunityBridgeChannel
from integrations.services.message_sync.inbox import claim_inbox, enqueue_slack_callback, process_inbox_once
from integrations.services.message_sync.scheduler import (
    BudgetDeferred, LeaseLost, claim_job, finish_job, schedule_job,
)


from integrations.services.message_sync.budgets import admit_request, record_cooldown


class SyncSchedulerTests(TransactionTestCase):
    def state(self, channel, workspace="T1"):
        public = CommunityBridgeChannel.objects.create(
            slack_workspace_id=workspace, slack_channel_id=channel,
            destination_platform="buzz", destination_workspace_id="test.invalid",
            destination_channel_id=f"fixture-{channel}",
        )
        return BridgeSyncState.objects.create(public_channel=public, workspace_id=workspace, source_channel_id=channel)

    def concurrent(self, operation):
        barrier = Barrier(2)
        def run():
            connections.close_all()
            try:
                barrier.wait(timeout=5)
                return operation()
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(lambda _: run(), range(2)))

    def test_database_requires_exactly_one_owner(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            BridgeSyncState.objects.create(workspace_id="T1", source_channel_id="C1")

    def test_busy_conversation_and_workspace_cannot_starve_other_users(self):
        a = self.state("A", "busy")
        b = self.state("B", "busy")
        c = self.state("C", "quiet")
        for state in (a, b, c):
            schedule_job(state, "head")
        sequence = []
        for _ in range(6):
            lease = claim_job()
            sequence.append(lease.state_id)
            finish_job(lease, checkpoint={})
        self.assertEqual(sequence, [a.pk, c.pk, b.pk, c.pk, a.pk, c.pk])

    def test_live_deliveries_cannot_starve_history_and_many_threads_cannot_starve_head(self):
        state = self.state("busy")
        schedule_job(state, "head")
        schedule_job(state, "archive")
        for i in range(20):
            schedule_job(state, "thread", source_object_key=f"1700000000.{i:06d}")
        kinds = []
        for _ in range(9):
            # A separate live-delivery lane continually uses this conversation.
            BridgeSyncState.objects.filter(pk=state.pk).update(last_served_at=timezone.now())
            lease = claim_job()
            kinds.append(lease.kind)
            finish_job(lease, checkpoint={})
        self.assertEqual(kinds, ["head", "archive", "thread"] * 3)

    def test_two_workers_cannot_claim_different_jobs_in_one_conversation(self):
        state = self.state("A")
        schedule_job(state, "head")
        schedule_job(state, "thread", source_object_key="1700000000.000001")
        claims = self.concurrent(claim_job)
        self.assertEqual(sum(item is not None for item in claims), 1)

    def test_expired_worker_cannot_overwrite_replacement_checkpoint(self):
        schedule_job(self.state("A"), "head")
        old = claim_job()
        BridgeSyncJob.objects.filter(pk=old.job_id).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        replacement = claim_job()
        finish_job(replacement, checkpoint={"cursor": "next"}, delay_seconds=60)
        with self.assertRaises(LeaseLost):
            finish_job(old, checkpoint={"cursor": "stale"})
        self.assertEqual(BridgeSyncJob.objects.get(pk=old.job_id).checkpoint, {"cursor": "next"})

    def test_revoked_authority_rejects_a_previously_claimed_page(self):
        state = self.state("A")
        schedule_job(state, "head")
        lease = claim_job()
        CommunityBridgeChannel.objects.filter(pk=state.public_channel_id).update(enabled=False)
        with self.assertRaises(LeaseLost):
            finish_job(lease, checkpoint={})

    def test_thread_job_keeps_checkpoint_and_recurs_after_completion(self):
        state = self.state("A")
        job = schedule_job(state, "thread", source_object_key="1700000000.000001")
        lease = claim_job()
        finish_job(lease, checkpoint={"oldest": "1700000000.000001"}, delay_seconds=3600, complete=True)
        same = schedule_job(state, "thread", source_object_key="1700000000.000001")
        self.assertEqual(job.pk, same.pk)
        self.assertEqual(same.checkpoint, {"oldest": "1700000000.000001"})
        self.assertIsNotNone(same.completed_at)
        self.assertIsNone(claim_job())

    def test_checkpoints_reject_message_bodies(self):
        schedule_job(self.state("A"), "head")
        with self.assertRaises(ValueError):
            finish_job(claim_job(), checkpoint={"text": "private body"})

    def test_provider_budget_and_cooldown_survive_an_outer_message_rollback(self):
        kwargs = dict(app_id="A1", workspace_id="T1", method="conversations.history")
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                admit_request(**kwargs, interval_seconds=60)
                record_cooldown(**kwargs, retry_after=180)
                raise RuntimeError("message transaction rolled back")
        with self.assertRaises(BudgetDeferred) as result:
            admit_request(**kwargs, interval_seconds=60)
        self.assertGreaterEqual(result.exception.retry_after, 179)

    def test_admission_is_shared_across_workers_and_method_cooldown_is_durable(self):
        kwargs = dict(app_id="A1", workspace_id="T1", method="conversations.history", interval_seconds=60)
        def attempt():
            try:
                admit_request(**kwargs)
                return True
            except BudgetDeferred:
                return False
        self.assertEqual(sorted(self.concurrent(attempt)), [False, True])
        record_cooldown(app_id="A1", workspace_id="T1", method="conversations.history", retry_after=180)
        with self.assertRaises(BudgetDeferred) as result:
            admit_request(**kwargs)
        self.assertGreaterEqual(result.exception.retry_after, 179)
        admit_request(**{**kwargs, "workspace_id": "T2"})
        admit_request(**{**kwargs, "method": "conversations.replies"})
        self.assertEqual(BridgeApiBudget.objects.count(), 3)


class SyncInboxTests(TransactionTestCase):
    def payload(self):
        return {"api_app_id": "A1", "team_id": "T1", "event_id": "E1", "type": "event_callback",
                "event": {"type": "message", "channel": "D1", "text": "private synthetic body"}}

    def test_encrypted_receipt_deduplicates_and_clears_body_only_after_success(self):
        first = enqueue_slack_callback(self.payload())
        duplicate = enqueue_slack_callback(self.payload())
        self.assertEqual(first["receipt_id"], duplicate["receipt_id"])
        with connection.cursor() as cursor:
            cursor.execute("SELECT encrypted_payload FROM bridge_sync_inbox")
            stored = cursor.fetchone()[0]
        self.assertNotIn("private synthetic body", stored)
        self.assertTrue(stored.startswith("mlai-enc:v1:"))
        with patch('integrations.services.slack_dm_mirror.ingest_slack_dm_event', return_value={"status": "enqueued"}):
            self.assertEqual(process_inbox_once(), 1)
        row = BridgeSyncInbox.objects.get()
        self.assertEqual(row.status, "completed")
        self.assertEqual(row.encrypted_payload, "")
        self.assertEqual(enqueue_slack_callback(self.payload())["status"], "duplicate")

    def test_crash_rolls_back_downstream_writes_and_retries_original_ciphertext(self):
        enqueue_slack_callback(self.payload())
        def fail(payload):
            CommunityBridgeChannel.objects.create(slack_channel_id="crash", destination_platform="buzz")
            raise RuntimeError("private synthetic body must never enter error metadata")
        with patch('integrations.services.slack_dm_mirror.ingest_slack_dm_event', side_effect=fail):
            self.assertEqual(process_inbox_once(), 0)
        self.assertEqual(CommunityBridgeChannel.objects.count(), 0)
        row = BridgeSyncInbox.objects.get()
        self.assertEqual(json.loads(row.encrypted_payload), self.payload())
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.last_error_code, "RuntimeError")

    def test_abandoned_claim_can_be_recovered_without_a_second_receipt(self):
        enqueue_slack_callback(self.payload())
        old = claim_inbox()
        self.assertIsNone(claim_inbox())
        BridgeSyncInbox.objects.update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        replacement = claim_inbox()
        self.assertEqual(old[0], replacement[0])
        self.assertNotEqual(old[1], replacement[1])


class PublicDeliveryLeaseTests(TransactionTestCase):
    state = SyncSchedulerTests.state

    def enqueue(self, channel, key):
        from integrations.services.community_bridge.store import ingest_inbound_event
        return ingest_inbound_event(source_platform="slack", receipt_key=key,
            source_channel_id=channel, event_type="message", raw_payload={}, normalized_event={
                "delivery_type": "create", "source_channel_id": channel, "source_message_id": key,
                "source_author_id": "U1", "source_author_display_name": "Fixture", "text": "fixture", "attachments": [],
            })["delivery_id"]

    def test_busy_channel_claims_only_one_slot_and_other_channel_gets_a_turn(self):
        from integrations.services.message_sync.delivery import claim_public
        a = self.state("A")
        b = self.state("B")
        self.enqueue("A", "1700000000.000001")
        self.enqueue("A", "1700000000.000002")
        self.enqueue("B", "1700000000.000003")
        claims = claim_public(10)
        self.assertEqual([item["channel_id"] for item in claims], [a.public_channel_id, b.public_channel_id])

    def test_old_worker_cannot_complete_or_fail_a_reclaimed_delivery(self):
        from integrations.models import CommunityBridgeDelivery
        from integrations.services.community_bridge.store import complete_delivery, mark_delivery_retry
        from integrations.services.message_sync.delivery import claim_public, delivery_context, recover_public_leases
        self.state("A")
        row_id = self.enqueue("A", "1700000000.000001")
        old = claim_public(1)[0]
        CommunityBridgeDelivery.objects.filter(pk=row_id).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(recover_public_leases(), 1)
        new = claim_public(1)[0]
        with delivery_context(old):
            with self.assertRaises(LeaseLost):
                complete_delivery(delivery_id=row_id)
            with self.assertRaises(LeaseLost):
                mark_delivery_retry(delivery_id=row_id, error_text="old failure")
        self.assertEqual(CommunityBridgeDelivery.objects.get(pk=row_id).status, "processing")
        with delivery_context(new):
            complete_delivery(delivery_id=row_id)
        self.assertEqual(CommunityBridgeDelivery.objects.get(pk=row_id).status, "completed")

    def test_shared_budget_wait_does_not_consume_delivery_attempts(self):
        from integrations.models import CommunityBridgeDelivery
        from integrations.services.community_bridge.store import defer_delivery
        from integrations.services.message_sync.delivery import claim_public, delivery_context
        state = self.state("A")
        row_id = self.enqueue("A", "1700000000.000001")
        claim = claim_public(1)[0]
        with delivery_context(claim):
            defer_delivery(delivery_id=row_id, retry_after=60)
        row = CommunityBridgeDelivery.objects.get(pk=row_id)
        self.assertEqual(row.attempts, 0)
        self.assertEqual(row.status, "pending")
        self.assertIsNone(row.lease_token)
        self.assertIsNone(row.lease_expires_at)
        state.refresh_from_db()
        self.assertIsNone(state.last_served_at)

    def test_old_failure_after_replacement_completed_cannot_reopen_the_row(self):
        from integrations.models import CommunityBridgeDelivery
        from integrations.services.community_bridge.store import complete_delivery, mark_delivery_retry
        from integrations.services.message_sync.delivery import claim_public, delivery_context, recover_public_leases
        self.state("A")
        row_id = self.enqueue("A", "1700000000.000001")
        old = claim_public(1)[0]
        CommunityBridgeDelivery.objects.filter(pk=row_id).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        recover_public_leases()
        new = claim_public(1)[0]
        with delivery_context(new):
            complete_delivery(delivery_id=row_id)
        with delivery_context(old), self.assertRaises(LeaseLost):
            mark_delivery_retry(delivery_id=row_id, error_text="late failure")
        self.assertEqual(CommunityBridgeDelivery.objects.get(pk=row_id).status, "completed")

    def test_delayed_older_edit_is_superseded_by_a_completed_newer_revision(self):
        from integrations.models import CommunityBridgeDelivery
        from integrations.services.message_sync.delivery import supersede_stale_mutation
        state = self.state("A")
        fields = dict(channel_id=state.public_channel_id, source_platform="slack", target_platform="buzz",
                      source_channel_id="A", source_message_id="1700000000.000001", delivery_type="edit", available_at=timezone.now())
        old = CommunityBridgeDelivery.objects.create(**fields, source_revision="1700000001.000001")
        CommunityBridgeDelivery.objects.create(**fields, source_revision="1700000001.000002", status="completed")
        self.assertTrue(supersede_stale_mutation(old.pk))
        old.refresh_from_db()
        self.assertEqual(old.status, "completed")
