"""Original native recovery leases fence provider writes and ambiguous replies."""

from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from . import cms_goal_recovery as recovery
from . import cms_goal_effects as effects
from . import tests_cms_goal_controller_unit as fixtures
from . import cms_goal_contract as goals
from .channel_artifact_contract import digest
from .cms_goal_recovery_contract import make_receipt
from .website_contract import WebsiteAuthorityError
from .website_failure_contract import classify_failure
from .website_models import WebsiteConnectionOperation


def response(body, code=200):
    return SimpleNamespace(status_code=code, content=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, json=lambda: deepcopy(body))


@override_settings(**{**fixtures.CmsGoalControllerTests._overridden_settings, "ARTICLES_CMS_GOAL_RECOVERY_ENABLED": True,
    "ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED": True, "CONTENT_FACTORY_URL": "https://factory.test", "CONTENT_FACTORY_API_KEY": "synthetic-service-key"})
class CmsGoalRecoveryTests(SimpleTestCase):
    configure = fixtures.CmsGoalControllerTests.configure
    reserve = fixtures.CmsGoalControllerTests.reserve
    hold = fixtures.CmsGoalControllerTests.hold
    prepare_generation = fixtures.CmsGoalControllerTests.prepare_generation
    consume = fixtures.CmsGoalControllerTests.consume
    paid_run = fixtures.CmsGoalControllerTests.paid_run

    def setUp(self):
        fixtures.CmsGoalControllerTests.setUp(self)
        self.payload["goal_budget"] = {"version": 1, "goal_id": str(self.goal_operation.pk), "scope_digest": digest(self.scope),
            "remaining_active_seconds": 7200, "remaining_model_usd": 25}
        self.goal_operation.next_attempt_at = self.now + timedelta(seconds=180)
        self.paid_run()
        self.run.workflow, self.run.status = "direct_generate", "failed"
        self.run.result = {"generation": 0, "state_version": 7,
            "failure": classify_failure("TEMPLATE_VALIDATION_FAILED", "assemble_article"), "failure_occurrence_id": str(uuid4()),
            "user_action_count": 0}
        self.run.result["usage"] = {"version": 1, "run_id": self.run.run_id, "generation": 0, "state_version": 7,
            "model_calls": 2, "tokens": 100, "provider_seconds": 2, "active_seconds": 2, "model_usd": 1, "builds": 0, "previews": 0}
        self.lease = "native-repair-lease"
        self.claim()
        self.stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "select_related", return_value=self.manager))
        self.transaction_state = SimpleNamespace(atomic_blocks=[], in_atomic_block=True)
        self.stack.enter_context(patch("django.db.transaction.get_connection", return_value=self.transaction_state))
        self.http_get = self.stack.enter_context(patch("integrations.http_client.get"))
        self.http_post = self.stack.enter_context(patch("integrations.http_client.post"))

    def claim(self):
        self.goal_operation.state = "running"
        self.goal_operation.payload["lease_id"] = self.lease
        self.goal_operation.next_attempt_at = self.now + timedelta(seconds=180)
        self.selector = recovery.authority.CmsGoalDispatch(str(self.goal_operation.pk), self.lease)

    def prepare(self): return recovery.prepare(self.selector, now=self.now)
    def deliver(self): return recovery.deliver(self.selector, user=self.user, context=self.context, now=self.now)

    def capable(self, plan=None, *, receipt=None):
        plan = plan or self.goal_operation.receipt["native_recovery"]
        value = plan["locator"]
        return {"schema_version": 1, "kind": "cms_goal_recovery_capability", "run_id": self.run.run_id,
            "generation": value["run_generation"] if receipt is None else receipt["target_generation"], "state_version": value["state_version"],
            "locator": value, "failure": plan["failure"] if receipt is None else None, "recovery_receipt": receipt}

    def receipt(self, plan=None):
        plan = plan or self.goal_operation.receipt["native_recovery"]
        return make_receipt(plan["locator"], step="assemble_article", queued_at=self.now.isoformat())

    def test_reserve_exact_failed_original_and_block_only_provider_work(self):
        original = deepcopy(self.outbox.payload)
        plan = self.prepare()
        self.assertEqual(plan["locator"], recovery.locator(self.goal_operation, self.run))
        self.assertEqual(plan["state"], "reserved")
        self.assertEqual(self.goal_operation.payload["source_run_id"], self.run.run_id)
        self.assertEqual(self.prepare(), plan)
        self.assertEqual(self.outbox.payload, original)
        self.assertEqual(self.spend.call_count, 1)
        self.http_get.assert_not_called(); self.http_post.assert_not_called()
        with self.assertRaises(effects.CmsGoalWorkDenied): effects.require_work(self.run.run_request, now=self.now)
        self.assertIs(effects.require_work(self.run.run_request, now=self.now, action="first-article-generation"), self.goal_operation)

    def test_supported_producer_receipt_commits_on_same_source_without_human_action(self):
        plan = self.prepare()
        receipt = self.receipt(plan)
        self.http_get.return_value = response(self.capable(plan))
        self.http_post.return_value = response(receipt)
        from .vibe_marketing_views import _control_article_run_for_channel
        result = _control_article_run_for_channel(user=self.user, context=self.context, run=self.run,
            action="resume", data={}, cms_goal_scope=self.selector)
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result.data["recovery_receipt"], receipt)
        self.assertEqual(self.goal_operation.receipt["native_recovery"]["state"], "submitted")
        self.assertEqual(self.http_post.call_args.kwargs["json"], {"locator": plan["locator"]})
        self.assertTrue(self.http_post.call_args.args[0].endswith("/cms-goal-recovery"))
        self.assertEqual(self.run.result["user_action_count"], 0)
        self.assertEqual(self.run.status, "failed")  # No optimistic local generation/queue projection.
        self.assertEqual(self.spend.call_count, 1)
        self.assertEqual(self.goal_operation.receipt["native_budget"]["budget_used"]["model_cost"], 1)

    def test_lost_post_reply_discovers_exact_memo_before_any_retransmission(self):
        from integrations import http_client
        plan = self.prepare(); receipt = self.receipt(plan)
        self.http_get.return_value = response(self.capable(plan))
        self.http_post.side_effect = http_client.RequestException("synthetic broker accepted then network closed")
        with self.assertRaises(WebsiteAuthorityError) as error: self.deliver()
        self.assertTrue(error.exception.retryable)
        self.assertEqual(self.goal_operation.receipt["native_recovery"], plan)
        self.http_get.return_value = response(self.capable(plan, receipt=receipt))
        self.assertEqual(self.deliver()["recovery_receipt"], receipt)
        self.assertEqual(self.http_post.call_count, 1)
        self.assertEqual(self.spend.call_count, 1)

    def test_unsupported_or_invalid_capability_never_posts_to_legacy_resume(self):
        plan = self.prepare()
        for body, code in (({}, 404), ({"status": "queued"}, 200), ({**self.capable(plan), "extra": True}, 200),
                ({**self.capable(plan), "generation": True}, 200), ({**self.capable(plan), "failure": None}, 200)):
            self.http_get.return_value = response(body, code)
            with self.subTest(code=code, body=body), self.assertRaises(WebsiteAuthorityError): self.deliver()
        self.http_post.assert_not_called()

    def test_wrong_source_or_rechecksummed_receipt_cannot_clear_barrier(self):
        from .cms_goal_recovery_contract import make_locator
        plan = self.prepare()
        fields = {k: v for k, v in plan["locator"].items() if k not in {"schema_version", "kind", "digest"}}
        foreign = make_locator(**{**fields, "run_id": "foreign-run"})
        self.http_get.return_value = response({**self.capable(plan), "locator": foreign})
        with self.assertRaises(WebsiteAuthorityError): self.deliver()
        self.http_post.assert_not_called()
        self.http_get.return_value = response(self.capable(plan))
        self.http_post.return_value = response(make_receipt(foreign, step="assemble_article", queued_at=self.now.isoformat()))
        with self.assertRaises(WebsiteAuthorityError): self.deliver()
        self.assertTrue(recovery.blocks_provider_work(self.goal_operation))

    def test_current_lease_owner_and_source_are_rechecked_before_post_and_commit(self):
        plan = self.prepare()
        def revoked(*args, **kwargs):
            self.goal_operation.payload["lease_id"] = "newer-lease"
            return response(self.capable(plan))
        self.http_get.side_effect = revoked
        with self.assertRaises(WebsiteAuthorityError): self.deliver()
        self.http_post.assert_not_called()
        self.claim(); self.http_get.side_effect = None; self.http_get.return_value = response(self.capable(plan))
        def late(*args, **kwargs):
            self.goal_operation.payload["lease_id"] = "another-newer-lease"
            return response(self.receipt(plan))
        self.http_post.side_effect = late
        with self.assertRaises(WebsiteAuthorityError): self.deliver()
        self.assertEqual(self.goal_operation.receipt["native_recovery"], plan)

    def test_capability_and_post_are_outside_explicit_database_transactions(self):
        from contextlib import contextmanager
        from .vibe_marketing_views import require_unlocked_remote_call
        @contextmanager
        def atomic():
            block = SimpleNamespace(_from_testcase=False)
            self.transaction_state.atomic_blocks.append(block)
            try: yield
            finally: self.transaction_state.atomic_blocks.remove(block)
        with patch("django.db.transaction.atomic", side_effect=atomic), \
                patch("content_factory.website_connections.require_unlocked_remote_call", side_effect=require_unlocked_remote_call):
            plan = self.prepare()
            def observe(*args, **kwargs):
                self.assertEqual(self.transaction_state.atomic_blocks, [])
                return response(self.capable(plan))
            def reserve(*args, **kwargs):
                self.assertEqual(self.transaction_state.atomic_blocks, [])
                return response(self.receipt(plan))
            self.http_get.side_effect = observe; self.http_post.side_effect = reserve
            self.deliver()

    def test_changed_failure_or_missing_original_payment_cannot_issue_resume(self):
        plan = self.prepare()
        self.http_get.return_value = response(self.capable(plan))
        self.run.result["state_version"] += 1
        with self.assertRaises(WebsiteAuthorityError): self.deliver()
        self.run.result["state_version"] -= 1
        self.refunds.return_value.exists.return_value = True
        with self.assertRaises(WebsiteAuthorityError): self.deliver()
        self.http_get.assert_called_once(); self.http_post.assert_not_called()

    def test_lost_reply_memo_is_readable_after_a_later_same_generation_snapshot(self):
        plan = self.prepare(); receipt = self.receipt(plan)
        self.run.result["state_version"] += 1
        self.http_get.return_value = response(self.capable(plan, receipt=receipt))
        self.assertEqual(self.deliver()["recovery_receipt"], receipt)
        self.http_post.assert_not_called()

    def test_unknown_cost_or_active_time_never_reserves_a_repair(self):
        for key in ("model_usd", "active_seconds"):
            value = self.run.result["usage"][key]
            self.run.result["usage"][key] = None
            with self.assertRaises(WebsiteAuthorityError): self.prepare()
            self.assertNotIn("native_recovery", self.goal_operation.receipt)
            self.run.result["usage"][key] = value

    def test_feature_pause_and_corrupted_private_plan_stop_model_and_provider_work(self):
        self.prepare()
        with override_settings(ARTICLES_CMS_GOAL_RECOVERY_ENABLED=False):
            with self.assertRaises(WebsiteAuthorityError): self.deliver()
            for action in (None, "first-article-generation"):
                with self.assertRaises(effects.CmsGoalWorkDenied): effects.require_work(self.run.run_request, now=self.now, action=action)
        self.goal_operation.receipt["native_recovery"]["locator"]["run_generation"] = 9
        for action in (None, "first-article-generation"):
            with self.assertRaises(effects.CmsGoalWorkDenied): effects.require_work(self.run.run_request, now=self.now, action=action)
        self.http_get.assert_not_called(); self.http_post.assert_not_called()

    def test_rechecksummed_foreign_envelope_in_private_plan_cannot_admit_original_model_work(self):
        from .cms_goal_recovery_contract import make_locator
        plan = self.prepare()
        fields = {k: v for k, v in plan["locator"].items() if k not in {"schema_version", "kind", "digest"}}
        plan["locator"] = make_locator(**{**fields, "envelope_digest": "f" * 64})
        self.goal_operation.receipt["native_recovery"] = plan
        for action in (None, "first-article-generation"):
            with self.assertRaises(effects.CmsGoalWorkDenied): effects.require_work(self.run.run_request, now=self.now, action=action)
        self.http_get.assert_not_called(); self.http_post.assert_not_called()

    def test_public_json_and_generic_worker_action_cannot_create_private_native_selector(self):
        self.prepare()
        from .vibe_marketing_views import _call_content_factory_run_action, _control_article_run_for_channel
        with self.assertRaises(WebsiteAuthorityError): recovery.prepare({"operation_id": str(self.goal_operation.pk), "lease_id": self.lease})
        result = _call_content_factory_run_action(run_id=self.run.run_id, action="resume", payload={"cms_goal_scope": self.selector.operation_id})
        self.assertFalse(result["allowed"])
        with self.assertRaises(WebsiteAuthorityError): _control_article_run_for_channel(user=self.user, context=self.context, run=self.run,
            action="resume", data={"force": True}, cms_goal_scope=self.selector)
        self.http_get.assert_not_called(); self.http_post.assert_not_called()

    def test_issued_native_media_and_post_prevent_any_source_repair(self):
        from . import cms_media_contract as media, cms_post_contract as posts
        from .tests_cms_media_contract_unit import intent as media_intent
        from .tests_cms_post_contract_unit import intent as post_intent
        base, image = media_intent()
        fields = {key: value for key, value in base.items() if key not in {"schema_version", "kind", "digest"}}
        item = media.make_intent(**{**fields, "source_run_id": self.run.run_id})
        state = media.issue_upload(item, media.observe_lookup(item, media.new_state(item), [], image=image))
        row = WebsiteConnectionOperation(connection=self.connection, generation=self.connection.generation, action="cms-media",
            payload={"intents": [item], "states": [state]})
        self.saved.append(row)
        with self.assertRaises(WebsiteAuthorityError): self.prepare()
        self.assertNotIn("native_recovery", self.goal_operation.receipt)
        self.saved.remove(row)
        item = post_intent(source_run_id=self.run.run_id)
        ready = posts.observe_posts(item, posts.new_post_state(item), [])
        state, _effect = posts.issue_create(item, ready)
        row = WebsiteConnectionOperation(connection=self.connection, generation=self.connection.generation, action="cms-publish",
            payload={"intent": item, "post_state": state})
        self.saved.append(row)
        with self.assertRaises(WebsiteAuthorityError): self.prepare()
        self.assertNotIn("native_recovery", self.goal_operation.receipt)
        self.http_get.assert_not_called(); self.http_post.assert_not_called()

    def test_observed_receipt_alone_cannot_admit_old_generation_provider_effects(self):
        plan = self.prepare()
        plan.update(state="observed", receipt=self.receipt(plan))
        self.goal_operation.receipt["native_recovery"] = plan
        self.assertTrue(recovery.blocks_provider_work(self.goal_operation))
        self.run.result["generation"] = 1
        self.goal_operation.receipt["goal"] = goals.bind_source(self.goal_operation.receipt["goal"], {
            "run_id": self.run.run_id, "run_generation": 1, "envelope_digest": plan["locator"]["envelope_digest"]}, expected_scope=self.scope, now=self.now)
        self.assertFalse(recovery.blocks_provider_work(self.goal_operation))


