"""Actual promotion and admission paths with only persistence/provider boundaries replaced."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from . import website_sealed_workflow as sealed, website_verification as verification, website_goal as goals
from .website_connections import contract_for
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnection
from .website_setup_consent import seal
from . import tests_prepare_permissions_unit as permission_fixtures


@override_settings(ARTICLES_PUBLISHING_INPUTS_V3_ENABLED=True, ARTICLES_HOSTED_ATTESTATION_ENABLED=True,
    ARTICLES_HOSTED_VERIFICATION_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"], ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
    ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class SealedWorkflowTests(SimpleTestCase):
    def setUp(self):
        bundle = json.loads((Path(__file__).parent / "testdata/sealed-verification-v3/static.json").read_text())
        self.proof, self.candidate = bundle["proof"], bundle["candidate"]
        self.value = value = permission_fixtures.PrepareVerificationRuntimeTests(); value.setUp()
        value.website.pk = self.proof["website_connection_id"]
        value.website.repository_id = self.proof["repository_id"]
        value.website.verified_sha = self.proof["source_sha"]
        value.website.refresh_from_db = Mock()
        value.inventory.source_sha = value.website.verified_sha
        value.config.website_connection = value.website
        value.config.refresh_from_db = Mock()
        value.config.default_publish_target_id = self.proof["target_id"]
        value.target.connection = value.website
        value.target.target_key = self.proof["target_id"]
        value.target.source_sha = self.proof["source_sha"]
        value.target.adapter = self.proof["adapter_id"]
        value.target.contract = {**deepcopy(self.candidate["payload"]["publishing"]["target"]),
            "contract_digest": self.proof["contract_digest"], "contract": {"contract_digest": self.proof["contract_digest"]}}
        value.prepare.connection = value.website
        value.prepare.refresh_from_db = Mock()
        goal = goals.new_goal(str(value.prepare.pk), goals.scope_for(value.website, "9"), now=value.now)
        goal["consent"] = seal(goal, {"version": 1, "publish_mode": "auto", "page_policy": "auto",
            "page_mode": "existing", "page_route": "/stories", "ci_mode": "hosted",
            "billing": {"policy": "reserved", "expected_points": 6, "accepted": True}}, user_id=4, now=value.now)
        value.prepare.receipt.update(goal=goal, setup_run_id="setup-parent")
        request = {**contract_for(value.website), "expected_source_sha": self.proof["baseline_source_sha"], "deletion_epoch": 0}
        self.source_run = SimpleNamespace(run_id="setup-parent", workflow="article_system_setup", organization_id=7,
            github_repo=value.website.github_repo, domain=value.org.domain, run_request=request, refresh_from_db=Mock())
        value.origin.pk = self.proof["operation_id"]
        value.origin.connection = value.website
        value.origin.refresh_from_db = Mock()
        value.origin.save = Mock()
        self.binding = {**contract_for(value.website), **{key: self.proof[key] for key in (
            "operation_id", "operation_attempt", "deletion_epoch", "target_id", "contract_digest",
            "publishing_inputs_digest", "baseline_source_sha", "verifier_sha256")},
            "run_id": str(value.origin.pk), "source_run_id": "setup-parent", "expected_source_sha": self.proof["source_sha"]}
        value.origin.payload.update(workflow="hosted_native_verification", run_id=self.binding["run_id"], binding=self.binding)
        self.child_run = SimpleNamespace(run_id=self.binding["run_id"], workflow="hosted_native_verification",
            organization_id=7, status="completed", run_request=self.binding)
        self.data = {**self.proof, "run_id": self.binding["run_id"], "source_run_id": "setup-parent"}

    @contextmanager
    def boundaries(self):
        from workflow_runs.models import ContentFactoryRun
        value = self.value
        with value.boundaries() as stack:
            operations = value.rows()
            def rows(**query):
                if "pk" in query:
                    result = next((op for op in (value.origin, value.prepare) if str(op.pk) == str(query["pk"])), None)
                else:
                    result = {"prepare": value.prepare, "ci-verify": value.ci, "deployment-verify": value.live}.get(query.get("action"))
                return value.rows(first=result)
            stack.enter_context(patch.object(WebsiteConnection, "operations", operations))
            operations.filter.side_effect = rows
            stack.enter_context(patch.object(WebsiteConnection.objects, "get", return_value=value.website))
            stack.enter_context(patch.object(ContentFactoryRun.objects, "filter", side_effect=lambda **query:
                value.rows(first=self.source_run if query.get("run_id") == "setup-parent" else self.child_run)))
            yield

    def pending(self):
        value = self.value
        snapshot = sealed.parent_snapshot(value.prepare, value.website, value.config, value.target, self.source_run)
        snapshot.update(parent_binding=sealed.parent_binding(value.prepare, value.website, self.proof["source_sha"]),
            accepted_target=deepcopy(value.target.contract))
        result = {"candidate": deepcopy(self.candidate), "snapshot": snapshot}
        value.origin.payload["sealed_inputs"] = result
        return result

    def promote(self, *, remote=None):
        with patch("content_factory.website_sealed_candidate.authenticated_request", side_effect=remote,
                return_value={"status": "completed", "run_id": self.binding["run_id"], "verification": self.proof}):
            return verification.record_ci_attestation(self.data, origin="authenticated_hosted")

    def test_actual_candidate_read_retains_requirement_when_parent_is_cancelled_during_fetch(self):
        from .website_sealed_candidate import SCOPE_FIELDS
        value = self.value
        before = deepcopy(value.target.contract)
        def fetched(request):
            value.prepare.state = "cancelled"
            return {"schema_version": 1, "source_run_id": request["source_run_id"],
                "scope": {key: request[key] for key in SCOPE_FIELDS}, "publishing_inputs_v1": self.candidate}
        with self.boundaries(), patch("content_factory.website_sealed_candidate.fetch_candidate", side_effect=fetched):
            with self.assertRaises(WebsiteAuthorityError):
                sealed.prepare_candidate(value.prepare, value.context, value.config, value.target, self.proof["source_sha"])
        self.assertEqual(value.target.contract, before)
        self.assertEqual(value.target.capabilities[sealed.REQUIRED_VERSION], 3)
        self.assertFalse(value.target.capabilities["publishingReady"])

    def test_lost_dispatch_response_reuses_original_child_and_budget(self):
        from workflow_runs.models import ContentFactoryRun
        from .website_hosted_verification import request_for
        value = self.value
        with self.boundaries():
            pending = self.pending()
            existing = []
            operation_rows = value.website.operations.filter.side_effect
            value.website.operations.filter.side_effect = lambda **query: (
                value.rows(first=existing[0] if existing else None) if "idempotency_key" in query else operation_rows(**query))
            def reserve(connection, *, workflow, payload):
                value.origin.state = "running"
                payload.update(operation_id=str(value.origin.pk), operation_attempt=1, deletion_epoch=0)
                existing.append(value.origin)
                return value.origin
            with patch.object(sealed, "prepare_candidate", return_value=(self.candidate, pending["snapshot"])), \
                    patch("content_factory.website_goal_budget.enabled", return_value=True), \
                    patch("content_factory.website_operations.reserve_workflow_operation", side_effect=reserve) as reservation, \
                    patch.object(ContentFactoryRun.objects, "get_or_create", return_value=(self.child_run, True)):
                first, request = request_for(value.prepare, value.context, value.config, value.target, self.proof["source_sha"])
                value.prepare.receipt["goal"]["budget_used"].update(active_seconds=100, model_cost=0.5)
                replay, repeated = request_for(value.prepare, value.context, value.config, value.target, self.proof["source_sha"])
            self.assertEqual(replay.pk, first.pk)
            self.assertEqual(repeated, request)
            self.assertNotIn("baseline_source_sha", request)
            self.assertNotIn("verifier_sha256", request)
            reservation.assert_called_once()

    def test_actual_hosted_promotion_saved_reader_and_article_admission(self):
        from .activation import durable_integration_evidence
        value = self.value
        with self.boundaries():
            self.pending(); sealed.mark_required(value.target)
            self.assertFalse(durable_integration_evidence(value.config, value.website)["verified"])
            result = self.promote()
            self.assertEqual(result.receipt["schema_version"], 3)
            self.assertEqual(value.target.contract["contract"]["publishing_inputs_v1"], self.candidate)
            self.assertEqual(verification.current_ci_attestation(value.website, value.target), result.receipt)
            self.assertTrue(durable_integration_evidence(value.config, value.website)["verified"])
            self.assertFalse(durable_integration_evidence(value.config, value.website)["publishingVerified"])

    def test_pending_requirement_survives_flag_disable_and_identical_scan_echo(self):
        from .incident_guards import target_update_allowed, protect_certified_config
        value = self.value
        with self.boundaries():
            before = deepcopy(value.target.contract)
            sealed.mark_required(value.target)
            with override_settings(ARTICLES_PUBLISHING_INPUTS_V3_ENABLED=False):
                self.assertTrue(sealed.required(value.website, value.target))
                self.assertIsNone(verification.current_ci_attestation(value.website, value.target))
            for source in (value.target.source_sha, "f" * 40):
                self.assertFalse(target_update_allowed(value.target, before, generation=3, sha=source))
            value.config.publish_targets = [before]
            with patch.object(WebsiteConnection, "targets", value.rows(first=value.target, items=[value.target])):
                result = protect_certified_config(value.config, {"publish_targets": [], "default_publish_target_id": "other"})
            self.assertEqual(result["publish_targets"], [before])
            self.assertEqual(result["default_publish_target_id"], value.target.target_key)

    def test_final_provider_read_cannot_promote_after_parent_cancel_or_target_change(self):
        for change in ("cancel", "attempt", "consent", "target", "source_request", "selection"):
            self.setUp(); value = self.value
            def changed(*args, **kwargs):
                if change == "cancel": value.prepare.state = "cancelled"
                elif change == "attempt": value.prepare.payload["attempt"] += 1
                elif change == "consent": value.prepare.receipt["goal"]["consent"]["answers"]["publish_mode"] = "review"
                elif change == "target": value.target.contract["route_path"] = "/changed"
                elif change == "source_request": self.source_run.run_request["expected_source_sha"] = "f" * 40
                else: value.config.default_publish_target_id = "another"
                return {"status": "completed", "run_id": self.binding["run_id"], "verification": self.proof}
            with self.subTest(change=change), self.boundaries():
                self.pending()
                with self.assertRaises(WebsiteAuthorityError): self.promote(remote=changed)
                value.target.save.assert_not_called()

    def test_invalid_saved_scope_or_native_origin_cannot_promote(self):
        with self.boundaries():
            self.pending()
            with self.assertRaises(WebsiteAuthorityError): verification.record_ci_attestation(self.data)
            self.value.origin.payload["binding"]["baseline_source_sha"] = "f" * 40
            with self.assertRaises(WebsiteAuthorityError): self.promote()
            self.value.target.save.assert_not_called()

    def test_legacy_proof_cannot_promote_if_protocol_enrolls_during_provider_read(self):
        from .tests_website_hosted_verification_unit import HostedPromotionTests
        from . import website_hosted_verification as hosted
        value = HostedPromotionTests().fixture()
        def fetched(*args, **kwargs):
            value.target.capabilities[sealed.REQUIRED_VERSION] = 3
            return {"status": "completed", "run_id": "native-run", "verification": value.sealed}
        with override_settings(ARTICLES_PUBLISHING_INPUTS_V3_ENABLED=False), value.boundaries(), \
                patch.object(WebsiteConnection.objects, "get", return_value=value.website), \
                patch.object(hosted, "remote_request", side_effect=fetched), \
                patch("content_factory.website_operations.validate_operation", return_value=value.origin):
            with self.assertRaises(WebsiteAuthorityError):
                verification.record_ci_attestation({**value.sealed, "run_id": "native-run", "source_run_id": "setup-parent"}, origin="authenticated_hosted")
        value.target.save.assert_not_called()

    def test_old_live_receipt_cannot_satisfy_new_execution_proof(self):
        from .activation import live_deployment_verified
        value = self.value
        with self.boundaries():
            self.pending(); result = self.promote()
            value.live = SimpleNamespace(receipt={"status": "passed", "source_sha": value.website.verified_sha,
                "connection_generation": 3, "target_id": value.target.target_key,
                "contract_digest": self.proof["contract_digest"], "artifact_digest": self.proof["artifact_digest"],
                "public_url": "https://fixture.test/stories", "checked_at": value.now.isoformat(), "ci_evidence_digest": "e" * 64})
            self.assertFalse(live_deployment_verified(value.website, value.target))
            value.live.receipt["ci_evidence_digest"] = result.receipt["evidence_digest"]
            self.assertTrue(live_deployment_verified(value.website, value.target))

    def test_lost_local_commit_response_replays_without_renewal_or_new_work(self):
        from .website_hosted_verification import advance
        value = self.value
        with self.boundaries():
            self.pending(); original = self.promote()
            before = deepcopy((original.receipt, value.target.contract))
            value.target.save.reset_mock()
            with patch("content_factory.website_sealed_candidate.authenticated_request") as remote:
                replay = verification.record_ci_attestation(self.data, origin="authenticated_hosted")
                result = advance(value.prepare, value.context, value.config, value.target, self.proof["source_sha"])
            self.assertEqual(replay.pk, original.pk)
            self.assertEqual(result, {"status": "completed", "operation_id": str(original.pk)})
            self.assertEqual((replay.receipt, value.target.contract), before)
            remote.assert_not_called(); value.target.save.assert_not_called()

    def test_pending_protocol_blocks_actual_article_charge_before_spending(self):
        from integrations.services import article_generation as generation
        from . import vibe_marketing_views as views
        from roo.services import PointsService
        value = self.value
        account = {"saved": True, "owned": True, "verified": True}
        access = {"verified": True, "writable": True, "branch": value.website.branch, "sha": value.website.verified_sha}
        with self.boundaries(), patch.object(generation, "_validate_authenticated_content_factory_actor"), \
                patch("content_factory.activation.founder_context_for_domain", return_value=value.context), \
                patch.object(views, "_latest_runs_for_org", return_value=[]), \
                patch.object(views, "_github_account_for_context", return_value=account), \
                patch.object(views, "_verify_github_repository_access", return_value=access), \
                patch.object(PointsService, "spend") as spend:
            value.target.capabilities[sealed.REQUIRED_VERSION] = 3
            with override_settings(ARTICLES_PUBLISHING_INPUTS_V3_ENABLED=False):
                with self.assertRaises(generation.ArticleGenerationError):
                    generation._charge_content_factory_user(user=value.user, created_by_slack_id="4",
                        article_request={"client_request_id": "read-only-test"}, resolved_domain=value.org.domain)
            spend.assert_not_called()
