"""Native controller leases preserve one original capture, choice and paid source."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from . import cms_goal_controller as controller
from . import cms_goal_contract as goals
from . import cms_goal_dispatch as dispatch
from . import cms_goal_authority as authority
from . import tests_cms_goal_billing_unit as fixtures
from .cms_inventory_contract import new_inventory
from .dispatch_models import ContentFactoryDispatchOutbox
from .tests_cms_capture_unit import Rows
from .website_models import WebsiteConnectionOperation
from .website_failure_contract import classify_failure
from workflow_runs.models import ContentFactoryRun


@override_settings(**fixtures.CmsGoalBillingTests._overridden_settings)
class CmsGoalControllerTests(SimpleTestCase):
    configure = fixtures.CmsGoalBillingTests.configure
    reserve = fixtures.CmsGoalBillingTests.reserve
    hold = fixtures.CmsGoalBillingTests.hold
    prepare_generation = fixtures.CmsGoalBillingTests.prepare_generation
    consume = fixtures.CmsGoalBillingTests.consume

    def setUp(self):
        fixtures.CmsGoalBillingTests.setUp(self)
        self.hold()
        self.goal_operation.next_attempt_at = self.now
        self.outbox.client_request_id = self.key
        self.outbox.payload = {}
        self.outbox.pk = 21
        self.lookup = self.stack.enter_context(patch.object(ContentFactoryDispatchOutbox.objects, "filter",
            side_effect=lambda **kwargs: SimpleNamespace(first=lambda: self.outbox if self.outbox.payload.get("request") else None)))
        self.topic = self.stack.enter_context(patch("content_factory.website_first_article_topics.current_selection", return_value={
            "topic_candidate_id": "current-candidate", "topic": "Current approved topic", "target_keyword": "Current approved topic"}))
        self.capture_http = self.stack.enter_context(patch.object(controller.capture, "request_json", return_value=([], {"pages": 0})))
        self.goal_operation.state = "pending"
        self.goal_operation.payload.pop("lease_id", None)
        self.now += timedelta(seconds=1)
        self.run = None

    def advance(self):
        current = (self.goal_operation.next_attempt_at or self.now) + timedelta(seconds=1)
        return controller.advance_goal(self.goal_operation.pk, now=current)

    def captured(self):
        self.advance()
        item = next(row for row in self.saved if row.action == controller.capture.ACTION)
        controller.capture.advance_capture(item.pk, now=self.now + timedelta(seconds=3))
        self.assertEqual(item.state, "completed")
        return item

    def prepared(self):
        item = self.captured()
        self.advance()
        self.assertEqual(self.goal_operation.receipt["status"], "topic_selected")
        return item

    def paid_run(self):
        self.prepare_generation()
        (_payer, _ledger, _cost), payload = self.consume()
        payload.update(roo_points_authorized=True, roo_points_action="article_generation", roo_points_billing_status="charged")
        self.outbox.payload = {"request": payload, "actor_id": "founder:4"}
        self.run = ContentFactoryRun(pk=31, run_id="original-native-run", organization=self.org, domain=self.org.domain,
            workflow="article_generation", status="running", run_request=deepcopy(payload), result={"generation": 0, "state_version": 1})
        self.run.refresh_from_db = Mock()
        rows = Rows([self.run])
        self.stack.enter_context(patch.object(ContentFactoryRun.objects, "filter", side_effect=rows.filter))
        self.stack.enter_context(patch("content_factory.billing_lineage.original_article_spend_for_run", return_value=self.ledger))
        self.goal_operation.state = "pending"
        self.goal_operation.payload.pop("lease_id", None)
        self.goal_operation.next_attempt_at = self.now
        return self.run

    def test_first_leases_capture_once_then_freeze_current_approved_topic_before_any_dispatch(self):
        self.advance()
        reference = self.goal_operation.payload["capture_operation_id"]
        self.advance()
        self.assertEqual(self.goal_operation.payload["capture_operation_id"], reference)
        self.assertEqual(len([row for row in self.saved if row.action == controller.capture.ACTION]), 1)
        self.topic.assert_not_called()
        item = next(row for row in self.saved if row.action == controller.capture.ACTION)
        controller.capture.advance_capture(item.pk, now=self.now + timedelta(seconds=3))
        self.advance()
        self.assertEqual(self.goal_operation.payload["generation_data"], self.topic.return_value)
        self.assertEqual(self.topic.call_count, 1)
        self.assertEqual(self.goal_operation.receipt["status"], "topic_selected")
        self.spend.assert_called_once()

    def test_dispatch_resume_keeps_original_run_and_held_money_without_reselecting_or_starting_again(self):
        self.prepared(); self.paid_run()
        with patch("content_factory.vibe_marketing_views._start_article_generation_for_channel") as start:
            self.advance(); self.advance()
        start.assert_not_called()
        self.assertEqual(self.goal_operation.payload["source_run_id"], self.run.run_id)
        self.assertEqual(self.goal_operation.receipt["goal"]["source"]["run_generation"], 0)
        self.assertEqual(self.goal_operation.receipt["status"], "working")
        self.topic.assert_called_once(); self.spend.assert_called_once()

    def test_worker_ready_boolean_or_native_provider_ack_cannot_complete_goal(self):
        self.prepared(); self.paid_run()
        self.run.result.update(publishing_verified=True, article_live=True, provider_modified=True)
        self.advance()
        self.assertEqual(self.goal_operation.state, "pending")
        self.assertNotIn("public_url", self.goal_operation.receipt)
        rows = {row["name"]: row for row in self.goal_operation.receipt["goal"]["milestones"]}
        self.assertEqual(rows["first_article_live"]["state"], "pending")
        self.assertEqual(rows["merged"]["state"], "not_required")

    def test_goal_finishes_only_when_private_reader_supplies_every_current_native_proof(self):
        self.prepared(); self.paid_run()
        self.run.status = "completed"
        original_reader = controller.evidence.collect
        original = {"run_id": self.run.run_id, "run_generation": 0,
            "envelope_digest": self.run.run_request["publishing_channel_artifact"]["digest"]}
        def read(goal, context, config, **kwargs):
            if kwargs.get("run") is None: return original_reader(goal, context, config, **kwargs)
            proofs = {name: goals.make_proof(name, expected_scope=self.scope, original_source=original,
                subject_digest="e" * 64, at=self.now) for name in goals.KINDS}
            return proofs, original, {"first_article_live": "private-publication"}
        publication = SimpleNamespace(pk=uuid4(), state="completed")
        with patch.object(controller, "_pipeline", return_value={controller.publications.ACTION: publication}), \
                patch.object(controller.evidence, "collect", side_effect=read), \
                patch.object(controller.publications, "current_publication", return_value=(None, None, None,
                    {"public_url": "https://fixture.test/original-article/"})):
            self.advance()
        self.assertEqual((self.goal_operation.state, self.goal_operation.receipt["status"]), ("completed", "first_article_live"))
        self.assertEqual(self.goal_operation.receipt["public_url"], "https://fixture.test/original-article/")
        self.spend.assert_called_once()

    def test_original_capture_is_required_even_when_another_capture_has_complete_inventory(self):
        item = self.prepared()
        item.payload["inventory"] = new_inventory(self.binding)
        item.receipt = controller.capture._progress(item.payload["inventory"])
        self.advance()
        self.assertEqual(self.goal_operation.state, "needs_attention")
        self.assertEqual(self.goal_operation.receipt["status"], "native_recovery_required")
        self.assertIsNone(self.goal_operation.payload["source_run_id"])

    def test_expired_budget_stops_and_queues_original_settlement_without_model_or_provider_work(self):
        self.goal_operation.receipt["native_budget"]["budget_used"]["model_cost"] = 25
        self.advance()
        self.assertEqual((self.goal_operation.state, self.goal_operation.receipt["budget_stop_reason"]), ("stopped", "model_cost"))
        self.assertTrue(any(row.action == "cms-goal-refund" for row in self.saved))
        self.capture_http.assert_not_called(); self.topic.assert_not_called()

    def test_original_run_usage_is_accounted_before_more_work_and_invalid_counters_stop(self):
        self.prepared(); self.paid_run()
        self.run.result["usage"] = {"version": 1, "run_id": self.run.run_id, "generation": 0, "state_version": 1,
            "model_calls": 1, "tokens": 100, "provider_seconds": 2, "active_seconds": 2, "model_usd": 25, "builds": 0, "previews": 0}
        self.advance()
        self.assertEqual(self.goal_operation.state, "stopped")
        self.assertEqual(self.goal_operation.receipt["native_budget"]["budget_used"]["model_cost"], 25)

    def test_a_stale_generation_usage_report_cannot_reset_the_current_native_budget(self):
        self.prepared(); self.paid_run()
        self.run.result["usage"] = {"version": 1, "run_id": self.run.run_id, "generation": 1, "state_version": 1,
            "model_calls": 0, "tokens": 0, "provider_seconds": 0, "active_seconds": 0, "model_usd": 0, "builds": 0, "previews": 0}
        self.advance()
        self.assertEqual((self.goal_operation.state, self.goal_operation.receipt["budget_stop_reason"]), ("stopped", "invalid_usage_report"))

    def test_newer_worker_or_cancellation_wins_a_late_controller_result(self):
        def newer(*args, **kwargs):
            self.goal_operation.payload["lease_id"] = "newer-worker"
            return SimpleNamespace(pk=uuid4())
        with patch.object(controller.capture, "start_capture", side_effect=newer): self.advance()
        self.assertEqual(self.goal_operation.payload["lease_id"], "newer-worker")
        self.assertIsNone(self.goal_operation.payload["capture_operation_id"])
        self.assertEqual(self.goal_operation.state, "running")

    def test_abandoned_lease_reclaims_same_capture_and_stays_frozen_when_disabled(self):
        self.advance()
        reference = self.goal_operation.payload["capture_operation_id"]
        self.goal_operation.state = "running"
        self.goal_operation.payload["lease_id"] = "abandoned-worker"
        self.goal_operation.next_attempt_at = self.now - timedelta(seconds=1)
        self.advance()
        self.assertEqual(self.goal_operation.payload["capture_operation_id"], reference)
        with override_settings(ARTICLES_CMS_GOALS_ENABLED=False): self.assertIsNone(self.advance())
        self.capture_http.assert_not_called()

    def test_same_terminal_child_occurrence_is_not_counted_again_by_polling(self):
        item = self.captured()
        item.state = "blocked"
        item.receipt["failure"] = classify_failure("CONNECTION_SERVICE_TRANSIENT", "cms_capture")
        self.advance(); self.advance()
        selected = self.goal_operation.receipt["native_budget"]
        self.assertEqual(selected["attempts_by_class"]["transient"], 1)
        self.assertEqual(len(selected["failure_policy_occurrences"]), 1)

    def test_one_transient_child_failure_rearms_same_journal_and_second_failure_escalates(self):
        item = self.captured()
        item.state = "blocked"
        item.payload["page_failures"] = 5
        item.receipt["failure"] = classify_failure("CONNECTION_SERVICE_TRANSIENT", "cms_capture")
        original_inventory = deepcopy(item.payload["inventory"])
        self.advance()
        self.assertEqual((item.state, item.payload["page_failures"]), ("pending", 0))
        self.assertEqual(item.payload["inventory"], original_inventory)
        item.state = "blocked"
        item.updated_at += timedelta(seconds=1)
        self.advance()
        self.assertEqual(self.goal_operation.state, "needs_attention")
        self.assertEqual(self.goal_operation.receipt["effective_failure_class"], "repairable")
        self.spend.assert_called_once()

    def test_changed_owner_before_claim_cancels_original_goal_and_queues_held_money(self):
        self.user.is_active = False
        self.advance()
        self.assertEqual(self.goal_operation.state, "cancelled")
        self.assertEqual(self.goal_operation.receipt["status"], "authority_changed")
        self.assertTrue(any(row.action == "cms-goal-refund" for row in self.saved))
        self.capture_http.assert_not_called()

    def test_rollout_disabled_mid_claim_freezes_hold_without_replaying_or_refunding_it(self):
        def disabled(*args, **kwargs):
            self.settings_context = override_settings(ARTICLES_CMS_GOALS_ENABLED=False)
            self.settings_context.enable(); self.addCleanup(self.settings_context.disable)
            return SimpleNamespace(pk=uuid4())
        with patch.object(controller.capture, "start_capture", side_effect=disabled): self.advance()
        self.assertEqual(self.goal_operation.state, "pending")
        self.assertNotIn("lease_id", self.goal_operation.payload)
        self.assertFalse(any(row.action == "cms-goal-refund" for row in self.saved))
        self.spend.assert_called_once()

    def test_approval_arriving_before_review_wait_is_saved_does_not_lose_its_wake(self):
        self.prepared(); self.paid_run()
        stage_id, revision = str(uuid4()), "e" * 64
        pending = WebsiteConnectionOperation(action=controller.approvals.ACTION, connection=self.connection, generation=1,
            payload={"stage_operation_id": stage_id, "staged_revision": revision}, state="pending")
        self.saved.append(pending)
        self.goal_operation.state = "running"
        self.goal_operation.payload["lease_id"] = "server-lease"
        self.goal_operation.next_attempt_at = self.now + timedelta(seconds=180)
        with patch.object(controller.approvals, "_current"):
            controller._save(self.goal_operation, "server-lease", now=self.now, state="needs_user", status="review_required",
                receipt={"user_action": {"id": "review_draft", "stageId": stage_id, "reviewRevision": revision}})
        self.assertEqual((self.goal_operation.state, self.goal_operation.receipt["status"]), ("pending", "approval_pending"))
        self.assertNotIn("user_action", self.goal_operation.receipt)

    def test_manual_review_wait_is_scoped_and_exact_owner_choice_wakes_without_another_click(self):
        self.prepared(); self.paid_run()
        stage = SimpleNamespace(pk=uuid4(), connection_id=self.connection.pk, generation=1,
            state="completed",
            payload={"review_policy": "review", "staged": {"revision": "e" * 64}})
        self.run.status = "completed"
        with patch.object(controller, "_pipeline", return_value={controller.stages.ACTION: stage}): self.advance()
        self.assertEqual((self.goal_operation.state, self.goal_operation.receipt["status"]), ("needs_user", "review_required"))
        self.assertEqual(self.goal_operation.receipt["user_action"]["stageId"], str(stage.pk))
        self.assertIn("owner_wait_started_at", self.goal_operation.receipt["native_budget"])
        source = SimpleNamespace(run=self.run)
        approved_at = goals._time(self.goal_operation.receipt["native_budget"]["owner_wait_started_at"]) + timedelta(seconds=1)
        with patch.object(controller.timezone, "now", return_value=approved_at): controller.wake_for_approval(stage, source)
        self.assertEqual(self.goal_operation.state, "pending")
        self.assertNotIn("user_action", self.goal_operation.receipt)
        self.assertNotIn("owner_wait_started_at", self.goal_operation.receipt["native_budget"])
        self.spend.assert_called_once()
