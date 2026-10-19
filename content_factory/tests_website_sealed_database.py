"""Real schema3 promotion transactions in disposable PostgreSQL CI only.

Do not run locally without explicit migration approval. The worker fixture is
unchanged; only authenticated provider reads and repository metadata are replaced.
"""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun

from . import website_connections as authority, website_goal as goals
from . import website_sealed_workflow as sealed, website_verification as verification
from .activation import durable_integration_evidence
from .models import OrganizationContentConfig
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnection, WebsiteConnectionOperation, WebsiteConnectionTarget
from .website_setup_consent import seal


def rollout_settings(organization_id):
    """Share exact fixture enrollment with database-free configuration checks."""
    return {"ARTICLES_PUBLISHING_INPUTS_V3_ENABLED": True,
        "ARTICLES_HOSTED_ATTESTATION_ENABLED": True, "ARTICLES_HOSTED_VERIFICATION_ENABLED": True,
        "ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED": True,
        "ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS": [str(organization_id)],
        "ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED": True,
        "ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS": [str(organization_id)],
        "ARTICLES_WEBSITE_EVENT_WAKES_ENABLED": False}


class SealedPromotionDatabaseTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Requires the disposable PostgreSQL CI lane")
        fixture = json.loads((Path(__file__).parent / "testdata/sealed-verification-v3/static.json").read_text())
        self.proof, self.candidate = deepcopy(fixture["proof"]), deepcopy(fixture["candidate"])
        self.org = Organization.objects.create(domain="sealed-fixture.example.test", name="Synthetic sealed fixture")
        self.website = WebsiteConnection.objects.create(id=self.proof["website_connection_id"], organization=self.org,
            github_repo=self.proof["github_repo"], repository_id=self.proof["repository_id"], installation_id="45",
            branch="main", app_root="", generation=self.proof["connection_generation"],
            verified_sha=self.proof["source_sha"], last_verified_at=timezone.now(),
            capabilities={"inventoryReady": True, "generationReady": True, "publishingReady": True})
        self.website.refresh_from_db()
        rollout = override_settings(**rollout_settings(self.org.pk))
        rollout.enable()
        self.addCleanup(rollout.disable)
        self.assertTrue(sealed.enabled(self.website), "Fixture must enroll the real hosted proof receiver")
        head = patch("content_factory.website_connections.verify_repository_head", side_effect=lambda website, sha: sha)
        head.start()
        self.addCleanup(head.stop)
        metadata = patch("content_factory.website_connections.read_repository_native_target",
            side_effect=lambda website: {"id": website.repository_id, "full_name": website.github_repo, "default_branch": "main"})
        metadata.start()
        self.addCleanup(metadata.stop)
        self.accepted = {**deepcopy(self.candidate["payload"]["publishing"]["target"]),
            "contract_digest": self.proof["contract_digest"], "contract": {"contract_digest": self.proof["contract_digest"]}}
        self.target = WebsiteConnectionTarget.objects.create(connection=self.website, generation=self.website.generation,
            target_key=self.proof["target_id"], adapter=self.proof["adapter_id"], source_sha=self.proof["source_sha"],
            contract=deepcopy(self.accepted), verified_at=timezone.now(), capabilities={"publishingReady": True})
        self.target.refresh_from_db()
        self.config = OrganizationContentConfig.objects.create(organization=self.org, website_connection=self.website,
            github_repo=self.website.github_repo, default_publish_target_id=self.target.target_key,
            publish_targets=[deepcopy(self.accepted)], article_template="# Article", design_guide="# Design")
        self.parent = WebsiteConnectionOperation.objects.create(connection=self.website, generation=self.website.generation,
            action="prepare", state="running", idempotency_key="sealed-parent", payload={"attempt": 1, "company_id": "9"})
        goal = goals.new_goal(str(self.parent.pk), goals.scope_for(self.website, "9"), now=timezone.now())
        goal["consent"] = seal(goal, {"version": 1, "publish_mode": "auto", "page_policy": "auto",
            "page_mode": "existing", "page_route": "/stories", "ci_mode": "hosted",
            "billing": {"policy": "reserved", "expected_points": 6, "accepted": True}}, user_id=4, now=timezone.now())
        self.parent.receipt = {"goal": goal, "setup_run_id": "sealed-setup-source"}
        self.parent.save(update_fields=["receipt"])
        self.source = ContentFactoryRun.objects.create(run_id="sealed-setup-source", organization=self.org,
            domain=self.org.domain, github_repo=self.website.github_repo, workflow="article_system_setup", status="completed",
            run_request={**authority.contract_for(self.website), "deletion_epoch": 0,
                "expected_source_sha": self.proof["baseline_source_sha"]})
        self.binding = {**authority.contract_for(self.website), **{key: self.proof[key] for key in (
            "operation_id", "operation_attempt", "deletion_epoch", "target_id", "contract_digest",
            "publishing_inputs_digest", "baseline_source_sha", "verifier_sha256")},
            "run_id": self.proof["operation_id"], "source_run_id": self.source.run_id,
            "expected_source_sha": self.proof["source_sha"]}
        snapshot = sealed.parent_snapshot(self.parent, self.website, self.config, self.target, self.source)
        snapshot.update(parent_binding=sealed.parent_binding(self.parent, self.website, self.proof["source_sha"]),
            accepted_target=deepcopy(self.accepted))
        self.pending = {"candidate": deepcopy(self.candidate), "snapshot": snapshot}
        self.child = WebsiteConnectionOperation.objects.create(id=self.proof["operation_id"], connection=self.website,
            generation=self.website.generation, action="workflow", state="completed", idempotency_key="sealed-child",
            payload={"attempt": 1, "workflow": "hosted_native_verification", "run_id": self.binding["run_id"],
                "binding": deepcopy(self.binding), "sealed_inputs": deepcopy(self.pending)})
        ContentFactoryRun.objects.create(run_id=self.binding["run_id"], organization=self.org, domain=self.org.domain,
            github_repo=self.website.github_repo, workflow="hosted_native_verification", status="completed", run_request=self.binding)
        self.data = {**self.proof, "run_id": self.binding["run_id"], "source_run_id": self.source.run_id}
        sealed.mark_required(self.target)

    def provider(self, *args, **kwargs):
        self.assertFalse(connection.in_atomic_block, "Provider reads must occur after releasing authority locks")
        return {"status": "completed", "run_id": self.binding["run_id"], "verification": deepcopy(self.proof)}

    def promote(self, remote=None):
        with patch("content_factory.website_sealed_candidate.authenticated_request", side_effect=remote or self.provider) as fetched:
            operation = verification.record_ci_attestation(deepcopy(self.data), origin="authenticated_hosted")
        return operation, fetched

    def stored(self):
        self.target.refresh_from_db()
        self.config.refresh_from_db()
        self.website.refresh_from_db()
        return {"target_contract": deepcopy(self.target.contract), "target_capabilities": deepcopy(self.target.capabilities),
            "target_verified_at": self.target.verified_at, "target_updated_at": self.target.updated_at,
            "config_targets": deepcopy(self.config.publish_targets), "config_updated_at": self.config.updated_at,
            "website_verified_sha": self.website.verified_sha, "website_verified_at": self.website.last_verified_at}

    def test_actual_worker_proof_commits_target_config_and_ci_receipt_together(self):
        before = self.stored()
        with CaptureQueriesContext(connection) as queries:
            operation, fetched = self.promote()
        fetched.assert_called_once()
        self.assertTrue(any("FOR UPDATE" in row["sql"] for row in queries.captured_queries))
        self.stored()
        operation.refresh_from_db()
        self.assertEqual(operation.action, "ci-verify")
        self.assertEqual(operation.state, "completed")
        self.assertEqual({key: operation.receipt[key] for key in self.proof}, self.proof)
        self.assertEqual(operation.receipt["attestation_origin"], "authenticated_hosted")
        self.assertTrue(operation.receipt["allowed"])
        self.assertEqual(self.target.contract["contract"]["publishing_inputs_v1"], self.candidate)
        self.assertEqual(self.target.contract["verification"], {**operation.receipt, "status": "verified"})
        self.assertEqual(self.config.publish_targets, [self.target.contract])
        self.assertEqual(self.target.capabilities[sealed.REQUIRED_VERSION], 3)
        self.assertTrue(self.target.capabilities["publishingReady"])
        self.assertTrue(self.website.capabilities["publishingReady"])
        self.assertGreater(self.target.verified_at, before["target_verified_at"])
        self.assertEqual(verification.current_ci_attestation(self.website, self.target), operation.receipt)
        self.assertTrue(durable_integration_evidence(self.config, self.website)["verified"])

    def test_lost_response_replays_identical_committed_receipt_without_timestamp_renewal(self):
        original, _ = self.promote()
        before = self.stored()
        receipt = deepcopy(original.receipt)
        with patch("content_factory.website_sealed_candidate.authenticated_request", side_effect=AssertionError("Replay must not refetch")) as fetched:
            replay = verification.record_ci_attestation(deepcopy(self.data), origin="authenticated_hosted")
        fetched.assert_not_called()
        self.assertEqual(replay.pk, original.pk)
        self.assertEqual(replay.receipt, receipt)
        self.assertEqual(replay.updated_at, original.updated_at)
        self.assertEqual(self.stored(), before)
        self.assertEqual(self.website.operations.filter(action="ci-verify").count(), 1)

    def denied_after_provider_change(self, change):
        before = self.stored()
        def changed(*args, **kwargs):
            response = self.provider(*args, **kwargs)
            change()
            return response
        with self.assertRaises(WebsiteAuthorityError):
            self.promote(changed)
        after = self.stored()
        self.assertEqual(after, before)
        self.assertFalse(self.website.operations.filter(action="ci-verify").exists())
        self.assertFalse(self.target.capabilities["publishingReady"])
        self.assertEqual(self.target.capabilities[sealed.REQUIRED_VERSION], 3)

    def test_parent_cancel_committed_during_provider_read_prevents_any_promotion(self):
        self.denied_after_provider_change(lambda: WebsiteConnectionOperation.objects.filter(pk=self.parent.pk).update(state="cancelled"))
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.state, "cancelled")

    def test_rebound_parent_attempt_during_provider_read_cannot_borrow_old_proof(self):
        self.denied_after_provider_change(lambda: WebsiteConnectionOperation.objects.filter(pk=self.parent.pk).update(
            payload={**self.parent.payload, "attempt": 2}))
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.payload["attempt"], 2)

    def test_rebound_hosted_child_attempt_cannot_commit_the_previous_attempt_proof(self):
        self.denied_after_provider_change(lambda: WebsiteConnectionOperation.objects.filter(pk=self.child.pk).update(
            payload={**self.child.payload, "attempt": 2}))
        self.child.refresh_from_db()
        self.assertEqual(self.child.payload["attempt"], 2)

    def test_connection_generation_change_during_provider_read_prevents_promotion(self):
        self.denied_after_provider_change(lambda: WebsiteConnection.objects.filter(pk=self.website.pk).update(generation=4))
        self.assertEqual(self.website.generation, 4)

    def test_pending_requirement_survives_rollback_flag_and_scan_target_replacement(self):
        before = self.stored()
        with override_settings(ARTICLES_PUBLISHING_INPUTS_V3_ENABLED=False):
            self.target.refresh_from_db()
            self.assertTrue(sealed.required(self.website, self.target))
            self.assertIsNone(verification.current_ci_attestation(self.website, self.target))
            self.assertFalse(durable_integration_evidence(self.config, self.website)["verified"])
            for replacement in (deepcopy(self.accepted), {**deepcopy(self.accepted), "route_path": "/invented"}):
                authority.record_scan_evidence(self.website, {"run_id": "sealed-scan-" + uuid4().hex,
                    "source_sha": self.proof["source_sha"], "publish_targets": [replacement],
                    "repository_inventory": {"discovery_complete": True}})
                self.target.refresh_from_db()
                self.assertEqual(self.target.contract, before["target_contract"])
                self.assertEqual(self.target.capabilities[sealed.REQUIRED_VERSION], 3)
                self.assertFalse(self.target.capabilities["publishingReady"])
            self.config.refresh_from_db()
            self.assertEqual(self.config.publish_targets, before["config_targets"])
            self.assertFalse(durable_integration_evidence(self.config, self.website)["verified"])
