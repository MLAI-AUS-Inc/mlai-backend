"""Actual delivery endpoints/projector, replacing persistence and HTTP only."""

from contextlib import ExitStack, nullcontext
from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from . import service_views, setup_delivery_receiver as receiver
from .setup_delivery_contract import digest, payload_digest, state_digest, validate_envelope, DeliveryContractError
from . import setup_delivery_reconciliation as recovery


def envelope(*, kind="setup_callback", version=1, event_id="delivery-event-1", status="running", result=None):
    scope = {"domain": "fixture.test", "github_repo": "fixture/site",
        "website_connection_id": "11111111-1111-4111-8111-111111111111", "connection_generation": 1,
        "repository_id": 42, "operation_id": "22222222-2222-4222-8222-222222222222",
        "operation_attempt": 1, "deletion_epoch": 0, "client_request_id": "dispatch-1"}
    projection = {"status": status, "current_step": "verify_directory_build", "generation": 0,
        "state_version": version, "approval_state": "not_required", "resume_available": False,
        "failure": {}, "failure_occurrence_id": None, "result": result or {}, "step_states": {},
        "verification_summary": {}, "acceptance_summary": {}}
    if status in {"failed", "blocked"}:
        from .website_failure_contract import classify_failure
        projection["failure"] = classify_failure("DIRECTORY_STYLE_REVIEW_FAILED", "verify_directory_build")
        projection["failure_occurrence_id"] = "33333333-3333-4333-8333-333333333333"
    value = {"schema_version": 1, "kind": kind, "event_id": event_id,
        "event_type": "article_system_setup_progress" if kind == "setup_callback" else None,
        "run_id": "setup-1", "workflow": "article_system_setup", "emitted_at": "2026-10-14T03:00:00+00:00",
        "scope": scope, "execution": {"generation": 0, "state_version": version},
        "body": {"state_projection": projection}}
    return signed(value)


def signed(value):
    value["state_sha256"] = state_digest(value)
    value["payload_sha256"] = payload_digest(value)
    return value


class MemoryQuery:
    """Only the ORM boundary is replaced; real identity and transition code runs."""
    def __init__(self, rows):
        self.rows = rows

    def filter(self, **where):
        def matches(row):
            return all(str(getattr(row, key, None)) == str(value) for key, value in where.items())
        return MemoryQuery([row for row in self.rows if matches(row)])

    def first(self):
        return self.rows[0] if self.rows else None

    def select_related(self, *args):
        return self

    def update(self, **values):
        for row in self.rows:
            for key, value in values.items():
                setattr(row, key, value)
        return len(self.rows)

    def get(self, **where):
        row = self.filter(**where).first()
        if row is None:
            raise AssertionError("Missing persistence fixture")
        return row


class MemoryManager(MemoryQuery):
    def __init__(self, model, rows):
        self.model, self.rows = model, rows

    def select_for_update(self):
        return self

    def create(self, **values):
        row = self.model(**values)
        row.save = Mock()
        self.rows.append(row)
        return row

    def get_or_create(self, *, defaults, **where):
        row = self.filter(**where).first()
        return (row, False) if row else (self.create(**where, **defaults), True)


