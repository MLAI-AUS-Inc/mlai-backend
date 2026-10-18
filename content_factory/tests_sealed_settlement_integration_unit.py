"""Combined publishing-proof admission and original-payment settlement boundaries."""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from . import tests_sealed_workflow_unit as sealed_fixtures
from . import tests_website_first_article_settlement_unit as payment_fixtures
from . import website_first_article_billing as billing, website_sealed_workflow as sealed
from . import website_goal as goals, website_setup_consent as consent, vibe_marketing_views as views
from .activation import durable_integration_evidence
from .website_connections import contract_for
from .website_contract import WebsiteAuthorityError


@override_settings(ARTICLES_PUBLISHING_INPUTS_V3_ENABLED=False, ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=True,
    ARTICLES_SETUP_CONSENT_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"], ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
    ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class SealedAdmissionBillingTests(SimpleTestCase):
    setUp = sealed_fixtures.SealedWorkflowTests.setUp
    boundaries = sealed_fixtures.SealedWorkflowTests.boundaries
    pending = sealed_fixtures.SealedWorkflowTests.pending
    promote = sealed_fixtures.SealedWorkflowTests.promote

    @contextmanager
    def charge_boundaries(self):
        """Replace persistence/provider I/O, retaining readiness, admission and billing policy."""
        from integrations.services import article_generation as generation
        from .models import OrganizationContentConfig
        from .website_models import WebsiteConnectionOperation
        from roo.models import Ledger
        from roo.services import PointsService
        value = self.value
        self.user = value.user
        goal = goals.new_goal(str(value.prepare.pk), goals.scope_for(value.website, "9"),
            target="first_article_live", now=value.now)
        goal["consent"] = consent.seal(goal, deepcopy(value.prepare.receipt["goal"]["consent"]["answers"]),
            user_id=value.user.pk, now=value.now)
        value.prepare.receipt["goal"] = goal
        self.ledger = SimpleNamespace(pk=11, id=11, user_id=value.user.pk, user=value.user, kind="SPEND",
            source="CONTENT_FACTORY", reference_type="CONTENT_FACTORY", reference_id=billing.request_key(goal),
            idempotency_key="content_factory:charge:" + billing.request_key(goal), delta_microroo=-6_000_000)
        with self.boundaries(), value.ExitStack() as stack:
            stack.enter_context(patch.object(generation, "_validate_authenticated_content_factory_actor"))
            stack.enter_context(patch("content_factory.activation.founder_context_for_domain", return_value=value.context))
            stack.enter_context(patch.object(views, "_latest_runs_for_org", return_value=[]))
            stack.enter_context(patch.object(views, "_github_account_for_context", return_value={"saved": True, "owned": True}))
            stack.enter_context(patch.object(views, "_verify_github_repository_access", return_value={
                "verified": True, "writable": True, "branch": value.website.branch, "sha": value.website.verified_sha}))
            stack.enter_context(patch.object(views, "_refresh_article_editorial_payload", return_value=None))
            # The workflow row is persisted before admission; its SQL is outside this regression.
            stack.enter_context(patch("content_factory.website_operations.reserve_workflow_operation"))
            stack.enter_context(patch.object(OrganizationContentConfig.objects, "select_for_update",
                return_value=value.rows(first=value.config)))
            stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "select_for_update",
                return_value=value.rows(first=value.prepare)))
            stack.enter_context(patch.object(Ledger.objects, "select_for_update", return_value=value.rows(first=self.ledger)))
            stack.enter_context(patch.object(Ledger.objects, "filter", return_value=value.rows(exists=False)))
            stack.enter_context(patch("founder_tools.services.founder_actor_id_for_user", return_value="founder:4"))
            stack.enter_context(patch.object(views, "founder_actor_id_for_user", return_value="founder:4"))
            stack.enter_context(patch("content_factory.company_billing.billing_user_for_organization", return_value=value.user))
            stack.enter_context(patch("content_factory.billing.get_content_factory_article_cost_points", return_value=6))
            stack.enter_context(patch.object(views, "get_content_factory_article_cost_points", return_value=6))
            stack.enter_context(patch.object(views, "_roo_points_balance_for_user", return_value=10))
            self.spend = stack.enter_context(patch.object(PointsService, "spend", return_value=(self.ledger, True)))
            self.ordinary_charge = stack.enter_context(patch.object(views, "charge_content_factory_request_for_user",
                return_value=(value.user, self.ledger, 6)))
            self.outbox = stack.enter_context(patch("content_factory.dispatch_outbox.reserve_dispatch"))
            yield

    def charge(self, *, parent=False, supplied=None):
        value = self.value
        payload = {**contract_for(value.website), "delivery_mode": "publish_code", "client_request_id": "ordinary", **(supplied or {})}
        result = views._charge_roo_points_for_article(SimpleNamespace(user=value.user, data={"expectedCostPoints": 6}),
            context=value.context, payload=payload, goal_operation_id=value.prepare.pk if parent else None)
        return result, payload

    def test_requirement_added_after_provider_admission_blocks_direct_charge_and_dispatch(self):
        with self.charge_boundaries():
            actual_admission = views._validate_article_start_admission
            def admitted_then_changed(*args, **kwargs):
                actual_admission(*args, **kwargs)
                sealed.mark_required(self.value.target)
            with patch.object(views, "_validate_article_start_admission", side_effect=admitted_then_changed):
                result, _ = self.charge()
            self.assertIsNotNone(result[3])
            self.assertEqual(result[3].status_code, 409)
            self.ordinary_charge.assert_not_called()
            self.spend.assert_not_called()
            self.outbox.assert_not_called()

    def test_requirement_added_after_admission_preserves_parent_hold_without_dispatch(self):
        with self.charge_boundaries():
            held = billing.reserve(self.value.prepare, user=self.user, now=self.value.now)
            actual_admission = views._validate_article_start_admission
            def admitted_then_changed(*args, **kwargs):
                actual_admission(*args, **kwargs)
                sealed.mark_required(self.value.target)
            with patch.object(views, "_validate_article_start_admission", side_effect=admitted_then_changed):
                result, payload = self.charge(parent=True)
            self.assertIsNotNone(result[3])
            self.assertEqual(result[3].status_code, 409)
            self.assertEqual(payload["client_request_id"], held["client_request_id"])
            self.assertEqual(self.value.prepare.receipt["first_article_billing"], held)
            self.spend.assert_called_once()
            self.ordinary_charge.assert_not_called()
            self.outbox.assert_not_called()

    def test_new_valid_source_cannot_authorize_previously_admitted_payload(self):
        with self.charge_boundaries():
            source = self.value.website.verified_sha
            actual_admission = views._validate_article_start_admission
            def admitted_then_changed(*args, **kwargs):
                actual_admission(*args, **kwargs)
                self.value.website.verified_sha = "f" * 40
                self.value.target.source_sha = "f" * 40
                self.assertTrue(durable_integration_evidence(self.value.config, self.value.website)["verified"])
            with patch.object(views, "_validate_article_start_admission", side_effect=admitted_then_changed):
                result, payload = self.charge()
            self.assertIsNotNone(result[3])
            self.assertEqual(payload["expected_source_sha"], source)
            self.ordinary_charge.assert_not_called()
            self.outbox.assert_not_called()

    def test_new_valid_default_cannot_authorize_another_selected_target(self):
        with self.charge_boundaries():
            selected = self.value.config.default_publish_target_id
            actual_admission = views._validate_article_start_admission
            def admitted_then_changed(*args, **kwargs):
                actual_admission(*args, **kwargs)
                self.value.config.default_publish_target_id = "another-valid-target"
                self.value.target.target_key = "another-valid-target"
                self.assertTrue(durable_integration_evidence(self.value.config, self.value.website)["verified"])
            with patch.object(views, "_validate_article_start_admission", side_effect=admitted_then_changed):
                result, payload = self.charge()
            self.assertIsNotNone(result[3])
            self.assertEqual(payload["connection_target_id"], selected)
            self.ordinary_charge.assert_not_called()
            self.outbox.assert_not_called()

    def test_supplied_foreign_source_or_target_is_rejected_without_retargeting(self):
        for supplied in ({"expected_source_sha": "f" * 40}, {"connection_target_id": "different"},
                {"connectionTargetId": "different"}):
            with self.subTest(supplied=supplied), self.charge_boundaries():
                result, payload = self.charge(supplied=supplied)
                self.assertIsNotNone(result[3])
                self.assertEqual({key: payload[key] for key in supplied}, supplied)
                self.ordinary_charge.assert_not_called()
                self.outbox.assert_not_called()

    def test_original_owner_context_target_cannot_be_silently_replaced(self):
        payload = {"delivery_mode": "publish_code", "client_request_id": "ordinary"}
        binding = {**contract_for(self.value.website), "connection_target_id": "original-other-target"}
        with self.charge_boundaries(), patch.object(views, "owner_operation_contract", return_value=binding):
            error = views._reserve_article_start_connection(self.value.context, payload)
            self.assertIsNotNone(error)
            self.assertEqual(error.status_code, 409)
            self.assertNotIn("connection_target_id", payload)
            self.ordinary_charge.assert_not_called()
            self.outbox.assert_not_called()

    def test_legacy_reserved_identity_cannot_be_upgraded_to_current_target(self):
        from . import website_operations
        from .website_models import WebsiteConnectionOperation
        from .website_contract import evidence_digest
        reserve = website_operations.reserve_workflow_operation
        old_payload = {**contract_for(self.value.website), "delivery_mode": "publish_code", "client_request_id": "ordinary"}
        legacy = SimpleNamespace(generation=self.value.website.generation, state="running", save=Mock(), receipt={},
            payload={"workflow": "article_generation", "attempt": 1,
                "request_digest": evidence_digest({key: value for key, value in old_payload.items() if key != "client_request_id"})})
        original = deepcopy(legacy.payload)
        with self.charge_boundaries(), patch.object(website_operations, "reserve_workflow_operation", new=reserve), \
                patch.object(WebsiteConnectionOperation.objects, "get_or_create", return_value=(legacy, False)):
            result, payload = self.charge()
            self.assertEqual(result[3].status_code, 409)
            self.assertEqual(result[3].data["code"], "operation_key_conflict")
            self.assertEqual(payload["client_request_id"], old_payload["client_request_id"])
            self.assertEqual(legacy.payload, original)
            legacy.save.assert_not_called()
            self.ordinary_charge.assert_not_called()
            self.spend.assert_not_called()
            self.outbox.assert_not_called()

    def test_promoted_exact_proof_reuses_original_hold_and_dispatch_identity(self):
        with self.charge_boundaries(), override_settings(ARTICLES_PUBLISHING_INPUTS_V3_ENABLED=True,
                ARTICLES_HOSTED_ATTESTATION_ENABLED=True, ARTICLES_HOSTED_VERIFICATION_ENABLED=True):
            held = billing.reserve(self.value.prepare, user=self.user, now=self.value.now)
            self.pending(); sealed.mark_required(self.value.target)
            result, _ = self.charge(parent=True)
            self.assertEqual(result[3].status_code, 409)
            self.outbox.assert_not_called()
            self.promote()
            result, payload = self.charge(parent=True)
            self.assertIsNone(result[3])
            self.assertEqual(result[:2], (self.user, self.ledger))
            self.assertEqual(payload["client_request_id"], held["client_request_id"])
            self.assertEqual(self.value.prepare.receipt["first_article_billing"], held)
            self.spend.assert_called_once()
            self.ordinary_charge.assert_not_called()
            self.outbox.assert_called_once()

    def test_legacy_unenrolled_target_still_admits_generation(self):
        with self.charge_boundaries():
            result, _ = self.charge()
            self.assertIsNone(result[3])
            self.ordinary_charge.assert_called_once()
            self.outbox.assert_called_once()

    def test_cancelled_parent_during_promotion_cannot_restore_paid_admission(self):
        with self.charge_boundaries(), override_settings(ARTICLES_PUBLISHING_INPUTS_V3_ENABLED=True,
                ARTICLES_HOSTED_ATTESTATION_ENABLED=True, ARTICLES_HOSTED_VERIFICATION_ENABLED=True):
            held = billing.reserve(self.value.prepare, user=self.user, now=self.value.now)
            self.pending(); sealed.mark_required(self.value.target)
            def changed(*args, **kwargs):
                self.value.prepare.state = "cancelled"
                return {"status": "completed", "run_id": self.binding["run_id"], "verification": self.proof}
            with self.assertRaises(WebsiteAuthorityError): self.promote(remote=changed)
            result, _ = self.charge(parent=True)
            self.assertEqual(result[3].status_code, 409)
            self.assertEqual(self.value.prepare.receipt["first_article_billing"], held)
            self.assertFalse(durable_integration_evidence(self.value.config, self.value.website)["verified"])
            self.spend.assert_called_once()
            self.ordinary_charge.assert_not_called()
            self.outbox.assert_not_called()


