"""Synthetic lifecycle and concurrency regressions in specifically approved DBs."""

import threading
from types import SimpleNamespace
from unittest.mock import patch

from django.db import close_old_connections, transaction
from django.test import TestCase, TransactionTestCase

from .tests_website_connections import WebsiteDatabaseFixture
from .website_connections import authority_guard, contract_for, transition_connection
from .website_contract import WebsiteAuthorityError
from .website_operations import reserve_workflow_operation, cancel_operation, deletion_epoch


class WebsiteReliabilityLifecycleTests(WebsiteDatabaseFixture, TestCase):
    def test_cancel_fences_one_operation_and_leaves_unrelated_work(self):
        first, other = {**self.binding, "client_request_id": "one"}, {**self.binding, "client_request_id": "two"}
        op = reserve_workflow_operation(self.website, workflow="article_generation", payload=first)
        reserve_workflow_operation(self.website, workflow="article_generation", payload=other)
        cancel_operation(self.config, data={**self.binding, "operation_id": str(op.pk)}, idempotency_key="cancel")
        with self.assertRaises(WebsiteAuthorityError):
            with authority_guard(first):
                pass
        with authority_guard(other):
            pass
        self.website.refresh_from_db()
        self.assertEqual(self.website.generation, 1)

    def test_purge_retains_deletion_fence_and_worker_cleanup_is_narrow(self):
        op = transition_connection(self.config, action="purge", expected=self.binding, idempotency_key="purge")
        self.website.refresh_from_db()
        self.assertGreater(deletion_epoch(self.website), 0)
        self.assertEqual(self.website.state, "disconnected")
        payload = {**self.binding, "operation_id": str(op.pk), "operation_attempt": 1,
            "deletion_epoch": deletion_epoch(self.website), "run_ids": []}
        with authority_guard(payload, action="worker_cleanup"):
            pass
        for bad in ({"deletion_epoch": 0}, {"operation_attempt": 2}, {"run_ids": ["foreign"]}):
            with self.subTest(bad=bad), self.assertRaises(WebsiteAuthorityError):
                with authority_guard({**payload, **bad}, action="worker_cleanup"):
                    pass
        with self.assertRaises(WebsiteAuthorityError):
            with authority_guard(payload, action="setup"):
                pass

    def test_repeated_logical_setup_resumes_persisted_dispatch_key(self):
        first, retry = {**self.binding, "client_request_id": "one"}, {**self.binding, "client_request_id": "reload"}
        op = reserve_workflow_operation(self.website, workflow="article_system_setup", payload=first)
        resumed = reserve_workflow_operation(self.website, workflow="article_system_setup", payload=retry)
        self.assertEqual(op.pk, resumed.pk)
        self.assertEqual(retry["client_request_id"], "one")

    def test_changed_setup_inputs_cannot_reuse_request_identity(self):
        reserve_workflow_operation(self.website, workflow="article_system_setup", payload={**self.binding, "client_request_id": "same", "path": "/articles"})
        with self.assertRaisesMessage(WebsiteAuthorityError, "different"):
            reserve_workflow_operation(self.website, workflow="article_system_setup", payload={**self.binding, "client_request_id": "same", "path": "/blog"})

    def test_cancel_invalid_uuid_is_typed_not_a_database_validation_error(self):
        with self.assertRaises(WebsiteAuthorityError) as error:
            cancel_operation(self.config, data={**self.binding, "operation_id": "invalid"}, idempotency_key="bad")
        self.assertEqual(error.exception.status, 422)

    def test_stale_configuration_revision_cannot_change_lifecycle(self):
        with self.assertRaises(WebsiteAuthorityError) as error:
            transition_connection(self.config, action="disconnect", expected={**self.binding, "configuration_revision": 999})
        self.assertEqual(error.exception.code, "website_configuration_changed")

    def test_account_revocation_fences_owned_bindings_and_keeps_another_users_installation(self):
        from django.contrib.auth import get_user_model
        from founder_tools.models import VibeRaisingProfile, VibeRaisingCompany
        from integrations.models import GitHubInstallation
        from .website_github_revocation import revocation_plan, apply_revocation
        user = get_user_model().objects.create(email="owner@example.test")
        other = get_user_model().objects.create(email="other@example.test")
        profile = VibeRaisingProfile.objects.create(user=user, role="founder")
        VibeRaisingCompany.objects.create(profile=profile, organization=self.org, name="Synthetic")
        self.website.authorized_by = user
        self.website.save(update_fields=["authorized_by"])
        GitHubInstallation.objects.create(user=user, installation_id="45")
        GitHubInstallation.objects.create(user=other, installation_id="45")
        plan = revocation_plan(user)
        self.assertEqual(len(plan["affectedCompanies"]), 1)
        result = apply_revocation(user, self.config, data={"approved": True, "plan_digest": plan["planDigest"]}, idempotency_key="same")
        self.website.refresh_from_db()
        self.assertEqual(self.website.state, "revoked")
        self.assertFalse(GitHubInstallation.objects.filter(user=user).exists())
        self.assertTrue(GitHubInstallation.objects.filter(user=other).exists())
        repeated = apply_revocation(user, self.config, data={"approved": True, "plan_digest": plan["planDigest"]}, idempotency_key="same")
        self.assertEqual(repeated["status"], result.receipt["status"])
        self.assertFalse(repeated["provider_revocation_complete"])

    def test_custom_verification_reserves_one_child_and_projects_pending_progress(self):
        from .website_support import owner_support_operation
        from .website_journey import journey_for_context
        from workflow_runs.models import ContentFactoryRun
        self.website.verified_sha = "a" * 40
        self.website.save(update_fields=["verified_sha"])
        payload = {**self.binding, "client_request_id": "parent"}
        parent = reserve_workflow_operation(self.website, workflow="article_system_setup", payload=payload)
        ContentFactoryRun.objects.create(run_id="parent-run", organization=self.org, domain=self.org.domain, github_repo=self.config.github_repo,
            workflow="article_system_setup", status="completed", run_request=payload)
        ordinary = {**self.binding, "client_request_id": "ordinary-child", "source_run_id": "parent-run"}
        reserve_workflow_operation(self.website, workflow="article_revision", payload=ordinary)
        ContentFactoryRun.objects.create(run_id="ordinary-child", organization=self.org, domain=self.org.domain,
            workflow="article_revision", status="completed", run_request=ordinary)
        context = SimpleNamespace(organization=self.org, company=SimpleNamespace(pk="42"))
        data = {**self.binding, "run_id": "parent-run", "phase": "verify", "idempotency_key": "one-child", "contract": {
            "schema_version": 1, "reviewed": True, "runtime_family": "static", "runtime_version": "1", "build_command": ["build"],
            "content_path_pattern": "content/{slug}.md", "route_template": "/articles/{slug}", "listing_route": "/articles"},
            "verification": {"source_sha": "a" * 40, "target_id": "custom", "file_overrides": {}}}
        with patch("content_factory.vibe_marketing_views._content_factory_remote_config", return_value={"enabled": True, "base_url": "https://worker.test"}), \
             patch("integrations.http_client.post", return_value=SimpleNamespace(status_code=202, json=lambda: {"status": "queued"})) as dispatch:
            child = owner_support_operation(context, self.config, action="custom-contract", data=data)
            retry = owner_support_operation(context, self.config, action="custom-contract", data=data)
        self.assertNotEqual(child.pk, parent.pk)
        self.assertEqual(child.pk, retry.pk)
        dispatch.assert_called_once()
        journey = journey_for_context(context, self.config, capabilities={"repositoryAccessVerified": True})
        self.assertEqual(journey["operation"]["state"], "running")
        self.assertEqual(journey["operation"]["runId"], str(child.pk))
        self.assertEqual([row["runId"] for row in journey["sourceRuns"]], ["parent-run"])
        self.assertEqual([row["runId"] for row in journey["ciSourceRuns"]], [str(child.pk), "parent-run"])

    def test_cleanup_requires_reviewed_manifest_and_current_provider_live_evidence(self):
        from .website_models import WebsiteConnectionOperation
        from .website_cleanup_verification import verify_cleanup_deployment
        op = WebsiteConnectionOperation.objects.create(connection=self.website, generation=1, action="cleanup", state="awaiting_deployment",
            idempotency_key="cleanup-completion", receipt={"merge_sha": "a" * 40})
        reviewed = {**self.binding, "operation_id": str(op.pk), "verification_routes": [{"path": "/forged", "expected_status": 404}]}
        with patch("content_factory.website_live_fetch.fetch_live_route") as fetch:
            with self.assertRaises(WebsiteAuthorityError) as denial:
                verify_cleanup_deployment(self.config, data=reviewed)
        self.assertEqual(denial.exception.code, "cleanup_verification_manifest_required")
        fetch.assert_not_called()
        op.receipt["verification_routes"] = [{"path": "/articles", "expected_status": 404}]
        op.save(update_fields=["receipt", "updated_at"])
        replies = [SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"sha": "a" * 40}),
            SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"check_runs": [{"head_sha": "a" * 40, "status": "completed", "conclusion": "success", "app": {"slug": "github-actions"}}]})]
        with patch("integrations.services.github_app.create_installation_access_token", return_value=SimpleNamespace(token="synthetic")), \
             patch("integrations.http_client.get", side_effect=replies), patch("integrations.http_client.delete"), \
             patch("content_factory.website_live_fetch.fetch_live_route", return_value=(b"gone", {})) as fetch:
            completed = verify_cleanup_deployment(self.config, data=reviewed)
        self.assertEqual(completed.state, "completed")
        self.assertTrue(completed.receipt["cleanup_complete"])
        fetch.assert_called_once_with(f"https://{self.org.domain}/articles", self.org.domain, expected_status=404)

    def test_required_environment_ci_can_certify_custom_target_then_configure_and_verify_live(self):
        from .website_support import owner_support_operation, validate_custom_contract, current_verification_data
        from .website_verification import BOOLEAN_PROOFS, validated_ci_identity, verify_live_deployment
        from .website_contract import evidence_digest
        from .website_journey import journey_for_context
        from .website_models import WebsiteConnectionTarget
        from workflow_runs.models import ContentFactoryRun
        self.website.verified_sha = "a" * 40
        self.website.capabilities = {"inventoryReady": True, "generationReady": False}
        self.website.save(update_fields=["verified_sha", "capabilities"])
        parent_binding = {**self.binding, "expected_source_sha": "a" * 40, "client_request_id": "parent-ci"}
        reserve_workflow_operation(self.website, workflow="article_system_setup", payload=parent_binding)
        ContentFactoryRun.objects.create(run_id="original-ci", organization=self.org, domain=self.org.domain,
            workflow="article_system_setup", status="completed", run_request=parent_binding)
        contract = validate_custom_contract({"schema_version": 1, "reviewed": True, "runtime_family": "static", "runtime_version": "1",
            "artifact_digest": "e" * 64,
            "build_command": ["build"], "environment_names": ["CONTENT_API_URL"], "content_path_pattern": "content/{slug}.md",
            "route_template": "/articles/{slug}", "listing_route": "/articles"})
        binding = {**self.binding, "source_run_id": "original-ci", "expected_source_sha": "a" * 40, "client_request_id": "requires-ci"}
        child = reserve_workflow_operation(self.website, workflow="native_verification", payload=binding)
        binding["run_id"] = str(child.pk)
        child.payload = {**child.payload, "binding": binding, "contract": contract, "source_run_id": "original-ci", "run_id": str(child.pk)}
        child.state, child.receipt = "failed", {"status": "requires_ci"}
        child.save()
        ContentFactoryRun.objects.create(run_id=str(child.pk), organization=self.org, domain=self.org.domain,
            workflow="article_system_setup", status="blocked", run_request=binding, result={"status": "requires_ci"})
        body = {**{key: binding[key] for key in ("website_connection_id", "connection_generation", "repository_id", "operation_id", "operation_attempt", "deletion_epoch")},
            "schema_version": 2, "github_repo": self.website.github_repo, "source_sha": "b" * 40, "target_id": "custom-ci",
            "adapter_id": "custom_contract_v1", "adapter_version": 1, "contract_digest": evidence_digest(contract),
            "source_tree_sha": "b" * 40, "environment_fingerprint": "d" * 64, "lockfile_digests": {}, "artifact_digest": "e" * 64,
            "verified_routes": {"listing": "/articles", "detail": "/articles/reviewed-seed", "unknown_slug": "/articles/missing-seed"},
            "baseline_build": "passed", "patched_build": "passed", **{key: True for key in BOOLEAN_PROOFS}}
        evidence = {**body, "evidence_digest": evidence_digest(body)}
        context = SimpleNamespace(organization=self.org, company=SimpleNamespace(pk="42"))
        def seal(website, requested):
            return validated_ci_identity(requested, evidence)
        with patch("content_factory.website_verification.read_ci_proof", side_effect=seal), \
             patch("content_factory.vibe_marketing_views._content_factory_remote_config", return_value={"enabled": True, "base_url": "https://worker.test"}), \
             patch("integrations.http_client.post", return_value=SimpleNamespace(status_code=200, json=lambda: {"status": "verified"})) as worker:
            owner_support_operation(context, self.config, action="ci-attestation", data={**self.binding, "run_id": "original-ci", "evidence": evidence})
        self.config.refresh_from_db()
        self.website.refresh_from_db()
        self.assertEqual(worker.call_args.kwargs["json"]["run_id"], str(child.pk))
        self.assertEqual(self.config.default_publish_target_id, "custom-ci")
        self.assertEqual(self.website.verified_sha, "b" * 40)
        self.assertTrue(WebsiteConnectionTarget.objects.get(connection=self.website, target_key="custom-ci").capabilities["adapterCertified"])
        self.assertFalse(self.website.capabilities.get("generationReady"))
        generation = {"reviewed": True,
            "article_template": "# Article\nReviewed source", "design_guide": "# Design\nReviewed design",
            "live_marker": {"kind": "artifact_digest", "schema_version": 1, "meta_name": "mlai-artifact-digest", "value": "e" * 64}}
        data = {**self.binding, "run_id": "original-ci", "phase": "configure", "contract": contract,
            "generation": generation, "configuration_revision": self.website.configuration_version, "idempotency_key": "configure-after-ci"}
        with patch("integrations.http_client.post") as worker:
            configured = owner_support_operation(context, self.config, action="custom-contract", data=data)
            retry = owner_support_operation(context, self.config, action="custom-contract", data=data)
        worker.assert_not_called()
        self.assertEqual(configured.pk, retry.pk)
        self.assertTrue(configured.receipt["generation_configured"])
        parent = ContentFactoryRun.objects.get(run_id="original-ci")
        self.assertEqual(parent.run_request["expected_source_sha"], "a" * 40)
        receipt = self.website.operations.filter(action="ci-verify").latest("created_at")
        live_data = current_verification_data(self.config, {**self.binding, "operation_id": str(receipt.pk),
            "operation_attempt": 99, "deletion_epoch": 99})
        self.assertEqual(live_data["operation_id"], evidence["operation_id"])
        self.assertEqual(live_data["operation_attempt"], 1)
        bad = [(b"", {"x-mlai-artifact-digest": "e" * 64}), (b"", {"x-mlai-artifact-digest": "f" * 64})]
        with patch("content_factory.website_live_fetch.fetch_live_route", side_effect=bad):
            with self.assertRaises(WebsiteAuthorityError) as detail:
                verify_live_deployment(self.config, data=live_data)
        self.assertEqual(detail.exception.code, "deployment_source_unverified")
        with patch("content_factory.website_live_fetch.fetch_live_route", return_value=(b"", {"x-mlai-artifact-digest": "e" * 64})) as live:
            verify_live_deployment(self.config, data=live_data)
        self.assertEqual(live.call_count, 3)
        self.assertEqual(live.call_args.kwargs["expected_status"], 404)
        self.config.refresh_from_db()
        journey = journey_for_context(context, self.config, capabilities={"repositoryAccessVerified": True, "canGenerateArticle": True})
        self.assertFalse(journey["capabilities"]["canPublishArticle"])
        self.assertEqual(journey["reasonCode"], "publishing_adapter_required")
        self.assertEqual(journey["prerequisites"]["verification"]["status"], "complete")

    def test_configuration_snapshots_cannot_erase_complete_inventory(self):
        from .website_models import WebsiteScanSnapshot
        from .website_journey import journey_for_context
        from django.utils import timezone
        context = SimpleNamespace(company=SimpleNamespace(pk="42"), organization=self.org)
        WebsiteScanSnapshot.objects.create(connection=self.website, generation=1, run_id="inventory", source_sha="a" * 40,
            fingerprint="inventory", evidence={"repository_inventory": {"discovery_complete": True, "source_sha": "a" * 40}})
        WebsiteScanSnapshot.objects.create(connection=self.website, generation=1, run_id="configure", source_sha="a" * 40,
            fingerprint="configure", evidence={"publish_targets": [{"target_id": "native"}]})
        self.website.verified_sha = "a" * 40
        self.website.save(update_fields=["verified_sha"])
        journey = journey_for_context(context, self.config, capabilities={"repositorySourceSha": "a" * 40})
        self.assertEqual(journey["prerequisites"]["inventory"]["status"], "complete")
        stale = journey_for_context(context, self.config, capabilities={"repositorySourceSha": "b" * 40})
        self.assertEqual(stale["prerequisites"]["inventory"]["status"], "stale")

    def test_native_authoring_does_not_grant_publication_before_live_proof(self):
        from .website_models import WebsiteConnectionTarget, WebsiteConnectionOperation
        from .activation import article_capabilities, live_deployment_verified
        from django.utils import timezone
        self.website.verified_sha = "a" * 40
        self.website.last_verified_at = timezone.now()
        self.website.save(update_fields=["verified_sha", "last_verified_at"])
        target = WebsiteConnectionTarget.objects.create(connection=self.website, generation=1, target_key="native", adapter="react_component",
            source_sha="a" * 40, verified_at=timezone.now(), capabilities={"publishingReady": True},
            contract={"contract_digest": "d" * 64, "live_marker": {"value": "e" * 64}})
        self.config.default_publish_target_id = "native"
        self.config.save(update_fields=["default_publish_target_id"])
        arguments = {"domain": self.org.domain, "account": {"saved": True, "owned": True},
            "repository_access": {"verified": True, "writable": True, "branch": "main", "sha": "a" * 40}}
        caps = article_capabilities(self.config, **arguments)
        self.assertTrue(caps["canGenerateArticle"])
        self.assertFalse(caps["canPublishArticle"])
        with self.assertRaises(WebsiteAuthorityError) as error:
            with authority_guard({**self.binding, "expected_source_sha": "a" * 40}, action="publish"):
                pass
        self.assertEqual(error.exception.code, "deployment_verification_required")
        receipt = {"status": "passed", "source_sha": "a" * 40, "connection_generation": 1, "target_id": "native",
            "contract_digest": "d" * 64, "artifact_digest": "e" * 64, "public_url": "https://site.example.test/articles", "checked_at": timezone.now().isoformat()}
        op = WebsiteConnectionOperation.objects.create(connection=self.website, generation=1, action="deployment-verify", state="completed",
            idempotency_key="synthetic-live", payload={"source_sha": "a" * 40, "target_id": "native"}, receipt=receipt)
        self.assertTrue(article_capabilities(self.config, **arguments)["canPublishArticle"])
        with authority_guard({**self.binding, "expected_source_sha": "a" * 40}, action="publish"):
            pass
        op.receipt = {**receipt, "artifact_digest": "f" * 64}
        op.save(update_fields=["receipt"])
        self.assertFalse(live_deployment_verified(self.website, target))

    def test_certified_custom_contract_does_not_authorize_repository_authoring(self):
        from .website_models import WebsiteConnectionTarget
        from .activation import durable_integration_evidence
        from .website_connections import summary_for
        from django.utils import timezone
        WebsiteConnectionTarget.objects.create(connection=self.website, generation=1, target_key="custom", adapter="custom_contract_v1",
            source_sha=self.website.verified_sha, verified_at=timezone.now(), contract={"route_path": "/articles"},
            capabilities={"adapterCertified": True, "publishingReady": True})
        self.config.default_publish_target_id = "custom"
        self.config.save(update_fields=["default_publish_target_id"])
        self.website.capabilities = {"generationReady": True, "publishingReady": True, "previewSupported": True}
        self.website.save(update_fields=["capabilities"])
        self.assertEqual(durable_integration_evidence(self.config, self.website)["reasonCode"], "publishing_adapter_required")
        summary = summary_for(self.config)
        self.assertFalse(summary["capabilities"]["generationReady"])
        self.assertFalse(summary["capabilities"]["publishingReady"])
        with self.assertRaises(WebsiteAuthorityError) as error:
            with authority_guard(self.binding, action="publish"):
                pass
        self.assertEqual(error.exception.code, "publishing_adapter_required")

    def test_new_custom_source_retains_only_exact_reverified_generation_marker(self):
        from .website_models import WebsiteConnectionTarget
        from .website_support import promote_custom_target
        from django.utils import timezone
        self.website.capabilities = {"generationReady": True}
        self.website.save(update_fields=["capabilities"])
        marker = {"kind": "artifact_digest", "schema_version": 1, "meta_name": "mlai-artifact-digest", "value": "e" * 64}
        WebsiteConnectionTarget.objects.create(connection=self.website, generation=1, target_key="custom", adapter="custom_contract_v1",
            source_sha="a" * 40, verified_at=timezone.now(), contract={"contract_digest": "d" * 64, "live_marker": marker})
        contract = {"content_path_pattern": "content/{slug}.md", "listing_route": "/articles", "route_template": "/articles/{slug}"}
        proof = {"target_id": "custom", "source_sha": "b" * 40, "contract_digest": "d" * 64, "artifact_digest": "e" * 64}
        promote_custom_target(self.website, contract=contract, proof=proof)
        self.website.refresh_from_db()
        self.assertTrue(self.website.capabilities["generationReady"])
        self.assertEqual(WebsiteConnectionTarget.objects.get(connection=self.website, target_key="custom").contract["live_marker"], marker)
        promote_custom_target(self.website, contract=contract, proof={**proof, "source_sha": "c" * 40, "artifact_digest": "f" * 64})
        self.website.refresh_from_db()
        self.assertFalse(self.website.capabilities["generationReady"])
        self.assertNotIn("live_marker", WebsiteConnectionTarget.objects.get(connection=self.website, target_key="custom").contract)

    def test_automatic_ci_source_advancement_requires_owned_reviewed_lineage(self):
        from .website_models import WebsiteConnectionTarget, WebsiteRepositoryMutation
        from .website_verification import record_ci_attestation, BOOLEAN_PROOFS, validated_ci_identity
        from .website_contract import evidence_digest
        self.website.verified_sha = "a" * 40
        self.website.save(update_fields=["verified_sha"])
        binding = {**self.binding, "expected_source_sha": "a" * 40, "client_request_id": "native-ci"}
        op = reserve_workflow_operation(self.website, workflow="article_system_setup", payload=binding)
        WebsiteConnectionTarget.objects.create(connection=self.website, generation=1, target_key="native-ci", adapter="react_article_system",
            source_sha="a" * 40, contract={"contract_digest": "d" * 64})
        body = {**{key: binding[key] for key in ("website_connection_id", "connection_generation", "repository_id", "operation_id", "operation_attempt", "deletion_epoch")},
            "schema_version": 2, "github_repo": self.website.github_repo, "source_sha": "b" * 40,
            "target_id": "native-ci", "adapter_id": "react_article_system", "adapter_version": 1, "contract_digest": "d" * 64,
            "source_tree_sha": "b" * 40, "environment_fingerprint": "f" * 64, "lockfile_digests": {}, "artifact_digest": "e" * 64,
            "baseline_build": "passed", "patched_build": "passed", **{key: True for key in BOOLEAN_PROOFS}}
        proof = {**body, "evidence_digest": evidence_digest(body)}
        data = {**proof, "run_id": "reviewed-native", "expected_source_sha": "a" * 40}
        with self.assertRaises(WebsiteAuthorityError) as unowned:
            record_ci_attestation(data)
        self.assertEqual(unowned.exception.code, "website_source_review_required")
        WebsiteRepositoryMutation.objects.create(connection=self.website, generation=1, operation_id="owned-native-patch", run_id="reviewed-native",
            base_sha="a" * 40, head_sha="c" * 40, patch_digest="d" * 64, status="applied", files=[])
        def seal(website, requested):
            self.assertEqual(requested["expected_source_sha"], "b" * 40)
            return validated_ci_identity(requested, proof)
        with patch("content_factory.website_verification.read_ci_proof", side_effect=seal), \
             patch("integrations.services.github_app.create_installation_access_token", return_value=SimpleNamespace(token="synthetic")), \
             patch("integrations.http_client.get", return_value=SimpleNamespace(status_code=200, json=lambda: {"status": "ahead"})) as compare, \
             patch("integrations.http_client.delete"):
            record_ci_attestation(data)
        compare.assert_called_once()
        self.assertIn("c" * 40 + "..." + "b" * 40, compare.call_args.args[0])
        self.website.refresh_from_db()
        op.refresh_from_db()
        self.assertEqual(self.website.verified_sha, "b" * 40)
        self.assertEqual(binding["expected_source_sha"], "a" * 40)
        self.assertEqual(op.generation, self.website.generation)


