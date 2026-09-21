"""Admission, privacy and consent regressions; disposable database only."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from community_chat.models import (
    CommunityChatAccountSession, CommunityChatBootstrapToken, CommunityChatDevice,
    CommunityChatEmailCodeChallenge, CommunityChatEmailCodeDelivery,
    CommunityMemberConsent, CommunityMemberProfile, CommunityMemberReviewRule,
)
from community_chat.onboarding import has_community_access
from community_chat.tests.test_account_profiles import credentials_for

ORIGIN = "https://chat.mlai.au"


@override_settings(
    COMMUNITY_CHAT_SIGNUP_ENABLED=True,
    COMMUNITY_CHAT_MEMBERSHIP_POLICY_VERSION="test-v1",
    COMMUNITY_CHAT_ALLOWED_ORIGINS=[ORIGIN, "mlaichat://callback"],
    COMMUNITY_CHAT_EMAIL_CODE_AUTH_ENABLED=True,
    COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET="test-delivery-secret",
    COMMUNITY_CHAT_EMAIL_CODE_MIN_RESPONSE_SECONDS=0,
    COMMUNITY_CHAT_EMAIL_CODE_PEPPER="test-code-pepper",
    CUSTOMERIO_API_KEY="test-key",
    CUSTOMERIO_COMMUNITY_CHAT_CODE_MESSAGE_ID="test-template",
)
class MemberOnboardingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(email="new@example.com", email_verified_at=timezone.now())
        self.credentials = credentials_for(self.user)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.credentials.access_token}")
        self.url = reverse("community_chat_onboarding")

    def basics(self, **changes):
        return self.client.put(self.url, {
            "step": "basics", "first_name": "Alex", "last_name": "",
            "adult_confirmed": True, "accept_rules": True, "policy_version": "test-v1", **changes,
        }, format="json")

    def complete(self, **changes):
        return self.client.put(self.url, {"step": "complete", "skip_personalisation": True, **changes}, format="json")

    def test_pending_account_can_resume_but_cannot_bootstrap_or_bypass_gate(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertTrue(self.client.get(reverse("community_chat_account")).data["onboarding"]["required"])
        self.assertFalse(has_community_access(self.user))
        self.assertEqual(self.client.get(reverse("community_chat_session")).status_code, 403)
        self.assertEqual(self.client.patch(reverse("community_chat_account"), {}, format="json").status_code, 403)
        self.assertEqual(self.complete().status_code, 400)
        self.assertEqual(self.basics().status_code, 200)
        self.assertFalse(has_community_access(self.user))
        self.assertEqual(self.client.get(reverse("community_chat_session")).status_code, 403)
        self.assertEqual(self.complete().data["onboarding"]["status"], "approved")
        self.assertTrue(has_community_access(self.user))
        self.assertEqual(self.client.get(reverse("community_chat_session")).status_code, 200)

    def test_required_consents_are_strict_and_cannot_be_injected(self):
        for changes in ({"adult_confirmed": False}, {"adult_confirmed": "true"},
                        {"accept_rules": 1}, {"policy_version": "old"}, {"first_name": ""},
                        {"first_name": "bad\u0001name"}, {"status": "approved"}, {"user_id": self.user.pk}):
            with self.subTest(changes=changes):
                self.assertEqual(self.basics(**changes).status_code, 400)
        self.assertFalse(CommunityMemberConsent.objects.exists())
        self.assertFalse(CommunityMemberProfile.objects.exists())

    def test_skip_and_retry_never_invent_optional_permission_or_duplicate_consent(self):
        self.basics()
        self.basics()
        self.complete()
        self.complete()
        profile = CommunityMemberProfile.objects.get(user=self.user)
        self.assertIsNone(profile.marketing_opt_in)
        self.assertEqual(profile.interests, [])
        self.assertEqual(CommunityMemberConsent.objects.filter(user=self.user).count(), 3)
        self.user.refresh_from_db()
        self.assertEqual(self.user.full_name, "Alex")
        self.assertNotIn("adult_confirmed_at", self.client.get(reverse("community_chat_account")).data["public_profile"])

    def test_optional_choices_are_private_limited_and_withdrawable(self):
        self.basics()
        for choices in (["research"] * 2, ["research", "startups", "exploring", "llms_agents"], ["unknown"]):
            self.assertEqual(self.complete(skip_personalisation=False, interests=choices).status_code, 400)
        self.assertEqual(self.complete(skip_personalisation=False, marketing_opt_in="yes").status_code, 400)
        response = self.complete(skip_personalisation=False, city="Melbourne", interests=["research"], marketing_opt_in=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["onboarding"]["marketing_opt_in"])
        response = self.complete(skip_personalisation=False, city="Melbourne", interests=["research"], marketing_opt_in=False)
        self.assertFalse(response.data["onboarding"]["marketing_opt_in"])
        history = list(CommunityMemberConsent.objects.filter(user=self.user, purpose="marketing_email").order_by("pk").values_list("granted", flat=True))
        self.assertEqual(history, [True, False])
        public = self.client.get(reverse("community_chat_account")).data["public_profile"]
        for key in ("email", "city", "interests", "marketing_opt_in", "adult_confirmed_at"):
            self.assertNotIn(key, public)
        other = get_user_model().objects.create_user(email="other@example.com")
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {credentials_for(other).access_token}")
        self.assertEqual(client.get(self.url).data["onboarding"]["interests"], [])

    def test_review_rules_are_specific_and_application_can_be_corrected(self):
        CommunityMemberReviewRule.objects.create(phrase="official", match="word", reason="Check affiliation")
        self.basics(first_name="Official", last_name="MLAI")
        response = self.complete()
        self.assertEqual(response.data["onboarding"]["status"], "pending_review")
        self.assertFalse(has_community_access(self.user))
        with override_settings(COMMUNITY_CHAT_SIGNUP_ENABLED=False):
            self.assertFalse(has_community_access(self.user))
            self.assertEqual(self.complete().status_code, 403)
        self.basics(first_name="李", last_name="")
        self.assertEqual(self.complete().data["onboarding"]["status"], "approved")

    def test_existing_members_keep_access_without_fabricated_age_or_consent(self):
        CommunityChatDevice.objects.create(user=self.user, public_key="b" * 64, status="verified", verified_at=timezone.now())
        self.assertTrue(has_community_access(self.user))
        self.assertFalse(self.client.get(self.url).data["onboarding"]["basics_complete"])
        self.assertFalse(CommunityMemberConsent.objects.exists())
        response = self.complete(skip_personalisation=False, city="Online only", interests=["exploring"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["onboarding"]["status"], "approved")
        self.assertFalse(response.data["onboarding"]["basics_complete"])
        self.assertFalse(CommunityMemberConsent.objects.exists())
        self.assertEqual(self.client.get(reverse("community_chat_account"))["Cache-Control"], "no-store")

    def test_pending_account_can_view_its_privacy_and_deletion_controls(self):
        for path in ("/api/v1/community-chat/account/ai-consent/", "/api/v1/community-chat/account/deletion/"):
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertFalse(has_community_access(self.user))

    def test_corrupt_signup_recipient_fails_closed_without_creating_an_account(self):
        from community_chat.email_codes import InvalidEmailCode, consume_email_code, issue_email_code_challenge
        with patch("community_chat.email_codes.secrets.randbelow", return_value=123456):
            challenge = issue_email_code_challenge(email="corrupt@example.com", client_id="mlai-chat-ios",
                installation_id=uuid.uuid4(), origin="mlaichat://callback", platform="ios", device_name="Phone",
                public_key="a" * 64, onboarding_version=1)
        CommunityChatEmailCodeChallenge.objects.filter(pk=challenge.pk).update(encrypted_signup_email="invalid")
        with self.assertRaises(InvalidEmailCode):
            consume_email_code(challenge_id=challenge.pk, code="123456", client_id=challenge.client_id,
                installation_id=challenge.installation_id)
        self.assertFalse(get_user_model().objects.filter(email="corrupt@example.com").exists())

    def test_rejected_application_and_expired_or_revoked_session_cannot_self_approve(self):
        CommunityMemberProfile.objects.create(user=self.user, status="rejected")
        self.assertEqual(self.basics().status_code, 403)
        self.assertEqual(self.complete().status_code, 403)
        CommunityChatAccountSession.objects.filter(pk=self.credentials.session.pk).update(revoked_at=timezone.now())
        self.assertEqual(self.basics().status_code, 401)

    def test_policy_change_requires_new_review_before_submission(self):
        self.basics()
        with override_settings(COMMUNITY_CHAT_MEMBERSHIP_POLICY_VERSION="test-v2"):
            self.assertFalse(self.client.get(self.url).data["onboarding"]["basics_complete"])
            self.assertEqual(self.complete().status_code, 400)
            self.assertEqual(self.basics(policy_version="test-v2").status_code, 200)
            self.assertEqual(self.complete().status_code, 200)

    def test_cookie_mutation_rejects_untrusted_origin(self):
        from community_chat.account_cookies import ACCESS_COOKIE
        client = APIClient()
        client.cookies[ACCESS_COOKIE] = self.credentials.access_token
        self.assertEqual(client.put(self.url, {"step": "complete"}, format="json", HTTP_ORIGIN="https://untrusted.example").status_code, 401)

    def test_committee_review_is_permissioned_and_audited(self):
        self.basics(first_name="MLAI", last_name="Support")
        self.complete()
        profile = CommunityMemberProfile.objects.get(user=self.user)
        url = reverse("admin:community_chat_communitymemberprofile_changelist")
        self.client.force_login(self.user)
        response = self.client.post(url, {"action": "approve_applications", "_selected_action": [profile.pk]})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(has_community_access(self.user))
        reviewer = get_user_model().objects.create_superuser(email="reviewer@example.com", password="test-password")
        self.client.force_login(reviewer)
        response = self.client.post(url, {"action": "approve_applications", "_selected_action": [profile.pk]})
        self.assertEqual(response.status_code, 302)
        profile.refresh_from_db()
        self.assertEqual(profile.status, "approved")
        self.assertEqual(profile.reviewed_by, reviewer)
        self.assertTrue(LogEntry.objects.filter(user=reviewer, object_id=str(profile.pk)).exists())

    def test_requested_correction_resumes_basics_without_erasing_consent_history(self):
        self.basics(first_name="MLAI", last_name="Support")
        self.complete()
        reviewer = get_user_model().objects.create_superuser(email="correction@example.com", password="test-password")
        self.client.force_login(reviewer)
        profile = CommunityMemberProfile.objects.get(user=self.user)
        self.client.post(reverse("admin:community_chat_communitymemberprofile_changelist"),
            {"action": "request_correction", "_selected_action": [profile.pk]})
        self.assertFalse(self.client.get(self.url).data["onboarding"]["basics_complete"])
        self.assertEqual(CommunityMemberConsent.objects.filter(user=self.user).count(), 3)
        self.assertEqual(self.complete().status_code, 400)
        self.assertFalse(has_community_access(self.user))
        self.assertEqual(self.basics(first_name="Alex").status_code, 200)
        self.assertTrue(self.client.get(self.url).data["onboarding"]["basics_complete"])

    def test_new_account_is_created_only_after_valid_code_and_without_bootstrap_access(self):
        client = APIClient()
        installation = str(uuid.uuid4())
        response = client.post(reverse("community_chat_email_code_request"), {
            "email": "NewSignup@Example.com", "onboarding_version": 1, "client_id": "mlai-chat-web",
            "device": {"installation_id": installation, "public_key": "a" * 64, "platform": "web", "name": "Browser"},
        }, format="json", HTTP_ORIGIN=ORIGIN)
        self.assertEqual(response.status_code, 202)
        self.assertFalse(get_user_model().objects.filter(email="newsignup@example.com").exists())
        challenge = CommunityChatEmailCodeChallenge.objects.get(pk=response.data["challenge_id"])
        self.assertIsNone(challenge.user_id)
        self.assertNotIn("newsignup", challenge.encrypted_signup_email)
        with patch("community_chat.email_delivery.send_community_chat_email_code", return_value={"delivery_id": "test"}) as send:
            call_command("run_email_code_worker", "--once", stdout=StringIO())
            self.assertEqual(send.call_args.args[0].email, "newsignup@example.com")
            code = send.call_args.args[1]
        verify = {"challenge_id": str(challenge.pk), "code": code, "client_id": "mlai-chat-web", "installation_id": installation}
        response = client.post(reverse("community_chat_email_code_verify"), verify, format="json", HTTP_ORIGIN=ORIGIN)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "onboarding_required")
        self.assertEqual(response.data["bootstrap_token"], "")
        user = get_user_model().objects.get(email="newsignup@example.com")
        self.assertIsNotNone(user.email_verified_at)
        self.assertFalse(user.has_usable_password())
        self.assertFalse(CommunityChatBootstrapToken.objects.filter(user=user).exists())
        self.assertFalse(CommunityChatDevice.objects.filter(user=user).exists())
        challenge.refresh_from_db()
        self.assertEqual(challenge.encrypted_signup_email, "")

        self.assertEqual(client.post(reverse("community_chat_email_code_verify"), verify, format="json", HTTP_ORIGIN=ORIGIN).status_code, 400)
        self.assertEqual(get_user_model().objects.filter(email="newsignup@example.com").count(), 1)

    def test_old_clients_and_disabled_signup_cannot_create_unknown_accounts(self):
        from community_chat.email_codes import issue_email_code_challenge
        for enabled, version in ((True, 0), (False, 1)):
            with self.subTest(enabled=enabled, version=version), override_settings(COMMUNITY_CHAT_SIGNUP_ENABLED=enabled):
                challenge = issue_email_code_challenge(email="missing@example.com", client_id="mlai-chat-ios",
                    installation_id=uuid.uuid4(), origin="mlaichat://callback", platform="ios", device_name="Phone",
                    public_key="a" * 64, onboarding_version=version)
                self.assertFalse(CommunityChatEmailCodeDelivery.objects.filter(challenge=challenge).exists())
                self.assertEqual(challenge.encrypted_signup_email, "")
        self.assertFalse(get_user_model().objects.filter(email="missing@example.com").exists())

    def test_unverified_signup_email_is_erased_on_expiry(self):
        from community_chat.email_codes import issue_email_code_challenge
        from community_chat.email_delivery import claim_email_code_delivery
        challenge = issue_email_code_challenge(email="expired@example.com", client_id="mlai-chat-ios",
            installation_id=uuid.uuid4(), origin="mlaichat://callback", platform="ios", device_name="Phone",
            public_key="a" * 64, onboarding_version=1)
        CommunityChatEmailCodeChallenge.objects.filter(pk=challenge.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        claim_email_code_delivery()
        challenge.refresh_from_db()
        self.assertEqual(challenge.encrypted_signup_email, "")

@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
@override_settings(
    COMMUNITY_CHAT_SIGNUP_ENABLED=True,
    COMMUNITY_CHAT_MEMBERSHIP_POLICY_VERSION="test-v1",
    COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET="test-delivery-secret",
    COMMUNITY_CHAT_EMAIL_CODE_PEPPER="test-code-pepper",
)
class MemberOnboardingConcurrencyTests(TransactionTestCase):
    def test_simultaneous_email_proofs_create_one_canonical_account(self):
        from community_chat.email_codes import consume_email_code, issue_email_code_challenge
        with patch("community_chat.email_codes.secrets.randbelow", return_value=123456):
            challenges = [issue_email_code_challenge(email="same@example.com", client_id="mlai-chat-ios",
                installation_id=uuid.uuid4(), origin="mlaichat://callback", platform="ios", device_name="Phone",
                public_key=("a" if i else "b") * 64, onboarding_version=1) for i in range(2)]
        barrier = Barrier(2)

        def verify(challenge):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                user, _ = consume_email_code(challenge_id=challenge.pk, code="123456",
                    client_id=challenge.client_id, installation_id=challenge.installation_id)
                return user.pk
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            users = list(pool.map(verify, challenges))
        self.assertEqual(users[0], users[1])
        self.assertEqual(get_user_model().objects.filter(email="same@example.com").count(), 1)
        self.assertEqual(CommunityMemberProfile.objects.count(), 1)

    def test_simultaneous_submission_approves_once_and_deduplicates_consent(self):
        from community_chat.onboarding import save_onboarding
        user = get_user_model().objects.create_user(email="concurrent@example.com", email_verified_at=timezone.now())
        credentials = [credentials_for(user) for _ in range(2)]
        save_onboarding(authenticated_session=credentials[0].session, values={"step": "basics", "first_name": "Alex",
            "adult_confirmed": True, "accept_rules": True, "policy_version": "test-v1"})
        barrier = Barrier(2)

        def complete(credentials):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                save_onboarding(authenticated_session=credentials.session, values={"step": "complete",
                    "city": "Melbourne", "interests": ["research"], "marketing_opt_in": True})
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(complete, credentials))
        self.assertEqual(CommunityMemberProfile.objects.get(user=user).status, "approved")
        self.assertEqual(CommunityMemberConsent.objects.filter(user=user).count(), 4)
