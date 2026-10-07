"""Incident replays with synthetic persistence; no database, network or models."""
from contextlib import ExitStack, nullcontext
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid

from django.core import signing
from django.test import SimpleTestCase, RequestFactory, override_settings
from django.utils import timezone

from .incident_guards import target_update_allowed, protect_certified_config, delivered_content, safe_template_seed, publish_child_binding
from .website_contract import WebsiteAuthorityError, cleanup_plan, TEMPLATE_WRAPPERS
from .run_state import stale_execution_event
from . import article_review_views as review
from . import service_views
from .article_review_billing import image_regeneration_cost, reserve_image_charge
from .article_preview_lease import ArticlePreviewLeaseProxyView, SALT
from .website_contract import evidence_digest


class IncidentAuthorityReplays(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.sha = "a" * 40
        self.target = {"target_id": "featured", "delivery_adapter": "react_component", "publish_capability": "direct",
            "verification": {"status": "verified", "source_sha": self.sha, "checked_at": self.now.isoformat()}}
        self.previous = SimpleNamespace(generation=3, source_sha=self.sha, verified_at=self.now,
            adapter="react_component", contract=self.target, target_key="featured", capabilities={"publishingReady": True})

    def test_verified_target_survives_empty_scan_and_weaker_same_key(self):
        for candidate in ({"target_id": "featured"}, {**self.target, "verification": {}},
                {**self.target, "delivery_adapter": "bundle_only"}):
            self.assertFalse(target_update_allowed(self.previous, candidate, generation=3, sha=self.sha))
        self.assertTrue(target_update_allowed(self.previous, deepcopy(self.target), generation=3, sha=self.sha))

    def test_changed_contract_requires_equal_or_newer_actual_proof(self):
        candidate = {**self.target, "content_path": "content/stories"}
        self.assertTrue(target_update_allowed(self.previous, candidate, generation=3, sha=self.sha))
        candidate["verification"] = {**self.target["verification"], "checked_at": (self.now - timedelta(days=1)).isoformat()}
        self.assertFalse(target_update_allowed(self.previous, candidate, generation=3, sha=self.sha))

    def test_generation_collision_fails_closed_until_specific_migration_approved(self):
        with self.assertRaises(WebsiteAuthorityError) as error:
            target_update_allowed(self.previous, self.target, generation=4, sha=self.sha)
        self.assertEqual(error.exception.code, "website_target_generation_migration_required")

    def test_approved_migration_and_model_scope_the_unique_key_by_generation(self):
        import importlib
        from .website_models import WebsiteConnectionTarget
        migration = importlib.import_module("content_factory.migrations.0044_website_target_generation_key").Migration
        self.assertEqual(migration.dependencies, [("content_factory", "0043_merge_credentials_website")])
        constraint = next(row for row in WebsiteConnectionTarget._meta.constraints if row.name == "cf_web_target_generation_unique")
        self.assertEqual(tuple(constraint.fields), ("connection", "generation", "target_key"))
        self.assertEqual(migration.operations[0].constraint.fields, ("connection", "generation", "target_key"))
        self.assertEqual(migration.operations[1].name, "cf_web_target_key_unique")

    def test_generic_category_target_cannot_displace_certified_featured(self):
        connection = SimpleNamespace(generation=3, verified_sha=self.sha, targets=Mock())
        connection.targets.filter.return_value = [self.previous]
        config = SimpleNamespace(website_connection=connection, default_publish_target_id="featured", publish_targets=[self.target])
        generic = {"target_id": "category", "publish_capability": "direct", "route_template": "/{category}/{slug}"}
        result = protect_certified_config(config, {"publish_targets": [generic], "default_publish_target_id": "category"})
        self.assertEqual(result["default_publish_target_id"], "featured")
        self.assertIn(self.target, result["publish_targets"])

    def test_cleanup_never_deletes_original_file_or_later_customer_edits(self):
        original = {"path": "src/Stories.tsx", "kind": "setup_route", "ownership": "edited", "after_sha256": "owned"}
        created = {"path": "src/mlai.ts", "kind": "integration_config", "ownership": "created", "after_sha256": "owned"}
        result = cleanup_plan([original, created], {original["path"]: "owned", created["path"]: "customer-change"})
        self.assertEqual(result["deletions"], [])
        self.assertEqual({item["reason"] for item in result["conflicts"]}, {"shared_or_unproven_ownership", "modified_after_generation"})
        self.assertEqual(cleanup_plan([created], {created["path"]: "owned"})["deletions"], [created["path"]])

    def test_backend_template_pattern_matches_worker_contract_and_hides_envelopes(self):
        for title in ("EXISTING ARTIFACT", "BASE TEMPLATE", "CODEBASE CONTEXT", "ARTIFACT TO ADAPT", "REPOSITORY CONTEXT", "GENERATION INSTRUCTIONS", "UPDATE INSTRUCTIONS"):
            for heading in ("#", "###", "######"):
                body, verdict = safe_template_seed(f"{heading} {title}\nprivate old seed")
                self.assertIsNone(body)
                self.assertEqual(verdict["code"], "legacy_template_envelope")
        self.assertEqual(safe_template_seed("<article>Saved template</article>")[0], "<article>Saved template</article>")

    def test_completed_and_unversioned_terminal_runs_cannot_be_revived(self):
        for saved in ({}, {"generation": 2, "state_version": 8}):
            self.assertTrue(stale_execution_event(saved, {"generation": 3, "state_version": 9, "status": "running"}, saved_status="completed"))
            self.assertTrue(stale_execution_event(saved, {"status": "running"}, saved_status="cancelled"))

    def test_no_delivery_refund_predicate_keeps_saved_copy_charge(self):
        self.assertFalse(delivered_content(SimpleNamespace(result={"failure": {"code": "EDITORIAL_REJECTED"}}, acceptance_summary={})))
        self.assertTrue(delivered_content(SimpleNamespace(result={"content_package": {"content_packaged": True}}, acceptance_summary={})))
        self.assertTrue(delivered_content(SimpleNamespace(result={"markdown": "Saved reviewable draft"}, acceptance_summary={})))

    def test_worker_snapshots_cannot_replace_backend_approval_or_image_receipt(self):
        existing = {"article_review_approval": {"revision": "reviewed"}, "approval_blocker": {"code": "current"}, "article_image_billing": {"charge": 1}}
        self.assertEqual(service_views._merge_django_owned_run_result(existing, {"article_review_approval": {"revision": "old"}}), existing)
        original = {"article_publish_approval_receipt": {"revision": "reviewed"}, "roo_points_billing_status": "charged", "keyword": "original"}
        self.assertEqual(service_views._merge_django_owned_run_request(original, {"keyword": "changed"})["article_publish_approval_receipt"], original["article_publish_approval_receipt"])
        with self.assertRaises(ValueError):
            service_views._merge_django_owned_run_request(original, {"article_publish_approval_receipt": {"revision": "old"}})

    def test_wrong_repository_old_draft_cannot_create_a_publish_child(self):
        connection = SimpleNamespace(pk=uuid.uuid4(), generation=3, github_repo="current/site", repository_id=42)
        config = SimpleNamespace(website_connection=connection)
        source = SimpleNamespace(run_request={"website_connection_id": str(connection.pk), "connection_generation": 2}, github_repo="older/site")
        with self.assertRaises(WebsiteAuthorityError) as error:
            publish_child_binding(config, source, {}, {})
        self.assertEqual(error.exception.code, "website_repository_changed")


class IncidentFacadeReplays(SimpleTestCase):
    def test_wrong_repository_draft_is_denied_before_publish_dispatch_or_pending_handoff(self):
        from . import vibe_marketing_views as views
        connection = SimpleNamespace(pk=uuid.uuid4(), generation=3, github_repo="current/site", repository_id=42)
        config = SimpleNamespace(website_connection=connection)
        run = SimpleNamespace(run_id="old-draft", workflow="article_generation", run_request={"website_connection_id": str(connection.pk), "connection_generation": 2}, github_repo="older/site")
        context = SimpleNamespace(organization=object())
        request = SimpleNamespace(user=SimpleNamespace(pk=8), data={})
        with patch.object(views, "_resolve_context_or_response", return_value=(context, None)), \
                patch.object(views, "get_object_or_404", return_value=run), \
                patch.object(views, "_run_belongs_to_context", return_value=True), \
                patch.object(views, "_setup_blocked_response_for_generation", return_value=None), \
                patch.object(views, "_get_config", return_value=config), \
                patch.object(views, "founder_actor_id_for_user", return_value="founder"), \
                patch.object(views, "_call_content_factory_run_action") as dispatch, \
                patch.object(views, "_mark_publish_handoff_pending") as pending:
            response = views.VibeMarketingRunControlView.post.__wrapped__(views.VibeMarketingRunControlView(), request, run.run_id, "publish-pr")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "website_repository_changed")
        dispatch.assert_not_called()
        pending.assert_not_called()

    def test_atomic_text_batch_is_forwarded_without_changing_identifiers(self):
        run = SimpleNamespace(workflow="article_generation", run_id="draft-1")
        payload = {"action": "editTextBatch", "expectedRevision": "saved", "operationId": "op-1", "edits": [{"fieldId": "paragraph.0", "value": "Exact copy"}]}
        view = review.VibeMarketingArticleReviewView()
        view._resolve_run = Mock(return_value=(object(), run, None))
        with patch.object(review.views, "_run_has_external_publish_evidence", return_value=False), \
                patch.object(review.views, "_latest_review_ready_component_revision", return_value=None), \
                patch.object(review, "remote_review", return_value={"revision": "next"}) as remote:
            result = view.post(SimpleNamespace(data=payload), run.run_id)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(remote.call_args.kwargs["payload"], payload)

    def test_review_failure_keeps_typed_code_and_action(self):
        run = SimpleNamespace(run_id="draft-1", run_request={"delivery_mode": "content_only"})
        remote = SimpleNamespace(status_code=422, json=lambda: {"detail": "Saved export rejected.", "code": "EXPORT_EDITORIAL_REJECTED", "next_action": "edit_section", "retryable": False})
        with patch.object(review.views, "_content_factory_remote_config", return_value={"enabled": True, "base_url": "https://synthetic.test"}), \
                patch.object(review.views, "_content_factory_headers", return_value={}), \
                patch.object(review.views.http_client, "request", return_value=remote):
            response = review.remote_review(run, payload={"action": "refresh"})
        self.assertEqual(response.data["code"], "EXPORT_EDITORIAL_REJECTED")
        self.assertEqual(response.data["next_action"], "edit_section")
        self.assertFalse(response.data["retryable"])

    def test_portable_preview_grant_never_enters_repository_guard(self):
        request = {"delivery_mode": "content_only", "delivery_mode_confirmed": True}
        run = SimpleNamespace(run_id="draft-1", run_request=request, domain="synthetic.test", organization_id=4)
        intent = evidence_digest({key: request.get(key) for key in ("delivery_mode", "delivery_mode_confirmed", "source_run_id")})
        token = signing.dumps({"run": run.run_id, "organization": 4, "domain": run.domain, "portable": True, "intent_digest": intent}, salt=SALT)
        view = ArticlePreviewLeaseProxyView(); view.kwargs = {"token": token}
        with patch("content_factory.article_preview_lease.views.get_object_or_404", return_value=run), \
                patch("content_factory.article_preview_lease.authority_guard", side_effect=AssertionError("Portable preview needs no repository")):
            self.assertIsNone(view._resolve_run(RequestFactory().get('/preview'), run.run_id)[2])
            run.run_request = {**request, "delivery_mode": "publish_code"}
            self.assertEqual(view._resolve_run(RequestFactory().get('/preview'), run.run_id)[2].status_code, 409)

    @override_settings(CONTENT_FACTORY_IMAGE_REGENERATION_COST_POINTS=None)
    def test_image_quote_is_explicit_and_unconfigured_price_blocks_before_ledger(self):
        self.assertIsNone(image_regeneration_cost("paid.example"))
        self.assertEqual(image_regeneration_cost("mlai.au"), 0)
        receipt, error = reserve_image_charge(user=object(), context=SimpleNamespace(organization=SimpleNamespace(domain="paid.example")), run=object(), payload={})
        self.assertIsNone(receipt)
        self.assertEqual(error.data["code"], "image_regeneration_quote_unavailable")


