"""Fresh-source recovery must not replay erased or unauthorized private bodies."""
from datetime import timedelta
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from django.utils import timezone

from community_chat.tests.test_slack_dm_io_authority import SlackDmIoAuthorityFixture, SCOPES
from integrations.models import ExternalServiceConnection, SlackDmMirrorConversation, SlackDmMirrorDelivery, SlackDmMirrorGrant
from integrations.services import slack_dm_mirror as dm
from integrations.services.message_sync.history import ensure_state, seed_states
from integrations.services.message_sync.private_history import private_page
from integrations.services.message_sync.recovery import schedule_private_recoveries
from integrations.services.message_sync.scheduler import claim_job


class PrivateSourceRecoveryTests(SlackDmIoAuthorityFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.grant.consented_at = timezone.now() - timedelta(days=1)
        self.grant.save()
        self.conversation.history_backfilled_at = timezone.now() - timedelta(hours=2)
        self.conversation.save()
        self.state = ensure_state(self.conversation)

    def dead(self, conversation=None, *, days=2, number=1, **metadata):
        conversation = conversation or self.conversation
        return SlackDmMirrorDelivery.objects.create(
            conversation=conversation, source_platform="slack", source_author_id="UOTHER",
            source_message_id=f"{int(timezone.now().timestamp()) - days * 86400}.{number:06d}",
            operation="create", status="dead", encrypted_text="",
            metadata={"backfill": True, "participant_hash": "old-boundary", **metadata},
            last_error="Private conversation participants changed", available_at=timezone.now(),
        )

    def scan(self, messages, *, limited=False, has_more=False):
        self.state.jobs.filter(kind="archive").update(due_at=timezone.now())
        self.state.refresh_from_db()
        with patch.object(dm, "_call_slack_with_grant_authority", return_value={
            "ok": True, "messages": messages, "is_limited": limited, "has_more": has_more,
        }) as api:
            private_page(claim_job(kinds=["archive"]), self.state)
        return api

    def test_durable_seed_fetches_new_body_and_supersedes_only_qualified_absence(self):
        observed = self.dead()
        absent = self.dead(number=2)
        with patch.object(dm, "WebClient") as provider:
            seed_states()
        provider.assert_not_called()
        observed.refresh_from_db()
        self.assertEqual(observed.status, "dead")
        self.assertEqual(observed.encrypted_text, "")
        self.assertTrue(observed.metadata["history_recovery_scheduled"])
        api = self.scan([{"ts": observed.source_message_id, "user": "UOTHER", "text": "fresh source body"}])
        self.assertGreater(int(api.call_args.kwargs["oldest"]), int(timezone.now().timestamp()) - 31 * 86400)
        observed.refresh_from_db()
        self.assertEqual(observed.status, "pending")
        self.assertIn("fresh source body", observed.encrypted_text)
        self.assertEqual(observed.metadata["participant_hash"], self.conversation.participant_hash)
        absent.refresh_from_db()
        self.assertEqual(absent.status, "dead")
        self.assertEqual(absent.encrypted_text, "")
        self.assertTrue(absent.metadata["history_recovery_superseded"])
        self.assertEqual(schedule_private_recoveries(), 0)

    def test_source_limit_does_not_supersede_and_does_not_restart_recovery_forever(self):
        row = self.dead()
        self.assertEqual(schedule_private_recoveries(), 1)
        self.scan([], limited=True)
        row.refresh_from_db()
        self.assertTrue(row.metadata["history_recovery_scheduled"])
        self.assertFalse(row.metadata.get("history_recovery_superseded", False))
        self.assertEqual(schedule_private_recoveries(), 0)

    def test_old_rows_are_excluded_without_source_call_or_scan_reset(self):
        row = self.dead(days=31)
        completed_at = self.conversation.history_backfilled_at
        self.assertEqual(schedule_private_recoveries(), 1)
        row.refresh_from_db()
        self.conversation.refresh_from_db()
        self.assertEqual(row.status, "completed")
        self.assertTrue(row.metadata["history_outside_window"])
        self.assertEqual(self.conversation.history_backfilled_at, completed_at)
        self.assertEqual(schedule_private_recoveries(), 0)

    def test_permanent_rejection_and_delete_are_not_recovered(self):
        permanent = self.dead(permanent_failure=True)
        deleted = self.dead(number=2)
        deleted.operation = "delete"
        deleted.save()
        self.assertEqual(schedule_private_recoveries(), 0)
        for row in [permanent, deleted]:
            row.refresh_from_db()
            self.assertEqual(row.status, "dead")
            self.assertNotIn("history_recovery_scheduled", row.metadata)

    def test_incomplete_scan_and_active_lease_keep_cursor_and_rows(self):
        row = self.dead()
        self.conversation.history_backfilled_at = None
        self.conversation.save()
        self.assertEqual(schedule_private_recoveries(), 0)
        self.conversation.history_backfilled_at = timezone.now()
        self.conversation.save()
        job = self.state.jobs.get(kind="archive")
        job.checkpoint = {"cursor": "in-flight"}
        job.lease_token = uuid.uuid4()
        job.lease_expires_at = timezone.now() + timedelta(minutes=1)
        job.save()
        self.assertEqual(schedule_private_recoveries(), 0)
        row.refresh_from_db()
        job.refresh_from_db()
        self.assertNotIn("history_recovery_scheduled", row.metadata)
        self.assertEqual(job.checkpoint, {"cursor": "in-flight"})

    def test_revoke_winning_before_schedule_cannot_change_tombstones(self):
        row = self.dead()
        capture = dm._capture_slack_grant_api_authority
        def revoke(grant, **kwargs):
            authority = capture(grant, **kwargs)
            SlackDmMirrorGrant.objects.filter(pk=grant.pk).update(status="revoked", revoked_at=timezone.now())
            return authority
        with patch.object(dm, "_capture_slack_grant_api_authority", side_effect=revoke):
            self.assertEqual(schedule_private_recoveries(), 0)
        row.refresh_from_db()
        self.assertNotIn("history_recovery_scheduled", row.metadata)

    def test_between_page_checkpoint_without_active_lease_is_preserved(self):
        row = self.dead()
        job = self.state.jobs.get(kind="head")
        job.checkpoint = {"cursor": "partial-window"}
        job.save()
        self.assertEqual(schedule_private_recoveries(), 0)
        job.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual(job.checkpoint, {"cursor": "partial-window"})
        self.assertNotIn("history_recovery_scheduled", row.metadata)

    def test_row_batch_is_bounded_and_each_tombstone_gets_only_one_recovery(self):
        first, second = self.dead(), self.dead(number=2)
        self.assertEqual(schedule_private_recoveries(row_limit=1), 1)
        self.assertEqual(self.conversation.deliveries.filter(metadata__history_recovery_scheduled=True).count(), 1)
        self.scan([])
        self.assertEqual(schedule_private_recoveries(row_limit=1), 1)
        self.scan([])
        self.assertEqual(schedule_private_recoveries(row_limit=1), 0)
        for row in [first, second]:
            row.refresh_from_db()
            self.assertTrue(row.metadata["history_recovery_superseded"])

    def test_quiet_owner_gets_turn_before_busy_owners_next_conversation(self):
        self.dead()
        busy = SlackDmMirrorConversation.objects.create(
            grant=self.grant, slack_workspace_id="TIOAUTH", slack_conversation_id="DBUSY",
            participant_slack_ids=["UOWNER", "UOTHER"], participant_hash="b" * 64,
            mlai_channel_id=uuid.uuid4(), status="live",
            history_backfilled_at=timezone.now()-timedelta(hours=2),
        )
        self.dead(busy)
        ensure_state(busy)
        user = get_user_model().objects.create_user(email="quiet-recovery@example.com")
        connection = ExternalServiceConnection.objects.create(
            user=user, provider="slack", external_account_id="TIOAUTH", access_token="xoxp-synthetic-quiet",
            scopes=SCOPES, provider_metadata={"team": {"id": "TIOAUTH"}, "authed_user": {"id": "UQUIET"}},
        )
        grant = SlackDmMirrorGrant.objects.create(
            user=user, connection=connection, slack_workspace_id="TIOAUTH", slack_user_id="UQUIET",
            consented_at=timezone.now()-timedelta(days=1),
        )
        quiet = SlackDmMirrorConversation.objects.create(
            grant=grant, slack_workspace_id="TIOAUTH", slack_conversation_id="DQUIET",
            participant_slack_ids=["UQUIET", "UOTHER"], participant_hash="c"*64,
            mlai_channel_id=uuid.uuid4(), status="live", history_backfilled_at=timezone.now(),
        )
        quiet_row = self.dead(quiet)
        ensure_state(quiet)
        self.assertEqual(schedule_private_recoveries(limit=1), 1)
        self.assertEqual(schedule_private_recoveries(limit=1), 1)
        quiet_row.refresh_from_db()
        self.assertTrue(quiet_row.metadata["history_recovery_scheduled"])
        busy.refresh_from_db()
        self.assertIsNotNone(busy.history_backfilled_at)
        # An authority failure also consumes only that owner's attempt turn.
        quiet.history_backfilled_at = timezone.now()
        quiet.save()
        self.dead(quiet, number=2)
        capture = dm._capture_slack_grant_api_authority
        def reject_busy(grant, **kwargs):
            self.assertFalse(kwargs["refresh_token"])
            if grant.pk == self.grant.pk:
                raise dm.SlackDmMirrorAuthorizationError("stale authority")
            return capture(grant, **kwargs)
        with patch.object(dm, "_capture_slack_grant_api_authority", side_effect=reject_busy):
            self.assertEqual(schedule_private_recoveries(limit=1), 0)
            self.assertEqual(schedule_private_recoveries(limit=1), 1)

    def legacy_reaction(self, *, reaction="clap", number=1):
        from integrations.services.message_sync.reaction_recovery import FIX_DEPLOYED_AT, LEGACY_ERRORS
        self.conversation.participant_buzz_pubkeys = [self.owner_key]
        self.conversation.save()
        row = self.dead(days=7, number=number)
        target = row.source_message_id
        row.operation = "reaction_add"
        row.source_message_id = dm.reaction_object_id(message_id=target, reaction=reaction, author_id="UOTHER")
        row.encrypted_text = "rejected old payload must never be replayed"
        row.metadata = {"backfill": True, "permanent_failure": True, "history_recovery_scheduled": True,
                        "participant_hash": self.conversation.participant_hash,
                        "slack_reaction": reaction, "target_source_message_id": target,
                        "reaction_object_id": row.source_message_id, "event_ts": target}
        row.last_error = LEGACY_ERRORS[0]
        row.save()
        SlackDmMirrorDelivery.objects.filter(pk=row.pk).update(updated_at=FIX_DEPLOYED_AT-timedelta(hours=3))
        return row, target

    def test_legacy_reaction_is_rebuilt_only_after_qualified_fresh_source(self):
        from integrations.services.message_sync.reaction_recovery import CONTRACT_KEY
        row, target = self.legacy_reaction()
        self.assertEqual(schedule_private_recoveries(), 1)
        row.refresh_from_db()
        self.assertEqual(row.status, "dead")
        self.assertEqual(row.encrypted_text, "")
        self.assertTrue(row.metadata["permanent_failure"])
        self.assertEqual(row.metadata[CONTRACT_KEY]["error_code"], "adapter_http_400")
        self.scan([{"ts": target, "user": "UOTHER", "text": "source parent",
                    "reactions": [{"name": "clap", "users": ["UOTHER"]}]}])
        row.refresh_from_db()
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.encrypted_text, "👏")
        self.assertNotIn("permanent_failure", row.metadata)
        self.assertEqual(row.metadata[CONTRACT_KEY]["outcome"], "fresh_source_observed")
        self.assertEqual(schedule_private_recoveries(), 0)

    def test_legacy_reaction_limited_observation_keeps_permanent_row_erased(self):
        row, target = self.legacy_reaction()
        self.assertEqual(schedule_private_recoveries(), 1)
        self.scan([{"ts": target, "user": "UOTHER", "text": "source parent",
                    "reactions": [{"name": "clap", "users": ["UOTHER"]}]}], limited=True)
        row.refresh_from_db()
        self.assertEqual(row.status, "dead")
        self.assertEqual(row.encrypted_text, "")
        self.assertTrue(row.metadata["permanent_failure"])
        self.assertFalse(row.metadata.get("history_recovery_superseded", False))
        self.assertEqual(schedule_private_recoveries(), 0)

    def test_legacy_reaction_waits_for_whole_archive_and_ignores_other_scan_epochs(self):
        from django.db import transaction
        row, target = self.legacy_reaction()
        self.assertEqual(schedule_private_recoveries(), 1)
        self.scan([{"ts": target, "user": "UOTHER", "text": "source parent",
                    "reactions": [{"name": "clap", "users": ["UOTHER"]}]}], has_more=True)
        row.refresh_from_db()
        archive_epoch = row.metadata["history_scan_epoch"]
        self.assertEqual(row.status, "dead")
        self.assertEqual(row.encrypted_text, "")
        with transaction.atomic():
            dm._upsert_history_delivery(
                self.conversation, source_message_id=row.source_message_id, author_id="UOTHER",
                operation="reaction_add", text="👏", held_until=timezone.now(),
                metadata={**row.metadata, "history_scan_epoch": "interleaved-head-scan"},
            )
        row.refresh_from_db()
        self.assertEqual(row.metadata["history_scan_epoch"], archive_epoch)
        self.scan([])
        row.refresh_from_db()
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.encrypted_text, "👏")

    def test_legacy_reaction_revoked_device_cannot_release_after_observation(self):
        from community_chat.models import CommunityChatDevice
        row, target = self.legacy_reaction()
        self.assertEqual(schedule_private_recoveries(), 1)
        self.scan([{"ts": target, "user": "UOTHER", "text": "source parent",
                    "reactions": [{"name": "clap", "users": ["UOTHER"]}]}], has_more=True)
        CommunityChatDevice.objects.filter(user=self.user).update(status="revoked", revoked_at=timezone.now())
        self.scan([])
        row.refresh_from_db()
        self.assertEqual(row.status, "dead")
        self.assertTrue(row.metadata["permanent_failure"])
        self.assertEqual(row.encrypted_text, "")

    def test_legacy_reaction_qualified_absence_supersedes_without_sending(self):
        from integrations.services.message_sync.reaction_recovery import CONTRACT_KEY
        row, _ = self.legacy_reaction()
        self.assertEqual(schedule_private_recoveries(), 1)
        self.scan([])
        row.refresh_from_db()
        self.assertEqual(row.status, "dead")
        self.assertEqual(row.encrypted_text, "")
        self.assertTrue(row.metadata["history_recovery_superseded"])
        self.assertEqual(row.metadata[CONTRACT_KEY]["outcome"], "source_absent")

    def test_legacy_exception_rejects_supported_old_emoji_new_failure_and_wrong_boundary(self):
        from integrations.services.message_sync.reaction_recovery import CONTRACT_KEY, FIX_DEPLOYED_AT
        allowed_old, _ = self.legacy_reaction(reaction="thumbsup")
        newer, _ = self.legacy_reaction(number=2)
        SlackDmMirrorDelivery.objects.filter(pk=newer.pk).update(updated_at=FIX_DEPLOYED_AT+timedelta(seconds=1))
        wrong_boundary, _ = self.legacy_reaction(number=3)
        wrong_boundary.metadata["participant_hash"] = "retired-boundary"
        SlackDmMirrorDelivery.objects.filter(pk=wrong_boundary.pk).update(metadata=wrong_boundary.metadata)
        self.assertEqual(schedule_private_recoveries(), 0)
        for row in [allowed_old, newer, wrong_boundary]:
            row.refresh_from_db()
            self.assertEqual(row.status, "dead")
            self.assertTrue(row.metadata["permanent_failure"])
            self.assertTrue(row.metadata["history_recovery_scheduled"])
            self.assertNotIn(CONTRACT_KEY, row.metadata)
