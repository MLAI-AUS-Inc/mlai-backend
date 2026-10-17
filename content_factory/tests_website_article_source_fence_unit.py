"""Trusted source stop history, permanent old-write fence and original owner stop."""
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from . import website_article_source_fence as fences, website_goal as goals
from . import website_setup_consent as consent, website_operations as operations
from . import tests_website_article_refresh_unit as fixtures
from .tests_website_first_article_unit import rows
from .website_contract import WebsiteAuthorityError


@override_settings(ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=True, ARTICLES_SETUP_CONSENT_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True, ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED=True, ARTICLES_ARTICLE_RECOVERY_ENABLED=True,
    ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=True, ARTICLES_WEBSITE_EVENT_WAKES_ENABLED=True)
class ArticleSourceFenceTests(SimpleTestCase):
    def setUp(self):
        self.fixture = fixtures.ArticleRefreshCoordinatorTests("runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.parent, self.connection, self.root = self.fixture.parent, self.fixture.connection, self.fixture.run
        self.config, self.now = self.fixture.chain.config, self.fixture.now
        self.config.organization, self.config.github_repo = self.connection.organization, self.connection.github_repo
        self.rebound = deepcopy(self.parent.receipt["goal"])
        old = deepcopy(self.rebound)
        old["scope"]["configuration_revision"] = 1
        old["current_source_sha"] = "a" * 40
        old["consent"] = consent.seal(old, old["consent"]["answers"], user_id=4, now=self.now)
        self.parent.receipt["goal"] = old
        self.parent.receipt["child_run_id"] = self.root.run_id
        self.workflow = self.fixture.chain.workflow
        self.workflow.receipt = {}
        self.workflow.save = Mock()
        self.workflow.connection_id = self.connection.pk
        self.workflow.connection = self.connection
        self.nodes = {self.root.run_id: self.root}
        self.reservations = {}
        stack = self.fixture.chain.fixture.stack
        stack.enter_context(patch.object(fences, "_run", side_effect=lambda connection, identifier, **kwargs: self.nodes.get(identifier)))
        stack.enter_context(patch.object(fences, "_operation", side_effect=lambda connection, identifier:
            self.workflow if str(identifier) == str(self.workflow.pk) else None))
        stack.enter_context(patch.object(fences, "_fence_row", side_effect=lambda connection, identifier, **kwargs:
            next((row for row in self.reservations.values() if str(row.pk) == str(identifier) and row.action == fences.ACTION), None)))
        stack.enter_context(patch("content_factory.website_models.WebsiteConnectionOperation.objects.get_or_create", side_effect=self.reserve))
        self.connection.operations.select_for_update.return_value = self.connection.operations
        self.connection.operations.filter.side_effect = self.find_operation
        self.connection.repository_mutations = Mock()
        self.connection.repository_mutations.filter.return_value.values.return_value = []

    def reserve(self, *, idempotency_key, defaults):
        if idempotency_key in self.reservations:
            return self.reservations[idempotency_key], False
        row = SimpleNamespace(pk=uuid4(), connection=self.connection, connection_id=self.connection.pk,
            generation=self.connection.generation, action=defaults["action"], state=defaults.get("state", "pending"),
            payload=deepcopy(defaults["payload"]), receipt=deepcopy(defaults.get("receipt", {})), save=Mock())
        self.reservations[idempotency_key] = row
        return row, True

    def find_operation(self, *args, **kwargs):
        if kwargs.get("action") == "goal-source-scope":
            result = rows()
            result.__getitem__.return_value = [SimpleNamespace(payload=edge) for edge in self.fixture.source.edges]
            return result
        identifier = str(kwargs.get("pk", ""))
        if identifier == str(self.workflow.pk): return rows(self.workflow)
        if identifier == str(self.parent.pk) or kwargs.get("action") == "prepare": return rows(self.parent)
        if "payload__run_id__in" in kwargs or "pk__in" in kwargs:
            result = rows()
            result.values_list.return_value = [self.workflow.pk]
            result.update.side_effect = lambda **updates: [setattr(self.workflow, key, value) for key, value in updates.items()]
            return result
        return rows()

    def record(self):
        return fences.record(self.parent, self.config, rebound=self.rebound, now=self.now)

    def rebound_parent(self):
        self.parent.receipt["goal"] = deepcopy(self.rebound)

    def run_rows(self, *args, **kwargs):
        selected = [run for run in self.nodes.values() if not kwargs.get("run_id") or kwargs["run_id"] == run.run_id]
        if kwargs.get("workflow") == "article_system_setup": selected = []
        result = rows(selected[0] if selected else None)
        result.values_list.return_value = [run.run_id for run in selected]
        result.__iter__.side_effect = lambda: iter(selected)
        result.update.side_effect = lambda **updates: [setattr(run, key, value) for run in selected for key, value in updates.items()]
        return result

    def test_source_record_retains_origin_before_cancellation_without_debit_or_remote_work(self):
        self.root.status = "running"
        fence = self.record()
        self.assertEqual(fence.payload["binding"]["runs"][0]["status"], "running")
        self.assertEqual(fence.payload["binding"]["runs"][0]["execution_version"], [1, 5])
        self.assertEqual(fence.payload["binding"]["billing"]["client_request_id"], self.root.run_request["client_request_id"])
        self.assertEqual(self.root.status, "running")
        self.assertEqual(self.workflow.receipt[fences.KEY], str(fence.pk))
        self.assertFalse(fence.receipt["remote_confirmed"]); self.assertFalse(fence.receipt["effect_authority"])
        self.fixture.post.assert_not_called()
        self.assertEqual(self.fixture.chain.fixture.spend.call_count, 1)
        self.rebound_parent()
        self.assertEqual(fences.validated(self.connection, self.parent, str(fence.pk)), fence.payload)
        self.assertIs(self.record(), fence)

    def test_completed_old_operation_cannot_authorize_generic_effects_even_after_flag_disabled(self):
        self.record(); self.rebound_parent(); self.workflow.state = "completed"
        for action in ("read", "setup", "publish", "merge", "cleanup", "restoration"):
            with self.subTest(action=action), override_settings(ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=False):
                with self.assertRaises(WebsiteAuthorityError):
                    operations.validate_operation(self.connection, {"operation_id": str(self.workflow.pk),
                        "operation_attempt": 1, "deletion_epoch": 0, "action": action})

    def test_owner_cancelled_original_is_never_relabelled_as_source_supersession(self):
        self.root.status = "cancelled"
        self.assertIsNone(self.record())
        self.assertNotIn(fences.KEY, self.workflow.receipt)
        self.assertEqual(self.reservations, {})

    def test_missing_source_edge_or_changed_owner_never_records_fence(self):
        self.fixture.source.edges = []
        with self.assertRaises(WebsiteAuthorityError): self.record()
        self.assertEqual(self.reservations, {})
        self.fixture.source.edges = [{"from_revision": 1, "to_revision": 2, "source_sha": "c" * 40}]
        self.rebound["consent"] = consent.seal(self.rebound, self.rebound["consent"]["answers"], user_id=99, now=self.now)
        with self.assertRaises(WebsiteAuthorityError): self.record()
        self.assertEqual(self.reservations, {})

    def test_source_flag_off_and_owned_merge_without_scope_edge_keep_existing_path(self):
        with override_settings(ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=False):
            self.assertIsNone(self.record())
        self.parent.receipt["goal"] = deepcopy(self.rebound)
        self.assertIsNone(self.record())
        self.assertEqual(self.reservations, {})

    def test_actual_source_wakeup_records_fence_before_child_cancellation(self):
        from .website_goal_runner import wake_goal_for_source
        def cancelled(parent, config, *, source_fence):
            self.assertGreater(self.fixture.depth, 0)
            self.assertEqual(parent.receipt[fences.KEY], str(source_fence.pk))
            self.assertEqual(parent.receipt["goal"]["scope"], self.rebound["scope"])
            self.assertEqual(self.workflow.receipt[fences.KEY], str(source_fence.pk))
            self.assertEqual(fences.authorize_automatic_cancellation(self.connection, self.workflow, str(source_fence.pk))[0], parent)
        with patch("content_factory.website_prepare._cancel_prepare_child", side_effect=cancelled) as cancel:
            result = wake_goal_for_source(self.config, company_id=9, user=self.fixture.chain.fixture.user, now=self.now)
        self.assertIs(result, self.parent)
        self.assertEqual(self.parent.state, "pending")
        cancel.assert_called_once()

    def test_actual_webhook_wake_records_same_private_fence_before_cancellation(self):
        from . import website_wakeups as wakes
        from .website_models import WebsiteConnection
        from organizations.models import Organization
        from .models import OrganizationContentConfig
        connection_rows = rows(self.connection)
        connection_rows.get.return_value = self.connection
        def cancelled(parent, config, *, source_fence):
            self.assertEqual(parent.receipt[fences.KEY], str(source_fence.pk))
            self.assertEqual(parent.receipt["goal"]["scope"], self.rebound["scope"])
            self.assertEqual(self.workflow.receipt[fences.KEY], str(source_fence.pk))
        with patch.object(WebsiteConnection.objects, "select_related", return_value=connection_rows), \
                patch.object(WebsiteConnection.objects, "select_for_update", return_value=connection_rows), \
                patch.object(Organization.objects, "select_for_update"), \
                patch.object(OrganizationContentConfig.objects, "get", return_value=self.config), \
                patch.object(wakes.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(wakes.transaction, "on_commit") as committed, \
                patch("content_factory.website_prepare._cancel_prepare_child", side_effect=cancelled) as cancel:
            wake = wakes.queue_wake(self.connection.pk, signal={"kind": "trusted_callback", "success": True}, expected_generation=3)
        self.assertEqual(wake.action, "goal-wake")
        self.assertEqual(self.parent.receipt["goal"]["scope"], self.rebound["scope"])
        cancel.assert_called_once(); committed.assert_called_once()

    def test_invalid_root_attempt_and_foreign_article_never_record_history(self):
        self.root.run_request["operation_attempt"] = True
        with self.assertRaises(WebsiteAuthorityError): self.record()
        self.assertEqual(self.reservations, {})
        self.root.run_request["operation_attempt"] = 1
        self.root.run_request["first_article_goal_id"] = str(uuid4())
        with self.assertRaises(WebsiteAuthorityError): self.record()
        self.assertEqual(self.reservations, {})

    def test_automatic_cancel_keeps_recovery_marker_but_owner_stop_revokes_entire_goal(self):
        from workflow_runs.models import ContentFactoryRun
        from .website_connections import contract_for
        self.root.status = "running"
        fence = self.record(); self.rebound_parent()
        data = {**contract_for(self.connection), "operation_id": str(self.workflow.pk), "operation_attempt": 1, "deletion_epoch": 0}
        with patch.object(ContentFactoryRun.objects, "filter", side_effect=self.run_rows):
            automatic = operations.cancel_operation(self.config, data=data, idempotency_key="automatic", source_fence=str(fence.pk))
            self.assertEqual(automatic.action, "cancel-operation")
            self.assertEqual(self.root.status, "cancelled")
            self.assertEqual(fence.state, "completed")
            self.assertEqual(self.parent.state, "running")
            # The client body cannot pass the server-only keyword; an explicit
            # later stop revokes refresh and follows normal whole-goal cleanup.
            stopped = operations.cancel_operation(self.config, data={**data, "source_fence": str(fence.pk)}, idempotency_key="owner")
        self.assertEqual(stopped.payload["cancelled_operation_id"], str(self.parent.pk))
        self.assertEqual(self.parent.state, "cancelled")
        self.assertEqual(fence.state, "cancelled")
        self.assertEqual(fence.receipt["status"], "revoked_by_owner")
        self.assertEqual(len([row for row in self.reservations.values() if row.action == "goal-refund"]), 1)
        self.assertEqual(self.fixture.chain.fixture.spend.call_count, 1)
        with self.assertRaises(WebsiteAuthorityError): fences.validated(self.connection, self.parent, str(fence.pk))

    def test_unknown_execution_version_stays_unknown_in_pre_cancel_record(self):
        self.root.result = {}
        fence = self.record()
        self.assertIsNone(fence.payload["binding"]["runs"][0]["execution_version"])
        self.rebound_parent()
        with self.assertRaises(WebsiteAuthorityError) as error: self.fixture.advance()
        self.assertTrue(error.exception.retryable)
        self.fixture.post.assert_not_called()

    def test_owner_can_stop_budget_suspended_goal_without_reopening_recovery(self):
        from workflow_runs.models import ContentFactoryRun
        from .website_connections import contract_for
        fence = self.record(); self.rebound_parent()
        self.parent.state = "needs_attention"
        with patch.object(ContentFactoryRun.objects, "filter", side_effect=self.run_rows):
            stopped = operations.cancel_operation(self.config, data={**contract_for(self.connection),
                "operation_id": str(self.parent.pk), "operation_attempt": self.parent.payload.get("attempt", 1),
                "deletion_epoch": 0}, idempotency_key="stop-budget-suspended-goal")
        self.assertEqual(self.parent.state, "cancelled")
        self.assertEqual(stopped.action, "cancel-operation")
        self.assertEqual(fence.state, "cancelled")
        with self.assertRaises(WebsiteAuthorityError): fences.validated(self.connection, self.parent, str(fence.pk))

    def test_historical_root_metadata_rechecks_canonical_payment_without_generic_write_access(self):
        from .website_first_article import article_run
        fence = self.record(); self.rebound_parent()
        historical = article_run(self.parent, self.config)
        self.assertIs(historical, self.root)
        self.assertEqual(historical._article_source_fence_read["operation_id"], str(fence.pk))
        self.assertEqual(historical.status, "completed")
        self.fixture.chain.fixture.refunds.return_value.exists.return_value = True
        with self.assertRaises(WebsiteAuthorityError): article_run(self.parent, self.config)
        self.assertEqual(self.fixture.chain.fixture.spend.call_count, 1)

    def test_changed_historical_request_or_foreign_marker_cannot_bypass_original_article_guard(self):
        from .website_first_article import article_run
        self.record(); self.rebound_parent()
        self.root.run_request["untrusted_new_field"] = True
        with self.assertRaises(WebsiteAuthorityError): article_run(self.parent, self.config)
        self.root.run_request.pop("untrusted_new_field")
        self.workflow.receipt[fences.KEY] = str(uuid4())
        with self.assertRaises(WebsiteAuthorityError): article_run(self.parent, self.config)

    def test_cancelled_fenced_root_waits_for_paired_remote_read_grant_without_dispatch(self):
        from workflow_runs.models import ContentFactoryRun
        from .website_connections import contract_for
        self.root.status = "running"
        fence = self.record(); self.rebound_parent()
        with patch.object(ContentFactoryRun.objects, "filter", side_effect=self.run_rows):
            operations.cancel_operation(self.config, data={**contract_for(self.connection), "operation_id": str(self.workflow.pk),
                "operation_attempt": 1, "deletion_epoch": 0}, idempotency_key="automatic", source_fence=str(fence.pk))
        with self.assertRaises(WebsiteAuthorityError) as error: self.fixture.advance()
        self.assertTrue(error.exception.retryable)
        self.assertEqual(error.exception.code, "website_prepare_dependency_transient")
        self.fixture.post.assert_not_called()
        self.assertEqual(self.fixture.chain.fixture.spend.call_count, 1)

    def test_source_stop_handles_active_publish_child_of_completed_shared_operation(self):
        from workflow_runs.models import ContentFactoryRun
        from .website_connections import contract_for
        self.workflow.state = "completed"
        child = SimpleNamespace(run_id="publish-child", organization_id=self.connection.organization_id,
            github_repo=self.connection.github_repo, workflow="article_generation", status="running", result={"generation": 2, "state_version": 7},
            run_request={**self.root.run_request, "source_run_id": self.root.run_id})
        self.nodes[child.run_id] = child
        self.root.result["publish_child_run_id"] = child.run_id
        fence = self.record(); self.rebound_parent()
        with patch.object(ContentFactoryRun.objects, "filter", side_effect=self.run_rows):
            operations.cancel_operation(self.config, data={**contract_for(self.connection), "operation_id": str(self.workflow.pk),
                "operation_attempt": 1, "deletion_epoch": 0}, idempotency_key="automatic", source_fence=str(fence.pk))
        self.assertEqual(self.workflow.state, "cancelled")
        self.assertEqual(fence.payload["binding"]["runs"][0]["status"], "completed")
        self.assertEqual(fence.payload["binding"]["runs"][1]["status"], "running")
        for run_id in (self.root.run_id, child.run_id):
            payload = {**contract_for(self.connection), "operation_id": str(self.workflow.pk),
                "operation_attempt": 1, "deletion_epoch": 0, "run_id": run_id, "status": "cancelled"}
            self.assertIs(operations.validate_operation(self.connection, payload, cancellation=True), self.workflow)
            self.assertIs(operations.validate_operation(self.connection, payload, cancellation_receipt=True), self.workflow)
        payload["run_id"] = "foreign-child"
        with self.assertRaises(WebsiteAuthorityError): operations.validate_operation(self.connection, payload, cancellation=True)

    def assert_original_draft_control_stops_goal(self, action):
        from workflow_runs.models import ContentFactoryRun
        from . import vibe_marketing_views as views
        fence = self.record(); self.rebound_parent()
        with override_settings(ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=False), \
                patch.object(fences, "_fenced_operation_for_run", return_value=self.workflow), \
                patch.object(ContentFactoryRun.objects, "filter", side_effect=self.run_rows), \
                patch.object(views, "_get_config", return_value=self.config), \
                patch.object(views, "_serialize_run", side_effect=lambda run, **kwargs: {"run_id": run.run_id, "status": run.status}), \
                patch.object(views, "_call_content_factory_run_action") as remote:
            response = views.control_article_run(user=self.fixture.chain.fixture.user, context=self.fixture.chain.context,
                run=self.root, action=action, data={"source_fence": str(fence.pk)})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["status"], "cancelled")
        self.assertEqual(self.parent.state, "cancelled")
        self.assertEqual(fence.state, "cancelled")
        remote.assert_not_called()
        self.assertEqual(self.fixture.chain.fixture.spend.call_count, 1)

    def test_original_draft_cancel_control_stops_fenced_goal_even_after_rollout_rollback(self):
        self.assert_original_draft_control_stops_goal("cancel")

    def test_original_draft_deny_control_revokes_fenced_goal_without_rewriting_history(self):
        self.assert_original_draft_control_stops_goal("deny")

    def test_later_review_metadata_cannot_prevent_owner_stopping_original_fenced_goal(self):
        from workflow_runs.models import ContentFactoryRun
        from . import vibe_marketing_views as views
        fence = self.record(); self.rebound_parent()
        self.root.run_request["review_metadata"] = {"version": 2}
        with patch.object(fences, "_fenced_operation_for_run", return_value=self.workflow), \
                patch.object(ContentFactoryRun.objects, "filter", side_effect=self.run_rows), \
                patch.object(views, "_get_config", return_value=self.config), \
                patch.object(views, "_serialize_run", side_effect=lambda run, **kwargs: {"run_id": run.run_id, "status": run.status}):
            response = views.control_article_run(user=self.fixture.chain.fixture.user, context=self.fixture.chain.context,
                run=self.root, action="cancel", data={})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.parent.state, "cancelled")
        self.assertEqual(fence.state, "cancelled")

    def test_owner_stop_wins_when_source_fence_is_recorded_during_legacy_worker_http(self):
        from workflow_runs.models import ContentFactoryRun
        from . import vibe_marketing_views as views
        def worker(**kwargs):
            self.assertEqual(self.fixture.depth, 0, "Worker HTTP is outside authority locks")
            self.record(); self.rebound_parent()
            return {"status": "cancelled", "content_factory_status_code": 200}
        def local_cancel(**kwargs):
            self.root.status = "cancelled"
            self.root.result = {"status": "cancelled", "cancelled": True}
            return self.root
        fetched = rows(self.root); fetched.get.return_value = self.root
        with patch.object(fences, "_fenced_operation_for_run", side_effect=lambda *args:
                    self.workflow if fences.KEY in self.workflow.receipt else None), \
                patch.object(ContentFactoryRun.objects, "filter", side_effect=self.run_rows), \
                patch.object(ContentFactoryRun.objects, "prefetch_related", return_value=fetched), \
                patch.object(views, "_get_config", return_value=self.config), \
                patch.object(views, "_run_has_external_publish_evidence", return_value=False), \
                patch.object(views, "_serialize_run", side_effect=lambda run, **kwargs: {"run_id": run.run_id, "status": run.status}), \
                patch.object(views, "_cancel_local_article_run", side_effect=local_cancel), \
                patch.object(views, "_call_content_factory_run_action", side_effect=worker) as remote:
            response = views.control_article_run(user=self.fixture.chain.fixture.user, context=self.fixture.chain.context,
                run=self.root, action="cancel", data={})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.parent.state, "cancelled")
        row = next(row for row in self.reservations.values() if row.action == fences.ACTION)
        self.assertEqual(row.state, "cancelled")
        self.assertEqual(row.payload["binding"]["runs"][0]["execution_version"], [1, 5])
        self.assertEqual(self.fixture.chain.fixture.spend.call_count, 1)
        remote.assert_called_once()
