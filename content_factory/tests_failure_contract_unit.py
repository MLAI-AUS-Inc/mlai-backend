"""B1 pure policy, fingerprint and provider regressions; never create a database."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from .website_contract import WebsiteAuthorityError
from . import website_prepare as prepare
from . import tests_one_click_operations_unit as operation_fixtures


@override_settings(ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=("7",))
class FailurePolicyTests(SimpleTestCase):
    def setUp(self):
        from .website_failure_scope import failure_scope
        scope = failure_scope(7)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)

    def operation(self):
        return operation_fixtures.PrepareJourneyTests().operation()

    def test_authority_uses_registry_not_legacy_boolean(self):
        body = WebsiteAuthorityError("TEMPLATE_VALIDATION_FAILED", "Raw provider text", retryable=True).as_dict()
        self.assertFalse(body["retryable"])
        self.assertEqual(body["failure"]["failure_class"], "repairable")
        self.assertEqual(body["failure_class"], "repairable")
        self.assertNotIn("Raw provider text", str(body["failure"]))

    def test_waiting_is_observation_without_failure_charge(self):
        op = self.operation()
        prepare.prepare_failure(op, "verify", WebsiteAuthorityError("live_route_unavailable", "Waiting."))
        self.assertEqual(op.state, "pending")
        self.assertEqual(op.receipt.get("step_failures", {}).get("verify", 0), 0)
        self.assertEqual(op.receipt["failure"]["failure_class"], "waiting")
        self.assertFalse(op.receipt["retryable"])

    def test_internal_unknown_stops_without_owner_or_channel_action(self):
        op = self.operation()
        prepare.prepare_failure(op, "setup", WebsiteAuthorityError("foreign arbitrary secret=xyz", "Unknown", retryable=True))
        self.assertEqual(op.state, "needs_attention")
        self.assertEqual(op.receipt["reason_code"], "UNREGISTERED_FAILURE_CODE")
        self.assertIsNone(op.next_attempt_at)
        self.assertNotIn("user_action", op.receipt)
        self.assertNotIn("channel_options", op.receipt)
        self.assertNotIn("foreign arbitrary", str(op.receipt))

    def test_six_class_truth_table(self):
        rows = (("worker_unavailable", "transient", "pending"),
                ("live_route_unavailable", "waiting", "pending"),
                ("TEMPLATE_VALIDATION_FAILED", "repairable", "needs_attention"),
                ("ARTICLE_DIRECTORY_AGENT_BUDGET", "strategy", "needs_attention"),
                ("github_workflow_permission_required", "needs_user", "needs_user"),
                ("invalid_connection_contract", "unsupported", "needs_attention"))
        for code, classification, state in rows:
            with self.subTest(code=code):
                op = self.operation()
                error = WebsiteAuthorityError(code, "Fixture", retryable=True)
                if classification == "needs_user":
                    error.user_action = {"id": "review_github_permissions", "label": "Review permissions", "url": "https://github.com/settings/installations/123"}
                prepare.prepare_failure(op, "setup", error)
                self.assertEqual(op.state, state)
                self.assertEqual(op.receipt["failure"]["failure_class"], classification)
                self.assertEqual(op.receipt["retryable"], classification == "transient")
                self.assertEqual(op.next_attempt_at is not None, classification in {"transient", "waiting"})

    def test_versioned_mismatch_cannot_take_transient_legacy_fallback(self):
        from .website_failure_contract import classify_failure
        failure = classify_failure("DIRECTORY_STYLE_REVIEW_FAILED", "directory_style")
        failure["failure_class"] = "transient"
        error = WebsiteAuthorityError("worker_unavailable", "Retry", retryable=True)
        error.result = {"failure": failure, "retryable": True, "failure_class": "transient"}
        op = self.operation()
        prepare.prepare_failure(op, "setup", error)
        self.assertEqual(op.state, "needs_attention")
        self.assertEqual(op.receipt["reason_code"], "FAILURE_CONTRACT_INVALID")
        self.assertIsNone(op.next_attempt_at)

    def test_versioned_duplicate_and_preview_aliases_cannot_diverge(self):
        from .website_failure_contract import classify_failure
        failure = classify_failure("DIRECTORY_STYLE_REVIEW_FAILED", "directory_style")
        other = classify_failure("worker_unavailable", "directory_style")
        for extra in ({"article_system_setup": {"failure": other}}, {"errorCode": other["code"]},
                      {"live_preview": {"errorCode": other["code"]}}):
            with self.subTest(extra=extra):
                error = WebsiteAuthorityError("worker_unavailable", "Retry", retryable=True)
                error.result = {"failure": failure, **extra}
                op = self.operation()
                prepare.prepare_failure(op, "setup", error)
                self.assertEqual(op.receipt["reason_code"], "FAILURE_CONTRACT_INVALID")
                self.assertEqual(op.state, "needs_attention")
                self.assertIsNone(op.next_attempt_at)

    def test_registry_failure_immutable_in_receipt(self):
        from .website_failure_contract import classify_failure
        failure = classify_failure("ARTICLE_DIRECTORY_AGENT_BUDGET", "agent", [{"observed_count": 24}])
        error = WebsiteAuthorityError(failure["code"], "Budget")
        error.result = {"failure": failure, "retryable": False}
        op = self.operation()
        prepare.prepare_failure(op, "setup", error)
        self.assertEqual(op.receipt["failure"], failure)
        self.assertEqual(failure["failure_class"], "strategy")

    @override_settings(ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=False, ARTICLES_FAILURE_TAXONOMY_OBSERVE_ENABLED=False)
    def test_flags_off_preserves_legacy_constructor_exact_shape(self):
        error = WebsiteAuthorityError("arbitrary-historical-code", "Historical", retryable=True)
        self.assertEqual(error.as_dict(), {"allowed": False, "error": "arbitrary-historical-code", "code": "arbitrary-historical-code",
            "detail": "Historical", "retryable": True, "fieldErrors": []})

    @override_settings(ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=False)
    def test_flags_off_versioned_input_gets_audited_v0_view(self):
        from .website_failure_contract import classify_failure
        failure = classify_failure("github_provider_payload_invalid", "repository_access")
        error = WebsiteAuthorityError("worker_unavailable", "Historical")
        error.result = {"failure": failure, "failure_class": failure["failure_class"], "retryable": failure["retryable"]}
        op = self.operation()
        prepare.prepare_failure(op, "verify-access", error)
        self.assertEqual(op.receipt["reason_code"], "github_repository_unavailable")
        self.assertEqual(op.receipt["failure_class"], "needs_user")
        self.assertEqual(op.state, "needs_user")  # Phase A's exact authorization code gate.
        self.assertIsNone(op.next_attempt_at)


class FingerprintTests(SimpleTestCase):
    def test_fingerprint_order_duplicates_and_volatile_origin_converge(self):
        from .website_failure_contract import classify_failure
        a = [{"slot": "article", "repo_path": "src/Card2.tsx", "message": "secret=discard", "run_id": "one"},
             {"route_role": "listing", "route_path": "https://preview-one.test/articles", "required_count": 3}]
        b = [deepcopy(a[1]), deepcopy(a[0]), deepcopy(a[0])]
        b[0]["route_path"] = "https://preview-two.test/articles"
        b[1]["message"] = "different discarded prose"
        self.assertEqual(classify_failure("DIRECTORY_STYLE_REVIEW_FAILED", "style", a)["failure_fingerprint"],
                         classify_failure("DIRECTORY_STYLE_REVIEW_FAILED", "style", b)["failure_fingerprint"])

    def test_semantic_slot_import_case_counts_and_http_status_remain_distinct(self):
        from .website_failure_contract import classify_failure
        values = [{"slot": "article"}, {"slot": "article2"}, {"import": "src/Card.tsx"}, {"import": "src/card.tsx"},
                  {"required_count": 2}, {"required_count": 3}, {"http_status": 403}, {"http_status": 429}]
        fingerprints = [classify_failure("DIRECTORY_STYLE_REVIEW_FAILED", "style", [value])["failure_fingerprint"] for value in values]
        self.assertEqual(len(set(fingerprints)), len(values))

    def test_secret_and_impossible_evidence_become_contract_failure(self):
        from .website_failure_contract import classify_failure
        for findings in ([{"repo_path": "/tmp/private/source.py"}], [{"route_path": "https://p.test/articles?token=secret"}],
                         [{"adapter": "ghp_private"}], [{"http_status": True}], [{"unexpected": "value"}], [{}] * 65):
            with self.subTest(findings=findings):
                failure = classify_failure("DIRECTORY_STYLE_REVIEW_FAILED", "style", findings)
                self.assertEqual(failure["code"], "FAILURE_EVIDENCE_INVALID")
                self.assertEqual(failure["failure_class"], "unsupported")
                self.assertNotIn("private", str(failure))

    def test_decoder_strict_types_identity_and_aliases(self):
        from .website_failure_contract import classify_failure, validate_failure, FailureContractError
        original = classify_failure("DIRECTORY_STYLE_REVIEW_FAILED", "style")
        self.assertEqual(validate_failure(original), original)
        for key, value in (("code", {}), ("schema_version", True), ("registry_version", True), ("fingerprint_version", True),
                           ("registry_sha256", "0" * 64), ("retryable", True), ("failure_class", "transient"),
                           ("action", {"id": "authorize_github", "label": "Authorize", "url": None})):
            with self.subTest(key=key):
                modified = {**original, key: value}
                with self.assertRaises(FailureContractError):
                    validate_failure(modified)

@override_settings(ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=("7",))
class ProviderAndEnrollmentTests(SimpleTestCase):
    def setUp(self):
        from .website_failure_scope import failure_scope
        scope = failure_scope(7)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)

    def response(self, status=200, body=None, headers=None):
        import json
        import requests
        response = requests.Response()
        response.status_code = status
        response.url = "https://api.github.com/repos/fixture/site"
        response.headers.update(headers or {})
        response._content = json.dumps(body if body is not None else {"id": 42, "full_name": "fixture/site", "default_branch": "main"}).encode()
        return response

    def test_actual_repository_reader_splits_network_identity_payload_and_unproved_access(self):
        from integrations.models import GitHubInstallation
        from integrations.services import github_app
        from integrations import http_client
        from .website_connections import verify_repository_access
        from django.contrib.auth import get_user_model
        user = get_user_model()(pk=4)
        installation = SimpleNamespace(installation_id="4", account_login="fixture")
        for response, expected in ((self.response(503), "github_repository_transient"),
                (self.response(429), "github_repository_transient"),
                (self.response(403, headers={"X-RateLimit-Remaining": "0"}), "github_repository_transient"),
                (self.response(403), "github_access_unverified"), (self.response(404), "github_access_unverified"),
                (self.response(body=[]), "github_provider_payload_invalid"),
                (self.response(body={"id": True, "full_name": "fixture/site", "default_branch": "main"}), "github_provider_payload_invalid"),
                (self.response(body={"id": 42, "full_name": "other/site", "default_branch": "main"}), "github_repository_identity_mismatch")):
            with self.subTest(expected=expected), \
                    patch.object(GitHubInstallation.objects, "filter", return_value=[installation]), \
                    patch.object(github_app, "create_installation_access_token", return_value=SimpleNamespace(token="synthetic-read")), \
                    patch.object(http_client, "get", return_value=response):
                with self.assertRaises(WebsiteAuthorityError) as raised:
                    verify_repository_access(user=user, repo="fixture/site")
                self.assertEqual(raised.exception.code, expected)
                self.assertEqual(raised.exception.retryable, expected == "github_repository_transient")
                self.assertNotEqual(raised.exception.failure["failure_class"], "needs_user")

    def test_ci_reader_does_not_retry_malformed_success_as_provider_outage(self):
        from integrations import http_client
        from .website_tokens import read_ci_provider_json
        with patch.object(http_client, "get", return_value=self.response(body=[])):
            with self.assertRaises(WebsiteAuthorityError) as raised:
                read_ci_provider_json("https://api.github.com/repos/fixture/site/check-runs", headers={})
        self.assertEqual(raised.exception.code, "github_ci_payload_invalid")
        self.assertFalse(raised.exception.retryable)

    def test_permission_payload_and_identity_are_not_owner_actions(self):
        from integrations.services import github_app
        from .website_github_failure import github_failure
        for exc, expected in ((github_app.GitHubPermissionPayloadInvalid("Bad JSON"), "github_permission_payload_invalid"),
                              (github_app.GitHubPermissionIdentityMismatch("Wrong ID"), "github_permission_identity_mismatch"),
                              (github_app.GitHubPlatformConfigurationInvalid("Service credential"), "github_platform_credentials_invalid")):
            error = github_failure(exc, legacy_code="github_unavailable", legacy_message="Legacy", legacy_retryable=True)
            self.assertEqual(error.code, expected)
            self.assertFalse(error.retryable)
            self.assertIsNone(error.failure["action"])
        proved = github_failure(github_app.GitHubInstallationAccessRequired("Suspended"), legacy_code="github_access_required", legacy_message="Legacy")
        self.assertEqual(proved.failure["failure_class"], "needs_user")
        self.assertEqual(proved.failure["action"]["id"], "authorize_github")

    @override_settings(ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=())
    def test_empty_allowlist_preserves_constructor_even_with_switch_on(self):
        body = WebsiteAuthorityError("TEMPLATE_VALIDATION_FAILED", "Legacy", retryable=True).as_dict()
        self.assertTrue(body["retryable"])
        self.assertNotIn("failure", body)

    @override_settings(ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=False, ARTICLES_FAILURE_TAXONOMY_OBSERVE_ENABLED=True)
    def test_shadow_reports_difference_without_changing_phase_a_transition(self):
        op = operation_fixtures.PrepareJourneyTests().operation()
        prepare.prepare_failure(op, "setup", WebsiteAuthorityError("TEMPLATE_VALIDATION_FAILED", "Legacy", retryable=True))
        self.assertEqual(op.state, "pending")
        self.assertEqual(op.receipt["failure_class"], "transient")
        self.assertEqual(op.receipt["failure_taxonomy_observation"]["classification"], "repairable")
        self.assertNotIn("failure", op.receipt)

    @override_settings(ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=False, ARTICLES_FAILURE_TAXONOMY_OBSERVE_ENABLED=False)
    def test_versioned_source_and_ci_payload_failures_keep_exact_v0_retry(self):
        from .website_failure_contract import classify_failure
        for cause, old_code in (("github_source_payload_invalid", "github_source_unavailable"),
                                ("github_ci_payload_invalid", "github_ci_evidence_unavailable")):
            failure = classify_failure(cause, "source_read")
            error = WebsiteAuthorityError("worker_unavailable", "Historical")
            error.result = {"failure": failure, "failure_class": failure["failure_class"], "retryable": False}
            op = operation_fixtures.PrepareJourneyTests().operation()
            prepare.prepare_failure(op, "verify", error)
            self.assertEqual(op.state, "pending")
            self.assertEqual(op.receipt["reason_code"], old_code)
            self.assertEqual(op.receipt["failure_class"], "transient")
            self.assertTrue(op.receipt["retryable"])
            # Preserve received evidence while applying its reviewed v0 policy.
            self.assertEqual(op.receipt["failure"], failure)
            self.assertFalse(op.receipt["failure_taxonomy_policy_applied"])

    @override_settings(ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=False, ARTICLES_FAILURE_TAXONOMY_OBSERVE_ENABLED=True)
    def test_source_and_ci_shadow_keep_their_original_local_legacy_view(self):
        from .website_github_failure import github_failure
        for code in ("github_source_unavailable", "github_ci_evidence_unavailable"):
            error = github_failure(ValueError("Malformed JSON"), legacy_code=code, legacy_message="Legacy", legacy_retryable=True, source=True)
            self.assertEqual(error.as_dict()["code"], code)
            self.assertTrue(error.as_dict()["retryable"])
            self.assertNotIn("failure", error.as_dict())
            op = operation_fixtures.PrepareJourneyTests().operation()
            prepare.prepare_failure(op, "verify", error)
            self.assertEqual(op.state, "pending")
            self.assertEqual(op.receipt["reason_code"], code)
            self.assertEqual(op.receipt["failure_class"], "transient")
            self.assertEqual(op.receipt["failure_taxonomy_observation"]["classification"], "unsupported")


class ScannerTests(SimpleTestCase):
    def test_real_production_inventory_is_registered(self):
        from scripts.check_failure_contract import check_emitters
        errors, rows = check_emitters()
        self.assertEqual(errors, [])
        self.assertGreater(len(rows), 500)

    def test_dynamic_audits_are_exact_and_bounded(self):
        import json
        from pathlib import Path
        from scripts.check_failure_contract import ROOT, inventory, site_key
        manifest = json.loads((ROOT / "contracts/failure-emitter-scopes.json").read_text())
        rows = {site_key(row): row for row in inventory()}
        for audit in manifest["dynamic_adapters"] + manifest["nonfailure_sites"]:
            self.assertIn(audit["site_sha256"], rows)
            self.assertTrue(audit["source"])
            self.assertTrue(audit["owner"])
            self.assertEqual(audit["allowed_codes_ref"], "contracts/failure-codes.json#/codes")
            self.assertNotIn("*", audit["path"] + audit["function"] + audit["source"])
            for field in ("path", "function", "field", "expression"):
                self.assertEqual(audit[field], rows[audit["site_sha256"]][field])

    def test_ast_covers_constructor_mutations_constants_enums_and_finite_strings(self):
        from scripts.check_failure_contract import python_emissions
        source = '''
BASE = "SLOT_"
class Kind:
    VALUE = "TEMPLATE_VALIDATION_FAILED"
def emit():
    code = "DIRECTORY_STYLE_REVIEW_FAILED"
    WebsiteAuthorityError(code, "reason")
    value = {"error_code": Kind.VALUE}
    value["blocking_code"] = f"{BASE}MISSING"
    value.update(reasonCode="worker_unavailable")
    value.setdefault("errorCode", "ARTICLE_DIRECTORY_AGENT_BUDGET")
'''
        rows = python_emissions(source, "content_factory/fixture.py")
        literals = {code for row in rows for code in row["literals"] or []}
        self.assertEqual(literals, {"DIRECTORY_STYLE_REVIEW_FAILED", "TEMPLATE_VALIDATION_FAILED", "SLOT_MISSING", "worker_unavailable", "ARTICLE_DIRECTORY_AGENT_BUDGET"})

    def fixture_root(self):
        import hashlib, json, tempfile
        from pathlib import Path
        from scripts.check_failure_contract import ARTIFACTS, ROOT, SOURCE_REPOSITORY
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "contracts").mkdir()
        (root / "content_factory").mkdir()
        files = {}
        for name in ARTIFACTS:
            raw = (ROOT / name).read_bytes()
            (root / name).write_bytes(raw)
            files[name] = hashlib.sha256(raw).hexdigest()
        lock = {"schema_version": 1, "source_repository": SOURCE_REPOSITORY, "source_revision": "a" * 40, "files": files}
        (root / "contracts/failure-codes.lock.json").write_text(json.dumps(lock))
        (root / "contracts/failure-emitter-scopes.json").write_text(json.dumps({"dynamic_adapters": [], "nonfailure_sites": []}))
        return root, lock

    def test_unknown_literal_and_unbounded_dynamic_fail_with_safe_locations(self):
        from scripts.check_failure_contract import check_emitters
        root, _ = self.fixture_root()
        (root / "content_factory/example.py").write_text('def emit(foreign):\n    WebsiteAuthorityError("ghp_private_literal", "bad")\n    return {"error_code": foreign.code}\n')
        errors, _ = check_emitters(root)
        self.assertEqual(len(errors), 2)
        self.assertTrue(all("example.py:" in error for error in errors))
        self.assertNotIn("ghp_private_literal", str(errors))

    def test_one_byte_digest_mutable_pin_and_missing_remote_access_fail(self):
        import json
        from scripts.check_failure_contract import verify_vendor, ContractCheckError, ARTIFACTS
        root, lock = self.fixture_root()
        verify_vendor(root)
        with self.assertRaisesRegex(ContractCheckError, "READ_TOKEN_required"):
            verify_vendor(root, remote=True)
        artifact = root / ARTIFACTS[0]
        original = artifact.read_bytes()
        artifact.write_bytes(original + b" ")
        with self.assertRaisesRegex(ContractCheckError, "vendor_digest_mismatch"):
            verify_vendor(root)
        artifact.write_bytes(original)
        lock["source_revision"] = "main"
        (root / "contracts/failure-codes.lock.json").write_text(json.dumps(lock))
        with self.assertRaisesRegex(ContractCheckError, "immutable_commit"):
            verify_vendor(root)

    def test_remote_parity_uses_exact_revision_and_refuses_changed_bytes(self):
        from scripts.check_failure_contract import verify_vendor, ContractCheckError, ARTIFACTS
        root, lock = self.fixture_root()
        seen = []
        def fetch(path, revision, token):
            seen.append((path, revision))
            self.assertEqual(token, "synthetic-read-only")
            return (root / path).read_bytes()
        verify_vendor(root, remote=True, token="synthetic-read-only", fetch=fetch)
        self.assertEqual(seen, [(path, lock["source_revision"]) for path in ARTIFACTS])
        with self.assertRaisesRegex(ContractCheckError, "source_byte_mismatch"):
            verify_vendor(root, remote=True, token="synthetic-read-only", fetch=lambda *args: b"changed")

@override_settings(ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=("7",),
                   CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED=True, ROO_API_KEY="fixture-service-key")
class FailureDeliveryChainTests(SimpleTestCase):
    def replay(self, code, expected):
        """Actual HTTP receiver → saved child → actual preparation and B1 policy."""
        from . import tests_setup_delivery_unit as delivery_fixtures
        from . import tests_prepare_permissions_unit as preparation_fixtures
        from .website_failure_contract import classify_failure, attach_failure_projection
        from workflow_runs.models import ContentFactoryRun
        from founder_tools import services as founder_services
        from founder_tools.models import VibeRaisingProfile
        from .models import OrganizationContentConfig
        from . import vibe_marketing_views as views
        from django.db.transaction import Atomic
        from uuid import UUID
        fixture = delivery_fixtures.SetupDeliveryChainTests("test_actual_callback_commits_projection_and_exact_ack_then_replay_is_idempotent")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        failure = classify_failure(code, "review_directory_visual_style", [{"slot": "listing", "required_count": 3}])
        occurrence = "33333333-3333-4333-8333-333333333333"
        wire = delivery_fixtures.envelope(status="failed", result=attach_failure_projection(
            {"article_system_setup": {"status": "preview_failed"}}, failure, occurrence))
        wire["event_type"] = "article_system_setup_failed"
        projection = wire["body"]["state_projection"]
        projection.update(failure=deepcopy(failure), failure_occurrence_id=occurrence, resume_available=failure["retryable"])
        delivery_fixtures.signed(wire)
        response = fixture.post(wire)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(fixture.run.status, "failed")
        self.assertEqual(fixture.run.result["failure"], failure)
        self.assertEqual(fixture.operation.state, "failed")
        # Do not leave receiver ORM replacements over preparation's independent
        # query fixture; committed in-memory rows themselves survive this boundary.
        fixture.stack.close()
        runtime = preparation_fixtures.PrepareVerificationRuntimeTests()
        runtime.setUp()
        scope = wire["scope"]
        runtime.website.pk = UUID(scope["website_connection_id"])
        runtime.website.generation = runtime.prepare.generation = scope["connection_generation"]
        runtime.website.repository_id = scope["repository_id"]
        runtime.prepare.connection = runtime.website
        runtime.config.website_connection = runtime.website
        runtime.prepare.receipt = {"child_run_id": fixture.run.run_id, "current_step": "setup"}
        company = SimpleNamespace(pk=9, id=9, domain=runtime.org.domain, organization_id=runtime.org.pk,
                                  organization=runtime.org)
        companies = runtime.rows()
        companies.select_related.return_value = companies
        companies.get.return_value = company
        profile = SimpleNamespace(user=runtime.user, companies=companies, role=VibeRaisingProfile.ROLE_FOUNDER)
        real_config = views._get_config
        with runtime.boundaries(), \
                patch.object(Atomic, "__enter__", return_value=None), \
                patch.object(Atomic, "__exit__", return_value=False), \
                patch.object(views, "get_founder_company_context", founder_services.get_founder_company_context), \
                patch.object(views, "_get_config", real_config), \
                patch.object(VibeRaisingProfile.objects, "get_or_create", return_value=(profile, False)), \
                patch.object(OrganizationContentConfig.objects, "get_or_create", return_value=(runtime.config, False)), \
                patch.object(ContentFactoryRun.objects, "filter", return_value=runtime.rows(first=fixture.run)):
            result = prepare.advance_prepare(runtime.prepare.pk, now=runtime.now)
        self.assertIs(result, runtime.prepare)
        self.assertEqual(result.state, expected)
        self.assertEqual(result.receipt["failure"], failure)
        self.assertEqual(result.receipt["failure_occurrence_id"], occurrence)
        self.assertEqual(result.receipt["child_run_id"], fixture.run.run_id)
        self.assertEqual(result.receipt.get("child_resume_pending", False), failure["retryable"])
        self.assertEqual(result.receipt["retryable"], failure["retryable"])
        self.assertEqual(fixture.run.result["failure_occurrence_id"], occurrence)
        fixture.legacy.assert_not_called()
        fixture.snapshot.assert_not_called()
        return fixture, wire, result

    def test_real_style_and_budget_chain_retains_causal_object_without_full_dispatch(self):
        for code in ("DIRECTORY_STYLE_REVIEW_FAILED", "ARTICLE_DIRECTORY_AGENT_BUDGET"):
            with self.subTest(code=code):
                self.replay(code, "needs_attention")

    def test_real_transient_chain_retains_the_original_child_for_same_step_resume(self):
        self.replay("worker_unavailable", "pending")
