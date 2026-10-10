"""Replay publish-child callbacks without a database or external services."""
from copy import deepcopy
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase

from .incident_guards import publish_child_binding
from .website_contract import WebsiteAuthorityError


class ReconciledRunIdentityTests(SimpleTestCase):
    def setUp(self):
        from .tests_editorial_snapshot_unit import EditorialSnapshotPersistenceSeamTests
        self.fixture = EditorialSnapshotPersistenceSeamTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.run = self.fixture.existing
        self.run.github_repo = "fixture/site"
        self.run.slack_user_id = "fixture-actor"
        self.run.run_request.update(domain=self.run.domain, github_repo=self.run.github_repo,
            slack_user_id=self.run.slack_user_id, website_connection_id=str(uuid4()), connection_generation=3,
            repository_id=42, operation_id="original-operation", operation_attempt=8, deletion_epoch=0)
        self.run.result.update(generation=1, state_version=10)
        self.run.refresh_from_db = Mock()
        self.original = deepcopy(self.run.run_request)
        self.snapshot = {"workflow": self.run.workflow, "status": "running", "generation": 1,
                         "state_version": 11, "result": {"message": "Worker checkpoint"}}

    def reconcile(self, snapshot, *, denial=None):
        from importlib import import_module
        reconciliation = import_module("content_factory.reconciliation")
        service_views = import_module("content_factory.service_views")
        website_connections = import_module("content_factory.website_connections")
        website_operations = import_module("content_factory.website_operations")
        guard = (lambda *a, **kw: nullcontext()) if denial is None else Mock(side_effect=denial)
        with patch.object(website_connections, "authority_guard", side_effect=guard), \
                patch("content_factory.run_observations.proven_run", return_value=None) as provenance, \
                patch.object(website_operations, "observe_workflow_status"), \
                patch.object(service_views, "_sync_content_factory_run_snapshot",
                             side_effect=self.fixture.ns["_sync_content_factory_run_snapshot"]) as sync:
            result = reconciliation._adopt_remote_payload(self.run, snapshot)
            self.synced = sync.called
            self.observation_attempted = provenance.called
            return result

    def test_sparse_remote_status_keeps_repository_domain_and_actor_through_real_snapshot_sync(self):
        self.reconcile(self.snapshot)
        self.assertEqual((self.run.github_repo, self.run.domain, self.run.slack_user_id),
                         ("fixture/site", "example.test", "fixture-actor"))
        self.assertEqual(self.run.run_request, self.original)
        self.assertEqual(self.run.result["state_version"], 11)

    def test_previously_cleared_model_column_recovers_only_from_the_saved_request(self):
        self.run.github_repo = ""
        self.reconcile({**self.snapshot, "github_repo": "", "domain": ""})
        self.assertEqual(self.run.github_repo, self.original["github_repo"])
        self.assertEqual(self.run.run_request, self.original)

    def test_conflicting_top_level_or_nested_identity_cannot_replace_original(self):
        for change in ({"github_repo": "other/site"}, {"domain": "other.example"},
                       {"run_request": {"github_repo": "other/site"}},
                       {"run_request": {"domain": "other.example"}}):
            with self.subTest(change=change):
                self.reconcile({**self.snapshot, **change})
                self.assertFalse(self.synced)
                self.assertFalse(self.observation_attempted)
                self.assertEqual(self.run.github_repo, "fixture/site")
                self.assertEqual(self.run.run_request, self.original)

    def test_revoked_authority_and_stale_snapshots_cannot_repair_history(self):
        self.run.github_repo = ""
        self.reconcile(self.snapshot, denial=WebsiteAuthorityError("website_connection_changed", "Changed"))
        self.assertFalse(self.synced)
        self.assertEqual(self.run.github_repo, "")
        self.reconcile({**self.snapshot, "state_version": 9})
        self.assertEqual(self.run.github_repo, "")
        self.assertEqual(self.run.run_request, self.original)

    def test_missing_original_repository_does_not_borrow_a_current_selection(self):
        self.run.github_repo = ""
        self.run.run_request.pop("github_repo")
        before = deepcopy(self.run.run_request)
        self.reconcile(self.snapshot)
        self.assertEqual(self.run.github_repo, "")
        self.assertEqual(self.run.run_request, before)


