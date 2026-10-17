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


@override_settings(CONTENT_FACTORY_ARTICLE_DELIVERY_V1_APPLY_ENABLED=True,ROO_API_KEY="fixture-service-key")
class ArticleDeliveryChainTests(SimpleTestCase):
    def setUp(self):
        self.chain=SetupDeliveryChainTests("runTest")
        self.chain.setUp();self.addCleanup(self.chain.doCleanups)
        self.input=signed({**envelope(status="failed"),"workflow":"direct_generate"})
        goal="44444444-4444-4444-8444-444444444444"
        original={**self.input["scope"],"article_delivery_v1":{"schema_version":1},
            "first_article_goal_id":goal,"first_article_goal_operation_id":goal}
        self.chain.operation.payload={**self.chain.operation.payload,"workflow":"article_generation",
            "article_delivery_v1":{"schema_version":1},"article_delivery_workflow":"direct_generate",
            "article_delivery_scope":deepcopy(self.input["scope"]),"article_delivery_request":deepcopy(original)}
        self.chain.operation.payload.pop("setup_delivery_v1")
        self.chain.run.workflow="article_generation";self.chain.run.run_request=original
        self.chain.run.result={"article_quality":{"saved_identity":"original"},"user_action_count":0}

    def test_failed_article_commits_same_cause_replay_and_keeps_rich_saved_evidence(self):
        result=self.chain.post(self.input)
        self.assertEqual(result.status_code,200);self.assertEqual(result.data["disposition"],"applied")
        self.assertEqual(self.chain.run.status,"failed")
        self.assertEqual(self.chain.run.result["failure"],self.input["body"]["state_projection"]["failure"])
        self.assertEqual(self.chain.run.result["article_quality"],{"saved_identity":"original"})
        self.assertEqual(self.chain.run.result["user_action_count"],0)
        self.assertEqual(self.chain.post(self.input).data["disposition"],"duplicate")
        self.chain.legacy.assert_not_called();self.chain.snapshot.assert_not_called()

    def test_setup_stream_cannot_receive_article_and_foreign_goal_cannot_bind(self):
        self.chain.operation.payload.pop("article_delivery_v1")
        response=self.chain.post(self.input)
        self.assertEqual(response.status_code,409);self.assertFalse(response.data["acknowledged"])
        self.chain.operation.payload["article_delivery_v1"]={"schema_version":1}
        self.chain.run.run_request["first_article_goal_id"]="other-goal"
        response=self.chain.post(self.input)
        self.assertEqual(response.status_code,409);self.assertFalse(response.data["acknowledged"])
        self.assertEqual(self.chain.run.status,"running")

    def test_article_apply_flag_does_not_borrow_enabled_setup_flag(self):
        with override_settings(CONTENT_FACTORY_ARTICLE_DELIVERY_V1_APPLY_ENABLED=False,
            CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED=True):
            response=self.chain.post(self.input)
        self.assertEqual(response.status_code,503);self.assertEqual(self.chain.run.status,"running")

    def test_worker_snapshot_cannot_enroll_or_replace_article_marker(self):
        merged=service_views._merge_django_owned_run_request({}, {"article_delivery_v1":{"schema_version":1}})
        self.assertNotIn("article_delivery_v1",merged)
        original=self.chain.run.run_request
        merged=service_views._merge_django_owned_run_request(original,{"article_delivery_v1":{"schema_version":2}})
        self.assertEqual(merged["article_delivery_v1"],{"schema_version":1})


class SetupDeliveryContractTests(SimpleTestCase):
    def test_usage_schema_accepts_explicit_unknown_numbers_and_rejects_strings(self):
        from .website_goal_budget import COUNTERS
        report={"version":1,"run_id":"setup-1","generation":0,"state_version":1,
            **{key:0 for key in COUNTERS}}
        for value in (0,None):
            with self.subTest(value=value):
                report["model_usd"]=value
                self.assertEqual(validate_envelope(envelope(result={"usage":report}))["body"]["state_projection"]["result"]["usage"]["model_usd"],value)
        report["model_usd"]="unknown"
        with self.assertRaises(DeliveryContractError):validate_envelope(envelope(result={"usage":report}))

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


