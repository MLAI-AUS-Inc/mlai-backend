"""GitHub permission prompts use installation grants, never minted tokens."""
from types import SimpleNamespace
from itertools import product
from unittest.mock import patch, Mock

from django.test import SimpleTestCase

from . import tests_prepare_journey_unit as fixtures
from .website_journey import project_journey
from .activation import article_capabilities


def projected_permissions(*, integration, build, deployed, workflows, checks, statuses, domain="fixture.test", diagnostic=None):
    """Run the real capability and journey projections on synthetic proof."""
    source = "a" * 40
    capabilities = article_capabilities(SimpleNamespace(github_repo="fixture/site"), domain=domain,
        account={"saved": True, "owned": True, "verified": True},
        evidence={"verified": integration and build == "fresh", "branch": "main", "sha": source,
            "publishingVerified": deployed},
        repository_access={"verified": True, "writable": True, "branch": "main", "sha": source})
    receipt = {"status": "passed", "source_sha": source, "connection_generation": 3, "target_id": "native",
        "public_url": f"https://{domain}/articles", "checked_at": "2026-10-12T00:00:00Z"} if deployed else {}
    return project_journey(company_id=9, domain=domain,
        website={"status": "connected", "repositoryId": 123, "connectionGeneration": 3,
            "verifiedSha": source, "writePolicy": {"allowed": True}, "capabilities": {"previewSupported": True}},
        capabilities=capabilities, discovery={"complete": True, "sourceSha": source},
        target={"key": "native", "adapter": "react_component", "deployment_receipt": receipt},
        proof={"integrationVerified": integration, "buildVerified": build == "fresh", "verificationStale": build == "stale"},
        permissions={"workflows": workflows, "checks": checks, "statuses": statuses,
            "settingsUrl": "https://github.com/settings/installations/4", **(diagnostic or {})})


