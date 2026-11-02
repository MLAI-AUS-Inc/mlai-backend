"""Actual persisted review command admission; no provider effects are performed."""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from founder_tools import startup_team
from roo.models import Ledger
from workflow_runs.models import ContentFactoryRun
from .run_execution_control import append_execution_ticket
from .run_ownership import (BUDGET_KEY, OWNER_EXECUTION_KEY, WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY,
    RunOwnershipConflict, bind_admitted_run, current_execution_ticket, owner_receipt, reserve_admitted_run, user_principal)
from .run_revision_child_admission import (accept_revision_child_response, prepare_component_revision_child,
    prepare_setup_revision_ticket, reviewed_update_child_id)
from .self_heal_testing import ALL_FLAGS_ON, make_p2_org
from .website_connections import authority_guard, contract_for, owner_operation_scope
from .website_models import WebsiteConnection
from .website_operations import bind_operation_run, reserve_workflow_operation

FLAGS = {**ALL_FLAGS_ON, "ARTICLES_RUN_OWNERSHIP_ENABLED": True,
    "ARTICLES_RUN_FACT_AUTHORITY_ENABLED": True, "ARTICLES_RUN_EVENT_AUTHORITY_ENABLED": True,
    "MY_STARTUP_TEAM_ENABLED": True, "MY_STARTUP_TEAM_DOMAIN_AUTO_JOIN_ENABLED": False,
    "MY_STARTUP_TRANSITION_NOTIFICATIONS_ENABLED": False,
    "ARTICLES_RUN_CONTROL_MODEL_USD": "5.00", "ARTICLES_RUN_CONTROL_MODEL_TOKENS": 50000,
    "ARTICLES_RUN_SDK_CONTROL_ENABLED": True}


