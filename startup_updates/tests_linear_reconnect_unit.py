"""Linear auth and Pulse recovery contracts; no database or network access."""

from contextlib import ExitStack, contextmanager, nullcontext
from datetime import date, timedelta
import json
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from requests import Response, Timeout

from community_chat.startups.lifecycle import source_capabilities
from community_chat.startups.views import GenerateView
from integrations.services import external_connectors as connectors
from startup_updates import activity_scope, update_identity
from vibe_raising import views


def response(status=200, payload=None, **headers):
    value = Response()
    value.status_code = status
    value.url = "https://api.linear.app/oauth/token"
    value._content = json.dumps(payload if payload is not None else {}).encode()
    value.headers.update(headers)
    return value


class LinearFixture:
    def setUp(self):
        self.user = Obj(pk=2, id=2)
        self.organization = Obj(pk=4, id=4, domain="startup.example")
        self.connection = Obj(
            pk=8, id=8, user_id=2, organization_id=4, provider="linear",
            access_token="synthetic-old-access", refresh_token="synthetic-refresh",
            token_type="Bearer", token_expires_at=timezone.now() - timedelta(days=1),
            updated_at=timezone.now() - timedelta(days=2), scopes=[],
            status="connected", last_error="", last_synced_at=None,
            account_label="Example", external_account_id="workspace", save=MagicMock(),
        )


