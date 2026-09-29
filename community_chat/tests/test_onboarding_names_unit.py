"""Required-name regressions without database access or migration execution."""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from rest_framework.exceptions import ValidationError

from community_chat import onboarding
from community_chat.onboarding_views import MemberOnboardingSerializer


@override_settings(
    COMMUNITY_CHAT_SIGNUP_ENABLED=True,
    COMMUNITY_CHAT_MEMBERSHIP_POLICY_VERSION="test-v1",
)
class RequiredOnboardingNamesTests(SimpleTestCase):
    def basics(self):
        return {
            "step": "basics",
            "first_name": "Alex",
            "last_name": "Member",
            "adult_confirmed": True,
            "accept_rules": True,
            "policy_version": "test-v1",
        }

    def profile(self, **changes):
        return SimpleNamespace(
            **{
                "first_name": "Alex",
                "last_name": "Member",
                "status": "incomplete",
                "adult_confirmed_at": True,
                "policy_version": "test-v1",
                "review_reasons": [],
                "city": "",
                "interests": [],
                "marketing_opt_in": None,
                "save": Mock(),
                **changes,
            }
        )

    def service(self, profile, *, existing_access=False):
        stack = ExitStack()
        self.addCleanup(stack.close)
        user = SimpleNamespace(
            first_name="Alex", last_name="Member", email="test@example.com"
        )
        stack.enter_context(patch.object(onboarding.transaction, "atomic"))
        stack.enter_context(
            patch.object(
                onboarding,
                "_locked_session_user",
                return_value=(user, SimpleNamespace(client_id="mlai-chat-web")),
            )
        )
        stack.enter_context(
            patch.object(
                onboarding, "has_community_access", return_value=existing_access
            )
        )
        manager = stack.enter_context(
            patch.object(onboarding.CommunityMemberProfile, "objects")
        )
        manager.select_for_update.return_value.get_or_create.return_value = (
            profile,
            False,
        )
        consent = stack.enter_context(patch.object(onboarding, "_consent"))
        return user, consent

    def test_basics_require_both_names_including_when_omitted(self):
        for name in ("first_name", "last_name"):
            for invalid in (None, "", " \t\n ", "\u2003", "omitted"):
                with self.subTest(name=name, invalid=invalid):
                    values = self.basics()
                    if invalid == "omitted":
                        del values[name]
                    else:
                        values[name] = invalid
                    serializer = MemberOnboardingSerializer(data=values)
                    self.assertFalse(serializer.is_valid())
                    self.assertIn(name, serializer.errors)

    def test_names_are_trimmed_and_unicode_names_remain_valid(self):
        serializer = MemberOnboardingSerializer(
            data={**self.basics(), "first_name": " 明 ", "last_name": " 李 "}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["first_name"], "明")
        self.assertEqual(serializer.validated_data["last_name"], "李")

    def test_optional_step_does_not_require_resending_names(self):
        serializer = MemberOnboardingSerializer(
            data={"step": "complete", "skip_personalisation": True}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_resumed_application_needs_both_names(self):
        user = SimpleNamespace(email="test@example.com")
        for surname, complete in (("", False), ("  ", False), ("Member", True)):
            with self.subTest(surname=surname), patch.object(
                onboarding.CommunityMemberProfile, "objects"
            ) as manager, patch.object(
                onboarding, "has_community_access", return_value=False
            ):
                manager.filter.return_value.first.return_value = self.profile(
                    last_name=surname
                )
                self.assertEqual(
                    onboarding.onboarding_payload(user)["basics_complete"], complete
                )

    def test_service_rejects_missing_surname_before_saving_consents(self):
        profile = self.profile()
        _, consent = self.service(profile)
        for surname in (None, "", "  "):
            with self.subTest(surname=surname):
                values = self.basics()
                if surname is None:
                    del values["last_name"]
                else:
                    values["last_name"] = surname
                with self.assertRaises(ValidationError) as error:
                    onboarding.save_onboarding(
                        authenticated_session=object(), values=values
                    )
                self.assertIn("last_name", error.exception.detail)
        consent.assert_not_called()
        profile.save.assert_not_called()

    def test_old_incomplete_application_cannot_skip_missing_name(self):
        profile = self.profile(last_name=" ")
        self.service(profile)
        with self.assertRaises(ValidationError):
            onboarding.save_onboarding(
                authenticated_session=object(),
                values={"step": "complete", "skip_personalisation": True},
            )
        profile.save.assert_not_called()

    def test_approved_members_can_keep_using_optional_preferences(self):
        profile = self.profile(last_name="", status="approved")
        user, consent = self.service(profile, existing_access=True)
        result = onboarding.save_onboarding(
            authenticated_session=object(),
            values={"step": "complete", "skip_personalisation": True},
        )
        self.assertIs(result, user)
        profile.save.assert_called_once_with()
        consent.assert_not_called()