@override_settings(**FLAGS)
class RevisionChildAdmissionTests(TransactionTestCase):
    def setUp(self):
        fixture = make_p2_org()
        self.org, self.user, self.config = fixture.organization, fixture.owner, fixture.config
        self.actor = f"mlai_user:{self.user.pk}"
        original = {"domain": self.org.domain, "client_request_id": "revision-source-original",
            "delivery_mode": "content_only", "delivery_mode_confirmed": True,
            "approved_body_sha256": "a" * 64, "topic": "Keep this original topic"}
        self.parent = reserve_admitted_run(organization=self.org, principal=user_principal(self.user.pk, self.actor),
            workflow="article_generation", payload=original)
        self.parent = bind_admitted_run(self.parent, "source-article-worker")
        self.parent_before = ContentFactoryRun.objects.values().get(pk=self.parent.pk)
        self.money_before = list(Ledger.objects.order_by("pk").values())

    def payload(self, **changes):
        return {"source_run_id": self.parent.run_id, "feedback_batch_id": "feedback-batch-0001",
            "requested_run_id": "component-revision-server-fixed", "comments": [{"comment_id": "saved-comment-1",
                "body": "Use the approved sources", "component_id": "article"}], **changes}

    def prepare(self, payload=None, user=None):
        return prepare_component_revision_child(run=self.parent, user=user or self.user,
            payload=payload if payload is not None else self.payload())

    def test_independent_fixed_child_exists_with_original_cap_before_provider_boundary(self):
        body = self.payload()
        child = self.prepare(body)
        self.assertFalse(connection.in_atomic_block)
        self.assertEqual(child.run_id, body["requested_run_id"])
        self.assertEqual(child.workflow, "article_revision")
        self.assertEqual(child.organization_id, self.parent.organization_id)
        self.assertEqual(child.run_request["source_run_id"], self.parent.run_id)
        self.assertEqual(body[WIRE_REQUEST_KEY], self.parent.run_request[WIRE_REQUEST_KEY])
        self.assertNotEqual(body[WIRE_REQUEST_KEY], body["child_dispatch_v1"][WIRE_REQUEST_KEY])
        self.assertEqual(body["child_dispatch_v1"][WIRE_REQUEST_KEY], child.run_request[WIRE_REQUEST_KEY])
        self.assertEqual(body["child_dispatch_v1"][WIRE_EXECUTION_KEY], child.run_request[WIRE_EXECUTION_KEY])
        self.assertEqual(body["child_dispatch_v1"][BUDGET_KEY], child.run_request[BUDGET_KEY])
        self.assertEqual(child.run_request[BUDGET_KEY]["max_model_usd"], "5.00")
        after = ContentFactoryRun.objects.values().get(pk=self.parent.pk)
        self.assertEqual({k: v for k, v in after.items() if k != "result"},
            {k: v for k, v in self.parent_before.items() if k != "result"})
        self.assertEqual(list(Ledger.objects.order_by("pk").values()), self.money_before)

    def test_exact_retry_keeps_child_original_cap_and_ticket_after_resume_and_flag_change(self):
        first_body = self.payload()
        child = self.prepare(first_body)
        with transaction.atomic():
            child = ContentFactoryRun.objects.select_for_update().get(pk=child.pk)
            append_execution_ticket(child, control_key="b" * 64, reason="user_resume")
        repeated = self.payload()
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False,
                ARTICLES_RUN_CONTROL_MODEL_USD="10.00"):
            again = self.prepare(repeated)
        self.assertEqual(child.pk, again.pk)
        self.assertEqual(first_body, repeated)
        self.assertEqual(repeated["child_dispatch_v1"][WIRE_EXECUTION_KEY]["generation"], 0)
        self.assertEqual(ContentFactoryRun.objects.count(), 2)
        self.assertEqual(list(Ledger.objects.order_by("pk").values()), self.money_before)

    def test_changed_batch_body_or_fixed_identity_cannot_reserve_another_child(self):
        self.prepare()
        for changes in [{"requested_run_id": "different-worker"},
                {"comments": [{"comment_id": "saved-comment-1", "body": "Different instructions"}]},
                {"github_repo": "foreign/repository"}, {"expected_source_sha": "f" * 40}]:
            with self.assertRaises(RunOwnershipConflict):
                self.prepare(self.payload(**changes))
        self.assertEqual(ContentFactoryRun.objects.count(), 2)

    def test_current_approved_member_can_own_a_child_without_inheriting_parent_principal(self):
        member = get_user_model().objects.create_user(email="reviewer@synthetic.example", first_name="Reviewer")
        member.email_verified_at = timezone.now()
        member.save(update_fields=["email_verified_at"])
        reference = startup_team.issue_join_reference(user=member, organization=self.org)
        with patch.object(startup_team, "_notify"):
            startup_team.request_join(user=member, join_reference=reference)
            self.config.refresh_from_db()
            pending = self.config.pillar_strategy["startup_team"]["requests"][0]
            startup_team.decide_request(organization=self.org, user=self.user,
                request_id=pending["request_id"], decision="approve")
        child = self.prepare(user=member)
        self.assertEqual(owner_receipt(child)["identity"]["principal"]["user_id"], str(member.pk))
        self.assertNotEqual(owner_receipt(child)["identity"]["principal"], owner_receipt(self.parent)["identity"]["principal"])
        self.assertEqual(list(Ledger.objects.order_by("pk").values()), self.money_before)
        self.config.refresh_from_db()
        row = self.config.pillar_strategy["startup_team"]["members"][0]
        startup_team.update_member(organization=self.org, user=self.user, member_id=row["memberId"], remove=True)
        with self.assertRaises(RunOwnershipConflict):
            self.prepare(user=member)

    def test_missing_or_deactivated_actual_requester_cannot_dispatch(self):
        with self.assertRaises(RunOwnershipConflict):
            prepare_component_revision_child(run=self.parent, user=None, payload=self.payload())
        type(self.user).objects.filter(pk=self.user.pk).update(is_active=False)
        with self.assertRaises(RunOwnershipConflict):
            self.prepare()
        self.assertEqual(ContentFactoryRun.objects.count(), 1)

    def test_foreign_existing_run_or_response_is_never_adopted(self):
        other = make_p2_org()
        ContentFactoryRun.objects.create(run_id="component-revision-server-fixed", organization=other.organization,
            domain=other.organization.domain, workflow="article_revision")
        before = list(ContentFactoryRun.objects.order_by("pk").values())
        with self.assertRaises(RunOwnershipConflict):
            self.prepare()
        self.assertEqual(list(ContentFactoryRun.objects.order_by("pk").values()), before)
        ContentFactoryRun.objects.filter(run_id="component-revision-server-fixed").delete()
        child = self.prepare()
        with self.assertRaises(RunOwnershipConflict):
            accept_revision_child_response(child, {"run_id": "foreign-worker"})
        self.assertEqual(child.run_id, "component-revision-server-fixed")

    def test_unmarked_legacy_source_has_no_admission_or_payload_changes(self):
        legacy = ContentFactoryRun.objects.create(run_id="legacy-source", organization=self.org,
            domain=self.org.domain, workflow="article_generation")
        body = {"source_run_id": legacy.run_id, "feedback_batch_id": "legacy-batch", "comments": []}
        original = deepcopy(body)
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False):
            self.assertIsNone(prepare_component_revision_child(run=legacy, user=None, payload=body))
        self.assertEqual(body, original)
        self.assertEqual(ContentFactoryRun.objects.count(), 2)


    def test_actual_component_provider_sees_reserved_fixed_child_and_exact_retry_before_http(self):
        from unittest.mock import Mock
        from .vibe_marketing_views import _call_content_factory_component_revision
        sent = []
        def provider(url, *, json, **kwargs):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            child = ContentFactoryRun.objects.get(run_id=json["requested_run_id"])
            self.assertIsNotNone(owner_receipt(child))
            self.assertEqual(json["child_dispatch_v1"][WIRE_REQUEST_KEY], child.run_request[WIRE_REQUEST_KEY])
            sent.append(deepcopy(json))
            response = Mock(status_code=202, content=b"accepted")
            response.json.return_value = {"run_id": child.run_id}
            return response
        with patch("content_factory.vibe_marketing_views._refresh_article_editorial_payload", return_value=None), \
                patch("content_factory.vibe_marketing_views._content_factory_remote_config", return_value={"enabled": True, "base_url": "http://synthetic-worker"}), \
                patch("content_factory.vibe_marketing_views.http_client.post", side_effect=provider):
            for _ in range(2):
                result = _call_content_factory_component_revision(organization=self.org,
                    run_id=self.parent.run_id, payload=self.payload(), user=self.user)
                self.assertEqual(result["run_id"], "component-revision-server-fixed")
        self.assertEqual(sent[0], sent[1])
        self.assertEqual(ContentFactoryRun.objects.count(), 2)
        self.assertEqual(list(Ledger.objects.order_by("pk").values()), self.money_before)

    def test_new_child_from_unmarked_parent_has_own_admission_without_parent_rewrite(self):
        from .tests_run_legacy_source import resigned
        legacy = ContentFactoryRun.objects.create(run_id="old-source", organization=self.org,
            domain=self.org.domain, workflow="article_generation", run_request={"domain": self.org.domain,
                "delivery_mode": "content_only", "delivery_mode_confirmed": True}, result={"retained_original": True})
        before = ContentFactoryRun.objects.values().get(pk=legacy.pk)
        descriptor = resigned({"schema_version": 1, "source_run_id": legacy.run_id, "workflow": "direct_generate",
            "domain": legacy.domain, "github_repo": "", "request_sha256": "a" * 64, "content_sha256": "b" * 64})
        def body():
            return {"source_run_id": legacy.run_id, "feedback_batch_id": "new-legacy-child", "comments": [],
                "requested_run_id": "new-independent-legacy-child"}
        first, second = body(), body()
        with patch("content_factory.run_legacy_source.fetch_legacy_source", return_value=descriptor):
            child = prepare_component_revision_child(run=legacy, user=self.user, payload=first)
            with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_CONTROL_MODEL_USD="9.00"):
                again = prepare_component_revision_child(run=legacy, user=self.user, payload=second)
        self.assertEqual(child.pk, again.pk)
        self.assertEqual(first, second)
        self.assertEqual(child.run_request["legacy_source_v1"], descriptor)
        self.assertEqual(first["child_dispatch_v1"]["legacy_source_v1"], descriptor)
        self.assertNotIn(WIRE_REQUEST_KEY, first)
        self.assertNotIn(WIRE_EXECUTION_KEY, first)
        self.assertIsNotNone(owner_receipt(child))
        self.assertEqual(child.run_request[BUDGET_KEY]["max_model_usd"], "5.00")
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=legacy.pk), before)
        changed = resigned({**descriptor, "content_sha256": "c" * 64})
        with patch("content_factory.run_legacy_source.fetch_legacy_source", return_value=changed):
            with self.assertRaises(RunOwnershipConflict):
                prepare_component_revision_child(run=legacy, user=self.user, payload=body())
        self.assertEqual(ContentFactoryRun.objects.count(), 3)


