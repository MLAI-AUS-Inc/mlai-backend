"""Exercise view redispatch ordering without a database or connector calls."""
from contextlib import ExitStack
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from django.utils import timezone

from integrations.services.external_connectors import ConnectorOAuthError
from vibe_raising import views


class QueuedDeliveryTimingTests(SimpleTestCase):
    def start(self, *, age=timedelta(minutes=5), status="queued", refresh_error=None):
        now = timezone.now()
        run = SimpleNamespace(run_id="synthetic-stale-run", status=status, result={}, updated_at=now - age)
        organization = SimpleNamespace(pk=16, id=16, domain="example.test")
        user = SimpleNamespace(pk=17, id=17)
        google = SimpleNamespace(id=18)
        binding = SimpleNamespace(google_connection_id=18)
        request = SimpleNamespace(user=user, data={"inputSources": ["gmail"], "targetMonth": "2026-03-01"})
        def refresh(**kwargs):
            if refresh_error:
                raise refresh_error
            kwargs["run"].updated_at = timezone.now()
        seams = {
            "_get_founder_company_context_or_response": {"return_value": ({
                "company": SimpleNamespace(pk=19), "domain": organization.domain}, None)},
            "_ensure_binding_for_company": {"return_value": (organization, SimpleNamespace(), binding)},
            "_resolve_manual_documents_for_request": {"return_value": []},
            "google_connection_for_org": {"return_value": google},
            "coerce_startup_update_sources_for_gmail_scope": {"return_value": (["gmail"], google, {})},
            "_sync_selected_connector_sources_for_draft": {"return_value": {}},
            "has_gmail_read_scope": {"return_value": True},
            "get_open_startup_update_run": {"return_value": run},
            "startup_update_run_matches_target_month": {"return_value": True},
            "set_startup_update_run_target_month": {"side_effect": lambda *args: setattr(run, "updated_at", timezone.now())},
            "refresh_startup_update_run_source_context": {"side_effect": refresh},
            "_dispatch_run_to_valley": {"return_value": True},
            "_build_status_payload": {"return_value": {}},
            "_serialize_run_summary": {"return_value": {"run_id": run.run_id}},
        }
        with ExitStack() as stack:
            mocks = {name: stack.enter_context(patch.object(views, name, **options))
                     for name, options in seams.items()}
            response = views.VibeRaisingStartupUpdateRunView().post(request)
        return response, run, mocks

    def test_stale_queued_delivery_is_redispatched_after_source_refresh(self):
        response, run, mocks = self.start()
        self.assertEqual(response.status_code, 200)
        mocks["refresh_startup_update_run_source_context"].assert_called_once()
        mocks["_dispatch_run_to_valley"].assert_called_once_with(run)

    def test_recent_queue_and_running_work_are_not_redispatched(self):
        for status, age in [("queued", timedelta(seconds=1)), ("running", timedelta(minutes=5))]:
            with self.subTest(status=status):
                response, _, mocks = self.start(age=age, status=status)
                self.assertEqual(response.status_code, 200)
                mocks["_dispatch_run_to_valley"].assert_not_called()

    def test_failed_authority_refresh_does_not_dispatch_a_stale_run(self):
        response, _, mocks = self.start(refresh_error=ConnectorOAuthError("Synthetic authority revoked"))
        self.assertEqual(response.status_code, 401)
        mocks["_dispatch_run_to_valley"].assert_not_called()