@override_settings(ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=True, ARTICLES_SETUP_CONSENT_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True, ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class PendingProofSettlementTests(SimpleTestCase):
    reserve = payment_fixtures.ParentSettlementTests.reserve
    child = payment_fixtures.ParentSettlementTests.child

    def setUp(self):
        payment_fixtures.ParentSettlementTests.setUp(self)
        self.target = SimpleNamespace(target_key="native", adapter="static_html", source_sha="a" * 40,
            verified_at=self.now, contract={}, capabilities={"publishingReady": True}, save=Mock())
        self.connection.verified_sha = "a" * 40
        self.connection.last_verified_at = self.now
        self.connection.capabilities = {"publishingReady": True}
        self.connection.targets = Mock()
        self.connection.targets.filter.return_value.filter.return_value.first.return_value = self.target
        self.config = SimpleNamespace(github_repo=self.connection.github_repo, default_publish_target_id="native")
        sealed.mark_required(self.target)
        self.assertFalse(durable_integration_evidence(self.config, self.connection)["verified"])

    def test_pending_verification_does_not_strand_proven_no_value_refund(self):
        self.child()
        billing.release(self.op, reason="Goal stopped during re-verification")
        billing.release(self.op, reason="Repeated stop")
        self.credit.assert_called_once()
        self.assertEqual(self.credit.call_args.kwargs["original_spend_key"], self.ledger.idempotency_key)
        self.assertEqual(self.op.receipt["first_article_billing"]["status"], "refunded")

    def test_pending_verification_does_not_turn_unknown_effect_into_refund(self):
        self.child("article-goal-pending-publication-v1.json")
        billing.release(self.op, reason="Goal stopped during re-verification")
        self.credit.assert_not_called()
        self.assertEqual(self.op.receipt["first_article_billing"], self.held)
