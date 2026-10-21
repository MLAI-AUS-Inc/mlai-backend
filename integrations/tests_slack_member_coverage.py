"""Disposable database fixtures for coverage ORM projection and command safety."""
import json
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from integrations.models import (
    CommunityBridgeChannel, CommunityBridgeIdentityLink,
    ExternalServiceConnection, SlackDmMirrorGrant, SlackOwnerConversationInventory,
)
from integrations.services.slack_member_coverage import CONSENT_KEY, INVENTORY_KEY, REQUIRED_SCOPES


class SlackMemberCoverageCommandTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="coverage-fixture@example.invalid")
        self.connection = ExternalServiceConnection.objects.create(
            user=self.user, provider="slack", external_account_id="TTEST",
            scopes=list(REQUIRED_SCOPES), provider_metadata={
                "team": {"id": "TTEST"}, "authed_user": {"id": "UTEST"},
            },
        )
        self.grant = SlackDmMirrorGrant.objects.create(
            user=self.user, connection=self.connection, slack_workspace_id="TTEST",
            slack_user_id="UTEST", consented_at=timezone.now(),
        )
        CommunityBridgeIdentityLink.objects.create(
            user=self.user, slack_workspace_id="TTEST", slack_user_id="UTEST",
            buzz_pubkey="a" * 64, display_name="Synthetic fixture", verification_method="account_challenge",
            verification_reference="coverage-fixture", verified_at=timezone.now(),
        )
        identity = {
            "grant_id": self.grant.pk, "connection_id": self.connection.pk,
            "workspace_id": "TTEST", "slack_user_id": "UTEST",
            "consent_generation": self.grant.consented_at.isoformat(),
            "consent_version": self.grant.consent_version, "oauth_generation": 0,
            "scopes": list(REQUIRED_SCOPES),
        }
        self.connection.sync_cursor = {
            CONSENT_KEY: identity,
            INVENTORY_KEY: {**identity, "coverage": {"public_channel": "complete"},
                            "public_sweep": "", "public_cursor": "", "last_public_at": timezone.now().isoformat()},
        }
        self.connection.save(update_fields=("sync_cursor", "updated_at"))
        CommunityBridgeChannel.objects.create(
            slack_workspace_id="TTEST", slack_channel_id="CTEST", destination_platform="buzz",
            destination_channel_id="fixture-room", slack_channel_name="not-in-output-sentinel",
        )
        SlackOwnerConversationInventory.objects.create(
            grant=self.grant, slack_conversation_id="CTEST", kind="public_channel",
            source_name="not-in-output-sentinel", eligibility="eligible", source_archived=False,
        )

    def report(self, workspace="TTEST"):
        output = StringIO()
        call_command("slack_member_coverage", workspace=workspace, stdout=output)
        return json.loads(output.getvalue())

    def test_command_uses_only_selects_and_never_decrypts_or_calls_slack(self):
        with (CaptureQueriesContext(connection) as queries,
              patch("integrations.fields.decrypt_credential_value", side_effect=AssertionError("No credential reads")),
              patch("slack_sdk.WebClient", side_effect=AssertionError("No Slack calls"))):
            result = self.report()
        self.assertEqual(result["scope_qualified_linked_member_count"], 1)
        self.assertEqual(result["channels"][0]["observed_member_count"], 1)
        self.assertEqual(len(queries), 4)
        for query in queries:
            self.assertTrue(query["sql"].lstrip().upper().startswith("SELECT"))
            self.assertNotIn('"access_token"', query["sql"])
            self.assertNotIn('"refresh_token"', query["sql"])
        self.assertNotIn("not-in-output-sentinel", json.dumps(result))

    def test_workspace_selection_never_includes_other_grants_or_channels(self):
        result = self.report("TOTHER")
        self.assertEqual(result["scope_qualified_linked_member_count"], 0)
        self.assertEqual(result["channels"], [])

    def test_missing_scopes_are_reported_without_using_saved_inventory(self):
        self.connection.scopes = ["chat:write"]
        self.connection.save(update_fields=("scopes", "updated_at"))
        result = self.report()
        self.assertEqual(result["members_missing_scopes"][0]["missing_scopes"], ["channels:history", "channels:read"])
        self.assertEqual(result["channels"][0]["observed_member_count"], 0)

    def test_connection_relink_cannot_reuse_an_old_owners_inventory(self):
        self.connection.provider_metadata = {"team": {"id": "TTEST"}, "authed_user": {"id": "UOTHER"}}
        self.connection.save(update_fields=("provider_metadata", "updated_at"))
        result = self.report()
        self.assertEqual(result["scope_qualified_linked_member_count"], 0)
        self.assertEqual(result["excluded_grants"][0]["reasons"], ["connection_identity_or_status"])
        self.assertEqual(result["channels"][0]["observed_member_count"], 0)

    def test_revoked_identity_link_is_not_counted(self):
        CommunityBridgeIdentityLink.objects.filter(user=self.user).update(revoked_at=timezone.now())
        self.assertEqual(self.report()["scope_qualified_linked_member_count"], 0)
