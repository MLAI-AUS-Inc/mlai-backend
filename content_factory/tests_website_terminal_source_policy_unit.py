"""Automatic source hints cannot create a fresh paid goal or erase a terminal stop."""
from contextlib import nullcontext
from copy import deepcopy
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from . import website_goal_runner as runner, website_wakeups as wakes
from . import tests_website_article_source_fence_unit as fixtures


@override_settings(ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=True, ARTICLES_SETUP_CONSENT_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True, ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED=True, ARTICLES_ARTICLE_RECOVERY_ENABLED=True,
    ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=True, ARTICLES_WEBSITE_EVENT_WAKES_ENABLED=True)
class TerminalSourcePolicyTests(SimpleTestCase):
    def setUp(self):
        self.fixture = fixtures.ArticleSourceFenceTests("runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.parent, self.config, self.connection = self.fixture.parent, self.fixture.config, self.fixture.connection

    def test_source_push_never_creates_a_successor_for_any_terminal_goal(self):
        for state in runner.TERMINAL:
            with self.subTest(state=state):
                self.parent.state = state
                before = deepcopy(self.parent.receipt), deepcopy(self.parent.payload)
                with patch("content_factory.website_prepare.start_prepare") as start, \
                        patch("content_factory.website_prepare._cancel_prepare_child") as cancel:
                    result = runner.wake_goal_for_source(self.config, company_id=9,
                        user=self.fixture.fixture.chain.fixture.user, now=self.fixture.now)
                self.assertIsNone(result)
                start.assert_not_called(); cancel.assert_not_called()
                self.assertEqual((self.parent.receipt, self.parent.payload), before)
                self.assertEqual(self.parent.state, state)
        self.assertEqual(self.fixture.fixture.chain.fixture.spend.call_count, 1)

    def test_automatic_hints_cannot_ignore_stop_markers_in_an_active_state(self):
        for state in ("pending", "running", "needs_user"):
            for marker in ("budget_stop_reason", "strategy_observation_invalid"):
                with self.subTest(state=state, marker=marker):
                    self.parent.state = state
                    self.parent.receipt[marker] = "recorded-stop"
                    self.assertFalse(wakes.policy_allows(self.parent, {"success": True}))
                    with patch("content_factory.website_prepare.start_prepare") as start, \
                            patch("content_factory.website_prepare._cancel_prepare_child") as cancel:
                        self.assertIsNone(runner.wake_goal_for_source(self.config, company_id=9,
                            user=self.fixture.fixture.chain.fixture.user, now=self.fixture.now))
                    start.assert_not_called(); cancel.assert_not_called()
                    self.parent.receipt.pop(marker)

    def test_budget_stop_policy_is_checked_before_source_rebind_or_child_cancel(self):
        from .website_models import WebsiteConnection
        self.parent.state = "needs_attention"
        self.parent.receipt["budget_stop_reason"] = "model_cost"
        before = deepcopy(self.parent.receipt), deepcopy(self.parent.payload)
        with patch.object(WebsiteConnection.objects, "select_related") as snapshots, \
                patch.object(WebsiteConnection.objects, "select_for_update") as locked, \
                patch("organizations.models.Organization.objects.select_for_update"), \
                patch.object(wakes.transaction, "atomic", return_value=nullcontext()), \
                patch.object(wakes.transaction, "on_commit") as schedule, \
                patch("content_factory.website_goal_scope.rebind_source_scope") as rebind, \
                patch("content_factory.website_prepare._cancel_prepare_child") as cancel:
            snapshots.return_value.filter.return_value.first.return_value = self.connection
            locked.return_value.get.return_value = self.connection
            self.connection.operations.filter.side_effect = lambda *a, **k: fixtures.rows(self.parent)
            self.assertIsNone(wakes.queue_wake(self.connection.pk, signal={"success": True, "milestone": "previewed"}))
        rebind.assert_not_called(); cancel.assert_not_called(); schedule.assert_not_called()
        self.assertEqual((self.parent.receipt, self.parent.payload), before)

    def test_completed_first_article_keeps_original_paid_root_and_proofs(self):
        self.parent.state = "completed"
        self.parent.receipt["first_article_live_receipt"] = {"article_run_id": self.fixture.root.run_id, "source_sha": "a" * 40}
        before = deepcopy(self.parent.receipt)
        with patch("content_factory.website_prepare.start_prepare") as start:
            self.assertIsNone(runner.wake_goal_for_source(self.config, company_id=9,
                user=self.fixture.fixture.chain.fixture.user, now=self.fixture.now))
        start.assert_not_called()
        self.assertEqual(self.parent.receipt, before)
        self.assertEqual(self.fixture.fixture.chain.fixture.spend.call_count, 1)

    def test_actual_source_caller_never_falls_back_to_legacy_after_rollout_off(self):
        from .website_prepare import wake_prepare_for_source
        from .website_models import WebsiteConnection
        from .models import OrganizationContentConfig
        from founder_tools.models import VibeRaisingCompany
        self.connection.authorized_by = self.fixture.fixture.chain.fixture.user
        self.connection.authorized_by_id = self.connection.authorized_by.pk
        self.parent.state = "completed"
        before = deepcopy(self.parent.receipt), deepcopy(self.parent.payload)
        with self.settings(ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=False), \
                patch.object(WebsiteConnection.objects, "select_related") as connections, \
                patch.object(OrganizationContentConfig.objects, "filter") as configs, \
                patch.object(VibeRaisingCompany.objects, "filter") as companies, \
                patch("content_factory.website_prepare.start_prepare", side_effect=AssertionError("No legacy fallback")) as start:
            connections.return_value.filter.return_value.first.return_value = self.connection
            configs.return_value.first.return_value = self.config
            companies.return_value.first.return_value = self.fixture.fixture.chain.context.company
            self.assertIsNone(wake_prepare_for_source(self.connection.pk, organization_id=7))
        start.assert_not_called()
        self.assertEqual((self.parent.receipt, self.parent.payload), before)
