import threading
import time
from unittest import skipUnless
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.db import DatabaseError, close_old_connections, connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from integrations.api_views_connectors import (
    SlackChannelListView,
    SlackChannelSelectionView,
)
from integrations.services import external_connectors, slack_dm_mirror
from organizations.models import Organization
from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceConnectionStatus,
    ExternalServiceProvider,
    SlackDmMirrorGrant,
    SlackDmMirrorGrantStatus,
)
from integrations.services.external_connectors import (
    ConnectorOAuthError,
    ConnectorRateLimitError,
    mark_sources_sync_requested,
    serialize_slack_channels,
    sync_slack_connection,
    sync_slack_connection_page,
    update_slack_channel_selections,
)
from integrations.services.slack_dm_mirror import (
    revoke_connection_grant,
    revoke_user_grant,
)
from startup_updates.models import (
    SlackChannelSelection,
    SlackMessageArtifact,
    SlackThreadArtifact,
    UserStartupBinding,
)

User = get_user_model()


CHANNELS_PAYLOAD = {
    "ok": True,
    "channels": [
        {
            "id": "C123",
            "name": "investor-updates",
            "is_private": False,
        }
    ],
    "response_metadata": {"next_cursor": ""},
}
HISTORY_PAYLOAD = {
    "ok": True,
    "messages": [
        {
            "ts": "1770000000.000100",
            "text": "Customer launch is ready.",
            "user": "U123",
            "reply_count": 0,
        }
    ],
    "response_metadata": {"next_cursor": ""},
}


class SlackConnectorAuthorityMixin:
    def create_slack_connection(self, *, with_selection=False):
        user = User.objects.create_user(
            email=f"founder-{User.objects.count()}@example.com",
            role="participant",
        )
        organization = Organization.objects.create(
            name=f"Acme {user.pk}",
            domain=f"acme-{user.pk}.example",
        )
        slack_connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.SLACK,
            user=user,
            organization=organization,
            access_token="slack-user-token",
            refresh_token="slack-refresh-token",
            external_account_id="T123",
            account_label="Acme Slack",
            status=ExternalServiceConnectionStatus.CONNECTED,
        )
        selection = None
        if with_selection:
            selection = SlackChannelSelection.objects.create(
                connection=slack_connection,
                user=user,
                organization=organization,
                channel_id="C123",
                channel_name="investor-updates",
                selected=True,
            )
        return user, organization, slack_connection, selection

    def revoke_user_without_remote_io(self, user):
        with patch(
            "integrations.services.slack_dm_mirror._revoke_remote_token"
        ):
            revoke_user_grant(user)


