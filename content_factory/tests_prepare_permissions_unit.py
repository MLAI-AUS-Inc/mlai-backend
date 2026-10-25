"""GitHub permission prompts use installation grants, never minted tokens."""
from types import SimpleNamespace
from itertools import product
from unittest.mock import patch, Mock

from django.test import SimpleTestCase

from . import tests_prepare_journey_unit as fixtures
from .website_journey import project_journey
from .activation import article_capabilities


class PrepareVerificationRuntimeTests(SimpleTestCase):
    """Run actual verification and, after 1.2, the frozen preparation chain."""

    def setUp(self):
        from contextlib import ExitStack, nullcontext
        from copy import deepcopy
        from unittest.mock import MagicMock
        from django.contrib.auth import get_user_model
        from django.utils import timezone
        from founder_tools.services import FounderCompanyContext
        from organizations.models import Organization
        from .models import OrganizationContentConfig
        from .website_connections import contract_for
        from .website_contract import evidence_digest
        from .website_models import WebsiteConnection, WebsiteConnectionTarget, WebsiteConnectionOperation, WebsiteScanSnapshot
        self.now = timezone.now()
        self.user = get_user_model()(pk=4, is_active=True)
        self.org = Organization(pk=7, domain="fixture.test")
        self.website = WebsiteConnection(organization=self.org, authorized_by=self.user, generation=3,
            github_repo="fixture/site", repository_id=123, installation_id="4", branch="main", verified_sha="a" * 40,
            last_verified_at=self.now, capabilities={"publishingReady": True, "generationReady": True, "previewSupported": True})
        self.website.save = Mock()
        self.config = OrganizationContentConfig(organization=self.org, website_connection=self.website,
            github_repo=self.website.github_repo, default_publish_target_id="native")
        self.config.save = Mock()
        self.prepare = WebsiteConnectionOperation(connection=self.website, generation=3, action="prepare", state="pending",
            updated_at=self.now, payload={"company_id": "9", "requested_by_user_id": "4", "attempt": 1},
            receipt={"current_step": "verify"})
        self.prepare.save = Mock()
        self.origin = WebsiteConnectionOperation(connection=self.website, generation=3, action="workflow", state="completed",
            payload={"attempt": 1}, updated_at=self.now)
        self.sealed = {**contract_for(self.website), "github_repo": self.website.github_repo, "source_sha": self.website.verified_sha,
            "target_id": "native", "contract_digest": "c" * 64, "operation_id": str(self.origin.pk), "operation_attempt": 1,
            "deletion_epoch": 0, "baseline_build": "passed", "patched_build": "passed", "adapter_id": "react_component",
            "adapter_version": "1", "source_tree_sha": "d" * 40, "environment_fingerprint": "fixture", "lockfile_digests": {},
            "artifact_digest": "e" * 64, "verified_routes": {"listing": "/articles", "detail": "/articles/known", "unknown_slug": "/articles/missing"}}
        from .website_verification import BOOLEAN_PROOFS
        self.sealed.update({field: True for field in BOOLEAN_PROOFS})
        self.sealed["evidence_digest"] = evidence_digest(self.sealed)
        self.ci = WebsiteConnectionOperation(connection=self.website, generation=3, action="ci-verify", state="completed",
            updated_at=self.now, payload=deepcopy(self.sealed), receipt={**self.sealed, "allowed": True,
                "attestation_origin": "authenticated_ci", "checked_at": self.now.isoformat()})
        self.target = WebsiteConnectionTarget(connection=self.website, generation=3, target_key="native", adapter="react_component",
            source_sha=self.website.verified_sha, verified_at=self.now, capabilities={"publishingReady": True}, contract={
                "contract_digest": "c" * 64, "route_path": "/articles", "route_template": "/articles/{slug}",
                "live_marker": {"kind": "artifact_digest", "value": "e" * 64}, "verification": {**self.sealed, "status": "verified"}})
        self.target.save, self.target.refresh_from_db = Mock(), Mock()
        self.inventory = WebsiteScanSnapshot(connection=self.website, generation=3, source_sha=self.website.verified_sha,
            evidence={"scan_complete": True}, detector_version="fixture")
        self.context = FounderCompanyContext(profile=SimpleNamespace(user=self.user),
            company=SimpleNamespace(pk=9), organization=self.org)
        self.live = None
        self.ci_present = True
        self.checks, self.statuses = False, False
        self.workflows = False
        self.live_error = None
        self.ExitStack, self.nullcontext, self.MagicMock = ExitStack, nullcontext, MagicMock

    def rows(self, *, first=None, items=(), exists=False):
        row = self.MagicMock()
        for method in ("filter", "exclude", "order_by", "select_related", "select_for_update", "prefetch_related", "values_list"):
            getattr(row, method).return_value = row
        row.first.return_value = row.get.return_value = first
        row.exists.return_value = exists
        row.__iter__.side_effect = lambda: iter(items)
        row.__getitem__.return_value = list(items)
        row.update.return_value = 1
        return row

    def dispatch_user(self):
        """Keep direct regressions compatible with the separate 1.2 signature."""
        from inspect import signature
        from .website_prepare import _dispatch_prepare_step
        return {"user": self.user} if "user" in signature(_dispatch_prepare_step).parameters else {}

    def boundaries(self):
        import base64, json
        from django.contrib.auth import get_user_model
        from integrations.models import GitHubInstallation
        from integrations.services import github_app
        from organizations.models import Organization
        from workflow_runs.models import ContentFactoryRun
        from . import website_prepare as prepare, website_verification as verification, vibe_marketing_views as views
        from .models import OrganizationContentConfig
        from .website_models import WebsiteConnection, WebsiteConnectionOperation
        stack = self.ExitStack()
        stack.enter_context(patch("startup_updates.benefits.has_monthly_update_discount", return_value=False))
        stack.enter_context(patch.object(prepare.transaction, "atomic", side_effect=lambda *a, **kw: self.nullcontext()))
        stack.enter_context(patch.object(get_user_model().objects, "get", return_value=self.user))
        stack.enter_context(patch.object(views, "get_founder_company_context", return_value=self.context))
        stack.enter_context(patch.object(views, "_get_config", return_value=self.config))
        stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "select_for_update", return_value=self.rows(first=self.prepare)))
        stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "filter", return_value=self.rows()))
        stack.enter_context(patch.object(WebsiteConnection.objects, "select_related", return_value=self.rows(first=self.website)))
        stack.enter_context(patch.object(WebsiteConnection.objects, "select_for_update", return_value=self.rows(first=self.website)))
        stack.enter_context(patch.object(Organization.objects, "select_for_update", return_value=self.rows(first=self.org)))
        stack.enter_context(patch.object(OrganizationContentConfig.objects, "filter", return_value=self.rows(first=self.config, exists=True)))
        stack.enter_context(patch.object(OrganizationContentConfig.objects, "get", return_value=self.config))
        stack.enter_context(patch.object(ContentFactoryRun.objects, "filter", return_value=self.rows()))
        stack.enter_context(patch.object(WebsiteConnection, "targets", self.rows(first=self.target)))
        stack.enter_context(patch.object(WebsiteConnection, "scan_snapshots", self.rows(first=self.inventory)))
        stack.enter_context(patch.object(WebsiteConnection, "repository_mutations", self.rows()))
        operations = self.rows(first=self.prepare)
        def operation_rows(**kwargs):
            if "pk" in kwargs:
                return self.rows(first=self.origin if str(kwargs["pk"]) == str(self.origin.pk) else None)
            if kwargs.get("action") == "ci-verify":
                return self.rows(first=self.ci if self.ci_present else None)
            if kwargs.get("action") == "deployment-verify":
                return self.rows(first=self.live)
            return self.rows(first=self.prepare if "action__in" in kwargs else None)
        operations.filter.side_effect = operation_rows
        stack.enter_context(patch.object(WebsiteConnection, "operations", operations))
        def store_operation(*, defaults, **kwargs):
            op = WebsiteConnectionOperation(**defaults, updated_at=self.now)
            if op.action == "deployment-verify":
                self.live = op
            if op.action == "ci-verify":
                self.ci = op
            return op, True
        stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "update_or_create", side_effect=store_operation))
        stack.enter_context(patch.object(prepare.transaction, "on_commit"))
        stack.enter_context(patch.object(GitHubInstallation.objects, "filter", return_value=self.rows()))
        stack.enter_context(patch.object(github_app, "probe_installation_liveness", return_value=github_app.INSTALLATION_LIVE))
        def credential(**kwargs):
            if kwargs.get("permission_profile") == "ci_evidence" and not (self.checks and self.statuses):
                raise github_app.GitHubCIEvidencePermissionRequired("Missing fixture CI read grants")
            return SimpleNamespace(token="synthetic")
        self.tokens = stack.enter_context(patch.object(github_app, "create_installation_access_token", side_effect=credential))
        def http_get(url, **kwargs):
            response = Mock(status_code=200)
            if "/check-runs" in url:
                response.json.return_value = {"check_runs": [{"name": verification.CHECK_NAME, "head_sha": self.website.verified_sha,
                    "status": "completed", "conclusion": "success", "app": {"slug": "github-actions"}, "output": {
                        "summary": "MLAI_ARTICLES_ATTESTATION:" + base64.b64encode(json.dumps(self.sealed).encode()).decode()}}]}
            else:
                response.json.return_value = {"sha": self.website.verified_sha}
            return response
        self.http = stack.enter_context(patch.object(views.http_client, "get", side_effect=http_get))
        stack.enter_context(patch.object(views.http_client, "delete"))
        def cached(key):
            if key.startswith("articles-repository-access-v2:"):
                return {"verified": True, "writable": True, "branch": "main", "sha": self.website.verified_sha}
            if key.startswith("website-installation-grants-v1:"):
                return {"workflows": self.workflows, "checks": self.checks, "statuses": self.statuses, "settingsUrl": "https://github.com/settings/installations/4"}
            return None
        stack.enter_context(patch.object(views.cache, "get", side_effect=cached))
        for method, result in (("set", None), ("add", True), ("delete", None)):
            stack.enter_context(patch.object(views.cache, method, return_value=result))
        def fetch(*args, **kwargs):
            if self.live_error:
                raise self.live_error
            return b"fixture", {"x-mlai-artifact-digest": "e" * 64}
        self.fetch = stack.enter_context(patch("content_factory.website_live_fetch.fetch_live_route", side_effect=fetch))
        return stack

    def test_only_the_owner_view_reports_a_source_check_as_running(self):
        from .website_journey import journey_for_context
        check = {"operationId": "check-1", "reason": "Checking your latest website changes."}
        with self.boundaries(), patch("content_factory.website_journey.source_check_in_flight", return_value=check) as found:
            runner = journey_for_context(self.context, self.config)
            owner = journey_for_context(self.context, self.config, owner_view=True)
        # Preparation keeps choosing its step from the unmasked facts.
        found.assert_called_once()
        self.assertEqual(runner["nextAction"]["id"], "verify")
        self.assertIsNone(owner["nextAction"])
        self.assertEqual((owner["prerequisites"]["deployment"]["status"], owner["prerequisites"]["deployment"]["operationId"]),
            ("running", "check-1"))

    def test_fresh_saved_proof_verifies_live_without_optional_ci_grants(self):
        from . import website_prepare as prepare
        from .website_journey import journey_for_context
        with self.boundaries():
            journey = journey_for_context(self.context, self.config)
            self.assertEqual(journey["nextAction"]["id"], "verify")
            self.assertEqual(journey["prerequisites"]["permissions"]["status"], "complete")
            result = prepare._dispatch_prepare_step(self.prepare, self.context, self.config, "verify", journey=journey, **self.dispatch_user())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.fetch.call_count, 3)
        # The branch head, then the merged kit's marker routes at the exact source.
        self.assertEqual(self.http.call_count, 2)
        self.assertIn("/contents/.mlai/articles-verification.json?ref=", self.http.call_args_list[1].args[0])
        self.assertFalse(any(call.kwargs.get("permission_profile") == "ci_evidence" for call in self.tokens.call_args_list))

    def test_missing_or_stale_proof_still_requires_ci_read_grants(self):
        from datetime import timedelta
        from . import website_prepare as prepare
        from .website_contract import WebsiteAuthorityError
        original = dict(self.target.contract)
        for state in ("missing", "stale"):
            self.target.contract = {**original, "verification": {}} if state == "missing" else original
            self.target.verified_at = self.now - timedelta(days=8) if state == "stale" else self.now
            with self.subTest(state=state), self.boundaries():
                with self.assertRaises(WebsiteAuthorityError) as raised:
                    prepare._dispatch_prepare_step(self.prepare, self.context, self.config, "verify", **self.dispatch_user())
                self.assertEqual(raised.exception.code, "github_ci_evidence_permission_required")
                self.fetch.assert_not_called()

    def test_current_proof_does_not_bypass_live_marker_checks(self):
        from . import website_prepare as prepare
        from .website_contract import WebsiteAuthorityError
        self.live_error = WebsiteAuthorityError("deployment_source_unverified", "The deployed source marker is missing.", retryable=True)
        with self.boundaries():
            with self.assertRaises(WebsiteAuthorityError) as raised:
                prepare._dispatch_prepare_step(self.prepare, self.context, self.config, "verify", **self.dispatch_user())
            self.assertEqual(raised.exception.code, "deployment_source_unverified")
            self.assertIsNone(self.live)

    def test_observed_source_and_branch_drift_cannot_reuse_current_receipt(self):
        from . import website_prepare as prepare
        from .website_contract import WebsiteAuthorityError
        for repository in ({"observedSourceSha": "b" * 40, "branch": "main"},
                {"observedSourceSha": self.website.verified_sha, "branch": "other"}):
            with self.subTest(repository=repository), self.boundaries():
                with self.assertRaises(WebsiteAuthorityError) as raised:
                    prepare._dispatch_prepare_step(self.prepare, self.context, self.config, "verify",
                        journey={"repository": repository}, **self.dispatch_user())
                self.assertEqual(raised.exception.code, "github_ci_evidence_permission_required")
                self.fetch.assert_not_called()

    def test_explicit_ci_attestation_still_reads_provider_even_for_fresh_receipt(self):
        from . import website_prepare as prepare
        self.checks = self.statuses = True
        with self.boundaries():
            result = prepare._dispatch_prepare_step(self.prepare, self.context, self.config, "ci-attestation", **self.dispatch_user())
            self.assertEqual(result["status"], "completed")
            self.assertEqual(sum(call.kwargs.get("permission_profile") == "ci_evidence" for call in self.tokens.call_args_list), 2)
            self.fetch.assert_not_called()

    def test_saved_proof_reuse_is_scoped_to_current_identity_and_attempt(self):
        from copy import deepcopy
        from . import website_verification as verification
        original = deepcopy(self.target.contract)
        changes = ({"connection_generation": 2}, {"website_connection_id": "another"}, {"repository_id": 999},
            {"github_repo": "other/site"}, {"source_sha": "b" * 40}, {"target_id": "other"}, {"contract_digest": "f" * 64},
            {"deletion_epoch": 1}, {"operation_attempt": 2}, {"operation_id": "another"}, {"evidence_digest": "f" * 64})
        for change in changes:
            self.target.contract = {**original, "verification": {**original["verification"], **change}}
            with self.subTest(change=change), self.boundaries():
                self.assertIsNone(verification.current_ci_attestation(self.website, self.target))
        self.target.contract = original
        self.ci_present = False
        with self.boundaries():
            self.assertIsNone(verification.current_ci_attestation(self.website, self.target))

    def test_saved_ci_identity_does_not_require_optional_provider_fields(self):
        from . import website_verification as verification
        for key in ("domain", "branch", "app_root"):
            self.target.contract["verification"].pop(key)
            self.ci.receipt.pop(key)
        with self.boundaries():
            self.assertEqual(verification.current_ci_attestation(self.website, self.target), self.ci.receipt)

    def test_preview_only_or_missing_mismatched_ci_proof_needs_grants_in_projection_and_dispatch(self):
        from . import website_prepare as prepare
        from .website_journey import journey_for_context
        from .website_contract import WebsiteAuthorityError
        for state in ("preview_only", "missing_ci", "mismatched_ci", "stale_ci"):
            self.setUp()
            if state == "preview_only":
                self.target.verified_at = None
                self.target.contract["verification"].update(status="preview_verified", checked_at=self.now.isoformat())
            if state == "missing_ci":
                self.ci_present = False
            if state == "mismatched_ci":
                self.ci.receipt["operation_attempt"] = 2
            if state == "stale_ci":
                from datetime import timedelta
                self.ci.receipt["checked_at"] = (self.now - timedelta(days=8)).isoformat()
            with self.subTest(state=state), self.boundaries():
                journey = journey_for_context(self.context, self.config)
                self.assertEqual(journey["nextAction"]["id"], "permissions")
                self.assertEqual(journey["prerequisites"]["permissions"]["reasonCode"], "github_ci_evidence_permission_required")
                self.assertEqual(journey["prerequisites"]["verification"]["reasonCode"], "ci_attestation_required")
                with self.assertRaises(WebsiteAuthorityError) as raised:
                    prepare._dispatch_prepare_step(self.prepare, self.context, self.config, "verify", journey=journey, **self.dispatch_user())
                self.assertEqual(raised.exception.code, "github_ci_evidence_permission_required")
                self.fetch.assert_not_called()

    def test_runtime_grant_truth_table_reuses_current_proof_and_reattests_other_proof(self):
        from datetime import timedelta
        from . import website_prepare as prepare
        from .website_contract import WebsiteAuthorityError
        for state, checks, statuses in product(("fresh", "missing", "stale"), (False, True), (False, True)):
            self.setUp()
            self.checks, self.statuses = checks, statuses
            if state == "missing":
                self.target.contract["verification"] = {}
            if state == "stale":
                self.target.verified_at = self.now - timedelta(days=8)
            with self.subTest(state=state, checks=checks, statuses=statuses), self.boundaries():
                if state != "fresh" and not (checks and statuses):
                    with self.assertRaises(WebsiteAuthorityError) as raised:
                        prepare._dispatch_prepare_step(self.prepare, self.context, self.config, "verify", **self.dispatch_user())
                    self.assertEqual(raised.exception.code, "github_ci_evidence_permission_required")
                    self.fetch.assert_not_called()
                else:
                    result = prepare._dispatch_prepare_step(self.prepare, self.context, self.config, "verify", **self.dispatch_user())
                    self.assertEqual(result["status"], "completed")
                    self.assertEqual(self.fetch.call_count, 3)
                    reads = [call for call in self.tokens.call_args_list if call.kwargs.get("permission_profile") == "ci_evidence"]
                    self.assertEqual(len(reads), 0 if state == "fresh" else 2)

    def test_real_chain_fresh_build_without_ci_grants_still_checks_live_deployment(self):
        from inspect import signature
        from datetime import timedelta
        from . import website_prepare as prepare
        if "user" not in signature(prepare._owner_request).parameters:
            self.skipTest("Requires earlier Phase 1.2 frozen-context fix")
        for checks, statuses in product((False, True), repeat=2):
            self.setUp()
            self.checks, self.statuses = checks, statuses
            with self.subTest(checks=checks, statuses=statuses), self.boundaries():
                prepare.advance_prepare(self.prepare.pk)
                self.assertEqual(self.prepare.state, "pending", self.prepare.receipt)
                self.assertIsNotNone(self.live)
                self.assertEqual(self.fetch.call_count, 3)
                self.assertFalse(any(call.kwargs.get("permission_profile") == "ci_evidence" for call in self.tokens.call_args_list))
                prepare.advance_prepare(self.prepare.pk, now=self.now + timedelta(minutes=2))
                self.assertEqual(self.prepare.state, "completed", self.prepare.receipt)
                self.assertEqual(self.prepare.receipt["status"], "ready")
                self.assertNotIn("prepare_user", self.context.__dict__)

    def test_real_chain_incomplete_ci_stops_at_permissions_and_current_ci_keeps_live_failure(self):
        from inspect import signature
        from datetime import timedelta
        from . import website_prepare as prepare
        from .website_contract import WebsiteAuthorityError
        if "user" not in signature(prepare._owner_request).parameters:
            self.skipTest("Requires earlier Phase 1.2 frozen-context fix")
        for state in ("preview_only", "missing_ci", "mismatched_ci", "stale", "missing", "live_failure"):
            self.setUp()
            if state == "preview_only":
                self.target.verified_at = None
                self.target.contract["verification"].update(status="preview_verified", checked_at=self.now.isoformat())
            if state == "missing_ci":
                self.ci_present = False
            if state == "mismatched_ci":
                self.ci.receipt["operation_attempt"] = 2
            if state == "stale":
                self.target.verified_at = self.now - timedelta(days=8)
            if state == "missing":
                self.target.contract["verification"] = {}
                self.ci_present = False
                self.workflows = True
            if state == "live_failure":
                self.live_error = WebsiteAuthorityError("deployment_source_unverified", "Current live marker missing.", retryable=True)
            with self.subTest(state=state), self.boundaries():
                prepare.advance_prepare(self.prepare.pk)
                if state == "live_failure":
                    self.assertEqual(self.prepare.state, "pending", self.prepare.receipt)
                    self.assertEqual(self.prepare.receipt["status"], "waiting_for_deployment")
                    self.assertEqual(self.prepare.receipt["reason_code"], "deployment_source_unverified")
                    self.assertIsNotNone(self.prepare.next_attempt_at)
                else:
                    self.assertEqual(self.prepare.state, "needs_user", self.prepare.receipt)
                    self.assertEqual(self.prepare.receipt["current_step"], "permissions")
                    self.assertEqual(self.prepare.receipt["reason_code"], "github_ci_evidence_permission_required")
                    self.fetch.assert_not_called()

    def test_real_chain_already_deployed_legacy_site_needs_no_ci_receipt_or_grants(self):
        from inspect import signature
        from . import website_prepare as prepare
        from .website_journey import journey_for_context
        from .website_models import WebsiteConnectionOperation
        if "user" not in signature(prepare._owner_request).parameters:
            self.skipTest("Requires earlier Phase 1.2 frozen-context fix")
        self.ci_present = False
        self.live = WebsiteConnectionOperation(connection=self.website, generation=3, action="deployment-verify", state="completed",
            updated_at=self.now, payload={"source_sha": self.website.verified_sha, "target_id": "native"}, receipt={
                "status": "passed", "source_sha": self.website.verified_sha, "connection_generation": 3, "target_id": "native",
                "artifact_digest": "e" * 64, "contract_digest": "c" * 64,
                "public_url": "https://fixture.test/articles", "checked_at": self.now.isoformat()})
        with self.boundaries():
            journey = journey_for_context(self.context, self.config)
            self.assertTrue(journey["capabilities"]["canPublishArticle"])
            self.assertEqual(journey["prerequisites"]["permissions"]["status"], "complete")
            self.assertNotIn("permissions", journey["allowedActions"])
            self.assertIsNone(journey["nextAction"])
            prepare.advance_prepare(self.prepare.pk)
            self.assertEqual(self.prepare.state, "completed", self.prepare.receipt)
            self.assertEqual(self.prepare.receipt["status"], "ready")
            self.tokens.assert_not_called()
            self.http.assert_not_called()
            self.fetch.assert_not_called()


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
