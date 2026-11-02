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
        self.assertEqual(first, {"available": True, "completed": ["founders"], "canEdit": True})
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