class SlackConnectorAuthorityTests(SlackConnectorAuthorityMixin, TestCase):
    @staticmethod
    def slack_oauth_payload(*, workspace_id="T123", slack_user_id="U123"):
        return {
            "access_token": f"xoxb-{workspace_id}",
            "team": {"id": workspace_id, "name": "Acme Slack"},
            "authed_user": {
                "id": slack_user_id,
                "access_token": f"xoxp-{slack_user_id}",
                "scope": "channels:read,channels:history",
            },
        }

    def test_cross_org_workspace_authority_rejects_a_different_slack_user(self):
        user, first_organization, first_connection, _selection = (
            self.create_slack_connection()
        )
        first_connection.provider_metadata = {
            "team": {"id": "T123"},
            "authed_user": {"id": "UORIGINAL"},
        }
        first_connection.save(update_fields=("provider_metadata", "updated_at"))
        grant = SlackDmMirrorGrant.objects.create(
            user=user,
            connection=first_connection,
            slack_workspace_id="T123",
            slack_user_id="UORIGINAL",
            consented_at=timezone.now(),
        )
        second_organization = Organization.objects.create(
            name="Second startup",
            domain=f"second-{user.pk}.example",
        )
        UserStartupBinding.objects.bulk_create(
            [
                UserStartupBinding(
                    user=user,
                    organization=first_organization,
                    role="founder",
                ),
                UserStartupBinding(
                    user=user,
                    organization=second_organization,
                    role="founder",
                ),
            ]
        )

        with (
            patch(
                "integrations.services.external_connectors.requests.post"
            ) as revoke_token,
            self.assertRaisesRegex(ConnectorOAuthError, "different Slack identity"),
        ):
            external_connectors._store_slack_connection(
                user,
                second_organization,
                self.slack_oauth_payload(slack_user_id="UOTHER"),
                expected_generation=0,
            )

        revoke_token.assert_called_once()
        first_connection.refresh_from_db()
        grant.refresh_from_db()
        self.assertEqual(first_connection.access_token, "slack-user-token")
        self.assertEqual(grant.status, SlackDmMirrorGrantStatus.ACTIVE)
        self.assertFalse(
            ExternalServiceConnection.objects.filter(
                user=user,
                organization=second_organization,
                provider=ExternalServiceProvider.SLACK,
            ).exists()
        )

    def test_duplicate_legacy_rows_cannot_hide_a_different_active_identity(self):
        user, _organization, first_legacy, _selection = (
            self.create_slack_connection()
        )
        first_legacy.organization = None
        first_legacy.save(update_fields=("organization", "updated_at"))
        active_legacy = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.SLACK,
            user=user,
            organization=None,
            access_token="active-legacy-token",
            refresh_token="active-legacy-refresh",
            external_account_id="T123",
            account_label="Active legacy Slack",
            status=ExternalServiceConnectionStatus.CONNECTED,
        )
        grant = SlackDmMirrorGrant.objects.create(
            user=user,
            connection=active_legacy,
            slack_workspace_id="T123",
            slack_user_id="UORIGINAL",
            consented_at=timezone.now(),
        )

        with (
            patch(
                "integrations.services.external_connectors.requests.post"
            ) as revoke_token,
            self.assertRaisesRegex(ConnectorOAuthError, "different Slack identity"),
        ):
            external_connectors._store_slack_connection(
                user,
                None,
                self.slack_oauth_payload(slack_user_id="UOTHER"),
                expected_generation=0,
            )

        revoke_token.assert_called_once()
        first_legacy.refresh_from_db()
        active_legacy.refresh_from_db()
        grant.refresh_from_db()
        self.assertEqual(first_legacy.access_token, "slack-user-token")
        self.assertEqual(active_legacy.access_token, "active-legacy-token")
        self.assertEqual(grant.status, SlackDmMirrorGrantStatus.ACTIVE)
        self.assertEqual(
            ExternalServiceConnection.objects.filter(
                user=user,
                organization=None,
                provider=ExternalServiceProvider.SLACK,
                external_account_id="T123",
            ).count(),
            2,
        )

    def test_partial_revocation_does_not_free_a_workspace_identity(self):
        user, _organization, connection_row, _selection = (
            self.create_slack_connection()
        )
        connection_row.organization = None
        connection_row.save(update_fields=("organization", "updated_at"))
        grant = SlackDmMirrorGrant.objects.create(
            user=user,
            connection=connection_row,
            slack_workspace_id="T123",
            slack_user_id="UORIGINAL",
            status=SlackDmMirrorGrantStatus.REVOKED,
            revoked_at=None,
            consented_at=timezone.now(),
        )

        with (
            patch(
                "integrations.services.external_connectors.requests.post"
            ) as revoke_token,
            self.assertRaisesRegex(ConnectorOAuthError, "different Slack identity"),
        ):
            external_connectors._store_slack_connection(
                user,
                None,
                self.slack_oauth_payload(slack_user_id="UOTHER"),
                expected_generation=0,
            )

        revoke_token.assert_called_once()
        connection_row.refresh_from_db()
        grant.refresh_from_db()
        self.assertEqual(connection_row.external_account_id, "T123")
        self.assertEqual(connection_row.access_token, "slack-user-token")
        self.assertIsNone(grant.revoked_at)

    def test_cross_org_workspace_authority_allows_the_same_slack_user(self):
        user, first_organization, first_connection, _selection = (
            self.create_slack_connection()
        )
        grant = SlackDmMirrorGrant.objects.create(
            user=user,
            connection=first_connection,
            slack_workspace_id="T123",
            slack_user_id="U123",
            consented_at=timezone.now(),
        )
        second_organization = Organization.objects.create(
            name="Same identity startup",
            domain=f"same-identity-{user.pk}.example",
        )
        UserStartupBinding.objects.bulk_create(
            [
                UserStartupBinding(
                    user=user,
                    organization=first_organization,
                    role="founder",
                ),
                UserStartupBinding(
                    user=user,
                    organization=second_organization,
                    role="founder",
                ),
            ]
        )

        connected = external_connectors._store_slack_connection(
            user,
            second_organization,
            self.slack_oauth_payload(slack_user_id="U123"),
            expected_generation=0,
        )

        self.assertNotEqual(connected.pk, first_connection.pk)
        self.assertEqual(connected.organization_id, second_organization.pk)
        self.assertEqual(connected.external_account_id, "T123")
        self.assertEqual(connected.access_token, "xoxp-U123")
        first_connection.refresh_from_db()
        grant.refresh_from_db()
        self.assertEqual(first_connection.access_token, "slack-user-token")
        self.assertEqual(grant.status, SlackDmMirrorGrantStatus.ACTIVE)

    def test_slack_channel_endpoints_map_disconnect_races_to_401(self):
        user, _organization, _connection, _selection = (
            self.create_slack_connection()
        )
        factory = APIRequestFactory()
        oauth_error = ConnectorOAuthError(
            "Slack authorization changed while the connector request was in progress."
        )

        list_request = factory.get("/api/v1/integrations/slack/channels")
        force_authenticate(list_request, user=user)
        with patch(
            "integrations.api_views_connectors.serialize_slack_channels",
            side_effect=oauth_error,
        ):
            list_response = SlackChannelListView.as_view()(list_request)

        selection_request = factory.post(
            "/api/v1/integrations/slack/channel-selections",
            {"channelIds": ["C123"]},
            format="json",
        )
        force_authenticate(selection_request, user=user)
        with patch(
            "integrations.api_views_connectors.update_slack_channel_selections",
            side_effect=oauth_error,
        ):
            selection_response = SlackChannelSelectionView.as_view()(
                selection_request
            )

        self.assertEqual(list_response.status_code, 401)
        self.assertEqual(selection_response.status_code, 401)
        self.assertIn("authorization changed", list_response.data["detail"])
        self.assertIn("authorization changed", selection_response.data["detail"])

    def test_slack_channel_endpoints_map_transient_failures(self):
        user, _organization, _connection, _selection = (
            self.create_slack_connection()
        )
        factory = APIRequestFactory()
        cases = (
            (ConnectorRateLimitError(17), 429),
            (requests.RequestException("Slack network failed"), 502),
            (DatabaseError("database unavailable"), 503),
        )

        for exc, expected_status in cases:
            with self.subTest(view="list", exception=exc.__class__.__name__):
                request = factory.get("/api/v1/integrations/slack/channels")
                force_authenticate(request, user=user)
                with patch(
                    "integrations.api_views_connectors.serialize_slack_channels",
                    side_effect=exc,
                ):
                    response = SlackChannelListView.as_view()(request)
                self.assertEqual(response.status_code, expected_status)

            with self.subTest(
                view="selection", exception=exc.__class__.__name__
            ):
                request = factory.post(
                    "/api/v1/integrations/slack/channel-selections",
                    {"channelIds": ["C123"]},
                    format="json",
                )
                force_authenticate(request, user=user)
                with patch(
                    "integrations.api_views_connectors.update_slack_channel_selections",
                    side_effect=exc,
                ):
                    response = SlackChannelSelectionView.as_view()(request)
                self.assertEqual(response.status_code, expected_status)

    def test_channel_discovery_and_selection_update_use_current_authority(self):
        user, organization, slack_connection, _selection = (
            self.create_slack_connection()
        )
        with patch(
            "integrations.services.external_connectors._slack_api_request",
            return_value=CHANNELS_PAYLOAD,
        ):
            discovery = serialize_slack_channels(user, organization=organization)

        self.assertEqual([item["channelId"] for item in discovery["channels"]], ["C123"])
        selection = SlackChannelSelection.objects.get(connection=slack_connection)
        self.assertFalse(selection.selected)

        updated = update_slack_channel_selections(
            user,
            ["C123"],
            organization=organization,
        )

        selection.refresh_from_db()
        self.assertTrue(selection.selected)
        self.assertEqual(updated["selectedChannelCount"], 1)

    def test_paged_sync_persists_messages_threads_and_cursor(self):
        _user, _organization, slack_connection, selection = (
            self.create_slack_connection(with_selection=True)
        )
        with patch(
            "integrations.services.external_connectors._slack_api_request",
            return_value=HISTORY_PAYLOAD,
        ):
            result = sync_slack_connection_page(
                slack_connection,
                run_id="startup-update-1",
            )

        self.assertEqual(result["status"], "synced")
        self.assertEqual(result["messagesSynced"], 1)
        self.assertEqual(result["threadsTouched"], 1)
        self.assertTrue(
            SlackMessageArtifact.objects.filter(connection=slack_connection).exists()
        )
        self.assertTrue(
            SlackThreadArtifact.objects.filter(connection=slack_connection).exists()
        )
        selection.refresh_from_db()
        self.assertTrue(selection.sync_cursor["run_backfill_complete"])
        self.assertEqual(selection.sync_cursor["startup_update_run_id"], "startup-update-1")

    def test_completed_disconnect_blocks_channel_discovery_before_slack_call(self):
        user, organization, stale_connection, _selection = (
            self.create_slack_connection()
        )
        self.revoke_user_without_remote_io(user)

        with (
            patch(
                "integrations.services.external_connectors._latest_slack_connection",
                return_value=stale_connection,
            ),
            patch(
                "integrations.services.external_connectors._slack_api_request"
            ) as mock_slack_api,
        ):
            with self.assertRaises(ConnectorOAuthError):
                serialize_slack_channels(user, organization=organization)

        mock_slack_api.assert_not_called()
        self.assertFalse(
            SlackChannelSelection.objects.filter(
                connection_id=stale_connection.pk
            ).exists()
        )

    def test_completed_disconnect_blocks_selection_update(self):
        user, organization, stale_connection, _selection = (
            self.create_slack_connection()
        )
        self.revoke_user_without_remote_io(user)

        with patch(
            "integrations.services.external_connectors._latest_slack_connection",
            return_value=stale_connection,
        ):
            with self.assertRaises(ConnectorOAuthError):
                update_slack_channel_selections(
                    user,
                    ["C123"],
                    organization=organization,
                )

        self.assertFalse(
            SlackChannelSelection.objects.filter(
                connection_id=stale_connection.pk
            ).exists()
        )

    def test_completed_disconnect_blocks_full_sync_before_slack_call(self):
        user, _organization, stale_connection, _selection = (
            self.create_slack_connection(with_selection=True)
        )
        self.revoke_user_without_remote_io(user)

        with patch(
            "integrations.services.external_connectors._slack_api_request"
        ) as mock_slack_api:
            with self.assertRaises(ConnectorOAuthError):
                sync_slack_connection(stale_connection)

        mock_slack_api.assert_not_called()
        self.assertFalse(
            SlackMessageArtifact.objects.filter(
                connection_id=stale_connection.pk
            ).exists()
        )
        self.assertFalse(
            SlackThreadArtifact.objects.filter(
                connection_id=stale_connection.pk
            ).exists()
        )

    def test_completed_disconnect_blocks_paged_sync_before_slack_call(self):
        user, _organization, stale_connection, _selection = (
            self.create_slack_connection(with_selection=True)
        )
        self.revoke_user_without_remote_io(user)

        with patch(
            "integrations.services.external_connectors._slack_api_request"
        ) as mock_slack_api:
            with self.assertRaises(ConnectorOAuthError):
                sync_slack_connection_page(
                    stale_connection,
                    run_id="startup-update-1",
                )

        mock_slack_api.assert_not_called()
        self.assertFalse(
            SlackMessageArtifact.objects.filter(
                connection_id=stale_connection.pk
            ).exists()
        )
        self.assertFalse(
            SlackThreadArtifact.objects.filter(
                connection_id=stale_connection.pk
            ).exists()
        )

    def test_disconnect_erases_generic_slack_selections_messages_and_threads(self):
        user, _organization, slack_connection, _selection = (
            self.create_slack_connection(with_selection=True)
        )
        with patch(
            "integrations.services.external_connectors._slack_api_request",
            return_value=HISTORY_PAYLOAD,
        ):
            result = sync_slack_connection(slack_connection)

        self.assertEqual(result["messagesSynced"], 1)
        self.assertTrue(
            SlackChannelSelection.objects.filter(connection=slack_connection).exists()
        )
        self.assertTrue(
            SlackMessageArtifact.objects.filter(connection=slack_connection).exists()
        )
        self.assertTrue(
            SlackThreadArtifact.objects.filter(connection=slack_connection).exists()
        )

        self.revoke_user_without_remote_io(user)

        slack_connection.refresh_from_db()
        self.assertEqual(
            slack_connection.status,
            ExternalServiceConnectionStatus.DISCONNECTED,
        )
        self.assertEqual(slack_connection.access_token, "")
        self.assertEqual(slack_connection.refresh_token, "")
        self.assertFalse(
            SlackChannelSelection.objects.filter(connection=slack_connection).exists()
        )
        self.assertFalse(
            SlackMessageArtifact.objects.filter(connection=slack_connection).exists()
        )
        self.assertFalse(
            SlackThreadArtifact.objects.filter(connection=slack_connection).exists()
        )

    def test_sync_orchestrator_does_not_replace_disconnect_with_stale_error(self):
        user, organization, slack_connection, _selection = (
            self.create_slack_connection(with_selection=True)
        )

        def disconnect_then_fail(_stale_connection):
            self.revoke_user_without_remote_io(user)
            raise ConnectorOAuthError("Slack authority was revoked")

        with patch(
            "integrations.services.external_connectors.sync_slack_connection",
            side_effect=disconnect_then_fail,
        ):
            result = mark_sources_sync_requested(
                user,
                providers=[ExternalServiceProvider.SLACK],
                organization=organization,
            )

        self.assertEqual(result["syncRuns"][0]["status"], "error")
        slack_connection.refresh_from_db()
        self.assertEqual(
            slack_connection.status,
            ExternalServiceConnectionStatus.DISCONNECTED,
        )
        self.assertEqual(slack_connection.access_token, "")
        self.assertEqual(slack_connection.last_error, "")


