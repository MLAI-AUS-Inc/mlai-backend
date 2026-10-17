"""Durable setup resumes original work across provider failures and lost workers."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from . import cms_setup_controller as controller
from . import cms_setup_start as starts
from . import cms_setup_authority as authority
from . import cms_setup_refunds as refunds
from . import cms_publication_policy as policies
from . import cms_setup_quote as quotes
from . import cms_policy_access as access
from . import tests_cms_setup_start_unit as fixtures
from .cms_api_transport import CmsApiError


@override_settings(**fixtures.CmsSetupStartTests._overridden_settings)
class CmsSetupControllerTests(SimpleTestCase):
    configure = fixtures.CmsSetupStartTests.configure
    reserve = fixtures.CmsSetupStartTests.reserve

    def setUp(self):
        fixtures.CmsSetupStartTests.setUp(self)
        self.stack.enter_context(patch("django.utils.timezone.now", side_effect=lambda: self.now))

    def accept(self):
        self.parent = fixtures.CmsSetupStartTests.start(self)
        return self.parent

    def advance(self, seconds=0):
        self.now += timedelta(seconds=seconds)
        return controller.advance_setup(self.parent.pk, now=self.now)

    def test_accepted_setup_runs_without_browser_into_same_held_native_goal_after_two_read_only_gets(self):
        original = self.accept()
        held = deepcopy(original.receipt["first_article_billing"])
        self.profile.metadata = {"selected_company_id": str(uuid4())}
        result = self.advance()
        self.assertIs(result, original)
        self.assertEqual((result.action, result.state), ("cms-goal", "pending"))
        self.assertEqual(result.receipt["first_article_billing"], held)
        self.assertEqual(result.receipt["native_budget"]["setup_usage"]["active_seconds"], 180)
        self.assertEqual(result.receipt["native_budget"]["setup_usage"]["provider_seconds"], 360)
        self.assertEqual(result.receipt["native_budget"]["usage_reports"], {})
        self.assertEqual(self.http.call_count, 2)
        self.assertTrue(all(call.kwargs["method"] == "GET" for call in self.http.call_args_list))
        self.spend.assert_called_once()
        self.assertIsNone(result.payload["source_run_id"])

    def test_previously_accepted_exact_owner_policy_needs_no_configuration_or_extra_get(self):
        self.configure()
        self.quote = quotes.for_context(self.context, self.config, user=self.user, expected_binding=self.binding)
        self.data["setup_quote_digest"] = self.quote["digest"]
        self.accept()
        self.http.reset_mock()
        result = self.advance()
        self.assertEqual(result.action, "cms-goal")
        self.assertEqual(result.receipt["native_budget"]["setup_usage"]["provider_seconds"], 0)
        self.http.assert_not_called(); self.spend.assert_called_once()

    def old_policy(self):
        self.configure()
        saved = policies.current_policy(self.context, self.config, user=self.user)
        row = next(row for row in self.saved if str(row.pk) == saved["operation_id"])
        row.created_at = self.now - timedelta(days=30)
        # An unrelated receipt write cannot pretend the original GET was recent.
        row.updated_at = self.now
        self.quote = quotes.for_context(self.context, self.config, user=self.user, expected_binding=self.binding)
        self.data["setup_quote_digest"] = self.quote["digest"]
        self.accept(); self.http.reset_mock()
        self.access_http = self.stack.enter_context(patch.object(access, "request_json", side_effect=self.http))
        return saved, row

    def test_old_policy_rechecks_real_publish_permissions_without_replacing_owner_defaults_or_price(self):
        saved, row = self.old_policy()
        before = deepcopy((row.payload, self.connection.configuration_version, self.parent.receipt["first_article_billing"]))
        result = self.advance()
        self.assertEqual(result.action, "cms-goal")
        self.assertEqual(result.payload["policy_operation_id"], saved["operation_id"])
        self.assertEqual(result.receipt["first_article_billing"], before[2])
        self.assertEqual((row.payload, self.connection.configuration_version), before[:2])
        at, proof = access.observation(row, saved)
        self.assertEqual(at, self.now)
        self.assertEqual(proof["policy_digest"], saved["policy"]["digest"])
        from .cms_goal_evidence import collect
        proofs, _source, _refs = collect(result.receipt["goal"], self.context, self.config, user=self.user, now=self.now)
        self.assertEqual(proofs["access"]["at"], at.isoformat())
        self.assertEqual(result.receipt["native_budget"]["setup_usage"]["provider_seconds"], 180)
        self.assertEqual(self.access_http.call_args.kwargs, {"method": "GET", "resource": "users", "identifier": "me", "query": {"context": "edit"}})
        self.http.assert_called_once(); self.spend.assert_called_once()

    def test_old_policy_hold_guards_use_short_transactions_while_provider_get_remains_unlocked(self):
        self.old_policy()
        from . import cms_setup_billing as billing
        actual = billing.require_held
        def require_locked(parent):
            self.assertGreater(self.depth, 0)
            return actual(parent)
        with patch("django.db.transaction.get_connection", side_effect=lambda: SimpleNamespace(in_atomic_block=self.depth > 0)), \
                patch.object(billing, "require_held", side_effect=require_locked) as held:
            result = self.advance()
        self.assertEqual((result.action, result.state), ("cms-goal", "pending"))
        self.assertGreater(held.call_count, 2)
        self.assertEqual(self.depth, 0)
        self.access_http.assert_called_once(); self.http.assert_called_once(); self.spend.assert_called_once()

    def test_lost_promotion_after_real_access_commit_reuses_that_observation_and_original_hold(self):
        saved, row = self.old_policy()
        from .website_contract import WebsiteAuthorityError
        with patch.object(starts, "promote", side_effect=WebsiteAuthorityError("website_prepare_dependency_transient", "Synthetic lost reply", retryable=True)):
            self.advance()
        observed = deepcopy(row.receipt["access_observation"])
        result = self.advance(30)
        self.assertEqual(result.action, "cms-goal")
        self.assertEqual(row.receipt["access_observation"], observed)
        self.http.assert_called_once(); self.spend.assert_called_once()

    def test_old_policy_transient_access_failure_retries_without_changing_native_choices(self):
        _saved, row = self.old_policy()
        original = self.http.side_effect
        self.http.side_effect = CmsApiError("transient")
        first = self.advance()
        self.assertEqual((first.state, first.receipt["failure"]["failure_class"]), ("pending", "transient"))
        self.assertNotIn("access_observation", row.receipt)
        self.http.side_effect = original
        self.assertEqual(self.advance(30).action, "cms-goal")
        self.assertEqual(self.http.call_count, 2); self.spend.assert_called_once()

    def test_changed_owner_during_access_get_cannot_commit_an_observation_or_promote(self):
        _saved, row = self.old_policy()
        original = self.http.side_effect
        def revoke(*args, **kwargs):
            result = original(*args, **kwargs)
            self.user.is_active = False
            return result
        self.http.side_effect = revoke
        self.advance()
        self.assertNotIn("access_observation", row.receipt)
        self.assertEqual(self.parent.action, "cms-setup")
        self.http.assert_called_once(); self.spend.assert_called_once()

    def test_access_observation_is_closed_policy_bound_and_not_a_receipt_update_time(self):
        saved, row = self.old_policy()
        self.assertEqual(access.observation(row, saved), (row.created_at, None))

        self.advance()
        original = deepcopy(row.receipt["access_observation"])
        from .channel_artifact_contract import digest
        from .website_contract import WebsiteAuthorityError
        for changes in ({"schema_version": True}, {"policy_digest": "f" * 64}, {"credential_operation_id": str(uuid4())},
                {"extra": True}, {"checked_at": (row.created_at - timedelta(seconds=1)).isoformat()}):
            value = {**deepcopy(original), **changes}
            value["digest"] = digest({key: item for key, item in value.items() if key != "digest"})
            row.receipt["access_observation"] = value
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): access.observation(row, saved)
        row.receipt.pop("access_observation")
        self.assertEqual(access.observation(row, saved), (row.created_at, None))

    def test_readonly_access_check_requires_the_same_actual_wordpress_publishing_identity(self):
        author = {"id": 7, "capabilities": {"edit_posts": True, "publish_posts": True}}
        access._verify_author({"platform": "wordpress"}, {"author": 7}, lambda **_options: author)
        for changes in ({"id": 8}, {"id": True}, {"capabilities": {"edit_posts": True, "publish_posts": 1}},
                {"capabilities": {"edit_posts": False, "publish_posts": True}}, {"capabilities": {}}):
            with self.subTest(changes=changes), self.assertRaises(CmsApiError):
                access._verify_author({"platform": "wordpress"}, {"author": 7}, lambda **_options: {**author, **changes})

    def test_ghost_access_observes_selected_existing_authors_without_selecting_a_replacement(self):
        author = {"id": "a" * 24, "status": "active"}
        template = {"authors": [author["id"]]}
        access._verify_author({"platform": "ghost"}, template,
            lambda **_options: {"users": [{"id": "b" * 24, "status": "active"}, author]})
        for users in ([], [{**author, "status": "inactive"}], [{"id": "b" * 24, "status": "active"}],
                [{**author, "id": True}], [author] * 101):
            with self.subTest(users=users), self.assertRaises(CmsApiError):
                access._verify_author({"platform": "ghost"}, template, lambda **_options: {"users": users})

    def test_valid_changed_or_reduced_wordpress_identity_is_an_owner_access_failure_not_unsupported(self):
        for author in ({"id": 8, "capabilities": {"edit_posts": True, "publish_posts": True}},
                {"id": 7, "capabilities": {"edit_posts": True}}, {"id": 7, "capabilities": {}}):
            with self.subTest(author=author), self.assertRaises(CmsApiError) as error:
                access._verify_author({"platform": "wordpress"}, {"author": 7}, lambda **_options: author)
            self.assertEqual(error.exception.category, "credentials_required")
        for malformed in ({"id": True, "capabilities": {}}, {"id": 7, "capabilities": None}):
            with self.subTest(malformed=malformed), self.assertRaises(CmsApiError) as error:
                access._verify_author({"platform": "wordpress"}, {"author": 7}, lambda **_options: malformed)
            self.assertEqual(error.exception.category, "native_defaults_unavailable")

    def test_transient_occurrences_repair_then_escalate_without_polling_burning_new_attempts(self):
        self.accept()
        self.http.side_effect = CmsApiError("transient")
        first = self.advance()
        self.assertEqual((first.state, first.receipt["effective_failure_class"]), ("pending", "transient"))
        self.assertEqual(self.advance().attempts, 1)
        self.assertEqual(self.http.call_count, 1)
        second = self.advance(30)
        self.assertEqual((second.state, second.receipt["effective_failure_class"]), ("pending", "repairable"))
        third = self.advance(30)
        self.assertEqual((third.state, third.receipt["effective_failure_class"]), ("needs_attention", "strategy"))
        self.assertEqual(third.receipt["native_budget"]["attempts_by_class"]["transient"], 3)
        self.assertEqual(len([row for row in self.saved if row.action == refunds.ACTION]), 1)
        self.assertEqual(self.http.call_count, 3)
        self.spend.assert_called_once()

    def test_real_provider_credential_failure_requires_only_owner_access_and_keeps_original_hold(self):
        original = self.accept().receipt["first_article_billing"]
        self.http.side_effect = CmsApiError("credentials_required")
        result = self.advance()
        self.assertEqual(result.state, "needs_user")
        self.assertEqual(result.receipt["user_action"], {"id": "connect_cms"})
        self.assertEqual(result.receipt["first_article_billing"], original)
        self.assertEqual(result.receipt["failure"]["failure_class"], "needs_user")
        self.assertFalse(any(row.action == refunds.ACTION for row in self.saved))
        self.spend.assert_called_once()

    def test_access_restored_before_next_wake_closes_owner_wait_and_continues_automatically(self):
        self.accept()
        original = self.http.side_effect
        self.http.side_effect = CmsApiError("credentials_required")
        self.advance()
        self.http.side_effect = original
        result = self.advance(300)
        self.assertEqual(result.action, "cms-goal")
        self.assertEqual(result.receipt["native_budget"]["owner_wait_completed_seconds"], 300)
        self.assertNotIn("owner_wait_started_at", result.receipt["native_budget"])
        self.spend.assert_called_once()

    def test_worker_loss_after_actual_policy_commit_replays_original_policy_without_more_gets_or_spend(self):
        original = self.accept()
        accepted_at = authority.historical(original)["accepted_at"]
        with patch.object(starts, "promote", side_effect=RuntimeError("Synthetic process interruption")), self.assertRaises(RuntimeError):
            self.advance()
        self.assertEqual(original.state, "running")
        self.assertIn(policies.REFERENCE, self.connection.capabilities)
        self.http.reset_mock()
        result = self.advance(181)
        self.assertEqual(result.action, "cms-goal")
        self.assertEqual(result.receipt["goal"]["started_at"], accepted_at)
        self.assertEqual(result.receipt["native_budget"]["setup_usage"]["active_seconds"], 360)
        self.assertEqual(result.receipt["native_budget"]["attempts_by_class"]["transient"], 1)
        self.http.assert_not_called(); self.spend.assert_called_once()

    def test_repeated_actual_worker_losses_escalate_once_per_dead_lease(self):
        self.accept()
        with patch.object(policies, "configure", side_effect=RuntimeError("Synthetic lost process")):
            for index in range(3):
                with self.assertRaises(RuntimeError): self.advance(181 if index else 0)
            result = self.advance(181)
        self.assertEqual(result.state, "needs_attention")
        self.assertEqual(result.receipt["effective_failure_class"], "strategy")
        self.assertEqual(result.receipt["native_budget"]["attempts_by_class"]["transient"], 3)
        self.assertEqual(len([row for row in self.saved if row.action == refunds.ACTION]), 1)
        self.http.assert_not_called(); self.spend.assert_called_once()

    def test_cancellation_during_first_get_fences_policy_and_preserves_queued_original_credit(self):
        self.accept()
        original = self.http.side_effect
        def cancel(*args, **kwargs):
            result = original(*args, **kwargs)
            refunds.cancel_held_setups(self.connection)
            return result
        self.http.side_effect = cancel
        self.assertIsNone(self.advance())
        self.assertEqual(self.parent.state, "cancelled")
        self.assertEqual(self.http.call_count, 1)
        self.assertNotIn(policies.REFERENCE, self.connection.capabilities)
        self.assertTrue(any(row.action == refunds.ACTION for row in self.saved))
        self.spend.assert_called_once()

    def test_revoked_connection_before_claim_cancels_unused_setup_without_any_provider_request(self):
        self.accept()
        self.connection.state = "disconnected"
        result = self.advance()
        self.assertEqual(result.state, "cancelled")
        self.assertTrue(any(row.action == refunds.ACTION for row in self.saved))
        self.http.assert_not_called()

    def test_budget_exhaustion_stops_before_provider_work_and_does_not_invent_an_execution_allowance(self):
        self.accept()
        with override_settings(ARTICLES_GOAL_ACTIVE_SECONDS=100):
            result = self.advance()
        self.assertEqual((result.state, result.receipt["budget_stop_reason"]), ("stopped", "active_compute"))
        self.assertNotIn("setup_usage", result.receipt["native_budget"])
        self.assertTrue(any(row.action == refunds.ACTION for row in self.saved))
        self.http.assert_not_called()

    def test_original_wall_time_is_not_reset_after_page_close_or_a_delayed_scheduler(self):
        self.accept()
        result = self.advance(172801)
        self.assertEqual((result.state, result.receipt["budget_stop_reason"]), ("stopped", "wall_time"))
        self.http.assert_not_called(); self.spend.assert_called_once()

    def test_malformed_private_budget_stops_deterministic_relaunches_and_queues_unused_money(self):
        self.accept()
        self.parent.receipt["native_budget"] = None
        result = self.advance()
        self.assertEqual((result.state, result.receipt["status"]), ("needs_attention", "invalid_setup_budget"))
        self.assertIsNone(result.next_attempt_at)
        self.assertTrue(any(row.action == refunds.ACTION for row in self.saved))
        self.http.assert_not_called()

    def test_invalid_private_intent_keeps_financial_proof_and_stops_without_inventing_a_credit(self):
        self.accept()
        self.parent.payload["intent_digest"] = "0" * 64
        result = self.advance()
        self.assertEqual((result.state, result.receipt["status"]), ("needs_attention", "invalid_setup_intent"))
        self.assertIsNone(result.next_attempt_at)
        self.assertIn("first_article_billing", result.receipt)
        self.assertFalse(any(row.action == refunds.ACTION for row in self.saved))
        self.http.assert_not_called()

    def test_rollout_off_preserves_original_accepted_operation_without_claim_provider_or_money_action(self):
        original = self.accept()
        before = deepcopy((original.payload, original.receipt, original.attempts))
        with override_settings(ARTICLES_CMS_SETUP_ENABLED=False): self.assertIsNone(self.advance())
        self.assertEqual((original.payload, original.receipt, original.attempts), before)
        self.http.assert_not_called(); self.spend.assert_called_once()