@override_settings(LINEAR_CLIENT_ID="synthetic-id", LINEAR_CLIENT_SECRET="synthetic-secret")
class LinearCredentialTests(LinearFixture, SimpleTestCase):
    def test_refresh_rejection_marks_only_the_rejected_scoped_version(self):
        for status in (400, 401, 403):
            with self.subTest(status=status), patch.object(connectors.ExternalServiceConnection, "objects") as rows, patch.object(
                connectors.requests, "post", return_value=response(status, {"error": "invalid_grant", "error_description": "sensitive-provider-detail"}),
            ), self.assertRaises(connectors.ConnectorReconnectRequired) as caught:
                connectors._refresh_linear_token(self.connection)
            self.assertEqual(str(caught.exception), connectors.LINEAR_RECONNECT_MESSAGE)
            self.assertNotIn("sensitive-provider-detail", str(caught.exception))
            rows.filter.assert_called_once_with(pk=8, user_id=2, organization_id=4,
                provider="linear", updated_at=self.connection.updated_at)
            changes = rows.filter.return_value.update.call_args.kwargs
            self.assertEqual(changes["status"], "error")
            self.assertEqual(changes["last_error"], connectors.LINEAR_RECONNECT_MESSAGE)
            self.assertNotIn("access_token", changes)
            self.assertNotIn("refresh_token", changes)
            self.assertEqual(self.connection.refresh_token, "synthetic-refresh")

    def test_missing_refresh_token_requires_reconnection_without_provider_request(self):
        self.connection.refresh_token = ""
        with patch.object(connectors.ExternalServiceConnection, "objects"), patch.object(connectors.requests, "post") as post:
            with self.assertRaises(connectors.ConnectorReconnectRequired):
                connectors.ensure_linear_connection_authorized(self.connection)
        post.assert_not_called()

    def test_missing_access_token_is_recorded_as_reconnect_required(self):
        self.connection.access_token = ""
        with patch.object(connectors.ExternalServiceConnection, "objects") as rows, self.assertRaises(connectors.ConnectorReconnectRequired):
            connectors.ensure_linear_connection_authorized(self.connection)
        rows.filter.return_value.update.assert_called_once()

    def test_known_reconnect_failure_is_not_retried_until_reauthorized(self):
        self.connection.status = "error"
        self.connection.last_error = connectors.LINEAR_RECONNECT_MESSAGE
        with patch.object(connectors.ExternalServiceConnection, "objects"), patch.object(connectors.requests, "post") as post:
            with self.assertRaises(connectors.ConnectorReconnectRequired):
                connectors.ensure_linear_connection_authorized(self.connection)
        post.assert_not_called()

    def test_successful_refresh_rotates_tokens_and_clears_previous_errors(self):
        self.connection.status, self.connection.last_error = "error", "Old transient failure"
        payload = {"access_token": "synthetic-new-access", "refresh_token": "synthetic-new-refresh", "expires_in": 86400}
        with patch.object(connectors.requests, "post", return_value=response(payload=payload)) as post:
            connectors.ensure_linear_connection_authorized(self.connection)
        self.assertEqual(self.connection.access_token, payload["access_token"])
        self.assertEqual(self.connection.refresh_token, payload["refresh_token"])
        self.assertGreater(self.connection.token_expires_at, timezone.now())
        self.assertEqual((self.connection.status, self.connection.last_error), ("connected", ""))
        self.connection.save.assert_called_once()
        self.assertEqual(post.call_args.kwargs["data"]["grant_type"], "refresh_token")

    def test_refresh_keeps_original_refresh_token_when_rotation_is_omitted(self):
        with patch.object(connectors.requests, "post", return_value=response(payload={"access_token": "synthetic-new", "expires_in": 3600})):
            connectors.ensure_linear_connection_authorized(self.connection)
        self.assertEqual(self.connection.refresh_token, "synthetic-refresh")

    def test_unexpired_credentials_do_not_request_refresh(self):
        self.connection.token_expires_at = timezone.now() + timedelta(hours=1)
        with patch.object(connectors.requests, "post") as post:
            connectors.ensure_linear_connection_authorized(self.connection)
        post.assert_not_called()
        self.connection.save.assert_not_called()

    def test_outages_and_malformed_responses_do_not_invalidate_credentials(self):
        cases = [
            response(503), Timeout("sensitive-provider-detail"), response(payload=[]),
            response(payload={"refresh_token": "unusable-without-access"}),
            response(payload={"error": "temporarily_unavailable"}),
            response(payload={"error": {"unexpected": "object"}}),
        ]
        for case in cases:
            with self.subTest(case=type(case).__name__), patch.object(connectors.ExternalServiceConnection, "objects") as rows, patch.object(
                connectors.requests, "post", side_effect=case if isinstance(case, Exception) else None,
                return_value=case,
            ), self.assertRaises(connectors.ConnectorTemporarilyUnavailable) as caught:
                connectors.ensure_linear_connection_authorized(self.connection)
            self.assertEqual(str(caught.exception), connectors.LINEAR_UNAVAILABLE_MESSAGE)
            rows.filter.assert_not_called()
            self.connection.save.assert_not_called()
            self.assertEqual(self.connection.status, "connected")

    def test_invalid_json_does_not_invalidate_credentials(self):
        malformed = response()
        malformed._content = b"not-json"
        with patch.object(connectors.requests, "post", return_value=malformed), self.assertRaises(connectors.ConnectorTemporarilyUnavailable):
            connectors.ensure_linear_connection_authorized(self.connection)
        self.connection.save.assert_not_called()

    def test_rate_limit_keeps_credentials_and_retry_delay(self):
        with patch.object(connectors.requests, "post", return_value=response(429, **{"Retry-After": "42"})), self.assertRaises(connectors.ConnectorRateLimitError) as caught:
            connectors.ensure_linear_connection_authorized(self.connection)
        self.assertEqual(caught.exception.retry_after_seconds, 42)
        self.assertEqual(self.connection.status, "connected")

    def test_oauth_error_in_success_response_also_requires_reconnection(self):
        with patch.object(connectors.ExternalServiceConnection, "objects"), patch.object(
            connectors.requests, "post", return_value=response(payload={"error": "invalid_grant"}),
        ), self.assertRaises(connectors.ConnectorReconnectRequired):
            connectors.ensure_linear_connection_authorized(self.connection)

    def test_revocation_during_catalog_discovery_refreshes_once_then_requires_reconnection(self):
        self.connection.token_expires_at = timezone.now() + timedelta(hours=1)
        replies = [response(401), response(payload={"access_token": "synthetic-new", "expires_in": 3600}), response(401)]
        with patch.object(connectors.ExternalServiceConnection, "objects"), patch.object(connectors.requests, "post", side_effect=replies) as post:
            with self.assertRaises(connectors.ConnectorReconnectRequired):
                connectors._linear_graphql_request(self.connection, "query { projects { nodes { id } } }")
        self.assertEqual(post.call_count, 3)
        self.assertEqual(self.connection.status, "error")

    def test_graphql_auth_error_without_refresh_token_requires_reconnection(self):
        self.connection.token_expires_at = timezone.now() + timedelta(hours=1)
        self.connection.refresh_token = ""
        failure = {"errors": [{"message": "sensitive-detail", "extensions": {"code": "UNAUTHENTICATED"}}]}
        with patch.object(connectors.ExternalServiceConnection, "objects"), patch.object(connectors.requests, "post", return_value=response(payload=failure)), self.assertRaises(connectors.ConnectorReconnectRequired):
            connectors._linear_graphql_request(self.connection, "query { projects { nodes { id } } }")

    def test_reconnect_marker_does_not_replace_a_newer_oauth_callback(self):
        error = connectors.ConnectorReconnectRequired(self.connection, connectors.LINEAR_RECONNECT_MESSAGE)
        stored = {"updated_at": timezone.now(), "status": "connected", "last_error": ""}
        def select(**filters):
            def update(**changes):
                if stored["updated_at"] != filters["updated_at"]:
                    return 0
                stored.update(changes)
                return 1
            return Obj(update=update)
        with patch.object(connectors.ExternalServiceConnection.objects, "filter", side_effect=select):
            error.persist()
        self.assertEqual(stored["status"], "connected")
        self.assertEqual(stored["last_error"], "")


