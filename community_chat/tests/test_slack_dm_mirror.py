import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase
from slack_sdk.errors import SlackApiError

from community_chat.account_sessions import issue_account_session
from community_chat.models import (
    CommunityChatDevice,
    CommunityChatEmailCodeChallenge,
    DeviceBindingStatus,
)
from integrations.models import (
    CommunityBridgeDeliveryStatus,
    CommunityBridgeDeliveryType,
    CommunityBridgeIdentityLink,
    CommunityBridgeIdentityVerificationMethod,
    CommunityBridgePlatform,
    ExternalServiceConnection,
    ExternalServiceConnectionStatus,
    ExternalServiceProvider,
    SlackDmMirrorConversation,
    SlackDmMirrorConversationStatus,
    SlackDmMirrorDelivery,
    SlackDmMirrorGrant,
    SlackDmMirrorGrantStatus,
)
from integrations.services import external_connectors, slack_dm_mirror
from integrations.services.community_bridge.buzz import BuzzBridgePermanentError
from integrations.services.external_connectors import ConnectorOAuthError
from integrations.services.slack_oauth_authority import SLACK_OAUTH_GENERATION_KEY
from integrations.services.slack_dm_mirror import (
    activate_connection,
    active_grant_for_user,
    backfill_grant,
    discover_conversations,
    ensure_owner_identity,
    ingest_mlai_dm_event,
    ingest_slack_dm_event,
    open_slack_dm,
    process_due_history_backfills,
    process_ready_deliveries,
    status_payload,
)
from organizations.models import Organization
from startup_updates.models import UserStartupBinding

DIRECT_SCOPES = [
    "im:read",
    "im:history",
    "im:write",
    "chat:write",
    "users:read",
    "reactions:read",
    "reactions:write",
    "files:read",
]
GROUP_SCOPES = ["mpim:read", "mpim:history", "mpim:write"]
SCOPES = DIRECT_SCOPES + GROUP_SCOPES
OAUTH_SCOPES = SCOPES + [
    "channels:read",
    "channels:history",
    "groups:read",
    "groups:history",
    "team:read",
]


def _slack_api_error(code: str, *, status_code: int = 200) -> SlackApiError:
    response = MagicMock()
    response.get.side_effect = {"error": code}.get
    response.status_code = status_code
    response.headers = {}
    return SlackApiError(code, response)


def _slack_connection(user, slack_user_id):
    return ExternalServiceConnection.objects.create(
        user=user,
        provider=ExternalServiceProvider.SLACK,
        access_token=f"xoxp-{slack_user_id}",
        scopes=SCOPES,
        external_account_id="TMLAI",
        account_label="MLAI",
        provider_metadata={
            "team": {"id": "TMLAI", "name": "MLAI"},
            "authed_user": {"id": slack_user_id, "scope": ",".join(SCOPES)},
        },
    )


class SlackDmMirrorApiTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="slack-link@example.com")
        challenge = CommunityChatEmailCodeChallenge.objects.create(
            user=self.user,
            email_digest="a" * 64,
            code_digest="b" * 64,
            client_id="mlai-chat-web",
            installation_id=uuid.uuid4(),
            origin="https://chat.mlai.au",
            platform="web",
            device_name="Chrome",
            public_key="1" * 64,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        credentials = issue_account_session(self.user, challenge)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {credentials.access_token}")
        self.url = reverse("community_chat_slack")

    @override_settings(COMMUNITY_CHAT_FRONTEND_URL="https://chat.mlai.au")
    def test_link_returns_provider_bound_top_level_oauth_url_and_privacy_copy(self):
        response = self.client.post(self.url, {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["authorization_url"].startswith("http"))
        self.assertIn(
            "/integrations/connect/slack?", response.data["authorization_url"]
        )
        self.assertIn(
            "slack-dm-mirror-v3-owner-direct-and-group",
            response.data["consent"]["version"],
        )
        self.assertIn(
            "direct and group Slack DMs", response.data["consent"]["summary"]
        )
        self.assertIn(
            "Up to 30 days of history", response.data["consent"]["summary"]
        )
        self.assertFalse(response.data["privacy"]["requires_both_participants"])
        self.assertTrue(response.data["privacy"]["owner_controlled"])
        self.assertFalse(response.data["privacy"]["included_in_roo"])

    @override_settings(
        COMMUNITY_CHAT_FRONTEND_URL="https://chat.mlai.au",
        SLACK_CLIENT_ID="client-id",
        SLACK_CLIENT_SECRET="client-secret",
        SLACK_OAUTH_REDIRECT_URI="https://api.mlai.au/integrations/callback/slack",
        SLACK_OAUTH_USER_SCOPES=OAUTH_SCOPES,
    )
    def test_link_ticket_survives_top_level_navigation_and_requests_user_dm_scopes(
        self,
    ):
        link_response = self.client.post(self.url, {}, format="json")
        link = urlparse(link_response.data["authorization_url"])

        self.client.credentials()
        oauth_response = self.client.get(f"{link.path}?{link.query}")

        self.assertEqual(oauth_response.status_code, 302)
        slack_url = urlparse(oauth_response["Location"])
        self.assertEqual(slack_url.netloc, "slack.com")
        scopes = set(parse_qs(slack_url.query)["user_scope"][0].split(","))
        self.assertTrue(set(SCOPES).issubset(scopes))

    @patch("community_chat.slack_views.backfill_grant")
    def test_linked_owner_can_request_an_idempotent_history_backfill(self, backfill):
        connection = _slack_connection(self.user, "UBACKFILL")
        grant = SlackDmMirrorGrant.objects.create(
            user=self.user,
            connection=connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UBACKFILL",
            consented_at=timezone.now(),
        )

        response = self.client.patch(self.url, {"action": "backfill"}, format="json")

        self.assertEqual(response.status_code, 200)
        backfill.assert_called_once_with(grant)

    @override_settings(COMMUNITY_CHAT_FRONTEND_URL="https://chat.mlai.au")
    def test_existing_slack_connection_without_dm_scopes_is_reauthorized(self):
        connection = _slack_connection(self.user, "UREAUTH")
        connection.scopes = ["channels:read"]
        connection.save(update_fields=("scopes", "updated_at"))

        response = self.client.post(self.url, {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["needs_reauthorization"])
        self.assertIn(
            "/integrations/connect/slack?", response.data["authorization_url"]
        )

    def test_resume_rejects_a_connection_without_a_usable_token(self):
        connection = _slack_connection(self.user, "URESUME")
        connection.access_token = ""
        connection.save(update_fields=("access_token", "updated_at"))
        SlackDmMirrorGrant.objects.create(
            user=self.user,
            connection=connection,
            slack_workspace_id="TMLAI",
            slack_user_id="URESUME",
            status="paused",
            consented_at=timezone.now(),
            paused_at=timezone.now(),
        )

        response = self.client.patch(
            self.url,
            {"action": "resume"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Re-authorize Slack", str(response.data))

    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.unregister_private_conversation"
    )
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_disconnect_revokes_adapter_registration_and_clears_the_local_token(
        self,
        web_client,
        unregister_private_conversation,
    ):
        connection = _slack_connection(self.user, "UREVOKE")
        grant = SlackDmMirrorGrant.objects.create(
            user=self.user,
            connection=connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UREVOKE",
            consented_at=timezone.now(),
        )
        channel_id = uuid.uuid4()
        conversation = SlackDmMirrorConversation.objects.create(
            grant=grant,
            slack_workspace_id="TMLAI",
            slack_conversation_id="DREVOKE",
            mlai_channel_id=channel_id,
            status=SlackDmMirrorConversationStatus.LIVE,
        )

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 204)
        unregister_private_conversation.assert_called_once_with(str(channel_id))
        web_client.return_value.auth_revoke.assert_called_once_with()
        connection.refresh_from_db()
        self.assertEqual(connection.status, "disconnected")
        self.assertEqual(connection.access_token, "")

    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.unregister_private_conversation"
    )
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_adapter_outage_revokes_locally_and_leaves_durable_cleanup_retry(
        self,
        web_client,
        unregister_private_conversation,
    ):
        connection = _slack_connection(self.user, "URETRY")
        grant = SlackDmMirrorGrant.objects.create(
            user=self.user,
            connection=connection,
            slack_workspace_id="TMLAI",
            slack_user_id="URETRY",
            consented_at=timezone.now(),
        )
        channel_id = uuid.uuid4()
        conversation = SlackDmMirrorConversation.objects.create(
            grant=grant,
            slack_workspace_id="TMLAI",
            slack_conversation_id="DRETRY",
            mlai_channel_id=channel_id,
            status=SlackDmMirrorConversationStatus.LIVE,
        )
        unregister_private_conversation.side_effect = RuntimeError(
            "adapter unavailable"
        )
        self.client.raise_request_exception = False

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 204)
        grant.refresh_from_db()
        connection.refresh_from_db()
        conversation.refresh_from_db()
        self.assertEqual(grant.status, SlackDmMirrorGrantStatus.REVOKED)
        self.assertIn("revocation is pending", grant.last_error)
        self.assertEqual(
            connection.status, ExternalServiceConnectionStatus.DISCONNECTED
        )
        self.assertEqual(connection.access_token, "")
        self.assertEqual(
            conversation.status,
            SlackDmMirrorConversationStatus.PAUSED,
        )
        web_client.return_value.auth_revoke.assert_called_once_with()

        unregister_private_conversation.side_effect = None
        SlackDmMirrorDelivery.objects.filter(
            source_message_id__startswith=slack_dm_mirror.REGISTRATION_STATE_PREFIX,
            status=CommunityBridgeDeliveryStatus.PENDING,
        ).update(available_at=timezone.now() - timedelta(seconds=1))
        retry_response = self.client.delete(self.url)

        self.assertEqual(retry_response.status_code, 204)
        self.assertEqual(unregister_private_conversation.call_count, 2)
        web_client.return_value.auth_revoke.assert_called_once_with()
        grant.refresh_from_db()
        self.assertEqual(grant.last_error, "")
        self.assertFalse(
            SlackDmMirrorDelivery.objects.filter(
                source_message_id__startswith=slack_dm_mirror.REGISTRATION_STATE_PREFIX,
                status__in=(
                    CommunityBridgeDeliveryStatus.PENDING,
                    CommunityBridgeDeliveryStatus.PROCESSING,
                ),
            ).exists()
        )

    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.unregister_private_conversation"
    )
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_generic_connector_disconnect_also_revokes_private_registration(
        self,
        web_client,
        unregister_private_conversation,
    ):
        connection = _slack_connection(self.user, "UGENERIC")
        grant = SlackDmMirrorGrant.objects.create(
            user=self.user,
            connection=connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UGENERIC",
            consented_at=timezone.now(),
        )
        channel_id = uuid.uuid4()
        conversation = SlackDmMirrorConversation.objects.create(
            grant=grant,
            slack_workspace_id="TMLAI",
            slack_conversation_id="DGENERIC",
            mlai_channel_id=channel_id,
            status=SlackDmMirrorConversationStatus.LIVE,
        )
        self.client.credentials()
        self.client.force_authenticate(self.user)

        response = self.client.delete(
            f"/api/v1/integrations/sources/connections/{connection.id}"
        )

        self.assertEqual(response.status_code, 200)
        unregister_private_conversation.assert_called_once_with(str(channel_id))
        web_client.return_value.auth_revoke.assert_called_once_with()
        grant.refresh_from_db()
        connection.refresh_from_db()
        self.assertEqual(grant.status, SlackDmMirrorGrantStatus.REVOKED)
        self.assertEqual(
            connection.status, ExternalServiceConnectionStatus.DISCONNECTED
        )
        self.assertEqual(connection.access_token, "")
        self.assertEqual(
            connection.provider_metadata,
            {SLACK_OAUTH_GENERATION_KEY: 1},
        )
        self.assertEqual(connection.sync_cursor, {})

    @patch("integrations.services.external_connectors.requests.post")
    def test_disconnect_generation_rejects_and_revokes_a_late_oauth_callback(
        self,
        post,
    ):
        slack_dm_mirror.revoke_user_grant(self.user)

        with self.assertRaisesRegex(
            ConnectorOAuthError,
            "disconnected while authorization was in progress",
        ):
            external_connectors._store_slack_connection(
                self.user,
                None,
                {
                    "access_token": "xoxb-late-bot-token",
                    "team": {"id": "TLATE", "name": "Late workspace"},
                    "authed_user": {
                        "id": "ULATE",
                        "access_token": "xoxp-late-user-token",
                        "scope": ",".join(SCOPES),
                    },
                },
                expected_generation=0,
            )

        post.assert_called_once_with(
            "https://slack.com/api/auth.revoke",
            headers={"Authorization": "Bearer xoxp-late-user-token"},
            timeout=(3, 20),
        )
        connections = list(
            ExternalServiceConnection.objects.filter(
                user=self.user,
                provider=ExternalServiceProvider.SLACK,
            )
        )
        self.assertTrue(connections)
        self.assertTrue(
            all(
                connection.access_token
                not in {"xoxb-late-bot-token", "xoxp-late-user-token"}
                for connection in connections
            )
        )
        self.assertFalse(SlackDmMirrorGrant.objects.filter(user=self.user).exists())

    def test_new_workspace_oauth_does_not_overwrite_an_active_grant_connection(self):
        original = _slack_connection(self.user, "UORIGINAL")
        grant = SlackDmMirrorGrant.objects.create(
            user=self.user,
            connection=original,
            slack_workspace_id="TMLAI",
            slack_user_id="UORIGINAL",
            consented_at=timezone.now(),
        )

        connected = external_connectors._store_slack_connection(
            self.user,
            None,
            {
                "access_token": "xoxb-new-workspace",
                "team": {"id": "TSECOND", "name": "Second workspace"},
                "authed_user": {
                    "id": "UNEW",
                    "access_token": "xoxp-new-workspace",
                    "scope": ",".join(SCOPES),
                },
            },
            expected_generation=0,
        )

        self.assertNotEqual(connected.pk, original.pk)
        self.assertEqual(connected.external_account_id, "TSECOND")
        self.assertEqual(connected.provider_metadata["authed_user"]["id"], "UNEW")
        original.refresh_from_db()
        grant.refresh_from_db()
        self.assertEqual(original.access_token, "xoxp-UORIGINAL")
        self.assertEqual(original.provider_metadata["team"]["id"], "TMLAI")
        self.assertEqual(grant.connection_id, original.pk)

    @patch("integrations.services.external_connectors.requests.post")
    def test_late_oauth_callback_rechecks_current_startup_membership(self, post):
        organization = Organization.objects.create(
            name="Former startup",
            domain="former-startup.example",
        )
        binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            role="founder",
        )
        binding.delete()

        with self.assertRaisesRegex(ConnectorOAuthError, "Startup access changed"):
            external_connectors._store_slack_connection(
                self.user,
                organization,
                {
                    "access_token": "xoxb-shared-installation",
                    "team": {"id": "TFORMER", "name": "Former startup"},
                    "authed_user": {
                        "id": "UFORMER",
                        "access_token": "xoxp-former-user",
                        "scope": ",".join(SCOPES),
                    },
                },
                expected_generation=0,
            )

        post.assert_called_once_with(
            "https://slack.com/api/auth.revoke",
            headers={"Authorization": "Bearer xoxp-former-user"},
            timeout=(3, 20),
        )
        self.assertFalse(
            ExternalServiceConnection.objects.filter(
                user=self.user,
                organization=organization,
                provider=ExternalServiceProvider.SLACK,
            ).exists()
        )

    @patch("integrations.services.external_connectors.requests.post")
    def test_same_workspace_different_user_oauth_cannot_replace_active_identity(
        self,
        post,
    ):
        original = _slack_connection(self.user, "UORIGINAL")
        SlackDmMirrorGrant.objects.create(
            user=self.user,
            connection=original,
            slack_workspace_id="TMLAI",
            slack_user_id="UORIGINAL",
            consented_at=timezone.now(),
        )

        with self.assertRaisesRegex(
            ConnectorOAuthError,
            "different Slack identity",
        ):
            external_connectors._store_slack_connection(
                self.user,
                None,
                {
                    "access_token": "xoxb-replacement",
                    "team": {"id": "TMLAI", "name": "MLAI"},
                    "authed_user": {
                        "id": "UOTHER",
                        "access_token": "xoxp-replacement",
                        "scope": ",".join(SCOPES),
                    },
                },
                expected_generation=0,
            )

        post.assert_called_once_with(
            "https://slack.com/api/auth.revoke",
            headers={"Authorization": "Bearer xoxp-replacement"},
            timeout=(3, 20),
        )
        original.refresh_from_db()
        self.assertEqual(original.access_token, "xoxp-UORIGINAL")
        self.assertEqual(
            original.provider_metadata["authed_user"]["id"],
            "UORIGINAL",
        )

    @patch("integrations.services.slack_dm_mirror._revoke_remote_token")
    def test_explicit_disconnect_allows_same_workspace_identity_switch(self, _revoke):
        original = _slack_connection(self.user, "UORIGINAL")
        original_grant = SlackDmMirrorGrant.objects.create(
            user=self.user,
            connection=original,
            slack_workspace_id="TMLAI",
            slack_user_id="UORIGINAL",
            consented_at=timezone.now(),
        )
        slack_dm_mirror.revoke_user_grant(self.user)

        connected = external_connectors._store_slack_connection(
            self.user,
            None,
            {
                "access_token": "xoxb-new-installation",
                "team": {"id": "TMLAI", "name": "MLAI"},
                "authed_user": {
                    "id": "UOTHER",
                    "access_token": "xoxp-replacement",
                    "scope": ",".join(SCOPES),
                },
            },
            expected_generation=1,
        )

        self.assertNotEqual(connected.pk, original.pk)
        self.assertEqual(connected.external_account_id, "TMLAI")
        self.assertEqual(connected.provider_metadata["authed_user"]["id"], "UOTHER")
        original.refresh_from_db()
        original_grant.refresh_from_db()
        self.assertEqual(
            original.status,
            ExternalServiceConnectionStatus.DISCONNECTED,
        )
        self.assertTrue(original.external_account_id.startswith("TMLAI:revoked:"))
        self.assertEqual(original_grant.status, SlackDmMirrorGrantStatus.REVOKED)

        CommunityChatDevice.objects.create(
            user=self.user,
            public_key="2" * 64,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        new_grant = activate_connection(connected)
        self.assertNotEqual(new_grant.pk, original_grant.pk)
        self.assertEqual(new_grant.slack_workspace_id, "TMLAI")
        self.assertEqual(new_grant.slack_user_id, "UOTHER")
        self.assertEqual(new_grant.status, SlackDmMirrorGrantStatus.ACTIVE)

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_user_directory_filters_non_humans_external_users_and_emails(
        self, web_client
    ):
        connection = _slack_connection(self.user, "UOWNER")
        SlackDmMirrorGrant.objects.create(
            user=self.user,
            connection=connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UOWNER",
            consented_at=timezone.now(),
        )
        web_client.return_value.users_list.return_value = {
            "members": [
                {
                    "id": "UHUMAN",
                    "team_id": "TMLAI",
                    "name": "alice",
                    "profile": {
                        "display_name": "Alice",
                        "email": "alice@example.com",
                        "image_192": "https://example.com/alice.png",
                    },
                },
                {"id": "UBOT", "team_id": "TMLAI", "is_bot": True},
                {"id": "UEXTERNAL", "team_id": "TOTHER", "name": "external"},
                {"id": "UOWNER", "team_id": "TMLAI", "name": "owner"},
            ],
            "response_metadata": {"next_cursor": ""},
        }

        response = self.client.get(
            reverse("community_chat_slack_users"),
            {"q": "ali", "limit": 10},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["users"],
            [
                {
                    "slack_user_id": "UHUMAN",
                    "display_name": "Alice",
                    "avatar_url": "https://example.com/alice.png",
                }
            ],
        )
        self.assertNotIn("email", response.data["users"][0])

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_user_directory_rejects_non_object_cursor(self, web_client):
        connection = _slack_connection(self.user, "UOWNER")
        SlackDmMirrorGrant.objects.create(
            user=self.user,
            connection=connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UOWNER",
            consented_at=timezone.now(),
        )

        response = self.client.get(
            reverse("community_chat_slack_users"),
            {"cursor": "W10"},  # URL-safe base64 for []
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["slack"], "Slack directory cursor is invalid.")
        web_client.assert_not_called()

    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.provision_private_conversation"
    )
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_start_dm_uses_authenticated_verified_key_and_returns_shadow_participants(
        self,
        web_client,
        provision,
    ):
        CommunityChatDevice.objects.create(
            user=self.user,
            public_key="1" * 64,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        connection = _slack_connection(self.user, "UOWNER")
        SlackDmMirrorGrant.objects.create(
            user=self.user,
            connection=connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UOWNER",
            consented_at=timezone.now(),
        )
        client = web_client.return_value
        client.users_info.side_effect = lambda *, user: {
            "user": {
                "id": user,
                "team_id": "TMLAI",
                "name": user.lower(),
                "profile": {"display_name": "Owner" if user == "UOWNER" else "Alice"},
            }
        }
        client.conversations_open.return_value = {"channel": {"id": "DNEW"}}
        channel_id = str(uuid.uuid4())
        provision.side_effect = lambda pubkeys, **_: {
            "channel_id": channel_id,
            "participant_pubkeys": pubkeys,
        }

        response = self.client.post(
            reverse("community_chat_slack_dms"),
            {
                "slack_user_ids": ["UALICE"],
                # This untrusted value must be ignored.
                "owner_pubkey": "f" * 64,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["mlai_channel_id"], channel_id)
        self.assertTrue(response.data["identity_repaired"])
        self.assertIn("1" * 64, response.data["owner_device_pubkeys"])
        counterpart = next(
            item for item in response.data["participants"] if not item["is_owner"]
        )
        self.assertEqual(counterpart["slack_user_id"], "UALICE")
        self.assertEqual(len(counterpart["buzz_pubkey"]), 64)
        self.assertNotEqual(counterpart["buzz_pubkey"], "f" * 64)
        client.conversations_open.assert_called_once_with(
            users="UALICE",
            return_im=True,
        )

    def test_legacy_full_history_action_uses_bounded_window_without_sync_io(self):
        connection = _slack_connection(self.user, "UOWNER")
        grant = SlackDmMirrorGrant.objects.create(
            user=self.user,
            connection=connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UOWNER",
            consented_at=timezone.now(),
        )
        conversation = SlackDmMirrorConversation.objects.create(
            grant=grant,
            slack_workspace_id="TMLAI",
            slack_conversation_id="DONE",
            participant_slack_ids=["UOWNER", "UALICE"],
            mlai_channel_id=uuid.uuid4(),
            status=SlackDmMirrorConversationStatus.LIVE,
            history_backfilled_at=timezone.now(),
            oldest_synced_ts="1787900000.000100",
        )

        response = self.client.patch(
            self.url,
            {"action": "backfill_all"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        grant.refresh_from_db()
        conversation.refresh_from_db()
        self.assertEqual(grant.history_days, 30)
        self.assertIsNone(conversation.history_backfilled_at)
        self.assertEqual(conversation.oldest_synced_ts, "")
        self.assertFalse(response.data["privacy"]["full_history"])
        self.assertTrue(response.data["privacy"]["history_is_bounded"])


class SlackDmMirrorOwnerTests(APITestCase):
    def setUp(self):
        self.first = get_user_model().objects.create_user(email="first@example.com")
        self.second = get_user_model().objects.create_user(email="second@example.com")
        for user, public_key in ((self.first, "1" * 64), (self.second, "2" * 64)):
            CommunityChatDevice.objects.create(
                user=user,
                public_key=public_key,
                status=DeviceBindingStatus.VERIFIED,
                verified_at=timezone.now(),
            )
        self.first_connection = _slack_connection(self.first, "UONE")
        self.second_connection = _slack_connection(self.second, "UTWO")

    @staticmethod
    def _message_deliveries(conversation):
        return conversation.deliveries.exclude(
            source_message_id__startswith=slack_dm_mirror.REGISTRATION_STATE_PREFIX,
        )

    def _live_conversation(
        self,
        *,
        participant_slack_ids=None,
        participant_identity_map=None,
    ):
        participant_slack_ids = participant_slack_ids or ["UONE", "UTWO"]
        participant_identity_map = participant_identity_map or {
            "UONE": "1" * 64,
            "UTWO": "3" * 64,
        }
        grant = SlackDmMirrorGrant.objects.create(
            user=self.first,
            connection=self.first_connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            consented_at=timezone.now(),
        )
        conversation = SlackDmMirrorConversation.objects.create(
            grant=grant,
            slack_workspace_id="TMLAI",
            slack_conversation_id="DONE",
            participant_slack_ids=participant_slack_ids,
            participant_buzz_pubkeys=sorted(set(participant_identity_map.values())),
            participant_identity_map=participant_identity_map,
            participant_profiles={
                slack_user_id: {"display_name": slack_user_id}
                for slack_user_id in participant_slack_ids
            },
            mlai_channel_id=uuid.uuid4(),
            status=SlackDmMirrorConversationStatus.LIVE,
        )
        return grant, conversation

    def test_singular_status_never_hides_an_older_active_grant(self):
        active = SlackDmMirrorGrant.objects.create(
            user=self.first,
            connection=self.first_connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            consented_at=timezone.now(),
        )
        newer_connection = ExternalServiceConnection.objects.create(
            user=self.first,
            provider=ExternalServiceProvider.SLACK,
            status=ExternalServiceConnectionStatus.DISCONNECTED,
            external_account_id="TNEWER",
            account_label="Disconnected workspace",
        )
        SlackDmMirrorGrant.objects.create(
            user=self.first,
            connection=newer_connection,
            slack_workspace_id="TNEWER",
            slack_user_id="UNEWER",
            status=SlackDmMirrorGrantStatus.REVOKED,
            consented_at=timezone.now(),
            revoked_at=timezone.now(),
        )

        payload = status_payload(self.first)

        self.assertTrue(payload["connected"])
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["status"], SlackDmMirrorGrantStatus.ACTIVE)
        self.assertEqual(payload["workspace_id"], active.slack_workspace_id)
        self.assertEqual(active_grant_for_user(self.first).pk, active.pk)

    def _conversation_ready_for_provision(self, slack_conversation_id="DRACE"):
        grant = SlackDmMirrorGrant.objects.create(
            user=self.first,
            connection=self.first_connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            consented_at=timezone.now(),
        )
        CommunityBridgeIdentityLink.objects.create(
            user=self.first,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            buzz_pubkey="1" * 64,
            display_name="First",
            verification_method=(
                CommunityBridgeIdentityVerificationMethod.ACCOUNT_CHALLENGE
            ),
            verification_reference="provision-race-test",
            verified_at=timezone.now(),
        )
        conversation = SlackDmMirrorConversation.objects.create(
            grant=grant,
            slack_workspace_id="TMLAI",
            slack_conversation_id=slack_conversation_id,
            participant_slack_ids=["UONE", "UTWO"],
        )
        return grant, conversation

    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.unregister_private_conversation"
    )
    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.provision_private_conversation"
    )
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_revocation_during_provision_unregisters_the_late_channel(
        self,
        web_client,
        provision_private_conversation,
        unregister_private_conversation,
    ):
        grant, conversation = self._conversation_ready_for_provision()
        late_channel_id = uuid.uuid4()

        def revoke_before_adapter_returns(pubkeys, **kwargs):
            if provision_private_conversation.call_count > 1:
                return {
                    "channel_id": str(late_channel_id),
                    "participant_pubkeys": pubkeys,
                }
            self.assertEqual(
                SlackDmMirrorConversation.objects.get(pk=conversation.pk).status,
                SlackDmMirrorConversationStatus.PROVISIONING,
            )
            slack_dm_mirror.revoke_grant(grant)
            return {
                "channel_id": str(late_channel_id),
                "participant_pubkeys": pubkeys,
            }

        provision_private_conversation.side_effect = revoke_before_adapter_returns

        with self.assertRaises(slack_dm_mirror.SlackDmMirrorAuthorizationError):
            slack_dm_mirror._provision_owner_conversation(conversation)

        grant.refresh_from_db()
        conversation.refresh_from_db()
        self.first_connection.refresh_from_db()
        self.assertEqual(grant.status, SlackDmMirrorGrantStatus.REVOKED)
        self.assertIsNotNone(grant.revoked_at)
        self.assertEqual(conversation.status, SlackDmMirrorConversationStatus.PAUSED)
        self.assertIsNone(conversation.mlai_channel_id)
        self.assertEqual(
            self.first_connection.status,
            ExternalServiceConnectionStatus.DISCONNECTED,
        )
        self.assertEqual(unregister_private_conversation.call_count, 1)
        unregister_private_conversation.assert_called_with(str(late_channel_id))
        web_client.return_value.auth_revoke.assert_called_once_with()

    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.unregister_private_conversation"
    )
    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.provision_private_conversation"
    )
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_late_registration_cleanup_failure_remains_retryable(
        self,
        web_client,
        provision_private_conversation,
        unregister_private_conversation,
    ):
        grant, conversation = self._conversation_ready_for_provision()
        late_channel_id = uuid.uuid4()
        client = web_client.return_value
        client.conversations_list.return_value = {
            "channels": [{"id": "DRACE", "user": "UTWO"}],
            "response_metadata": {"next_cursor": ""},
        }
        client.users_info.side_effect = lambda *, user: {
            "user": {"id": user, "team_id": "TMLAI", "name": user.lower()}
        }

        def revoke_before_adapter_returns(pubkeys, **kwargs):
            if provision_private_conversation.call_count > 1:
                return {
                    "channel_id": str(late_channel_id),
                    "participant_pubkeys": pubkeys,
                }
            self.assertEqual(
                SlackDmMirrorConversation.objects.get(pk=conversation.pk).status,
                SlackDmMirrorConversationStatus.PROVISIONING,
            )
            slack_dm_mirror.revoke_grant(grant)
            return {
                "channel_id": str(late_channel_id),
                "participant_pubkeys": pubkeys,
            }

        provision_private_conversation.side_effect = revoke_before_adapter_returns
        unregister_private_conversation.side_effect = RuntimeError(
            "adapter unavailable"
        )

        self.assertEqual(slack_dm_mirror.discover_conversations(grant), 0)

        grant.refresh_from_db()
        conversation.refresh_from_db()
        self.assertEqual(grant.status, SlackDmMirrorGrantStatus.REVOKED)
        self.assertEqual(
            grant.last_error,
            slack_dm_mirror.PRIVATE_REGISTRATION_REVOCATION_PENDING,
        )
        self.assertEqual(conversation.status, SlackDmMirrorConversationStatus.PAUSED)
        self.assertIsNone(conversation.mlai_channel_id)
        registration = conversation.deliveries.get(
            source_message_id__startswith=slack_dm_mirror.REGISTRATION_STATE_PREFIX,
        )
        self.assertEqual(
            registration.metadata["channel_id"],
            str(late_channel_id),
        )
        self.assertEqual(
            registration.metadata["registration_state"],
            slack_dm_mirror.REGISTRATION_STATE_CLEANUP_PENDING,
        )

        unregister_private_conversation.side_effect = None
        SlackDmMirrorDelivery.objects.filter(
            conversation=conversation,
            source_message_id__startswith=slack_dm_mirror.REGISTRATION_STATE_PREFIX,
            status=CommunityBridgeDeliveryStatus.PENDING,
        ).update(available_at=timezone.now() - timedelta(seconds=1))
        slack_dm_mirror.revoke_grant(grant)

        grant.refresh_from_db()
        self.assertEqual(grant.last_error, "")
        self.assertEqual(unregister_private_conversation.call_count, 2)
        unregister_private_conversation.assert_called_with(str(late_channel_id))
        web_client.return_value.auth_revoke.assert_called_once_with()

    def test_reauthorization_reports_legacy_full_history_as_bounded(self):
        grant = SlackDmMirrorGrant.objects.create(
            user=self.first,
            connection=self.first_connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            consented_at=timezone.now(),
            history_days=0,
        )

        activate_connection(self.first_connection)

        grant.refresh_from_db()
        payload = status_payload(self.first)
        self.assertEqual(grant.history_days, 30)
        self.assertEqual(payload["history_days"], 30)
        self.assertTrue(payload["privacy"]["history_is_bounded"])

    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.provision_private_conversation"
    )
    @patch("integrations.services.slack_dm_mirror.BuzzBridgeClient.deliver_private")
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_one_link_provisions_owner_only_mirror_and_backfills_in_timestamp_order(
        self,
        web_client,
        deliver_private,
        provision,
    ):
        first_client = MagicMock()
        first_client.conversations_list.return_value = {
            "channels": [{"id": "DONE", "user": "UTWO"}],
            "response_metadata": {"next_cursor": ""},
        }
        first_client.users_info.side_effect = lambda *, user: {
            "user": {
                "id": user,
                "name": user.lower(),
                "profile": {
                    "display_name": (
                        "First person" if user == "UONE" else "Second person"
                    ),
                    "image_192": f"https://avatars.slack-edge.com/{user}.png",
                },
            }
        }
        first_client.conversations_history.return_value = {
            "messages": [
                {"ts": "1787900001.000200", "user": "UTWO", "text": "private second"},
                {"ts": "1787900000.000100", "user": "UONE", "text": "private first"},
            ],
            "response_metadata": {"next_cursor": ""},
        }
        web_client.return_value = first_client
        channel_id = str(uuid.uuid4())
        provision.side_effect = lambda pubkeys, **_: {
            "channel_id": channel_id,
            "participant_pubkeys": pubkeys,
        }

        grant = activate_connection(self.first_connection)
        discover_conversations(grant)
        self.assertEqual(process_due_history_backfills(), 1)
        conversation = SlackDmMirrorConversation.objects.get(
            slack_conversation_id="DONE"
        )
        self.assertEqual(conversation.status, SlackDmMirrorConversationStatus.LIVE)
        self.assertIsNotNone(conversation.mlai_channel_id)
        self.assertEqual(conversation.grant.slack_user_id, "UONE")
        identity_map = conversation.participant_identity_map
        self.assertEqual(identity_map["UONE"], "1" * 64)
        self.assertEqual(len(identity_map["UTWO"]), 64)
        self.assertNotEqual(identity_map["UTWO"], "2" * 64)
        self.assertEqual(
            conversation.participant_profiles["UTWO"]["display_name"],
            "Second person",
        )
        queued = list(self._message_deliveries(conversation).order_by("id"))
        self.assertEqual(
            [item.encrypted_text for item in queued],
            ["private first", "private second"],
        )
        provision.assert_called_once_with(
            conversation.participant_buzz_pubkeys,
            conversation_name="Second person",
            callback_author_pubkeys=["1" * 64],
        )

        self.assertEqual(process_ready_deliveries(limit=10), 2)
        self.assertEqual(process_ready_deliveries(limit=10), 0)
        delivered_times = [
            call.kwargs["created_at"] for call in deliver_private.call_args_list
        ]
        self.assertEqual(delivered_times, [1787900000, 1787900001])
        self.assertEqual(
            [
                call.kwargs["source_author_display_name"]
                for call in deliver_private.call_args_list
            ],
            ["First person", "Second person"],
        )
        self.assertEqual(
            deliver_private.call_args_list[1].kwargs["source_author_avatar_url"],
            "https://avatars.slack-edge.com/UTWO.png",
        )
        self.assertFalse(
            SlackDmMirrorDelivery.objects.filter(
                conversation=conversation,
                encrypted_text__gt="",
            ).exists()
        )

    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.provision_private_conversation"
    )
    @patch("integrations.services.slack_dm_mirror.BuzzBridgeClient.deliver_private")
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_discovery_resumes_durable_cursor_after_page_failure(
        self,
        web_client,
        _deliver_private,
        provision,
    ):
        client = web_client.return_value
        client.conversations_list.side_effect = [
            {
                "channels": [{"id": "DONE", "user": "UTWO"}],
                "response_metadata": {"next_cursor": "page-2"},
            },
            RuntimeError("Slack rate limited page 2"),
            {
                "channels": [{"id": "DTHREE", "user": "UTHREE"}],
                "response_metadata": {"next_cursor": ""},
            },
        ]
        client.users_info.side_effect = lambda *, user: {
            "user": {
                "id": user,
                "team_id": "TMLAI",
                "name": user.lower(),
                "profile": {"display_name": user},
            }
        }
        provision.side_effect = lambda pubkeys, **_: {
            "channel_id": str(uuid.uuid4()),
            "participant_pubkeys": pubkeys,
        }
        grant = activate_connection(self.first_connection)

        self.assertEqual(discover_conversations(grant), 1)
        self.first_connection.refresh_from_db()
        checkpoint = self.first_connection.sync_cursor[
            slack_dm_mirror.DISCOVERY_CHECKPOINT_KEY
        ]
        self.assertEqual(checkpoint["cursor"], "page-2")
        self.assertEqual(checkpoint["seen_channel_ids"], ["DONE"])
        grant.refresh_from_db()
        self.assertIsNone(grant.last_discovery_at)

        with self.assertRaisesRegex(RuntimeError, "rate limited page 2"):
            discover_conversations(grant)
        self.first_connection.refresh_from_db()
        self.assertEqual(
            self.first_connection.sync_cursor[
                slack_dm_mirror.DISCOVERY_CHECKPOINT_KEY
            ]["cursor"],
            "page-2",
        )

        self.assertEqual(discover_conversations(grant), 1)
        self.assertCountEqual(
            SlackDmMirrorConversation.objects.filter(grant=grant).values_list(
                "slack_conversation_id",
                flat=True,
            ),
            ["DONE", "DTHREE"],
        )
        self.assertEqual(
            [
                call.kwargs["cursor"]
                for call in client.conversations_list.call_args_list
            ],
            ["", "page-2", "page-2"],
        )
        self.first_connection.refresh_from_db()
        grant.refresh_from_db()
        self.assertNotIn(
            slack_dm_mirror.DISCOVERY_CHECKPOINT_KEY,
            self.first_connection.sync_cursor,
        )
        self.assertIsNotNone(grant.last_discovery_at)

    def test_discovery_checkpoint_does_not_truncate_large_seen_set(self):
        grant = SlackDmMirrorGrant.objects.create(
            user=self.first,
            connection=self.first_connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            consented_at=timezone.now(),
        )
        authority = slack_dm_mirror._capture_slack_grant_api_authority(grant)
        seen = {f"D{index:06d}" for index in range(5001)}
        started_at = timezone.now()

        slack_dm_mirror._save_discovery_checkpoint(
            authority,
            cursor="next",
            seen_channel_ids=seen,
            failures=[],
            started_at=started_at,
        )
        (
            cursor,
            restored,
            failures,
            restored_started_at,
        ) = slack_dm_mirror._load_discovery_checkpoint(authority)

        self.assertEqual(cursor, "next")
        self.assertEqual(restored, seen)
        self.assertEqual(failures, [])
        self.assertEqual(restored_started_at, started_at)

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_discovery_finalization_preserves_new_conversation_and_staged_event(
        self,
        web_client,
    ):
        grant, existing = self._live_conversation()
        existing.status = SlackDmMirrorConversationStatus.PAUSED
        existing.save(update_fields=("status", "updated_at"))
        pending_event = {}

        def finish_page(**_kwargs):
            pending_event.update(
                {
                    "key": "late-event",
                    "channel_id": "DNEW",
                    "ciphertext": "encrypted",
                    "staged_at": timezone.now().isoformat(),
                }
            )
            SlackDmMirrorConversation.objects.create(
                grant=grant,
                slack_workspace_id="TMLAI",
                slack_conversation_id="DNEW",
                participant_slack_ids=["UONE", "UTHREE"],
                mlai_channel_id=uuid.uuid4(),
                status=SlackDmMirrorConversationStatus.LIVE,
            )
            ExternalServiceConnection.objects.filter(
                pk=self.first_connection.pk
            ).update(
                sync_cursor={
                    slack_dm_mirror.PENDING_EVENT_CHECKPOINT_KEY: [pending_event]
                }
            )
            return {
                "channels": [],
                "response_metadata": {"next_cursor": ""},
            }

        web_client.return_value.conversations_list.side_effect = finish_page

        self.assertEqual(discover_conversations(grant), 0)

        late = SlackDmMirrorConversation.objects.get(
            grant=grant,
            slack_conversation_id="DNEW",
        )
        self.assertEqual(late.status, SlackDmMirrorConversationStatus.LIVE)
        self.first_connection.refresh_from_db()
        grant.refresh_from_db()
        self.assertEqual(
            self.first_connection.sync_cursor[
                slack_dm_mirror.PENDING_EVENT_CHECKPOINT_KEY
            ],
            [pending_event],
        )
        self.assertIsNone(grant.last_discovery_at)

    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.provision_private_conversation"
    )
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_one_link_discovers_and_backfills_group_dms(
        self,
        web_client,
        provision,
    ):
        client = MagicMock()
        client.conversations_list.return_value = {
            "channels": [
                {
                    "id": "GMPIM",
                    "is_mpim": True,
                    # Slack can truncate this embedded list. Discovery must use
                    # the authoritative paginated members endpoint instead.
                    "members": ["UONE"],
                }
            ],
            "response_metadata": {"next_cursor": ""},
        }
        client.conversations_members.side_effect = [
            {
                "members": ["UONE", "UTWO"],
                "response_metadata": {"next_cursor": "members-page-2"},
            },
            {
                "members": ["UTHREE"],
                "response_metadata": {"next_cursor": ""},
            },
        ]
        client.users_info.side_effect = lambda *, user: {
            "user": {
                "id": user,
                "name": user.lower(),
                "profile": {
                    "display_name": {
                        "UONE": "First",
                        "UTWO": "Second",
                        "UTHREE": "Third",
                    }[user]
                },
            }
        }
        client.conversations_history.return_value = {
            "messages": [
                {"ts": "1787900300.000100", "user": "UTHREE", "text": "group history"},
            ],
            "response_metadata": {"next_cursor": ""},
        }
        web_client.return_value = client
        provision.side_effect = lambda pubkeys, **_: {
            "channel_id": str(uuid.uuid4()),
            "participant_pubkeys": pubkeys,
        }

        grant = activate_connection(self.first_connection)
        discover_conversations(grant)
        self.assertEqual(process_due_history_backfills(), 1)

        conversation = SlackDmMirrorConversation.objects.get(
            slack_conversation_id="GMPIM"
        )
        self.assertEqual(
            conversation.participant_slack_ids,
            ["UONE", "UTHREE", "UTWO"],
        )
        self.assertEqual(len(conversation.participant_buzz_pubkeys), 3)
        self.assertEqual(conversation.participant_identity_map["UONE"], "1" * 64)
        self.assertNotEqual(conversation.participant_identity_map["UTWO"], "2" * 64)
        self.assertIsNotNone(conversation.history_backfilled_at)
        self.assertEqual(self._message_deliveries(conversation).count(), 1)
        client.conversations_list.assert_called_once_with(
            types="im,mpim",
            exclude_archived=True,
            limit=200,
            cursor="",
        )
        self.assertEqual(
            [call.kwargs for call in client.conversations_members.call_args_list],
            [
                {"channel": "GMPIM", "limit": 200, "cursor": ""},
                {
                    "channel": "GMPIM",
                    "limit": 200,
                    "cursor": "members-page-2",
                },
            ],
        )
        provision.assert_called_once_with(
            conversation.participant_buzz_pubkeys,
            conversation_name="Third, Second",
            callback_author_pubkeys=["1" * 64],
        )

    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.provision_private_conversation"
    )
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_existing_live_mirror_gets_one_automatic_idempotent_backfill(
        self,
        web_client,
        provision,
    ):
        client = MagicMock()
        client.conversations_list.return_value = {
            "channels": [{"id": "DONE", "user": "UTWO"}],
            "response_metadata": {"next_cursor": ""},
        }
        client.users_info.side_effect = lambda *, user: {
            "user": {"id": user, "name": user.lower(), "profile": {}}
        }
        client.conversations_history.return_value = {
            "messages": [
                {"ts": "1787900400.000100", "user": "UTWO", "text": "recovered"},
            ],
            "response_metadata": {"next_cursor": ""},
        }
        web_client.return_value = client
        channel_id = str(uuid.uuid4())
        provision.return_value = {
            "channel_id": channel_id,
            "participant_pubkeys": [],
        }
        provision.side_effect = lambda pubkeys, **_: {
            "channel_id": channel_id,
            "participant_pubkeys": pubkeys,
        }

        grant = activate_connection(self.first_connection)
        discover_conversations(grant)
        self.assertEqual(process_due_history_backfills(), 1)
        conversation = SlackDmMirrorConversation.objects.get(
            slack_conversation_id="DONE"
        )
        first_marker = conversation.history_backfilled_at
        self.assertIsNotNone(first_marker)
        self.assertEqual(self._message_deliveries(conversation).count(), 1)

        backfill_grant(conversation.grant)
        conversation.refresh_from_db()
        self.assertIsNone(conversation.history_backfilled_at)
        self.assertEqual(process_due_history_backfills(), 1)
        conversation.refresh_from_db()

        self.assertGreaterEqual(conversation.history_backfilled_at, first_marker)
        self.assertEqual(self._message_deliveries(conversation).count(), 1)

    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.provision_private_conversation"
    )
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_direct_only_grant_backfills_once_and_requests_group_reauthorization(
        self,
        web_client,
        provision,
    ):
        self.first_connection.scopes = DIRECT_SCOPES
        self.first_connection.save(update_fields=("scopes", "updated_at"))
        client = MagicMock()
        client.conversations_list.return_value = {
            "channels": [{"id": "DONE", "user": "UTWO"}],
            "response_metadata": {"next_cursor": ""},
        }
        client.users_info.side_effect = lambda *, user: {
            "user": {"id": user, "name": user.lower(), "profile": {}}
        }
        client.conversations_history.return_value = {
            "messages": [],
            "response_metadata": {"next_cursor": ""},
        }
        web_client.return_value = client
        provision.side_effect = lambda pubkeys, **_: {
            "channel_id": str(uuid.uuid4()),
            "participant_pubkeys": pubkeys,
        }

        grant = activate_connection(self.first_connection)
        discover_conversations(grant)
        self.assertEqual(process_due_history_backfills(), 1)
        activate_connection(self.first_connection)

        conversation = SlackDmMirrorConversation.objects.get(
            slack_conversation_id="DONE"
        )
        self.assertIsNotNone(conversation.history_backfilled_at)
        self.assertEqual(client.conversations_history.call_count, 1)
        client.conversations_list.assert_called_with(
            types="im",
            exclude_archived=True,
            limit=200,
            cursor="",
        )
        payload = status_payload(self.first)
        self.assertTrue(payload["enabled"])
        self.assertTrue(payload["needs_reauthorization"])
        self.assertFalse(payload["group_dms_enabled"])
        self.assertEqual(payload["backfill"]["complete"], 1)
        self.assertEqual(payload["backfill"]["pending"], 0)
        self.assertEqual(payload["backfill"]["queued_messages"], 0)

    def test_private_channel_event_is_not_misclassified_as_a_group_dm(self):
        result = ingest_slack_dm_event(
            {
                "team_id": "TMLAI",
                "authorizations": [{"user_id": "UONE"}],
                "event": {
                    "channel": "GPRIVATE",
                    "channel_type": "group",
                    "ts": "1787900500.000100",
                    "user": "UTWO",
                    "text": "private channel message",
                },
            }
        )

        self.assertIsNone(result)

    def test_live_slack_event_routes_only_to_explicitly_authorized_owner_mirror(
        self,
    ):
        first_grant = SlackDmMirrorGrant.objects.create(
            user=self.first,
            connection=self.first_connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            consented_at=timezone.now(),
        )
        second_grant = SlackDmMirrorGrant.objects.create(
            user=self.second,
            connection=self.second_connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UTWO",
            consented_at=timezone.now(),
        )
        for grant, identity_map in (
            (first_grant, {"UONE": "1" * 64, "UTWO": "3" * 64}),
            (second_grant, {"UONE": "4" * 64, "UTWO": "2" * 64}),
        ):
            SlackDmMirrorConversation.objects.create(
                grant=grant,
                slack_workspace_id="TMLAI",
                slack_conversation_id="DONE",
                participant_slack_ids=["UONE", "UTWO"],
                participant_buzz_pubkeys=sorted(identity_map.values()),
                participant_identity_map=identity_map,
                mlai_channel_id=uuid.uuid4(),
                status=SlackDmMirrorConversationStatus.LIVE,
            )

        result = ingest_slack_dm_event(
            {
                "team_id": "TMLAI",
                "authorizations": [{"user_id": "UONE"}],
                "event": {
                    "channel": "DONE",
                    "ts": "1787900100.000100",
                    "user": "UONE",
                    "text": "visible only in the authorized owner's private copy",
                },
            }
        )

        self.assertEqual(result["status"], "enqueued")
        self.assertEqual(len(result["delivery_ids"]), 1)
        delivery = SlackDmMirrorDelivery.objects.get()
        self.assertEqual(delivery.conversation.grant_id, first_grant.pk)
        self.assertFalse(
            SlackDmMirrorDelivery.objects.filter(
                conversation__grant=second_grant,
            ).exists()
        )

    def test_unknown_dm_event_queues_owner_grants_for_immediate_rediscovery(self):
        grant = SlackDmMirrorGrant.objects.create(
            user=self.first,
            connection=self.first_connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            consented_at=timezone.now(),
            last_discovery_at=timezone.now(),
        )
        SlackDmMirrorGrant.objects.create(
            user=self.second,
            connection=self.second_connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UTWO",
            consented_at=timezone.now(),
            last_discovery_at=timezone.now(),
        )

        result = ingest_slack_dm_event(
            {
                "team_id": "TMLAI",
                "authorizations": [{"user_id": "UONE"}],
                "event": {
                    "channel": "DNEW",
                    "ts": "1787900200.000100",
                    "user": "UTWO",
                    "text": "first message in a newly opened DM",
                },
            }
        )

        self.assertEqual(result["status"], "discovery_queued")
        self.assertEqual(result["staged"], 1)
        grant.refresh_from_db()
        self.assertIsNone(grant.last_discovery_at)
        self.first_connection.refresh_from_db()
        self.assertNotIn(
            "first message in a newly opened DM",
            str(self.first_connection.sync_cursor),
        )
        pending = self.first_connection.sync_cursor[
            slack_dm_mirror.PENDING_EVENT_CHECKPOINT_KEY
        ]
        self.assertEqual(len(pending), 1)
        self.second_connection.refresh_from_db()
        self.assertNotIn(
            slack_dm_mirror.PENDING_EVENT_CHECKPOINT_KEY,
            self.second_connection.sync_cursor,
        )

        conversation = SlackDmMirrorConversation.objects.create(
            grant=grant,
            slack_workspace_id="TMLAI",
            slack_conversation_id="DNEW",
            participant_slack_ids=["UONE", "UTWO"],
            participant_buzz_pubkeys=["1" * 64, "3" * 64],
            participant_identity_map={"UONE": "1" * 64, "UTWO": "3" * 64},
            mlai_channel_id=uuid.uuid4(),
            status=SlackDmMirrorConversationStatus.LIVE,
        )
        authority = slack_dm_mirror._capture_slack_grant_api_authority(grant)
        self.assertEqual(
            slack_dm_mirror._drain_staged_events_for_conversation(
                authority,
                conversation.pk,
            ),
            1,
        )
        delivery = conversation.deliveries.get(
            source_platform=CommunityBridgePlatform.SLACK
        )
        self.assertEqual(delivery.encrypted_text, "first message in a newly opened DM")
        self.first_connection.refresh_from_db()
        self.assertNotIn(
            slack_dm_mirror.PENDING_EVENT_CHECKPOINT_KEY,
            self.first_connection.sync_cursor,
        )

    @patch("integrations.api_views_bridge.ingest_inbound_event")
    @patch(
        "integrations.api_views_bridge.BuzzBridgeClient.validate_callback_signature",
        return_value=True,
    )
    def test_late_callback_for_retired_private_channel_never_falls_through_public(
        self,
        _validate_signature,
        ingest_public,
    ):
        grant = SlackDmMirrorGrant.objects.create(
            user=self.first,
            connection=self.first_connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            consented_at=timezone.now(),
        )
        channel_id = uuid.uuid4()
        conversation = SlackDmMirrorConversation.objects.create(
            grant=grant,
            slack_workspace_id="TMLAI",
            slack_conversation_id="DRETIRED",
            mlai_channel_id=channel_id,
            status=SlackDmMirrorConversationStatus.LIVE,
        )
        SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id=f"{slack_dm_mirror.REGISTRATION_STATE_PREFIX}retired",
            source_author_id="",
            operation=CommunityBridgeDeliveryType.CREATE,
            metadata={"registration_control": True, "channel_id": str(channel_id)},
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            completed_at=timezone.now(),
            available_at=timezone.now(),
        )
        conversation.status = SlackDmMirrorConversationStatus.PAUSED
        conversation.mlai_channel_id = None
        conversation.save(update_fields=("status", "mlai_channel_id", "updated_at"))

        response = self.client.post(
            reverse("community_bridge_buzz_events"),
            {
                "receipt_key": "late-private-callback",
                "source_channel_id": str(channel_id),
                "event_type": "message_create",
                "normalized_event": {
                    "delivery_type": CommunityBridgeDeliveryType.CREATE,
                    "source_message_id": "a" * 64,
                    "source_author_id": "1" * 64,
                    "text": "must remain private",
                },
            },
            format="json",
            HTTP_X_MLAI_BRIDGE_TIMESTAMP="1",
            HTTP_X_MLAI_BRIDGE_SIGNATURE="v1=test",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "ignored")
        ingest_public.assert_not_called()

    @patch("integrations.services.slack_dm_mirror.BuzzBridgeClient.deliver_private")
    def test_official_shape_delete_without_previous_message_resolves_author(
        self,
        deliver_private,
    ):
        self._live_conversation()
        source_ts = "1787900599.000100"
        deliver_private.side_effect = [
            {"message_id": "a" * 64},
            {"message_id": "b" * 64},
        ]
        ingest_slack_dm_event(
            {
                "event_id": "EvOfficialDeleteCreate",
                "team_id": "TMLAI",
                "event": {
                    "channel": "DONE",
                    "ts": source_ts,
                    "user": "UTWO",
                    "text": "delete me",
                },
            }
        )
        self.assertEqual(process_ready_deliveries(limit=1), 1)

        deleted = ingest_slack_dm_event(
            {
                "event_id": "EvOfficialDelete",
                "team_id": "TMLAI",
                "event": {
                    "type": "message",
                    "subtype": "message_deleted",
                    "channel": "DONE",
                    "deleted_ts": source_ts,
                    "event_ts": "1787900600.000100",
                },
            }
        )

        self.assertEqual(deleted["status"], "enqueued")
        self.assertEqual(process_ready_deliveries(limit=1), 1)
        self.assertEqual(
            deliver_private.call_args_list[-1].kwargs["operation"],
            CommunityBridgeDeliveryType.DELETE,
        )
        self.assertEqual(
            deliver_private.call_args_list[-1].kwargs["target_message_id"],
            "a" * 64,
        )
        deletion = SlackDmMirrorDelivery.objects.get(pk=deleted["delivery_ids"][0])
        self.assertEqual(deletion.source_author_id, "UTWO")

    @patch("integrations.services.slack_dm_mirror.BuzzBridgeClient.deliver_private")
    def test_slack_edits_deletes_and_attachment_links_target_original_event(
        self,
        deliver_private,
    ):
        self._live_conversation()
        source_ts = "1787900600.000100"
        deliver_private.side_effect = [
            {"message_id": "a" * 64, "parent_message_id": ""},
            {"message_id": "b" * 64, "parent_message_id": ""},
            {"message_id": "c" * 64, "parent_message_id": ""},
        ]

        result = ingest_slack_dm_event(
            {
                "event_id": "EvCreatePrivate",
                "team_id": "TMLAI",
                "event": {
                    "type": "message",
                    "subtype": "file_share",
                    "channel": "DONE",
                    "ts": source_ts,
                    "user": "UTWO",
                    "text": "a file",
                    "files": [
                        {
                            "title": "private.png",
                            "permalink": "https://mlai.slack.com/files/UTWO/F1",
                        }
                    ],
                    "attachments": [
                        {
                            "title": "Design",
                            "title_link": "https://example.com/design",
                        }
                    ],
                },
            }
        )
        self.assertEqual(result["status"], "enqueued")
        self.assertEqual(process_ready_deliveries(limit=1), 1)
        create_text = deliver_private.call_args.kwargs["text"]
        self.assertIn("https://mlai.slack.com/files/UTWO/F1", create_text)
        self.assertIn("https://example.com/design", create_text)

        result = ingest_slack_dm_event(
            {
                "event_id": "EvEditPrivate",
                "team_id": "TMLAI",
                "event": {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "DONE",
                    "event_ts": "1787900601.000100",
                    "message": {
                        "ts": source_ts,
                        "user": "UTWO",
                        "text": "edited body",
                    },
                },
            }
        )
        self.assertEqual(result["status"], "enqueued")
        self.assertEqual(process_ready_deliveries(limit=1), 1)
        self.assertEqual(deliver_private.call_args.kwargs["operation"], "edit")
        self.assertEqual(
            deliver_private.call_args.kwargs["target_message_id"],
            "a" * 64,
        )

        result = ingest_slack_dm_event(
            {
                "event_id": "EvDeletePrivate",
                "team_id": "TMLAI",
                "event": {
                    "type": "message",
                    "subtype": "message_deleted",
                    "channel": "DONE",
                    "event_ts": "1787900602.000100",
                    "deleted_ts": source_ts,
                    "previous_message": {"ts": source_ts, "user": "UTWO"},
                },
            }
        )
        self.assertEqual(result["status"], "enqueued")
        self.assertEqual(process_ready_deliveries(limit=1), 1)
        self.assertEqual(deliver_private.call_args.kwargs["operation"], "delete")
        self.assertEqual(deliver_private.call_args.kwargs["text"], "")
        self.assertEqual(
            deliver_private.call_args.kwargs["target_message_id"],
            "a" * 64,
        )

    @patch("integrations.services.slack_dm_mirror.BuzzBridgeClient.deliver_private")
    def test_slack_reactions_keep_distinct_actors_and_remove_exact_reaction(
        self,
        deliver_private,
    ):
        _, conversation = self._live_conversation()
        source_ts = "1787900700.000100"
        conversation.deliveries.create(
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id=source_ts,
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            metadata={"destination_message_id": "a" * 64},
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            available_at=timezone.now(),
            completed_at=timezone.now(),
        )
        deliver_private.side_effect = [
            {"message_id": "b" * 64, "parent_message_id": ""},
            {"message_id": "c" * 64, "parent_message_id": ""},
            {"message_id": "d" * 64, "parent_message_id": ""},
        ]
        for event_id, actor in (("EvReactOwner", "UONE"), ("EvReactPeer", "UTWO")):
            result = ingest_slack_dm_event(
                {
                    "event_id": event_id,
                    "team_id": "TMLAI",
                    "event": {
                        "type": "reaction_added",
                        "event_ts": "1787900701.000100",
                        "user": actor,
                        "reaction": "heart",
                        "item": {
                            "type": "message",
                            "channel": "DONE",
                            "ts": source_ts,
                        },
                    },
                }
            )
            self.assertEqual(result["status"], "enqueued")
        reactions = list(
            conversation.deliveries.filter(
                operation=CommunityBridgeDeliveryType.REACTION_ADD
            ).order_by("id")
        )
        self.assertEqual(len(reactions), 2)
        self.assertNotEqual(
            reactions[0].metadata["reaction_object_id"],
            reactions[1].metadata["reaction_object_id"],
        )
        self.assertEqual(process_ready_deliveries(limit=2), 2)
        linked_pubkeys = {
            call.kwargs["linked_pubkey"] for call in deliver_private.call_args_list[:2]
        }
        self.assertEqual(linked_pubkeys, {"1" * 64, "3" * 64})

        result = ingest_slack_dm_event(
            {
                "event_id": "EvUnreactPeer",
                "team_id": "TMLAI",
                "event": {
                    "type": "reaction_removed",
                    "event_ts": "1787900702.000100",
                    "user": "UTWO",
                    "reaction": "heart",
                    "item": {
                        "type": "message",
                        "channel": "DONE",
                        "ts": source_ts,
                    },
                },
            }
        )
        self.assertEqual(result["status"], "enqueued")
        self.assertEqual(process_ready_deliveries(limit=1), 1)
        self.assertEqual(
            deliver_private.call_args.kwargs["operation"],
            "reaction_remove",
        )
        self.assertEqual(deliver_private.call_args.kwargs["text"], "")
        self.assertEqual(
            deliver_private.call_args.kwargs["target_message_id"],
            "c" * 64,
        )

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_history_scans_thread_replies_one_page_per_tick(self, web_client):
        _, conversation = self._live_conversation()
        root_ts = "1787900800.000100"
        reply_ts = "1787900801.000100"
        second_reply_ts = "1787900802.000100"
        web_client.return_value.conversations_history.return_value = {
            "messages": [
                {
                    "ts": root_ts,
                    "user": "UTWO",
                    "text": "root",
                    "reply_count": 1,
                }
            ],
            "has_more": False,
            "response_metadata": {"next_cursor": ""},
        }
        web_client.return_value.conversations_replies.side_effect = [
            {
                "messages": [
                    {"ts": root_ts, "user": "UTWO", "text": "root"},
                    {
                        "ts": reply_ts,
                        "thread_ts": root_ts,
                        "user": "UONE",
                        "text": "reply",
                        "files": [
                            {
                                "title": "reply.txt",
                                "permalink": "https://mlai.slack.com/files/UONE/F2",
                            }
                        ],
                    },
                ],
                "has_more": True,
                "response_metadata": {"next_cursor": "reply-page-2"},
            },
            {
                "messages": [
                    {
                        "ts": second_reply_ts,
                        "thread_ts": root_ts,
                        "user": "UTWO",
                        "text": "second reply",
                        "reactions": [{"name": "eyes", "users": ["UONE"], "count": 1}],
                    }
                ],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            },
        ]

        self.assertEqual(process_due_history_backfills(), 1)
        conversation.refresh_from_db()
        self.assertIsNone(conversation.history_backfilled_at)
        web_client.return_value.conversations_history.assert_called_once()
        web_client.return_value.conversations_replies.assert_not_called()

        self.assertEqual(process_due_history_backfills(), 1)
        conversation.refresh_from_db()
        self.assertIsNone(conversation.history_backfilled_at)
        self.assertEqual(
            web_client.return_value.conversations_replies.call_args_list[0].kwargs,
            {
                "channel": "DONE",
                "ts": root_ts,
                "limit": 200,
                "oldest": web_client.return_value.conversations_history.call_args.kwargs[
                    "oldest"
                ],
                "inclusive": True,
            },
        )

        self.assertEqual(process_due_history_backfills(), 1)
        conversation.refresh_from_db()
        self.assertIsNotNone(conversation.history_backfilled_at)
        self.assertEqual(
            web_client.return_value.conversations_replies.call_args_list[1].kwargs,
            {
                "channel": "DONE",
                "ts": root_ts,
                "limit": 200,
                "cursor": "reply-page-2",
                "oldest": web_client.return_value.conversations_history.call_args.kwargs[
                    "oldest"
                ],
                "inclusive": True,
            },
        )
        web_client.return_value.conversations_replies.assert_called_with(
            channel="DONE",
            ts=root_ts,
            limit=200,
            cursor="reply-page-2",
            oldest=web_client.return_value.conversations_history.call_args.kwargs[
                "oldest"
            ],
            inclusive=True,
        )
        reply = conversation.deliveries.get(
            source_message_id=reply_ts,
            operation=CommunityBridgeDeliveryType.CREATE,
        )
        self.assertEqual(reply.metadata["thread_ts"], root_ts)
        self.assertIn("https://mlai.slack.com/files/UONE/F2", reply.encrypted_text)
        reaction = conversation.deliveries.get(
            operation=CommunityBridgeDeliveryType.REACTION_ADD
        )
        self.assertEqual(reaction.source_author_id, "UONE")
        self.assertEqual(
            reaction.metadata["target_source_message_id"],
            second_reply_ts,
        )
        self.assertFalse(
            conversation.deliveries.filter(
                source_message_id__startswith="history-state:"
            ).exists()
        )

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_history_fetches_parent_before_reversed_replies(
        self,
        web_client,
    ):
        _, conversation = self._live_conversation()
        parent_ts = "1787900800.000100"
        child_ts = "1787900801.000100"
        web_client.return_value.conversations_history.return_value = {
            "messages": [
                {
                    "ts": child_ts,
                    "thread_ts": parent_ts,
                    "user": "UTWO",
                    "text": "recent child whose parent predates the cutoff",
                }
            ],
            "has_more": False,
            "response_metadata": {"next_cursor": ""},
        }
        # Slack reply pages are not trusted to be parent-first.
        web_client.return_value.conversations_replies.return_value = {
            "messages": [
                {
                    "ts": child_ts,
                    "thread_ts": parent_ts,
                    "user": "UTWO",
                    "text": "recent child whose parent predates the cutoff",
                },
                {
                    "ts": parent_ts,
                    "user": "UONE",
                    "text": "old parent",
                },
            ],
            "has_more": False,
            "response_metadata": {"next_cursor": ""},
        }

        self.assertEqual(process_due_history_backfills(), 1)
        self.assertEqual(process_due_history_backfills(), 1)

        queued = list(
            conversation.deliveries.filter(
                source_platform=CommunityBridgePlatform.SLACK,
                operation=CommunityBridgeDeliveryType.CREATE,
                metadata__backfill=True,
            ).order_by("available_at", "id")
        )
        self.assertEqual(
            [delivery.source_message_id for delivery in queued],
            [parent_ts, child_ts],
        )

        delivered_ids = {
            parent_ts: "a" * 64,
            child_ts: "b" * 64,
        }

        def deliver(**kwargs):
            return {"message_id": delivered_ids[kwargs["source_message_id"]]}

        with patch(
            "integrations.services.slack_dm_mirror.BuzzBridgeClient.deliver_private",
            side_effect=deliver,
        ) as deliver_private:
            self.assertEqual(process_ready_deliveries(limit=10), 2)

        self.assertEqual(
            [call.kwargs["source_message_id"] for call in deliver_private.call_args_list],
            [parent_ts, child_ts],
        )
        self.assertEqual(
            deliver_private.call_args_list[1].kwargs["parent_message_id"],
            "a" * 64,
        )

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_bounded_history_detaches_recent_reply_from_out_of_window_parent(
        self,
        web_client,
    ):
        _, conversation = self._live_conversation()
        old_parent_ts = "1700000000.000100"
        recent_child_ts = "1787900801.000100"
        web_client.return_value.conversations_history.return_value = {
            "messages": [
                {
                    "ts": recent_child_ts,
                    "thread_ts": old_parent_ts,
                    "user": "UTWO",
                    "text": "recent bounded reply",
                }
            ],
            "has_more": False,
            "response_metadata": {"next_cursor": ""},
        }

        self.assertEqual(process_due_history_backfills(), 1)

        delivery = conversation.deliveries.get(
            source_message_id=recent_child_ts,
            operation=CommunityBridgeDeliveryType.CREATE,
        )
        self.assertEqual(delivery.metadata["thread_ts"], "")
        self.assertEqual(delivery.metadata["original_thread_ts"], old_parent_ts)
        self.assertTrue(delivery.metadata["thread_parent_outside_history_window"])
        web_client.return_value.conversations_replies.assert_not_called()

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_thread_page_cannot_finish_before_older_main_pages(self, web_client):
        _, conversation = self._live_conversation()
        root_ts = "1787900800.000100"
        reply_ts = "1787900801.000100"
        older_ts = "1787000000.000100"
        web_client.return_value.conversations_history.side_effect = [
            {
                "messages": [
                    {
                        "ts": root_ts,
                        "user": "UTWO",
                        "text": "root",
                        "reply_count": 1,
                    }
                ],
                "has_more": True,
                "response_metadata": {"next_cursor": "older-page"},
            },
            {
                "messages": [
                    {"ts": older_ts, "user": "UONE", "text": "older main page"}
                ],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            },
        ]
        web_client.return_value.conversations_replies.return_value = {
            "messages": [
                {"ts": root_ts, "user": "UTWO", "text": "root"},
                {
                    "ts": reply_ts,
                    "thread_ts": root_ts,
                    "user": "UONE",
                    "text": "reply",
                },
            ],
            "has_more": False,
            "response_metadata": {"next_cursor": ""},
        }

        self.assertEqual(process_due_history_backfills(), 1)
        self.assertEqual(process_due_history_backfills(), 1)
        conversation.refresh_from_db()
        self.assertIsNone(conversation.history_backfilled_at)
        self.assertEqual(
            web_client.return_value.conversations_history.call_count,
            1,
        )

        self.assertEqual(process_due_history_backfills(), 1)
        conversation.refresh_from_db()
        self.assertIsNotNone(conversation.history_backfilled_at)
        self.assertEqual(
            web_client.return_value.conversations_history.call_count,
            2,
        )
        self.assertTrue(
            conversation.deliveries.filter(source_message_id=older_ts).exists()
        )

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_history_uses_one_fixed_cutoff_across_pages(self, web_client):
        _, conversation = self._live_conversation()
        web_client.return_value.conversations_history.side_effect = [
            {
                "messages": [
                    {"ts": "1787901000.000100", "user": "UTWO", "text": "new"}
                ],
                "has_more": True,
                "response_metadata": {"next_cursor": "next"},
            },
            {
                "messages": [],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            },
        ]

        with patch.object(slack_dm_mirror.time, "time", return_value=2_000_000):
            self.assertEqual(process_due_history_backfills(), 1)
            self.assertEqual(process_due_history_backfills(), 1)

        expected_oldest = str(
            max(0, 2_000_000 - conversation.grant.history_days * 86_400)
        )
        self.assertEqual(
            [
                call.kwargs["oldest"]
                for call in web_client.return_value.conversations_history.call_args_list
            ],
            [expected_oldest, expected_oldest],
        )

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_old_bounded_response_cannot_complete_restarted_bounded_scan(
        self,
        web_client,
    ):
        grant, conversation = self._live_conversation()

        def stale_response(**_kwargs):
            backfill_grant(grant, full_history=True)
            return {
                "messages": [
                    {
                        "ts": "1787901000.000100",
                        "user": "UTWO",
                        "text": "stale bounded body",
                    }
                ],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            }

        web_client.return_value.conversations_history.side_effect = stale_response

        self.assertEqual(process_due_history_backfills(), 0)

        grant.refresh_from_db()
        conversation.refresh_from_db()
        self.assertEqual(grant.history_days, 30)
        self.assertIsNone(conversation.history_backfilled_at)
        self.assertFalse(
            conversation.deliveries.filter(
                source_message_id="1787901000.000100"
            ).exists()
        )

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_same_identity_reauthorization_restarts_partial_history_epoch(
        self,
        web_client,
    ):
        grant, conversation = self._live_conversation()
        CommunityBridgeIdentityLink.objects.create(
            user=self.first,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            buzz_pubkey="1" * 64,
            display_name="First",
            verification_method=(
                CommunityBridgeIdentityVerificationMethod.ACCOUNT_CHALLENGE
            ),
            verification_reference="reauth-history-test",
            verified_at=timezone.now(),
        )
        web_client.return_value.conversations_history.side_effect = [
            {
                "messages": [
                    {
                        "ts": "1787901300.000100",
                        "user": "UTWO",
                        "text": "first consent page",
                    }
                ],
                "has_more": True,
                "response_metadata": {"next_cursor": "older"},
            },
            {
                "messages": [
                    {
                        "ts": "1787901300.000100",
                        "user": "UTWO",
                        "text": "re-read under renewed consent",
                    }
                ],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            },
        ]

        self.assertEqual(process_due_history_backfills(), 1)
        conversation.refresh_from_db()
        self.assertTrue(conversation.oldest_synced_ts)
        self.assertTrue(
            conversation.deliveries.filter(
                source_message_id=slack_dm_mirror.HISTORY_MAIN_STATE_ID
            ).exists()
        )

        activate_connection(self.first_connection)

        conversation.refresh_from_db()
        self.assertEqual(conversation.oldest_synced_ts, "")
        self.assertFalse(
            conversation.deliveries.filter(
                source_message_id__startswith=slack_dm_mirror.HISTORY_STATE_PREFIX
            ).exists()
        )
        self.assertEqual(process_due_history_backfills(), 1)
        conversation.refresh_from_db()
        self.assertIsNotNone(conversation.history_backfilled_at)
        refreshed = conversation.deliveries.get(
            source_message_id="1787901300.000100",
            operation=CommunityBridgeDeliveryType.CREATE,
        )
        self.assertEqual(refreshed.encrypted_text, "re-read under renewed consent")

    @patch("integrations.services.slack_dm_mirror.BuzzBridgeClient.deliver_private")
    def test_transient_parent_failure_does_not_dead_letter_child(
        self,
        deliver_private,
    ):
        _, conversation = self._live_conversation()
        parent_ts = "1787901100.000100"
        child_ts = "1787901101.000100"
        parent = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id=parent_ts,
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="parent",
            metadata={"backfill": True},
            attempts=4,
            available_at=timezone.now() - timedelta(seconds=2),
        )
        child = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id=child_ts,
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="child",
            metadata={"backfill": True, "thread_ts": parent_ts},
            available_at=timezone.now() - timedelta(seconds=1),
        )
        deliver_private.side_effect = [
            RuntimeError("MLAI Chat adapter returned HTTP 502"),
            {"message_id": "a" * 64},
            {"message_id": "b" * 64},
        ]

        self.assertEqual(process_ready_deliveries(limit=1), 0)
        parent.refresh_from_db()
        self.assertEqual(parent.status, CommunityBridgeDeliveryStatus.PENDING)
        self.assertEqual(parent.attempts, 5)

        self.assertEqual(process_ready_deliveries(limit=1), 0)
        child.refresh_from_db()
        self.assertEqual(child.status, CommunityBridgeDeliveryStatus.PENDING)
        self.assertEqual(child.attempts, 0)
        self.assertEqual(child.encrypted_text, "child")

        SlackDmMirrorDelivery.objects.filter(pk=parent.pk).update(
            available_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(process_ready_deliveries(limit=1), 1)
        SlackDmMirrorDelivery.objects.filter(pk=child.pk).update(
            available_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(process_ready_deliveries(limit=1), 1)

        parent.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(parent.status, CommunityBridgeDeliveryStatus.COMPLETED)
        self.assertEqual(child.status, CommunityBridgeDeliveryStatus.COMPLETED)
        self.assertEqual(
            deliver_private.call_args_list[-1].kwargs["parent_message_id"],
            "a" * 64,
        )

    @patch("integrations.services.slack_dm_mirror.BuzzBridgeClient.deliver_private")
    def test_live_reply_waits_for_parent_callback_that_arrives_later(
        self,
        deliver_private,
    ):
        _, conversation = self._live_conversation()
        parent_ts = "1787901399.000100"
        child_ts = "1787901400.000100"
        deliver_private.side_effect = [
            {"message_id": "a" * 64},
            {"message_id": "b" * 64},
        ]

        child_result = ingest_slack_dm_event(
            {
                "event_id": "EvChildFirst",
                "team_id": "TMLAI",
                "event": {
                    "channel": "DONE",
                    "ts": child_ts,
                    "thread_ts": parent_ts,
                    "user": "UTWO",
                    "text": "child first",
                },
            }
        )
        self.assertEqual(child_result["status"], "enqueued")
        self.assertEqual(process_ready_deliveries(limit=1), 0)
        child = SlackDmMirrorDelivery.objects.get(pk=child_result["delivery_ids"][0])
        self.assertEqual(child.attempts, 0)

        parent_result = ingest_slack_dm_event(
            {
                "event_id": "EvParentSecond",
                "team_id": "TMLAI",
                "event": {
                    "channel": "DONE",
                    "ts": parent_ts,
                    "user": "UTWO",
                    "text": "parent second",
                },
            }
        )
        self.assertEqual(parent_result["status"], "enqueued")
        self.assertEqual(process_ready_deliveries(limit=1), 1)
        SlackDmMirrorDelivery.objects.filter(pk=child.pk).update(
            available_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(process_ready_deliveries(limit=1), 1)
        self.assertEqual(
            deliver_private.call_args_list[-1].kwargs["parent_message_id"],
            "a" * 64,
        )

    @patch("integrations.services.slack_dm_mirror.BuzzBridgeClient.deliver_private")
    def test_live_edit_waits_for_create_callback_that_arrives_later(
        self,
        deliver_private,
    ):
        self._live_conversation()
        target_ts = "1787901402.000100"
        deliver_private.side_effect = [
            {"message_id": "a" * 64},
            {"message_id": "b" * 64},
        ]
        edit_result = ingest_slack_dm_event(
            {
                "event_id": "EvEditFirst",
                "team_id": "TMLAI",
                "event": {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "DONE",
                    "event_ts": "1787901403.000100",
                    "message": {
                        "ts": target_ts,
                        "user": "UTWO",
                        "text": "edited",
                    },
                },
            }
        )
        self.assertEqual(process_ready_deliveries(limit=1), 0)
        edit = SlackDmMirrorDelivery.objects.get(pk=edit_result["delivery_ids"][0])

        create_result = ingest_slack_dm_event(
            {
                "event_id": "EvCreateSecond",
                "team_id": "TMLAI",
                "event": {
                    "channel": "DONE",
                    "ts": target_ts,
                    "user": "UTWO",
                    "text": "original",
                },
            }
        )
        self.assertEqual(create_result["status"], "enqueued")
        self.assertEqual(process_ready_deliveries(limit=1), 1)
        SlackDmMirrorDelivery.objects.filter(pk=edit.pk).update(
            available_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(process_ready_deliveries(limit=1), 1)
        self.assertEqual(
            deliver_private.call_args_list[-1].kwargs["target_message_id"],
            "a" * 64,
        )

    @patch("integrations.services.slack_dm_mirror.BuzzBridgeClient.deliver_private")
    def test_delayed_older_edit_cannot_overwrite_newer_completed_edit(
        self,
        deliver_private,
    ):
        _, conversation = self._live_conversation()
        target_ts = "1787901404.000100"
        SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id=target_ts,
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            metadata={"destination_message_id": "a" * 64},
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            completed_at=timezone.now(),
            available_at=timezone.now(),
        )
        deliver_private.return_value = {"message_id": "b" * 64}
        newer = ingest_slack_dm_event(
            {
                "event_id": "EvNewerEdit",
                "team_id": "TMLAI",
                "event": {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "DONE",
                    "event_ts": "1787901406.000100",
                    "message": {
                        "ts": target_ts,
                        "user": "UTWO",
                        "text": "newer",
                    },
                },
            }
        )
        self.assertEqual(process_ready_deliveries(limit=1), 1)
        older = ingest_slack_dm_event(
            {
                "event_id": "EvOlderEdit",
                "team_id": "TMLAI",
                "event": {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "DONE",
                    "event_ts": "1787901405.000100",
                    "message": {
                        "ts": target_ts,
                        "user": "UTWO",
                        "text": "older",
                    },
                },
            }
        )
        self.assertEqual(process_ready_deliveries(limit=1), 1)
        self.assertEqual(deliver_private.call_count, 1)
        stale = SlackDmMirrorDelivery.objects.get(pk=older["delivery_ids"][0])
        self.assertTrue(stale.metadata["dependency_superseded"])
        self.assertEqual(stale.encrypted_text, "")
        self.assertEqual(newer["status"], "enqueued")

    @patch("integrations.services.slack_dm_mirror.BuzzBridgeClient.deliver_private")
    def test_live_reply_with_unavailable_historical_parent_is_delivered_top_level(
        self,
        deliver_private,
    ):
        _, conversation = self._live_conversation()
        child = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id="1787901401.000100",
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="live reply",
            metadata={
                "thread_ts": "1700000000.000100",
                "dependency_outside_history": True,
            },
            available_at=timezone.now(),
        )
        SlackDmMirrorDelivery.objects.filter(pk=child.pk).update(
            created_at=timezone.now()
            - timedelta(seconds=slack_dm_mirror.DEPENDENCY_ARRIVAL_GRACE_SECONDS + 1)
        )
        child.refresh_from_db()
        deliver_private.return_value = {"message_id": "c" * 64}

        self.assertEqual(process_ready_deliveries(limit=1), 1)

        child.refresh_from_db()
        self.assertEqual(deliver_private.call_args.kwargs["parent_message_id"], "")
        self.assertTrue(child.metadata["thread_parent_unavailable"])
        self.assertEqual(
            child.metadata["original_thread_ts"],
            "1700000000.000100",
        )

    @patch("integrations.services.slack_dm_mirror.BuzzBridgeClient.deliver_private")
    def test_live_mutation_with_unavailable_target_is_superseded_content_free(
        self,
        deliver_private,
    ):
        _, conversation = self._live_conversation()
        mutation = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id="edit:missing",
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.EDIT,
            encrypted_text="private edit",
            metadata={
                "target_source_message_id": "1700000000.000100",
                "dependency_outside_history": True,
            },
            available_at=timezone.now(),
        )
        SlackDmMirrorDelivery.objects.filter(pk=mutation.pk).update(
            created_at=timezone.now()
            - timedelta(seconds=slack_dm_mirror.DEPENDENCY_ARRIVAL_GRACE_SECONDS + 1)
        )
        mutation.refresh_from_db()

        self.assertEqual(process_ready_deliveries(limit=1), 1)

        mutation.refresh_from_db()
        self.assertEqual(mutation.status, CommunityBridgeDeliveryStatus.COMPLETED)
        self.assertEqual(mutation.encrypted_text, "")
        self.assertTrue(mutation.metadata["dependency_superseded"])
        deliver_private.assert_not_called()

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_outbound_reply_with_unavailable_parent_is_sent_top_level(
        self,
        web_client,
    ):
        _, conversation = self._live_conversation()
        web_client.return_value.chat_postMessage.return_value = {
            "ts": "1787901500.000100"
        }
        result = ingest_mlai_dm_event(
            {
                "source_channel_id": str(conversation.mlai_channel_id),
                "normalized_event": {
                    "delivery_type": "create",
                    "source_message_id": "e" * 64,
                    "source_parent_message_id": "f" * 64,
                    "source_author_id": "1" * 64,
                    "text": "reply to pre-mirror history",
                },
            }
        )
        self.assertEqual(result["status"], "enqueued")

        SlackDmMirrorDelivery.objects.filter(pk=result["delivery_id"]).update(
            created_at=timezone.now()
            - timedelta(seconds=slack_dm_mirror.DEPENDENCY_ARRIVAL_GRACE_SECONDS + 1)
        )

        self.assertEqual(process_ready_deliveries(limit=1), 1)

        kwargs = web_client.return_value.chat_postMessage.call_args.kwargs
        self.assertNotIn("thread_ts", kwargs)
        delivery = SlackDmMirrorDelivery.objects.get(pk=result["delivery_id"])
        self.assertTrue(delivery.metadata["thread_parent_unavailable"])
        self.assertEqual(
            delivery.metadata["original_source_parent_message_id"],
            "f" * 64,
        )

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_terminal_backfill_is_rehydrated_by_idempotent_source_rescan(
        self,
        web_client,
    ):
        _, conversation = self._live_conversation()
        conversation.history_backfilled_at = timezone.now()
        conversation.save(update_fields=("history_backfilled_at", "updated_at"))
        source_ts = "1787901200.000100"
        dead = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id=source_ts,
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="",
            metadata={"backfill": True},
            status=CommunityBridgeDeliveryStatus.DEAD,
            attempts=5,
            available_at=timezone.now() - timedelta(seconds=1),
            last_error="BuzzBridgeError: MLAI Chat adapter returned HTTP 502",
        )
        web_client.return_value.conversations_history.return_value = {
            "messages": [
                {"ts": source_ts, "user": "UTWO", "text": "rehydrated safely"}
            ],
            "has_more": False,
            "response_metadata": {"next_cursor": ""},
        }

        self.assertEqual(process_due_history_backfills(), 1)

        dead.refresh_from_db()
        conversation.refresh_from_db()
        self.assertEqual(dead.status, CommunityBridgeDeliveryStatus.PENDING)
        self.assertEqual(dead.attempts, 0)
        self.assertEqual(dead.encrypted_text, "rehydrated safely")
        self.assertIsNotNone(conversation.history_backfilled_at)

    def test_status_waits_for_backfill_delivery_and_surfaces_dead_rows(self):
        grant = SlackDmMirrorGrant.objects.create(
            user=self.first,
            connection=self.first_connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            consented_at=timezone.now(),
        )
        conversation = SlackDmMirrorConversation.objects.create(
            grant=grant,
            slack_workspace_id="TMLAI",
            slack_conversation_id="DONE",
            participant_slack_ids=["UONE", "UTWO"],
            mlai_channel_id=uuid.uuid4(),
            status=SlackDmMirrorConversationStatus.LIVE,
            history_backfilled_at=timezone.now(),
        )
        delivery = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id="1787901000.000100",
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            metadata={"backfill": True},
            status=CommunityBridgeDeliveryStatus.DEAD,
            available_at=timezone.now(),
        )

        payload = status_payload(self.first)

        self.assertEqual(payload["backfill"]["complete"], 0)
        self.assertEqual(payload["backfill"]["pending"], 1)
        self.assertEqual(payload["backfill"]["failed_messages"], 1)

        delivery.status = CommunityBridgeDeliveryStatus.COMPLETED
        delivery.save(update_fields=("status", "updated_at"))
        payload = status_payload(self.first)
        self.assertEqual(payload["backfill"]["complete"], 1)
        self.assertEqual(payload["backfill"]["pending"], 0)

    def test_status_excludes_retired_and_superseded_backfill_rows(self):
        grant, live = self._live_conversation()
        live.history_backfilled_at = timezone.now()
        live.save(update_fields=("history_backfilled_at", "updated_at"))
        retired = SlackDmMirrorConversation.objects.create(
            grant=grant,
            slack_workspace_id="TMLAI",
            slack_conversation_id="DRETIRED",
            participant_slack_ids=[],
            status=SlackDmMirrorConversationStatus.PAUSED,
        )
        for conversation, superseded in ((live, True), (retired, False)):
            SlackDmMirrorDelivery.objects.create(
                conversation=conversation,
                source_platform=CommunityBridgePlatform.SLACK,
                source_message_id=f"dead-{conversation.pk}",
                source_author_id="UTWO",
                operation=CommunityBridgeDeliveryType.CREATE,
                metadata={
                    "backfill": True,
                    "history_recovery_superseded": superseded,
                },
                status=CommunityBridgeDeliveryStatus.DEAD,
                available_at=timezone.now(),
            )

        payload = status_payload(self.first)

        self.assertEqual(payload["backfill"]["complete"], 1)
        self.assertEqual(payload["backfill"]["pending"], 0)
        self.assertEqual(payload["backfill"]["failed_messages"], 0)

    def test_history_completion_rolls_back_if_release_fails(self):
        _, conversation = self._live_conversation()
        held = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id="1787900999.000100",
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="held until the scan commits",
            metadata={"backfill": True},
            available_at=timezone.now() + timedelta(days=365),
        )

        with patch.object(
            slack_dm_mirror,
            "_release_history_deliveries",
            side_effect=RuntimeError("release failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "release failed"):
                slack_dm_mirror._finish_history_scan(conversation)

        conversation.refresh_from_db()
        held.refresh_from_db()
        self.assertIsNone(conversation.history_backfilled_at)
        self.assertGreater(held.available_at, timezone.now() + timedelta(days=300))

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_legacy_unbounded_grant_is_capped_and_requeues_dead_rows(
        self, web_client
    ):
        grant = SlackDmMirrorGrant.objects.create(
            user=self.first,
            connection=self.first_connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            consented_at=timezone.now(),
            history_days=0,
        )
        conversation = SlackDmMirrorConversation.objects.create(
            grant=grant,
            slack_workspace_id="TMLAI",
            slack_conversation_id="DONE",
            participant_slack_ids=["UONE", "UTWO"],
            participant_buzz_pubkeys=["1" * 64, "3" * 64],
            participant_identity_map={"UONE": "1" * 64, "UTWO": "3" * 64},
            participant_hash="a" * 64,
            mlai_channel_id=uuid.uuid4(),
            status=SlackDmMirrorConversationStatus.LIVE,
        )
        dead = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id="1787900900.000100",
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            status=CommunityBridgeDeliveryStatus.DEAD,
            available_at=timezone.now(),
        )
        web_client.return_value.conversations_history.side_effect = [
            {
                "messages": [
                    {
                        "ts": "1787901000.000100",
                        "user": "UONE",
                        "text": "newer",
                    },
                    {
                        "ts": "1787900900.000100",
                        "user": "UTWO",
                        "text": "recovered",
                    },
                ],
                "has_more": True,
                "response_metadata": {"next_cursor": "next"},
            },
            {
                "messages": [
                    {
                        "ts": "1787900800.000100",
                        "user": "UTWO",
                        "text": "oldest",
                    }
                ],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            },
        ]

        self.assertEqual(process_due_history_backfills(), 1)
        conversation.refresh_from_db()
        self.assertIsNone(conversation.history_backfilled_at)
        self.assertEqual(conversation.oldest_synced_ts, "1787900900.000100")
        dead.refresh_from_db()
        self.assertEqual(dead.status, CommunityBridgeDeliveryStatus.PENDING)
        self.assertEqual(dead.encrypted_text, "recovered")

        self.assertEqual(process_due_history_backfills(), 1)
        conversation.refresh_from_db()
        self.assertIsNotNone(conversation.history_backfilled_at)
        self.assertEqual(self._message_deliveries(conversation).count(), 3)
        first_kwargs = web_client.return_value.conversations_history.call_args_list[
            0
        ].kwargs
        second_kwargs = web_client.return_value.conversations_history.call_args_list[
            1
        ].kwargs
        grant.refresh_from_db()
        self.assertEqual(grant.history_days, 30)
        self.assertIn("oldest", first_kwargs)
        self.assertEqual(second_kwargs["oldest"], first_kwargs["oldest"])
        self.assertEqual(second_kwargs["latest"], "1787900900.000100")
        self.assertFalse(second_kwargs["inclusive"])

    def test_history_rate_limit_honors_the_full_retry_after_value(self):
        error = RuntimeError("rate limited")
        error.response = MagicMock(headers={"Retry-After": "900"})
        original_available_at = slack_dm_mirror._history_scan_available_at
        try:
            with patch.object(slack_dm_mirror.time, "monotonic", return_value=100.0):
                slack_dm_mirror._apply_slack_retry_after(error)
            self.assertEqual(slack_dm_mirror._history_scan_available_at, 1000.0)
        finally:
            slack_dm_mirror._history_scan_available_at = original_available_at

    @patch("integrations.services.slack_dm_mirror.WebClient")
    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.unregister_private_conversation"
    )
    def test_invalid_slack_delivery_credential_revokes_and_erases_local_authority(
        self,
        unregister_private_conversation,
        web_client,
    ):
        grant, conversation = self._live_conversation()
        result = ingest_mlai_dm_event(
            {
                "source_channel_id": str(conversation.mlai_channel_id),
                "normalized_event": {
                    "delivery_type": "create",
                    "source_message_id": "9" * 64,
                    "source_author_id": "1" * 64,
                    "text": "must be erased",
                },
            }
        )
        web_client.return_value.chat_postMessage.side_effect = _slack_api_error(
            "invalid_auth"
        )

        self.assertEqual(process_ready_deliveries(limit=1), 0)

        grant.refresh_from_db()
        self.first_connection.refresh_from_db()
        delivery = SlackDmMirrorDelivery.objects.get(pk=result["delivery_id"])
        self.assertEqual(grant.status, SlackDmMirrorGrantStatus.REVOKED)
        self.assertEqual(self.first_connection.status, "disconnected")
        self.assertEqual(self.first_connection.access_token, "")
        self.assertEqual(delivery.encrypted_text, "")
        self.assertTrue(status_payload(self.first)["needs_reauthorization"])
        unregister_private_conversation.assert_called()

    @patch("integrations.services.slack_dm_mirror.revoke_user_grant")
    @patch("integrations.services.slack_dm_mirror.discover_conversations")
    def test_discovery_auth_failure_fences_user_grant(
        self,
        discover,
        revoke,
    ):
        grant, _ = self._live_conversation()
        grant.last_discovery_at = None
        grant.save(update_fields=("last_discovery_at", "updated_at"))
        discover.side_effect = _slack_api_error("token_revoked")
        slack_dm_mirror._last_grant_discovery_scan = 0.0

        slack_dm_mirror.discover_grants_if_due()

        revoke.assert_called_once_with(self.first)

    @patch("integrations.services.slack_dm_mirror.revoke_user_grant")
    @patch("integrations.services.slack_dm_mirror._enqueue_history_page")
    def test_history_auth_failure_fences_user_grant(
        self,
        enqueue_history,
        revoke,
    ):
        _, conversation = self._live_conversation()
        conversation.history_backfilled_at = None
        conversation.save(update_fields=("history_backfilled_at", "updated_at"))
        enqueue_history.side_effect = _slack_api_error("token_expired")
        slack_dm_mirror._history_scan_available_at = 0.0

        self.assertEqual(process_due_history_backfills(limit=1), 0)

        revoke.assert_called_once_with(self.first)

    @patch("integrations.services.slack_dm_mirror.BuzzBridgeClient.deliver_private")
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_outbound_retry_stays_slack_direction_with_stable_client_message_id(
        self,
        web_client,
        deliver_private,
    ):
        grant = SlackDmMirrorGrant.objects.create(
            user=self.first,
            connection=self.first_connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            consented_at=timezone.now(),
        )
        channel_id = uuid.uuid4()
        conversation = SlackDmMirrorConversation.objects.create(
            grant=grant,
            slack_workspace_id="TMLAI",
            slack_conversation_id="DONE",
            participant_slack_ids=["UONE", "UTWO"],
            participant_buzz_pubkeys=["1" * 64, "3" * 64],
            participant_identity_map={"UONE": "1" * 64, "UTWO": "3" * 64},
            mlai_channel_id=channel_id,
            status=SlackDmMirrorConversationStatus.LIVE,
        )
        echo_results = []

        def accepted_then_timeout(**kwargs):
            echo_results.append(
                ingest_slack_dm_event(
                    {
                        "event_id": "EvAmbiguousCreateEcho",
                        "team_id": "TMLAI",
                        "event": {
                            "type": "message",
                            "channel": "DONE",
                            "ts": "1787901200.000100",
                            "user": "UONE",
                            "text": "from MLAI",
                            "client_msg_id": kwargs["client_msg_id"],
                        },
                    }
                )
            )
            raise RuntimeError("timeout after Slack accepted create")

        create_attempts = 0

        def create_side_effect(**kwargs):
            nonlocal create_attempts
            create_attempts += 1
            if create_attempts == 1:
                return accepted_then_timeout(**kwargs)
            return {"ts": "1787901200.000100"}

        web_client.return_value.chat_postMessage.side_effect = create_side_effect
        payload = {
            "source_channel_id": str(channel_id),
            "normalized_event": {
                "delivery_type": "create",
                "source_message_id": "b" * 64,
                "source_author_id": "1" * 64,
                "text": "from MLAI",
            },
        }

        result = ingest_mlai_dm_event(payload)
        self.assertEqual(result["status"], "enqueued")
        self.assertEqual(process_ready_deliveries(), 0)
        self.assertEqual(echo_results, [{"status": "echo_ignored", "count": 1}])
        self.assertFalse(
            conversation.deliveries.filter(
                source_platform=CommunityBridgePlatform.SLACK
            ).exists()
        )
        delivery = SlackDmMirrorDelivery.objects.get(
            source_platform=CommunityBridgePlatform.BUZZ
        )
        self.assertEqual(delivery.status, CommunityBridgeDeliveryStatus.PENDING)
        delivery.available_at = timezone.now()
        delivery.save(update_fields=("available_at", "updated_at"))

        self.assertEqual(process_ready_deliveries(), 1)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CommunityBridgeDeliveryStatus.COMPLETED)
        self.assertEqual(delivery.encrypted_text, "")
        first_id = web_client.return_value.chat_postMessage.call_args_list[0].kwargs[
            "client_msg_id"
        ]
        second_id = web_client.return_value.chat_postMessage.call_args_list[1].kwargs[
            "client_msg_id"
        ]
        self.assertEqual(first_id, second_id)
        uuid.UUID(first_id)
        deliver_private.assert_not_called()

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_pending_mutation_echo_is_suppressed_after_slack_retry_window(
        self,
        web_client,
    ):
        grant, conversation = self._live_conversation()
        slack_ts = "1787901240.000100"
        echo_key = slack_dm_mirror._slack_echo_key(
            operation=CommunityBridgeDeliveryType.EDIT,
            target_message_id=slack_ts,
            author_id=grant.slack_user_id,
            text="edited after timeout",
        )
        outbound = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id="a" * 64,
            source_author_id="1" * 64,
            operation=CommunityBridgeDeliveryType.EDIT,
            encrypted_text="edited after timeout",
            metadata={"slack_echo_key": echo_key},
            status=CommunityBridgeDeliveryStatus.PENDING,
            available_at=timezone.now() + timedelta(minutes=10),
        )
        SlackDmMirrorDelivery.objects.filter(pk=outbound.pk).update(
            updated_at=timezone.now()
            - timedelta(seconds=slack_dm_mirror.SLACK_ECHO_WINDOW_SECONDS + 1)
        )

        echoed = ingest_slack_dm_event(
            {
                "event_id": "EvDelayedAmbiguousEditEcho",
                "team_id": "TMLAI",
                "event": {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "DONE",
                    "event_ts": "1787901241.000100",
                    "message": {
                        "ts": slack_ts,
                        "user": grant.slack_user_id,
                        "text": "edited after timeout",
                    },
                },
            }
        )

        self.assertEqual(echoed, {"status": "echo_ignored", "count": 1})
        self.assertEqual(
            conversation.deliveries.filter(
                source_platform=CommunityBridgePlatform.SLACK
            ).count(),
            0,
        )
        web_client.assert_not_called()

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_ambiguous_outbound_edit_echo_is_ignored_before_retry(self, web_client):
        _, conversation = self._live_conversation()
        source_event_id = "a" * 64
        slack_ts = "1787901250.000100"
        SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id=source_event_id,
            source_author_id="1" * 64,
            operation=CommunityBridgeDeliveryType.CREATE,
            metadata={"slack_ts": slack_ts},
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            completed_at=timezone.now(),
            available_at=timezone.now(),
        )
        echo_results = []

        def accepted_then_timeout(**_kwargs):
            echo_results.append(
                ingest_slack_dm_event(
                    {
                        "event_id": "EvAmbiguousEditEcho",
                        "team_id": "TMLAI",
                        "event": {
                            "type": "message",
                            "subtype": "message_changed",
                            "channel": "DONE",
                            "event_ts": "1787901251.000100",
                            "message": {
                                "ts": slack_ts,
                                "user": "UONE",
                                "text": "edited after timeout",
                            },
                        },
                    }
                )
            )
            raise RuntimeError("timeout after Slack accepted edit")

        edit_attempts = 0

        def edit_side_effect(**kwargs):
            nonlocal edit_attempts
            edit_attempts += 1
            if edit_attempts == 1:
                return accepted_then_timeout(**kwargs)
            return {"ts": slack_ts}

        web_client.return_value.chat_update.side_effect = edit_side_effect
        result = ingest_mlai_dm_event(
            {
                "receipt_key": f"message_update:{source_event_id}:1",
                "source_channel_id": str(conversation.mlai_channel_id),
                "normalized_event": {
                    "delivery_type": "edit",
                    "source_message_id": source_event_id,
                    "source_parent_message_id": "",
                    "source_author_id": "1" * 64,
                    "text": "edited after timeout",
                },
            }
        )

        self.assertEqual(result["status"], "enqueued")
        self.assertEqual(process_ready_deliveries(limit=1), 0)
        self.assertEqual(echo_results, [{"status": "echo_ignored", "count": 1}])
        edit = SlackDmMirrorDelivery.objects.get(pk=result["delivery_id"])
        self.assertEqual(edit.status, CommunityBridgeDeliveryStatus.PENDING)
        self.assertTrue(edit.metadata["slack_echo_key"])
        self.assertFalse(
            conversation.deliveries.filter(
                source_platform=CommunityBridgePlatform.SLACK,
                operation=CommunityBridgeDeliveryType.EDIT,
            ).exists()
        )

        edit.available_at = timezone.now()
        edit.save(update_fields=("available_at", "updated_at"))
        self.assertEqual(process_ready_deliveries(limit=1), 1)

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_mlai_reply_mutations_and_reactions_use_persisted_slack_ids(
        self,
        web_client,
    ):
        _, conversation = self._live_conversation()
        root_event_id = "a" * 64
        root_slack_ts = "1787901300.000100"
        conversation.deliveries.create(
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id=root_event_id,
            source_author_id="1" * 64,
            operation=CommunityBridgeDeliveryType.CREATE,
            metadata={
                "source_event_id": root_event_id,
                "slack_ts": root_slack_ts,
            },
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            available_at=timezone.now(),
            completed_at=timezone.now(),
        )
        client = web_client.return_value
        client.chat_postMessage.return_value = {"ts": "1787901301.000100"}
        client.chat_update.return_value = {"ts": root_slack_ts}

        reply_event_id = "b" * 64
        result = ingest_mlai_dm_event(
            {
                "receipt_key": f"message_create:{reply_event_id}",
                "source_channel_id": str(conversation.mlai_channel_id),
                "normalized_event": {
                    "delivery_type": "create",
                    "source_message_id": reply_event_id,
                    "source_parent_message_id": root_event_id,
                    "source_author_id": "1" * 64,
                    "text": "reply",
                },
            }
        )
        self.assertEqual(result["status"], "enqueued")
        self.assertEqual(process_ready_deliveries(limit=1), 1)
        self.assertEqual(
            client.chat_postMessage.call_args.kwargs["thread_ts"],
            root_slack_ts,
        )

        result = ingest_mlai_dm_event(
            {
                "receipt_key": f"message_update:{root_event_id}:1",
                "source_channel_id": str(conversation.mlai_channel_id),
                "normalized_event": {
                    "delivery_type": "edit",
                    "source_message_id": root_event_id,
                    "source_parent_message_id": "",
                    "source_author_id": "1" * 64,
                    "text": "edited",
                },
            }
        )
        self.assertEqual(result["status"], "enqueued")
        self.assertEqual(process_ready_deliveries(limit=1), 1)
        client.chat_update.assert_called_once_with(
            channel="DONE",
            ts=root_slack_ts,
            text="edited",
        )
        edit_delivery = conversation.deliveries.get(
            source_platform=CommunityBridgePlatform.BUZZ,
            operation=CommunityBridgeDeliveryType.EDIT,
        )
        self.assertTrue(edit_delivery.metadata["slack_echo_key"])

        reaction_event_id = "c" * 64
        for receipt_key, operation in (
            (f"reaction_add:{reaction_event_id}", "reaction_add"),
            (f"reaction_remove:{reaction_event_id}", "reaction_remove"),
        ):
            result = ingest_mlai_dm_event(
                {
                    "receipt_key": receipt_key,
                    "source_channel_id": str(conversation.mlai_channel_id),
                    "normalized_event": {
                        "delivery_type": operation,
                        "source_message_id": reaction_event_id,
                        "source_parent_message_id": root_event_id,
                        "source_author_id": "1" * 64,
                        "text": ":party_parrot:",
                    },
                }
            )
            self.assertEqual(result["status"], "enqueued")
            self.assertEqual(process_ready_deliveries(limit=1), 1)
        client.reactions_add.assert_called_once_with(
            channel="DONE",
            timestamp=root_slack_ts,
            name="party_parrot",
        )
        client.reactions_remove.assert_called_once_with(
            channel="DONE",
            timestamp=root_slack_ts,
            name="party_parrot",
        )

        result = ingest_mlai_dm_event(
            {
                "receipt_key": f"message_delete:{root_event_id}",
                "source_channel_id": str(conversation.mlai_channel_id),
                "normalized_event": {
                    "delivery_type": "delete",
                    "source_message_id": root_event_id,
                    "source_parent_message_id": "",
                    "source_author_id": "1" * 64,
                    "text": "must be discarded",
                },
            }
        )
        self.assertEqual(result["status"], "enqueued")
        self.assertEqual(process_ready_deliveries(limit=1), 1)
        client.chat_delete.assert_called_once_with(channel="DONE", ts=root_slack_ts)

        echoed = ingest_slack_dm_event(
            {
                "event_id": "EvEchoedPrivateEdit",
                "team_id": "TMLAI",
                "event": {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "DONE",
                    "event_ts": "1787901302.000100",
                    "message": {
                        "ts": root_slack_ts,
                        "user": "UONE",
                        "text": "edited",
                    },
                },
            }
        )
        self.assertEqual(echoed["status"], "echo_ignored")

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_revoked_device_cannot_drain_a_previously_queued_outbound_body(
        self,
        web_client,
    ):
        _, conversation = self._live_conversation()
        result = ingest_mlai_dm_event(
            {
                "receipt_key": "message_create:" + "d" * 64,
                "source_channel_id": str(conversation.mlai_channel_id),
                "normalized_event": {
                    "delivery_type": "create",
                    "source_message_id": "d" * 64,
                    "source_author_id": "1" * 64,
                    "text": "must not leave after revocation",
                },
            }
        )
        self.assertEqual(result["status"], "enqueued")
        CommunityChatDevice.objects.filter(
            user=self.first,
            public_key="1" * 64,
        ).update(revoked_at=timezone.now())

        self.assertEqual(process_ready_deliveries(limit=1), 0)

        delivery = conversation.deliveries.get(
            source_platform=CommunityBridgePlatform.BUZZ
        )
        self.assertEqual(delivery.status, CommunityBridgeDeliveryStatus.DEAD)
        self.assertEqual(delivery.encrypted_text, "")
        web_client.return_value.chat_postMessage.assert_not_called()

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_mismatched_slack_credential_cannot_drain_a_queued_private_body(
        self,
        web_client,
    ):
        _, conversation = self._live_conversation()
        result = ingest_mlai_dm_event(
            {
                "receipt_key": "message_create:" + "e" * 64,
                "source_channel_id": str(conversation.mlai_channel_id),
                "normalized_event": {
                    "delivery_type": "create",
                    "source_message_id": "e" * 64,
                    "source_author_id": "1" * 64,
                    "text": "must not cross Slack identities",
                },
            }
        )
        self.assertEqual(result["status"], "enqueued")
        self.first_connection.access_token = "xoxp-other-identity"
        self.first_connection.external_account_id = "TOTHER"
        self.first_connection.provider_metadata = {
            "team": {"id": "TOTHER", "name": "Other"},
            "authed_user": {"id": "UOTHER", "scope": ",".join(SCOPES)},
        }
        self.first_connection.save(
            update_fields=(
                "access_token",
                "external_account_id",
                "provider_metadata",
                "updated_at",
            )
        )

        self.assertEqual(process_ready_deliveries(limit=1), 0)

        delivery = self._message_deliveries(conversation).get(
            source_platform=CommunityBridgePlatform.BUZZ
        )
        self.assertEqual(delivery.status, CommunityBridgeDeliveryStatus.DEAD)
        self.assertEqual(delivery.encrypted_text, "")
        web_client.assert_not_called()

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_mismatched_slack_credential_cannot_enqueue_history_bodies(
        self,
        web_client,
    ):
        _, conversation = self._live_conversation()
        self.first_connection.access_token = "xoxp-other-identity"
        self.first_connection.external_account_id = "TOTHER"
        self.first_connection.provider_metadata = {
            "team": {"id": "TOTHER", "name": "Other"},
            "authed_user": {"id": "UOTHER", "scope": ",".join(SCOPES)},
        }
        self.first_connection.save(
            update_fields=(
                "access_token",
                "external_account_id",
                "provider_metadata",
                "updated_at",
            )
        )

        self.assertEqual(process_due_history_backfills(limit=1), 0)

        self.assertFalse(self._message_deliveries(conversation).exists())
        web_client.assert_not_called()

    @patch("integrations.services.slack_dm_mirror.BuzzBridgeClient.deliver_private")
    def test_stale_claim_cannot_deliver_after_consent_is_revoked(
        self,
        deliver_private,
    ):
        grant, conversation = self._live_conversation()
        delivery = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id="1787901303.000100",
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="must not leave after revocation",
            metadata={"participant_hash": conversation.participant_hash},
            status=CommunityBridgeDeliveryStatus.PROCESSING,
            available_at=timezone.now(),
        )
        stale_claim = SlackDmMirrorDelivery.objects.select_related(
            "conversation__grant__connection"
        ).get(pk=delivery.pk)
        now = timezone.now()
        grant.status = SlackDmMirrorGrantStatus.REVOKED
        grant.revoked_at = now
        grant.save(update_fields=("status", "revoked_at", "updated_at"))
        conversation.status = SlackDmMirrorConversationStatus.PAUSED
        conversation.save(update_fields=("status", "updated_at"))
        SlackDmMirrorDelivery.objects.filter(pk=delivery.pk).update(
            status=CommunityBridgeDeliveryStatus.DEAD,
            encrypted_text="",
            last_error="Consent revoked",
        )

        with self.assertRaises(slack_dm_mirror.SlackDmMirrorAuthorizationError):
            slack_dm_mirror._deliver_private(stale_claim)

        deliver_private.assert_not_called()

    @patch("integrations.services.slack_dm_mirror.BuzzBridgeClient.deliver_private")
    def test_private_reply_and_mutation_resolve_adapter_message_ids(
        self,
        deliver_private,
    ):
        grant = SlackDmMirrorGrant.objects.create(
            user=self.first,
            connection=self.first_connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            consented_at=timezone.now(),
        )
        conversation = SlackDmMirrorConversation.objects.create(
            grant=grant,
            slack_workspace_id="TMLAI",
            slack_conversation_id="DONE",
            participant_slack_ids=["UONE", "UTWO"],
            participant_buzz_pubkeys=["1" * 64, "3" * 64],
            participant_identity_map={"UONE": "1" * 64, "UTWO": "3" * 64},
            mlai_channel_id=uuid.uuid4(),
            status=SlackDmMirrorConversationStatus.LIVE,
        )
        parent_source_id = "1787901400.000100"
        parent_destination_id = "a" * 64
        SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id=parent_source_id,
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            metadata={"destination_message_id": parent_destination_id},
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            available_at=timezone.now(),
            completed_at=timezone.now(),
        )
        reply_source_id = "1787901401.000100"
        SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id=reply_source_id,
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="reply",
            metadata={"thread_ts": parent_source_id},
            available_at=timezone.now(),
        )
        deliver_private.return_value = {
            "message_id": "b" * 64,
            "parent_message_id": parent_destination_id,
        }

        self.assertEqual(process_ready_deliveries(limit=1), 1)
        self.assertEqual(
            deliver_private.call_args.kwargs["parent_message_id"],
            parent_destination_id,
        )
        reply = conversation.deliveries.get(source_message_id=reply_source_id)
        self.assertEqual(reply.metadata["destination_message_id"], "b" * 64)

        conversation.deliveries.create(
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id=reply_source_id,
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.EDIT,
            encrypted_text="edited reply",
            metadata={"target_source_message_id": reply_source_id},
            available_at=timezone.now(),
        )
        deliver_private.return_value = {
            "message_id": "c" * 64,
            "parent_message_id": "",
        }

        self.assertEqual(process_ready_deliveries(limit=1), 1)
        self.assertEqual(
            deliver_private.call_args.kwargs["target_message_id"],
            "b" * 64,
        )

    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.provision_private_conversation"
    )
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_discovery_continues_after_one_conversation_fails(
        self,
        web_client,
        provision,
    ):
        client = web_client.return_value
        client.conversations_list.return_value = {
            "channels": [
                {"id": "DFIRST", "user": "UTWO"},
                {"id": "DSECOND", "user": "UTHREE"},
            ],
            "response_metadata": {"next_cursor": ""},
        }
        client.users_info.side_effect = lambda *, user: {
            "user": {"id": user, "team_id": "TMLAI", "name": user.lower()}
        }
        provision.side_effect = lambda pubkeys, **kwargs: (
            (_ for _ in ()).throw(RuntimeError("first adapter call failed"))
            if provision.call_count == 1
            else {
                "channel_id": str(uuid.uuid4()),
                "participant_pubkeys": pubkeys,
            }
        )
        grant = activate_connection(self.first_connection)

        self.assertEqual(discover_conversations(grant), 1)

        first = grant.conversations.get(slack_conversation_id="DFIRST")
        second = grant.conversations.get(slack_conversation_id="DSECOND")
        self.assertEqual(first.status, SlackDmMirrorConversationStatus.ERROR)
        self.assertIn("first adapter call failed", first.last_error)
        self.assertEqual(second.status, SlackDmMirrorConversationStatus.LIVE)
        grant.refresh_from_db()
        self.assertIn("DFIRST", grant.last_error)

    def test_revoked_identity_repairs_atomically_and_marks_completed_history_due(self):
        old_device = CommunityChatDevice.objects.get(
            user=self.first,
            public_key="1" * 64,
        )
        old_device.status = DeviceBindingStatus.REVOKED
        old_device.revoked_at = timezone.now()
        old_device.save(update_fields=("status", "revoked_at", "updated_at"))
        new_public_key = "5" * 64
        CommunityChatDevice.objects.create(
            user=self.first,
            public_key=new_public_key,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        grant = SlackDmMirrorGrant.objects.create(
            user=self.first,
            connection=self.first_connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            consented_at=timezone.now(),
            last_discovery_at=timezone.now(),
        )
        CommunityBridgeIdentityLink.objects.create(
            user=self.first,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            buzz_pubkey="1" * 64,
            display_name="First",
            verification_method=(
                CommunityBridgeIdentityVerificationMethod.ACCOUNT_CHALLENGE
            ),
            verification_reference="old-device",
            verified_at=timezone.now(),
        )
        conversation = SlackDmMirrorConversation.objects.create(
            grant=grant,
            slack_workspace_id="TMLAI",
            slack_conversation_id="DONE",
            participant_slack_ids=["UONE", "UTWO"],
            participant_buzz_pubkeys=["1" * 64, "3" * 64],
            participant_identity_map={"UONE": "1" * 64, "UTWO": "3" * 64},
            mlai_channel_id=uuid.uuid4(),
            status=SlackDmMirrorConversationStatus.LIVE,
            history_backfilled_at=timezone.now(),
        )
        delivery = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id="1787901300.000100",
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            completed_at=timezone.now(),
            available_at=timezone.now(),
        )

        link, repaired, authenticated_matches = ensure_owner_identity(
            grant,
            authenticated_public_key=new_public_key,
        )

        self.assertTrue(repaired)
        self.assertTrue(authenticated_matches)
        self.assertEqual(link.buzz_pubkey, new_public_key)
        grant.refresh_from_db()
        conversation.refresh_from_db()
        delivery.refresh_from_db()
        self.assertIsNone(grant.last_discovery_at)
        self.assertIsNone(conversation.history_backfilled_at)
        self.assertEqual(
            conversation.status,
            SlackDmMirrorConversationStatus.PROVISIONING,
        )
        self.assertEqual(delivery.status, CommunityBridgeDeliveryStatus.DEAD)
        self.assertEqual(delivery.encrypted_text, "")
        identity = status_payload(
            self.first,
            authenticated_public_key=new_public_key,
        )["identity"]
        self.assertEqual(identity["state"], "active")
        self.assertFalse(identity["repair_required"])
        self.assertTrue(identity["authenticated_device_matches"])

    def test_active_identity_is_not_flipped_by_another_verified_device(self):
        second_public_key = "5" * 64
        CommunityChatDevice.objects.create(
            user=self.first,
            public_key=second_public_key,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        grant = SlackDmMirrorGrant.objects.create(
            user=self.first,
            connection=self.first_connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            consented_at=timezone.now(),
        )
        original = CommunityBridgeIdentityLink.objects.create(
            user=self.first,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            buzz_pubkey="1" * 64,
            display_name="First",
            verification_method=(
                CommunityBridgeIdentityVerificationMethod.ACCOUNT_CHALLENGE
            ),
            verification_reference="first-device",
            verified_at=timezone.now(),
        )

        link, repaired, authenticated_matches = ensure_owner_identity(
            grant,
            authenticated_public_key=second_public_key,
        )

        self.assertFalse(repaired)
        self.assertFalse(authenticated_matches)
        self.assertEqual(link.pk, original.pk)
        self.assertEqual(link.buzz_pubkey, "1" * 64)

    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.provision_private_conversation"
    )
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_two_active_devices_share_one_to_one_mirror_and_can_send_outbound(
        self,
        web_client,
        provision,
    ):
        second_device_key = "5" * 64
        CommunityChatDevice.objects.create(
            user=self.first,
            public_key=second_device_key,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        client = web_client.return_value
        client.conversations_list.return_value = {
            "channels": [{"id": "DONE", "user": "UTWO"}],
            "response_metadata": {"next_cursor": ""},
        }
        client.users_info.side_effect = lambda *, user: {
            "user": {"id": user, "team_id": "TMLAI", "name": user.lower()}
        }
        provision.side_effect = lambda pubkeys, **_: {
            "channel_id": str(uuid.uuid4()),
            "participant_pubkeys": pubkeys,
        }
        grant = activate_connection(self.first_connection)
        discover_conversations(grant)
        conversation = grant.conversations.get(slack_conversation_id="DONE")

        self.assertIn("1" * 64, conversation.participant_buzz_pubkeys)
        self.assertIn(second_device_key, conversation.participant_buzz_pubkeys)
        self.assertEqual(len(conversation.participant_buzz_pubkeys), 3)
        self.assertEqual(
            set(provision.call_args.kwargs["callback_author_pubkeys"]),
            {"1" * 64, second_device_key},
        )
        for index, public_key in enumerate(("1" * 64, second_device_key)):
            result = ingest_mlai_dm_event(
                {
                    "source_channel_id": str(conversation.mlai_channel_id),
                    "normalized_event": {
                        "delivery_type": "create",
                        "source_message_id": f"{index + 6:x}" * 64,
                        "source_author_id": public_key,
                        "text": f"from device {index}",
                    },
                }
            )
            self.assertEqual(result["status"], "enqueued")
        self.assertEqual(
            conversation.deliveries.filter(source_platform=CommunityBridgePlatform.BUZZ)
            .exclude(
                source_message_id__startswith=slack_dm_mirror.REGISTRATION_STATE_PREFIX
            )
            .count(),
            2,
        )

    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.provision_private_conversation"
    )
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_maximum_mpim_reports_when_only_authenticated_device_fits(
        self,
        web_client,
        provision,
    ):
        authenticated_key = "5" * 64
        CommunityChatDevice.objects.create(
            user=self.first,
            public_key=authenticated_key,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        grant = SlackDmMirrorGrant.objects.create(
            user=self.first,
            connection=self.first_connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            consented_at=timezone.now(),
        )
        CommunityBridgeIdentityLink.objects.create(
            user=self.first,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            buzz_pubkey="1" * 64,
            display_name="First",
            verification_method=(
                CommunityBridgeIdentityVerificationMethod.ACCOUNT_CHALLENGE
            ),
            verification_reference="preferred-device",
            verified_at=timezone.now(),
        )
        targets = [f"U{index}" for index in range(2, 10)]
        client = web_client.return_value
        client.users_info.side_effect = lambda *, user: {
            "user": {"id": user, "team_id": "TMLAI", "name": user.lower()}
        }
        client.conversations_open.return_value = {"channel": {"id": "GMAX"}}
        provision.side_effect = lambda pubkeys, **_: {
            "channel_id": str(uuid.uuid4()),
            "participant_pubkeys": pubkeys,
        }

        payload = open_slack_dm(
            grant,
            slack_user_ids=targets,
            authenticated_public_key=authenticated_key,
        )

        self.assertEqual(payload["device_capacity"]["active"], 2)
        self.assertEqual(payload["device_capacity"]["included"], 1)
        self.assertTrue(payload["device_capacity"]["limited"])
        self.assertTrue(payload["device_capacity"]["authenticated_device_included"])
        self.assertEqual(payload["owner_device_pubkeys"], [authenticated_key])
        self.assertEqual(
            provision.call_args.kwargs["callback_author_pubkeys"],
            [authenticated_key],
        )

    @patch("integrations.services.slack_dm_mirror.BuzzBridgeClient.deliver_private")
    def test_missing_live_dependency_waits_for_history_reconciliation(
        self,
        deliver_private,
    ):
        _, conversation = self._live_conversation()
        child = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id="1787902001.000100",
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="late reply",
            metadata={"thread_ts": "1787902000.000100"},
            available_at=timezone.now(),
        )

        self.assertEqual(process_ready_deliveries(limit=1), 0)

        child.refresh_from_db()
        conversation.refresh_from_db()
        self.assertTrue(child.metadata["dependency_reconciliation_pending"])
        self.assertIsNone(conversation.history_backfilled_at)
        deliver_private.assert_not_called()

        slack_dm_mirror._finish_history_scan(conversation)
        deliver_private.return_value = {"message_id": "c" * 64}

        self.assertEqual(process_ready_deliveries(limit=1), 1)
        child.refresh_from_db()
        self.assertTrue(child.metadata["dependency_reconciliation_complete"])
        self.assertTrue(child.metadata["thread_parent_unavailable"])
        self.assertEqual(deliver_private.call_args.kwargs["parent_message_id"], "")

    @patch("integrations.services.slack_dm_mirror.BuzzBridgeClient.deliver_private")
    def test_dependency_target_arriving_after_legacy_grace_is_not_discarded(
        self,
        deliver_private,
    ):
        _, conversation = self._live_conversation()
        parent_ts = "1787902010.000100"
        child = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id="1787902011.000100",
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="eventually threaded",
            metadata={"thread_ts": parent_ts},
            available_at=timezone.now(),
        )
        SlackDmMirrorDelivery.objects.filter(pk=child.pk).update(
            created_at=timezone.now() - timedelta(minutes=20)
        )

        self.assertEqual(process_ready_deliveries(limit=1), 0)

        SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id=parent_ts,
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="",
            metadata={"destination_message_id": "p" * 64},
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            completed_at=timezone.now(),
            available_at=timezone.now(),
        )
        SlackDmMirrorDelivery.objects.filter(pk=child.pk).update(
            available_at=timezone.now()
        )
        deliver_private.return_value = {"message_id": "c" * 64}

        self.assertEqual(process_ready_deliveries(limit=1), 1)
        self.assertEqual(
            deliver_private.call_args.kwargs["parent_message_id"],
            "p" * 64,
        )

    @patch("integrations.services.slack_dm_mirror.BuzzBridgeClient.deliver_private")
    def test_delete_waits_for_a_late_create_target(self, deliver_private):
        _, conversation = self._live_conversation()
        target_ts = "1787902015.000100"
        deletion = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id="delete-before-create",
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.DELETE,
            encrypted_text="",
            metadata={"target_source_message_id": target_ts},
            available_at=timezone.now(),
        )
        SlackDmMirrorDelivery.objects.filter(pk=deletion.pk).update(
            created_at=timezone.now() - timedelta(minutes=20)
        )

        self.assertEqual(process_ready_deliveries(limit=1), 0)

        SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id=target_ts,
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="",
            metadata={"destination_message_id": "t" * 64},
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            completed_at=timezone.now(),
            available_at=timezone.now(),
        )
        SlackDmMirrorDelivery.objects.filter(pk=deletion.pk).update(
            available_at=timezone.now()
        )
        deliver_private.return_value = {}

        self.assertEqual(process_ready_deliveries(limit=1), 1)
        self.assertEqual(
            deliver_private.call_args.kwargs["target_message_id"],
            "t" * 64,
        )

    def test_history_skips_outbound_message_and_reaction_then_reconciles_delete(
        self,
    ):
        grant, conversation = self._live_conversation()
        grant.history_days = 0
        grant.save(update_fields=("history_days", "updated_at"))
        slack_ts = "1787902020.000100"
        outbound_create = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id="a" * 64,
            source_author_id="1" * 64,
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="",
            metadata={"slack_ts": slack_ts},
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            completed_at=timezone.now(),
            available_at=timezone.now(),
        )
        reaction_id = slack_dm_mirror.reaction_object_id(
            message_id=slack_ts,
            reaction="thumbsup",
            author_id=grant.slack_user_id,
        )
        SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id="b" * 64,
            source_author_id="1" * 64,
            operation=CommunityBridgeDeliveryType.REACTION_ADD,
            encrypted_text="",
            metadata={"slack_reaction_object_id": reaction_id},
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            completed_at=timezone.now(),
            available_at=timezone.now(),
        )
        authority = slack_dm_mirror._SlackHistoryScanAuthority(
            epoch="history-epoch",
            participant_hash=conversation.participant_hash,
            mlai_channel_id=str(conversation.mlai_channel_id),
            registration_id="",
            registration_generation="",
            history_days=0,
            oldest="",
        )
        slack_dm_mirror._mark_history_reconciliation_candidates_locked(conversation)
        slack_dm_mirror._enqueue_history_message(
            conversation,
            {
                "ts": slack_ts,
                "user": grant.slack_user_id,
                "text": "from MLAI",
                "reactions": [
                    {
                        "name": "thumbsup",
                        "users": [grant.slack_user_id],
                    }
                ],
            },
            scan_authority=authority,
            held_until=timezone.now(),
        )

        outbound_create.refresh_from_db()
        self.assertNotIn("history_reconcile_candidate", outbound_create.metadata)
        self.assertFalse(
            SlackDmMirrorDelivery.objects.filter(
                conversation=conversation,
                source_platform=CommunityBridgePlatform.SLACK,
                operation__in=(
                    CommunityBridgeDeliveryType.CREATE,
                    CommunityBridgeDeliveryType.REACTION_ADD,
                ),
            ).exists()
        )

        deleted_slack_ts = "1787902021.000100"
        SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id="c" * 64,
            source_author_id="1" * 64,
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="",
            metadata={"slack_ts": deleted_slack_ts},
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            completed_at=timezone.now(),
            available_at=timezone.now(),
        )
        SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id="d" * 64,
            source_author_id="1" * 64,
            operation=CommunityBridgeDeliveryType.DELETE,
            encrypted_text="",
            metadata={"slack_ts": deleted_slack_ts},
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            completed_at=timezone.now(),
            available_at=timezone.now(),
        )
        slack_dm_mirror._mark_history_reconciliation_candidates_locked(conversation)
        outbound_create.refresh_from_db()
        slack_dm_mirror._ensure_history_state(
            conversation,
            source_message_id=slack_dm_mirror.HISTORY_MAIN_STATE_ID,
            metadata={
                "history_scan_state": "main",
                "complete": True,
                "scan_epoch": "history-epoch",
                "oldest": outbound_create.metadata[
                    slack_dm_mirror.HISTORY_RECONCILE_OLDEST_KEY
                ],
                slack_dm_mirror.HISTORY_RECONCILE_EPOCH_KEY: (
                    outbound_create.metadata[
                        slack_dm_mirror.HISTORY_RECONCILE_EPOCH_KEY
                    ]
                ),
            },
        )
        slack_dm_mirror._finish_history_scan(conversation)

        deletion = SlackDmMirrorDelivery.objects.get(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            operation=CommunityBridgeDeliveryType.DELETE,
        )
        self.assertEqual(deletion.metadata["target_source_message_id"], slack_ts)
        self.assertTrue(deletion.metadata["history_reconciliation"])
        self.assertFalse(
            SlackDmMirrorDelivery.objects.filter(
                conversation=conversation,
                source_platform=CommunityBridgePlatform.SLACK,
                operation=CommunityBridgeDeliveryType.DELETE,
                metadata__target_source_message_id=deleted_slack_ts,
            ).exists()
        )

    def test_history_completes_ambiguous_outbound_create_edit_and_reaction(self):
        grant, conversation = self._live_conversation()
        slack_ts = "1787902025.000100"
        client_message_id = str(uuid.uuid4())
        outbound_create = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id="1" * 63 + "a",
            source_author_id="1" * 64,
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="ambiguous create",
            metadata={"client_msg_id": client_message_id},
            status=CommunityBridgeDeliveryStatus.PENDING,
            available_at=timezone.now(),
        )
        edit_echo_key = slack_dm_mirror._slack_echo_key(
            operation=CommunityBridgeDeliveryType.EDIT,
            target_message_id=slack_ts,
            author_id=grant.slack_user_id,
            text="edited in MLAI",
        )
        outbound_edit = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id="2" * 63 + "b",
            source_author_id="1" * 64,
            operation=CommunityBridgeDeliveryType.EDIT,
            encrypted_text="edited in MLAI",
            metadata={"slack_echo_key": edit_echo_key},
            status=CommunityBridgeDeliveryStatus.PENDING,
            available_at=timezone.now(),
        )
        reaction_id = slack_dm_mirror.reaction_object_id(
            message_id=slack_ts,
            reaction="thumbsup",
            author_id=grant.slack_user_id,
        )
        outbound_reaction = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id="3" * 63 + "c",
            source_author_id="1" * 64,
            operation=CommunityBridgeDeliveryType.REACTION_ADD,
            encrypted_text="👍",
            metadata={"slack_reaction_object_id": reaction_id},
            status=CommunityBridgeDeliveryStatus.PENDING,
            available_at=timezone.now(),
        )
        authority = slack_dm_mirror._SlackHistoryScanAuthority(
            epoch="ambiguous-history-epoch",
            participant_hash=conversation.participant_hash,
            mlai_channel_id=str(conversation.mlai_channel_id),
            registration_id="",
            registration_generation="",
            history_days=0,
            oldest="",
        )

        slack_dm_mirror._enqueue_history_message(
            conversation,
            {
                "ts": slack_ts,
                "client_msg_id": client_message_id,
                "user": grant.slack_user_id,
                "text": "edited in MLAI",
                "edited": {"ts": "1787902026.000100"},
                "reactions": [
                    {"name": "thumbsup", "users": [grant.slack_user_id]}
                ],
            },
            scan_authority=authority,
            held_until=timezone.now(),
        )

        for delivery in (outbound_create, outbound_edit, outbound_reaction):
            delivery.refresh_from_db()
            self.assertEqual(
                delivery.status,
                CommunityBridgeDeliveryStatus.COMPLETED,
            )
            self.assertEqual(delivery.encrypted_text, "")
        self.assertFalse(
            SlackDmMirrorDelivery.objects.filter(
                conversation=conversation,
                source_platform=CommunityBridgePlatform.SLACK,
            ).exists()
        )

    def test_partial_history_reaction_users_never_imply_removal(self):
        _, conversation = self._live_conversation()
        reaction = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id="reaction:historical",
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.REACTION_ADD,
            encrypted_text="",
            metadata={"reaction_object_id": "historical"},
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            completed_at=timezone.now(),
            available_at=timezone.now(),
        )

        slack_dm_mirror._mark_history_reconciliation_candidates_locked(conversation)

        reaction.refresh_from_db()
        self.assertNotIn("history_reconcile_candidate", reaction.metadata)

    @override_settings(SLACK_CLIENT_ID="client", SLACK_CLIENT_SECRET="secret")
    @patch("integrations.services.slack_dm_mirror.requests.post")
    def test_expiring_slack_token_rotates_and_fences_prior_authority(self, post):
        grant, _ = self._live_conversation()
        self.first_connection.refresh_token = "xoxe-old"
        self.first_connection.token_expires_at = timezone.now() - timedelta(minutes=1)
        self.first_connection.save(
            update_fields=("refresh_token", "token_expires_at", "updated_at")
        )
        grant.refresh_from_db()
        old_authority = slack_dm_mirror._capture_slack_grant_api_authority(
            grant,
            refresh_token=False,
        )
        response = MagicMock()
        response.json.return_value = {
            "ok": True,
            "team": {"id": "TMLAI"},
            "authed_user": {
                "id": "UONE",
                "access_token": "xoxp-new",
                "refresh_token": "xoxe-new",
                "expires_in": 3600,
                "scope": ",".join(SCOPES),
            },
        }
        post.return_value = response

        new_authority = slack_dm_mirror._capture_slack_grant_api_authority(grant)

        self.first_connection.refresh_from_db()
        self.assertEqual(self.first_connection.access_token, "xoxp-new")
        self.assertEqual(self.first_connection.refresh_token, "xoxe-new")
        self.assertGreater(
            new_authority.oauth_generation,
            old_authority.oauth_generation,
        )
        with transaction.atomic(), self.assertRaises(
            slack_dm_mirror.SlackDmMirrorAuthorizationError
        ):
            slack_dm_mirror._lock_slack_grant_api_authority(
                old_authority,
                required_scopes=slack_dm_mirror.DIRECT_DM_SCOPES,
            )

    @patch("integrations.services.slack_dm_mirror.BuzzBridgeClient.deliver_private")
    def test_permanent_adapter_failure_requires_explicit_recovery(self, deliver):
        grant, conversation = self._live_conversation()
        delivery = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id="1787902030.000100",
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="private body",
            metadata={"participant_hash": conversation.participant_hash},
            available_at=timezone.now(),
        )
        deliver.side_effect = BuzzBridgePermanentError("adapter rejected payload")

        self.assertEqual(process_ready_deliveries(limit=1), 0)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CommunityBridgeDeliveryStatus.DEAD)
        self.assertEqual(delivery.encrypted_text, "")
        self.assertTrue(delivery.metadata["permanent_failure"])
        self.assertEqual(slack_dm_mirror.recover_dead_backfill_deliveries(), 0)
        slack_dm_mirror._upsert_history_delivery(
            conversation,
            source_message_id=delivery.source_message_id,
            author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            text="must stay fenced",
            metadata={"backfill": True},
            held_until=timezone.now(),
        )
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CommunityBridgeDeliveryStatus.DEAD)
        self.assertEqual(delivery.encrypted_text, "")
        slack_dm_mirror._finish_history_scan(conversation)
        delivery.refresh_from_db()
        self.assertTrue(delivery.metadata["permanent_failure"])
        self.assertNotIn("history_recovery_superseded", delivery.metadata)

        self.assertEqual(backfill_grant(grant), 1)
        delivery.refresh_from_db()
        conversation.refresh_from_db()
        self.assertNotIn("permanent_failure", delivery.metadata)
        self.assertTrue(delivery.metadata["history_recovery_scheduled"])
        self.assertIsNone(conversation.history_backfilled_at)

    def test_app_rate_limit_schedules_current_state_reconciliation(self):
        _, conversation = self._live_conversation()
        conversation.history_backfilled_at = timezone.now()
        conversation.save(update_fields=("history_backfilled_at", "updated_at"))
        current = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id="1787902040.000100",
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="",
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            completed_at=timezone.now(),
            available_at=timezone.now(),
        )
        permanent = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id="1787902041.000100",
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="",
            metadata={
                "backfill": True,
                "permanent_failure": True,
                "history_recovery_scheduled": True,
            },
            status=CommunityBridgeDeliveryStatus.DEAD,
            available_at=timezone.now(),
        )

        result = ingest_slack_dm_event(
            {"type": "app_rate_limited", "team_id": "TMLAI"}
        )

        self.assertEqual(
            result,
            {"status": "history_reconciliation_queued", "count": 1},
        )
        conversation.refresh_from_db()
        current.refresh_from_db()
        permanent.refresh_from_db()
        self.assertIsNone(conversation.history_backfilled_at)
        self.assertTrue(current.metadata["history_reconcile_candidate"])
        self.assertTrue(permanent.metadata["permanent_failure"])
        self.assertTrue(permanent.metadata["history_recovery_scheduled"])

    def test_completed_mutation_echo_survives_slack_final_retry_jitter(self):
        grant, conversation = self._live_conversation()
        slack_ts = "1787902050.000100"
        echo_key = slack_dm_mirror._slack_echo_key(
            operation=CommunityBridgeDeliveryType.EDIT,
            target_message_id=slack_ts,
            author_id=grant.slack_user_id,
            text="edited in MLAI",
        )
        outbound = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id="f" * 64,
            source_author_id="1" * 64,
            operation=CommunityBridgeDeliveryType.EDIT,
            encrypted_text="",
            metadata={"slack_echo_key": echo_key},
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            completed_at=timezone.now(),
            available_at=timezone.now(),
        )
        SlackDmMirrorDelivery.objects.filter(pk=outbound.pk).update(
            updated_at=timezone.now() - timedelta(minutes=6)
        )

        result = ingest_slack_dm_event(
            {
                "event_id": "EvCompletedEditRetry",
                "team_id": "TMLAI",
                "event": {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "DONE",
                    "event_ts": "1787902051.000100",
                    "message": {
                        "ts": slack_ts,
                        "user": grant.slack_user_id,
                        "text": "edited in MLAI",
                    },
                },
            }
        )

        self.assertEqual(result, {"status": "echo_ignored", "count": 1})
        self.assertFalse(
            SlackDmMirrorDelivery.objects.filter(
                conversation=conversation,
                source_platform=CommunityBridgePlatform.SLACK,
            ).exists()
        )

    def test_history_completion_marks_an_inflight_dependency_reconciliation(self):
        _, conversation = self._live_conversation()
        delivery = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id="1787902060.000100",
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="in flight",
            metadata={"dependency_reconciliation_pending": True},
            status=CommunityBridgeDeliveryStatus.PROCESSING,
            available_at=timezone.now(),
        )

        slack_dm_mirror._finish_history_scan(conversation)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CommunityBridgeDeliveryStatus.PROCESSING)
        self.assertNotIn("dependency_reconciliation_pending", delivery.metadata)
        self.assertTrue(delivery.metadata["dependency_reconciliation_complete"])

    def test_reconciliation_candidates_and_scan_share_one_fixed_cutoff(self):
        grant, conversation = self._live_conversation()
        marker_now = int(timezone.now().timestamp())
        source_ts = f"{marker_now - grant.history_days * 86400 + 1}.000100"
        candidate = SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id=source_ts,
            source_author_id="UTWO",
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="",
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            completed_at=timezone.now(),
            available_at=timezone.now(),
        )
        with patch.object(slack_dm_mirror.time, "time", return_value=marker_now):
            slack_dm_mirror._mark_history_reconciliation_candidates_locked(
                conversation
            )
        candidate.refresh_from_db()
        expected_oldest = candidate.metadata[
            slack_dm_mirror.HISTORY_RECONCILE_OLDEST_KEY
        ]
        authority = slack_dm_mirror._capture_slack_grant_api_authority(grant)

        with patch.object(
            slack_dm_mirror.time,
            "time",
            return_value=marker_now + 3600,
        ):
            scan_authority, *_ = slack_dm_mirror._prepare_history_scan_page(
                conversation.pk,
                grant.pk,
                authority,
                slack_dm_mirror.DIRECT_DM_SCOPES,
            )

        self.assertEqual(scan_authority.oldest, expected_oldest)
