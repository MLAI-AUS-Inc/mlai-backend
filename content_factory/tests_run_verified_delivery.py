"""Actual retained artifacts, independent canonical bytes and retired writers."""
from copy import deepcopy
import hashlib
import json
from unittest.mock import Mock, patch

from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from workflow_runs.models import ContentFactoryRun
from .run_artifact_observations import ArtifactObservationInvalid, _digest
from .run_ownership import (VERIFIED_DELIVERY_RESULT_KEY, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY,
    bind_admitted_run, current_execution_ticket, reserve_admitted_run, user_principal, wire_identity, owner_receipt)
from .run_verified_delivery import has_verified_delivery, project_verified_delivery, verified_draft_ready
from .self_heal_testing import make_p2_org
from .tests_run_event_authority import FLAGS, batch, advance_ticket
from .founder_journey_testing import ALL_P1_FLAGS_ON


@override_settings(**FLAGS, **ALL_P1_FLAGS_ON, ARTICLES_FREE_FIRST_ARTICLE_ENABLED=True,
    CONTENT_FACTORY_URL="https://cf.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-proof-key",
    ROO_API_KEY="synthetic-observation-key")
class VerifiedDeliveryTests(TransactionTestCase):
    def setUp(self):
        fixture = make_p2_org()
        self.org, self.user = fixture.organization, fixture.owner
        self.key = "verified-original"
        raw = {"domain": self.org.domain, "client_request_id": self.key, "topic": "Retained article",
            "delivery_mode": "content_only", "delivery_mode_confirmed": True, "expected_cost_points": 0}
        from .first_article_entitlement import reserve_entitlement, entitled_authorization
        from .models import OrganizationContentConfig
        self.config = OrganizationContentConfig.objects.get(organization=self.org)
        self.grant = reserve_entitlement(organization=self.org, user=self.user, client_request_id=self.key,
            data=raw, config=self.config)
        self.assertIsNotNone(self.grant)
        raw.update(entitled_authorization(domain=self.org.domain, action="article_generation", ledger=self.grant))
        self.run = bind_admitted_run(reserve_admitted_run(organization=self.org,
            principal=user_principal(self.user.pk, f"mlai_user:{self.user.pk}"), workflow="article_generation", payload=raw),
            "verified-worker")
        self.body = "# Retained article\n\nThe independently readable saved body."
        self.revision = "a" * 64
        self.review = {"revision": self.revision, "previewPending": False,
            "articleExport": {"version": 1, "status": "ready", "runId": self.run.run_id,
                "revision": self.revision, "format": "markdown", "markdown": self.body, "metadata": {}, "media": []}}

    def evidence(self, run=None):
        run = run or self.run
        identity, execution = wire_identity(owner_receipt(run)), current_execution_ticket(run)
        now = timezone.now().isoformat()
        records = []
        for kind, payload in [("article_body_v1", {"format": "markdown", "body": self.body, "revision": self.revision,
                "article_sha256": hashlib.sha256(self.body.encode()).hexdigest()}),
                ("draft_ready_v1", {"version": 1, "revision": self.revision, "at": now})]:
            record = {"schema_version": 1, "run_id": run.run_id, "kind": kind,
                WIRE_REQUEST_KEY: deepcopy(identity), WIRE_EXECUTION_KEY: deepcopy(execution),
                "payload": payload, "payload_sha256": _digest(payload),
                "source_sha256": _digest(payload) if kind == "draft_ready_v1" else "b" * 64, "observed_at": now}
            record["evidence_sha256"] = _digest(record)
            records.append(record)
        return {"schema_version": 1, "run_id": run.run_id, WIRE_REQUEST_KEY: identity,
                WIRE_EXECUTION_KEY: execution, "evidence": records}

    def remote(self, value):
        response = Mock(status_code=200)
        response.iter_content.return_value = [json.dumps(value).encode()]
        return response

    def reads(self, evidence=None, review=None):
        evidence = self.evidence() if evidence is None else evidence
        review = self.review if review is None else review
        return patch("requests.get", side_effect=lambda url, **kwargs:
            self.remote(evidence if url.endswith("/evidence") else review))

    def immutable(self, before):
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for field in ("run_request", "created_at", "updated_at", "last_event_emitted_at", "reconciled_at", "status",
                      "current_step", "approval_state", "billing_amount", "billing_status", "billing_ledger_id"):
            self.assertEqual(after[field], before[field], field)
        self.assertFalse(self.run.steps.exists())

    def test_actual_typed_body_and_ready_are_independently_verified_without_lifecycle_or_clock_writes(self):
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        with self.reads() as get:
            result = project_verified_delivery(self.run.run_id)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(result["verified"], ["article_body_v1", "draft_ready_v1"])
        self.run.refresh_from_db()
        self.assertTrue(has_verified_delivery(self.run))
        self.assertTrue(verified_draft_ready(self.run))
        from .first_article_entitlement import entitlement_payload
        self.assertEqual(entitlement_payload(self.run)["entitlementStatus"], "consumed")
        from .founder_funnel import has_first_draft
        self.assertTrue(has_first_draft(self.org))
        self.immutable(before)

    def test_completed_event_and_package_step_cannot_consume_entitlement_or_mark_first_delivery(self):
        from .run_event_authority import receive_authoritative_run_events
        from .first_article_entitlement import consume_entitlement, entitlement_payload
        from .founder_funnel import has_first_draft
        from .founder_journey_alerts import organization_has_delivered_draft
        payload = batch(self.run, [{"status": "running"}, {"type": "step_started", "step": "package_content_delivery", "attempt": 1},
            {"type": "step_done", "step": "package_content_delivery", "attempt": 1, "status": "completed"}, {"status": "completed"}])
        self.assertEqual(receive_authoritative_run_events(payload).status_code, 200)
        self.run.refresh_from_db()
        self.assertFalse(consume_entitlement(organization=self.org, run=self.run))
        self.assertEqual(entitlement_payload(self.run)["entitlementStatus"], "reserved")
        self.assertFalse(has_verified_delivery(self.run))
        self.assertFalse(has_first_draft(self.org))
        self.assertFalse(organization_has_delivered_draft(self.org.pk))

    def test_mutated_canonical_body_or_revision_holds_every_fact(self):
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for field in ("body", "revision"):
            changed = deepcopy(self.review)
            changed["articleExport"]["markdown" if field == "body" else "revision"] += "changed"
            with self.reads(review=changed), self.assertRaises(ArtifactObservationInvalid):
                project_verified_delivery(self.run.run_id)
            self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)

    def test_old_creation_source_is_not_blessed_by_current_outer_ticket(self):
        old = self.evidence()
        advance_ticket(self.run, generation=1)
        old[WIRE_EXECUTION_KEY] = current_execution_ticket(self.run)
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        with self.reads(evidence=old), self.assertRaises(ArtifactObservationInvalid):
            project_verified_delivery(self.run.run_id)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)

    def test_proven_delivered_benefit_survives_later_failed_ticket_and_malformed_worker_hints(self):
        with self.reads():
            project_verified_delivery(self.run.run_id)
        self.run.refresh_from_db()
        advance_ticket(self.run, generation=1)
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(status="failed")
        self.run.refresh_from_db()
        self.assertTrue(has_verified_delivery(self.run))
        from .first_article_entitlement import _delivered, entitlement_payload
        self.assertTrue(_delivered(self.run))
        self.assertEqual(entitlement_payload(self.run)["entitlementStatus"], "consumed")
        from .founder_funnel import has_first_draft
        self.assertTrue(has_first_draft(self.org))

    def test_owned_legacy_callback_is_intercepted_before_slack_refund_and_ordered_transaction(self):
        from .service_views import ContentFactoryCallbackView
        data = {"event": "generation_failed", "job_id": self.run.run_id, "domain": self.org.domain,
            "generation": 0, "state_version": 1, WIRE_REQUEST_KEY: wire_identity(owner_receipt(self.run)),
            WIRE_EXECUTION_KEY: current_execution_ticket(self.run), "refundable": True}
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        req = APIRequestFactory().post("/callback", data, format="json", HTTP_X_API_KEY="synthetic-observation-key")
        with self.reads(), patch.object(ContentFactoryCallbackView, "_dispatch_ordered_callback") as old:
            response = ContentFactoryCallbackView.as_view()(req)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "observation_only")
        old.assert_not_called()
        self.immutable(before)

    def test_owned_snapshot_mutable_status_steps_request_and_approval_are_ignored(self):
        from .service_views import _apply_run_snapshot, _sync_content_factory_run_snapshot
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        payload = {"workflow": "article_generation", "domain": self.org.domain, "status": "completed",
            "approval_state": "approved", "generation": 0, "state_version": 1, "run_request": {"topic": "worker replacement"},
            "result": {"draft_ready_v1": {"version": 1}}, "step_states": {"write": {"status": "completed"}},
            WIRE_REQUEST_KEY: wire_identity(owner_receipt(self.run)), WIRE_EXECUTION_KEY: current_execution_ticket(self.run)}
        with self.reads():
            response = _apply_run_snapshot(self.run.run_id, payload)
        self.assertEqual(response.status_code, 200)
        self.immutable(before)
        self.run.refresh_from_db()
        verified = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        _sync_content_factory_run_snapshot(run_id=self.run.run_id, data=payload, step_states=payload["step_states"])
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), verified)

    def test_legacy_cancellation_receipt_cannot_project_owned_lifecycle(self):
        from .service_views import _apply_cancellation_receipt
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        response = _apply_cancellation_receipt(self.run.run_id,
            {"domain": self.org.domain, "status": "cancelled", "result": {"cancelled": True}})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)

    def test_reservation_only_wire_keeps_its_original_legacy_observation_boundary(self):
        from .run_owned_observations import maybe_receive_owned_observation
        with override_settings(ARTICLES_RUN_FACT_AUTHORITY_ENABLED=False, ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False):
            run = reserve_admitted_run(organization=self.org,
                principal=user_principal(self.user.pk, f"mlai_user:{self.user.pk}"), workflow="article_generation",
                payload={"domain": self.org.domain, "client_request_id": "reservation-only-boundary"})
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with patch("content_factory.run_verified_delivery.project_verified_delivery") as bridge:
            self.assertIsNone(maybe_receive_owned_observation({"run_id": run.run_id,
                WIRE_REQUEST_KEY: wire_identity(owner_receipt(run)), WIRE_EXECUTION_KEY: current_execution_ticket(run)}))
        bridge.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)

    def test_no_remote_read_inside_sql_and_projection_failure_rolls_history_and_entitlement_back(self):
        with transaction.atomic(), patch("requests.get") as get, self.assertRaises(ArtifactObservationInvalid):
            project_verified_delivery(self.run.run_id)
        get.assert_not_called()
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        with self.reads(), patch("content_factory.first_article_entitlement.consume_entitlement", side_effect=RuntimeError("synthetic effect failure")), \
                self.assertRaises(RuntimeError):
            project_verified_delivery(self.run.run_id)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)

    def test_portable_child_retains_its_real_source_without_acquiring_website_scope(self):
        child_request = {"domain": self.org.domain, "client_request_id": "portable-child-original",
            "topic": "Child retained article", "delivery_mode": "content_only", "delivery_mode_confirmed": True,
            "source_run_id": self.run.run_id}
        child = bind_admitted_run(reserve_admitted_run(organization=self.org,
            principal=user_principal(self.user.pk, f"mlai_user:{self.user.pk}"), workflow="article_revision", payload=child_request),
            "portable-child-worker")
        self.review["articleExport"]["runId"] = child.run_id
        with self.reads(evidence=self.evidence(child)):
            self.assertEqual(project_verified_delivery(child.run_id)["verified"], ["article_body_v1", "draft_ready_v1"])
        child.refresh_from_db()
        self.assertTrue(has_verified_delivery(child))
        self.assertEqual(child.run_request["source_run_id"], self.run.run_id)

    def test_actual_pull_provider_read_projects_only_exact_repo_head_and_reviewed_body(self):
        from .website_models import WebsiteConnection
        from .website_connections import contract_for
        from .website_operations import reserve_workflow_operation, bind_operation_run
        website = WebsiteConnection.objects.create(organization=self.org, authorized_by=self.user,
            github_repo="synthetic/retained", repository_id=42, installation_id="1001", branch="main", verified_sha="c" * 40)
        self.config.website_connection, self.config.github_repo = website, website.github_repo
        self.config.save(update_fields=["website_connection", "github_repo"])
        digest = hashlib.sha256(self.body.encode()).hexdigest()
        request = {**contract_for(website), "domain": self.org.domain, "github_repo": website.github_repo,
            "expected_source_sha": website.verified_sha, "client_request_id": "publish-proof-original",
            "approved_body": self.body, "approved_body_sha256": digest,
            "approval": {"by_user_id": self.user.pk, "at": timezone.now().isoformat()}}
        operation = reserve_workflow_operation(website, workflow="article_generation", payload=request)
        run = bind_admitted_run(reserve_admitted_run(organization=self.org,
            principal=user_principal(self.user.pk, f"mlai_user:{self.user.pk}"), workflow="article_generation", payload=request),
            "publish-proof-worker")
        bind_operation_run(operation, run)
        ContentFactoryRun.objects.filter(pk=run.pk).update(result={**run.result, "article_approval_v1": {
            "approved_by_user_id": self.user.pk, "approved_body_sha256": digest}})
        run.refresh_from_db()
        evidence = self.evidence(run)
        payload = {"kind": "article", "number": 9, "repository_id": 42, "head_repository_id": 42,
            "branch": "mlai/reviewed-article", "base": "main", "head_sha": "d" * 40,
            "pr_url": "https://github.com/synthetic/retained/pull/9", "article_sha256": digest}
        record = {"schema_version": 1, "run_id": run.run_id, "kind": "publish_pr_v1",
            WIRE_REQUEST_KEY: wire_identity(owner_receipt(run)), WIRE_EXECUTION_KEY: current_execution_ticket(run),
            "payload": payload, "payload_sha256": _digest(payload), "source_sha256": "e" * 64,
            "observed_at": timezone.now().isoformat()}
        record["evidence_sha256"] = _digest(record)
        evidence["evidence"].append(record)
        review = deepcopy(self.review)
        review["articleExport"]["runId"] = run.run_id
        pull = {"number": 9, "state": "open", "html_url": payload["pr_url"],
            "head": {"ref": payload["branch"], "sha": payload["head_sha"], "repo": {"id": 42, "full_name": website.github_repo}},
            "base": {"ref": "main", "repo": {"id": 42, "full_name": website.github_repo}}}
        provider = Mock(status_code=200)
        provider.json.return_value = pull
        with self.reads(evidence=evidence, review=review), \
                patch("content_factory.website_connections.verify_repository_head", return_value=None), \
                patch("integrations.services.github_connections.read_bound_repository", return_value=provider) as get:
            self.assertIn("publish_pr_v1", project_verified_delivery(run.run_id)["verified"])
        self.assertEqual(get.call_args.args[1], "https://api.github.com/repos/synthetic/retained/pulls/9")
        run.refresh_from_db()
        self.assertEqual(run.result["publish_pr_v1"], payload)
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        provider.json.return_value = {**pull, "head": {**pull["head"], "sha": "f" * 40}}
        with self.reads(evidence=evidence, review=review), \
                patch("content_factory.website_connections.verify_repository_head", return_value=None), \
                patch("integrations.services.github_connections.read_bound_repository", return_value=provider), \
                self.assertRaises(ArtifactObservationInvalid):
            project_verified_delivery(run.run_id)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)
