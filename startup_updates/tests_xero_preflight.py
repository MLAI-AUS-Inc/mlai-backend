"""Exercise real Xero HTTP boundaries with synthetic credentials and no network."""

import json
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from integrations import http_client
from integrations.models import ExternalServiceConnection
from integrations.services import external_connectors as ec
from startup_updates.preflight import apply_xero_cooldown, check_sources, probe_source


def response(status=200, retry=None):
    result = requests.Response()
    result.status_code = status
    result.url = "https://api.xero.com/api.xro/2.0/Reports/ProfitAndLoss"
    # Xero's actual 429 can have an empty/non-JSON body.
    result._content = b"" if status == 429 else b'{"Reports": []}'
    if retry is not None:
        result.headers["Retry-After"] = str(retry)
    return result


@override_settings(XERO_CLIENT_ID="synthetic-pulse-app")
class XeroPreflightHttpTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.connection = SimpleNamespace(pk=7, external_account_id="tenant-a")
        self.query = Mock()
        self.query.exclude.return_value.order_by.return_value.first.return_value = self.connection

    def check(self):
        with patch.object(ExternalServiceConnection.objects, "filter", return_value=self.query), \
             patch("integrations.services.source_accounts.report_account_id", return_value=None), \
             patch.object(ec, "_xero_required_token", return_value="synthetic-token"):
            return check_sources(["xero"], lambda _: probe_source("xero", user="user-a", organization="startup-a"))[0]

    def test_real_empty_429_preserves_wait_and_reconnect_cannot_reset_shared_quota(self):
        with patch.object(http_client.requests, "request", return_value=response(429, 27384)) as request:
            first = self.check()
            self.connection.pk = 99  # Recreated/reconnected credentials, same Xero tenant.
            second = self.check()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(first["reason"], "rate_limited")
        self.assertEqual(first["status"], "temporarily_unavailable")
        self.assertGreaterEqual(int(second["retry_after_seconds"]), 27383)
        self.assertIn("Reconnecting Xero does not reset", second["message"])
        self.assertNotIn("synthetic-token", json.dumps(first))
        self.assertNotIn("tenant-a", json.dumps(first))
        self.assertEqual(request.call_args.kwargs["timeout"], (3, 15))

    def test_different_tenant_and_app_have_independent_quotas(self):
        with patch.object(http_client.requests, "request", side_effect=[response(429, 27384), response(), response()]) as request:
            self.check()
            self.connection.external_account_id = "tenant-b"
            self.assertEqual(self.check()["status"], "ready")
            self.connection.external_account_id = "tenant-a"
            with override_settings(XERO_CLIENT_ID="another-app"):
                self.assertEqual(self.check()["status"], "ready")
        self.assertEqual(request.call_count, 3)

    def test_expired_quota_rechecks_actual_report_and_clears_failure(self):
        with patch.object(http_client.requests, "request", side_effect=[response(429, 20), response()]) as request:
            self.check()
            future = time.time() + 21
            with patch.object(http_client.time, "time", return_value=future):
                result = self.check()
        self.assertEqual(request.call_count, 2)
        self.assertEqual(result, {"provider": "xero", "status": "ready", "connectionId": 7, "accountId": "tenant-a"})

    def test_revoked_credentials_still_require_reconnect(self):
        with patch.object(http_client.requests, "request", return_value=response(401)):
            result = self.check()
        self.assertEqual(result["status"], "needs_reconnect")
        self.assertNotIn("retry_after_seconds", result)

    def test_timeout_remains_bounded_transient_failure(self):
        with patch.object(http_client.requests, "request", side_effect=requests.Timeout("secret upstream data")):
            result = self.check()
        self.assertEqual(result["status"], "temporarily_unavailable")
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("retry_after_seconds", result)

    def test_http_date_and_missing_retry_headers_are_safe(self):
        from email.utils import format_datetime
        for header in (format_datetime(timezone.now() + timedelta(hours=7), usegmt=True), None, "malformed"):
            with self.subTest(header=header):
                cache.clear()
                with patch.object(http_client.requests, "request", return_value=response(429, header)):
                    result = self.check()
                self.assertEqual(result["reason"], "rate_limited")
                self.assertGreaterEqual(result["retry_after_seconds"], 60)

    def test_quota_on_one_source_never_loses_other_selected_source_outcomes(self):
        def probe(provider):
            if provider == "xero":
                raise ec.ConnectorRateLimitError(27384)
            if provider == "gmail":
                raise PermissionError()
            return {"connectionId": 8}
        results = check_sources(["slack", "xero", "gmail", "stripe"], probe)
        self.assertEqual([c["provider"] for c in results], ["slack", "xero", "gmail", "stripe"])
        self.assertEqual([c["status"] for c in results], ["ready", "temporarily_unavailable", "needs_reconnect", "ready"])

    def test_google_style_and_headerless_quota_errors_keep_safe_retry_hints(self):
        google_error = RuntimeError("synthetic provider detail")
        google_error.resp = type("GoogleHeaders", (dict,), {"status": 429})({"retry-after": "27384"})
        api_error = RuntimeError("synthetic provider detail")
        api_error.status_code = 429
        for error, seconds in ((google_error, 27384), (api_error, 60)):
            with self.subTest(error=type(error).__name__):
                result = check_sources(["gmail"], Mock(side_effect=error))[0]
                self.assertEqual(result["retry_after_seconds"], seconds)
                self.assertEqual(result["reason"], "rate_limited")
                self.assertNotIn("synthetic provider detail", json.dumps(result))

    def test_temporary_linear_failure_does_not_request_reconnect(self):
        result = check_sources(["linear"], Mock(side_effect=ec.ConnectorTemporarilyUnavailable("linear", "synthetic provider detail")))[0]
        self.assertEqual(result["status"], "temporarily_unavailable")
        self.assertNotIn("synthetic provider detail", json.dumps(result))

    def test_quota_during_report_refresh_keeps_other_sources_and_connection_pins(self):
        checks = [{"provider": "gmail", "status": "ready", "connectionId": 1},
            {"provider": "xero", "status": "ready", "connectionId": 7, "accountId": "tenant-a"}]
        with patch.object(http_client.requests, "request", return_value=response(429, 27384)):
            self.check()
        actual = apply_xero_cooldown(checks)
        self.assertEqual(actual[0], checks[0])
        self.assertEqual(actual[1]["reason"], "rate_limited")
        self.assertEqual(actual[1]["connectionId"], 7)
        self.assertEqual(checks[1]["status"], "ready")  # Caller data is immutable.