@override_settings(CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED=True, ROO_API_KEY="fixture-service-key")
class SetupDeliveryChainTests(SimpleTestCase):
    def setUp(self):
        self.input = envelope()
        scope = self.input["scope"]
        self.organization = SimpleNamespace(pk=7, domain="fixture.test")
        self.website = SimpleNamespace(pk=scope["website_connection_id"], id=scope["website_connection_id"],
            organization_id=7, organization=self.organization, generation=1, github_repo="fixture/site",
            repository_id=42, state="connected", blockers=[], capabilities={}, verified_sha="")
        self.operation = SimpleNamespace(pk=scope["operation_id"], connection=self.website,
            connection_id=scope["website_connection_id"],
            generation=1, action="workflow", state="running", receipt={}, save=Mock(),
            payload={"attempt": 1, "workflow": "article_system_setup", "client_request_id": "dispatch-1",
                "run_id": "setup-1", "setup_delivery_v1": {"schema_version": 1},
                "setup_delivery_scope": deepcopy(scope)})
        self.run = receiver.ContentFactoryRun(run_id="setup-1", workflow="article_system_setup",
            organization_id=7, domain="fixture.test", github_repo="fixture/site", status="running",
            run_request={**deepcopy(scope), "setup_delivery_v1": {"schema_version": 1}}, result={})
        self.run.save = Mock()
        self.rows, self.markers = [self.run], []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(receiver.transaction, "atomic", side_effect=lambda: nullcontext()))
        for model, rows in [(receiver.Organization, [self.organization]), (receiver.WebsiteConnection, [self.website]),
                (receiver.WebsiteConnectionOperation, [self.operation]), (receiver.ContentFactoryRun, self.rows),
                (receiver.ContentFactoryCallbackEvent, self.markers)]:
            self.stack.enter_context(patch.object(model, "objects", MemoryManager(model, rows)))
        self.steps = self.stack.enter_context(patch.object(receiver.ContentFactoryRunStep.objects, "update_or_create"))
        # These are effects, not dependency helpers. A recovery projection is
        # required never to call them, even for a terminal completed snapshot.
        self.legacy = self.stack.enter_context(patch.object(service_views.ContentFactoryCallbackView,
            "_dispatch_callback_event", side_effect=AssertionError("legacy effect dispatch")))
        self.snapshot = self.stack.enter_context(patch.object(service_views, "_apply_run_snapshot",
            side_effect=AssertionError("legacy snapshot effects")))

    def post(self, payload=None):
        request = APIRequestFactory().post("/api/content-factory/callback", self.input if payload is None else payload,
            format="json", HTTP_X_API_KEY="fixture-service-key")
        return service_views.ContentFactoryCallbackView.as_view()(request)

    def put(self, payload):
        request = APIRequestFactory().put("/api/content-factory/runs/" + payload["run_id"], payload,
            format="json", HTTP_X_API_KEY="fixture-service-key")
        return service_views.ContentFactoryRunView.as_view()(request, run_id=payload["run_id"])

    def test_actual_callback_commits_projection_and_exact_ack_then_replay_is_idempotent(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "applied")
        self.assertEqual(response.data["payload_sha256"], self.input["payload_sha256"])
        self.assertEqual(self.run.current_step, "verify_directory_build")
        self.assertIsNotNone(self.markers[0].processed_at)
        calls = self.run.save.call_count
        replay = self.post()
        self.assertEqual(replay.data["status"], "duplicate")
        self.assertEqual(self.run.save.call_count, calls)
        self.legacy.assert_not_called()

    def test_malformed_input_rejected_before_any_claim_bind_or_write(self):
        for mutate in [lambda value: value.update(event_id="x" * 101),
                lambda value: value["execution"].update(generation=True),
                lambda value: value["scope"].update(operation_attempt="1"),
                lambda value: value["body"]["state_projection"].update(result=[]),
                lambda value: value["body"]["state_projection"].update(approval_state={}),
                lambda value: value.update(schema_version=99)]:
            with self.subTest(mutate=mutate):
                value = deepcopy(self.input)
                mutate(value)
                with self.assertLogs(receiver.logger, level="WARNING") as logs:
                    response = self.post(value)
                self.assertEqual(response.status_code, 422)
                self.assertIn("fields=", logs.output[0])
        self.run.save.assert_not_called()
        self.assertEqual(self.markers, [])
        self.operation.save.assert_not_called()

    def test_changed_identity_or_payload_cannot_reuse_an_event_id(self):
        self.post()
        changed = deepcopy(self.input)
        changed["body"]["state_projection"]["current_step"] = "prepare_branch"
        signed(changed)
        response = self.post(changed)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["code"], "delivery_identity_conflict")
        self.assertEqual(self.run.current_step, "verify_directory_build")

    def test_equal_version_mirror_matches_shared_state_and_changed_state_is_conflict(self):
        self.post()
        mirror = envelope(kind="run_snapshot", event_id="mirror-1")
        self.assertNotEqual(mirror["payload_sha256"], self.input["payload_sha256"])
        self.assertEqual(mirror["state_sha256"], self.input["state_sha256"])
        self.assertEqual(self.put(mirror).data["status"], "duplicate")
        mirror = envelope(kind="run_snapshot", event_id="mirror-2", result={"retryable": False})
        self.assertEqual(self.put(mirror).data["code"], "delivery_state_conflict")

    def test_stale_version_does_not_regress_and_invented_generation_is_refused(self):
        self.post(envelope(version=3))
        old = envelope(version=1, event_id="old-1")
        self.assertEqual(self.post(old).data["status"], "ignored_stale")
        newer = envelope(version=1, event_id="invented-1")
        newer["execution"]["generation"] = 1
        newer["body"]["state_projection"]["generation"] = 1
        signed(newer)
        self.assertEqual(self.post(newer).data["code"], "delivery_generation_unproven")
        self.assertEqual(self.run.result["generation"], 0)

    def test_live_lease_defers_then_expired_claim_is_reclaimed(self):
        metadata = {"receipts": {self.input["event_id"]: {"payload_sha256": self.input["payload_sha256"],
            "claimed_at": timezone.now().isoformat(), "nonce": "dead-worker", "processed_at": None}}}
        self.run.result["delivery_v1"] = metadata
        self.markers.append(receiver.ContentFactoryCallbackEvent(event_id=self.input["event_id"],
            job_id="setup-1", event_type=self.input["event_type"], claimed_at=timezone.now()))
        self.markers[0].save = Mock()
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.data["acknowledged"])
        metadata["receipts"][self.input["event_id"]]["claimed_at"] = (timezone.now() - timedelta(minutes=3)).isoformat()
        self.assertEqual(self.post().data["status"], "applied")

    @override_settings(CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED=False)
    def test_apply_off_is_unconfirmed_and_keeps_legacy_protocol_available(self):
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.data["acknowledged"])
        self.assertEqual(self.markers, [])
        self.run.save.assert_not_called()

    def test_worker_marker_cannot_self_enroll(self):
        self.operation.payload.pop("setup_delivery_v1")
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "delivery_not_enrolled")
        self.assertFalse(response.data["acknowledged"])
        self.assertEqual(self.markers, [])

    def test_terminal_fence_acknowledges_only_exact_original_scope(self):
        self.run.status, self.operation.state = "cancelled", "cancelled"
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.data["acknowledged"])
        self.assertEqual(response.data["status"], "ack_terminal")
        self.assertEqual(self.run.status, "cancelled")
        conflict = deepcopy(self.input)
        conflict["scope"]["repository_id"] = 99
        signed(conflict)
        response = self.post(conflict)
        self.assertFalse(response.data["acknowledged"])
        self.run.save.assert_not_called()

    def test_durable_receiver_metadata_survives_legacy_result_merge_but_cannot_be_injected(self):
        self.post()
        metadata = deepcopy(self.run.result["delivery_v1"])
        merged = service_views._merge_django_owned_run_result(self.run.result, {"delivery_v1": {}, "status": "running"})
        self.assertEqual(merged["delivery_v1"], metadata)
        self.assertNotIn("delivery_v1", service_views._merge_django_owned_run_result({}, {"delivery_v1": metadata}))

    def test_late_completed_event_is_observation_only_without_approval_or_proof_changes(self):
        self.run.status, self.run.approval_state, self.operation.state = "completed", "approved", "completed"
        self.run.result = {"generation": 0, "state_version": 1, "backend_proof": {"sha": "a" * 40}}
        original = deepcopy(self.run.result)
        late = envelope(version=2, result={"article_system_setup": {"publishing_ready": True}})
        response = self.post(late)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["disposition"], "observation_only")
        self.assertEqual(self.run.status, "completed")
        self.assertEqual(self.run.approval_state, "approved")
        self.assertEqual(self.run.result["backend_proof"], original["backend_proof"])
        self.assertNotIn("article_system_setup", self.run.result)
        self.assertEqual(self.run.result["state_version"], 1)

    def test_explicitly_reserved_resume_allows_higher_generation(self):
        self.post()
        self.run.status = "failed"
        self.operation.payload.update(resume_pending=True, resume_execution_version=[0, 1])
        newer = envelope(version=0, event_id="resumed-1")
        newer["execution"]["generation"] = 1
        newer["body"]["state_projection"]["generation"] = 1
        signed(newer)
        self.assertEqual(self.post(newer).data["status"], "applied")
        self.assertEqual(self.run.result["generation"], 1)
        self.assertEqual(self.run.status, "running")

    def test_lost_terminal_push_is_recovered_by_one_pure_get_and_healthy_repeat_is_no_change(self):
        completed = envelope(kind="run_snapshot", status="completed", event_id="snapshot-terminal")
        response = Mock(status_code=200, iter_content=Mock(return_value=[
            __import__("json").dumps(completed).encode()]))
        with patch.object(recovery.requests, "get", return_value=response) as get:
            outcome = recovery.reconcile_setup_delivery_run(self.run, base_url="https://worker.fixture",
                headers={"X-API-KEY": "test-key"})
            self.assertEqual(outcome, "repaired")
            self.assertEqual(self.run.status, "completed")
            self.assertEqual(recovery.reconcile_setup_delivery_run(self.run, base_url="https://worker.fixture",
                headers={"X-API-KEY": "test-key"}), "no_change")
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs["params"], {"view": "delivery_snapshot"})
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.legacy.assert_not_called()
        self.snapshot.assert_not_called()
        self.assertNotIn("pull", self.run.result["delivery_v1"])

    def test_pull_cannot_overwrite_callback_that_won_during_get(self):
        stale = envelope(kind="run_snapshot", version=1, status="failed", event_id="snapshot-old")
        def remote(*args, **kwargs):
            self.assertEqual(self.post(envelope(version=3, event_id="push-newer")).data["status"], "applied")
            return Mock(status_code=200, iter_content=Mock(return_value=[__import__("json").dumps(stale).encode()]))
        with patch.object(recovery.requests, "get", side_effect=remote):
            outcome = recovery.reconcile_setup_delivery_run(self.run, base_url="https://worker.fixture",
                headers={"X-API-KEY": "test-key"})
        self.assertEqual(outcome, "deferred")
        self.assertEqual(self.run.status, "running")
        self.assertEqual(self.run.result["state_version"], 3)

    def test_missing_or_malformed_snapshot_never_infers_failure_refund_or_restart(self):
        original = self.run.status
        for response, expected in [(Mock(status_code=404), "unavailable"),
                (Mock(status_code=200, iter_content=Mock(return_value=[b"{"])), "contract_rejected")]:
            with patch.object(recovery.requests, "get", return_value=response):
                self.assertEqual(recovery.reconcile_setup_delivery_run(self.run,
                    base_url="https://worker.fixture", headers={"X-API-KEY": "test-key"}), expected)
        self.assertEqual(self.run.status, original)
        self.assertNotIn("failure", self.run.result)
        self.operation.save.assert_not_called()
        self.snapshot.assert_not_called()

    @override_settings(CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED=False)
    def test_apply_off_preserves_valid_identical_duplicate(self):
        with override_settings(CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED=True):
            self.post()
        self.assertEqual(self.post().data["disposition"], "duplicate")

    def test_legacy_large_article_callback_is_not_routed_into_setup_schema(self):
        data = {"event_type": "article_complete", "job_id": "article-1", "article_content": "x" * (3 * 1024 * 1024)}
        view = service_views.ContentFactoryCallbackView()
        request = SimpleNamespace(data=data, method="POST", query_params={})
        result = view.post(request)
        legacy = view._post_legacy(request)
        self.assertEqual(result.status_code, legacy.status_code)
        self.assertEqual(result.data, legacy.data)
        # This unbound publishing callback retains its original consent denial;
        # the setup-only 2 MiB schema must not replace it with a 422 size error.
        self.assertEqual(result.status_code, 409)
        self.legacy.assert_not_called()

    def test_legacy_revoked_cleanup_put_preserves_url_identity_and_erasure_receipt(self):
        self.run.workflow = "article_generation"
        self.run.run_request.pop("setup_delivery_v1")
        self.run.created_at = self.run.updated_at = timezone.now()
        self.website.state, self.website.generation = "disconnected", 2
        self.operation.action, self.operation.state = "disconnect", "cancelled"
        self.operation.receipt = {"website_database_evidence_erased": True,
            "artifact_retention": {"status": "retained"}}
        erased = deepcopy(self.operation.receipt)
        body = {"domain": self.run.domain, "workflow": "article_generation", "status": "cancelled",
            "repository_inventory": {"discovery_complete": True},
            "result": {"publishing_ready": True, "cleanup_pending": False}}
        steps = Mock(order_by=Mock(return_value=[]))
        with patch.object(receiver.ContentFactoryRun, "steps", PropertyMock(return_value=steps)):
            request = APIRequestFactory().put("/api/content-factory/runs/" + self.run.run_id, body,
                format="json", HTTP_X_API_KEY="fixture-service-key")
            response = service_views.ContentFactoryRunView.as_view()(request, run_id=self.run.run_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["run_id"], self.run.run_id)
        self.assertIs(response.data["result"]["cancellation_receipt"]["repository_authority"], False)
        self.assertEqual(self.run.status, "cancelled")
        self.assertNotIn("publishing_ready", self.run.result)
        self.assertEqual(self.website.capabilities, {})
        self.assertEqual(self.operation.receipt, erased)
        self.operation.save.assert_not_called()
        self.snapshot.assert_not_called()
        self.legacy.assert_not_called()

    def test_legacy_cleanup_put_still_rejects_another_tenant_before_writes(self):
        body = {"domain": "another.example.org", "workflow": "article_generation", "status": "cancelled"}
        request = APIRequestFactory().put("/api/content-factory/runs/" + self.run.run_id, body,
            format="json", HTTP_X_API_KEY="fixture-service-key")
        response = service_views.ContentFactoryRunView.as_view()(request, run_id=self.run.run_id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "website_scope_mismatch")
        self.run.save.assert_not_called()
        self.assertEqual(self.markers, [])

    def test_structured_repair_diagnostics_remain_valid_without_becoming_action_status(self):
        data = envelope(result={"repair_status": {"attempts": 2, "reason": "generated import needs repair"},
            "repair_diagnostics": {"attempts": 2}, "repair_state": "failed"})
        self.assertEqual(self.post(data).status_code, 200)
        self.assertIsInstance(self.run.result["repair_status"], dict)

    def test_strict_step_shapes_are_checked_before_writes(self):
        data = deepcopy(self.input)
        data["body"]["state_projection"]["step_states"] = {"compile": {"status": "running", "required": True, "attempts": "one"}}
        signed(data)
        self.assertEqual(self.post(data).status_code, 422)
        self.run.save.assert_not_called()
        self.assertEqual(self.markers, [])

    def test_unauthenticated_delivery_is_denied_before_schema_or_any_claim(self):
        request = APIRequestFactory().post("/api/content-factory/callback", self.input, format="json")
        result = service_views.ContentFactoryCallbackView.as_view()(request)
        self.assertEqual(result.status_code, 403)
        self.run.save.assert_not_called()
        self.assertEqual(self.markers, [])

    def test_unproven_quality_claim_is_observed_without_promoting_readiness_or_approval(self):
        self.run.verification_summary = {"accepted_before": "existing-proof"}
        value = envelope(status="completed", result={"article_system_setup": {"status": "adopted",
            "build_verified": True, "browser_verified": True}, "build_verified": True,
            "directory_quality_gates": {"status": "passed"}, "live_preview": {"exactRender": True}})
        value["body"]["state_projection"]["approval_state"] = "approved"
        signed(value)
        result = self.post(value)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data["disposition"], "observation_only")
        self.assertEqual(self.run.approval_state, "not_required")
        self.assertNotIn("directory_quality_gates", self.run.result)
        self.assertNotIn("build_verified", self.run.result)
        self.assertEqual(self.run.verification_summary, {"accepted_before": "existing-proof"})
        self.assertEqual(self.website.capabilities, {})

    def test_exact_adopted_setup_proof_is_preserved_through_actual_verifier(self):
        base, head = "a" * 40, "b" * 40
        self.run.run_request["expected_source_sha"] = base
        value = envelope(status="completed", result={"status": "adopted", "adopted": True,
            "source_sha": base, "branch_commit_sha": head, "resume_generation": 0,
            "verification_kit_required": True, "adoption_kit_preview_verified": True,
            "build_verified": True, "browser_verified": True, "preview_commit_sha": head,
            "article_system_setup": {"status": "adopted", "setup_run_id": "setup-1", "adopted": True,
                "source_sha": base, "branch_commit_sha": head, "resume_generation": 0,
                "build_verified": True, "browser_verified": True, "preview_commit_sha": head},
            "live_preview": {"exactRender": True, "commitSha": head},
            "directory_quality_gates": {"status": "passed", "local_build_skipped": False,
                **{key: True for key in ("dependency_validation", "static_policy", "local_build",
                    "dom_slot_compliance", "publish_surface", "browser", "visual_style")}},
            "directory_quality_verification": {"status": "passed", "commit_sha": head,
                "preview_identity": f"commit:{head}", "resume_generation": 0,
                "task_id": "quality-task-1", "completed_at": "2026-10-14T03:00:00+00:00"}})
        value["scope"]["expected_source_sha"] = base
        signed(value)
        response = self.post(value)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["disposition"], "applied")
        self.assertTrue(self.run.result["browser_verified"])
        self.assertEqual(self.run.approval_state, "not_required")
        self.assertEqual(self.website.capabilities, {})

    @override_settings(CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED=False)
    def test_read_mode_validates_without_projecting_or_creating_business_receipts(self):
        value = envelope(kind="run_snapshot", status="completed", event_id="shadow-1")
        response = Mock(status_code=200, iter_content=Mock(return_value=[__import__("json").dumps(value).encode()]))
        with patch.object(recovery.requests, "get", return_value=response):
            self.assertEqual(recovery.reconcile_setup_delivery_run(self.run,
                base_url="https://worker.fixture", headers={"X-API-KEY": "test-key"}), "shadow_validated")
        self.assertEqual(self.run.status, "running")
        self.assertEqual(self.markers, [])
        self.assertNotIn("latest", self.run.result["delivery_v1"])

    def test_transport_duplicate_remains_valid_after_owner_reserves_another_attempt(self):
        self.post()
        self.operation.payload["attempt"] = 2
        self.run.run_request["operation_attempt"] = 2
        self.assertEqual(self.post().data["disposition"], "duplicate")

    def test_all_generic_proof_bypasses_are_observations(self):
        variants = [{"status": "preview_ready"}, {"status": "adopted"}, {"status": "pr_created"},
            {"adopted": True}, {"adoption_kit_preview_verified": True},
            {"article_system_setup": {"build_verified": True}},
            {"article_system_setup": {"publishing_ready": True}}]
        for index, result in enumerate(variants):
            with self.subTest(result=result):
                value = envelope(version=index + 1, event_id="generic-" + str(index), result=result)
                response = self.post(value)
                self.assertEqual(response.data["disposition"], "observation_only")
                self.assertNotIn("adopted", self.run.result)
                self.assertNotIn("adoption_kit_preview_verified", self.run.result)
                self.assertEqual(self.website.capabilities, {})
        for name in ("verification_summary", "acceptance_summary"):
            value = envelope(version=30 if name == "verification_summary" else 31,
                event_id="generic-" + name)
            value["body"]["state_projection"][name] = {"publishing_ready": True, "browser_verified": True}
            signed(value)
            self.assertEqual(self.post(value).data["disposition"], "observation_only")
            self.assertEqual(self.run.verification_summary, {})
            self.assertEqual(self.run.acceptance_summary, {})

    def test_sparse_negative_progress_preserves_saved_owner_approval_and_accepted_proofs(self):
        self.run.approval_state = "approved"
        self.run.result = {"build_verified": True, "browser_verified": True,
            "directory_quality_verification": {"status": "passed", "commit_sha": "b" * 40},
            "article_system_setup": {"status": "preview_ready", "build_verified": True}}
        self.run.verification_summary = {"accepted": {"commit_sha": "b" * 40}}
        value = envelope(result={"build_verified": False, "browser_verified": False,
            "article_system_setup": {"status": "running", "build_verified": False}})
        self.assertEqual(self.post(value).status_code, 200)
        self.assertEqual(self.run.approval_state, "approved")
        self.assertTrue(self.run.result["build_verified"])
        self.assertTrue(self.run.result["article_system_setup"]["build_verified"])
        self.assertEqual(self.run.verification_summary, {"accepted": {"commit_sha": "b" * 40}})

    def test_generic_scan_observations_cannot_promote_readiness_or_proof_summaries(self):
        self.run.workflow = self.operation.payload["workflow"] = "repo_scan"
        value = envelope(result={"status": "preview_ready", "repository_inventory": {}})
        value["workflow"], value["event_type"] = "repo_scan", "scan_complete"
        value["body"]["state_projection"]["verification_summary"] = {"publishing_ready": True}
        signed(value)
        response = self.post(value)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["disposition"], "observation_only")
        self.assertNotIn("status", self.run.result)
        self.assertEqual(self.run.verification_summary, {})
        self.assertEqual(self.website.capabilities, {})

    def test_actual_scan_and_generation_event_vocabulary_reaches_state_only_receiver(self):
        for index, event in enumerate(("scan_complete", "scan_progress", "generation_failed",
                "generation_blocked", "generation_recovering", "scaffold_complete")):
            with self.subTest(event=event):
                value = envelope(version=index + 1, event_id="emitter-" + str(index))
                if event.startswith("scan_"):
                    value["workflow"] = self.run.workflow = self.operation.payload["workflow"] = "repo_scan"
                else:
                    value["workflow"] = self.run.workflow = self.operation.payload["workflow"] = "article_system_setup"
                value["event_type"] = event
                signed(value)
                self.assertEqual(self.post(value).status_code, 200)
        self.legacy.assert_not_called()
        self.snapshot.assert_not_called()

    def test_proof_type_confusion_and_causal_alias_conflicts_fail_before_writes(self):
        invalid = [{"publish_targets": [{"verification": {"preview_capable": "true"}}]},
            {"publish_targets": [{"verification": {"source_sha": True}}]},
            {"directory_quality_verification": {"task_id": True}},
            {"directory_quality_verification": {"completed_at": []}},
            {"directory_quality_verification": {"resume_generation": False}},
            {"live_preview": {"commitSha": {}}}]
        for item in invalid:
            with self.subTest(item=item):
                self.assertEqual(self.post(envelope(result=item)).status_code, 422)
        for key, value in (("retryable", 0), ("failure_class", "transient"),
                ("error_code", "ANOTHER_FAILURE"), ("failure_fingerprint", "incorrect")):
            failed = envelope(status="failed", result={"article_system_setup": {key: value}})
            self.assertEqual(self.post(failed).status_code, 422)
        self.run.save.assert_not_called()
        self.assertEqual(self.markers, [])

    def test_blank_pending_proof_fields_are_valid_but_do_not_supply_proof(self):
        value = envelope(result={"directory_quality_verification": {"status": "pending",
            "task_id": "", "completed_at": "", "commit_sha": "", "resume_generation": 0},
            "live_preview": {"commitSha": "", "exactRender": False}})
        self.assertEqual(self.post(value).status_code, 200)
        self.assertEqual(self.website.capabilities, {})

    def test_scalar_and_invalid_timestamp_input_is_422_before_writes(self):
        for value in ([], False, "invalid"):
            self.assertEqual(self.post(value).status_code, 422)
        value = deepcopy(self.input)
        value["emitted_at"] = "2026-99-99T03:00:00+00:00"
        signed(value)
        self.assertEqual(self.post(value).status_code, 422)
        self.run.save.assert_not_called()
        self.assertEqual(self.markers, [])

    def test_recursive_json_decoder_failure_returns_typed_422_before_writes(self):
        raw = '[' * 1100 + '0' + ']' * 1100
        request = APIRequestFactory().post("/api/content-factory/callback", raw,
            content_type="application/json", HTTP_X_API_KEY="fixture-service-key")
        response = service_views.ContentFactoryCallbackView.as_view()(request)
        self.assertEqual(response.status_code, 422)
        self.assertIs(response.data["acknowledged"], False)
        self.assertEqual(response.data["fieldErrors"], [{"path": "/"}])
        self.run.save.assert_not_called()
        self.assertEqual(self.markers, [])


