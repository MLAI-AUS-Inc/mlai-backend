"""New publication children retain original review/spend and current authority."""
from copy import deepcopy
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from roo.models import Ledger
from workflow_runs.models import ContentFactoryRun
from .self_heal_testing import make_p2_org
from .tests_r3_revision_child_admission import FLAGS
from .tests_article_publish_approval_unit import _review_run
from .run_ownership import (BUDGET_KEY, WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, RunOwnershipConflict,
    reserve_admitted_run, owner_receipt, user_principal)
from .run_revision_child_admission import prepare_publish_child
from .website_connections import contract_for, authority_guard, owner_operation_scope
from .website_models import WebsiteConnection, WebsiteConnectionTarget, WebsiteConnectionOperation
from .website_operations import reserve_workflow_operation, bind_operation_run
from .article_publish_approval import article_review_identity
from .vibe_marketing_views import _record_article_publish_approval_receipt, VibeMarketingRunControlView


@override_settings(**FLAGS, ARTICLES_PUBLISHING_INPUTS_V3_ENABLED=False)
class PublishChildAdmissionTests(TransactionTestCase):
    def setUp(self):
        self.fixture = make_p2_org()
        self.org, self.user, self.config = self.fixture.organization, self.fixture.owner, self.fixture.config
        self.connection = WebsiteConnection.objects.create(organization=self.org, authorized_by=self.user,
            github_repo="synthetic/site", repository_id=42, installation_id="1001", branch="main", verified_sha="a" * 40,
            last_verified_at=timezone.now(), capabilities={"publishingReady": True})
        contract = {"route_template": "/articles/{slug}", "contract_digest": "c" * 64, "live_marker": {"value": "d" * 64}}
        self.target = WebsiteConnectionTarget.objects.create(connection=self.connection, generation=1,
            target_key="articles", source_sha="a" * 40, verified_at=timezone.now(),
            capabilities={"publishingReady": True}, contract=contract)
        self.config.website_connection, self.config.github_repo = self.connection, self.connection.github_repo
        self.config.default_publish_target_id = "articles"
        self.config.save(update_fields=["website_connection", "github_repo", "default_publish_target_id"])
        WebsiteConnectionOperation.objects.create(connection=self.connection, generation=1, action="deployment-verify",
            state="completed", idempotency_key="synthetic-live-deployment", payload={"source_sha": "a" * 40, "target_id": "articles"},
            receipt={"status": "passed", "source_sha": "a" * 40, "connection_generation": 1, "target_id": "articles",
                "contract_digest": "c" * 64, "artifact_digest": "d" * 64, "public_url": f"https://{self.org.domain}/articles",
                "checked_at": timezone.now().isoformat()})
        self.charge = Ledger.objects.create(user=self.user, kind="SPEND", source="CONTENT_FACTORY", delta=-5,
            idempotency_key="synthetic-original-article-spend", reference_type="ARTICLE_GENERATION")
        request = {**contract_for(self.connection), "domain": self.org.domain, "client_request_id": "approved-publication-source",
            "expected_source_sha": "a" * 40, "topic": "Keep the approved article", "article_slug": "approved-article",
            "delivery_mode": "review_draft", "delivery_mode_confirmed": True,
            "roo_points_ledger_id": str(self.charge.pk), "roo_points_cost": 5, "roo_points_billing_status": "charged"}
        with authority_guard(request, action="read"), owner_operation_scope(request):
            operation = reserve_workflow_operation(self.connection, workflow="article_generation", payload=request)
        self.parent = reserve_admitted_run(organization=self.org, principal=user_principal(self.user.pk, f"mlai_user:{self.user.pk}"),
            workflow="article_generation", payload=request)
        bind_operation_run(operation, self.parent)
        from .tests_run_event_authority import batch
        from .run_event_authority import receive_authoritative_run_events
        from .tests_run_current_review import project_test_review
        self.assertEqual(receive_authoritative_run_events(batch(self.parent,
            [{"status": "running"}, {"status": "completed"}])).status_code, 200)
        self.parent.refresh_from_db()
        accepted, canonical = project_test_review(self.parent, _review_run().result)
        self.assertTrue(_record_article_publish_approval_receipt(self.parent, actor_id=f"mlai_user:{self.user.pk}",
            expected_identity=article_review_identity(self.parent), accepted_control=accepted, user=self.user))
        self.review = {**canonical, "approved": True, "previewPending": False, "previewCommit": "a" * 40, "fields": []}
        # Only provider observations are replaced. Consent, source, destination,
        # role, canonical review/approval and admission are the real functions.
        for name, value in [("verify_repository_native_target", None), ("verify_repository_head", "a" * 40)]:
            p = patch("content_factory.website_connections." + name, side_effect=lambda *a, **k: value)
            p.start(); self.addCleanup(p.stop)
        remote = patch("content_factory.article_review_views.remote_review", return_value=self.review)
        self.remote = remote.start(); self.addCleanup(remote.stop)

    def test_new_fixed_child_has_separate_cap_and_original_charge_before_http_and_exact_retry(self):
        first, second = {"auto_merge": False}, {"auto_merge": False}
        before = ContentFactoryRun.objects.values().get(pk=self.parent.pk)
        money = list(Ledger.objects.values())
        child = prepare_publish_child(run=self.parent, user=self.user, payload=first, action="promote-bundle")
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_CONTROL_MODEL_USD="9.00"):
            again = prepare_publish_child(run=self.parent, user=self.user, payload=second, action="promote-bundle")
        self.assertEqual(child.pk, again.pk)
        self.assertEqual(first, second)
        self.assertEqual(first["requested_child_run_id"], child.run_id)
        self.assertNotEqual(first[WIRE_REQUEST_KEY], first["child_dispatch_v1"][WIRE_REQUEST_KEY])
        self.assertEqual(child.run_request["roo_points_ledger_id"], str(self.charge.pk))
        self.assertEqual(child.run_request["roo_points_cost"], 5)
        self.assertEqual(child.run_request["topic"], self.parent.run_request["topic"])
        self.assertNotEqual(child.run_request["operation_id"], self.parent.run_request["operation_id"])
        self.assertEqual(child.run_request[BUDGET_KEY]["max_model_usd"], "5.00")
        evidence = first["child_dispatch_v1"]["reviewed_publication_source_v1"]
        self.assertIsNone(evidence["reviewed_source_ref"])
        self.assertIsNone(evidence["review_source_proof_digest"])
        self.assertEqual(evidence["expected_source_sha"], "a" * 40)
        self.assertEqual(evidence["reviewed_revision"], self.review["revision"])
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.parent.pk), before)
        self.assertEqual(list(Ledger.objects.values()), money)
        self.assertFalse(transaction.get_connection().in_atomic_block)
        if os.environ.get("R3_PUBLISH_CHILD_FIXTURE_OUTPUT"):
            Path(os.environ["R3_PUBLISH_CHILD_FIXTURE_OUTPUT"]).write_text(json.dumps({
                "parent_request": self.parent.run_request, "approved_review": self.review,
                "dispatch": first, "child_request": child.run_request}, indent=2, sort_keys=True) + "\n")

    def test_prepared_owned_publication_child_is_recovered_from_protected_source_dispatch_without_http(self):
        from . import vibe_marketing_views as views
        from .run_revision_child_admission import record_publish_handoff
        child = prepare_publish_child(run=self.parent, user=self.user, payload={}, action="publish-pr")
        record_publish_handoff(run=self.parent, child=child, pending=False)
        self.parent.refresh_from_db(); child.refresh_from_db()
        before = {run.pk: ContentFactoryRun.objects.values().get(pk=run.pk) for run in (self.parent, child)}
        with patch.object(views, "_call_content_factory_run_status") as remote, patch.object(views.http_client, "post") as post:
            resolved = views._admitted_publish_child_for_source(self.parent, child.run_id)
            recovered = views._recover_publish_child_for_run(self.parent, request=None, context=self.fixture)
        self.assertEqual(resolved.pk, child.pk)
        self.assertEqual(recovered.pk, child.pk)
        remote.assert_not_called(); post.assert_not_called()
        for run in (self.parent, child):
            self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before[run.pk])

    def test_changed_review_body_or_destination_cannot_reuse_admission(self):
        prepare_publish_child(run=self.parent, user=self.user, payload={}, action="publish-pr")
        with self.assertRaises(RunOwnershipConflict):
            prepare_publish_child(run=self.parent, user=self.user, payload={"auto_merge": True}, action="publish-pr")
        self.review["previewPending"] = True
        with self.assertRaises(RunOwnershipConflict):
            prepare_publish_child(run=self.parent, user=self.user, payload={}, action="publish-pr")
        self.assertEqual(ContentFactoryRun.objects.count(), 2)

    def test_auto_merge_uses_protected_current_publisher_control_without_recipe_or_clock_writes(self):
        from .run_revision_child_admission import publish_auto_merge_control, record_publish_auto_merge_control
        from .run_ownership import ARTICLE_PUBLISH_CONTROL_RESULT_KEY
        from .run_execution_control import append_execution_ticket
        from .vibe_marketing_views import _record_publish_auto_merge_flag, _publish_auto_merge_enabled
        child = prepare_publish_child(run=self.parent, user=self.user, payload={"auto_merge": False}, action="publish-pr")
        before = ContentFactoryRun.objects.values().get(pk=child.pk)
        ContentFactoryRun.objects.filter(pk=child.pk).update(result={**child.result, "publish_auto_merge": True})
        child.refresh_from_db()
        self.assertFalse(_publish_auto_merge_enabled(child, self.parent))
        _record_publish_auto_merge_flag(child, user=self.user)
        self.assertTrue(publish_auto_merge_control(child))
        self.assertTrue(_publish_auto_merge_enabled(child, self.parent))
        first = deepcopy(child.result[ARTICLE_PUBLISH_CONTROL_RESULT_KEY])
        _record_publish_auto_merge_flag(child, user=self.user)
        self.assertEqual(child.result[ARTICLE_PUBLISH_CONTROL_RESULT_KEY], first)
        record_publish_auto_merge_control(run=child, user=self.user, enabled=False)
        self.assertFalse(publish_auto_merge_control(child))
        history = child.result[ARTICLE_PUBLISH_CONTROL_RESULT_KEY]
        self.assertEqual(len(history["controls"]), 2)
        for key, value in first["controls"].items():
            self.assertEqual(history["controls"][key], value)
        after = ContentFactoryRun.objects.values().get(pk=child.pk)
        for field in before.keys() - {"result"}:
            self.assertEqual(after[field], before[field], field)
        record_publish_auto_merge_control(run=child, user=self.user, enabled=True)
        with transaction.atomic():
            append_execution_ticket(child, control_key="e" * 64, reason="user_resume")
        child.refresh_from_db()
        self.assertFalse(publish_auto_merge_control(child))
        self.assertEqual(child.run_request, before["run_request"])
        self.assertEqual(Ledger.objects.count(), 1)

    def test_current_member_publisher_owns_its_child_and_removed_rights_cannot_enable_auto_merge(self):
        from django.contrib.auth import get_user_model
        from founder_tools import startup_team
        from .run_revision_child_admission import checked_publish_child, record_publish_auto_merge_control, publish_auto_merge_control
        member = get_user_model().objects.create_user(email="publisher@synthetic.example", first_name="Publisher")
        member.email_verified_at = timezone.now(); member.save(update_fields=["email_verified_at"])
        reference = startup_team.issue_join_reference(user=member, organization=self.org)
        with patch.object(startup_team, "_notify"):
            startup_team.request_join(user=member, join_reference=reference)
            self.config.refresh_from_db()
            pending = self.config.pillar_strategy["startup_team"]["requests"][0]
            startup_team.decide_request(organization=self.org, user=self.user, request_id=pending["request_id"], decision="approve")
            self.config.refresh_from_db()
            row = self.config.pillar_strategy["startup_team"]["members"][0]
            startup_team.update_member(organization=self.org, user=self.user, member_id=row["memberId"],
                rights={"publish": True, "billing": False})
        payload = {}
        child = prepare_publish_child(run=self.parent, user=member, payload=payload, action="publish-pr")
        self.assertEqual(owner_receipt(child)["identity"]["principal"]["user_id"], str(member.pk))
        if owner_receipt(self.parent):
            self.assertNotEqual(owner_receipt(child)["identity"]["principal"], owner_receipt(self.parent)["identity"]["principal"])
        record_publish_auto_merge_control(run=child, user=member, enabled=True)
        self.assertTrue(publish_auto_merge_control(child))
        startup_team.update_member(organization=self.org, user=self.user, member_id=row["memberId"],
            rights={"publish": False, "billing": False})
        self.assertFalse(publish_auto_merge_control(child))
        with self.assertRaises(RunOwnershipConflict):
            checked_publish_child(run=self.parent, user=member, payload=payload)
        with self.assertRaises(RunOwnershipConflict):
            record_publish_auto_merge_control(run=child, user=member, enabled=True)
        self.assertEqual(Ledger.objects.count(), 1)

    def test_explicit_stop_consent_refuses_new_children_before_provider_reads(self):
        from .run_user_stop import record_user_denial
        from .run_revision_child_admission import prepare_component_revision_child
        record_user_denial(self.parent, user=self.user, actor_id=f"mlai_user:{self.user.pk}")
        with self.assertRaises(RunOwnershipConflict):
            prepare_publish_child(run=self.parent, user=self.user, payload={}, action="publish-pr")
        with self.assertRaises(RunOwnershipConflict):
            prepare_component_revision_child(run=self.parent, user=self.user, payload={"source_run_id": self.parent.run_id,
                "feedback_batch_id": "stopped-feedback-batch", "requested_run_id": "must-not-exist", "comments": []})
        self.remote.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        self.assertEqual(Ledger.objects.count(), 1)

    def test_actual_authenticated_route_admits_before_post_and_preserves_callback_first_child(self):
        from . import vibe_marketing_views as views
        sent = []
        before = ContentFactoryRun.objects.values().get(pk=self.parent.pk)
        def provider(url, *, json, **kwargs):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            child = ContentFactoryRun.objects.get(run_id=json["requested_child_run_id"])
            self.assertIsNotNone(owner_receipt(child))
            sent.append(deepcopy(json))
            ContentFactoryRun.objects.filter(pk=child.pk).update(status="completed", current_step="done",
                result={**child.result, "callback_first_fact": True})
            response = Mock(status_code=202, content=b"accepted")
            response.json.return_value = {"run_id": child.run_id, "status": "queued"}
            return response
        request = APIRequestFactory().post("/synthetic", {"company_id": str(self.fixture.company.pk), "auto_merge": False}, format="json")
        force_authenticate(request, self.user)
        with patch.object(views, "_content_factory_remote_config", return_value={"enabled": True, "base_url": "http://synthetic-worker"}), \
                patch.object(views, "_github_account_for_context", return_value={"saved": True, "owned": True, "verified": True}), \
                patch.object(views, "_verify_github_repository_access", return_value={"verified": True, "writable": True, "branch": "main", "sha": "a" * 40}), \
                patch.object(views.http_client, "post", side_effect=provider):
            response = VibeMarketingRunControlView.as_view()(request, run_id=self.parent.run_id, action="promote-bundle")
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(len(sent), 1)
        child = ContentFactoryRun.objects.exclude(pk=self.parent.pk).get()
        self.assertEqual(child.status, "completed")
        self.assertEqual(child.current_step, "done")
        self.assertIs(child.result["callback_first_fact"], True)
        after = ContentFactoryRun.objects.values().get(pk=self.parent.pk)
        for field in before.keys() - {"result"}:
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(Ledger.objects.count(), 1)