@override_settings(**FLAGS)
class SetupFeedbackTicketTests(TransactionTestCase):
    def setUp(self):
        fixture = make_p2_org()
        self.org, self.user, self.config = fixture.organization, fixture.owner, fixture.config
        self.connection = WebsiteConnection.objects.create(organization=self.org, authorized_by=self.user,
            github_repo="synthetic/site", repository_id=42, installation_id="1001", branch="main", verified_sha="a" * 40)
        self.config.website_connection = self.connection
        self.config.github_repo = self.connection.github_repo
        self.config.save(update_fields=["website_connection", "github_repo"])
        native = patch("content_factory.website_connections.verify_repository_native_target", return_value=None)
        native.start()
        self.addCleanup(native.stop)
        request = {**contract_for(self.connection), "domain": self.org.domain,
            "github_repo": self.connection.github_repo, "expected_source_sha": "a" * 40,
            "client_request_id": "setup-original"}
        with authority_guard(request, action="setup"):
            with owner_operation_scope(request):
                op = reserve_workflow_operation(self.connection, workflow="article_system_setup", payload=request)
        self.parent = reserve_admitted_run(organization=self.org, principal=user_principal(self.user.pk, f"mlai_user:{self.user.pk}"),
            workflow="article_system_setup", payload=request)
        bind_operation_run(op, self.parent)

    def payload(self, **changes):
        return {"feedback_batch_id": "setup-feedback-0001", "comments": [{"comment_id": "stored-1", "body": "Make it clear"}], **changes}

    def test_one_generation_per_exact_batch_preserves_request_charge_and_clocks(self):
        before = ContentFactoryRun.objects.values().get(pk=self.parent.pk)
        money = list(Ledger.objects.values())
        first, second = self.payload(), self.payload()
        prepare_setup_revision_ticket(run=self.parent, user=self.user, payload=first)
        prepare_setup_revision_ticket(run=self.parent, user=self.user, payload=second)
        self.assertEqual(first, second)
        self.assertEqual(first[WIRE_EXECUTION_KEY]["generation"], 1)
        self.assertNotIn("child_dispatch_v1", first)
        after = ContentFactoryRun.objects.values().get(pk=self.parent.pk)
        for key in before.keys() - {"result"}:
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(list(Ledger.objects.values()), money)
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        with self.assertRaises(RunOwnershipConflict):
            prepare_setup_revision_ticket(run=self.parent, user=self.user,
                payload=self.payload(comments=[{"comment_id": "stored-1", "body": "Different instructions"}]))
        third = self.payload(feedback_batch_id="setup-feedback-0002")
        prepare_setup_revision_ticket(run=self.parent, user=self.user, payload=third)
        self.assertEqual(third[WIRE_EXECUTION_KEY]["generation"], 2)
        replay = self.payload()
        prepare_setup_revision_ticket(run=self.parent, user=self.user, payload=replay)
        self.assertEqual(replay, first)