@override_settings(CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED=True, ROO_API_KEY="fixture-service-key")
class ProducerConsumerDeliveryChainTests(SimpleTestCase):
    """Consume actual ArtifactStore-produced bytes, also executable in CI."""
    setUp = SetupDeliveryChainTests.setUp
    post = SetupDeliveryChainTests.post
    put = SetupDeliveryChainTests.put

    def configure(self):
        source = os.environ.get("ARTICLES_B5_PRODUCER_FIXTURE") or str(
            Path(__file__).resolve().parents[1] / "contracts" / "setup-delivery-v1.example.json")
        fixture = json.loads(Path(source).read_text())
        callback, scope = fixture["callback"], fixture["callback"]["scope"]
        self.input = callback
        self.organization.domain = scope["domain"]
        self.website.pk = self.website.id = scope["website_connection_id"]
        self.website.github_repo = scope["github_repo"]
        self.website.repository_id, self.website.generation = scope["repository_id"], scope["connection_generation"]
        self.operation.pk, self.operation.connection_id = scope["operation_id"], scope["website_connection_id"]
        self.operation.generation = scope["connection_generation"]
        self.operation.payload = {"attempt": scope["operation_attempt"], "workflow": callback["workflow"],
            "client_request_id": scope["client_request_id"], "run_id": callback["run_id"],
            "setup_delivery_v1": {"schema_version": 1}, "setup_delivery_scope": deepcopy(scope)}
        self.run.run_id, self.run.workflow = callback["run_id"], callback["workflow"]
        self.run.domain, self.run.github_repo = scope["domain"], scope["github_repo"]
        self.run.run_request = {**deepcopy(scope), "setup_delivery_v1": {"schema_version": 1}}
        return fixture

    def test_actual_producer_failure_survives_callback_mirror_and_pure_pull_once(self):
        fixture = self.configure()
        failure = deepcopy(fixture["failure"])
        self.assertEqual(self.post(fixture["callback"]).data["disposition"], "applied")
        self.assertEqual(self.put(fixture["snapshot"]).data["disposition"], "duplicate")
        response = Mock(status_code=200, iter_content=Mock(return_value=[json.dumps(fixture["snapshot"]).encode()]))
        with patch.object(recovery.requests, "get", return_value=response):
            self.assertEqual(recovery.reconcile_setup_delivery_run(self.run,
                base_url="https://worker.example.org", headers={"X-API-KEY": "test-key"}), "no_change")
        self.assertEqual(self.run.result["failure"], failure)
        self.assertEqual(len(self.run.result["delivery_v1"]["failure_occurrences"]), 1)
        self.legacy.assert_not_called()
        self.snapshot.assert_not_called()

    def test_actual_producer_terminal_failure_is_recovered_when_both_pushes_are_dropped(self):
        fixture = self.configure()
        response = Mock(status_code=200, iter_content=Mock(return_value=[json.dumps(fixture["snapshot"]).encode()]))
        with patch.object(recovery.requests, "get", return_value=response):
            self.assertEqual(recovery.reconcile_setup_delivery_run(self.run,
                base_url="https://worker.example.org", headers={"X-API-KEY": "test-key"}), "repaired")
        self.assertEqual(self.run.status, "failed")
        self.assertEqual(self.run.result["failure"], fixture["failure"])
        self.assertEqual(len(self.run.result["delivery_v1"]["failure_occurrences"]), 1)
        self.legacy.assert_not_called()
        self.snapshot.assert_not_called()