class PreparePermissionsTests(SimpleTestCase):
    def test_action_scoped_grants_truth_table(self):
        diagnostics = (None, {"reasonCode": "github_workflow_permission_required", "reason": "Old workflow grant diagnostic."},
            {"reasonCode": "github_ci_evidence_permission_required", "reason": "Old CI grant diagnostic."})
        cases = product((False, True), ("missing", "fresh", "stale"), (False, True), (False, True), (False, True), (False, True), diagnostics)
        for integration, build, deployed, workflows, checks, statuses, diagnostic in cases:
            with self.subTest(integration=integration, build=build, deployed=deployed,
                    workflows=workflows, checks=checks, statuses=statuses, diagnostic=diagnostic):
                journey = projected_permissions(integration=integration, build=build, deployed=deployed,
                    workflows=workflows, checks=checks, statuses=statuses, diagnostic=diagnostic)
                workflow_missing = not integration and not workflows
                ci_missing = build != "fresh" and not (checks and statuses)
                permissions_complete = not (workflow_missing or ci_missing)
                publish_ready = integration and build == "fresh" and deployed
                next_id = "permissions" if not permissions_complete else "setup" if not integration else (
                    "ci-attestation" if build == "stale" else "verify" if build != "fresh" or not deployed else None)
                self.assertEqual(journey["capabilities"]["canPublishArticle"], publish_ready)
                self.assertEqual(journey["capabilities"]["canGenerateArticle"], integration and build == "fresh")
                self.assertEqual((journey["nextAction"] or {}).get("id"), next_id)
                self.assertEqual(journey["prerequisites"]["permissions"]["status"] == "complete", permissions_complete)
                permission_step = next(step for step in journey["steps"] if step["id"] == "permissions")
                self.assertEqual(permission_step["status"] == "complete", permissions_complete)
                self.assertEqual("permissions" in journey["allowedActions"], not permissions_complete)
                self.assertTrue(journey["capabilities"]["canScan"])
                self.assertTrue(journey["capabilities"]["canPreview"])
                self.assertIn("disconnect", journey["allowedActions"])
                permission = journey["prerequisites"]["permissions"]
                if workflow_missing:
                    self.assertEqual(permission["reasonCode"], "github_workflow_permission_required")
                    self.assertIn("Workflows: write", permission["reason"])
                elif ci_missing:
                    self.assertEqual(permission["reasonCode"], "github_ci_evidence_permission_required")
                    self.assertEqual("Checks: read" in permission["reason"], not checks)
                    self.assertEqual("Commit statuses: read" in permission["reason"], not statuses)

    def test_mlai_verified_and_deployed_publishes_without_workflows_grant(self):
        journey = projected_permissions(integration=True, build="fresh", deployed=True,
            workflows=False, checks=True, statuses=True, domain="mlai.example.test")
        self.assertTrue(journey["capabilities"]["canPublishArticle"])
        self.assertIsNone(journey["nextAction"])
        self.assertNotIn("permissions", journey["allowedActions"])
        self.assertEqual(journey["prerequisites"]["permissions"]["status"], "complete")

    def test_grant_lookup_outage_is_preserved_only_while_optional_grants_are_needed(self):
        diagnostic = {"reasonCode": "github_unavailable", "reason": "GitHub permission checks are temporarily unavailable.", "retryable": True}
        for build in ("missing", "fresh"):
            with self.subTest(build=build):
                journey = projected_permissions(integration=True, build=build, deployed=True,
                    workflows=False, checks=False, statuses=False, diagnostic=diagnostic)
                permission = journey["prerequisites"]["permissions"]
                if build == "missing":
                    self.assertEqual(permission["reasonCode"], diagnostic["reasonCode"])
                    self.assertEqual(permission["reason"], diagnostic["reason"])
                else:
                    self.assertEqual(permission["status"], "complete")
                    self.assertEqual(permission["reasonCode"], "")
                    self.assertIsNone(journey["nextAction"])
                    self.assertTrue(journey["capabilities"]["canPublishArticle"])

    def test_permission_failure_prompts_only_for_a_step_using_that_grant(self):
        from .website_prepare import prepare_failure
        from .website_contract import WebsiteAuthorityError
        needed = {"github_workflow_permission_required": {"permissions", "setup"},
            "github_ci_evidence_permission_required": {"permissions", "verify", "ci-attestation"}}
        steps = {"permissions", "setup", "verify", "ci-attestation", "scan", "preview", "publish", "generate", "disconnect"}
        for code, needed_steps in needed.items():
            for step, supplied_action in product(steps, (False, True)):
                with self.subTest(code=code, step=step, supplied_action=supplied_action), \
                        patch("content_factory.website_github_permissions.installation_grants", return_value={"settingsUrl": "https://github.com/settings/installations/4"}) as grants:
                    fixture = fixtures.PrepareJourneyReplayTests()
                    fixture.setUp()
                    error = WebsiteAuthorityError(code, "Review permissions.", retryable=False)
                    if supplied_action:
                        error.user_action = {"id": "review_github_permissions", "url": "https://github.com/settings/installations/4"}
                    prepare_failure(fixture.op, step, error)
                    if step in needed_steps:
                        self.assertEqual(fixture.op.state, "needs_user")
                        self.assertEqual(fixture.op.receipt["user_action"]["id"], "review_github_permissions")
                    else:
                        self.assertEqual(fixture.op.state, "needs_attention")
                        self.assertEqual(fixture.op.receipt["user_action"]["id"], "retry")
                        grants.assert_not_called()

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

    def test_setup_child_permission_failure_uses_the_same_installation_prompt(self):
        fixture = fixtures.PrepareJourneyReplayTests()
        fixture.setUp()
        fixture.op.receipt = {"child_run_id": "setup-child", "current_step": "setup"}
        child = SimpleNamespace(status="blocked", workflow="article_system_setup", error="Workflows permission is required.",
            result={"error_code": "github_workflow_permission_required", "retryable": False})
        with patch("content_factory.website_github_permissions.installation_grants", return_value={"settingsUrl": "https://github.com/settings/installations/4"}):
            fixture.advance(child=child)
        self.assertEqual(fixture.op.state, "needs_user")
        self.assertEqual(fixture.op.receipt["user_action"]["id"], "review_github_permissions")
        self.assertEqual(fixture.op.receipt["message"], child.error)

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
