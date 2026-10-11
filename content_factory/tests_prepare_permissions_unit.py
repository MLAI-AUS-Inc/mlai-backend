"""GitHub permission prompts use installation grants, never minted tokens."""
from types import SimpleNamespace
from unittest.mock import patch, Mock

from django.test import SimpleTestCase

from . import tests_prepare_journey_unit as fixtures
from .website_journey import project_journey


class PreparePermissionsTests(SimpleTestCase):
    def test_missing_grant_uses_the_stored_installation_account_url(self):
        from . import website_github_permissions as permissions
        from integrations.services.github_app import GitHubWorkflowPermissionRequired
        for kind, login, url in (("User", "Gshah810", "https://github.com/settings/installations/4"),
                ("Organization", "Vyavos", "https://github.com/organizations/Vyavos/settings/installations/4")):
            with self.subTest(kind=kind), patch.object(permissions.cache, "get", return_value=None), \
                    patch.object(permissions.cache, "set"), patch.object(permissions.GitHubInstallation.objects, "filter") as rows, \
                    patch("integrations.services.github_app._require_installation_profile_permissions", side_effect=[
                        GitHubWorkflowPermissionRequired("missing"), {"checks": "read", "statuses": "read"}]):
                rows.return_value.order_by.return_value.first.return_value = SimpleNamespace(account_type=kind, account_login=login)
                grants = permissions.installation_grants(SimpleNamespace(installation_id="4"))
            self.assertEqual(grants, {"workflows": False, "checks": True, "statuses": True, "settingsUrl": url})

    def test_permission_lookup_outage_is_retryable_not_a_missing_grant(self):
        from . import website_github_permissions as permissions
        from integrations.services.github_app import GitHubPermissionLookupUnavailable
        with patch.object(permissions.cache, "get", return_value=None), \
                patch.object(permissions.GitHubInstallation.objects, "filter") as rows, \
                patch("integrations.services.github_app._require_installation_profile_permissions", side_effect=GitHubPermissionLookupUnavailable("unavailable")):
            rows.return_value.order_by.return_value.first.return_value = None
            grants = permissions.installation_grants(SimpleNamespace(installation_id="4"))
        self.assertEqual(grants["reasonCode"], "github_unavailable")
        self.assertTrue(grants["retryable"])

    def test_missing_grants_stop_before_setup_for_user_and_org(self):
        from . import website_prepare as prepare
        for url in ("https://github.com/settings/installations/4", "https://github.com/organizations/Vyavos/settings/installations/4"):
            with self.subTest(url=url):
                fixture = fixtures.PrepareJourneyReplayTests()
                fixture.setUp()
                original = fixture.journey
                def journey(*args, **kwargs):
                    projected = original(*args, **kwargs)
                    return project_journey(company_id=9, domain="fixture.test", website={"status": "connected", "repositoryId": 123,
                        "writePolicy": {"allowed": True}}, capabilities={"repositoryAccessVerified": True},
                        discovery=projected["discovery"], permissions={"workflows": False, "checks": True, "statuses": True, "settingsUrl": url})
                fixture.journey = journey
                setup = Mock()
                fixture.advance({"setup": setup})
                self.assertEqual(fixture.op.state, "needs_user")
                self.assertEqual(fixture.op.receipt["current_step"], "permissions")
                self.assertEqual(fixture.op.receipt["reason_code"], "github_workflow_permission_required")
                self.assertEqual(fixture.op.receipt["user_action"]["url"], url)
                setup.assert_not_called()

    def test_present_grants_continue_to_setup_and_ci_grants_are_required(self):
        kwargs = dict(company_id=9, domain="fixture.test", website={"status": "connected", "repositoryId": 123,
            "writePolicy": {"allowed": True}}, capabilities={"repositoryAccessVerified": True}, discovery={"complete": True})
        self.assertEqual(project_journey(**kwargs, permissions={"workflows": True, "checks": True, "statuses": True})["nextAction"]["id"], "setup")
        journey = project_journey(**kwargs, permissions={"workflows": True, "checks": False, "statuses": True})
        self.assertEqual(journey["prerequisites"]["permissions"]["reasonCode"], "github_ci_evidence_permission_required")

    def test_grants_cache_expires_after_sixty_seconds_and_never_mints(self):
        from . import website_github_permissions as permissions
        from integrations.services import github_app
        connection = SimpleNamespace(installation_id="4")
        stored = SimpleNamespace(account_type="Organization", account_login="Vyavos")
        cached = {}
        clock = [0]
        def get(key):
            value = cached.get(key)
            return value[0] if value and clock[0] < value[1] else None
        def set_value(key, value, timeout):
            cached[key] = (value, clock[0] + timeout)
        with patch.object(permissions.GitHubInstallation.objects, "filter") as installations, \
                patch.object(permissions.cache, "get", side_effect=get), patch.object(permissions.cache, "set", side_effect=set_value), \
                patch.object(github_app, "_require_installation_profile_permissions", return_value={"workflows": "write", "checks": "read", "statuses": "read"}) as check, \
                patch.object(github_app, "create_installation_access_token") as token:
            installations.return_value.order_by.return_value.first.return_value = stored
            self.assertEqual(permissions.installation_grants(connection)["settingsUrl"], "https://github.com/organizations/Vyavos/settings/installations/4")
            clock[0] = 59
            permissions.installation_grants(connection)
            self.assertEqual(check.call_count, 2)
            clock[0] = 60
            permissions.installation_grants(connection)
            self.assertEqual(check.call_count, 4)
            token.assert_not_called()

    def test_webhook_invalidates_cached_grants_and_wakes_only_waiting_current_connections(self):
        from .github_webhook import process_github_event
        with patch("content_factory.website_github_permissions.invalidate_installation_grants") as invalidate, \
                patch("content_factory.website_models.WebsiteConnection.objects.filter") as rows, \
                patch("content_factory.website_prepare.advance_prepares_for_connection", return_value=1) as wake:
            rows.return_value.values_list.return_value.distinct.return_value = ["connection"]
            report = process_github_event("installation", {"action": "new_permissions_accepted", "installation": {"id": 4}})
        invalidate.assert_called_once_with("4")
        wake.assert_called_once_with("connection")
        self.assertEqual(report["prepares_woken"], 1)
        self.assertEqual(rows.call_args.kwargs["operations__state"], "needs_user")