@override_settings(**FLAGS)
class ReviewedSourceChildTests(SetupFeedbackTicketTests):
    def setUp(self):
        super().setUp()
        from .website_models import WebsiteScanSnapshot
        WebsiteScanSnapshot.objects.create(connection=self.connection, generation=self.connection.generation,
            run_id="observed-default", source_sha="a" * 40, detector_version="github_head", fingerprint="default")
        self.snapshot = {"revision": "review-revision-1", "baseCommit": "b" * 40,
            "sourceRef": "mlai/setup-preview"}
        self.branch_head = "b" * 40
        self.provider_calls = []
        from unittest.mock import Mock
        def provider(connection, url):
            self.assertFalse(transaction.get_connection().in_atomic_block, "provider I/O under SQL locks")
            self.provider_calls.append((str(connection.pk), url))
            response = Mock()
            response.json.return_value = {"name": "mlai/setup-preview", "commit": {"sha": self.branch_head}}
            return response
        provider_patch = patch("integrations.services.github_connections.read_bound_repository", side_effect=provider)
        provider_patch.start()
        self.addCleanup(provider_patch.stop)

    def body(self):
        return {"action": "applyUpdate", "operationId": "saved-review-operation-0001",
            "expectedRevision": self.snapshot["revision"], "textEdits": [{"fieldId": "heading",
                "originalValue": "Before", "value": "After"}], "commentIds": [], "comments": [],
            "source_run_id": self.parent.run_id, "feedback_batch_id": "saved-review-operation-0001"}

    def create_child(self):
        from .run_revision_child_admission import prepare_review_update_child
        body = self.body()
        child = prepare_review_update_child(run=self.parent, user=self.user, payload=body, snapshot=self.snapshot)
        return child, body

    def test_exact_preview_commit_passes_without_changing_selected_default_source(self):
        from .website_connections import observed_source_sha, scoped_run_contract
        from .website_models import WebsiteConnectionOperation
        before = ContentFactoryRun.objects.values().get(pk=self.parent.pk)
        child, body = self.create_child()
        self.assertEqual(child.run_id, reviewed_update_child_id(self.parent.run_id, body["operationId"], setup=True))
        self.assertEqual(child.run_request["expected_source_sha"], "b" * 40)
        self.assertEqual(child.run_request["reviewed_source_ref"], "mlai/setup-preview")
        self.assertEqual(body[WIRE_REQUEST_KEY], self.parent.run_request[WIRE_REQUEST_KEY])
        self.assertNotEqual(body[WIRE_REQUEST_KEY], body["child_dispatch_v1"][WIRE_REQUEST_KEY])
        op = WebsiteConnectionOperation.objects.get(pk=child.run_request["operation_id"])
        proof = op.payload["review_source_v1"]["proofs"][current_execution_ticket(child)["execution_id"]]
        self.assertEqual(proof["expected_source_sha"], "b" * 40)
        import os
        fixture_output = os.environ.get("R3_REVIEW_SOURCE_FIXTURE_OUTPUT")
        if fixture_output:
            import json
            from pathlib import Path
            Path(fixture_output).write_text(json.dumps({"child_request": child.run_request,
                "parent_dispatch": body, "operation_review_source_v1": op.payload["review_source_v1"]},
                sort_keys=True, indent=2) + "\n")
        with patch("content_factory.website_connections.verify_repository_head") as default_head:
            with authority_guard(scoped_run_contract(child), action="preview"):
                pass
        default_head.assert_not_called()
        self.assertEqual(len(self.provider_calls), 2)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.branch, "main")
        self.assertEqual(self.connection.verified_sha, "a" * 40)
        self.assertEqual(observed_source_sha(self.connection), "a" * 40)
        after = ContentFactoryRun.objects.values().get(pk=self.parent.pk)
        self.assertEqual({k: v for k, v in before.items() if k != "result"},
            {k: v for k, v in after.items() if k != "result"})

    def test_moved_ref_stale_revision_and_forged_operation_refuse_before_effect(self):
        from .run_revision_child_admission import prepare_review_update_child
        from .website_connections import scoped_run_contract
        from .website_contract import WebsiteAuthorityError
        with self.assertRaises(WebsiteAuthorityError):
            prepare_review_update_child(run=self.parent, user=self.user, payload=self.body(),
                snapshot={**self.snapshot, "revision": "stale-revision"})
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        child, _ = self.create_child()
        body = scoped_run_contract(child)
        cases = [{**body, "operation_id": str(uuid4())}, {**body, "github_repo": "foreign/repo"},
            {**body, "reviewed_source_ref": "unreviewed/ref"},
            {**body, WIRE_EXECUTION_KEY: {**body[WIRE_EXECUTION_KEY], "generation": True}}]
        for changed in cases:
            with self.assertRaises((WebsiteAuthorityError, RunOwnershipConflict)):
                with authority_guard(changed, action="preview"):
                    self.fail("forged source reached an effect")
        self.branch_head = "c" * 40
        with self.assertRaises(WebsiteAuthorityError):
            with authority_guard(body, action="preview"):
                self.fail("moved branch reached an effect")

    def test_resume_requires_fresh_backend_guard_and_appends_immutable_ticket_proofs(self):
        from .website_connections import scoped_run_contract
        from .website_models import WebsiteConnectionOperation
        from .website_contract import WebsiteAuthorityError
        child, _ = self.create_child()
        original_request = deepcopy(child.run_request)
        old_ticket = current_execution_ticket(child)
        op = WebsiteConnectionOperation.objects.get(pk=child.run_request["operation_id"])
        old_proofs = deepcopy(op.payload["review_source_v1"]["proofs"])
        with self.assertRaises(WebsiteAuthorityError), transaction.atomic():
            locked = ContentFactoryRun.objects.select_for_update().get(pk=child.pk)
            append_execution_ticket(locked, control_key="d" * 64, reason="user_resume")
        child.refresh_from_db()
        self.assertEqual(current_execution_ticket(child), old_ticket)
        with authority_guard(scoped_run_contract(child), action="preview"):
            locked = ContentFactoryRun.objects.select_for_update().get(pk=child.pk)
            ticket = append_execution_ticket(locked, control_key="d" * 64, reason="user_resume")
        child.refresh_from_db()
        op.refresh_from_db()
        proofs = op.payload["review_source_v1"]["proofs"]
        self.assertEqual(len(proofs), 2)
        self.assertEqual(proofs[old_ticket["execution_id"]], old_proofs[old_ticket["execution_id"]])
        self.assertEqual(proofs[ticket["execution_id"]]["generation"], 1)
        self.assertEqual(child.run_request, original_request)
        with self.assertRaises((WebsiteAuthorityError, RunOwnershipConflict)):
            with authority_guard(original_request, action="preview"):
                self.fail("old ticket reached an effect")
        with authority_guard(scoped_run_contract(child), action="preview"):
            pass

    def test_ordinary_article_revision_uses_real_revision_and_original_source_without_fabricated_ref(self):
        from .run_revision_child_admission import prepare_review_update_child
        original = {**contract_for(self.connection), "domain": self.org.domain,
            "github_repo": self.connection.github_repo, "expected_source_sha": "a" * 40,
            "client_request_id": "ordinary-article-source"}
        with authority_guard(original, action="read"), owner_operation_scope(original):
            operation = reserve_workflow_operation(self.connection, workflow="article_generation", payload=original)
        parent = reserve_admitted_run(organization=self.org,
            principal=user_principal(self.user.pk, f"mlai_user:{self.user.pk}"), workflow="article_generation", payload=original)
        bind_operation_run(operation, parent)
        body = {"action": "applyUpdate", "operationId": "ordinary-review-update", "expectedRevision": "ordinary-revision",
            "source_run_id": parent.run_id, "commentIds": [], "comments": [],
            "restoredSentences": ["Restore this approved sentence"], "textEdits": []}
        def head(connection, expected):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            self.assertEqual(expected, "a" * 40)
            return expected
        with patch("content_factory.website_connections.verify_repository_head", side_effect=head) as provider:
            child = prepare_review_update_child(run=parent, user=self.user, payload=body,
                snapshot={"revision": "ordinary-revision", "fields": [], "previewCommit": "d" * 40})
        provider.assert_called_once()
        self.assertEqual(child.workflow, "article_revision")
        self.assertEqual(child.run_request["expected_source_sha"], "a" * 40)
        self.assertNotIn("reviewed_source_ref", child.run_request)
        self.assertNotIn("reviewed_revision", child.run_request)
        self.assertEqual(body["requested_run_id"], reviewed_update_child_id(parent.run_id, "ordinary-review-update", setup=False))

    def test_actual_apply_update_reserves_before_post_and_preserves_callback_first_lifecycle(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from .article_review_views import apply_article_review_update
        calls = []
        money = list(Ledger.objects.values())
        parent_before = ContentFactoryRun.objects.values().get(pk=self.parent.pk)
        def provider(method, url, *, json, **kwargs):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            response = Mock(status_code=202 if method == "POST" else 200)
            if method == "GET":
                response.json.return_value = self.snapshot
            else:
                calls.append(deepcopy(json))
                child = ContentFactoryRun.objects.get(run_id=json["requested_run_id"])
                self.assertEqual(json["child_dispatch_v1"][WIRE_REQUEST_KEY], child.run_request[WIRE_REQUEST_KEY])
                self.assertEqual(child.run_request["expected_source_sha"], self.snapshot["baseCommit"])
                ContentFactoryRun.objects.filter(pk=child.pk).update(status="completed", current_step="complete",
                    result={**child.result, "callback_first_fact": {"safe": True}})
                response.json.return_value = {"revisionRunId": child.run_id, "previewPending": True}
            return response
        body = {key: value for key, value in self.body().items() if key in {
            "action", "operationId", "expectedRevision", "textEdits", "commentIds"}}
        with patch("content_factory.article_review_views.views._latest_review_ready_component_revision", return_value=None), \
                patch("content_factory.article_review_views.views._content_factory_remote_config", return_value={"enabled": True, "base_url": "http://synthetic-worker"}), \
                patch("content_factory.article_review_views.views.http_client.request", side_effect=provider):
            result = apply_article_review_update(user=self.user, context=SimpleNamespace(organization=self.org),
                run=self.parent, data=body)
        self.assertEqual(result.status_code, 202)
        self.assertEqual(len(calls), 1)
        child = ContentFactoryRun.objects.exclude(pk=self.parent.pk).get()
        self.assertEqual(child.status, "completed")
        self.assertEqual(child.current_step, "complete")
        self.assertEqual(child.result["callback_first_fact"], {"safe": True})
        self.assertEqual(list(Ledger.objects.values()), money)
        parent_after = ContentFactoryRun.objects.values().get(pk=self.parent.pk)
        for field in parent_before.keys() - {"result"}:
            self.assertEqual(parent_after[field], parent_before[field], field)

    def test_connection_revoked_during_ref_verification_cannot_seal_or_use_proof(self):
        from .website_connections import scoped_run_contract
        from .website_contract import WebsiteAuthorityError
        child, _ = self.create_child()
        from unittest.mock import Mock
        def revoke(connection, url):
            WebsiteConnection.objects.filter(pk=connection.pk).update(state="revoked")
            response = Mock()
            response.json.return_value = {"name": "mlai/setup-preview", "commit": {"sha": "b" * 40}}
            return response
        with patch("integrations.services.github_connections.read_bound_repository", side_effect=revoke):
            with self.assertRaises(WebsiteAuthorityError):
                with authority_guard(scoped_run_contract(child), action="preview"):
                    self.fail("revocation race reached an effect")


@override_settings(**FLAGS)
class LegacyReviewedSourceChildTests(ReviewedSourceChildTests):
    def setUp(self):
        super().setUp()
        from .tests_run_legacy_source import resigned
        request = {key: value for key, value in self.parent.run_request.items()
            if key not in {WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, BUDGET_KEY}}
        ContentFactoryRun.objects.filter(pk=self.parent.pk).update(run_request=request, result={"retained_original": True})
        self.parent.refresh_from_db()
        self.descriptor = resigned({"schema_version": 1, "source_run_id": self.parent.run_id,
            "workflow": "article_system_setup", "domain": self.parent.domain, "github_repo": self.parent.github_repo,
            "request_sha256": "a" * 64, "content_sha256": "b" * 64})
        descriptor_patch = patch("content_factory.run_legacy_source.fetch_legacy_source", return_value=self.descriptor)
        descriptor_patch.start()
        self.addCleanup(descriptor_patch.stop)

    def test_exact_preview_commit_passes_without_changing_selected_default_source(self):
        from .website_connections import scoped_run_contract, observed_source_sha
        from .website_models import WebsiteConnectionOperation
        before = ContentFactoryRun.objects.values().get(pk=self.parent.pk)
        child, body = self.create_child()
        self.assertIsNone(owner_receipt(self.parent))
        self.assertIsNotNone(owner_receipt(child))
        self.assertNotIn(WIRE_REQUEST_KEY, body)
        self.assertNotIn(WIRE_EXECUTION_KEY, body)
        self.assertEqual(body["child_dispatch_v1"]["legacy_source_v1"], self.descriptor)
        operation = WebsiteConnectionOperation.objects.get(pk=child.run_request["operation_id"])
        proof = operation.payload["review_source_v1"]["proofs"][current_execution_ticket(child)["execution_id"]]
        self.assertIsNone(proof["parent_admission_digest"])
        self.assertEqual(proof["legacy_source_digest"], self.descriptor["descriptor_sha256"])
        with patch("content_factory.website_connections.verify_repository_head") as default_head:
            with authority_guard(scoped_run_contract(child), action="preview"):
                pass
        default_head.assert_not_called()
        self.assertEqual(observed_source_sha(self.connection), "a" * 40)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.parent.pk), before)

    def test_one_generation_per_exact_batch_preserves_request_charge_and_clocks(self):
        # The old same-run feedback route stays legacy. It creates no new child
        # and cannot retroactively turn a historical execution into an admission.
        before = ContentFactoryRun.objects.values().get(pk=self.parent.pk)
        body = self.payload()
        self.assertIsNone(prepare_setup_revision_ticket(run=self.parent, user=self.user, payload=body))
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.parent.pk), before)
