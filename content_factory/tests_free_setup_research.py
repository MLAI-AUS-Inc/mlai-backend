"""P0.2a: free startup research for new founders, with per-account daily caps.

29 Oct 2026 funnel: 45 of 80 new users had 0 points, and every AI action needed
6 even when it cost nothing, so startup autofill and the website baseline were
out of reach before a founder had seen any value.
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.exceptions import Throttled
from rest_framework.test import APIRequestFactory, force_authenticate

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization

from . import vibe_marketing_views as views
from .billing import (
    FREE_SETUP_ACTIONS,
    build_roo_points_payload,
    enforce_free_setup_daily_cap,
    free_setup_actions,
    normalized_account_key,
)

FREE_ON = {"ARTICLES_FREE_SETUP_RESEARCH_ENABLED": True,
    "ARTICLES_FREE_SETUP_AUTOFILL_DAILY_CAP": 5, "ARTICLES_FREE_SETUP_BASELINE_DAILY_CAP": 2}


class FreeSetupActionsTests(SimpleTestCase):
    def test_flag_adds_only_autofill_and_baseline(self):
        with override_settings(ARTICLES_FREE_SETUP_RESEARCH_ENABLED=False):
            self.assertEqual(free_setup_actions(), FREE_SETUP_ACTIONS)
        with override_settings(ARTICLES_FREE_SETUP_RESEARCH_ENABLED=True):
            self.assertEqual(free_setup_actions() - FREE_SETUP_ACTIONS, {"startup_autofill", "website_baseline"})


@override_settings(**FREE_ON)
class FreeSetupGateTests(SimpleTestCase):
    """The gate at balance 0: only the named free actions pass without points."""

    def setUp(self):
        cache.clear()
        self.user = SimpleNamespace(pk=7, email="Founder@Example.test")
        self.balance = self.enterContext(patch.object(views, "_roo_points_balance_for_user", return_value=0))

    def test_autofill_and_baseline_pass_at_zero_balance_and_send_free_authorization(self):
        for action in ("startup_autofill", "website_baseline"):
            with self.subTest(action=action):
                self.assertEqual(views._require_roo_points_for_ai_agent(self.user, domain="new.test", action=action), (None, None))
                payload = {}
                views._mark_roo_points_gate_authorized(payload, domain="new.test", action=action, current_balance=None)
                self.assertEqual((payload["roo_points_billing_status"], payload["roo_points_required"],
                    payload["roo_points_cost"], payload["roo_points_authorized"]), ("free", 0, 0, True))
                self.assertNotIn("roo_points_ledger_id", payload)
        self.balance.assert_not_called()

    def test_other_ai_actions_keep_the_six_point_floor(self):
        # Every gated action quotes 0 today; a cost-0 bypass would have removed
        # the floor everywhere. Only the named actions are free.
        for action in ("article_system_revision", "article_revision", "topic_discovery"):
            with self.subTest(action=action):
                response, balance = views._require_roo_points_for_ai_agent(self.user, domain="new.test", action=action)
                self.assertEqual((response.status_code, balance), (402, 0))
                self.assertEqual(response.data["required_points"], 6)
                self.assertEqual(response.data["message"], "This needs 6 points. You have 0.")

    @override_settings(ARTICLES_FREE_SETUP_RESEARCH_ENABLED=False)
    def test_flag_off_keeps_todays_balance_floor(self):
        for action in ("startup_autofill", "website_baseline"):
            with self.subTest(action=action):
                response, _ = views._require_roo_points_for_ai_agent(self.user, domain="new.test", action=action)
                self.assertEqual(response.status_code, 402)
                payload = {}
                views._mark_roo_points_gate_authorized(payload, domain="new.test", action=action, current_balance=6)
                self.assertEqual((payload["roo_points_billing_status"], payload["roo_points_required"]), ("gated", 6))


@override_settings(**FREE_ON)
class FreeSetupDailyCapTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_autofill_allows_five_per_account_email_per_rolling_day(self):
        founder = SimpleNamespace(pk=1, email="founder@example.test")
        same_email = SimpleNamespace(pk=2, email=" FOUNDER@example.test ")
        for offset in range(5):
            enforce_free_setup_daily_cap(founder if offset % 2 else same_email, "startup_autofill", now=1_000 + offset)
        with self.assertRaises(Throttled) as limited:
            enforce_free_setup_daily_cap(founder, "startup_autofill", now=2_000)
        # The oldest use (t=1000) frees its slot 24 hours later.
        self.assertEqual(limited.exception.wait, 1_000 + 86_400 - 2_000 + 1)
        self.assertIn("Expected available in", str(limited.exception.detail))
        # Another account and another action have their own counters.
        enforce_free_setup_daily_cap(SimpleNamespace(pk=3, email="other@example.test"), "startup_autofill", now=2_000)
        enforce_free_setup_daily_cap(founder, "website_baseline", now=2_000)
        # The window rolls rather than resetting at midnight.
        enforce_free_setup_daily_cap(founder, "startup_autofill", now=1_000 + 86_400)

    def test_baseline_allows_two_and_zero_disables_the_cap(self):
        founder = SimpleNamespace(pk=1, email="")
        self.assertEqual(normalized_account_key(founder), "user:1")
        enforce_free_setup_daily_cap(founder, "website_baseline", now=10)
        enforce_free_setup_daily_cap(founder, "website_baseline", now=11)
        with self.assertRaises(Throttled):
            enforce_free_setup_daily_cap(founder, "website_baseline", now=12)
        with override_settings(ARTICLES_FREE_SETUP_BASELINE_DAILY_CAP=0):
            for second in range(10):
                enforce_free_setup_daily_cap(founder, "website_baseline", now=20 + second)

    def test_gate_applies_the_cap_only_to_free_research(self):
        user = SimpleNamespace(pk=9, email="cap@example.test")
        with patch.object(views, "_roo_points_balance_for_user", return_value=0):
            for _ in range(2):
                views._require_roo_points_for_ai_agent(user, domain="new.test", action="website_baseline")
            with self.assertRaises(Throttled):
                views._require_roo_points_for_ai_agent(user, domain="new.test", action="website_baseline")
            # Setup work that was already free is never capped.
            for _ in range(10):
                self.assertEqual(views._require_roo_points_for_ai_agent(user, domain="new.test", action="repo_scan"), (None, None))


class InsufficientPointsCopyTests(SimpleTestCase):
    """The 402 sentence names the numbers only, never a masked email."""

    def test_three_action_specific_sentences_without_account_identity(self):
        cases = (
            ({"action": "article_generation", "cost_points": 12, "required_points": 12}, "This article uses 12 points. You have 4."),
            ({"action": "content_island_topic_generation", "cost_points": 4, "required_points": 4}, "Finding topics uses 4 points. You have 4."),
            ({"action": "content_island_topic_generation", "cost_points": 1, "required_points": 1}, "Finding topics uses 1 point. You have 4."),
            ({"action": "article_system_revision", "cost_points": 0, "required_points": 6}, "This needs 6 points. You have 4."),
        )
        for fields, sentence in cases:
            with self.subTest(sentence=sentence):
                payload = build_roo_points_payload(domain="customer.test", current_balance=4,
                    account_email="sam@example.test", billing_email="pat@example.test", **fields)
                self.assertEqual((payload["error"], payload["detail"], payload["message"]), (sentence,) * 3)
                text = " ".join((payload["error"], payload["detail"], payload["message"]))
                self.assertNotIn("@", text)
                self.assertNotIn("Roo", text)
                # Keys are unchanged for older clients; the identities stay masked.
                self.assertEqual((payload["signed_in_account"], payload["billing_account"]), ("s***@example.test", "p***@example.test"))
                self.assertEqual(payload["error_code"], "INSUFFICIENT_ROO_POINTS")


@override_settings(**FREE_ON)
class FreeBaselineViewTests(TestCase):
    """A zero-balance founder starts the website baseline, until the daily cap."""

    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(email="baseline@synthetic.example.test")
        profile = VibeRaisingProfile.objects.create(user=self.user, role="founder")
        self.org = Organization.objects.create(domain="baseline.example.test", name="Baseline")
        self.company = VibeRaisingCompany.objects.create(profile=profile, organization=self.org,
            name="Baseline", domain=self.org.domain)

    def post(self):
        request = APIRequestFactory().post(f"/api/v1/vibe-marketing/baseline/?company_id={self.company.pk}", {}, format="json")
        force_authenticate(request, user=self.user)
        return views.VibeMarketingBaselineView.as_view()(request)

    def test_zero_balance_baseline_queues_with_free_authorization_then_hits_the_cap(self):
        queued = []

        def queue(**kwargs):
            queued.append(kwargs["payload"])
            return SimpleNamespace(run_id=f"baseline-{len(queued)}", status="queued")

        with patch.object(views, "_queue_content_factory_run", side_effect=queue), \
                patch.object(views, "_run_start_payload", side_effect=lambda run: {"run_id": run.run_id}):
            for _ in range(2):
                self.assertEqual(self.post().status_code, 202)
            limited = self.post()
        self.assertEqual(len(queued), 2)
        self.assertEqual((queued[0]["roo_points_billing_status"], queued[0]["roo_points_required"]), ("free", 0))
        self.assertEqual(limited.status_code, 429)
        self.assertGreater(int(limited["Retry-After"]), 0)

    @override_settings(ARTICLES_FREE_SETUP_RESEARCH_ENABLED=False)
    def test_flag_off_zero_balance_baseline_is_refused(self):
        with patch.object(views, "_queue_content_factory_run") as queue:
            response = self.post()
        self.assertEqual(response.status_code, 402)
        queue.assert_not_called()