class WebsiteReliabilityProviderConcurrencyTests(WebsiteDatabaseFixture, TransactionTestCase):
    def test_provider_write_releases_locks_and_retains_effect_after_concurrent_disconnect(self):
        from .website_connections import guarded_backend_run_action
        from .vibe_marketing_views import _github_api_request
        from workflow_runs.models import ContentFactoryRun
        payload = {**self.binding, "client_request_id": "provider-boundary"}
        op = reserve_workflow_operation(self.website, workflow="article_system_setup", payload=payload)
        run = ContentFactoryRun.objects.create(run_id="provider-boundary", organization=self.org, domain=self.org.domain,
            github_repo=self.website.github_repo, workflow="article_system_setup", status="running", run_request=payload)
        def provider(*args, **kwargs):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            transition_connection(self.config, action="disconnect", expected=self.binding)
            return SimpleNamespace(status_code=200, content=b"{}", json=lambda: {"merged": True, "sha": "b" * 40})
        @guarded_backend_run_action("setup")
        def merge(*, run):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            return _github_api_request("PUT", "/repos/example/site/pulls/1/merge", token="synthetic", body={"sha": "a" * 40})
        with patch("integrations.http_client.request", side_effect=provider):
            self.assertTrue(merge(run=run)["merged"])
        op.refresh_from_db()
        self.assertEqual(op.state, "cancelled")
        self.assertTrue(op.receipt["repository_modified"])
        self.assertFalse(op.receipt["cancellation_undoes_remote_writes"])

    def test_disconnect_does_not_wait_for_provider_read_and_invalidates_finalize(self):
        entered, release, errors = threading.Event(), threading.Event(), []
        def verify(candidate):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            entered.set()
            self.assertTrue(release.wait(10))
        def verify_thread():
            close_old_connections()
            try:
                with authority_guard(self.binding, action="setup"):
                    errors.append("unexpected authority")
            except WebsiteAuthorityError as exc:
                errors.append(exc.code)
            finally:
                close_old_connections()
        with patch("content_factory.website_connections.verify_repository_native_target", side_effect=verify):
            worker = threading.Thread(target=verify_thread)
            worker.start()
            self.assertTrue(entered.wait(10))
            transition_connection(self.config, action="disconnect", expected=self.binding)
            release.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, ["website_connection_changed"])
