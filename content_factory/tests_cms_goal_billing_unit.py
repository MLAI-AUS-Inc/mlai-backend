"""Native first-article holds keep the original payer/amount through dispatch."""

from contextlib import ExitStack
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from . import cms_goal_authority as authority
from . import cms_goal_billing as billing
from . import cms_goal_consent as consent
from . import channel_artifacts
from . import tests_cms_goal_consent_unit as fixtures
from .channel_artifact_contract import digest
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnectionOperation
from roo.models import Ledger


@override_settings(ARTICLES_CHANNEL_DISCOVERY_ENABLED=True, ARTICLES_PUBLISHING_CHANNELS_ENABLED=True,
    ARTICLES_CMS_CHANNEL_ENABLED=True, ARTICLES_CHANNEL_ARTIFACTS_ENABLED=True, ARTICLES_CMS_GOALS_ENABLED=True,
    ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"], ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
    ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"],
    CONNECTOR_CREDENTIAL_KEYS=json.dumps({"test": fixtures.fixtures.TEST_KEY}), CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID="test")
class CmsGoalBillingTests(SimpleTestCase):
    configure = fixtures.CmsGoalConsentTests.configure
    reserve = fixtures.CmsGoalConsentTests.reserve

    def setUp(self):
        fixtures.CmsGoalConsentTests.setUp(self)
        self.goal_operation.state = "pending"
        self.goal_operation.payload.pop("lease_id")
        from .cms_goal_budget import initial
        self.goal_operation.receipt["native_budget"] = initial(self.goal)
        value = authority.current(self.goal_operation)
        self.key = billing.request_key(value)
        self.payer = SimpleNamespace(pk=5, is_active=True)
        self.ledger = SimpleNamespace(pk=11, id=11, user_id=5, user=self.payer, kind="SPEND", source="CONTENT_FACTORY",
            reference_type="CONTENT_FACTORY", reference_id=self.key, idempotency_key="content_factory:charge:" + self.key,
            delta_microroo=-6_000_000)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.spend = self.stack.enter_context(patch("roo.services.PointsService.spend", return_value=(self.ledger, True)))
        self.price = self.stack.enter_context(patch("content_factory.billing.get_content_factory_article_cost_points", return_value=6))
        self.payer_lookup = self.stack.enter_context(patch("content_factory.company_billing.billing_user_for_organization", return_value=self.payer))
        self.stack.enter_context(patch("founder_tools.services.founder_actor_id_for_user", return_value="founder:4"))
        self.stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "select_for_update", return_value=self.manager))
        self.ledger_rows = self.stack.enter_context(patch.object(Ledger.objects, "select_for_update"))
        self.ledger_rows.return_value.select_related.return_value.filter.return_value.first.return_value = self.ledger
        self.refunds = self.stack.enter_context(patch.object(Ledger.objects, "filter"))
        self.refunds.return_value.exists.return_value = False
        self.dispatch = authority.CmsGoalDispatch(str(self.goal_operation.pk), "server-lease")

    def hold(self):
        return billing.reserve(self.goal_operation, user=self.user, now=self.now)

    def prepare_generation(self):
        self.hold()
        self.goal_operation.state = "running"
        self.goal_operation.payload["lease_id"] = self.dispatch.lease_id
        self.payload.update(client_request_id=self.key, cms_goal_operation_id=str(self.goal_operation.pk), cms_goal_id=self.goal["goal_id"])
        scope = channel_artifacts.prepare_generation(self.context, self.config, user=self.user)
        return channel_artifacts.reserve_generation(scope, context=self.context, config=self.config, user=self.user, payload=self.payload)

    def consume(self, **changes):
        payload = {**deepcopy(self.payload), **changes}
        result = billing.consume(self.dispatch, context=self.context, user=self.user, payload=payload)
        return result, payload

    def test_native_hold_debits_once_and_consumption_keeps_original_payer_and_accepted_price(self):
        held = self.hold()
        self.assertEqual(self.hold(), held)
        self.assertEqual(self.spend.call_count, 1)
        self.assertIs(self.spend.call_args.kwargs["user"], self.payer)
        self.prepare_generation()
        self.price.return_value = 99
        self.payer_lookup.return_value = SimpleNamespace(pk=99, is_active=True)
        (payer, ledger, cost), payload = self.consume()
        self.consume()
        self.assertEqual((payer, ledger, cost), (self.payer, self.ledger, 6))
        self.assertEqual((payload["roo_points_requested_by_user_id"], payload["roo_points_billing_user_id"]), (4, 5))
        self.assertEqual(billing.generation_key(self.dispatch, context=self.context, user=self.user), self.key)
        self.assertEqual(self.spend.call_count, 1)
        self.assertEqual(self.goal_operation.receipt["first_article_billing"]["status"], "reserved")

    def test_canonical_charge_path_reserves_native_artifact_and_outbox_using_the_original_hold(self):
        self.hold()
        self.goal_operation.state = "running"
        self.goal_operation.payload["lease_id"] = self.dispatch.lease_id
        scope = channel_artifacts.prepare_generation(self.context, self.config, user=self.user)
        from . import vibe_marketing_views as views
        request = SimpleNamespace(user=self.user, data={"expectedCostPoints": 6})
        self.price.return_value = 99
        with patch.object(views, "_refresh_article_editorial_payload", return_value=None), \
                patch.object(views, "_validate_article_start_admission"), patch.object(views, "_get_config", return_value=self.config), \
                patch.object(views, "_roo_points_balance_for_user", return_value=20), \
                patch.object(views, "charge_content_factory_request_for_user") as charge, \
                patch("content_factory.dispatch_outbox.reserve_dispatch") as outbox:
            payer, ledger, _request, error = views._charge_roo_points_for_article(request, context=self.context,
                payload=self.payload, channel_artifact_scope=scope, cms_goal_scope=self.dispatch)
        self.assertIsNone(error)
        self.assertEqual((payer, ledger), (self.payer, self.ledger))
        charge.assert_not_called()
        self.assertEqual(self.spend.call_count, 1)
        self.assertEqual(outbox.call_args.kwargs["payload"]["client_request_id"], self.key)
        self.assertEqual(outbox.call_args.kwargs["payload"]["roo_points_ledger_id"], "11")
        self.assertEqual(outbox.call_args.kwargs["payload"]["roo_points_cost"], 6)
        self.assertEqual(self.payload["cms_goal_operation_id"], str(self.goal_operation.pk))
        native = next(row for row in self.saved if row.action == channel_artifacts.ACTION)
        self.assertEqual(native.payload["request_sha256"], channel_artifacts._request_identity(self.payload))

    def test_canonical_charge_path_rejects_a_json_goal_selector_or_git_goal_mix_before_reserving(self):
        self.hold()
        self.goal_operation.state = "running"
        self.goal_operation.payload["lease_id"] = self.dispatch.lease_id
        from . import vibe_marketing_views as views
        scope = channel_artifacts.prepare_generation(self.context, self.config, user=self.user)
        request = SimpleNamespace(user=self.user, data={})
        with patch.object(views, "_refresh_article_editorial_payload") as editorial:
            *_result, error = views._charge_roo_points_for_article(request, context=self.context, payload=self.payload,
                channel_artifact_scope=scope, cms_goal_scope=self.dispatch.__dict__)
            self.assertEqual(error.status_code, 409)
            *_result, error = views._charge_roo_points_for_article(request, context=self.context, payload=self.payload,
                channel_artifact_scope=scope, cms_goal_scope=self.dispatch, goal_operation_id="repository-goal")
            self.assertEqual(error.status_code, 409)
        editorial.assert_not_called()
        self.assertFalse(any(row.action == channel_artifacts.ACTION for row in self.saved))

    def test_current_quote_change_or_insufficient_funds_cannot_leave_a_native_hold(self):
        self.price.return_value = 7
        with self.assertRaises(WebsiteAuthorityError): self.hold()
        self.spend.assert_not_called()
        self.price.return_value = 6
        from roo.permissions import InsufficientBalanceError
        self.spend.side_effect = InsufficientBalanceError("synthetic balance")
        with self.assertRaises(WebsiteAuthorityError) as failure: self.hold()
        self.assertEqual(failure.exception.status, 402)
        self.assertNotIn("first_article_billing", self.goal_operation.receipt)

    def test_explicit_original_free_hold_needs_no_debit_but_missing_paid_spend_is_not_free(self):
        self.goal_operation.payload["consent"] = consent.seal(self.goal, user_id=self.user.pk, review_policy="auto",
            expected_points=0, confirmed=True, now=self.now)
        self.price.return_value = 0
        held = self.hold()
        self.assertEqual((held["cost_points"], held["spend_ledger_id"]), (0, None))
        self.spend.assert_not_called()
        self.prepare_generation()
        (_payer, ledger, cost), _payload = self.consume()
        self.assertEqual((ledger, cost), (None, 0))

    def test_missing_wrong_payer_or_wrong_original_amount_cannot_be_requoted_for_native_generation(self):
        self.prepare_generation()
        original = deepcopy(self.ledger.__dict__)
        for changed in (None, SimpleNamespace(**{**original, "user_id": 99}),
                SimpleNamespace(**{**original, "delta_microroo": -5_000_000}),
                SimpleNamespace(**{**original, "reference_id": "other"})):
            self.ledger_rows.return_value.select_related.return_value.filter.return_value.first.return_value = changed
            with self.subTest(changed=changed), self.assertRaises(WebsiteAuthorityError): self.consume()
        self.assertEqual(self.spend.call_count, 1)

    def test_credit_committed_before_receipt_stamp_denies_generation(self):
        self.prepare_generation()
        self.refunds.return_value.exists.return_value = True
        with self.assertRaises(WebsiteAuthorityError): self.consume()

    def test_owner_body_or_worker_cannot_pick_a_financial_scope_or_change_sealed_goal_metadata(self):
        self.prepare_generation()
        for raw in (None, self.dispatch.__dict__, {"operation_id": str(self.goal_operation.pk), "lease_id": "server-lease"}):
            with self.assertRaises(WebsiteAuthorityError): billing.consume(raw, context=self.context, user=self.user, payload=self.payload)
        for changed in ({"cms_goal_operation_id": str(uuid4())}, {"cms_goal_id": str(uuid4())},
                {"client_request_id": "other"}, {"github_repo": "customer/site"}, {"delivery_mode": "publish_code"}):
            with self.subTest(changed=changed), self.assertRaises(WebsiteAuthorityError): self.consume(**changed)
        from .service_views import _merge_django_owned_run_request
        from .editorial_run_state import EditorialRunConflict
        original = {"cms_goal_operation_id": str(self.goal_operation.pk), "cms_goal_id": self.goal["goal_id"]}
        self.assertEqual(_merge_django_owned_run_request(original, {}), original)
        self.assertEqual(_merge_django_owned_run_request({}, original), {})
        with self.assertRaises(EditorialRunConflict): _merge_django_owned_run_request(original, {"cms_goal_id": "other"})

    def test_rechecksummed_boolean_financial_binding_is_not_the_original_held_scope(self):
        self.prepare_generation()
        receipt = self.goal_operation.receipt["first_article_billing"]
        receipt["binding"]["scope"]["binding"]["generation"] = True
        receipt["digest"] = digest({key: item for key, item in receipt.items() if key != "digest"})
        with self.assertRaises(WebsiteAuthorityError): self.consume()

    def test_historical_native_financial_receipt_survives_rollout_or_credential_revocation_without_authorizing_dispatch(self):
        self.prepare_generation()
        held = deepcopy(self.goal_operation.receipt["first_article_billing"])
        self.private.state = "deleted"
        with self.assertRaises(WebsiteAuthorityError): self.consume()
        self.assertEqual(billing.historical_reservation(self.goal_operation), held)
        self.private.state = "completed"
        with override_settings(ARTICLES_CMS_GOALS_ENABLED=False), self.assertRaises(WebsiteAuthorityError): self.consume()
        self.assertEqual(billing.historical_reservation(self.goal_operation), held)

    def test_superseding_goal_or_lost_lease_cannot_consume_another_original_hold(self):
        self.prepare_generation()
        self.goal_operation.payload["lease_id"] = "newer-lease"
        with self.assertRaises(WebsiteAuthorityError): self.consume()
        self.assertEqual(self.spend.call_count, 1)

    def test_reservation_requires_the_canonical_start_transaction(self):
        with patch("django.db.transaction.get_connection", return_value=SimpleNamespace(in_atomic_block=False)), self.assertRaises(WebsiteAuthorityError):
            self.hold()
        self.spend.assert_not_called()

    def test_native_terminal_credit_is_original_idempotent_and_survives_rollout_or_key_revocation(self):
        self.hold()
        self.goal_operation.state = "cancelled"
        self.private.state = "deleted"
        credit = SimpleNamespace(user_id=5, delta_microroo=6_000_000, idempotency_key="content_factory:refund:" + self.key)
        with override_settings(ARTICLES_CMS_GOALS_ENABLED=False), patch("integrations.services.article_generation._refund_content_factory_request", return_value=credit) as refund:
            result = billing.release(self.goal_operation, reason="Synthetic cancelled setup")
            self.assertEqual(result["status"], "refunded")
            self.assertEqual(billing.release(self.goal_operation, reason="Repeated cancellation"), result)
        refund.assert_called_once()
        self.assertIs(refund.call_args.kwargs["user"], self.payer)
        self.assertEqual(refund.call_args.kwargs["article_request"]["client_request_id"], self.key)

    def test_missing_credit_acknowledgement_cannot_mark_an_original_hold_refunded(self):
        self.hold(); self.goal_operation.state = "cancelled"
        with patch("integrations.services.article_generation._refund_content_factory_request", return_value=None), self.assertRaises(WebsiteAuthorityError):
            billing.release(self.goal_operation, reason="Synthetic credit wait")
        self.assertEqual(self.goal_operation.receipt["first_article_billing"]["status"], "reserved")

    def test_a_generated_original_draft_retains_charge_after_owner_cancellation(self):
        self.prepare_generation()
        from . import cms_goal_contract as goals
        original = {"run_id": "original-native-run", "run_generation": 0, "envelope_digest": self.payload["publishing_channel_artifact"]["digest"]}
        value = goals.bind_source(self.goal, original, expected_scope=self.scope, now=self.now)
        proof = goals.make_proof("first_draft", expected_scope=self.scope, original_source=original, subject_digest="e" * 64, at=self.now)
        self.goal_operation.receipt["goal"] = goals.refresh(value, {"first_draft": proof}, expected_scope=self.scope, now=self.now)
        self.goal_operation.payload["source_run_id"] = original["run_id"]
        self.goal_operation.state = "cancelled"
        with patch("integrations.services.article_generation._refund_content_factory_request") as refund:
            result = billing.release(self.goal_operation, reason="Synthetic cancellation after original work")
        self.assertEqual(result["status"], "reserved"); refund.assert_not_called()
        value = goals.bind_source(self.goal_operation.receipt["goal"], {**original, "run_generation": 1}, expected_scope=self.scope, now=self.now)
        self.goal_operation.receipt["goal"] = value
        self.assertTrue(billing.retains_after_draft(self.goal_operation))

    def test_child_refund_uses_original_backend_outbox_even_when_worker_drops_or_changes_goal_fields(self):
        self.prepare_generation()
        (_payer, _ledger, _cost), payload = self.consume()
        self.outbox.payload = {"request": payload}
        packet = {"client_request_id": self.key, "cms_goal_operation_id": "worker-changed-reference"}
        self.assertTrue(billing.defer_child_refund(packet, resolved_domain="fixture.test"))
        from .website_first_article_billing import defer_child_refund
        self.assertTrue(defer_child_refund({"client_request_id": self.key}, resolved_domain="fixture.test"))
        self.assertFalse(billing.defer_child_refund(packet, resolved_domain="foreign.test"))
        self.goal_operation.state = "cancelled"
        self.assertFalse(billing.defer_child_refund(packet, resolved_domain="fixture.test"))
        self.goal_operation.state = "running"
        self.outbox.payload["request"]["cms_goal_id"] = "other-goal"
        self.assertFalse(billing.defer_child_refund(packet, resolved_domain="fixture.test"))

    def test_active_native_hold_cannot_be_released_before_its_terminal_state_wins(self):
        self.hold()
        with patch("integrations.services.article_generation._refund_content_factory_request") as refund, self.assertRaises(WebsiteAuthorityError):
            billing.release(self.goal_operation, reason="Synthetic premature credit")
        refund.assert_not_called()

    def test_private_accepted_handoff_retains_generation_charge_even_before_parent_collects_draft_proof(self):
        self.prepare_generation()
        (_payer, _ledger, _cost), payload = self.consume()
        payload.update(roo_points_authorized=True, roo_points_action="article_generation", roo_points_billing_status="charged")
        self.outbox.payload = {"request": deepcopy(payload)}
        from workflow_runs.models import ContentFactoryRun
        from . import cms_artifact_review as reviews
        from .tests_cms_artifact_review_unit import fixture, seal
        from .tests_cms_capture_unit import Rows
        run = ContentFactoryRun(pk=16, organization=self.org, run_id="original-native-run", domain=self.org.domain,
            workflow="article_generation", status="completed", run_request=payload, result={"generation": 0})
        value = fixture()
        value.update(run_id=run.run_id, run_generation=0, envelope=payload["publishing_channel_artifact"]); seal(value)
        with patch.object(ContentFactoryRun.objects, "filter", side_effect=Rows([run]).filter), \
                patch.object(ContentFactoryRun.objects, "select_for_update", return_value=Rows([run])), \
                patch.object(reviews, "fetch_handoff", return_value=value):
            review = reviews.start_review(run)
            reviews.advance_review(review.pk, now=self.now + timedelta(seconds=1))
        self.assertEqual(review.state, "completed")
        self.assertIsNone(self.goal_operation.receipt["goal"]["source"])
        self.assertTrue(billing.retains_after_draft(self.goal_operation))
        self.goal_operation.state = "cancelled"
        with patch("integrations.services.article_generation._refund_content_factory_request") as refund:
            self.assertEqual(billing.release(self.goal_operation, reason="Synthetic cancellation")["status"], "reserved")
        refund.assert_not_called()