class SetupDeliveryEnrollmentTests(SimpleTestCase):
    @override_settings(CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED=True,
        CONTENT_FACTORY_SETUP_DELIVERY_ENROLLED_ORGANIZATIONS=("7",))
    def test_server_allowlist_reserves_original_scope_and_review_child_does_not_inherit_marker(self):
        from .setup_delivery_enrollment import enroll_setup_delivery
        scope = envelope()["scope"]
        website = SimpleNamespace(organization_id=7, organization=SimpleNamespace(domain="fixture.test"), github_repo="fixture/site")
        op = SimpleNamespace(payload={})
        self.assertTrue(enroll_setup_delivery(op, scope, website, "article_system_setup"))
        self.assertEqual(op.payload["setup_delivery_scope"], {key: value for key, value in scope.items() if key != "setup_delivery_v1"})
        child = {**scope, "review_update_operation_id": "review-1"}
        child_op = SimpleNamespace(payload={})
        self.assertFalse(enroll_setup_delivery(child_op, child, website, "article_system_setup"))
        self.assertNotIn("setup_delivery_v1", child)
        self.assertNotIn("setup_delivery_v1", child_op.payload)

    @override_settings(CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED=False)
    def test_enrollment_off_preserves_legacy_shape_but_existing_attempt_stays_enrolled(self):
        from .setup_delivery_enrollment import enroll_setup_delivery
        scope = envelope()["scope"]
        before = deepcopy(scope)
        website = SimpleNamespace(organization_id=7, organization=SimpleNamespace(domain="fixture.test"), github_repo="fixture/site")
        self.assertFalse(enroll_setup_delivery(SimpleNamespace(payload={}), scope, website, "article_system_setup"))
        self.assertEqual(scope, before)
        existing = SimpleNamespace(payload={"setup_delivery_v1": {"schema_version": 1}})
        self.assertTrue(enroll_setup_delivery(existing, scope, website, "article_system_setup"))


class SetupDeliveryContractTests(SimpleTestCase):
    def test_hashes_bind_identity_scope_and_kind(self):
        value = envelope()
        validate_envelope(value)
        for key, replacement in [("kind", "run_snapshot"), ("run_id", "another-run"), ("event_id", "another-event")]:
            changed = deepcopy(value)
            changed[key] = replacement
            self.assertNotEqual(payload_digest(changed), value["payload_sha256"])
        changed = deepcopy(value)
        changed["scope"]["operation_attempt"] = 2
        self.assertNotEqual(state_digest(changed), value["state_sha256"])

    def test_nested_secrets_transport_metadata_and_logs_fail_validation(self):
        for key in ("githubToken", "delivery_v1", "log_excerpt", "access_token", "opaque_source",
                "routeSource", "template_body", "message", "error_excerpt"):
            value = envelope(result={"article_system_setup": {key: "sensitive-fixture"}})
            with self.assertRaises(DeliveryContractError):
                validate_envelope(value)

    def test_delivery_namespace_has_no_digest_self_reference(self):
        value = envelope()
        self.assertEqual(value["state_sha256"], digest({"scope": value["scope"],
            "state_projection": value["body"]["state_projection"]}))