class CmsGoalRecoveryContractTests(SimpleTestCase):
    def test_immutable_producer_pin_and_example_roundtrip(self):
        from scripts.check_cms_goal_recovery_contract import ROOT, verify
        from .cms_goal_recovery_contract import validate_locator, validate_receipt
        lock = verify()
        self.assertEqual(lock["source_revision"], "efca859a1f6781edeb0c286f9286f519f2d0d0b3")
        value = json.loads((ROOT / "contracts/cms-goal-recovery-v1.example.json").read_text())
        self.assertEqual(validate_locator(value["locator"]), value["locator"])
        self.assertEqual(validate_receipt(value["receipt"], value["locator"]), value["receipt"])

    def test_private_source_pin_requires_exact_remote_bytes_and_read_access(self):
        from scripts.check_cms_goal_recovery_contract import ROOT, PAIRS, verify
        from scripts.check_failure_contract import ContractCheckError
        observed = []
        def fetch(path, revision, token):
            observed.append((path, revision, token))
            return (ROOT / PAIRS[path]).read_bytes()
        result = verify(remote=True, token="synthetic-read-token", fetch=fetch)
        self.assertEqual(observed, [(row["source_path"], result["source_revision"], "synthetic-read-token") for row in result["files"]])
        with self.assertRaises(ContractCheckError): verify(remote=True)
        with self.assertRaises(ContractCheckError): verify(remote=True, token="synthetic-read-token", fetch=lambda *args: b"changed")