class IncidentIntegratedReplays(SimpleTestCase):
    def test_repository_probe_reads_grants_and_reuses_one_read_token_per_request(self):
        from . import vibe_marketing_views as views
        from integrations.services import github_app
        connection = SimpleNamespace(pk=uuid.uuid4(), generation=3, repository_id=42, installation_id="45",
            state="connected", configuration_version=5, verified_sha="a" * 40)
        config = SimpleNamespace(github_repo="synthetic/site", website_connection=connection, connected_slack_user_id="founder",
            github_installation_id="45", github_token_encrypted="", github_token_expires_at=None)
        context = SimpleNamespace(profile=SimpleNamespace(user=SimpleNamespace(pk=8)), organization=SimpleNamespace(pk=4))
        responses = [SimpleNamespace(status_code=200, json=lambda: {"full_name": "synthetic/site", "default_branch": "main"}),
            SimpleNamespace(status_code=200, json=lambda: {"sha": "a" * 40})]
        with patch.object(views, "_github_account_for_context", return_value={"owned": True}), \
                patch.object(github_app, "create_installation_access_token", return_value=SimpleNamespace(token="synthetic-read-token")) as mint, \
                patch.object(github_app, "require_installation_repository_permissions", return_value={"contents": "write", "pull_requests": "write"}) as grants, \
                patch.object(views.http_client, "get", side_effect=responses) as get, \
                patch.object(views.http_client, "delete") as revoke:
            first = views._verify_github_repository_access(context, config, force=True)
            self.assertEqual(views._verify_github_repository_access(context, config, force=True), first)
        self.assertTrue(first["writable"])
        mint.assert_called_once()
        self.assertEqual(mint.call_args.kwargs["permission_mode"], "read")
        grants.assert_called_once()
        self.assertEqual(get.call_count, 2)
        revoke.assert_called_once()

    def test_current_denied_terminal_callback_records_failure_but_cannot_revive_completed_run(self):
        from . import website_connections as authority
        from workflow_runs.models import ContentFactoryRun
        from organizations.models import Organization
        binding = {"website_connection_id": str(uuid.uuid4()), "connection_generation": 3, "repository_id": 42}
        run = SimpleNamespace(pk=1, run_id="current-run", organization_id=4, status="running",
            run_request=binding, result={"generation": 1, "state_version": 4}, save=Mock())
        website = SimpleNamespace(generation=3, state="connected")
        payload = {**binding, "run_id": run.run_id, "event_type": "generation_failed", "generation": 1, "state_version": 5,
            "failure": {"code": "SOURCE_CHANGED", "retryable": False}, "error": "Repository source changed"}
        with patch.object(authority.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(ContentFactoryRun.objects, "filter") as selected, \
                patch.object(ContentFactoryRun.objects, "select_for_update") as locked, \
                patch.object(Organization.objects, "select_for_update"), \
                patch.object(authority.WebsiteConnection.objects, "select_for_update") as connections, \
                patch("content_factory.website_operations.validate_operation"):
            selected.return_value.first.return_value = run
            locked.return_value.get.return_value = run
            connections.return_value.filter.return_value.first.return_value = website
            denial = WebsiteAuthorityError("website_source_changed", "Rescan the changed source.")
            self.assertTrue(authority.record_denied_terminal_callback(payload, denial))
            self.assertEqual(run.status, "failed")
            self.assertEqual(run.result["failure"]["code"], "SOURCE_CHANGED")
            self.assertFalse(run.resume_available)
            run.status = "completed"
            self.assertFalse(authority.record_denied_terminal_callback(payload, denial))
            run.status = "running"
            self.assertFalse(authority.record_denied_terminal_callback({**payload, "connection_generation": 2}, denial))

    def test_terminal_operation_rejects_statusless_progress_callback(self):
        from .website_operations import validate_operation
        op = SimpleNamespace(pk=uuid.uuid4(), state="completed", payload={"attempt": 2}, generation=3)
        operations = Mock(); operations.filter.return_value.first.return_value = op
        connection = SimpleNamespace(operations=operations, blockers=[], generation=3)
        payload = {"operation_id": str(op.pk), "operation_attempt": 2, "deletion_epoch": 0}
        with self.assertRaises(WebsiteAuthorityError) as error:
            validate_operation(connection, {**payload, "event_type": "article_progress"})
        self.assertEqual(error.exception.code, "website_operation_terminal")
        self.assertIs(validate_operation(connection, {**payload, "event_type": "article_complete"}), op)
        self.assertIs(validate_operation(connection, payload), op)

    def test_new_resume_attempt_acknowledges_failure_then_next_resume_advances_again(self):
        from . import website_operations as operations
        from . import website_connections as authority
        identifier, connection_id = uuid.uuid4(), uuid.uuid4()
        website = SimpleNamespace(pk=connection_id, generation=3, repository_id=42, github_repo="synthetic/site", branch="main", app_root="", blockers=[], organization=SimpleNamespace(domain="synthetic.test"))
        binding = {"website_connection_id": str(connection_id), "connection_generation": 3, "operation_id": str(identifier), "operation_attempt": 1}
        run = SimpleNamespace(run_id="setup-run", status="failed", run_request=binding, result={"generation": 1, "state_version": 10}, save=Mock())
        op = SimpleNamespace(pk=identifier, connection_id=connection_id, generation=3, state="failed", payload={"attempt": 1, "run_id": run.run_id}, receipt={}, save=Mock())
        with patch.object(authority, "authority_guard", side_effect=lambda *args, **kwargs: nullcontext(website)), \
                patch.object(authority, "extend_owner_operation_contract"), \
                patch.object(operations.WebsiteConnectionOperation.objects, "select_for_update") as selected:
            selected.return_value.get.return_value = op
            selected.return_value.filter.return_value.first.return_value = op
            self.assertEqual(operations.advance_workflow_attempt(run)["operation_attempt"], 2)
            self.assertEqual(operations.advance_workflow_attempt(run)["operation_attempt"], 2)
            operations.observe_workflow_status(run, {"status": "failed"})
            self.assertTrue(op.payload["resume_pending"])
            run.result = {"generation": 2, "state_version": 11}
            operations.observe_workflow_status(run, {"operation_attempt": 2})
            self.assertFalse(op.payload["resume_pending"])
            self.assertEqual(op.state, "failed")
            self.assertEqual(operations.advance_workflow_attempt(run)["operation_attempt"], 3)

    def test_slack_projection_cannot_treat_legacy_scaffold_flag_as_verified(self):
        from integrations import api_views
        config = SimpleNamespace(organization=SimpleNamespace(domain="synthetic.test"), github_repo="synthetic/site",
            article_system={}, publish_targets=[], default_publish_target_id=None, scan_summary="Inventory",
            articles_scaffolded=True, last_scanned_at=None, last_scanned_sha="a" * 40)
        with patch("content_factory.activation.integration_evidence", return_value={"verified": False}), \
                patch.object(api_views, "_derive_connection_state", return_value="connected"):
            projected = api_views._serialize_connected_domain(config)
        self.assertFalse(projected["articles_scaffolded"])
        self.assertFalse(projected["can_generate_article"])
        self.assertTrue(projected["can_generate_portable_draft"])

    def test_current_scan_and_setup_survive_busy_article_history_window(self):
        from . import vibe_marketing_views as views
        class Rows(list):
            def exclude(self, **kwargs): return self
            def prefetch_related(self, *args): return self
            def order_by(self, *args): return self
            def filter(self, **kwargs): return Rows(row for row in self if row.workflow in kwargs["workflow__in"])
        now = timezone.now()
        rows = Rows(SimpleNamespace(run_id=f"draft-{i}", workflow="direct_generate", updated_at=now - timedelta(minutes=i)) for i in range(60))
        scan = SimpleNamespace(run_id="current-scan", workflow="repo_scan", updated_at=now - timedelta(hours=2))
        setup = SimpleNamespace(run_id="current-setup", workflow="article_system_setup", updated_at=now - timedelta(hours=3))
        rows.extend([scan, setup])
        with patch.object(views.ContentFactoryRun.objects, "filter", return_value=rows), \
                patch.object(views, "_get_config", return_value=SimpleNamespace(website_connection=None)), \
                patch.object(views, "article_setup_reset_ignores_run", return_value=False):
            result = views._latest_runs_for_org(SimpleNamespace(domain="synthetic.test"), limit=6)
        self.assertEqual(len(result), 6)
        self.assertIn(scan, result)
        self.assertIn(setup, result)

    def test_scan_write_preserves_exact_accepted_proof_and_verification_time(self):
        from . import website_connections as authority
        sha, accepted_at = "a" * 40, timezone.now() - timedelta(days=2)
        previous = SimpleNamespace(generation=3, source_sha=sha, verified_at=accepted_at,
            target_key="featured", adapter="react_component", capabilities={"publishingReady": True},
            contract={"verification": {"status": "verified", "preview_capable": True, "source_sha": sha}})
        targets = Mock()
        targets.filter.side_effect = lambda **kwargs: SimpleNamespace(first=lambda: previous) if "target_key" in kwargs else [previous]
        connection = SimpleNamespace(pk=uuid.uuid4(), generation=3, verified_sha=sha, last_verified_at=accepted_at,
            targets=targets, capabilities={"publishingReady": True}, blockers=[], state="connected", save=Mock())
        config = SimpleNamespace(article_template="<article>Template</article>", design_guide="Native classes")
        with patch.object(authority, "_authority_depth") as depth, \
                patch.object(authority, "_verified_heads") as heads, \
                patch.object(authority.WebsiteScanSnapshot.objects, "get_or_create"), \
                patch.object(authority.WebsiteConnectionTarget.objects, "update_or_create") as upsert, \
                patch.object(authority.OrganizationContentConfig.objects, "get", return_value=config), \
                patch.object(authority, "check_source_identity"):
            depth.get.return_value = 1
            heads.get.return_value = {(str(connection.pk), 3, sha)}
            authority.record_scan_evidence(connection, {"source_sha": sha, "publish_targets": [{"target_id": "featured", "delivery_adapter": "bundle_only"}]})
        upsert.assert_not_called()
        self.assertTrue(connection.capabilities["publishingReady"])
        self.assertTrue(connection.capabilities["templatesValid"])
        self.assertEqual(connection.last_verified_at, accepted_at)
        self.assertEqual(connection.verified_sha, sha)

    def push(self, *, owned):
        from . import website_reconciliation as reconciliation
        from workflow_runs.models import ContentFactoryRun
        from organizations.models import Organization
        website = SimpleNamespace(pk=uuid.uuid4(), generation=3, branch="main", github_repo="synthetic/site", verified_sha="a" * 40,
            repository_id=42, state="connected", installation_id="45", organization=SimpleNamespace(domain="synthetic.test"),
            capabilities={"publishingReady": True, "previewSupported": True}, configuration_version=5, blockers=[], save=Mock())
        config = SimpleNamespace(website_connection=website, organization_id=4, default_publish_target_id="featured")
        source = SimpleNamespace(run_id="owned-merge") if owned else None
        with patch.object(reconciliation.OrganizationContentConfig.objects, "filter") as configs, \
                patch.object(reconciliation.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(Organization.objects, "select_for_update") as org, \
                patch.object(reconciliation.WebsiteConnection.objects, "select_for_update") as connections, \
                patch.object(reconciliation.WebsiteScanSnapshot.objects, "get_or_create", return_value=(object(), True)), \
                patch.object(ContentFactoryRun.objects, "filter") as runs, \
                patch.object(reconciliation.WebsiteConnectionOperation.objects, "get_or_create") as operation:
            configs.return_value.select_related.return_value = [config]
            connections.return_value.get.return_value = website
            runs.return_value.filter.return_value.first.return_value = source
            result = reconciliation.handle_website_github_event("push", {"repository": {"id": 42}, "after": "b" * 40, "ref": "refs/heads/main"})
        return website, operation.call_args.kwargs["defaults"], result

    def test_owned_article_merge_preserves_readiness_and_queues_live_verification(self):
        website, queued, result = self.push(owned=True)
        self.assertTrue(website.capabilities["publishingReady"])
        self.assertEqual(queued["action"], "source-reverify")
        self.assertFalse(queued["payload"]["scan_required"])
        self.assertEqual(queued["payload"]["owned_merge_run_id"], "owned-merge")

    def test_external_source_change_has_specific_blocker_and_queues_rescan(self):
        website, queued, result = self.push(owned=False)
        self.assertFalse(website.capabilities["publishingReady"])
        self.assertEqual(website.blockers[0]["code"], "repository_source_changed")
        self.assertTrue(queued["payload"]["scan_required"])

    def test_owned_merge_reconciliation_executes_ci_and_deployment_verification(self):
        from . import website_reconciliation as reconciliation
        from . import website_connections as authority
        from . import website_verification as verification
        website = SimpleNamespace(pk=uuid.uuid4(), generation=3, github_repo="synthetic/site", repository_id=42, app_root="", branch="main",
            organization=SimpleNamespace(domain="synthetic.test"), targets=Mock())
        target = SimpleNamespace(target_key="featured")
        website.targets.filter.return_value.first.return_value = target
        op = SimpleNamespace(pk=uuid.uuid4(), connection=website, payload={"source_sha": "b" * 40, "target_id": "featured", "scan_required": False},
            attempts=0, next_attempt_at=None, receipt={}, save=Mock())
        proof = {"source_sha": "b" * 40, "provider_verified": True}
        with patch.object(reconciliation.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(reconciliation.WebsiteConnectionOperation.objects, "select_for_update") as claimed, \
                patch.object(reconciliation.WebsiteConnectionOperation.objects, "filter") as update, \
                patch.object(authority, "authority_guard", side_effect=lambda *a, **kw: nullcontext(website)), \
                patch.object(verification, "discover_source_attestation", return_value=proof) as discover, \
                patch.object(verification, "record_ci_attestation", return_value=SimpleNamespace(pk=uuid.uuid4(), receipt=proof)) as ci, \
                patch.object(verification, "verify_live_deployment", return_value=SimpleNamespace(pk=uuid.uuid4())) as deployment, \
                patch.object(reconciliation.OrganizationContentConfig.objects, "get", return_value=object()), \
                patch("integrations.http_client.post", side_effect=AssertionError("Owned merge requires no fresh scan")):
            claimed.return_value.select_related.return_value.filter.return_value.first.return_value = op
            result = reconciliation._process_source_reverification(op.pk, timezone.now())
        self.assertEqual(result, "completed")
        ci.assert_called_once_with(proof)
        deployment.assert_called_once()
        self.assertEqual(update.return_value.update.call_args.kwargs["state"], "completed")

    @override_settings(WEBSITE_CONNECTION_WRITE_MODE="enabled")
    def test_owned_merge_live_receipt_allows_second_article_admission_without_rescan(self):
        from . import website_connections as authority, website_reconciliation as reconciliation, website_verification as verification
        from . import vibe_marketing_views as views, activation
        from integrations.services import article_generation as generation
        website, queued, _ = self.push(owned=True)
        now, digest, contract_digest = timezone.now(), "d" * 64, "c" * 64
        website.organization = SimpleNamespace(domain="mlai.au", id=4)
        website.state, website.repository_id, website.app_root, website.installation_id = "connected", 42, "", "45"
        website.last_verified_at = now
        target = SimpleNamespace(target_key="featured", adapter="react_component", source_sha="a" * 40, verified_at=now,
            contract={"contract_digest": contract_digest, "route_path": "/articles", "route_template": "/articles/{slug}",
                "live_marker": {"kind": "artifact_digest", "value": digest}}, capabilities={"publishingReady": True},
            save=Mock(), refresh_from_db=Mock())
        class Result:
            def __init__(self, row): self.row = row
            def filter(self, **kwargs): return self
            def order_by(self, *args): return self
            def first(self): return self.row
        website.targets = SimpleNamespace(filter=lambda **kwargs: Result(target))
        website.repository_mutations = Mock()
        website.repository_mutations.filter.return_value.exclude.return_value.values_list.return_value = ["a" * 40]
        origin = SimpleNamespace(pk=uuid.uuid4(), payload={"run_id": "owned-setup", "attempt": 1})
        stored = {}
        website.operations = SimpleNamespace(filter=lambda **kwargs: Result(origin if "pk" in kwargs else stored.get(kwargs.get("action"))))
        provider = {**authority.contract_for(website), "operation_id": str(origin.pk), "operation_attempt": 1, "deletion_epoch": 0,
            "source_sha": "b" * 40, "target_id": "featured", "contract_digest": contract_digest, "adapter_id": "react_component",
            "adapter_version": "1", "source_tree_sha": "e" * 40, "environment_fingerprint": "synthetic-ci", "lockfile_digests": {},
            "baseline_build": "passed", "patched_build": "passed", "artifact_digest": digest,
            "verified_routes": {"listing": "/articles", "detail": "/articles/first-article", "unknown_slug": "/articles/not-an-article"},
            **{field: True for field in verification.BOOLEAN_PROOFS}}
        provider["evidence_digest"] = evidence_digest(provider)
        config = SimpleNamespace(organization=website.organization, organization_id=4, website_connection=website, website_connection_id=website.pk,
            github_repo=website.github_repo, default_publish_target_id="featured", publish_targets=[target.contract], article_system={}, save=Mock())
        op = SimpleNamespace(pk=uuid.uuid4(), connection=website, generation=3, payload=queued["payload"], attempts=0,
            next_attempt_at=None, receipt={}, save=Mock())
        def save_operation(**kwargs):
            row = SimpleNamespace(pk=uuid.uuid4(), **kwargs["defaults"])
            stored[row.action] = row
            return row, True
        with patch.object(reconciliation.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(reconciliation.WebsiteConnectionOperation.objects, "select_for_update") as claimed, \
                patch.object(reconciliation.WebsiteConnectionOperation.objects, "filter"), \
                patch.object(verification.WebsiteConnectionOperation.objects, "update_or_create", side_effect=save_operation), \
                patch.object(authority, "authority_guard", side_effect=lambda *args, **kwargs: nullcontext(website)), \
                patch.object(verification, "authority_guard", side_effect=lambda *args, **kwargs: nullcontext(website)), \
                patch.object(authority, "verify_repository_head"), \
                patch.object(verification, "verify_reviewed_source_lineage"), \
                patch.object(verification, "discover_source_attestation", return_value={**provider, "run_id": "owned-setup"}), \
                patch.object(verification, "read_ci_proof", side_effect=lambda site, binding: verification.validated_ci_identity(binding, provider)), \
                patch("content_factory.website_live_fetch.fetch_live_route", return_value=(b"", {"x-mlai-artifact-digest": digest})), \
                patch.object(reconciliation.OrganizationContentConfig.objects, "get", return_value=config), \
                patch("integrations.http_client.post", side_effect=AssertionError("Owned merge must not rescan")), \
                patch.object(activation, "founder_context_for_domain", return_value=SimpleNamespace(organization=website.organization)), \
                patch.object(views, "_get_config", return_value=config), \
                patch.object(views, "_latest_runs_for_org", return_value=[]), \
                patch.object(views, "_github_account_for_context", return_value={"saved": True, "owned": True, "verified": True}), \
                patch.object(views, "_article_system_setup_gate", return_value={}), \
                patch.object(views, "_verify_github_repository_access", return_value={"verified": True, "writable": True, "branch": "main", "sha": "b" * 40}), \
                patch.object(generation, "_validate_authenticated_content_factory_actor"):
            claimed.return_value.select_related.return_value.filter.return_value.first.return_value = op
            self.assertEqual(reconciliation._process_source_reverification(op.pk, now), "completed")
            self.assertEqual(website.verified_sha, "b" * 40)
            self.assertEqual(stored["deployment-verify"].receipt["status"], "passed")
            self.assertIs(generation.require_article_activation(domain="mlai.au", actor_id="mlai_user:8", user=SimpleNamespace(pk=8),
                expected_repo=website.github_repo, article_request={"topic": "Second article"}), config)


class OwnedMergeWebhookRaceTests(SimpleTestCase):
    def setUp(self):
        from .website_connections import contract_for
        self.website = SimpleNamespace(pk=uuid.uuid4(), organization_id=4, generation=3,
            repository_id=42, github_repo="synthetic/site", branch="main", app_root="", state="connected",
            installation_id="45", verified_sha="a" * 40, configuration_version=5,
            organization=SimpleNamespace(domain="synthetic.test"), targets=Mock(),
            capabilities={"publishingReady": True, "previewSupported": True}, blockers=[], save=Mock())
        self.config = SimpleNamespace(website_connection=self.website, organization_id=4,
            github_repo=self.website.github_repo, default_publish_target_id="featured")
        self.run = SimpleNamespace(pk=1, run_id="owned-publication", organization_id=4,
            github_repo=self.website.github_repo, workflow="publish_article", status="running", result={},
            run_request=contract_for(self.website), save=Mock())
        repo = {"id": 42, "full_name": self.website.github_repo}
        self.pull = {"number": 17, "merged": True, "merge_commit_sha": "b" * 40,
            "head": {"sha": "c" * 40, "ref": "mlai/article", "repo": repo},
            "base": {"sha": "a" * 40, "ref": "main", "repo": repo}}
        self.intent = {**contract_for(self.website), "run_id": self.run.run_id, "pr_number": 17,
            "source_sha": "a" * 40, "base_branch": "main", "head_sha": "c" * 40, "head_branch": "mlai/article"}

    def _rows(self):
        run = self.run
        class Rows(list):
            def filter(self, **kwargs):
                if "result__merge_status" in kwargs:
                    return Rows([run] if run.result.get("merge_status") == "merged"
                        and (run.result.get("merge_response") or {}).get("sha") == kwargs["result__merge_response__sha"] else [])
                return Rows([run] if run.result.get("publish_merge_intent") else [])
            def first(self): return self[0] if self else None
            def order_by(self, *args): return self
        return Rows([run])

    def _patches(self, stack, *, pull=None):
        from . import website_reconciliation as reconciliation, website_connections as authority
        from workflow_runs.models import ContentFactoryRun
        from organizations.models import Organization
        from integrations.services import github_app
        stack.enter_context(patch.object(reconciliation.transaction, "atomic", side_effect=lambda: nullcontext()))
        stack.enter_context(patch.object(authority, "authority_guard", side_effect=lambda *a, **kw: nullcontext(self.website)))
        stack.enter_context(patch.object(Organization.objects, "select_for_update"))
        connections = stack.enter_context(patch.object(reconciliation.WebsiteConnection.objects, "select_for_update"))
        connections.return_value.get.return_value = self.website
        selected = stack.enter_context(patch.object(ContentFactoryRun.objects, "select_for_update"))
        selected.return_value.get.return_value = self.run
        stack.enter_context(patch.object(ContentFactoryRun.objects, "filter", side_effect=lambda **kw: self._rows()))
        configs = stack.enter_context(patch.object(reconciliation.OrganizationContentConfig.objects, "filter"))
        configs.return_value.select_related.return_value = [self.config]
        stack.enter_context(patch.object(reconciliation.WebsiteScanSnapshot.objects, "get_or_create", return_value=(object(), True)))
        queued = stack.enter_context(patch.object(reconciliation.WebsiteConnectionOperation.objects, "get_or_create"))
        mint = stack.enter_context(patch.object(github_app, "create_installation_access_token", return_value=SimpleNamespace(token="synthetic-read-token")))
        get = stack.enter_context(patch("integrations.http_client.get", return_value=SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: pull if pull is not None else self.pull)))
        stack.enter_context(patch("integrations.http_client.delete"))
        return queued, mint, get

    def _push(self):
        from .website_reconciliation import handle_website_github_event
        return handle_website_github_event("push", {"repository": {"id": 42}, "ref": "refs/heads/main", "after": "b" * 40})

    def test_push_before_merge_response_uses_actual_saved_intent_and_authenticated_pr(self):
        from . import vibe_marketing_views as views, website_connections as authority
        with ExitStack() as stack:
            queued, mint, get = self._patches(stack)
            stack.enter_context(patch.object(views, "_get_config", return_value=self.config))
            stack.enter_context(patch.object(views, "_setup_blocked_response_for_generation", return_value=None))
            stack.enter_context(patch.object(views, "_pull_request_number_from_run", return_value=17))
            stack.enter_context(patch.object(views, "_github_token_for_repo_operation", return_value=("synthetic-write-token", "fixture")))
            stack.enter_context(patch.object(views, "_github_pull_checks_state", return_value=({**self.pull, "merged": False}, {"ready": True})))
            stack.enter_context(patch.object(authority, "validate_publish_merge_source"))
            def merge_request(*args, **kwargs):
                self.assertEqual(self.run.result["publish_merge_intent"]["head_sha"], "c" * 40)
                self.assertNotIn("merge_response", self.run.result)
                self._push()
                return {"merged": True, "sha": "b" * 40}
            stack.enter_context(patch.object(views, "_github_api_request", side_effect=merge_request))
            result = views._check_and_merge_publish_pr(run=self.run, context=SimpleNamespace(organization=self.website.organization))
        self.assertEqual(result["outcome"], "merged")
        self.assertTrue(self.website.capabilities["publishingReady"])
        self.assertFalse(queued.call_args.kwargs["defaults"]["payload"]["scan_required"])
        self.assertEqual(queued.call_args.kwargs["defaults"]["payload"]["owned_merge_run_id"], self.run.run_id)
        get.assert_called_once()
        self.assertEqual(mint.call_args.kwargs["permission_mode"], "read")
        self.assertEqual(service_views._merge_django_owned_run_result(self.run.result,
            {"publish_merge_intent": {"head_sha": "untrusted"}})["publish_merge_intent"]["head_sha"], "c" * 40)
        self.assertEqual(service_views._merge_django_owned_run_result({},
            {"publish_merge_intent": self.intent, "merge_response": {"sha": "b" * 40}, "merge_status": "merged"}), {})
        self.assertEqual(service_views._merge_django_owned_run_result(self.run.result,
            {"merge_response": {"sha": "untrusted"}, "merge_status": "untrusted"})["merge_response"]["sha"], "b" * 40)
        self.assertEqual(views._merge_django_owned_article_result(self.run.result,
            {"publish_merge_intent": {"head_sha": "untrusted"}, "merge_response": {"sha": "untrusted"}})["publish_merge_intent"]["head_sha"], "c" * 40)
        self.assertEqual(views._merge_django_owned_article_result({},
            {"publish_merge_intent": self.intent, "merge_status": "merged"}), {})

    def test_push_after_saved_merge_receipt_needs_no_extra_github_read(self):
        self.run.result = {"merge_status": "merged", "merge_response": {"sha": "b" * 40}}
        with ExitStack() as stack:
            queued, mint, get = self._patches(stack)
            self._push()
        self.assertTrue(self.website.capabilities["publishingReady"])
        self.assertFalse(queued.call_args.kwargs["defaults"]["payload"]["scan_required"])
        mint.assert_not_called()
        get.assert_not_called()

    def test_initial_dispatch_projection_cannot_introduce_worker_merge_evidence(self):
        from . import vibe_marketing_views as views
        for workflow in ("publish_article", "article_system_setup"):
            with self.subTest(workflow=workflow), patch.object(views.ContentFactoryRun.objects, "get_or_create", return_value=(self.run, True)) as create, \
                    patch.object(views, "_persist_web_article_billing_to_job"), patch.object(views, "_merge_job_billing_into_run_request"):
                views._create_local_run_authorized(workflow=workflow, domain="synthetic.test", remote_data={
                    "run_id": self.run.run_id, "result": {"publish_merge_intent": self.intent,
                        "merge_status": "merged", "merge_response": {"sha": "b" * 40}, "title": "Worker copy"}})
            self.assertEqual(create.call_args.kwargs["defaults"]["result"], {"title": "Worker copy"})

    def test_setup_merge_also_records_verified_intent_before_early_webhook(self):
        from . import vibe_marketing_views as views, website_connections as authority
        self.run.workflow = "article_system_setup"
        with ExitStack() as stack:
            queued, _, get = self._patches(stack)
            stack.enter_context(patch.object(views, "_get_config", return_value=self.config))
            stack.enter_context(patch.object(views, "_pull_request_number_from_run", return_value=17))
            stack.enter_context(patch.object(views, "_github_token_for_repo_operation", return_value=("synthetic-write-token", "fixture")))
            stack.enter_context(patch.object(views, "_github_pull_checks_state_lenient", return_value=({**self.pull, "merged": False}, {"state": "passed"})))
            verified = stack.enter_context(patch.object(authority, "validate_setup_merge_source"))
            stack.enter_context(patch.object(views, "_apply_setup_merge_result", return_value=self.run))
            def merge_request(*args, **kwargs):
                self.assertEqual(self.run.result["publish_merge_intent"]["head_sha"], "c" * 40)
                self._push()
                return {"merged": True, "sha": "b" * 40}
            stack.enter_context(patch.object(views, "_github_api_request", side_effect=merge_request))
            result = views._attempt_setup_publish_merge_authorized(run=self.run, context=SimpleNamespace(organization=self.website.organization))
        self.assertEqual(result["outcome"], "merged")
        verified.assert_called_once_with(self.run, "c" * 40)
        self.assertFalse(queued.call_args.kwargs["defaults"]["payload"]["scan_required"])
        get.assert_called_once()

    def test_unrelated_or_changed_pr_remains_external_and_requires_scan(self):
        invalid_pulls = []
        for key, value in (("merge_commit_sha", "d" * 40), ("number", 18), ("merged", False)):
            invalid_pulls.append({**deepcopy(self.pull), key: value})
        for side, field, value in (("head", "sha", "d" * 40), ("head", "ref", "customer-branch"), ("base", "ref", "other-branch")):
            candidate = deepcopy(self.pull); candidate[side][field] = value; invalid_pulls.append(candidate)
        for side in ("head", "base"):
            candidate = deepcopy(self.pull); candidate[side]["repo"]["id"] = 999; invalid_pulls.append(candidate)
        for index, pull in enumerate(invalid_pulls):
            with self.subTest(case=index), ExitStack() as stack:
                self.website.capabilities = {"publishingReady": True, "previewSupported": True}
                self.run.result = {"publish_merge_intent": self.intent}
                queued, _, _ = self._patches(stack, pull=pull)
                self._push()
                self.assertFalse(self.website.capabilities["publishingReady"])
                self.assertTrue(queued.call_args.kwargs["defaults"]["payload"]["scan_required"])

    def test_rebind_between_provider_observation_and_persistence_cannot_preserve_readiness(self):
        from . import website_connections as authority
        self.run.result = {"publish_merge_intent": self.intent}
        with ExitStack() as stack:
            queued, _, _ = self._patches(stack)
            calls = 0
            def guard(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    self.website.generation += 1
                return nullcontext(self.website)
            stack.enter_context(patch.object(authority, "authority_guard", side_effect=guard))
            self._push()
        self.assertFalse(self.website.capabilities["publishingReady"])
        self.assertTrue(queued.call_args.kwargs["defaults"]["payload"]["scan_required"])

    def test_late_merge_receipt_is_rechecked_before_rescan_and_continues_ci_live_verification(self):
        from . import website_connections as authority, website_reconciliation as reconciliation, website_verification as verification
        self.run.result = {"merge_status": "merged", "merge_response": {"sha": "b" * 40}}
        self.website.capabilities["publishingReady"] = False
        self.website.targets.filter.return_value.first.return_value = SimpleNamespace(target_key="featured")
        op = SimpleNamespace(pk=uuid.uuid4(), connection=self.website, payload={"source_sha": "b" * 40,
            "target_id": "featured", "scan_required": True}, attempts=0, next_attempt_at=None, receipt={}, save=Mock())
        with ExitStack() as stack:
            self._patches(stack)
            claimed = stack.enter_context(patch.object(reconciliation.WebsiteConnectionOperation.objects, "select_for_update"))
            claimed.return_value.select_related.return_value.filter.return_value.first.return_value = op
            updated = stack.enter_context(patch.object(reconciliation.WebsiteConnectionOperation.objects, "filter"))
            updated.return_value.update.return_value = 1
            stack.enter_context(patch.object(reconciliation.OrganizationContentConfig.objects, "get", return_value=self.config))
            stack.enter_context(patch.object(verification, "discover_source_attestation", return_value={"source_sha": "b" * 40}))
            stack.enter_context(patch.object(verification, "record_ci_attestation", return_value=SimpleNamespace(pk=uuid.uuid4(), receipt={})))
            deployment = stack.enter_context(patch.object(verification, "verify_live_deployment", return_value=SimpleNamespace(pk=uuid.uuid4())))
            post = stack.enter_context(patch("integrations.http_client.post", side_effect=AssertionError("Late owned receipt must prevent rescan")))
            result = reconciliation._process_source_reverification(op.pk, timezone.now())
        self.assertEqual(result, "completed")
        self.assertFalse(op.payload["scan_required"])
        self.assertEqual(op.payload["owned_merge_run_id"], self.run.run_id)
        deployment.assert_called_once()
        post.assert_not_called()


class IncidentBillingReplays(SimpleTestCase):
    @override_settings(CONTENT_FACTORY_IMAGE_REGENERATION_COST_POINTS=3)
    def test_image_request_charges_once_rejects_drift_before_spend_and_refunds_definite_rejection(self):
        from . import article_review_billing as billing
        from roo.models import Ledger
        from roo.services import PointsService
        from .models import ContentFactoryJob
        from workflow_runs.models import ContentFactoryRun
        user = SimpleNamespace(pk=8)
        context = SimpleNamespace(organization=SimpleNamespace(id=4, domain="paid.example"))
        run = SimpleNamespace(pk=1, run_id="saved-draft", result={}, save=Mock())
        payload = {"action": "regenerateImage", "fieldId": "image.hero", "instruction": "Blue skyline", "operationId": "image-op", "expectedRevision": "saved", "expectedCostPoints": 3}
        with patch.object(billing.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(Ledger.objects, "filter") as ledgers, \
                patch.object(ContentFactoryRun.objects, "select_for_update") as selected, \
                patch.object(PointsService, "spend", return_value=(SimpleNamespace(id=9), True)) as spend, \
                patch.object(PointsService, "refund", return_value=(SimpleNamespace(id=10), True)) as refund:
            ledgers.return_value.exists.return_value = False
            selected.return_value.get.return_value = run
            receipt, error = billing.reserve_image_charge(user=user, context=context, run=run, payload=payload)
            self.assertIsNone(error)
            self.assertEqual(receipt["costPoints"], 3)
            self.assertIsNone(billing.reserve_image_charge(user=user, context=context, run=run, payload=payload)[1])
            _, error = billing.reserve_image_charge(user=SimpleNamespace(pk=99), context=context, run=run, payload=payload)
            self.assertEqual(error.data["code"], "image_operation_owner_conflict")
            changed, error = billing.reserve_image_charge(user=user, context=context, run=run, payload={**payload, "instruction": "Different image"})
            self.assertEqual(error.data["code"], "image_operation_conflict")
            spend.assert_called_once()
            billing.settle_image_charge(user=user, run=run, receipt=receipt, rejected=True)
            refund.assert_called_once()
            self.assertEqual(refund.call_args.kwargs["delta"], 3)
            billing.settle_image_charge(user=user, run=run, receipt=receipt)
            self.assertEqual(run.result["article_image_billing"][receipt["identity"]]["status"], "refunded")
            self.assertEqual(billing.reserve_image_charge(user=user, context=context, run=run, payload=payload)[1].data["code"], "roo_points_billing_refunded")

    @override_settings(CONTENT_FACTORY_IMAGE_REGENERATION_COST_POINTS=3)
    def test_shared_run_reader_refunds_the_original_image_payer(self):
        from . import article_review_billing as billing
        from django.contrib.auth import get_user_model
        from roo.services import PointsService
        from workflow_runs.models import ContentFactoryRun
        payer = SimpleNamespace(pk=8)
        reader = SimpleNamespace(pk=99)
        receipt = {"identity": "image-identity", "key": "image-key", "costPoints": 3,
            "userId": payer.pk, "status": "accepted"}
        run = SimpleNamespace(pk=1, result={"article_image_billing": {"image-identity": receipt}}, save=Mock())
        with patch.object(billing.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(ContentFactoryRun.objects, "select_for_update") as selected, \
                patch.object(get_user_model().objects, "filter") as users, \
                patch.object(PointsService, "refund") as refund:
            selected.return_value.get.return_value = run
            users.return_value.first.return_value = payer
            billing.settle_image_charge(user=reader, run=run, receipt=receipt, rejected=True)
            users.assert_called_once_with(pk=payer.pk)
            self.assertIs(refund.call_args.kwargs["user"], payer)
            billing.settle_image_charge(user=reader, run=run, receipt=receipt, rejected=True)
            refund.assert_called_once()

    @override_settings(CONTENT_FACTORY_IMAGE_REGENERATION_COST_POINTS=3)
    def test_queued_image_provider_failure_refunds_only_exact_failed_candidate(self):
        from . import article_review_billing as billing
        from roo.models import Ledger
        from roo.services import PointsService
        from workflow_runs.models import ContentFactoryRun
        user = SimpleNamespace(pk=8)
        context = SimpleNamespace(organization=SimpleNamespace(id=4, domain="paid.example"))
        run = SimpleNamespace(pk=1, run_id="saved-draft", result={}, save=Mock())
        payload = {"fieldId": "image.hero", "operationId": "async-image", "expectedRevision": "saved", "expectedCostPoints": 3}
        with patch.object(billing.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(Ledger.objects, "filter") as ledgers, \
                patch.object(ContentFactoryRun.objects, "select_for_update") as selected, \
                patch.object(PointsService, "spend", return_value=(SimpleNamespace(id=9), True)), \
                patch.object(PointsService, "refund") as refund:
            ledgers.return_value.exists.return_value = False
            selected.return_value.get.return_value = run
            receipt, error = billing.reserve_image_charge(user=user, context=context, run=run, payload=payload)
            billing.settle_image_charge(user=user, run=run, receipt=receipt)
            for candidate in ({"status": "running", "fieldId": "image.hero"}, {"status": "failed", "fieldId": "wrong-slot"}):
                billing.reconcile_image_charges(user=user, run=run, snapshot={"candidates": {"async-image": candidate}})
            refund.assert_not_called()
            snapshot = {"candidates": {"async-image": {"status": "failed", "fieldId": "image.hero", "error": "Provider failed"}}}
            billing.reconcile_image_charges(user=user, run=run, snapshot=snapshot)
            billing.reconcile_image_charges(user=user, run=run, snapshot=snapshot)
            refund.assert_called_once()

    def test_refund_updates_durable_run_stamp_and_refunded_job_cannot_reuse_charge(self):
        from integrations.services import article_generation as generation
        from . import vibe_marketing_views as views
        from roo.models import Ledger
        from roo.services import PointsService
        from .models import ContentFactoryJob
        from workflow_runs.models import ContentFactoryRun
        user = SimpleNamespace(pk=8)
        run = SimpleNamespace(run_id="paid-draft", run_request={"client_request_id": "draft-identity"}, save=Mock())
        original = SimpleNamespace(id=9, delta_microroo=-12_000_000)
        with patch.object(Ledger.objects, "filter") as ledgers, \
                patch.object(PointsService, "refund", return_value=(SimpleNamespace(id=10), True)) as refund, \
                patch.object(ContentFactoryJob.objects, "filter") as jobs, \
                patch.object(ContentFactoryRun.objects, "filter") as runs:
            ledgers.return_value.first.return_value = original
            runs.return_value.filter.return_value = [run]
            generation._refund_content_factory_request(user=user, slack_user_id="founder", article_request={"client_request_id": "draft-identity"}, resolved_domain="paid.example", reason="No saved delivery")
            self.assertEqual(run.run_request["roo_points_billing_status"], "refunded")
            self.assertFalse(run.run_request["roo_points_authorized"])
            self.assertEqual(refund.call_args.kwargs["delta"], 12)
            jobs.return_value.first.return_value = SimpleNamespace(billing_status="refunded", billing_ledger_id=10)
            self.assertIsNone(views._reusable_content_factory_charge_for_run(run))

    def test_invalid_legacy_ledger_stamp_returns_billing_action_without_crashing(self):
        from . import vibe_marketing_views as views
        from roo.models import Ledger
        for stamp in ("ledger-article-original", "-1", "0", "999999999999999999999999999999999999999", "9" * 5000):
            with self.subTest(stamp=stamp):
                run = SimpleNamespace(run_request={"roo_points_authorized": True,
                    "roo_points_action": "article_generation", "roo_points_cost": 6,
                    "roo_points_billing_status": "charged", "roo_points_ledger_id": stamp})
                with patch.object(Ledger.objects, "filter") as ledgers, \
                        patch.object(views, "_reusable_content_factory_charge_for_run", return_value=None):
                    ledgers.return_value.first.return_value = None
                    result = views._reuse_roo_points_authorization_for_article_job(
                        run=run, payload={}, domain="paid.example", failure_detail="A paid draft is required.")
                    self.assertEqual(result.status_code, 409)
                    self.assertEqual(result.data["code"], "roo_points_billing_required")