@override_settings(XERO_CLIENT_ID="synthetic-pulse-app")
class XeroPreflightScopeTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from organizations.models import Organization
        cache.clear()
        self.users = [get_user_model().objects.create_user(email=f"xero-{n}@example.invalid") for n in range(2)]
        self.orgs = [Organization.objects.create(name=f"Startup {n}", domain=f"xero-{n}.invalid") for n in range(2)]

    def test_real_connection_lookup_is_scoped_across_users_and_startups(self):
        for user in self.users:
            for org in self.orgs:
                ExternalServiceConnection.objects.create(user=user, organization=org, provider="xero",
                    access_token=f"synthetic-{user.pk}-{org.pk}", external_account_id=f"tenant-{user.pk}-{org.pk}",
                    token_expires_at=timezone.now() + timedelta(hours=1))
        with patch.object(http_client.requests, "request", return_value=response()) as request:
            for user in self.users:
                for org in self.orgs:
                    result = probe_source("xero", user=user, organization=org)
                    self.assertEqual(result["accountId"], f"tenant-{user.pk}-{org.pk}")
                    self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], f"Bearer synthetic-{user.pk}-{org.pk}")

    def test_sync_quota_does_not_invalidate_connection_or_advance_cursor(self):
        connection = ExternalServiceConnection.objects.create(user=self.users[0], organization=self.orgs[0], provider="xero",
            status="connected", access_token="synthetic", external_account_id="tenant-sync", sync_cursor={"saved": True},
            token_expires_at=timezone.now() + timedelta(hours=1))
        with patch.object(http_client.requests, "request", return_value=response(429, 27384)):
            result = ec.mark_sources_sync_requested(self.users[0], ["xero"], organization=self.orgs[0])
        connection.refresh_from_db()
        self.assertEqual(connection.status, "connected")
        self.assertEqual(connection.last_error, "")
        self.assertEqual(connection.sync_cursor, {"saved": True})
        self.assertIsNone(connection.last_synced_at)
        self.assertEqual(result["syncRuns"][0]["retry_after_seconds"], 27384)

    def test_discovery_and_linear_preparation_preserve_quota_and_auth_outcomes(self):
        from rest_framework.test import APIRequestFactory
        from startup_updates.api_views import StartupUpdateSourcePreflightView
        from startup_updates.models import StartupProfile, UserStartupBinding
        from workflow_runs.models import ContentFactoryRun
        StartupProfile.objects.create(organization=self.orgs[0])
        binding = UserStartupBinding.objects.create(user=self.users[0], organization=self.orgs[0])
        scenarios = [("discovery", p, ec.ConnectorRateLimitError(27384), "temporarily_unavailable")
            for p in ("slack", "linear", "google_analytics")]
        scenarios += [
            ("preparation", "linear", ec.ConnectorRateLimitError(27384), "temporarily_unavailable"),
            ("preparation", "linear", ec.ConnectorTemporarilyUnavailable("linear", "synthetic provider detail"), "temporarily_unavailable"),
            ("preparation", "linear", ec.ConnectorOAuthError("synthetic provider detail"), "needs_reconnect")]
        for index, (stage, provider, error, expected_status) in enumerate(scenarios):
            with self.subTest(stage=stage, provider=provider, error=type(error).__name__):
                checks = [{"provider": "gmail", "status": "ready", "connectionId": 1},
                    {"provider": provider, "status": "ready", "connectionId": 7}]
                run = ContentFactoryRun.objects.create(run_id=f"quota-discovery-{index}", workflow="startup_monthly_update",
                    organization=self.orgs[0], domain=self.orgs[0].domain, status="queued",
                    run_request={"organization_id": self.orgs[0].pk, "binding_id": binding.pk,
                        "source_preflight_version": 1, "input_sources": ["gmail", provider],
                        "automatic_source_scope_requested": stage == "discovery", "current_month": "2026-08-01"})
                with override_settings(MLAI_API_KEY="synthetic"), patch("startup_updates.preflight.require_source_preflight", return_value=checks), patch(
                    "startup_updates.activity_scope.discover_resource_page", side_effect=error if stage == "discovery" else None), patch(
                    "vibe_raising.views._sync_selected_connector_sources_for_draft", side_effect=error if stage == "preparation" else None), patch(
                    "startup_updates.services.refresh_startup_update_run_source_context") as collect:
                    result = StartupUpdateSourcePreflightView.as_view()(APIRequestFactory().post(
                        "/", {}, format="json", HTTP_X_API_KEY="synthetic"), run_id=run.run_id)
                self.assertEqual(result.status_code, 200, result.data)
                self.assertFalse(result.data["ready"])
                self.assertEqual(result.data["checks"][0], checks[0])
                failed = result.data["checks"][1]
                self.assertEqual(failed["status"], expected_status)
                self.assertEqual(failed["connectionId"], 7)
                if isinstance(error, ec.ConnectorRateLimitError):
                    self.assertEqual(failed["retry_after_seconds"], 27384)
                self.assertNotIn("synthetic provider detail", json.dumps(result.data))
                collect.assert_not_called()
                run.refresh_from_db()
                self.assertFalse(run.run_request["source_preflight_complete"])
                self.assertEqual(run.result["source_preflight"]["checks"], result.data["checks"])

    def test_old_sync_quota_cannot_replace_a_newer_connection_status(self):
        connection = ExternalServiceConnection.objects.create(user=self.users[0], organization=self.orgs[0], provider="xero",
            status="connected", access_token="synthetic", external_account_id="tenant-concurrent",
            token_expires_at=timezone.now() + timedelta(hours=1))
        def provider(*args, **kwargs):
            ExternalServiceConnection.objects.filter(pk=connection.pk).update(status="error",
                last_error="A newer credential needs attention.", updated_at=timezone.now() + timedelta(seconds=1))
            return response(429, 27384)
        with patch.object(http_client.requests, "request", side_effect=provider):
            ec.mark_sources_sync_requested(self.users[0], ["xero"], organization=self.orgs[0])
        connection.refresh_from_db()
        self.assertEqual(connection.status, "error")
        self.assertEqual(connection.last_error, "A newer credential needs attention.")

    def test_preparation_and_report_refresh_quota_keep_real_endpoint_barrier_closed(self):
        from rest_framework.test import APIRequestFactory
        from startup_updates.api_views import StartupUpdateSourcePreflightView
        from startup_updates.models import StartupProfile, UserStartupBinding
        from workflow_runs.models import ContentFactoryRun

        StartupProfile.objects.create(organization=self.orgs[0])
        binding = UserStartupBinding.objects.create(user=self.users[0], organization=self.orgs[0])
        checks = [{"provider": "gmail", "status": "ready", "connectionId": 1},
            {"provider": "xero", "status": "ready", "connectionId": 7, "accountId": "tenant-a"}]
        for stage in ("preparation", "report_refresh"):
            with self.subTest(stage=stage):
                cache.clear()
                run = ContentFactoryRun.objects.create(run_id=f"quota-{stage}", workflow="startup_monthly_update",
                    organization=self.orgs[0], domain=self.orgs[0].domain, status="queued",
                    run_request={"organization_id": self.orgs[0].pk, "binding_id": binding.pk,
                        "source_preflight_version": 1, "input_sources": ["gmail", "xero"],
                        "current_month": "2026-08-01", "draft_months": ["2026-08-01"]})

                def quota():
                    http_client.get("https://api.xero.com/api.xro/2.0/Reports/ProfitAndLoss", headers={"Xero-Tenant-Id": "tenant-a"})

                def prepare(*args, **kwargs):
                    if stage == "preparation":
                        quota()
                        return {"xero": ["Synthetic sync failed."]}
                    return {}

                with override_settings(MLAI_API_KEY="synthetic"), patch("startup_updates.preflight.require_source_preflight", return_value=checks), patch(
                    "vibe_raising.views._sync_selected_connector_sources_for_draft", side_effect=prepare), patch(
                    "startup_updates.services.refresh_startup_update_run_source_context",
                    side_effect=lambda **_: quota() if stage == "report_refresh" else None), patch.object(
                    http_client.requests, "request", return_value=response(429, 27384)) as request:
                    result = StartupUpdateSourcePreflightView.as_view()(APIRequestFactory().post(
                        "/", {}, format="json", HTTP_X_API_KEY="synthetic"), run_id=run.run_id)
                self.assertEqual(result.status_code, 200, result.data)
                self.assertFalse(result.data["ready"])
                self.assertFalse(result.data.get("pending"))
                run.refresh_from_db()
                self.assertFalse(run.run_request["source_preflight_complete"])
                self.assertEqual(run.result["source_preflight"]["checks"][0], checks[0])
                self.assertEqual(run.result["source_preflight"]["checks"][1]["reason"], "rate_limited")
                self.assertEqual(request.call_count, 1)

                # The real worker snapshot API must retain the deadline across
                # reloads, while the backend's failed checks remain authoritative.
                from content_factory.service_views import ContentFactoryRunView
                deadline = (timezone.now() + timedelta(seconds=27384)).isoformat()
                payload = {"run_id": run.run_id, "workflow": run.workflow, "domain": run.domain,
                    "status": "running", "current_step": "source_preflight", "step_order": ["source_preflight"],
                    "run_request": {"input_sources": ["xero"]},
                    "result": {"preflight_retry_at": deadline, "source_preflight": {"ready": True}}}
                with override_settings(MLAI_API_KEY="synthetic"):
                    synced = ContentFactoryRunView.as_view()(APIRequestFactory().put(
                        "/", payload, format="json", HTTP_X_API_KEY="synthetic"), run_id=run.run_id)
                self.assertEqual(synced.status_code, 200, synced.data)
                run.refresh_from_db()
                self.assertEqual(run.result["preflight_retry_at"], deadline)
                self.assertFalse(run.result["source_preflight"]["ready"])
                self.assertEqual(run.run_request["input_sources"], ["gmail", "xero"])
                run.status = "cancelled"
                run.save(update_fields=["status"])
                with override_settings(MLAI_API_KEY="synthetic"):
                    cancelled = ContentFactoryRunView.as_view()(APIRequestFactory().put(
                        "/", payload, format="json", HTTP_X_API_KEY="synthetic"), run_id=run.run_id)
                self.assertEqual(cancelled.status_code, 409)
                run.refresh_from_db()
                self.assertEqual(run.status, "cancelled")
