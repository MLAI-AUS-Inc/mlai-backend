"""A first-time articles setup with only inventory after a verified squash merge."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase, override_settings

from workflow_runs.models import ContentFactoryRun
from . import website_setup_merge_proof as carry
from .activation import durable_integration_evidence
from .founder_journey_testing import ALL_P1_FLAGS_ON, make_new_journey_org
from .tests_live_url_verification import BASE, MERGED, CONTRACT_DIGEST, DIGEST, LISTING, _connect
from .tests_setup_pr_changes_unit import static_setup_run
from .vibe_marketing_views import _record_setup_verification_refusal
from .website_connections import contract_for, summary_for
from .website_contract import WebsiteAuthorityError, evidence_digest
from .website_live_verification import build_proof
from .website_operations import deletion_epoch
from .website_prepare import _dispatch_prepare_step

HEAD, TREE = "c" * 40, "d" * 40


class SetupMergeFixture:
    """A new-journey site whose setup pull request merged as a squash of the reviewed head."""

    def setUp(self):
        super().setUp()
        built = make_new_journey_org(domain="squash.example.test")
        self.website = _connect(built, verified_sha="")
        self.config = built.config
        self.website.blockers = [{"code": "website_data_deleted", "deletion_epoch": 12}]
        self.website.save(update_fields=["blockers"])
        self.config.refresh_from_db()
        self.org = built.organization
        self.target = self.website.targets.create(generation=self.website.generation, target_key="articles",
            source_sha=BASE, adapter="registry_entry", contract={"target_id": "articles"})
        for index, (sha, detector) in enumerate(((BASE, "legacy"), (BASE, "github_head"),
                                                 (MERGED, "legacy"), (MERGED, "github_head"))):
            self.website.scan_snapshots.create(generation=self.website.generation, run_id=f"scan-{index}",
                source_sha=sha, detector_version=detector, fingerprint=str(index), evidence={})
        result = static_setup_run(head=HEAD).result
        result.update(status="merged", merge_status="merged", merge_sha=MERGED, route_path="/articles",
            default_publish_target_id="articles", publish_targets=[{"target_id": "articles", "route_path": "/articles",
                "publish_capability": "direct", "delivery_adapter": "registry_entry", "provisional": True,
                "live_marker": {"kind": "artifact_digest", "value": DIGEST}, "contract_digest": CONTRACT_DIGEST}],
            merge_response={"sha": MERGED, "pull": {"merge_commit_sha": MERGED, "head_sha": HEAD}})
        result["article_system_setup"].update(status="merged", merge_status="merged")
        result["directory_quality_gates"].update(local_build_skipped=False)
        self.run = ContentFactoryRun.objects.create(run_id="setup-1", organization=self.org, domain=self.org.domain,
            github_repo=self.website.github_repo, workflow="article_system_setup", status="completed", approval_state="approved",
            run_request={**contract_for(self.website), "deletion_epoch": 12}, result=result)
        self.op = self.website.operations.create(generation=self.website.generation, action="prepare", state="running",
            idempotency_key="prepare", receipt={"merged_setup_run_id": self.run.run_id}, payload={"attempt": 2})
        self.trees = self.enterContext(patch.object(carry, "commit_tree_sha", return_value=TREE))
        self.head = self.enterContext(patch("content_factory.website_connections.verify_repository_head", return_value=MERGED))
        self.fetch = self.enterContext(patch("content_factory.website_live_fetch.fetch_live_route", return_value=(LISTING, {})))

    def dispatch(self, step="ci-attestation"):
        return _dispatch_prepare_step(self.op, SimpleNamespace(organization=self.org), self.config, step, user=None)

    def assert_withheld(self):
        self.website.refresh_from_db()
        self.target.refresh_from_db()
        self.assertFalse(build_proof(self.target))
        self.assertFalse(self.website.verified_sha)
        self.assertFalse(self.website.capabilities.get("publishingReady"))
        self.fetch.assert_not_called()


@override_settings(**ALL_P1_FLAGS_ON)
class SetupMergeProofTests(SetupMergeFixture, TestCase):
    def test_identical_tree_then_live_marker_activates_without_a_full_rescan(self):
        self.assertEqual(self.dispatch()["status"], "completed")
        self.assertEqual([call.args[1] for call in self.trees.call_args_list], [HEAD, MERGED])
        self.website.refresh_from_db()
        self.config.refresh_from_db()
        self.target.refresh_from_db()
        self.assertEqual(self.website.verified_sha, MERGED)
        self.assertEqual(self.target.contract["verification"], {"status": "passed", "source_sha": MERGED,
            "attestation_origin": "setup_merge_carry", "carried_from": HEAD, "setup_run_id": "setup-1",
            "source_tree_sha": TREE})
        proof = carry.worker_publish_targets(self.config)[0]["verification"]
        self.assertEqual(proof["website_connection_id"], str(self.website.pk))
        self.assertEqual(proof["operation_id"], str(self.website.operations.get(action="deployment-verify").pk))
        self.assertEqual(proof["deletion_epoch"], 12)
        self.assertTrue(self.website.capabilities["generationReady"])
        self.assertTrue(self.website.capabilities["publishingReady"])
        self.assertTrue(durable_integration_evidence(self.config, self.website)["liveVerified"])
        self.assertEqual(summary_for(self.config)["blockers"], [])
        self.assertEqual(deletion_epoch(self.website), 12)
        self.trees.reset_mock()
        self.assertEqual(self.dispatch(step="verify")["status"], "completed")
        self.trees.assert_not_called()
        self.assertEqual(self.website.operations.filter(action="deployment-verify").count(), 1)

    def test_missing_scan_target_can_be_created_from_the_reviewed_default(self):
        self.target.delete()
        self.config.default_publish_target_id = None
        self.config.save(update_fields=["default_publish_target_id"])
        self.dispatch()
        self.config.refresh_from_db()
        self.assertEqual(self.config.default_publish_target_id, "articles")
        self.assertEqual(self.config.publish_targets[0]["route_path"], "/articles")

    def test_a_recorded_merge_sha_without_the_optional_provider_payload_still_verifies(self):
        del self.run.result["merge_response"]
        self.run.save(update_fields=["result"])
        self.assertEqual(self.dispatch()["status"], "completed")

    def test_an_unlinked_preparation_adopts_the_setup_that_merged_the_observed_head(self):
        """TalaThrive, 27 Nov: the push arrived before the run recorded its merge."""
        self.op.receipt = {"setup_run_id": "older-failed-setup"}
        self.op.save(update_fields=["receipt"])
        self.assertEqual(self.dispatch()["status"], "completed")
        self.op.refresh_from_db()
        self.website.refresh_from_db()
        self.assertEqual(self.op.receipt["merged_setup_run_id"], "setup-1")
        self.assertEqual(self.op.receipt["setup_run_id"], "older-failed-setup")
        self.assertEqual(self.website.verified_sha, MERGED)

    def test_an_unlinked_preparation_never_adopts_a_setup_that_merged_another_commit(self):
        self.op.receipt = {}
        self.op.save(update_fields=["receipt"])
        self.run.result["merge_sha"] = "e" * 40
        self.run.save(update_fields=["result"])
        with self.assertRaises(WebsiteAuthorityError):
            self.dispatch()
        self.op.refresh_from_db()
        self.assertNotIn("merged_setup_run_id", self.op.receipt)
        self.assert_withheld()

    def test_an_unlinked_preparation_never_adopts_an_unapproved_setup(self):
        self.op.receipt = {}
        self.op.save(update_fields=["receipt"])
        self.run.approval_state = "pending"
        self.run.save(update_fields=["approval_state"])
        with self.assertRaises(WebsiteAuthorityError):
            self.dispatch()
        self.op.refresh_from_db()
        self.assertNotIn("merged_setup_run_id", self.op.receipt)
        self.assert_withheld()

    def test_a_contradictory_merge_receipt_or_sealed_setup_contract_is_refused(self):
        receipt = self.run.result["merge_response"]
        receipt["pull"]["head_sha"] = BASE
        self.run.save(update_fields=["result"])
        with self.assertRaises(WebsiteAuthorityError):
            self.dispatch()
        self.assert_withheld()
        receipt["pull"]["head_sha"] = HEAD
        self.run.result["publish_targets"][0]["contract"] = {"publishing_inputs_v1": {}}
        self.run.save(update_fields=["result"])
        with self.assertRaises(WebsiteAuthorityError):
            self.dispatch()
        self.assert_withheld()
        self.trees.assert_not_called()

    def test_selection_changed_during_live_fetch_leaves_connection_unverified(self):
        def select_other(*args, **kwargs):
            self.config.default_publish_target_id = "other-target"
            self.config.save(update_fields=["default_publish_target_id"])
            return LISTING, {}
        self.fetch.side_effect = select_other
        with self.assertRaisesMessage(WebsiteAuthorityError, "selected target changed"):
            self.dispatch()
        self.website.refresh_from_db()
        self.target.refresh_from_db()
        self.assertEqual(self.website.verified_sha, "")
        self.assertNotIn("live_verification", self.target.contract)
        self.assertFalse(self.website.operations.filter(action="deployment-verify").exists())

    def test_build_proof_waits_for_the_right_live_marker(self):
        self.fetch.return_value = (b"<html>previous deploy</html>", {})
        with self.assertRaisesMessage(WebsiteAuthorityError, "has not deployed"):
            self.dispatch()
        self.target.refresh_from_db()
        self.website.refresh_from_db()
        self.assertTrue(build_proof(self.target))
        self.assertFalse(self.website.verified_sha)
        self.assertFalse(self.website.capabilities.get("generationReady"))
        self.assertFalse(self.website.operations.filter(action="deployment-verify").exists())
        self.assertEqual(summary_for(self.config)["blockers"][0]["code"], "website_data_deleted")
        self.trees.reset_mock()
        self.fetch.return_value = (LISTING, {})
        self.dispatch()
        self.trees.assert_not_called()

    def test_changed_tree_or_unavailable_comparison_never_carries(self):
        for comparison in ([TREE, "e" * 40], OSError("GitHub unavailable")):
            with self.subTest(comparison=comparison), self.assertRaises(WebsiteAuthorityError):
                self.trees.side_effect = comparison
                self.dispatch()
            self.assert_withheld()

    def test_moved_branch_or_moved_observation_never_carries(self):
        self.head.side_effect = WebsiteAuthorityError("website_source_changed", "Branch moved.")
        with self.assertRaisesMessage(WebsiteAuthorityError, "Branch moved"):
            self.dispatch()
        self.assert_withheld()
        self.trees.reset_mock()
        self.website.scan_snapshots.create(generation=self.website.generation, run_id="new-push",
            source_sha="e" * 40, detector_version="github_head", fingerprint="new")
        with self.assertRaises(WebsiteAuthorityError):
            self.dispatch()
        self.assert_withheld()
        self.trees.assert_not_called()

    def test_sealed_hosted_and_disabled_paths_keep_their_verification(self):
        for patcher in (patch.object(carry, "hosted", return_value=True),
                        override_settings(ARTICLES_PROOF_CARRY_FORWARD_ENABLED=False)):
            with patcher, self.assertRaises(WebsiteAuthorityError):
                self.dispatch()
            self.assert_withheld()
        self.target.capabilities = {"requiredProofVersion": 3}
        self.target.save(update_fields=["capabilities"])
        self.assertEqual(carry.carry_setup_merge_proof(self.op, self.config, self.target), self.target)
        self.assert_withheld()
        self.trees.assert_not_called()

    def test_rejects_an_unverified_render_or_a_proof_for_another_attempt(self):
        proof = self.run.result["directory_quality_verification"]
        for changes in ({"status": "code_review_ready"}, {"commit_sha": BASE}, {"resume_generation": 10}):
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError):
                self.run.result["directory_quality_verification"] = {**proof, **changes}
                self.run.save(update_fields=["result"])
                self.dispatch()
            self.assert_withheld()
        self.trees.assert_not_called()

    def test_proof_cannot_cross_source_or_target_or_generation(self):
        self.website.scan_snapshots.filter(source_sha=BASE).delete()
        with self.assertRaises(WebsiteAuthorityError):
            self.dispatch()
        self.assert_withheld()
        self.run.run_request["expected_source_sha"] = BASE
        self.run.save(update_fields=["run_request"])
        self.config.default_publish_target_id = "other-target"
        self.config.save(update_fields=["default_publish_target_id"])
        with self.assertRaises(WebsiteAuthorityError):
            self.dispatch()
        self.config.default_publish_target_id = "articles"
        self.config.save(update_fields=["default_publish_target_id"])
        self.run.run_request["connection_generation"] += 1
        self.run.save(update_fields=["run_request"])
        with self.assertRaises(WebsiteAuthorityError):
            self.dispatch()
        self.assert_withheld()
        self.trees.assert_not_called()

    def test_cancelled_prepare_cannot_commit_after_reading_github(self):
        def cancel(website, sha):
            self.website.operations.filter(pk=self.op.pk).update(state="cancelled")
            return TREE
        self.trees.side_effect = cancel
        with self.assertRaises(WebsiteAuthorityError):
            self.dispatch()
        self.assert_withheld()

    def test_refusal_is_saved_on_the_run_without_overwriting_a_later_merge(self):
        self.run.result.update(status="pr_created", merge_status="not_merged")
        self.run.save(update_fields=["result"])
        exc = WebsiteAuthorityError("setup_verification_required", "Verify the updated setup preview.")
        _record_setup_verification_refusal(self.run, HEAD, exc)
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["merge_blocked_reason"], str(exc))
        self.assertEqual(self.run.result["setup_merge_refusal"]["code"], exc.code)
        self.assertEqual(self.run.result["status"], "pr_created")
        merged = {**self.run.result, "status": "merged", "merge_status": "merged"}
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(result=merged)
        _record_setup_verification_refusal(self.run, BASE, exc)
        self.run.refresh_from_db()
        self.assertEqual(self.run.result, merged)


class WorkerSetupMergeProofTests(SimpleTestCase):
    def setUp(self):
        website = SimpleNamespace(pk="connection-1", generation=11, repository_id=777,
            organization_id=17, organization=SimpleNamespace(domain="acme.example"),
            github_repo="acme/website", branch="main", app_root="", state="connected",
            verified_sha=MERGED, blockers=[{"deletion_epoch": 12}], targets=Mock(), operations=Mock())
        marker = {"kind": "artifact_digest", "value": DIGEST}
        self.reviewed = {"target_id": "articles", "kind": "registry_driven_seo",
            "delivery_adapter": "registry_entry", "publish_capability": "direct", "route_path": "/articles",
            "contract_digest": CONTRACT_DIGEST, "live_marker": marker,
            "contract": {"live_marker": marker, "contract_digest": CONTRACT_DIGEST}}
        self.build = {"attestation_origin": "setup_merge_carry", "status": "passed", "source_sha": MERGED,
            "carried_from": HEAD, "setup_run_id": "reviewed-setup", "source_tree_sha": TREE}
        self.binding = {**contract_for(website), "source_sha": MERGED, "target_id": "articles",
            "contract_digest": CONTRACT_DIGEST, "method": carry.live.METHOD}
        receipt = {**self.binding, "status": "passed", "attestation_origin": carry.live.ORIGIN,
            "artifact_digest": DIGEST, "public_url": "https://acme.example/articles"}
        self.selected = {**self.reviewed, "verification": self.build, "live_verification": {
            **receipt, "status": "verified",
        }}
        self.target = SimpleNamespace(contract=self.selected, adapter="registry_entry",
            source_sha=MERGED, verified_at=True, capabilities={"publishingReady": True})
        self.operation = SimpleNamespace(pk="deployment-receipt", payload=self.binding, receipt=receipt)
        website.targets.filter.return_value.first.return_value = self.target
        website.operations.filter.return_value.first.return_value = self.operation
        self.config = SimpleNamespace(organization_id=17, website_connection=website,
            default_publish_target_id="articles", publish_targets=[self.selected])
        self.enterContext(patch.object(carry, "_reviewed_target", return_value=(HEAD, MERGED, self.reviewed)))
        self.enterContext(patch.object(carry.ContentFactoryRun.objects, "filter"))
        self.enterContext(patch.object(carry, "observed_source_sha", return_value=MERGED))
        self.enterContext(patch.object(carry.live, "selected", return_value=True))

    def test_worker_proof_uses_the_saved_build_and_deployment_receipts(self):
        proof = carry.worker_publish_targets(self.config)[0]["verification"]
        self.assertEqual(proof, {
            **self.build, **contract_for(self.config.website_connection), "target_id": "articles",
            "adapter_id": "registry_entry", "operation_id": self.operation.pk, "operation_attempt": 1,
            "deletion_epoch": 12, "contract_digest": CONTRACT_DIGEST,
            "evidence_digest": evidence_digest({
                "binding": self.binding, "build": self.build, "deployment": self.operation.receipt,
            }),
        })
        self.assertNotIn("operation_id", self.selected["verification"])

    def test_missing_or_different_deployment_evidence_keeps_the_incomplete_proof(self):
        receipt = self.operation.receipt
        for field, value in (("source_sha", BASE), ("artifact_digest", "e" * 64), ("target_id", "other")):
            with self.subTest(field=field):
                self.operation.receipt = {**receipt, field: value}
                self.assertIs(carry.worker_publish_targets(self.config), self.config.publish_targets)
        self.config.website_connection.operations.filter.return_value.first.return_value = None
        self.assertIs(carry.worker_publish_targets(self.config), self.config.publish_targets)


PUSHED = "e" * 40
SETUP_FILES = {"registry_path": "src/content/articles/registry.ts", "route_shell_path": "src/app/articles/page.tsx",
    "managed_files": ["src/lib/articles.ts"]}


def _worker_accepts(projected, config, website):
    """The source-bound checks Content Factory's ``verified_registered_target`` applies.

    Mirrors CF ``harness/registered_targets.py`` for the fields this backend
    projects; ``native_contract_digest`` is computed by CF and is not repeated here.
    """
    import re
    scope = contract_for(website)
    rows = [row for row in projected or [] if row.get("target_id") == config.default_publish_target_id]
    if len(rows) != 1:
        return False
    proof = rows[0].get("verification") or {}
    identity = ("website_connection_id", "connection_generation", "repository_id", "github_repo")
    return bool(proof.get("status") in {"passed", "verified"}
        and proof.get("source_sha") == website.verified_sha
        and all(proof.get(key) == scope[key] for key in identity)
        and proof.get("target_id") == config.default_publish_target_id
        and proof.get("adapter_id") == rows[0].get("delivery_adapter")
        and proof.get("operation_id") and type(proof.get("operation_attempt")) is int and proof["operation_attempt"] >= 1
        and type(proof.get("deletion_epoch")) is int and proof["deletion_epoch"] == deletion_epoch(website)
        and re.fullmatch(r"[a-f0-9]{64}", str(proof.get("evidence_digest") or ""))
        and proof.get("contract_digest") == rows[0].get("contract_digest"))


@override_settings(**ALL_P1_FLAGS_ON, ARTICLES_READY_ON_MERGE_ENABLED=True, ARTICLES_PROOF_CARRY_FORWARD_ENABLED=True)
class CarriedSetupMergeProofTests(SetupMergeFixture, TestCase):
    """A13: the first founder push after a setup-merge Ready must not switch the worker projection off.

    La Vie (18 Nov 2026) is Ready on a setup-merge proof; a push that leaves
    MLAI's setup files alone carries that proof to the new head. The worker then
    needs a projected proof at the new head, while sealed and custom targets keep
    exact-commit proof.
    """

    def ready_then_push(self, *, files=("src/app/page.tsx",)):
        from . import website_reconciliation as reconciliation
        from . import website_source_carry
        from .tests_website_source_carry import _compare
        self.assertEqual(self.dispatch()["status"], "completed")
        self.config.refresh_from_db()
        self.website.refresh_from_db()
        projected = carry.worker_publish_targets(self.config)
        self.assertTrue(_worker_accepts(projected, self.config, self.website), projected)
        self.config.article_system_setup_cache = SETUP_FILES
        self.config.save(update_fields=["article_system_setup_cache"])
        with patch.object(website_source_carry, "compare_sources", return_value=_compare(*files)), \
                patch("content_factory.website_reconciliation._find_owned_merge", return_value=None), \
                patch("content_factory.website_prepare.wake_prepare_for_source"):
            result = reconciliation.handle_website_github_event("push", {
                "repository": {"id": 777}, "ref": "refs/heads/main", "after": PUSHED})
        self.website.refresh_from_db()
        self.target.refresh_from_db()
        return result

    def recheck(self):
        from django.utils import timezone
        from . import website_reconciliation as reconciliation
        op = self.website.operations.get(action="source-reverify", payload__source_sha=PUSHED)
        state = reconciliation._process_source_reverification(op.pk, timezone.now())
        self.website.refresh_from_db()
        self.target.refresh_from_db()
        self.config.refresh_from_db()
        return state, op

    def test_a_carried_push_keeps_a_worker_proof_at_the_new_head(self):
        result = self.ready_then_push()
        self.assertEqual(result.get("carried"), 1)
        self.assertEqual(self.website.verified_sha, PUSHED)
        state, _ = self.recheck()
        self.assertEqual(state, "completed")
        projected = carry.worker_publish_targets(self.config)
        proof = projected[0]["verification"]
        self.assertEqual(proof["source_sha"], PUSHED)
        deployment = self.website.operations.get(action="deployment-verify", payload__source_sha=PUSHED)
        self.assertEqual(proof["operation_id"], str(deployment.pk))
        self.assertTrue(_worker_accepts(projected, self.config, self.website), proof)

    def test_before_the_recheck_the_worker_still_waits(self):
        self.ready_then_push()
        self.assertFalse(_worker_accepts(carry.worker_publish_targets(self.config), self.config, self.website))

    def test_a_push_that_changes_setup_files_keeps_exact_commit_proof(self):
        result = self.ready_then_push(files=("src/app/articles/page.tsx",))
        self.assertNotIn("carried", result)
        self.assertEqual(self.website.verified_sha, MERGED)
        self.assertFalse(_worker_accepts(carry.worker_publish_targets(self.config), self.config, self.website))

    def test_a_sealed_target_is_never_projected_from_a_carried_head(self):
        self.ready_then_push()
        self.recheck()
        with patch("content_factory.website_sealed_workflow.required", return_value=True):
            self.assertIs(carry.worker_publish_targets(self.config), self.config.publish_targets)

    def test_a_custom_contract_target_is_never_projected_from_a_carried_head(self):
        self.ready_then_push()
        self.recheck()
        custom = {**self.run.result["publish_targets"][0], "delivery_adapter": "custom_contract_v1"}
        self.run.result["publish_targets"] = [custom]
        self.run.save(update_fields=["result"])
        self.assertIs(carry.worker_publish_targets(self.config), self.config.publish_targets)

    def test_a_carry_without_a_backend_recorded_link_is_refused(self):
        self.ready_then_push()
        self.recheck()
        self.website.operations.filter(action="source-reverify", payload__source_sha=PUSHED).update(
            payload={"source_sha": PUSHED, "carried_from": "", "scan_required": True})
        self.assertIs(carry.worker_publish_targets(self.config), self.config.publish_targets)

    def test_a_failed_carry_recheck_withdraws_the_projection(self):
        self.ready_then_push()
        self.recheck()
        self.website.operations.filter(action="source-reverify", payload__source_sha=PUSHED).update(
            state="needs_attention", receipt={"status": "needs_attention", "retryable": False})
        self.assertIs(carry.worker_publish_targets(self.config), self.config.publish_targets)

    def test_two_quick_carried_pushes_follow_the_recorded_chain(self):
        from django.utils import timezone
        from . import website_reconciliation as reconciliation
        from . import website_source_carry
        from .tests_website_source_carry import _compare
        self.ready_then_push()
        first = self.website.operations.get(action="source-reverify", payload__source_sha=PUSHED)
        later = "f" * 40
        with patch.object(website_source_carry, "compare_sources", return_value=_compare("README.md")), \
                patch("content_factory.website_reconciliation._find_owned_merge", return_value=None), \
                patch("content_factory.website_prepare.wake_prepare_for_source"):
            reconciliation.handle_website_github_event("push", {
                "repository": {"id": 777}, "ref": "refs/heads/main", "after": later})
        # The first push's own re-check finds the branch moved on and is superseded.
        self.head.side_effect = WebsiteAuthorityError("website_source_changed", "Branch moved.")
        self.assertEqual(reconciliation._process_source_reverification(first.pk, timezone.now()), "cancelled")
        self.head.side_effect = None
        second = self.website.operations.get(action="source-reverify", payload__source_sha=later)
        self.assertEqual(second.payload["carried_from"], PUSHED)
        self.assertEqual(reconciliation._process_source_reverification(second.pk, timezone.now()), "completed")
        self.website.refresh_from_db()
        self.config.refresh_from_db()
        projected = carry.worker_publish_targets(self.config)
        self.assertEqual(projected[0]["verification"]["source_sha"], later)
        self.assertTrue(_worker_accepts(projected, self.config, self.website), projected)
        # A failed re-check anywhere on the chain breaks it.
        self.website.operations.filter(pk=first.pk).update(state="needs_attention",
            receipt={"status": "needs_attention", "retryable": False})
        self.assertIs(carry.worker_publish_targets(self.config), self.config.publish_targets)

    def test_a_long_run_of_carried_pushes_keeps_the_projection(self):
        """More pushes than any per-link query budget: the chain is read in one query."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from django.utils import timezone
        from . import website_reconciliation as reconciliation
        from . import website_source_carry
        from .tests_website_source_carry import _compare
        self.ready_then_push()
        previous = self.website.operations.get(action="source-reverify", payload__source_sha=PUSHED)
        heads = [f"{index:040x}" for index in range(1, 26)]
        for head in heads:
            with patch.object(website_source_carry, "compare_sources", return_value=_compare("README.md")), \
                    patch("content_factory.website_reconciliation._find_owned_merge", return_value=None), \
                    patch("content_factory.website_prepare.wake_prepare_for_source"):
                reconciliation.handle_website_github_event("push", {
                    "repository": {"id": 777}, "ref": "refs/heads/main", "after": head})
            self.head.side_effect = WebsiteAuthorityError("website_source_changed", "Branch moved.")
            self.assertEqual(reconciliation._process_source_reverification(previous.pk, timezone.now()), "cancelled")
            self.head.side_effect = None
            previous = self.website.operations.get(action="source-reverify", payload__source_sha=head)
        self.assertEqual(reconciliation._process_source_reverification(previous.pk, timezone.now()), "completed")
        self.website.refresh_from_db()
        self.config.refresh_from_db()
        with CaptureQueriesContext(connection) as queries:
            projected = carry.worker_publish_targets(self.config)
        chain_queries = [query for query in queries.captured_queries if "source-reverify" in query["sql"]]
        self.assertEqual(len(chain_queries), 1)
        self.assertEqual(projected[0]["verification"]["source_sha"], heads[-1])
        self.assertTrue(_worker_accepts(projected, self.config, self.website), projected)

    def test_a_link_cancelled_for_another_reason_breaks_the_chain(self):
        from django.utils import timezone
        from . import website_reconciliation as reconciliation
        from . import website_source_carry
        from .tests_website_source_carry import _compare
        self.ready_then_push()
        first = self.website.operations.get(action="source-reverify", payload__source_sha=PUSHED)
        later = "f" * 40
        with patch.object(website_source_carry, "compare_sources", return_value=_compare("README.md")), \
                patch("content_factory.website_reconciliation._find_owned_merge", return_value=None), \
                patch("content_factory.website_prepare.wake_prepare_for_source"):
            reconciliation.handle_website_github_event("push", {
                "repository": {"id": 777}, "ref": "refs/heads/main", "after": later})
        self.website.operations.filter(pk=first.pk).update(state="cancelled",
            receipt={"status": "superseded", "code": "verified_target_required", "retryable": False})
        second = self.website.operations.get(action="source-reverify", payload__source_sha=later)
        self.assertEqual(reconciliation._process_source_reverification(second.pk, timezone.now()), "completed")
        self.config.refresh_from_db()
        self.assertIs(carry.worker_publish_targets(self.config), self.config.publish_targets)

    @override_settings(ROO_API_KEY="synthetic-test-key", INTERNAL_API_KEY="synthetic-test-key")
    def test_the_config_endpoint_serves_the_worker_a_proof_at_the_carried_head(self):
        """The worker reads its targets from the org-config endpoint, not the helper."""
        from rest_framework.test import APIRequestFactory
        from .service_views import ContentFactoryOrgConfigView
        self.ready_then_push()
        self.recheck()
        request = APIRequestFactory().get("/synthetic", {"domain": self.website.organization.domain},
            HTTP_X_API_KEY="synthetic-test-key")
        response = ContentFactoryOrgConfigView.as_view()(request)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        projected = response.data["publish_targets"]
        self.assertEqual(projected[0]["verification"]["source_sha"], PUSHED)
        self.assertTrue(_worker_accepts(projected, self.config, self.website), projected)
