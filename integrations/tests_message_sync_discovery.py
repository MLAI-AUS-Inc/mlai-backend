"""Discovery fairness and late-worker fencing against disposable PostgreSQL."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TransactionTestCase
from django.utils import timezone

from community_chat.tests.test_slack_dm_io_authority import SlackDmIoAuthorityFixture
from integrations.models import ExternalServiceConnection, SlackDmMirrorGrant
from integrations.services import slack_dm_mirror as dm
from integrations.services.message_sync.discovery import (
    KEY, claim_discovery, discovery_context, finish_discovery,
)
from integrations.services.message_sync.scheduler import LeaseLost


class DiscoveryTests(SlackDmIoAuthorityFixture, TransactionTestCase):
    def make_grant(self, index, workspace):
        user = get_user_model().objects.create_user(email=f"discovery-{index}@example.invalid")
        connection = ExternalServiceConnection.objects.create(user=user, provider="slack", access_token="synthetic")
        return SlackDmMirrorGrant.objects.create(user=user, connection=connection,
            slack_workspace_id=workspace, slack_user_id=f"U{index}", consented_at=timezone.now())

    def release_due(self):
        for connection in ExternalServiceConnection.objects.all():
            value = dict(connection.sync_cursor or {})
            if KEY in value:
                value[KEY] = {**value[KEY], "due": 0}
                connection.sync_cursor = value
                connection.save(update_fields=["sync_cursor"])

    def test_incomplete_grants_rotate_across_workspaces_and_every_user(self):
        busy = [self.make_grant(i, "TIOAUTH") for i in range(12)]
        quiet = self.make_grant(99, "TQUIET")
        seen = []
        for _ in range(27):
            lease = claim_discovery(900)
            self.assertIsNotNone(lease)
            seen.append(lease.grant_id)
            finish_discovery(lease)
            self.release_due()
        self.assertEqual(seen[0:4], [self.grant.pk, quiet.pk, busy[0].pk, quiet.pk])
        self.assertEqual(set(seen), {self.grant.pk, quiet.pk, *(g.pk for g in busy)})

    def test_lease_blocks_second_claim_and_expired_worker_cannot_write_or_call(self):
        old = claim_discovery(900)
        self.assertIsNone(claim_discovery(900))
        self.connection.refresh_from_db()
        self.connection.sync_cursor[KEY]["expires"] = 0
        self.connection.save(update_fields=["sync_cursor"])
        current = claim_discovery(900)
        self.assertNotEqual(old.token, current.token)
        authority = dm._capture_slack_grant_api_authority(self.grant)
        with discovery_context(old), patch.object(dm, "WebClient") as client:
            with self.assertRaises(LeaseLost), transaction.atomic():
                dm._lock_slack_grant_api_authority(authority, required_scopes=dm.DIRECT_DM_SCOPES)
            with self.assertRaises(LeaseLost):
                dm._call_slack_with_grant_authority(authority, "users_conversations", required_scopes=dm.DIRECT_DM_SCOPES)
            client.assert_not_called()
        with self.assertRaises(LeaseLost):
            finish_discovery(old)
        finish_discovery(current)

    def test_deferral_preserves_checkpoint_and_allows_another_user(self):
        other = self.make_grant(100, "TIOAUTH")
        self.connection.sync_cursor = {dm.DISCOVERY_CHECKPOINT_KEY: {"cursor": "next-page"}}
        self.connection.save(update_fields=["sync_cursor"])
        lease = claim_discovery(900)
        finish_discovery(lease, delay_seconds=60, error_code="BudgetDeferred")
        self.assertEqual(claim_discovery(900).grant_id, other.pk)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.sync_cursor[dm.DISCOVERY_CHECKPOINT_KEY], {"cursor": "next-page"})