class LinearStatusTests(LinearFixture, SimpleTestCase):
    def test_status_is_actionable_and_disables_linear_only(self):
        self.connection.status = "error"
        self.connection.last_error = connectors.LINEAR_RECONNECT_MESSAGE
        with patch.object(connectors, "_scope_org", return_value=self.organization), patch.object(
            connectors.ExternalServiceConnection, "objects",
        ) as rows, patch.object(connectors.LinearProjectSelection, "objects"), patch.object(
            connectors, "_provider_configuration_error", return_value=None,
        ):
            rows.filter.return_value.exclude.return_value.filter.return_value.order_by.return_value.first.return_value = self.connection
            source = connectors._serialize_external_source(self.user, "linear", self.organization)
        self.assertEqual(source["status"], "needs_reauth")
        self.assertFalse(source["selected"])
        self.assertEqual(source["warning"], connectors.LINEAR_RECONNECT_MESSAGE)
        capability = source_capabilities(source)
        self.assertFalse(capability["usableForUpdates"])
        self.assertTrue(capability["canConnect"])
        self.assertTrue(capability["canDisconnect"])
        rows.filter.assert_called_once_with(user=self.user, provider="linear")
        rows.filter.return_value.exclude.return_value.filter.assert_called_once_with(organization=self.organization)

    def test_excluding_linear_never_discovers_its_projects(self):
        with patch.object(connectors, "serialize_slack_channels", return_value={"channels": [{"channelId": "C1"}]}), patch.object(activity_scope, "discover_linear_projects") as linear:
            scope = activity_scope.discover_activity_resources(self.user, self.organization, ["slack"])
        self.assertEqual(scope, {"slack": ["C1"]})
        linear.assert_not_called()

    def test_catalog_discovery_never_rotates_credentials_inside_the_run_transaction(self):
        with patch.object(connectors, "_latest_linear_connection", return_value=self.connection), patch.object(
            connectors.ExternalServiceConnection, "objects",
        ), patch.object(connectors.requests, "post", return_value=response(401)) as post, patch.object(
            connectors, "_refresh_linear_token",
        ) as refresh, self.assertRaises(connectors.ConnectorReconnectRequired):
            activity_scope.discover_linear_projects(self.user, self.organization)
        refresh.assert_not_called()
        post.assert_called_once()