@override_settings(**CmsGoalRecoveryTests._overridden_settings)
class CmsGoalRecoveryControllerTests(SimpleTestCase):
    configure = fixtures.CmsGoalControllerTests.configure
    reserve = fixtures.CmsGoalControllerTests.reserve
    hold = fixtures.CmsGoalControllerTests.hold
    prepare_generation = fixtures.CmsGoalControllerTests.prepare_generation
    consume = fixtures.CmsGoalControllerTests.consume
    paid_run = fixtures.CmsGoalControllerTests.paid_run
    advance = fixtures.CmsGoalControllerTests.advance
    captured = fixtures.CmsGoalControllerTests.captured
    prepared = fixtures.CmsGoalControllerTests.prepared
    capable = CmsGoalRecoveryTests.capable
    receipt = CmsGoalRecoveryTests.receipt

    def setUp(self):
        fixtures.CmsGoalControllerTests.setUp(self)
        self.prepared()
        self.payload["goal_budget"] = {"version": 1, "goal_id": str(self.goal_operation.pk), "scope_digest": digest(self.scope),
            "remaining_active_seconds": 7200, "remaining_model_usd": 25}
        self.paid_run()
        self.run.workflow, self.run.status = "direct_generate", "failed"
        self.run.result = {"generation": 0, "state_version": 7,
            "failure": classify_failure("TEMPLATE_VALIDATION_FAILED", "assemble_article"), "failure_occurrence_id": str(uuid4()),
            "user_action_count": 0}
        self.usage(0, 7)
        self.stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "select_related", return_value=self.manager))
        self.stack.enter_context(patch("django.db.transaction.get_connection", return_value=SimpleNamespace(atomic_blocks=[], in_atomic_block=True)))
        self.http_get = self.stack.enter_context(patch("integrations.http_client.get"))
        self.http_post = self.stack.enter_context(patch("integrations.http_client.post"))

    def usage(self, generation, state_version):
        self.run.result["usage"] = {"version": 1, "run_id": self.run.run_id, "generation": generation, "state_version": state_version,
            "model_calls": generation + 2, "tokens": 100 + generation, "provider_seconds": 2 + generation,
            "active_seconds": 2 + generation, "model_usd": 1 + generation, "builds": 0, "previews": 0}

    def reserve_repair(self):
        self.advance()
        self.assertEqual(self.goal_operation.receipt["status"], "native_recovery_pending")
        self.assertNotIn("native_recovery", self.goal_operation.receipt)
        self.advance()
        self.assertEqual(self.goal_operation.receipt["native_recovery"]["state"], "reserved")
        self.http_get.return_value = response(self.capable())
        self.http_post.return_value = response(self.receipt())

    def test_controller_repairs_original_then_waits_for_real_source_without_a_new_charge_or_human_action(self):
        self.reserve_repair()
        self.advance()
        self.assertEqual(self.goal_operation.receipt["native_recovery"]["state"], "submitted")
        self.advance()
        self.assertEqual(self.http_post.call_count, 1)
        self.assertEqual(self.run.result["user_action_count"], 0)
        self.assertFalse(any(row.action == "cms-goal-refund" for row in self.saved))
        self.run.status = "running"
        self.run.result = {"generation": 1, "state_version": 1, "user_action_count": 0}
        self.usage(1, 1)
        self.advance()
        self.assertEqual(self.goal_operation.receipt["native_recovery"]["state"], "observed")
        self.assertEqual(self.goal_operation.receipt["goal"]["source"]["run_generation"], 1)
        self.assertEqual(self.goal_operation.payload["source_run_id"], self.run.run_id)
        self.assertEqual(self.goal_operation.receipt["native_budget"]["budget_used"]["model_cost"], 2)
        self.assertEqual(self.goal_operation.state, "pending")
        self.spend.assert_called_once(); self.topic.assert_called_once()
        self.assertEqual(self.http_post.call_count, 1)

    def test_same_causal_failure_polling_does_not_count_a_heartbeat_as_another_failure(self):
        self.advance()
        occurrence = self.goal_operation.receipt["failure_occurrence_id"]
        self.run.result["state_version"] += 1
        self.usage(0, 8)
        self.advance()
        selected = self.goal_operation.receipt["native_budget"]
        self.assertEqual(selected["attempts_by_class"]["repairable"], 1)
        self.assertEqual(len(selected["failure_policy_occurrences"]), 1)
        self.assertEqual(self.goal_operation.receipt["failure_occurrence_id"], occurrence)
        self.assertEqual(self.goal_operation.receipt["native_recovery"]["locator"]["state_version"], 8)
        self.http_get.assert_not_called(); self.http_post.assert_not_called()

    def test_new_real_failure_after_repair_escalates_same_fingerprint_without_repeating_repair(self):
        self.reserve_repair(); self.advance()
        self.run.result.update(generation=1, state_version=2, failure_occurrence_id=str(uuid4()))
        self.usage(1, 2)
        self.advance()
        self.assertEqual((self.goal_operation.state, self.goal_operation.receipt["effective_failure_class"]), ("needs_attention", "strategy"))
        self.assertEqual(self.goal_operation.receipt["native_budget"]["attempts_by_class"]["repairable"], 2)
        self.assertEqual(self.http_post.call_count, 1)
        self.assertTrue(any(row.action == "cms-goal-refund" for row in self.saved))
        self.spend.assert_called_once()

    def test_pending_repair_pauses_without_losing_its_original_hold_when_feature_is_disabled(self):
        self.reserve_repair()
        before = deepcopy(self.goal_operation.receipt["first_article_billing"])
        with override_settings(ARTICLES_CMS_GOAL_RECOVERY_ENABLED=False): self.advance()
        self.assertEqual((self.goal_operation.state, self.goal_operation.receipt["status"]), ("pending", "native_recovery_paused"))
        self.assertEqual(self.goal_operation.receipt["first_article_billing"], before)
        self.assertFalse(any(row.action == "cms-goal-refund" for row in self.saved))
        self.http_get.assert_not_called(); self.http_post.assert_not_called()

    def test_unknown_post_reply_retries_same_plan_without_consuming_another_failure_occurrence(self):
        from integrations import http_client
        self.reserve_repair()
        plan = deepcopy(self.goal_operation.receipt["native_recovery"])
        self.http_post.side_effect = http_client.RequestException("synthetic accepted before disconnect")
        self.advance()
        self.assertEqual((self.goal_operation.state, self.goal_operation.receipt["status"]), ("pending", "native_recovery_pending"))
        self.assertEqual(self.goal_operation.receipt["native_recovery"], plan)
        self.http_get.return_value = response(self.capable(plan, receipt=self.receipt(plan)))
        self.advance()
        self.assertEqual(self.goal_operation.receipt["native_recovery"]["state"], "submitted")
        self.assertEqual(self.goal_operation.receipt["native_budget"]["attempts_by_class"]["repairable"], 1)
        self.assertEqual(self.http_post.call_count, 1)
        self.assertFalse(any(row.action == "cms-goal-refund" for row in self.saved))