class WorkflowOrderFormattingTests(SimpleTestCase):
    """Actual v1 receiver paths; ORM and transaction boundaries only are replaced."""
    ORDER = ["fetch_org_config", "generate_directory_sources", "draft_section:one", "verify_build"]

    def value(self, *, version=1, event="order-event", order=None):
        from .workflow_order import KEY
        order = self.ORDER if order is None else order
        value = envelope(version=version, event_id=event)
        projection = value["body"]["state_projection"]
        projection["current_step"] = order[0]
        projection["step_states"] = {key: {"status": "pending", "required": True, "attempts": 0}
            for key in sorted(order)}
        projection["result"] = {"article_system_setup": {KEY: {"version": 1, "steps": list(order)}}}
        return signed(value)

    def chain(self, *, admitted=True):
        case = SetupDeliveryChainTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        if admitted:
            case.run.run_request["workflow_order_v1"] = {"version": 1}
            case.operation.payload["setup_delivery_request"] = deepcopy(case.run.run_request)
        return case

    @override_settings(CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED=True, ROO_API_KEY="fixture-service-key")
    def test_current_apply_orders_rows_and_run_array_but_replays_and_stale_events_do_not_project(self):
        case, value = self.chain(), self.value(version=5)
        response = case.post(value)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(case.run.step_order, self.ORDER)
        self.assertEqual([(call.kwargs["step_key"], call.kwargs["defaults"]["display_order"])
            for call in case.steps.call_args_list], list(zip(self.ORDER, range(len(self.ORDER)))))
        self.assertTrue(any("step_order" in call.kwargs["update_fields"] for call in case.run.save.call_args_list))
        with patch("content_factory.workflow_order.received_order", side_effect=AssertionError("non-current projection")):
            self.assertEqual(case.post(value).data["disposition"], "duplicate")
            older = self.value(version=4, event="older", order=list(reversed(self.ORDER)))
            self.assertEqual(case.post(older).data["disposition"], "ignored_stale")
        self.assertEqual(case.run.step_order, self.ORDER)
        snapshot = deepcopy(value)
        snapshot.update(kind="run_snapshot", event_id="order-mirror", event_type=None)
        self.assertEqual(case.put(signed(snapshot)).data["disposition"], "duplicate")

    @override_settings(CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED=True, ROO_API_KEY="fixture-service-key")
    def test_absent_original_admission_cannot_be_invented_by_worker_metadata(self):
        for source in ("none", "run_only", "operation_only"):
            with self.subTest(source=source):
                case = self.chain(admitted=False)
                if source == "run_only": case.run.run_request["workflow_order_v1"] = {"version": 1}
                if source == "operation_only":
                    case.operation.payload["setup_delivery_request"] = {"workflow_order_v1": {"version": 1}}
                self.assertEqual(case.post(self.value()).status_code, 200)
                self.assertEqual(case.run.step_order, [])
                case.doCleanups()

    @override_settings(CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED=True, ROO_API_KEY="fixture-service-key")
    def test_invalid_optional_order_never_blocks_business_delivery_or_grants_proof(self):
        from .workflow_order import KEY
        invalid = [None, {}, {"version": True, "steps": self.ORDER}, {"version": 2, "steps": self.ORDER},
            {"version": 1, "steps": self.ORDER[:-1]}, {"version": 1, "steps": [*self.ORDER, self.ORDER[0]]},
            {"version": 1, "steps": [*self.ORDER, "unknown"]}, {"version": 1, "steps": [1]},
            {"version": 1, "steps": self.ORDER, "approved": True}]
        for metadata in invalid:
            with self.subTest(metadata=metadata):
                case, value = self.chain(), self.value()
                value["body"]["state_projection"]["result"]["article_system_setup"][KEY] = metadata
                response = case.post(signed(value))
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(response.data["disposition"], "applied")
                self.assertEqual(case.run.step_order, [])
                self.assertNotEqual(case.run.approval_state, "approved")
                self.assertEqual(case.run.verification_summary, {})
                case.doCleanups()

    def test_shared_bounds_and_version_validation(self):
        from .workflow_order import validated_order
        for count in (0, 512, 513):
            order = [f"step_{i}" for i in range(count)]
            result = validated_order({"version": 1, "steps": order}, dict.fromkeys(order))
            self.assertEqual(result is not None, count <= 512)
        self.assertIsNone(validated_order({"version": 1, "steps": ["a" * 121]}, {"a" * 121: {}}))

    @override_settings(CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED=True,
        CONTENT_FACTORY_SETUP_DELIVERY_ENROLLED_ORGANIZATIONS=("7",),
        CONTENT_FACTORY_WORKFLOW_ORDER_FORMATTING_ADMISSION_ENABLED=True)
    def test_only_new_owned_b5_runs_admit_formatting_and_both_presence_choices_stay_sticky(self):
        from .setup_delivery_enrollment import enroll_setup_delivery
        scope = envelope()["scope"]
        website = SimpleNamespace(organization_id=7, organization=SimpleNamespace(domain="fixture.test"), github_repo="fixture/site")
        op, payload = SimpleNamespace(payload={}), deepcopy(scope)
        self.assertTrue(enroll_setup_delivery(op, payload, website, "article_system_setup", new_run=True))
        self.assertEqual(payload["workflow_order_v1"], {"version": 1})
        with override_settings(CONTENT_FACTORY_WORKFLOW_ORDER_FORMATTING_ADMISSION_ENABLED=False):
            self.assertTrue(enroll_setup_delivery(op, payload, website, "article_system_setup"))
            self.assertEqual(payload["workflow_order_v1"], {"version": 1})
        historical = SimpleNamespace(payload={"setup_delivery_v1": {"schema_version": 1},
            "setup_delivery_request": {**scope, "setup_delivery_v1": {"schema_version": 1}}})
        old_payload = {**scope, "workflow_order_v1": {"version": 1}}
        self.assertTrue(enroll_setup_delivery(historical, old_payload, website, "article_system_setup"))
        self.assertNotIn("workflow_order_v1", old_payload)
        self.assertNotIn("workflow_order_v1", historical.payload["setup_delivery_request"])
        with override_settings(CONTENT_FACTORY_WORKFLOW_ORDER_FORMATTING_ADMISSION_ENABLED=False):
            default_op, default_payload = SimpleNamespace(payload={}), deepcopy(scope)
            self.assertTrue(enroll_setup_delivery(default_op, default_payload, website, "article_system_setup", new_run=True))
            self.assertNotIn("workflow_order_v1", default_payload)
        review = {**scope, "workflow_order_v1": {"version": 1}, "feedback_batch_id": "independent"}
        self.assertFalse(enroll_setup_delivery(SimpleNamespace(payload={}), review, website, "article_system_setup", new_run=True))
        self.assertNotIn("workflow_order_v1", review)
        with override_settings(CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED=False):
            denied = {**scope, "workflow_order_v1": {"version": 1}}
            self.assertFalse(enroll_setup_delivery(SimpleNamespace(payload={}), denied, website, "article_system_setup", new_run=True))
            self.assertNotIn("workflow_order_v1", denied)

    def test_worker_merge_never_enrolls_replaces_or_removes_original_formatting_marker(self):
        merge = service_views._merge_django_owned_run_request
        self.assertNotIn("workflow_order_v1", merge({}, {"workflow_order_v1": {"version": 1}}))
        original = {"workflow_order_v1": {"version": 1}}
        self.assertEqual(merge(original, {}), original)
        self.assertEqual(merge(original, {"workflow_order_v1": {"version": 2}}), original)

    @override_settings(CONTENT_FACTORY_ARTICLE_DELIVERY_V1_APPLY_ENABLED=True, ROO_API_KEY="fixture-service-key")
    def test_article_stream_reads_raw_order_without_granting_or_replacing_article_evidence(self):
        from .workflow_order import KEY
        for workflow in ("direct_generate", "confirmed_topic"):
            with self.subTest(workflow=workflow):
                article = ArticleDeliveryChainTests("runTest")
                article.setUp()
                try:
                    case, value = article.chain, deepcopy(article.input)
                    case.operation.payload["article_delivery_workflow"] = value["workflow"] = workflow
                    if workflow == "confirmed_topic":
                        case.operation.payload["article_delivery_request"]["source_run_id"] = "topic-source"
                        case.run.run_request["source_run_id"] = "topic-source"
                    for request in (case.operation.payload["article_delivery_request"], case.run.run_request):
                        request["workflow_order_v1"] = {"version": 1}
                    projection = value["body"]["state_projection"]
                    projection["step_states"] = {key: {"status": "pending", "required": True, "attempts": 0}
                        for key in sorted(self.ORDER)}
                    projection["result"]["article_system_setup"] = {KEY: {"version": 1, "steps": self.ORDER}}
                    response = case.post(signed(value))
                    self.assertEqual(response.status_code, 200, response.data)
                    self.assertEqual(case.run.step_order, self.ORDER)
                    self.assertEqual(case.run.result["article_quality"], {"saved_identity": "original"})
                    self.assertNotIn("article_system_setup", case.run.result)
                    self.assertNotEqual(case.run.approval_state, "approved")
                    self.assertEqual(case.post(value).data["disposition"], "duplicate")
                finally:
                    article.doCleanups()

    @override_settings(CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED=True, ROO_API_KEY="fixture-service-key")
    def test_step_write_failure_rolls_back_order_with_state_and_receipt_then_same_bytes_recover(self):
        from contextlib import contextmanager
        case, value = self.chain(), self.value()
        rows, attempts = {}, [0]
        @contextmanager
        def atomic():
            fields = ("status", "current_step", "step_order", "result", "approval_state", "resume_available",
                "verification_summary", "acceptance_summary", "last_event_emitted_at")
            before = {key: deepcopy(getattr(case.run, key)) for key in fields}
            before_rows = deepcopy(rows)
            try:
                yield
            except Exception:
                for key, item in before.items(): setattr(case.run, key, item)
                rows.clear(); rows.update(before_rows)
                raise
        def write(**kwargs):
            attempts[0] += 1
            if attempts[0] == 2: raise RuntimeError("synthetic database write failure")
            rows[kwargs["step_key"]] = deepcopy(kwargs["defaults"])
        with patch.object(receiver.transaction, "atomic", side_effect=lambda: atomic()), patch.object(
                receiver.ContentFactoryRunStep.objects, "update_or_create", side_effect=write):
            with self.assertRaisesRegex(RuntimeError, "synthetic database"):
                case.post(value)
            self.assertEqual(case.run.step_order, [])
            self.assertEqual(rows, {})
            receipt = case.run.result["delivery_v1"]["receipts"][value["event_id"]]
            self.assertIsNone(receipt["processed_at"])
            future = timezone.now() + timedelta(seconds=121)
            with patch.object(receiver.timezone, "now", return_value=future):
                self.assertEqual(case.post(value).data["disposition"], "applied")
        self.assertEqual(case.run.step_order, self.ORDER)
        self.assertEqual([key for key, row in sorted(rows.items(), key=lambda item: item[1]["display_order"])], self.ORDER)

    @override_settings(CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED=True, ROO_API_KEY="fixture-service-key")
    def test_higher_absent_or_invalid_metadata_clears_stale_order_in_actual_public_rows(self):
        from .workflow_order import KEY
        from .vibe_marketing_views import _serialize_run
        case = self.chain()
        self.assertEqual(case.post(self.value()).data["disposition"], "applied")
        self.assertEqual(case.run.step_order, self.ORDER)
        current = [step for step in self.ORDER if step != "draft_section:one"]
        for version, metadata in ((2, None), (3, {"version": 2, "steps": current}),
                (4, {"version": 1, "steps": [*current, current[0]]})):
            value = self.value(version=version, event=f"missing-{version}", order=current)
            setup = value["body"]["state_projection"]["result"]["article_system_setup"]
            if metadata is None: setup.pop(KEY)
            else: setup[KEY] = metadata
            self.assertEqual(case.post(signed(value)).data["disposition"], "applied")
            self.assertEqual(case.run.step_order, [])
            self.assertFalse(case.run.result["delivery_v1"]["workflow_order"]["available"])
            persisted = {}
            for i, call in enumerate(case.steps.call_args_list):
                fields = {"step_key": call.kwargs["step_key"], "message": "", "error": "", "artifacts": [],
                    "started_at": None, "completed_at": None, "id": i, **call.kwargs["defaults"]}
                persisted[fields["step_key"]] = SimpleNamespace(**fields)
            relation = SimpleNamespace(all=lambda: list(persisted.values()),
                order_by=lambda *args: sorted(persisted.values(), key=lambda row: (row.display_order, row.id)))
            with patch.object(type(case.run), "steps", PropertyMock(return_value=relation)):
                case.run._bootstrap_component_comments = []
                case.run.created_at = case.run.updated_at = timezone.now()
                public = _serialize_run(case.run)
            self.assertEqual(public["stepOrder"], [])
            self.assertEqual([row["key"] for row in public["steps"]], sorted(current))
            self.assertNotIn("draft_section:one", [row["key"] for row in public["steps"]])

    def test_optional_public_row_filter_falls_through_unknown_or_malformed_legacy_json(self):
        from .vibe_marketing_views import _serialize_run_steps
        case = self.chain(admitted=False)
        row = SimpleNamespace(step_key="legacy_step", required=True, status="pending", attempts=0,
            message="", error="", artifacts=[], started_at=None, completed_at=None)
        relation = SimpleNamespace(order_by=lambda *args: [row])
        malformed = [None, ["truthy"], "truthy", 7, {"delivery_v1": ["truthy"]},
            {"delivery_v1": {"workflow_order": "truthy"}},
            {"delivery_v1": {"workflow_order": {"version": 2, "step_keys": []}}},
            {"delivery_v1": {"workflow_order": {"version": 1, "available": False,
                "step_keys": [], "execution": None}, "latest": {"execution": None}}},
            {"delivery_v1": {"workflow_order": {"version": 1, "available": False,
                "step_keys": [], "execution": [0, 1]}, "latest": "truthy"}}]
        with patch.object(type(case.run), "steps", PropertyMock(return_value=relation)):
            for request in ({}, {"workflow_order_v1": {"version": 1}}):
                for result in malformed:
                    with self.subTest(request=request, result=result):
                        case.run.run_request, case.run.result = request, result
                        self.assertEqual([item["key"] for item in _serialize_run_steps(case.run)], ["legacy_step"])

    @override_settings(CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED=True, ROO_API_KEY="fixture-service-key")
    def test_malformed_saved_request_remains_an_upstream_scope_rejection_not_a_formatting_error(self):
        from .workflow_order import admitted, original_admission
        for malformed in (["not-a-mapping"], "not-a-mapping", 7):
            with self.subTest(malformed=malformed):
                self.assertFalse(admitted(malformed))
                case = self.chain()
                case.run.run_request = malformed
                self.assertFalse(original_admission(case.run, case.operation, self.value()))
                response = case.post(self.value())
                self.assertEqual(response.status_code, 409, response.data)
                self.assertEqual(response.data["code"], "website_scope_mismatch")
                case.steps.assert_not_called()
                case.doCleanups()