class ReviewedArticleStatusPollTests(SimpleTestCase):
    def setUp(self):
        from .tests_article_publish_approval_unit import _review_run
        from .article_publish_approval import RECEIPT_KEY, make_article_publish_approval_receipt
        self.run = _review_run()
        self.run.status = "completed"
        self.run.approval_state = "approved"
        self.run.result.update(generation=7, state_version=40)
        self.run.run_request[RECEIPT_KEY] = make_article_publish_approval_receipt(self.run, actor_id="fixture-owner")
        self.run.pk = 1
        self.run.workflow = "direct_generate"
        self.run.current_step = "await_review"
        self.run.artifact_root = "fixture-artifacts"
        self.run.step_order = []
        self.run.acceptance_summary = {}
        self.run.verification_summary = {}
        self.run.resume_available = False
        self.run.error = ""
        self.run.save = Mock()
        self.run.refresh_from_db = Mock()
        result = deepcopy(self.run.result)
        result.pop("generation")
        result.pop("state_version")
        self.snapshot = {"status": "completed", "generation": 7, "state_version": 41, "result": result}

    def poll(self, snapshot):
        from . import vibe_marketing_views as views
        with patch.object(views, "authority_guard", side_effect=lambda *a, **kw: nullcontext()), \
                patch.object(views, "scoped_run_contract", return_value={}), \
                patch.object(views.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(views.ContentFactoryRun.objects, "select_for_update") as rows, \
                patch.object(views, "_sync_steps_from_remote"), \
                patch.object(views, "_persist_completed_article_memory_if_possible"):
            rows.return_value.get.return_value = self.run
            return views._sync_local_run_from_remote(self.run, snapshot)

    def test_full_poll_keeps_approval_and_uses_authoritative_execution_envelope(self):
        from .article_publish_approval import RECEIPT_KEY, article_publish_approval_receipt_matches
        receipt = deepcopy(self.run.run_request[RECEIPT_KEY])
        for nested in ({}, {"generation": 0, "state_version": 1}):
            with self.subTest(nested=nested):
                self.poll({**self.snapshot, "result": {**self.snapshot["result"], **nested}})
                self.assertEqual(self.run.result["generation"], 7)
                self.assertEqual(self.run.result["state_version"], 41)
                self.assertTrue(article_publish_approval_receipt_matches(self.run))
                self.assertEqual(self.run.run_request[RECEIPT_KEY], receipt)

    def test_stale_and_unversioned_polls_do_not_replace_review(self):
        before = deepcopy(self.run.result)
        for snapshot in ({**self.snapshot, "generation": 6}, {**self.snapshot, "state_version": 39},
                         {"status": "completed", "result": self.snapshot["result"]}):
            with self.subTest(snapshot_version=(snapshot.get("generation"), snapshot.get("state_version"))):
                self.poll(snapshot)
                self.assertEqual(self.run.result, before)
                self.run.save.assert_not_called()

    def test_new_generation_invalidates_previous_approval_and_malformed_fence_cannot_write(self):
        from .article_publish_approval import article_publish_approval_receipt_matches
        with self.assertRaises(ValueError):
            self.poll({**self.snapshot, "generation": True})
        self.run.save.assert_not_called()
        self.poll({**self.snapshot, "generation": 8})
        self.assertEqual(self.run.result["generation"], 8)
        self.assertFalse(article_publish_approval_receipt_matches(self.run))


class PublishChildReviewIdentityTests(SimpleTestCase):
    def setUp(self):
        self.slug = "reviewed-fixture-article"
        target = SimpleNamespace(
            target_key="featured", capabilities={"publishingReady": True},
            contract={"route_template": "/articles/featured/{slug}"},
        )
        connection = SimpleNamespace(
            pk=uuid4(), generation=3, github_repo="fixture/site", repository_id=42,
            verified_sha="a" * 40, targets=Mock(),
        )
        connection.targets.filter.return_value.first.return_value = target
        self.config = SimpleNamespace(website_connection=connection, default_publish_target_id="featured")
        request = {"website_connection_id": str(connection.pk), "connection_generation": 3,
                   "expected_source_sha": connection.verified_sha, "operation_id": "original-operation",
                   "operation_attempt": 8, "deletion_epoch": 0}
        self.source = SimpleNamespace(
            run_id="review-source", organization_id=7, github_repo=connection.github_repo,
            run_request=request, result={"delivery_package": {"slug": self.slug}}, acceptance_summary={},
        )
        self.child = SimpleNamespace(
            run_id="publish-child", organization_id=7, github_repo=connection.github_repo,
            run_request={**request, "source_run_id": self.source.run_id, "delivery_mode": "publish_code"},
            result={}, acceptance_summary={"evidence_summary": {"content_package_slug": None}},
        )

    def test_empty_child_uses_reviewed_parent_slug_without_mutating_saved_requests(self):
        before = deepcopy((self.source.run_request, self.child.run_request, self.child.result))
        binding = publish_child_binding(self.config, self.child, {}, {}, reviewed_source=self.source)
        self.assertEqual(binding["article_slug"], self.slug)
        self.assertEqual(binding["route_path"], f"/articles/featured/{self.slug}")
        self.assertEqual(before, (self.source.run_request, self.child.run_request, self.child.result))
        self.assertNotIn("repository_id", self.child.run_request)
        self.assertNotIn("connection_target_id", self.child.run_request)

    def test_unrelated_or_changed_scope_cannot_borrow_a_parent_slug(self):
        changes = [
            {"source_run_id": "another-source"}, {"operation_id": "replacement-operation"},
            {"operation_attempt": 9}, {"deletion_epoch": 1},
            {"connection_target_id": "replacement-target"}, {"repository_id": 42},
        ]
        for change in changes:
            with self.subTest(change=change):
                child = SimpleNamespace(**{**vars(self.child), "run_request": {**self.child.run_request, **change}})
                with self.assertRaises(WebsiteAuthorityError):
                    publish_child_binding(self.config, child, {}, {}, reviewed_source=self.source)
        self.child.organization_id = 8
        with self.assertRaises(WebsiteAuthorityError):
            publish_child_binding(self.config, self.child, {}, {}, reviewed_source=self.source)

    def test_child_and_remote_slug_or_route_mismatch_still_fail_closed(self):
        for result, remote in [
            ({"slug": "another-article"}, {}), ({}, {"slug": "another-article"}),
            ({}, {"route_path": "/articles/featured/another-article"}),
        ]:
            with self.subTest(result=result, remote=remote):
                self.child.result = result
                with self.assertRaises(WebsiteAuthorityError) as error:
                    publish_child_binding(self.config, self.child, {}, remote, reviewed_source=self.source)
                self.assertEqual(error.exception.code, "capture_target_mismatch")

    def test_parent_without_slug_or_with_stale_source_is_not_authority(self):
        for result, request in [({}, self.source.run_request),
                                (self.source.result, {**self.source.run_request, "expected_source_sha": "b" * 40})]:
            with self.subTest(result=result, request=request):
                source = SimpleNamespace(**{**vars(self.source), "result": result, "run_request": request})
                with self.assertRaises(WebsiteAuthorityError):
                    publish_child_binding(self.config, self.child, {}, {}, reviewed_source=source)

    def test_independent_draft_still_requires_its_own_saved_slug(self):
        with self.assertRaises(WebsiteAuthorityError) as error:
            publish_child_binding(self.config, self.child, {}, {})
        self.assertEqual(error.exception.code, "capture_target_mismatch")

    def test_existing_child_acknowledgement_validates_against_its_reviewed_source(self):
        from . import vibe_marketing_views as views
        context = SimpleNamespace(organization=SimpleNamespace(domain="fixture.test"))
        with patch.object(views, "_get_config", return_value=self.config), \
                patch.object(views.ContentFactoryRun.objects, "filter") as runs, \
                patch.object(views, "_run_belongs_to_context", return_value=True), \
                patch.object(views, "_create_local_run") as create:
            runs.return_value.prefetch_related.return_value.first.return_value = self.child
            acknowledged = views._ensure_local_publish_child_from_known_id(
                child_run_id=self.child.run_id, source_run=self.source,
                request=SimpleNamespace(user=object()), context=context,
                remote_data={"status": "queued"},
            )
        self.assertIs(acknowledged, self.child)
        create.assert_not_called()


class ApprovedPublishChildOperationTests(SimpleTestCase):
    def setUp(self):
        from .tests_article_publish_approval_unit import _review_run
        from .article_publish_approval import RECEIPT_KEY, make_article_publish_approval_receipt
        self.connection = SimpleNamespace(pk=uuid4(), organization_id=7, generation=3,
                                         github_repo="fixture/site", blockers=[], operations=Mock())
        self.op = SimpleNamespace(pk=uuid4(), state="completed", action="workflow", generation=3,
                                  payload={"run_id": "review-source", "attempt": 8}, save=Mock())
        self.connection.operations.filter.return_value.first.return_value = self.op
        self.source = _review_run()
        self.source.run_id = "review-source"
        self.source.organization_id = 7
        self.source.github_repo = self.connection.github_repo
        self.source.workflow = "article_generation"
        self.source.status = "completed"
        self.source.approval_state = "approved"
        self.source.run_request = {
            "website_connection_id": str(self.connection.pk), "connection_generation": 3,
            "expected_source_sha": "a" * 40, "operation_id": str(self.op.pk),
            "operation_attempt": 8, "deletion_epoch": 0,
            "delivery_mode": "review_draft", "delivery_mode_confirmed": True,
        }
        self.source.result["publish_child_run_id"] = "publish-child"
        self.source.run_request[RECEIPT_KEY] = make_article_publish_approval_receipt(self.source, actor_id="fixture-owner")
        self.child = SimpleNamespace(
            run_id="publish-child", organization_id=7, github_repo=self.connection.github_repo,
            workflow="direct_generate", status="queued", result={}, save=Mock(),
            run_request={**self.source.run_request, "source_run_id": self.source.run_id,
                         "delivery_mode": "publish_code", "delivery_mode_confirmed": True},
        )
        self.child.run_request.pop(RECEIPT_KEY)

    def _validate(self, run=None, **payload):
        from workflow_runs.models import ContentFactoryRun
        from .website_operations import validate_operation
        with patch.object(ContentFactoryRun.objects, "filter") as runs:
            runs.return_value.first.side_effect = [run or self.child, self.source]
            return validate_operation(self.connection, {
                **self.child.run_request, "run_id": self.child.run_id, "status": "running", **payload,
            })

    def test_explicitly_approved_child_checkpoint_keeps_source_operation_completed(self):
        original = deepcopy(self.op.payload)
        self.assertIs(self._validate(), self.op)
        self.assertEqual(self.op.state, "completed")
        self.assertEqual(self.op.payload, original)
        self.op.save.assert_not_called()

    def test_missing_or_changed_review_receipt_never_authorizes_a_child(self):
        from .article_publish_approval import RECEIPT_KEY
        for mutation in ["missing", "new_review", "wrong_child", "portable", "old_source"]:
            with self.subTest(mutation=mutation):
                request, result = deepcopy((self.source.run_request, self.source.result))
                if mutation == "missing": self.source.run_request.pop(RECEIPT_KEY)
                elif mutation == "new_review": self.source.result["article_preview_quality"]["inputs_sha256"] = "c" * 64
                elif mutation == "wrong_child": self.source.result["publish_child_run_id"] = "another-child"
                elif mutation == "portable": self.source.run_request["delivery_mode"] = "content_only"
                else: self.source.run_request["expected_source_sha"] = "b" * 40
                with self.assertRaises(WebsiteAuthorityError): self._validate()
                self.source.run_request, self.source.result = request, result

    def test_changed_saved_child_scope_and_cancelled_or_failed_operations_stay_denied(self):
        for mutation in ["operation", "target", "source", "cancelled", "failed"]:
            with self.subTest(mutation=mutation):
                request = deepcopy(self.child.run_request)
                if mutation == "operation": self.child.run_request["operation_attempt"] = 9
                elif mutation == "target": self.child.run_request["connection_target_id"] = "replacement-target"
                elif mutation == "source": self.child.run_request["source_run_id"] = "another-source"
                else: self.op.state = mutation
                with self.assertRaises(WebsiteAuthorityError): self._validate()
                self.child.run_request, self.op.state = request, "completed"

    def test_owner_resume_of_approved_child_preserves_original_attempt_and_consent(self):
        from contextlib import contextmanager
        from workflow_runs.models import ContentFactoryRun
        from . import website_operations as operations, website_connections as authority
        @contextmanager
        def guard(*args, **kwargs):
            yield self.connection
        request, operation = deepcopy((self.child.run_request, self.op.payload))
        with patch.object(authority, "authority_guard", side_effect=guard), \
                patch.object(authority, "extend_owner_operation_contract") as extend, \
                patch.object(ContentFactoryRun.objects, "filter") as runs, \
                patch.object(operations.WebsiteConnectionOperation.objects, "select_for_update") as locked:
            runs.return_value.first.return_value = self.source
            locked.return_value.get.return_value = self.op
            fields = operations.advance_workflow_attempt(self.child)
        self.assertEqual(fields, {key: request[key] for key in operations.OPERATION_FIELDS})
        extend.assert_called_once_with(fields)
        self.assertEqual(self.child.run_request, request)
        self.assertEqual(self.op.payload, operation)
        self.assertEqual(self.op.state, "completed")
        self.child.save.assert_not_called()
        self.op.save.assert_not_called()
