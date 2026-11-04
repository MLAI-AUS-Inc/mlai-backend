"""P1.3: approving a pending member sends one "You're in" email (G9: nobody else)."""

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from community_chat.models import CommunityMemberProfile
from community_chat.onboarding_admin import CommunityMemberProfileAdmin
from content_factory.founder_journey_testing import ALL_P1_FLAGS_ON

POLICY = "test-v1"
BASE = dict(COMMUNITY_CHAT_MEMBERSHIP_POLICY_VERSION=POLICY, RESEND_API_KEY="re_test_key")
ON = {**BASE, "COMMUNITY_CHAT_REVIEW_CLEARED_EMAIL_ENABLED": True, "COMMUNITY_CHAT_ONE_STEP_JOIN_ENABLED": True}


@override_settings(**BASE)
class ReviewClearedEmailTests(TestCase):
    def setUp(self):
        cache.clear()
        self.reviewer = get_user_model().objects.create_user(email="committee@example.com", is_staff=True)
        self.admin = CommunityMemberProfileAdmin(CommunityMemberProfile, admin.site)
        self.request = RequestFactory().post("/admin/")
        self.request.user = self.reviewer
        patcher = patch.object(CommunityMemberProfileAdmin, "message_user")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("community_chat.onboarding_admin.http_client.post",
                        return_value=SimpleNamespace(status_code=200))
        self.post = patcher.start()
        self.addCleanup(patcher.stop)

    def member(self, *, status=CommunityMemberProfile.Status.PENDING, last_name="Shah", email="priya@example.com"):
        user = get_user_model().objects.create_user(email=email, email_verified_at=timezone.now())
        return CommunityMemberProfile.objects.create(
            user=user, status=status, first_name="Priya", last_name=last_name,
            adult_confirmed_at=timezone.now(), policy_version=POLICY, review_reasons=["reserved_name"],
        )

    def approve(self, profile):
        with self.captureOnCommitCallbacks(execute=True):
            self.admin._review(self.request, CommunityMemberProfile.objects.filter(pk=profile.pk),
                               CommunityMemberProfile.Status.APPROVED)
        profile.refresh_from_db()
        return profile

    @override_settings(**ON)
    def test_approving_a_pending_member_sends_exactly_one_email(self):
        profile = self.approve(self.member())
        self.assertEqual(profile.status, CommunityMemberProfile.Status.APPROVED)
        self.post.assert_called_once()
        call = self.post.call_args
        self.assertEqual(call.args[0], "https://api.resend.com/emails")
        self.assertEqual(call.kwargs["json"]["to"], ["priya@example.com"])
        self.assertEqual(call.kwargs["json"]["subject"], "You're in")
        self.assertEqual(call.kwargs["json"]["text"],
                         "Your MLAI membership is approved. You can now post in the community.")
        self.assertEqual(call.kwargs["headers"]["Idempotency-Key"], f"chat-review-cleared/{profile.pk}")
        for text in (call.kwargs["json"]["subject"], call.kwargs["json"]["text"]):
            self.assertNotIn(chr(0x2014), text)
            self.assertNotIn(chr(0x2013), text)

    @override_settings(**ON)
    def test_one_word_pending_member_is_approved_and_emailed_once(self):
        profile = self.approve(self.member(last_name=""))
        self.assertEqual(profile.status, CommunityMemberProfile.Status.APPROVED)
        self.assertEqual(self.post.call_count, 1)

    def test_one_word_pending_member_is_skipped_with_the_one_step_flag_off(self):
        with override_settings(COMMUNITY_CHAT_REVIEW_CLEARED_EMAIL_ENABLED=True):
            profile = self.approve(self.member(last_name=""))
        self.assertEqual(profile.status, CommunityMemberProfile.Status.PENDING)
        self.post.assert_not_called()

    @override_settings(**ON)
    def test_re_approving_or_approving_an_approved_member_sends_nothing(self):
        profile = self.approve(self.member())
        self.approve(profile)
        approved = self.approve(self.member(status=CommunityMemberProfile.Status.APPROVED, email="ok@example.com"))
        self.assertEqual(approved.status, CommunityMemberProfile.Status.APPROVED)
        self.assertEqual(self.post.call_count, 1)

    @override_settings(**ON)
    def test_a_member_pended_again_is_never_emailed_twice(self):
        profile = self.approve(self.member())
        CommunityMemberProfile.objects.filter(pk=profile.pk).update(status=CommunityMemberProfile.Status.PENDING)
        self.approve(profile)
        self.assertEqual(self.post.call_count, 1)

    @override_settings(**ON)
    def test_rejection_and_correction_send_nothing(self):
        for status in (CommunityMemberProfile.Status.REJECTED, CommunityMemberProfile.Status.INCOMPLETE):
            with self.subTest(status=status), self.captureOnCommitCallbacks(execute=True):
                profile = self.member(email=f"{status}@example.com")
                self.admin._review(self.request, CommunityMemberProfile.objects.filter(pk=profile.pk), status)
        self.post.assert_not_called()

    def test_flag_off_sends_nothing(self):
        profile = self.approve(self.member())
        self.assertEqual(profile.status, CommunityMemberProfile.Status.APPROVED)
        self.post.assert_not_called()

    @override_settings(**ON)
    def test_failed_send_never_undoes_the_approval(self):
        self.post.side_effect = RuntimeError("provider down")
        profile = self.approve(self.member())
        self.assertEqual(profile.status, CommunityMemberProfile.Status.APPROVED)

    @override_settings(**{**BASE, **ALL_P1_FLAGS_ON})
    def test_existing_approved_members_get_no_email_with_every_flag_on(self):
        self.approve(self.member(status=CommunityMemberProfile.Status.APPROVED))
        self.post.assert_not_called()