@override_settings(LINEAR_CLIENT_ID="synthetic-id", LINEAR_CLIENT_SECRET="synthetic-secret")
class PulseLinearRecoveryTests(LinearFixture, SimpleTestCase):
    def start(self, providers, *, create_effect=None, atomic=nullcontext):
        company = Obj(pk="company", organization=self.organization)
        binding = Obj(id=3, google_connection_id=None)
        draft = Obj(pk=6, current_revision_id=None, month=date(2026, 10, 1), update_date=None, save=MagicMock())
        run = Obj(run_id="synthetic-run", run_request={})
        view = GenerateView()
        view.company = company
        request = Obj(user=self.user, data={"inputSources": providers, "targetMonth": "2026-10-01", "updateDate": "2026-10-17"}, query_params={})
        sources = [
            {"key": "slack", "provider": "slack", "label": "Slack", "status": "connected", "connectionId": 9},
            {"key": "linear", "provider": "linear", "label": "Linear", "status": "needs_reauth" if self.connection.status == "error" else "connected", "connectionId": 8},
        ]
        seams = {
            "_get_founder_company_context_or_response": ({"company": company, "domain": self.organization.domain}, None),
            "_ensure_binding_for_company": (self.organization, Obj(reporting_timezone="UTC"), binding),
            "_resolve_manual_documents_for_request": [],
            "google_connection_for_org": None,
            "coerce_startup_update_sources_for_gmail_scope": (providers, None, {}),
            "_requested_target_month_from_request": date(2026, 10, 1),
            "get_open_startup_update_run": None,
            "_build_email_draft_payload": {},
        }
        with ExitStack() as stack:
            for name, value in seams.items():
                stack.enter_context(patch.object(views, name, return_value=value))
            stack.enter_context(patch.object(connectors, "serialize_source_status", return_value={"sources": sources}))
            stack.enter_context(patch.object(views.transaction, "atomic", side_effect=atomic))
            stack.enter_context(patch.object(update_identity, "resolve_update", return_value=(draft, True)))
            stack.enter_context(patch.object(update_identity, "identity_payload", return_value={}))
            stack.enter_context(patch.object(update_identity, "narrative_window", return_value={"start": "2026-10-01T00:00:00Z", "end": "2026-10-17T00:00:00Z"}))
            rows = stack.enter_context(patch.object(views.ExternalServiceConnection, "objects"))
            rows.filter.return_value.exclude.return_value.filter.return_value.order_by.return_value.first.return_value = self.connection
            create = stack.enter_context(patch.object(views, "create_startup_update_run", return_value=run, side_effect=create_effect))
            dispatch = stack.enter_context(patch.object(views, "_dispatch_run_to_valley", return_value=True))
            result = view.post(request)
        return result, create, dispatch, rows

    def test_failed_refresh_then_generate_with_other_selected_sources(self):
        with patch.object(connectors.requests, "post", return_value=response(400)) as post:
            failed, create, dispatch, rows = self.start(["slack", "linear"])
            self.assertEqual(failed.status_code, 409)
            self.assertEqual(failed.data["code"], "startup_update_sources_unavailable")
            self.assertEqual(failed.data["unavailableSources"], ["linear"])
            self.assertEqual(failed.data["detail"], connectors.LINEAR_RECONNECT_MESSAGE)
            self.assertEqual(self.connection.status, "error")
            create.assert_not_called()
            dispatch.assert_not_called()
            self.assertEqual(rows.filter.return_value.update.call_count, 2)
            post.reset_mock()
            success, create, dispatch, _ = self.start(["slack"])
        self.assertEqual(success.status_code, 201)
        self.assertEqual(create.call_args.kwargs["input_sources"], ["slack"])
        self.assertTrue(create.call_args.kwargs["automatic_source_scope"])
        dispatch.assert_called_once()
        post.assert_not_called()

    def test_known_failed_selected_source_returns_409_before_run_creation(self):
        self.connection.status = "error"
        with patch.object(connectors.requests, "post") as post:
            result, create, dispatch, _ = self.start(["slack", "linear"])
        self.assertEqual(result.status_code, 409)
        self.assertIn("Linear", result.data["detail"])
        self.assertIn("other sources", result.data["detail"])
        self.assertEqual(result.data["unavailableSources"], ["linear"])
        create.assert_not_called()
        dispatch.assert_not_called()
        post.assert_not_called()

    def test_successful_refresh_is_saved_before_the_creation_transaction(self):
        events = []
        self.connection.save.side_effect = lambda **kwargs: events.append("refreshed")
        @contextmanager
        def atomic():
            events.append("begin")
            yield
            events.append("commit")
        with patch.object(connectors.requests, "post", return_value=response(payload={"access_token": "synthetic-new", "expires_in": 3600})):
            result, _, dispatch, _ = self.start(["linear"], atomic=atomic)
        self.assertEqual(result.status_code, 201)
        self.assertEqual(events, ["refreshed", "begin", "commit"])
        dispatch.assert_called_once()

    def test_catalog_rejection_is_persisted_after_creation_rolls_back(self):
        self.connection.token_expires_at = timezone.now() + timedelta(hours=1)
        events = []
        @contextmanager
        def atomic():
            events.append("begin")
            try:
                yield
            except connectors.ConnectorReconnectRequired:
                events.append("rollback")
                self.connection.status, self.connection.last_error = "connected", ""
                raise
        original = connectors.ConnectorReconnectRequired.persist
        def persist(error):
            events.append("mark_reconnect")
            original(error)
        def catalog(**kwargs):
            connectors._linear_reconnect_required(self.connection)
        with patch.object(connectors.ConnectorReconnectRequired, "persist", new=persist):
            result, _, dispatch, _ = self.start(["linear"], create_effect=catalog, atomic=atomic)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(events, ["begin", "mark_reconnect", "rollback", "mark_reconnect"])
        self.assertEqual(self.connection.status, "error")
        dispatch.assert_not_called()

    def test_temporary_failure_is_retryable_without_marking_reconnect(self):
        with patch.object(connectors.requests, "post", side_effect=Timeout("sensitive-detail")):
            result, create, dispatch, rows = self.start(["linear"])
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.data["detail"], connectors.LINEAR_UNAVAILABLE_MESSAGE)
        self.assertEqual(self.connection.status, "connected")
        rows.filter.return_value.update.assert_not_called()
        create.assert_not_called()
        dispatch.assert_not_called()

    def test_rate_limit_returns_retry_after_without_marking_reconnect(self):
        with patch.object(connectors.requests, "post", return_value=response(429, **{"Retry-After": "42"})):
            result, create, dispatch, _ = self.start(["linear"])
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result["Retry-After"], "42")
        self.assertEqual(self.connection.status, "connected")
        create.assert_not_called()
        dispatch.assert_not_called()