@override_settings(**FLAGS, ARTICLES_PUBLISHING_INPUTS_V3_ENABLED=False)
class LegacyPublishChildAdmissionTests(PublishChildAdmissionTests):
    test_prepared_owned_publication_child_is_recovered_from_protected_source_dispatch_without_http = None

    def setUp(self):
        super().setUp()
        from .article_publish_approval import article_publish_approval_receipt, RECEIPT_KEY
        from .tests_run_legacy_source import resigned
        receipt = deepcopy(article_publish_approval_receipt(self.parent))
        request = {key: value for key, value in self.parent.run_request.items()
            if key not in {WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, BUDGET_KEY}}
        request[RECEIPT_KEY] = receipt
        ContentFactoryRun.objects.filter(pk=self.parent.pk).update(run_request=request, result=_review_run().result, approval_state="approved")
        self.parent.refresh_from_db()
        self.descriptor = resigned({"schema_version": 1, "source_run_id": self.parent.run_id,
            "workflow": "direct_generate", "domain": self.org.domain, "github_repo": self.parent.github_repo,
            "request_sha256": "a" * 64, "content_sha256": "b" * 64})
        p = patch("content_factory.run_legacy_source.fetch_legacy_source", return_value=self.descriptor)
        p.start(); self.addCleanup(p.stop)

    def test_explicit_stop_consent_refuses_new_children_before_provider_reads(self):
        ContentFactoryRun.objects.filter(pk=self.parent.pk).update(status="denied")
        self.parent.refresh_from_db()
        with self.assertRaises(RunOwnershipConflict):
            prepare_publish_child(run=self.parent, user=self.user, payload={}, action="publish-pr")
        self.remote.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.count(), 1)

    def test_new_fixed_child_has_separate_cap_and_original_charge_before_http_and_exact_retry(self):
        before = ContentFactoryRun.objects.values().get(pk=self.parent.pk)
        first, second = {}, {}
        child = prepare_publish_child(run=self.parent, user=self.user, payload=first, action="promote-bundle")
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False):
            again = prepare_publish_child(run=self.parent, user=self.user, payload=second, action="promote-bundle")
        self.assertEqual(child.pk, again.pk)
        self.assertEqual(first, second)
        self.assertIsNone(owner_receipt(self.parent))
        self.assertIsNotNone(owner_receipt(child))
        self.assertNotIn(WIRE_REQUEST_KEY, first)
        self.assertNotIn(WIRE_EXECUTION_KEY, first)
        nested = first["child_dispatch_v1"]
        self.assertEqual(nested["legacy_source_v1"], self.descriptor)
        source = nested["reviewed_publication_source_v1"]
        for key in ("source_admission_digest", "source_execution_id", "source_ticket_digest", "review_source_proof_digest"):
            self.assertIsNone(source[key])
        self.assertNotIn("legacy_source_v1", source)
        self.assertEqual(set(source), {"schema_version", "source_run_id", "reviewed_revision",
            "reviewed_source_ref", "expected_source_sha", "source_admission_digest",
            "source_execution_id", "source_ticket_digest", "review_source_proof_digest"})
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.parent.pk), before)
        self.assertEqual(Ledger.objects.count(), 1)
        if os.environ.get("R3_LEGACY_PUBLISH_CHILD_FIXTURE_OUTPUT"):
            Path(os.environ["R3_LEGACY_PUBLISH_CHILD_FIXTURE_OUTPUT"]).write_text(json.dumps({
                "parent_request": self.parent.run_request, "approved_review": self.review,
                "dispatch": first, "child_request": child.run_request}, indent=2, sort_keys=True) + "\n")
