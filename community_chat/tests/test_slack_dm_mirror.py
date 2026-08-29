import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

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
from integrations.services import slack_dm_mirror
from integrations.services.slack_dm_mirror import (
    activate_connection,
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
        self.assertIn("direct and group Slack DMs", response.data["consent"]["summary"])
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
        SlackDmMirrorConversation.objects.create(
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
    def test_adapter_outage_revokes_locally_but_requires_a_successful_retry(
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

        failed_response = self.client.delete(self.url)

        self.assertEqual(failed_response.status_code, 500)
        grant.refresh_from_db()
        connection.refresh_from_db()
        conversation.refresh_from_db()
        self.assertEqual(grant.status, SlackDmMirrorGrantStatus.REVOKED)
        self.assertIn("revocation is pending", grant.last_error)
        self.assertEqual(connection.status, ExternalServiceConnectionStatus.DISCONNECTED)
        self.assertEqual(connection.access_token, "")
        self.assertEqual(
            conversation.status,
            SlackDmMirrorConversationStatus.PAUSED,
        )
        web_client.return_value.auth_revoke.assert_called_once_with()

        unregister_private_conversation.side_effect = None
        retry_response = self.client.delete(self.url)

        self.assertEqual(retry_response.status_code, 204)
        self.assertEqual(unregister_private_conversation.call_count, 2)
        web_client.return_value.auth_revoke.assert_called_once_with()
        grant.refresh_from_db()
        self.assertEqual(grant.last_error, "")

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
        SlackDmMirrorConversation.objects.create(
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
        self.assertEqual(connection.status, ExternalServiceConnectionStatus.DISCONNECTED)
        self.assertEqual(connection.access_token, "")
        self.assertEqual(connection.provider_metadata, {})
        self.assertEqual(connection.sync_cursor, {})

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

    def test_full_history_action_marks_live_conversations_due_without_sync_io(self):
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
        self.assertEqual(grant.history_days, 0)
        self.assertIsNone(conversation.history_backfilled_at)
        self.assertEqual(conversation.oldest_synced_ts, "")
        self.assertTrue(response.data["privacy"]["full_history"])
        self.assertFalse(response.data["privacy"]["history_is_bounded"])


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

    def test_reauthorization_preserves_explicit_full_history_setting(self):
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
        self.assertEqual(grant.history_days, 0)

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
        queued = list(
            SlackDmMirrorDelivery.objects.filter(conversation=conversation).order_by(
                "id"
            )
        )
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
                    "members": ["UONE", "UTWO", "UTHREE"],
                }
            ],
            "response_metadata": {"next_cursor": ""},
        }
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
        self.assertEqual(conversation.deliveries.count(), 1)
        client.conversations_list.assert_called_once_with(
            types="im,mpim",
            exclude_archived=True,
            limit=200,
            cursor="",
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
        self.assertEqual(conversation.deliveries.count(), 1)

        backfill_grant(conversation.grant)
        conversation.refresh_from_db()
        self.assertIsNone(conversation.history_backfilled_at)
        self.assertEqual(process_due_history_backfills(), 1)
        conversation.refresh_from_db()

        self.assertGreaterEqual(conversation.history_backfilled_at, first_marker)
        self.assertEqual(conversation.deliveries.count(), 1)

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

    def test_live_slack_event_fans_out_to_each_independent_owner_mirror(self):
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
                "event": {
                    "channel": "DONE",
                    "ts": "1787900100.000100",
                    "user": "UONE",
                    "text": "visible in each owner's private copy",
                },
            }
        )

        self.assertEqual(result["status"], "enqueued")
        self.assertEqual(len(result["delivery_ids"]), 2)
        self.assertEqual(SlackDmMirrorDelivery.objects.count(), 2)

    def test_unknown_dm_event_queues_owner_grants_for_immediate_rediscovery(self):
        grant = SlackDmMirrorGrant.objects.create(
            user=self.first,
            connection=self.first_connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            consented_at=timezone.now(),
            last_discovery_at=timezone.now(),
        )

        result = ingest_slack_dm_event(
            {
                "team_id": "TMLAI",
                "event": {
                    "channel": "DNEW",
                    "ts": "1787900200.000100",
                    "user": "UTWO",
                    "text": "first message in a newly opened DM",
                },
            }
        )

        self.assertEqual(result["status"], "discovery_queued")
        grant.refresh_from_db()
        self.assertIsNone(grant.last_discovery_at)

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
            },
        )
        web_client.return_value.conversations_replies.assert_called_with(
            channel="DONE",
            ts=root_ts,
            limit=200,
            cursor="reply-page-2",
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
    def test_full_history_is_paged_without_oldest_and_requeues_dead_rows(
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
        self.assertEqual(conversation.deliveries.count(), 3)
        first_kwargs = web_client.return_value.conversations_history.call_args_list[
            0
        ].kwargs
        second_kwargs = web_client.return_value.conversations_history.call_args_list[
            1
        ].kwargs
        self.assertNotIn("oldest", first_kwargs)
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
        SlackDmMirrorConversation.objects.create(
            grant=grant,
            slack_workspace_id="TMLAI",
            slack_conversation_id="DONE",
            participant_slack_ids=["UONE", "UTWO"],
            participant_buzz_pubkeys=["1" * 64, "3" * 64],
            participant_identity_map={"UONE": "1" * 64, "UTWO": "3" * 64},
            mlai_channel_id=channel_id,
            status=SlackDmMirrorConversationStatus.LIVE,
        )
        web_client.return_value.chat_postMessage.side_effect = [
            RuntimeError("transient"),
            {"ts": "1787901200.000100"},
        ]
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
            conversation.deliveries.filter(
                source_platform=CommunityBridgePlatform.BUZZ
            ).count(),
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
