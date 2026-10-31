"""P1.3 one-screen join: flag off is today's two-step flow; flag on admits in one step."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from community_chat.models import (
    AiConsentRecord, CommunityChatDevice, CommunityMemberConsent, CommunityMemberProfile, CommunityMemberReviewRule,
)
from community_chat.onboarding import has_community_access
from community_chat.tests.test_account_profiles import credentials_for
from content_factory.founder_journey_testing import ALL_P1_FLAGS_ON, make_named_legacy_org
from founder_tools.models import VibeRaisingProfile

ORIGIN = "https://chat.mlai.au"
BASE_SETTINGS = dict(
    COMMUNITY_CHAT_SIGNUP_ENABLED=True,
    COMMUNITY_CHAT_MEMBERSHIP_POLICY_VERSION="test-v1",
    COMMUNITY_CHAT_ALLOWED_ORIGINS=[ORIGIN, "mlaichat://callback"],
)
ONE_STEP_ON = {**BASE_SETTINGS, "COMMUNITY_CHAT_ONE_STEP_JOIN_ENABLED": True}


class _JoinTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(email="joiner@example.com", email_verified_at=timezone.now())
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {credentials_for(self.user).access_token}")
        self.url = reverse("community_chat_onboarding")

    def join(self, **changes):
        body = {
            "step": "join", "first_name": "Priya", "last_name": "Shah", "adult_and_terms": True,
            "policy_version": "test-v1", "intent": "startup", **changes,
        }
        return self.client.put(self.url, {key: value for key, value in body.items() if value is not None},
                               format="json")

    def basics(self, **changes):
        return self.client.put(self.url, {
            "step": "basics", "first_name": "Alex", "last_name": "Member",
            "adult_confirmed": True, "accept_rules": True, "policy_version": "test-v1", **changes,
        }, format="json")

    def complete(self, **changes):
        return self.client.put(self.url, {"step": "complete", "skip_personalisation": True, **changes}, format="json")


@override_settings(**BASE_SETTINGS)
class OneStepJoinFlagOffTests(_JoinTestCase):
    def test_payload_says_two_step_without_intent_options(self):
        onboarding = self.client.get(self.url).data["onboarding"]
        self.assertEqual(onboarding["join_flow"], "two_step")
        self.assertNotIn("intent_options", onboarding)
        account = self.client.get(reverse("community_chat_account")).data["onboarding"]
        self.assertEqual(account["join_flow"], "two_step")

    def test_join_step_and_join_fields_are_refused_as_today(self):
        response = self.join()
        self.assertEqual(response.status_code, 400)
        self.assertIn("step", response.data)
        self.assertEqual(self.basics(intent="startup").status_code, 400)
        self.assertFalse(CommunityMemberProfile.objects.exists())

    def test_one_word_names_stay_incomplete_and_stale_policy_keeps_old_copy(self):
        self.assertEqual(self.basics(last_name="").status_code, 400)
        stale = self.basics(policy_version="old")
        self.assertEqual(stale.status_code, 400)
        self.assertEqual(stale.data, {"detail": "The community rules changed. Reload and review them again."})
        CommunityMemberProfile.objects.create(
            user=self.user, first_name="Priya", last_name="", adult_confirmed_at=timezone.now(),
            policy_version="test-v1",
        )
        self.assertFalse(self.client.get(self.url).data["onboarding"]["basics_complete"])
        self.assertEqual(self.complete().status_code, 400)

    def test_basics_then_complete_still_admits(self):
        self.assertEqual(self.basics().status_code, 200)
        self.assertEqual(self.complete().data["onboarding"]["status"], "approved")


@override_settings(**ONE_STEP_ON)
class OneStepJoinTests(_JoinTestCase):
    def test_payload_advertises_one_step_and_intents_everywhere(self):
        onboarding = self.client.get(self.url).data["onboarding"]
        self.assertEqual(onboarding["join_flow"], "one_step")
        self.assertEqual(onboarding["intent_options"], ["startup", "learn", "explore"])
        account = self.client.get(reverse("community_chat_account")).data["onboarding"]
        self.assertEqual(account["join_flow"], "one_step")
        self.assertEqual(account["intent_options"], ["startup", "learn", "explore"])

    def test_startup_join_admits_at_once_with_founder_role_and_interest(self):
        response = self.join()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["next"], "my_startup")
        self.assertEqual(response.data["onboarding"]["status"], "approved")
        self.assertFalse(response.data["onboarding"]["required"])
        self.assertTrue(has_community_access(self.user))
        profile = CommunityMemberProfile.objects.get(user=self.user)
        self.assertEqual(profile.interests, ["startups"])
        self.assertIsNotNone(profile.adult_confirmed_at)
        self.assertEqual(
            sorted(CommunityMemberConsent.objects.filter(user=self.user).values_list("purpose", flat=True)),
            ["adult_eligibility", "code_of_conduct", "terms"],
        )
        self.assertEqual(VibeRaisingProfile.objects.get(user=self.user).role, VibeRaisingProfile.ROLE_FOUNDER)
        self.user.refresh_from_db()
        self.assertEqual(self.user.full_name, "Priya Shah")

    def test_learn_and_explore_go_home_without_founder_profile(self):
        for intent in ("learn", "explore"):
            with self.subTest(intent=intent):
                response = self.join(intent=intent)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["next"], "home")
        self.assertFalse(VibeRaisingProfile.objects.filter(user=self.user).exists())
        self.assertEqual(CommunityMemberProfile.objects.get(user=self.user).interests, [])

    def test_one_word_name_is_complete_everywhere(self):
        response = self.join(last_name="")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["onboarding"]["status"], "approved")
        self.assertTrue(response.data["onboarding"]["basics_complete"])
        self.assertEqual(self.complete().status_code, 200)

    def test_two_step_basics_still_needs_a_surname(self):
        for changes in ({"last_name": ""}, {"last_name": "   "}):
            with self.subTest(changes=changes):
                response = self.basics(**changes)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["last_name"], ["Enter your surname."])

    def test_flagged_name_is_pending_but_still_goes_to_my_startup(self):
        CommunityMemberReviewRule.objects.create(phrase="official", match="word", reason="Check affiliation")
        response = self.join(first_name="Official", last_name="Person")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["onboarding"]["status"], "pending_review")
        self.assertEqual(response.data["next"], "my_startup")
        self.assertFalse(has_community_access(self.user))

    def test_stale_policy_is_refused_with_current_version_for_join_and_basics(self):
        for response in (self.join(policy_version="old"), self.basics(policy_version="old")):
            with self.subTest(step=response.wsgi_request.path):
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data, {
                    "detail": "Our terms were just updated. Please tick to agree again.",
                    "code": "policy_version_changed",
                    "policy_version": "test-v1",
                })
        self.assertFalse(CommunityMemberConsent.objects.exists())

    def test_join_requires_the_checkbox_a_name_and_an_intent(self):
        for changes in ({"adult_and_terms": False}, {"adult_and_terms": "true"}, {"first_name": ""},
                        {"first_name": None}, {"intent": None}, {"intent": "investing"},
                        {"policy_version": None}, {"status": "approved"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.join(**changes).status_code, 400)
        self.assertFalse(CommunityMemberConsent.objects.exists())

    def test_join_writes_no_ai_sharing_consent(self):
        with patch("community_chat.privacy.set_ai_consent", side_effect=AssertionError) as ai_write:
            self.assertEqual(self.join().status_code, 200)
        ai_write.assert_not_called()
        self.assertFalse(AiConsentRecord.objects.filter(user=self.user).exists())

    def test_basics_then_complete_still_work_for_old_clients(self):
        self.assertEqual(self.basics().status_code, 200)
        response = self.complete()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["onboarding"]["status"], "approved")
        self.assertNotIn("next", response.data)

    def test_existing_founder_keeps_access_and_never_sees_the_join_form(self):
        legacy = make_named_legacy_org("talathrive")
        CommunityChatDevice.objects.create(user=legacy.owner, public_key="c" * 64, status="verified",
                                           verified_at=timezone.now())
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {credentials_for(legacy.owner).access_token}")
        with override_settings(**ALL_P1_FLAGS_ON):
            onboarding = client.get(self.url).data["onboarding"]
        self.assertFalse(onboarding["required"])
        self.assertEqual(onboarding["status"], "approved")


@override_settings(**{**BASE_SETTINGS, **ALL_P1_FLAGS_ON})
class OneStepJoinAllFlagsTests(_JoinTestCase):
    def test_approved_member_with_complete_basics_is_never_asked_again(self):
        self.basics()
        self.complete()
        onboarding = self.client.get(self.url).data["onboarding"]
        self.assertFalse(onboarding["required"])
        self.assertTrue(onboarding["basics_complete"])
