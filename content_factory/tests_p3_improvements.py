"""Optional improvement eligibility and monotonic progress without a drafting gate."""
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings

from . import founder_improvements as improvements
from .editorial_catalog import public_strategy
from .models import OrganizationContentConfig
from .self_heal_testing import ALL_FLAGS_ON, make_p2_org, make_named_legacy_org


@override_settings(**{**ALL_FLAGS_ON, "MY_STARTUP_ARTICLE_IMPROVEMENTS_ENABLED": True})
class ImprovementTests(TransactionTestCase):
    def setUp(self):
        self.fixture = make_p2_org()
        self.org, self.owner = self.fixture.organization, self.fixture.owner

    def test_absent_before_actual_first_draft_even_with_completed_profile_tools(self):
        config = self.fixture.config
        config.authors = [{"id": "pat", "name": "Pat", "role": "Founder"}]
        config.save(update_fields=["authors"])
        with patch.object(improvements, "has_first_draft", return_value=False):
            result = improvements.summary(self.org, user=self.owner)
        self.assertFalse(result["available"])
        self.assertEqual(result["completed"], [])
        config.refresh_from_db()
        self.assertNotIn(improvements.KEY, config.pillar_strategy.get("self_heal", {}))

    def test_delivery_and_completion_stay_earned_after_draft_and_profile_removal_without_clock_mutation(self):
        config = self.fixture.config
        config.authors = [{"id": "pat", "name": "Pat", "role": "Founder"}]
        config.save(update_fields=["authors"])
        before = config.updated_at
        with patch.object(improvements, "has_first_draft", return_value=True):
            first = improvements.summary(self.org, user=self.owner)
        self.assertEqual(first, {"available": True, "completed": ["founders"], "canEdit": True, "websiteCheckFree": False})
        OrganizationContentConfig.objects.filter(pk=config.pk).update(authors=[])
        with patch.object(improvements, "has_first_draft", return_value=False):
            again = improvements.summary(self.org, user=self.owner)
        self.assertEqual(again, first)
        config.refresh_from_db()
        self.assertEqual(config.updated_at, before)
        self.assertNotIn("self_heal", public_strategy(config.pillar_strategy))

    def test_flagoff_and_legacy_do_not_write_or_add_a_payload(self):
        with override_settings(MY_STARTUP_ARTICLE_IMPROVEMENTS_ENABLED=False), self.assertNumQueries(0):
            self.assertIsNone(improvements.summary(self.org, user=self.owner))
        legacy = make_named_legacy_org("la_vie")
        self.assertIsNone(improvements.summary(legacy.organization, user=legacy.owner))

    def test_member_can_read_progress_but_receives_no_edit_permission(self):
        from core.models import User
        member = User.objects.create_user(email="member@improvements.synthetic.test")
        with patch.object(improvements, "has_first_draft", return_value=True):
            result = improvements.summary(self.org, user=member)
        self.assertTrue(result["available"])
        self.assertFalse(result["canEdit"])

    def test_free_copy_requires_explicit_research_allowance_even_for_funded_owner(self):
        with override_settings(ARTICLES_FREE_SETUP_RESEARCH_ENABLED=False):
            self.assertFalse(improvements.summary(self.org, user=self.owner)["websiteCheckFree"])
        with override_settings(ARTICLES_FREE_SETUP_RESEARCH_ENABLED=True):
            self.assertTrue(improvements.summary(self.org, user=self.owner)["websiteCheckFree"])

    def test_real_first_draft_receipt_unlocks_progress_and_survives_delivery_removal(self):
        config = self.fixture.config
        strategy = dict(config.pillar_strategy)
        strategy["founder_funnel"] = {"first_draft": {"version": 1, "runId": "delivered-draft", "at": "2026-11-02T10:00:00Z"}}
        OrganizationContentConfig.objects.filter(pk=config.pk).update(pillar_strategy=strategy)
        self.assertTrue(improvements.summary(self.org, user=self.owner)["available"])
        config.refresh_from_db()
        strategy = dict(config.pillar_strategy)
        strategy.pop("founder_funnel")
        OrganizationContentConfig.objects.filter(pk=config.pk).update(pillar_strategy=strategy)
        self.assertTrue(improvements.summary(self.org, user=self.owner)["available"])