@override_settings(CONTENT_FACTORY_ARTICLE_DELIVERY_V1_APPLY_ENABLED=True)
class ArticleDeliveryEnrollmentWorkflowTests(SimpleTestCase):
    """Fresh server reservations must accept their actual worker's wire name."""

    def setUp(self):
        self.goal_id = "44444444-4444-4444-8444-444444444444"
        self.website = SimpleNamespace(organization_id=7, organization=SimpleNamespace(domain="fixture.test"),
            github_repo="fixture/site")
        self.parent = SimpleNamespace(pk=self.goal_id, receipt={"goal": {"current_source_sha": "a" * 40}})
        self.manager = Mock()
        self.manager.select_related.return_value.filter.return_value.first.return_value = self.parent
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("content_factory.website_models.WebsiteConnectionOperation.objects", self.manager))
        self.stack.enter_context(patch("content_factory.website_article_recovery.enabled", return_value=True))

    def request(self, *, source=False):
        return {**envelope()["scope"], "first_article_goal_id": self.goal_id,
            "first_article_goal_operation_id": self.goal_id, "expected_source_sha": "a" * 40,
            **({"source_run_id": "original-paid-article"} if source else {})}

    def test_new_reservations_map_backend_workflow_and_source_to_actual_worker(self):
        from .setup_delivery_enrollment import enroll_article_delivery, delivery_workflow
        for workflow, source, expected in (
            ("article_publish", True, "direct_generate"), ("publish_article", True, "direct_generate"),
            ("article_publish", False, "direct_generate"), ("publish_article", False, "direct_generate"),
            ("article_generation", True, "confirmed_topic"), ("article_generation", False, "direct_generate"),
            ("direct_generate", True, "direct_generate"), ("confirmed_topic", False, "confirmed_topic"),
            ("confirmed_topic", True, "confirmed_topic")):
            with self.subTest(workflow=workflow, source=source):
                payload, operation = self.request(source=source), SimpleNamespace(payload={})
                self.assertTrue(enroll_article_delivery(operation, payload, self.website, workflow))
                self.assertEqual(operation.payload["article_delivery_workflow"], expected)
                self.assertEqual(delivery_workflow(SimpleNamespace(workflow=workflow, run_request=payload)), expected)
                self.assertEqual(operation.payload["article_delivery_request"]["source_run_id"] if source else None,
                    "original-paid-article" if source else None)

    def test_existing_stream_keeps_its_frozen_wire_even_when_new_enrollment_is_disabled(self):
        from .setup_delivery_enrollment import enroll_article_delivery
        payload, operation = self.request(source=True), SimpleNamespace(payload={})
        self.assertTrue(enroll_article_delivery(operation, payload, self.website, "article_generation"))
        with override_settings(CONTENT_FACTORY_ARTICLE_DELIVERY_V1_APPLY_ENABLED=False):
            self.assertTrue(enroll_article_delivery(operation, payload, self.website, "article_generation"))
        self.assertEqual(operation.payload["article_delivery_workflow"], "confirmed_topic")
        changed = {**payload, "expected_source_sha": "b" * 40}
        from .website_contract import WebsiteAuthorityError
        with self.assertRaises(WebsiteAuthorityError):
            enroll_article_delivery(operation, changed, self.website, "article_publish")
        self.assertEqual(operation.payload["article_delivery_workflow"], "confirmed_topic")

    def test_wrong_goal_or_source_cannot_create_a_new_publication_stream(self):
        from .setup_delivery_enrollment import enroll_article_delivery
        for changes in ({"first_article_goal_id": "foreign"}, {"expected_source_sha": "b" * 40}):
            payload, operation = {**self.request(source=True), **changes}, SimpleNamespace(payload={})
            self.assertFalse(enroll_article_delivery(operation, payload, self.website, "article_publish"))
            self.assertNotIn("article_delivery_v1", operation.payload)
