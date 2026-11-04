"""Actual provider facts and native admission preserve owned Run execution."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch
import json
from pathlib import Path

from django.db import transaction
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import User
from workflow_runs.models import ContentFactoryRun
from .run_ownership import (bind_admitted_run, reserve_admitted_run, user_principal,
    owner_receipt, WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, public_run_value, HOSTED_PUBLICATION_OBSERVATION_KEY)
from .tests_run_event_authority import FLAGS
from .tests_website_connections import WebsiteDatabaseFixture
from .website_contract import WebsiteAuthorityError
from .website_support import _project_legacy_verification, owner_support_operation




@override_settings(**FLAGS)
class OwnedNativeVerificationTests(WebsiteDatabaseFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        from .website_models import WebsiteConnectionOperation
        self.website.verified_sha = "a" * 40
        self.website.state = "connected"
        self.website.save(update_fields=["verified_sha", "state"])
        operation = WebsiteConnectionOperation.objects.create(connection=self.website,
            generation=self.website.generation, action="workflow", state="pending", idempotency_key="native-parent",
            payload={"attempt": 1})
        raw = {**self.binding, "domain": self.org.domain, "github_repo": self.website.github_repo,
            "client_request_id": "native-parent", "expected_source_sha": "a" * 40,
            "operation_id": str(operation.pk), "operation_attempt": 1, "deletion_epoch": 0}
        self.run = bind_admitted_run(reserve_admitted_run(organization=self.org,
            principal=user_principal(123, "user:123"), workflow="article_system_setup", payload=raw), "native-parent-worker")
        operation.payload = {**operation.payload, "run_id": self.run.run_id}
        operation.save(update_fields=["payload"])
        self.user = User.objects.create_user(email="native-requester@synthetic.example.test")
        from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
        from founder_tools.services import get_founder_company_context
        profile = VibeRaisingProfile.objects.create(user=self.user, role="founder")
        company = VibeRaisingCompany.objects.create(profile=profile, organization=self.org,
            domain=self.org.domain, name="Native verification fixture")
        self.context = get_founder_company_context(self.user, company_id=company.pk)
        self.website.authorized_by = self.user
        self.website.save(update_fields=["authorized_by"])
        self.data = {**self.binding, "company_id": str(self.context.company.pk), "run_id": self.run.run_id, "phase": "verify", "idempotency_key": "native-one",
            "contract": {"schema_version": 1, "reviewed": True, "runtime_family": "static", "runtime_version": "1",
                "build_command": ["build"], "content_path_pattern": "content/{slug}.md",
                "route_template": "/articles/{slug}", "listing_route": "/articles"},
            "verification": {"source_sha": "a" * 40, "target_id": "custom", "seed_slug": "mlai-verification", "expected_h1": "MLAI verification article", "file_overrides": {}}}

    def test_actual_authenticated_route_admits_exact_native_child_before_post_and_replays_flags_off(self):
        from .website_views import WebsiteConnectionActionView
        parent = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        sent = []
        def send(url, **kwargs):
            wire = kwargs["json"]
            child = ContentFactoryRun.objects.get(run_id=wire["run_id"])
            self.assertIsNotNone(owner_receipt(child))
            self.assertEqual(owner_receipt(child)["identity"]["principal"]["user_id"], str(self.user.pk))
            self.assertEqual(child.workflow, "article_system_setup")
            self.assertEqual(child.run_request, wire)
            self.assertEqual(wire["source_run_id"], self.run.run_id)
            self.assertEqual(wire[WIRE_REQUEST_KEY]["dispatch_key"], "native-one")
            self.assertIn(WIRE_EXECUTION_KEY, wire)
            sent.append(deepcopy(wire))
            return SimpleNamespace(status_code=202, json=lambda: {"status": "queued"})
        def call():
            request = APIRequestFactory().post("/", self.data, format="json")
            force_authenticate(request, user=self.user)
            return WebsiteConnectionActionView.as_view()(request, action="custom-contract")
        with patch("content_factory.vibe_marketing_views._content_factory_remote_config",
                    return_value={"enabled": True, "base_url": "https://worker.test"}), \
                patch("integrations.http_client.post", side_effect=send) as dispatch:
            first = call()
            self.assertLess(first.status_code, 300, first.data)
            with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False):
                second = call()
            self.assertLess(second.status_code, 300, second.data)
        dispatch.assert_called_once()
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), parent)
        if __import__('os').environ.get('FOUNDER_EXPORT_NATIVE_FIXTURE'):
            Path(__import__('os').environ['FOUNDER_EXPORT_NATIVE_FIXTURE']).write_text(json.dumps({
                "schema_version": 1, "endpoint": "/api/connections/{id}/custom-contract",
                "workflow": "article_system_setup", "parent_request": self.run.run_request,
                "payload": sent[0]}, indent=2) + "\n")

    def test_native_proof_projection_keeps_owned_run_but_legacy_keeps_its_exact_projection(self):
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        legacy = ContentFactoryRun.objects.create(run_id="legacy-native", domain=self.org.domain, status="queued")
        with transaction.atomic():
            _project_legacy_verification(self.run.run_id, status="completed", outcome={"status": "verified"})
            _project_legacy_verification(legacy.run_id, status="completed", outcome={"status": "verified"})
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)
        legacy.refresh_from_db()
        self.assertEqual((legacy.status, legacy.result), ("completed", {"status": "verified"}))

    def test_new_native_admission_cannot_infer_requester_from_source_payer_or_owner(self):
        with self.assertRaises(WebsiteAuthorityError), patch("integrations.http_client.post") as send:
            owner_support_operation(self.context, self.config, action="custom-contract", data=self.data)
        send.assert_not_called()

    def test_missing_seeded_article_inputs_refuse_before_any_child_or_http(self):
        before = ContentFactoryRun.objects.count()
        for field in ("seed_slug", "expected_h1"):
            data = deepcopy(self.data)
            data["verification"].pop(field)
            with self.subTest(field=field), patch("integrations.http_client.post") as send, \
                    self.assertRaises(WebsiteAuthorityError) as error:
                owner_support_operation(self.context, self.config, action="custom-contract", data=data, user=self.user)
            self.assertEqual(error.exception.status, 422)
            send.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.count(), before)

    def test_legacy_parent_stays_unmarked_while_new_native_child_retains_authenticated_descriptor(self):
        import hashlib, os
        from django.db import connection
        from .website_models import WebsiteConnectionOperation
        from .website_operations import reserve_workflow_operation
        key = "legacy-native-parent"
        raw = {**self.binding, "client_request_id": key, "expected_source_sha": "a" * 40}
        operation = reserve_workflow_operation(self.website, workflow="repo_scan", payload=raw)
        raw["run_id"] = key
        operation.payload = {**operation.payload, "run_id": key}
        operation.save(update_fields=["payload"])
        parent = ContentFactoryRun.objects.create(run_id=key, workflow="repo_scan", organization=self.org,
            github_repo=self.config.github_repo, domain=self.org.domain, status="completed", run_request=raw)
        before = ContentFactoryRun.objects.values().get(pk=parent.pk)
        digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True,
            separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        source_status = {"workflow": "repo_scan", "generation": 0, "result": {
            "requested_action": "setup_article_system", "result": {"scaffold_plan": {"retained": True}}}}
        descriptor = {"schema_version": 1, "source_run_id": key, "workflow": "repo_scan",
            "domain": self.org.domain, "github_repo": self.config.github_repo,
            "request_sha256": digest(raw),
            "content_sha256": "f86de969b6bab98a67aa29ac78c17aea8e591ebef6f7f29f4009706ee4676c39"}
        descriptor["descriptor_sha256"] = digest(descriptor)
        def read(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            response = Mock(status_code=200, headers={})
            response.iter_content.return_value = [json.dumps(descriptor).encode()]
            return response
        sent = []
        def dispatch(url, **kwargs):
            wire = kwargs["json"]
            self.assertEqual(wire["legacy_source_v1"], descriptor)
            self.assertNotEqual(wire[WIRE_REQUEST_KEY]["dispatch_key"], key)
            self.assertEqual(wire["source_run_id"], key)
            child = ContentFactoryRun.objects.get(run_id=wire["run_id"])
            self.assertIsNotNone(owner_receipt(child))
            self.assertEqual(child.run_request, wire)
            sent.append(deepcopy(wire))
            return SimpleNamespace(status_code=202, json=lambda: {"status": "queued"})
        data = {**deepcopy(self.data), "run_id": key, "idempotency_key": "legacy-native-new-child"}
        with patch("content_factory.vibe_marketing_views._content_factory_remote_config",
                return_value={"enabled": True, "base_url": "https://worker.test"}), \
                patch("integrations.http_client.get", side_effect=read) as get, \
                patch("integrations.http_client.post", side_effect=dispatch) as post:
            child = owner_support_operation(self.context, self.config, action="custom-contract", data=data, user=self.user)
        self.assertEqual(get.call_args.kwargs["allow_redirects"], False)
        post.assert_called_once()
        self.assertIsNotNone(child)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=parent.pk), before)
        if os.environ.get("FOUNDER_EXPORT_LEGACY_NATIVE_FIXTURE"):
            Path(os.environ["FOUNDER_EXPORT_LEGACY_NATIVE_FIXTURE"]).write_text(json.dumps({
                "schema_version": 1, "source_request": raw, "source_status": source_status,
                "legacy_source_v1": descriptor, "payload": sent[0]}, indent=2) + "\n")

    def test_provider_merge_preserves_concurrent_typed_history_and_all_execution_fields(self):
        from .run_event_authority import receive_authoritative_run_events
        from .tests_run_event_authority import batch
        from .vibe_marketing_views import _save_setup_merge_result
        stale = self.run
        response = receive_authoritative_run_events(batch(stale, [{"status": "running"}]))
        self.assertEqual(response.status_code, 200, response.data)
        before = ContentFactoryRun.objects.values().get(pk=stale.pk)
        _save_setup_merge_result(stale, checks_status="success", merge_response={"merged": True, "sha": "d" * 40})
        after = ContentFactoryRun.objects.values().get(pk=stale.pk)
        for field in ("status", "current_step", "approval_state", "run_request", "created_at", "updated_at", "last_event_emitted_at", "reconciled_at"):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(after["result"]["run_owner_events_v1"], before["result"]["run_owner_events_v1"])
        self.assertEqual(after["result"]["merge_status"], "merged")
        self.assertEqual(stale.status, before["status"])

    def test_provider_result_rejects_replaced_control_history_and_stale_execution(self):
        from .run_provider_facts import save_provider_result
        from .run_backend_facts import BackendFactConflict
        from .tests_run_event_authority import advance_ticket
        baseline = deepcopy(self.run.result)
        proposed = {**deepcopy(baseline), "run_owner_v1": {"changed": True}}
        with self.assertRaises(BackendFactConflict):
            save_provider_result(self.run, proposed, baseline=baseline, legacy_fields=["result", "updated_at"])
        fresh = ContentFactoryRun.objects.get(pk=self.run.pk)
        advance_ticket(fresh)
        before = ContentFactoryRun.objects.values().get(pk=fresh.pk)
        with self.assertRaises(BackendFactConflict):
            save_provider_result(self.run, {**baseline, "merge_status": "merged"}, baseline=baseline,
                legacy_fields=["result", "updated_at"])
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=fresh.pk), before)

    def test_preview_and_pr_failure_acknowledgements_do_not_project_execution(self):
        from .run_event_authority import receive_authoritative_run_events
        from .tests_run_event_authority import batch
        from .vibe_marketing_views import _persist_live_preview_payload, _project_setup_pr_creation_failure
        response = receive_authoritative_run_events(batch(self.run, [{"status": "running"}]))
        self.assertEqual(response.status_code, 200, response.data)
        self.run.refresh_from_db()
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        preview = {"available": False, "status": "failed", "error": "Synthetic browser failure"}
        _persist_live_preview_payload(self.run, preview)
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        self.assertEqual(after["result"], {**before["result"], "livePreview": preview})
        for field in set(before) - {"result"}:
            self.assertEqual(after[field], before[field], field)
        config_before = type(self.config).objects.values().get(pk=self.config.pk)
        detail = _project_setup_pr_creation_failure(self.run, self.config,
            {"status": "setup_pr_create_failed", "error": "Synthetic PR refusal"})
        self.assertEqual(detail, "Synthetic PR refusal")
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), after)
        self.assertEqual(type(self.config).objects.values().get(pk=self.config.pk), config_before)

    def test_restart_reservation_preserves_typed_history_and_original_execution_clocks(self):
        from .run_event_authority import receive_authoritative_run_events
        from .tests_run_event_authority import batch
        from .vibe_marketing_views import _reserve_article_restart
        response = receive_authoritative_run_events(batch(self.run, [{"status": "running"}]))
        self.assertEqual(response.status_code, 200, response.data)
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        receipt = {"state": "pending", "client_request_id": "restart-child"}
        self.assertIsNone(_reserve_article_restart(run=self.run, context=self.context,
            payload={"topic": "Synthetic saved topic"}, receipt=receipt))
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        self.assertEqual(after["result"]["run_owner_events_v1"], before["result"]["run_owner_events_v1"])
        self.assertEqual(after["result"]["restart_receipt"]["user_action_count"], 1)
        for field in set(before) - {"result"}:
            self.assertEqual(after[field], before[field], field)

    def test_source_wake_retains_non_goal_owned_child_without_reopening_or_dispatch(self):
        from .run_event_authority import receive_authoritative_run_events
        from .tests_run_event_authority import batch
        from .website_models import WebsiteConnectionOperation
        from .website_prepare import wake_prepare_for_source
        response = receive_authoritative_run_events(batch(self.run, [{"status": "failed"}]))
        self.assertEqual(response.status_code, 200, response.data)
        operation = WebsiteConnectionOperation.objects.create(connection=self.website,
            generation=self.website.generation, action="prepare", state="needs_user",
            idempotency_key="legacy-prepare-owned-child", receipt={"child_run_id": self.run.run_id},
            payload={"company_id": str(self.context.company.pk), "requested_by_user_id": str(self.user.pk), "attempt": 1})
        before = WebsiteConnectionOperation.objects.values().get(pk=operation.pk)
        run_before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        self.website.verified_sha = "b" * 40
        self.website.save(update_fields=["verified_sha"])
        with patch("content_factory.website_prepare.start_prepare") as start, \
                patch("content_factory.website_prepare._adopt_source_checks") as adopt:
            result = wake_prepare_for_source(self.website.pk, organization_id=self.org.pk)
        start.assert_not_called()
        adopt.assert_not_called()
        self.assertEqual(result.pk, operation.pk)
        self.assertEqual(WebsiteConnectionOperation.objects.values().get(pk=operation.pk), before)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), run_before)

    def test_provider_verified_setup_merge_metadata_preserves_owned_execution(self):
        from .vibe_marketing_views import _persist_setup_merged_verification, SETUP_MERGED_VERIFICATION_KEY
        from .run_event_authority import receive_authoritative_run_events
        from .tests_run_event_authority import batch
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(result={**self.run.result,
            "pr_url": "https://github.com/example/site/pull/42", "pr_number": 42})
        self.run.refresh_from_db()
        response = receive_authoritative_run_events(batch(self.run, [{"status": "running"}]))
        self.assertEqual(response.status_code, 200, response.data)
        self.run.refresh_from_db()
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        repo = {"id": self.website.repository_id, "full_name": self.website.github_repo}
        pull = {"number": 42, "merged": True, "html_url": "https://github.com/example/site/pull/42",
            "merge_commit_sha": "e" * 40, "head": {"sha": "d" * 40, "repo": repo},
            "base": {"ref": "main", "repo": repo}}
        with patch("content_factory.website_connections.read_setup_merge_pull", return_value=pull):
            _persist_setup_merged_verification(self.run, {"accepted": True, "status": "verified"})
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        self.assertEqual(after["result"][SETUP_MERGED_VERIFICATION_KEY], {"accepted": True, "status": "verified"})
        self.assertEqual(after["result"]["run_owner_events_v1"], before["result"]["run_owner_events_v1"])
        for field in set(before) - {"result"}:
            self.assertEqual(after[field], before[field], field)

    def test_owned_selection_release_changes_only_selection_fact_not_run_clocks(self):
        from .run_event_authority import receive_authoritative_run_events
        from .tests_run_event_authority import batch
        from .island_removal import release_from_selections
        from .island_selection import STATE_KEY
        state = {"selected_ids": ["one"], "managed_proposal_ids": ["one"], "managed_slugs": ["gardens"],
            "groups": [{"slug": "gardens", "proposal_ids": ["one"]}], "revision": 1}
        response = receive_authoritative_run_events(batch(self.run, [{"status": "completed"}]))
        self.assertEqual(response.status_code, 200, response.data)
        self.run.refresh_from_db()
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(result={**self.run.result, STATE_KEY: state})
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        with transaction.atomic():
            release_from_selections(self.org, "gardens")
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        self.assertEqual(after["result"][STATE_KEY]["managed_slugs"], [])
        for field in ("status", "run_request", "updated_at", "created_at", "last_event_emitted_at", "reconciled_at"):
            self.assertEqual(after[field], before[field], field)

    @override_settings(CONTENT_FACTORY_IMAGE_REGENERATION_COST_POINTS=2)
    def test_image_charge_and_original_refund_preserve_owned_clock_and_concurrent_event_history(self):
        from roo.models import Ledger
        from roo.services import PointsService
        from .article_review_billing import reserve_image_charge, settle_image_charge
        from .run_event_authority import receive_authoritative_run_events
        from .tests_run_event_authority import batch
        PointsService.credit_purchased_topup(user=self.user, delta=10,
            description="Synthetic image price funding", idempotency_key="image-clock-funding")
        response = receive_authoritative_run_events(batch(self.run, [{"status": "running"}]))
        self.assertEqual(response.status_code, 200, response.data)
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        payload = {"operationId": "image-clock", "fieldId": "cover", "expectedCostPoints": 2}
        receipt, error = reserve_image_charge(user=self.user, context=self.context, run=self.run, payload=payload)
        self.assertIsNone(error)
        self.assertEqual(Ledger.objects.filter(kind="SPEND", reference_type="CONTENT_FACTORY_IMAGE").count(), 1)
        settle_image_charge(user=self.user, run=self.run, receipt=receipt, pending=True)
        settle_image_charge(user=self.user, run=self.run, receipt=receipt, rejected=True)
        settle_image_charge(user=self.user, run=self.run, receipt=receipt, rejected=True)
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for field in ("status", "run_request", "updated_at", "created_at", "last_event_emitted_at", "reconciled_at"):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(after["result"]["run_owner_events_v1"], before["result"]["run_owner_events_v1"])
        self.assertEqual(after["result"]["article_image_billing"][receipt["identity"]]["status"], "refunded")
        self.assertEqual(Ledger.objects.filter(kind="REFUND", refund_of_id=receipt["ledgerId"]).count(), 1)