@skipUnless(
    connection.vendor == "postgresql",
    "Requires PostgreSQL row-lock concurrency semantics.",
)
class SlackConnectorAuthorityPostgresTests(
    SlackConnectorAuthorityMixin,
    TransactionTestCase,
):
    reset_sequences = True

    def _postgres_backend_pid(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            return int(cursor.fetchone()[0])

    def _assert_backend_waits_for_row_lock(self, backend_pid, *, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_blocking_pids(%s)", [backend_pid])
                blocking_pids = cursor.fetchone()[0]
            if blocking_pids:
                return blocking_pids
        self.fail(
            f"PostgreSQL backend {backend_pid} never contended for the authority lock"
        )

    def _run_in_thread(self, callback, errors, completed):
        close_old_connections()
        try:
            callback()
        except Exception as exc:  # pragma: no cover - asserted by the caller
            errors.append(exc)
        finally:
            connection.close()
            completed.set()

    def test_user_disconnect_waits_for_channel_discovery_then_erases_selection(self):
        user, organization, slack_connection, _selection = (
            self.create_slack_connection()
        )
        api_entered = threading.Event()
        release_api = threading.Event()
        operation_completed = threading.Event()
        revoke_completed = threading.Event()
        revoke_backend_ready = threading.Event()
        revoke_backend_pid = []
        operation_errors = []
        revoke_errors = []

        def blocking_api(*_args, **_kwargs):
            api_entered.set()
            if not release_api.wait(10):
                raise TimeoutError("Timed out waiting to release mocked Slack API")
            return CHANNELS_PAYLOAD

        def revoke_after_recording_backend():
            revoke_backend_pid.append(self._postgres_backend_pid())
            revoke_backend_ready.set()
            revoke_user_grant(User.objects.get(pk=user.pk))

        operation = threading.Thread(
            target=self._run_in_thread,
            args=(
                lambda: serialize_slack_channels(
                    User.objects.get(pk=user.pk),
                    organization=Organization.objects.get(pk=organization.pk),
                ),
                operation_errors,
                operation_completed,
            ),
        )
        revoke = threading.Thread(
            target=self._run_in_thread,
            args=(
                revoke_after_recording_backend,
                revoke_errors,
                revoke_completed,
            ),
        )
        with (
            patch(
                "integrations.services.external_connectors._slack_api_request",
                side_effect=blocking_api,
            ) as mock_slack_api,
            patch(
                "integrations.services.slack_dm_mirror._revoke_remote_token"
            ),
        ):
            operation.start()
            self.assertTrue(api_entered.wait(5))
            revoke.start()
            self.assertTrue(revoke_backend_ready.wait(5))
            self._assert_backend_waits_for_row_lock(revoke_backend_pid[0])
            self.assertFalse(revoke_completed.is_set())
            release_api.set()
            operation.join(10)
            revoke.join(10)

        self.assertTrue(operation_completed.is_set())
        self.assertTrue(revoke_completed.is_set())
        self.assertEqual(operation_errors, [])
        self.assertEqual(revoke_errors, [])
        self.assertEqual(mock_slack_api.call_count, 1)
        slack_connection.refresh_from_db()
        self.assertEqual(
            slack_connection.status,
            ExternalServiceConnectionStatus.DISCONNECTED,
        )
        self.assertEqual(slack_connection.access_token, "")
        self.assertFalse(
            SlackChannelSelection.objects.filter(connection=slack_connection).exists()
        )

    def test_connection_disconnect_waits_for_sync_then_erases_all_artifacts(self):
        user, _organization, slack_connection, _selection = (
            self.create_slack_connection(with_selection=True)
        )
        api_entered = threading.Event()
        release_api = threading.Event()
        operation_completed = threading.Event()
        revoke_completed = threading.Event()
        revoke_backend_ready = threading.Event()
        revoke_backend_pid = []
        operation_errors = []
        revoke_errors = []

        def blocking_api(*_args, **_kwargs):
            api_entered.set()
            if not release_api.wait(10):
                raise TimeoutError("Timed out waiting to release mocked Slack API")
            return HISTORY_PAYLOAD

        def revoke_after_recording_backend():
            revoke_backend_pid.append(self._postgres_backend_pid())
            revoke_backend_ready.set()
            revoke_connection_grant(
                User.objects.get(pk=user.pk),
                slack_connection.pk,
            )

        operation = threading.Thread(
            target=self._run_in_thread,
            args=(
                lambda: sync_slack_connection(
                    ExternalServiceConnection.objects.get(pk=slack_connection.pk)
                ),
                operation_errors,
                operation_completed,
            ),
        )
        revoke = threading.Thread(
            target=self._run_in_thread,
            args=(
                revoke_after_recording_backend,
                revoke_errors,
                revoke_completed,
            ),
        )
        with (
            patch(
                "integrations.services.external_connectors._slack_api_request",
                side_effect=blocking_api,
            ) as mock_slack_api,
            patch(
                "integrations.services.slack_dm_mirror._revoke_remote_token"
            ),
        ):
            operation.start()
            self.assertTrue(api_entered.wait(5))
            revoke.start()
            self.assertTrue(revoke_backend_ready.wait(5))
            self._assert_backend_waits_for_row_lock(revoke_backend_pid[0])
            self.assertFalse(revoke_completed.is_set())
            release_api.set()
            operation.join(10)
            revoke.join(10)

        self.assertTrue(operation_completed.is_set())
        self.assertTrue(revoke_completed.is_set())
        self.assertEqual(revoke_errors, [])
        self.assertTrue(
            not operation_errors
            or (
                len(operation_errors) == 1
                and isinstance(operation_errors[0], ConnectorOAuthError)
            )
        )
        self.assertEqual(mock_slack_api.call_count, 1)
        slack_connection.refresh_from_db()
        self.assertEqual(
            slack_connection.status,
            ExternalServiceConnectionStatus.DISCONNECTED,
        )
        self.assertEqual(slack_connection.access_token, "")
        self.assertEqual(slack_connection.refresh_token, "")
        self.assertFalse(
            SlackChannelSelection.objects.filter(connection=slack_connection).exists()
        )
        self.assertFalse(
            SlackMessageArtifact.objects.filter(connection=slack_connection).exists()
        )
        self.assertFalse(
            SlackThreadArtifact.objects.filter(connection=slack_connection).exists()
        )

        with patch(
            "integrations.services.external_connectors._slack_api_request"
        ) as post_revoke_slack_api:
            with self.assertRaises(ConnectorOAuthError):
                sync_slack_connection(slack_connection)
        post_revoke_slack_api.assert_not_called()

    def test_disconnect_commits_before_stale_sync_and_blocks_slack_call(self):
        user, _organization, slack_connection, _selection = (
            self.create_slack_connection(with_selection=True)
        )
        delete_holds_authority = threading.Event()
        release_delete = threading.Event()
        stale_sync_attempted_lock = threading.Event()
        operation_completed = threading.Event()
        revoke_completed = threading.Event()
        operation_errors = []
        revoke_errors = []

        original_clear = slack_dm_mirror._clear_slack_connection_locked
        original_lock = external_connectors._lock_slack_connection_authority

        def blocked_clear(connection_row):
            delete_holds_authority.set()
            if not release_delete.wait(10):
                raise TimeoutError("Timed out waiting to release Slack disconnect")
            return original_clear(connection_row)

        def observed_authority_lock(authority):
            stale_sync_attempted_lock.set()
            return original_lock(authority)

        revoke = threading.Thread(
            target=self._run_in_thread,
            args=(
                lambda: revoke_connection_grant(
                    User.objects.get(pk=user.pk),
                    slack_connection.pk,
                ),
                revoke_errors,
                revoke_completed,
            ),
        )
        operation = threading.Thread(
            target=self._run_in_thread,
            args=(
                lambda: sync_slack_connection(
                    ExternalServiceConnection.objects.get(pk=slack_connection.pk)
                ),
                operation_errors,
                operation_completed,
            ),
        )
        with (
            patch(
                "integrations.services.slack_dm_mirror._clear_slack_connection_locked",
                side_effect=blocked_clear,
            ),
            patch(
                "integrations.services.external_connectors._lock_slack_connection_authority",
                side_effect=observed_authority_lock,
            ),
            patch(
                "integrations.services.slack_dm_mirror._revoke_remote_token"
            ),
            patch(
                "integrations.services.external_connectors._slack_api_request"
            ) as mock_slack_api,
        ):
            revoke.start()
            self.assertTrue(delete_holds_authority.wait(5))
            operation.start()
            self.assertTrue(stale_sync_attempted_lock.wait(5))
            # No timing assumption: DELETE still holds the shared user lock, so
            # the stale sync cannot pass authority validation or call Slack.
            self.assertFalse(operation_completed.is_set())
            mock_slack_api.assert_not_called()
            release_delete.set()
            revoke.join(10)
            operation.join(10)

        self.assertTrue(revoke_completed.is_set())
        self.assertTrue(operation_completed.is_set())
        self.assertEqual(revoke_errors, [])
        self.assertEqual(len(operation_errors), 1)
        self.assertIsInstance(operation_errors[0], ConnectorOAuthError)
        mock_slack_api.assert_not_called()
        slack_connection.refresh_from_db()
        self.assertEqual(
            slack_connection.status,
            ExternalServiceConnectionStatus.DISCONNECTED,
        )
        self.assertEqual(slack_connection.access_token, "")
        self.assertFalse(
            SlackMessageArtifact.objects.filter(
                connection_id=slack_connection.pk
            ).exists()
        )
        self.assertFalse(
            SlackThreadArtifact.objects.filter(
                connection_id=slack_connection.pk
            ).exists()
        )
