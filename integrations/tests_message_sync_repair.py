"""Repair scheduling is bounded and cannot mutate retained delivery authority."""
from datetime import timedelta
from io import StringIO
import json
import uuid
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from integrations.models import BridgeSyncJob, BridgeSyncState, CommunityBridgeDelivery, CommunityBridgeReceipt
from integrations.tests_message_sync_member_history_phase4b import Fixture, SETTINGS


@override_settings(**SETTINGS)
class DurableRepairTests(Fixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.channel.audience = "community"
        self.channel.save()
        self.root = f"{self.now-60}.000001"

    def repair(self, **kwargs):
        output = StringIO()
        call_command("message_sync_repair", stdout=output,
            **{"workspace_id": "TTEST", "channel_id": "CROOM", **kwargs})
        return json.loads(output.getvalue())

    def test_plan_has_no_writes_or_provider_calls_even_without_existing_state(self):
        self.state.delete()
        report = self.repair(thread_ts=[self.root])
        self.assertEqual(report["mode"], "dry_run")
        self.assertEqual(report["history_days"], 30)
        self.assertEqual([j["kind"] for j in report["jobs"]], ["head", "archive", "thread"])
        self.assertFalse(any(j["exists"] for j in report["jobs"]))
        self.assertEqual(BridgeSyncState.objects.count(), 0)
        self.assertEqual(BridgeSyncJob.objects.count(), 0)
        self.assertEqual(self.calls, [])
        self.bot.assert_not_called()

    def test_apply_is_idempotent_and_schedules_without_direct_reads_or_replay(self):
        first = self.repair(apply=True, thread_ts=[self.root, self.root])
        self.assertEqual(first["jobs"][-1]["action"], "created")
        before = list(BridgeSyncJob.objects.order_by("pk").values())
        second = self.repair(apply=True, thread_ts=[self.root])
        self.assertTrue(all(j["action"] == "preserved" for j in second["jobs"]))
        self.assertEqual(list(BridgeSyncJob.objects.order_by("pk").values()), before)
        self.assertEqual(self.calls, [])
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 0)
        self.assertEqual(CommunityBridgeReceipt.objects.count(), 0)

    def test_active_lease_checkpoint_and_cooldown_remain_byte_for_byte(self):
        self.repair(apply=True, thread_ts=[self.root])
        now = timezone.now()
        BridgeSyncJob.objects.update(checkpoint={"cursor": "retained-cursor"},
            due_at=now+timedelta(hours=1), backoff_seconds=3600, last_error_code="ratelimited",
            lease_token=uuid.uuid4(), lease_expires_at=now+timedelta(minutes=2))
        before = list(BridgeSyncJob.objects.order_by("pk").values())
        self.repair(apply=True, thread_ts=[self.root])
        self.assertEqual(list(BridgeSyncJob.objects.order_by("pk").values()), before)

    def test_only_completed_idle_thread_gets_next_pass_without_erasing_receipt(self):
        self.repair(apply=True, thread_ts=[self.root])
        now = timezone.now()
        BridgeSyncJob.objects.update(completed_at=now, due_at=now+timedelta(hours=1))
        archived = BridgeSyncJob.objects.get(state=self.state, kind="archive").due_at
        report = self.repair(apply=True, thread_ts=[self.root])
        job = BridgeSyncJob.objects.get(state=self.state, kind="thread")
        self.assertLessEqual(job.due_at, timezone.now())
        self.assertEqual(job.completed_at, now)
        self.assertEqual(BridgeSyncJob.objects.get(state=self.state, kind="archive").due_at, archived)
        self.assertEqual(report["jobs"][-1]["action"], "scheduled_next_pass")

    def test_pauses_restrictions_and_identity_changes_cannot_be_overridden(self):
        before = list(BridgeSyncJob.objects.order_by("pk").values())
        with override_settings(MESSAGE_SYNC_ENABLED=False), self.assertRaisesMessage(CommandError, "processing pause"):
            self.repair(apply=True)
        for field, value in (("enabled", False), ("hold_reason", "admin"), ("slack_archived", True),
                             ("slack_visibility", "private"), ("mirror_decision", "exclude")):
            old = getattr(self.channel, field)
            setattr(self.channel, field, value)
            self.channel.save()
            with self.subTest(field=field), self.assertRaises(CommandError):
                self.repair(apply=True)
            setattr(self.channel, field, old)
            self.channel.save()
        self.state.status = "paused"
        self.state.save()
        with self.assertRaisesMessage(CommandError, "paused or revoked"):
            self.repair(apply=True)
        self.assertEqual(list(BridgeSyncJob.objects.order_by("pk").values()), before)

    def test_invalid_or_cross_workspace_selection_is_rejected(self):
        for options in ({"workspace_id": "TOTHER"}, {"channel_id": "GPRIVATE"},
                        {"thread_ts": ["1.2"]}, {"thread_ts": [self.root]*21}):
            with self.subTest(options=options), self.assertRaises(CommandError):
                self.repair(apply=True, **options)
        self.assertEqual(self.calls, [])

    def test_scheduled_thread_runs_only_through_normal_member_page(self):
        self.repair(apply=True, thread_ts=[self.root])
        self.run_page("thread", source=self.root)
        self.assertIn("conversations_replies", [method for _, method, _ in self.calls])
        self.bot.assert_not_called()

    def test_reader_loss_keeps_scheduled_work_and_never_uses_bot(self):
        self.repair(apply=True, thread_ts=[self.root])
        type(self.a).objects.update(status="paused")
        self.run_page("thread", source=self.root)
        self.assertEqual(self.calls, [])
        self.assertTrue(BridgeSyncJob.objects.filter(state=self.state, kind="thread", source_object_key=self.root).exists())
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 0)
        self.bot.assert_not_called()

    def test_retired_commands_cannot_directly_replay_history(self):
        names = ["backfill_community_bridge_thread_references", "repair_community_bridge_slack_thread",
                 "reconcile_community_bridge_slack_threads", "repair_community_bridge_threads",
                 "backfill_community_bridge_slack_avatars", "backfill_community_bridge_slack_mentions"]
        for name in names:
            with self.subTest(command=name), self.assertRaisesMessage(CommandError, "Unknown command"):
                call_command(name, apply=True, stdout=StringIO())
        self.assertEqual(self.calls, [])
        self.bot.assert_not_called()
