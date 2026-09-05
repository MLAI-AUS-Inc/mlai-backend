import threading
import time
import uuid
from unittest import skipUnless
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from django.utils import timezone

from community_chat.models import CommunityChatDevice, DeviceBindingStatus
from integrations.models import (
    CommunityBridgeIdentityLink,
    CommunityBridgeIdentityVerificationMethod,
    CommunityBridgePlatform,
    ExternalServiceConnection,
    ExternalServiceProvider,
    SlackDmMirrorConversation,
    SlackDmMirrorConversationStatus,
    SlackDmMirrorDelivery,
    SlackDmMirrorGrant,
    SlackDmMirrorGrantStatus,
)
from integrations.services import slack_dm_mirror

SCOPES = [
    "im:read",
    "im:history",
    "im:write",
    "chat:write",
    "users:read",
    "reactions:read",
    "reactions:write",
    "files:read",
    "mpim:read",
    "mpim:history",
    "mpim:write",
]


class SlackDmIoAuthorityFixture:
    def setUp(self):
        super().setUp()
        slack_dm_mirror._history_scan_available_at = 0.0
        self.user = get_user_model().objects.create_user(
            email="slack-io-authority@example.com"
        )
        self.owner_key = (
            "79be667ef9dcbbac55a06295ce870b070"
            "29bfcdb2dce28d959f2815b16f81798"
        )
        CommunityChatDevice.objects.create(
            user=self.user,
            public_key=self.owner_key,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        self.connection = ExternalServiceConnection.objects.create(
            user=self.user,
            provider=ExternalServiceProvider.SLACK,
            access_token="xoxp-io-authority",
            scopes=SCOPES,
            external_account_id="TIOAUTH",
            provider_metadata={
                "team": {"id": "TIOAUTH", "name": "I/O Authority"},
                "authed_user": {
                    "id": "UOWNER",
                    "scope": ",".join(SCOPES),
                },
            },
        )
        self.grant = SlackDmMirrorGrant.objects.create(
            user=self.user,
            connection=self.connection,
            slack_workspace_id="TIOAUTH",
            slack_user_id="UOWNER",
            consented_at=timezone.now(),
        )
        CommunityBridgeIdentityLink.objects.create(
            user=self.user,
            slack_workspace_id="TIOAUTH",
            slack_user_id="UOWNER",
            buzz_pubkey=self.owner_key,
            display_name="Owner",
            verification_method=(
                CommunityBridgeIdentityVerificationMethod.ACCOUNT_CHALLENGE
            ),
            verification_reference="slack-io-authority-test",
            verified_at=timezone.now(),
        )
        self.conversation = SlackDmMirrorConversation.objects.create(
            grant=self.grant,
            slack_workspace_id="TIOAUTH",
            slack_conversation_id="DIOAUTH",
            participant_slack_ids=["UOWNER", "UOTHER"],
            participant_profiles={
                "UOWNER": {"display_name": "Owner"},
                "UOTHER": {"display_name": "Other"},
            },
            participant_hash="a" * 64,
            mlai_channel_id=uuid.uuid4(),
            status=SlackDmMirrorConversationStatus.LIVE,
        )


class SlackDmIoAuthorityTests(SlackDmIoAuthorityFixture, TransactionTestCase):
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_exact_token_change_rejects_call_before_client_construction(
        self,
        web_client,
    ):
        authority = slack_dm_mirror._capture_slack_grant_api_authority(self.grant)
        self.connection.access_token = "xoxp-replaced"
        self.connection.save(update_fields=("access_token", "updated_at"))

        with self.assertRaises(slack_dm_mirror.SlackDmMirrorAuthorizationError):
            slack_dm_mirror._call_slack_with_grant_authority(
                authority,
                "users_list",
                required_scopes=slack_dm_mirror.DIRECT_DM_SCOPES,
                limit=200,
                cursor="",
            )

        web_client.assert_not_called()

    def test_disconnect_winning_after_list_response_blocks_discovery_final_save(self):
        def disconnect_then_return(*_args, **_kwargs):
            slack_dm_mirror.revoke_user_grant(self.user)
            return {
                "channels": [],
                "response_metadata": {"next_cursor": ""},
            }

        with (
            patch(
                "integrations.services.slack_dm_mirror._call_slack_with_grant_authority",
                side_effect=disconnect_then_return,
            ),
            patch("integrations.services.slack_dm_mirror._revoke_remote_token"),
            patch("integrations.services.slack_dm_mirror._finish_grant_registration_revoke"),
            self.assertRaises(slack_dm_mirror.SlackDmMirrorAuthorizationError),
        ):
            slack_dm_mirror.discover_conversations(self.grant)

        self.grant.refresh_from_db()
        self.assertEqual(self.grant.status, SlackDmMirrorGrantStatus.REVOKED)
        self.assertIsNone(self.grant.last_discovery_at)

    def test_disconnect_winning_after_profile_error_blocks_stale_error_save(self):
        self.conversation.last_error = "before-discovery"
        self.conversation.participant_profiles = {}
        self.conversation.save(
            update_fields=("last_error", "participant_profiles", "updated_at")
        )

        def slack_call(_authority, method, **_kwargs):
            if method == "conversations_list":
                return {
                    "channels": [{"id": "DIOAUTH", "user": "UOTHER"}],
                    "response_metadata": {"next_cursor": ""},
                }
            if method == "users_info":
                slack_dm_mirror.revoke_user_grant(self.user)
                raise RuntimeError("profile failed after disconnect")
            raise AssertionError(f"unexpected Slack method {method}")

        with (
            patch(
                "integrations.services.slack_dm_mirror._call_slack_with_grant_authority",
                side_effect=slack_call,
            ),
            patch("integrations.services.slack_dm_mirror._revoke_remote_token"),
            patch("integrations.services.slack_dm_mirror._finish_grant_registration_revoke"),
        ):
            self.assertEqual(slack_dm_mirror.discover_conversations(self.grant), 0)

        self.conversation.refresh_from_db()
        self.assertEqual(
            self.conversation.status,
            SlackDmMirrorConversationStatus.PAUSED,
        )
        self.assertEqual(self.conversation.last_error, "before-discovery")


@skipUnless(
    connection.features.has_select_for_update,
    "Requires PostgreSQL row locks to verify the production Slack I/O fence",
)
class SlackDmIoAuthorityTransactionTests(
    SlackDmIoAuthorityFixture,
    TransactionTestCase,
):
    def _wait_until_backend_is_lock_waiting(self, backend_pid: int) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                    [backend_pid],
                )
                row = cursor.fetchone()
            if row and row[0] == "Lock":
                return
            time.sleep(0.02)
        self.fail("disconnect backend never entered a database lock wait")

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_bounded_restart_wins_before_old_page_persistence(self, web_client):
        persist_started = threading.Event()
        allow_persist = threading.Event()
        scan_results: list[int] = []
        errors: list[BaseException] = []
        original_persist = slack_dm_mirror._persist_history_page
        web_client.return_value.conversations_history.return_value = {
            "messages": [
                {
                    "ts": "1787900000.000100",
                    "user": "UOTHER",
                    "text": "old bounded response",
                }
            ],
            "has_more": False,
            "response_metadata": {"next_cursor": ""},
        }

        def blocked_persist(*args, **kwargs):
            persist_started.set()
            if not allow_persist.wait(timeout=5):
                raise RuntimeError("test timed out waiting to persist history")
            return original_persist(*args, **kwargs)

        def scan_history():
            close_old_connections()
            try:
                scan_results.append(
                    slack_dm_mirror.process_due_history_backfills(limit=1)
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                connection.close()

        with patch.object(
            slack_dm_mirror,
            "_persist_history_page",
            side_effect=blocked_persist,
        ):
            scan_thread = threading.Thread(target=scan_history)
            scan_thread.start()
            self.assertTrue(persist_started.wait(timeout=5))
            close_old_connections()
            grant = SlackDmMirrorGrant.objects.get(pk=self.grant.pk)
            slack_dm_mirror.backfill_grant(grant, full_history=True)
            allow_persist.set()
            scan_thread.join(timeout=5)
            close_old_connections()

        self.assertFalse(scan_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(scan_results, [0])
        self.grant.refresh_from_db()
        self.conversation.refresh_from_db()
        self.assertEqual(self.grant.history_days, 30)
        self.assertIsNone(self.conversation.history_backfilled_at)
        self.assertFalse(
            SlackDmMirrorDelivery.objects.filter(
                conversation=self.conversation,
                source_message_id="1787900000.000100",
            ).exists()
        )

    @patch("integrations.services.slack_dm_mirror._finish_grant_registration_revoke")
    @patch("integrations.services.slack_dm_mirror._revoke_remote_token")
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_disconnect_waits_for_history_call_then_erases_or_rejects_response(
        self,
        web_client,
        _revoke_remote,
        _finish_cleanup,
    ):
        history_started = threading.Event()
        allow_history_to_finish = threading.Event()
        revoke_started = threading.Event()
        revoke_returned = threading.Event()
        revoke_backend_pid: list[int] = []
        errors: list[BaseException] = []
        client = MagicMock()

        def blocked_history(**_kwargs):
            history_started.set()
            if not allow_history_to_finish.wait(timeout=5):
                raise RuntimeError("test timed out waiting to release Slack history")
            return {
                "messages": [
                    {
                        "ts": "1787900000.000100",
                        "user": "UOTHER",
                        "text": "must be erased if it briefly persists",
                    }
                ],
                "response_metadata": {"next_cursor": "next-page"},
            }

        client.conversations_history.side_effect = blocked_history
        web_client.return_value = client

        def scan_history():
            close_old_connections()
            try:
                slack_dm_mirror.process_due_history_backfills(limit=1)
            except BaseException as exc:
                errors.append(exc)
            finally:
                connection.close()

        def revoke():
            close_old_connections()
            try:
                connection.ensure_connection()
                revoke_backend_pid.append(connection.connection.info.backend_pid)
                revoke_started.set()
                user = get_user_model().objects.get(pk=self.user.pk)
                slack_dm_mirror.revoke_user_grant(user)
            except BaseException as exc:
                errors.append(exc)
            finally:
                revoke_returned.set()
                connection.close()

        scan_thread = threading.Thread(target=scan_history)
        scan_thread.start()
        self.assertTrue(history_started.wait(timeout=5))
        revoke_thread = threading.Thread(target=revoke)
        revoke_thread.start()
        self.assertTrue(revoke_started.wait(timeout=5))
        self._wait_until_backend_is_lock_waiting(revoke_backend_pid[0])
        self.assertFalse(revoke_returned.is_set())

        allow_history_to_finish.set()
        scan_thread.join(timeout=5)
        revoke_thread.join(timeout=5)
        self.assertFalse(scan_thread.is_alive())
        self.assertFalse(revoke_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(revoke_returned.is_set())

        self.grant.refresh_from_db()
        self.connection.refresh_from_db()
        self.assertEqual(self.grant.status, SlackDmMirrorGrantStatus.REVOKED)
        self.assertEqual(self.connection.access_token, "")
        private_rows = SlackDmMirrorDelivery.objects.filter(
            conversation=self.conversation,
            source_platform=CommunityBridgePlatform.SLACK,
        )
        self.assertFalse(private_rows.exclude(encrypted_text="").exists())
        self.assertEqual(client.conversations_history.call_count, 1)
        self.assertEqual(slack_dm_mirror.process_due_history_backfills(limit=1), 0)
        self.assertEqual(client.conversations_history.call_count, 1)

    @patch("integrations.services.slack_dm_mirror._finish_grant_registration_revoke")
    @patch("integrations.services.slack_dm_mirror._revoke_remote_token")
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_disconnect_waits_for_profile_call_and_blocks_next_start_dm_call(
        self,
        web_client,
        _revoke_remote,
        _finish_cleanup,
    ):
        profile_started = threading.Event()
        allow_profile_to_finish = threading.Event()
        revoke_started = threading.Event()
        revoke_returned = threading.Event()
        revoke_backend_pid: list[int] = []
        errors: list[BaseException] = []
        client = MagicMock()

        def blocked_profile(*, user):
            profile_started.set()
            if not allow_profile_to_finish.wait(timeout=5):
                raise RuntimeError("test timed out waiting to release Slack profile")
            return {
                "user": {
                    "id": user,
                    "team_id": "TIOAUTH",
                    "name": "other",
                    "profile": {"display_name": "Other"},
                }
            }

        client.users_info.side_effect = blocked_profile
        web_client.return_value = client

        def start_dm():
            close_old_connections()
            try:
                grant = SlackDmMirrorGrant.objects.select_related("connection").get(
                    pk=self.grant.pk
                )
                slack_dm_mirror.open_slack_dm(
                    grant,
                    slack_user_ids=["UNEW"],
                    authenticated_public_key=self.owner_key,
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                connection.close()

        def revoke():
            close_old_connections()
            try:
                connection.ensure_connection()
                revoke_backend_pid.append(connection.connection.info.backend_pid)
                revoke_started.set()
                user = get_user_model().objects.get(pk=self.user.pk)
                slack_dm_mirror.revoke_user_grant(user)
            except BaseException as exc:
                errors.append(exc)
            finally:
                revoke_returned.set()
                connection.close()

        start_thread = threading.Thread(target=start_dm)
        start_thread.start()
        self.assertTrue(profile_started.wait(timeout=5))
        revoke_thread = threading.Thread(target=revoke)
        revoke_thread.start()
        self.assertTrue(revoke_started.wait(timeout=5))
        self._wait_until_backend_is_lock_waiting(revoke_backend_pid[0])
        self.assertFalse(revoke_returned.is_set())

        allow_profile_to_finish.set()
        start_thread.join(timeout=5)
        revoke_thread.join(timeout=5)
        self.assertFalse(start_thread.is_alive())
        self.assertFalse(revoke_thread.is_alive())
        self.assertTrue(revoke_returned.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(
            errors[0],
            slack_dm_mirror.SlackDmMirrorAuthorizationError,
        )
        client.users_info.assert_called_once_with(user="UNEW")
        client.conversations_open.assert_not_called()
        self.assertFalse(
            SlackDmMirrorConversation.objects.filter(
                grant=self.grant,
                slack_conversation_id__in=("DNEW", "GNEW"),
            ).exists()
        )
