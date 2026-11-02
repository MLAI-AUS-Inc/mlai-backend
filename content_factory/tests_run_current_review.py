"""Exact PostgreSQL admission -> real CF review worker -> parent approval wire."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import User
from organizations.models import Organization
from roo.models import Ledger
from workflow_runs.models import ContentFactoryRun
from .run_artifact_observations import ArtifactObservationInvalid, _digest
from .run_ownership import (CURRENT_REVIEW_RESULT_KEY, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY,
    bind_admitted_run, current_execution_ticket, owner_receipt, reserve_admitted_run, user_principal, wire_identity)
from .run_verified_delivery import project_verified_delivery, verified_draft_ready
from .self_heal_testing import make_p2_org
from .tests_run_event_authority import FLAGS
from .founder_journey_testing import ALL_P1_FLAGS_ON


def response(value, status=200):
    remote = Mock(status_code=status, content=b"{}")
    remote.iter_content.side_effect = lambda **kwargs: iter([json.dumps(value).encode()])
    remote.json.side_effect = lambda: deepcopy(value)
    return remote


def project_test_review(run, facts):
    """Lower-layer child fixtures still pass through the actual artifact bridge.

    These deterministic CF/HTTP observations establish their review precondition;
    the paired tests below use the unchanged real CF worker export instead.
    """
    identity, ticket = wire_identity(owner_receipt(run)), current_execution_ticket(run)
    body = "# Retained synthetic article\n\nThe approved article stays unchanged."
    revision = _digest({"run_id": run.run_id, "body": body})
    body_hash = hashlib.sha256(body.encode()).hexdigest()
    quality = deepcopy(facts["article_preview_quality"])
    preview = deepcopy(facts["livePreview"])
    export = {"version": 1, "status": "ready", "runId": run.run_id, "revision": revision,
        "format": "markdown", "markdown": body, "metadata": {}, "media": []}
    review = {"revision": revision, "previewPending": False, "articleExport": export}
    payloads = {"article_body_v1": {"format": "markdown", "body": body, "revision": revision, "article_sha256": body_hash},
        "article_preview_quality_v1": {"version": 1, "revision": revision, "article_sha256": body_hash,
            "generation": ticket["generation"], "article_preview_quality": quality, "live_preview": preview}}
    records = []
    for kind, payload in payloads.items():
        record = {"schema_version": 1, "run_id": run.run_id, WIRE_REQUEST_KEY: identity, WIRE_EXECUTION_KEY: ticket,
            "kind": kind, "payload": payload, "payload_sha256": _digest(payload), "source_sha256": _digest(payload),
            "observed_at": timezone.now().isoformat()}
        record["evidence_sha256"] = _digest(record)
        records.append(record)
    evidence = {"schema_version": 1, "run_id": run.run_id, WIRE_REQUEST_KEY: identity,
        WIRE_EXECUTION_KEY: ticket, "evidence": records}
    metadata = {"schema_version": 1, "run_id": run.run_id, WIRE_REQUEST_KEY: identity, WIRE_EXECUTION_KEY: ticket,
        "generation": ticket["generation"], "revision": revision, "article_sha256": body_hash,
        "article_preview_quality": quality, "livePreview": preview}
    def get(url, **kwargs):
        return response(evidence if url.endswith("/evidence") else metadata if "view=review_metadata" in url else review)
    provider = Mock(status_code=200)
    provider.json.return_value = {"id": ticket["scope"].get("repository_id"), "full_name": run.github_repo,
        "default_branch": "main", "sha": ticket["scope"].get("expected_source_sha")}
    with override_settings(CONTENT_FACTORY_URL="https://cf.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-review-unit-key"), \
            patch("requests.get", side_effect=get), \
            patch("integrations.services.github_connections.read_bound_repository", return_value=provider):
        project_verified_delivery(run.run_id)
    run.refresh_from_db()
    acknowledgement = {"action": "approve", "accepted": True, "run_id": run.run_id, "reviewedRevision": revision,
        WIRE_REQUEST_KEY: identity, WIRE_EXECUTION_KEY: ticket}
    return acknowledgement, review


@override_settings(**FLAGS, **ALL_P1_FLAGS_ON, ARTICLES_FREE_FIRST_ARTICLE_ENABLED=True,
    CONTENT_FACTORY_URL="https://cf.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-paired-key")
class CurrentReviewTests(TransactionTestCase):
    def setUp(self):
        self.fixture = json.loads((Path(__file__).parent / "fixtures" / getattr(self, "fixture_name", "r3_owned_review_approval.json")).read_text())
        original = self.fixture["original_backend_fixture"]
        wire = original["admission"]
        # Keep the actual producer's IDs, while exercising the real backend
        # organisation, owner, grant and admission constructors again.
        create_org, create_user = Organization.objects.create, User.objects.create_user
        get_grant = Ledger.objects.get_or_create
        def grant(**kwargs):
            kwargs["defaults"] = {**kwargs.get("defaults", {}), "id": int(original["run_request"]["roo_points_ledger_id"])}
            return get_grant(**kwargs)
        with patch.object(Organization.objects, "create", side_effect=lambda **kwargs:
                create_org(id=int(wire["organization_id"]), **kwargs)), \
                patch.object(User.objects, "create_user", side_effect=lambda **kwargs:
                    create_user(id=int(wire["principal"]["user_id"]), **kwargs)):
            built = make_p2_org(domain=original["domain"])
        self.org, self.user, self.company = built.organization, built.owner, built.company
        raw = {key: deepcopy(value) for key, value in original["run_request"].items()
            if key not in {WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, "free_domain"} and not key.startswith("roo_points_")}
        from .first_article_entitlement import reserve_entitlement, entitled_authorization
        with patch.object(Ledger.objects, "get_or_create", side_effect=grant):
            ledger = reserve_entitlement(organization=self.org, user=self.user, client_request_id=raw["client_request_id"],
                data=raw, config=built.config)
        self.assertIsNotNone(ledger)
        raw.update(entitled_authorization(domain=self.org.domain, action="article_generation", ledger=ledger))
        self.run = bind_admitted_run(reserve_admitted_run(organization=self.org,
            principal=user_principal(self.user.pk, wire["principal"]["actor_id"]), workflow=wire["workflow"], payload=raw),
            original["run_id"])
        self.assertEqual(self.run.run_request, original["run_request"])
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def reads(self, *, evidence=None, metadata=None):
        fixture = self.fixture
        def get(url, **kwargs):
            if url.endswith("/evidence"):
                return response(fixture["evidence_response"] if evidence is None else evidence)
            if "view=review_metadata" in url:
                return response(fixture["review_metadata_response"] if metadata is None else metadata)
            if "/events" in url and "events_response" in fixture:
                cursor = kwargs.get("params", {}).get("after_seq", 0)
                page = deepcopy(fixture["events_response"])
                if cursor >= page["next_cursor"]:
                    page.update(batches=[], next_cursor=cursor, has_more=False)
                return response(page)
            if url.endswith("/article-review"):
                return response(fixture["article_review_response"])
            raise AssertionError(f"Unexpected read: {url}")
        return patch("requests.get", side_effect=get)

    def test_exact_real_worker_fixture_projects_current_review_and_first_draft_without_lifecycle(self):
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        with self.reads():
            projected = project_verified_delivery(self.run.run_id)
        self.run.refresh_from_db()
        self.assertIn("article_preview_quality_v1", projected["verified"])
        from .article_publish_approval import article_review_identity_is_complete
        self.assertTrue(article_review_identity_is_complete(self.run))
        self.assertTrue(verified_draft_ready(self.run))
        from .first_article_entitlement import entitlement_payload
        self.assertEqual(entitlement_payload(self.run)["entitlementStatus"], "consumed")
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for field in before.keys() - {"result"}:
            self.assertEqual(after[field], before[field], field)

    def test_fresh_quality_omission_revokes_approval_purpose_and_preserves_delivered_benefit(self):
        with self.reads():
            project_verified_delivery(self.run.run_id)
        evidence = deepcopy(self.fixture["evidence_response"])
        evidence["evidence"] = [row for row in evidence["evidence"] if row["kind"] != "article_preview_quality_v1"]
        with self.reads(evidence=evidence):
            project_verified_delivery(self.run.run_id)
        self.run.refresh_from_db()
        from .article_publish_approval import article_review_identity_is_complete
        self.assertFalse(article_review_identity_is_complete(self.run))
        self.assertNotIn(CURRENT_REVIEW_RESULT_KEY, self.run.result)
        self.assertTrue(verified_draft_ready(self.run))

    def test_changed_metadata_or_previous_ticket_never_blesses_current_approval(self):
        metadata = deepcopy(self.fixture["review_metadata_response"])
        metadata["article_preview_quality"]["inputs_sha256"] = "d" * 64
        with self.reads(metadata=metadata), self.assertRaises(ArtifactObservationInvalid):
            project_verified_delivery(self.run.run_id)
        self.run.refresh_from_db()
        self.assertNotIn(CURRENT_REVIEW_RESULT_KEY, self.run.result)

    def test_actual_first_approve_drains_real_history_records_parent_consent_and_replays_without_publication(self):
        from .run_reconciliation import reconcile_authoritative_run
        from .article_publish_approval import article_publish_approval_receipt_matches
        from .founder_draft_views import is_delivered_content_only
        with self.reads():
            self.assertEqual(reconcile_authoritative_run(self.run.run_id, refresh=True)["outcome"], "history_received")
            project_verified_delivery(self.run.run_id)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, self.fixture["source_status"]["status"])
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        original_steps = list(self.run.steps.order_by("pk").values())
        url = f"/api/v1/vibe-marketing/runs/{self.run.run_id}/approve/?company_id={self.company.pk}"
        data = {"reviewedRevision": self.fixture["canonical_export"]["revision"], "auto_merge": True}
        with self.reads(), \
                patch("content_factory.vibe_marketing_views.http_client.request",
                    return_value=response(self.fixture["article_review_response"])), \
                patch("content_factory.vibe_marketing_views.http_client.post",
                    return_value=response(self.fixture["approval_response"])) as post:
            accepted = self.client.post(url, data, format="json")
            replay = self.client.post(url, data, format="json")
        self.assertEqual(accepted.status_code, 202, accepted.data)
        self.assertEqual(replay.status_code, 202, replay.data)
        self.assertEqual(post.call_count, 1)
        self.assertTrue(post.call_args.args[0].endswith("/approve"))
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent[WIRE_REQUEST_KEY], wire_identity(owner_receipt(self.run)))
        self.assertEqual(sent[WIRE_EXECUTION_KEY], current_execution_ticket(self.run))
        self.assertNotIn("auto_merge", sent)
        self.run.refresh_from_db()
        self.assertTrue(article_publish_approval_receipt_matches(self.run))
        self.assertEqual(accepted.data["approvalState"], "approved")
        self.assertEqual(accepted.data["status"], before["status"])
        self.assertTrue(is_delivered_content_only(self.run))
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        self.assertFalse(Ledger.objects.filter(kind="SPEND").exists())
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for field in before.keys() - {"result"}:
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(list(self.run.steps.order_by("pk").values()), original_steps)
        # Exercise the actual R5 selected journey after consent, rather than
        # manually filling the public approval field in a presentation fixture.
        journey_url = f"/api/v1/my-startup/journey/?company_id={self.company.pk}&runId={self.run.run_id}"
        with patch("community_chat.throttles.StartupScopedThrottle.allow_request", return_value=True):
            journey = self.client.get(journey_url)
        self.assertEqual(journey.status_code, 200, journey.data)
        self.assertEqual(journey.data["article"]["approvalState"], "approved")
        self.assertEqual(journey.data["article"]["status"], before["status"])
        self.assertTrue(journey.data["capabilities"]["canCopyDraft"])
        self.assertFalse(journey.data["capabilities"]["canPublish"])
        self.assertEqual(journey.data["stage"], "review")
        self.assertFalse(journey.data["state"] == "working")
        with patch("content_factory.vibe_marketing_views.http_client.request",
                return_value=response(self.fixture["article_review_response"])):
            copied = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/copied/?company_id={self.company.pk}",
                {"revision": self.fixture["canonical_export"]["revision"]}, format="json")
        self.assertEqual(copied.status_code, 200, copied.data)
        with patch("community_chat.throttles.StartupScopedThrottle.allow_request", return_value=True):
            done = self.client.get(journey_url)
        self.assertEqual(done.status_code, 200, done.data)
        self.assertEqual(done.data["stage"], "done")
        self.assertTrue(done.data["capabilities"]["canCopyDraft"])
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        self.assertFalse(Ledger.objects.filter(kind="SPEND").exists())

    def test_actual_daily_automation_command_merges_backend_facts_without_owned_clocks_or_lost_metadata(self):
        from . import vibe_marketing_views as views
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        cost = views.get_content_factory_research_cost_points(self.org.domain, 3)
        def automation(**kwargs):
            current = ContentFactoryRun.objects.get(pk=self.run.pk)
            ContentFactoryRun.objects.filter(pk=current.pk).update(result={**current.result,
                "concurrent_provider_fact": {"retained": True}})
            return None, []
        with patch.object(views, "_profile_checks", return_value={"dailyAutomation": {"passed": True}}), \
                patch("integrations.services.notification_channels.ensure_research_automation_for_org", side_effect=automation), \
                patch.object(views.http_client, "post") as remote:
            enabled = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/enable-daily-automation/?company_id={self.company.pk}",
                {"expectedCostPoints": cost}, format="json")
        self.assertEqual(enabled.status_code, 200, enabled.data)
        remote.assert_not_called()
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["concurrent_provider_fact"], {"retained": True})
        self.assertIn("daily_automation_enabled_at", self.run.result)
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for field in before.keys() - {"result"}:
            self.assertEqual(after[field], before[field], field)

    def test_actual_canonical_deny_records_intent_before_http_without_applying_acknowledgement_state(self):
        from .run_reconciliation import reconcile_authoritative_run
        from .run_ownership import user_stop_intent
        with self.reads():
            reconcile_authoritative_run(self.run.run_id, refresh=True)
            project_verified_delivery(self.run.run_id)
        self.run.refresh_from_db()
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        def deny(url, **kwargs):
            self.run.refresh_from_db()
            self.assertTrue(user_stop_intent(self.run))
            return response({"run_id": self.run.run_id, "status": "denied", "approval_state": "denied"})
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.post", side_effect=deny):
            denied = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/deny/?company_id={self.company.pk}",
                {}, format="json")
        self.assertEqual(denied.status_code, 202, denied.data)
        self.run.refresh_from_db()
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for field in before.keys() - {"result"}:
            self.assertEqual(after[field], before[field], field)
        self.assertTrue(verified_draft_ready(self.run))


class PortableCurrentReviewTests(CurrentReviewTests):
    fixture_name = "r3_owned_portable_review_approval.json"

    def test_actual_pipeline_delivers_copy_ready_before_preview_quality_exists(self):
        with self.reads(evidence=self.fixture["pre_preview_evidence_response"]):
            project_verified_delivery(self.run.run_id)
        self.run.refresh_from_db()
        self.assertTrue(verified_draft_ready(self.run))
        from .article_publish_approval import article_review_identity_is_complete
        self.assertFalse(article_review_identity_is_complete(self.run))
        from .founder_funnel import has_first_draft
        self.assertTrue(has_first_draft(self.org))
        with patch("content_factory.vibe_marketing_views.http_client.request", return_value=response(self.fixture["article_review_response"])):
            copied = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/copied/?company_id={self.company.pk}",
                {"revision": self.fixture["canonical_export"]["revision"]}, format="json")
        self.assertEqual(copied.status_code, 200, copied.data)
        self.assertFalse(Ledger.objects.filter(kind="SPEND").exists())

    def test_actual_preview_post_and_cleanup_forward_exact_current_echoes_and_keep_owned_clocks(self):
        with self.reads(evidence=self.fixture["pre_preview_evidence_response"]):
            project_verified_delivery(self.run.run_id)
        self.run.refresh_from_db()
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        url = f"/api/v1/vibe-marketing/runs/{self.run.run_id}/live-preview/?company_id={self.company.pk}"
        with patch("content_factory.vibe_marketing_views.http_client.post", return_value=response(self.fixture["preview_response"])) as post:
            started = self.client.post(url, {"force": False, "localRepoPath": "/browser/forgery",
                WIRE_REQUEST_KEY: {"forged": True}, WIRE_EXECUTION_KEY: {"forged": True}}, format="json")
        self.assertEqual(started.status_code, 200, started.data)
        self.assertEqual(post.call_count, 1)
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent[WIRE_REQUEST_KEY], self.fixture["preview_request"][WIRE_REQUEST_KEY])
        self.assertEqual(sent[WIRE_EXECUTION_KEY], self.fixture["preview_request"][WIRE_EXECUTION_KEY])
        self.assertEqual(sent["local_repo_path"], "")
        self.assertNotIn("github_token", sent)
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["livePreview"]["status"], self.fixture["preview_response"]["status"])
        from .run_user_stop import record_user_denial
        record_user_denial(self.run, user=self.user, actor_id=f"mlai_user:{self.user.pk}")
        with patch("content_factory.vibe_marketing_views.http_client.delete", return_value=response({"status": "stopped", "available": False})) as delete:
            stopped = self.client.delete(url)
        self.assertEqual(stopped.status_code, 200, stopped.data)
        self.assertEqual(delete.call_args.kwargs["json"][WIRE_REQUEST_KEY], wire_identity(owner_receipt(self.run)))
        self.assertEqual(delete.call_args.kwargs["json"][WIRE_EXECUTION_KEY], current_execution_ticket(self.run))
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for field in before.keys() - {"result"}:
            self.assertEqual(after[field], before[field], field)
        self.assertTrue(verified_draft_ready(self.run))
