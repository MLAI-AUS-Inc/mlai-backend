"""A first-time articles setup with only inventory after a verified squash merge."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from workflow_runs.models import ContentFactoryRun
from . import website_setup_merge_proof as carry
from .activation import durable_integration_evidence
from .founder_journey_testing import ALL_P1_FLAGS_ON, make_new_journey_org
from .tests_live_url_verification import BASE, MERGED, CONTRACT_DIGEST, DIGEST, LISTING, _connect
from .tests_setup_pr_changes_unit import static_setup_run
from .vibe_marketing_views import _record_setup_verification_refusal
from .website_connections import contract_for, summary_for
from .website_contract import WebsiteAuthorityError
from .website_live_verification import build_proof
from .website_operations import deletion_epoch
from .website_prepare import _dispatch_prepare_step

HEAD, TREE = "c" * 40, "d" * 40


@override_settings(**ALL_P1_FLAGS_ON)
class SetupMergeProofTests(TestCase):
    def setUp(self):
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
