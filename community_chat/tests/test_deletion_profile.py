"""A deletion removes private Chat data without erasing paid entitlements."""

import hashlib
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from community_chat import deletion_profile, deletion_tasks
from community_chat.authentication import USAGE_TOKEN_PREFIX
from community_chat.models import (
    AccountDeletionRequest,
    AiConsentRecord,
    CommunityMemberConsent,
    CommunityMemberProfile,
    TokenUsageAccount,
    TokenUsageDailyBucket,
    TokenUsageSession,
)
from community_chat.roo_avatar.models import CommunityRooAvatar


class DeletionProfileTests(TestCase):
    def test_purge_waits_for_verified_revocation_and_preserves_other_users_and_rewards(self):
        user_model = get_user_model()
        user = user_model.objects.create_user(email="departing@example.test")
        other = user_model.objects.create_user(email="retained@example.test")
        CommunityMemberProfile.objects.create(user=user, first_name="Remove")
        CommunityMemberProfile.objects.create(user=other, first_name="Keep")
        CommunityMemberConsent.objects.create(
            user=user, purpose="marketing_email", granted=False, source="fixture", policy_version="fixture",
        )
        AiConsentRecord.objects.create(user=user, granted_at=timezone.now())
        outfit = CommunityRooAvatar.objects.create(user=user, community="test", fur="blue_flier")
        raw_token = f"{USAGE_TOKEN_PREFIX}synthetic"
        usage = TokenUsageAccount.objects.create(user=user, token_hash=hashlib.sha256(raw_token.encode()).hexdigest())
        TokenUsageSession.objects.create(
            account=usage, source="codex", session_id="session", model="fixture", started_at=timezone.now(),
        )
        TokenUsageDailyBucket.objects.create(
            account=usage, source="codex", session_id="session", model="fixture", usage_date=timezone.localdate(),
        )

        case = AccountDeletionRequest.objects.create(
            user=user, scope=AccountDeletionRequest.Scope.CHAT, policy_version="fixture",
        )
        deletion_tasks.schedule_deletion(case)
        profile_task = case.tasks.get(target=deletion_profile.TARGET)
        self.assertFalse(deletion_profile.execute_task(profile_task.pk))
        self.assertTrue(CommunityMemberProfile.objects.filter(user=user).exists())

        chat_auth = APIClient()
        chat_auth.force_authenticate(user=user)
        self.assertEqual(chat_auth.post(reverse("token_usage_token")).status_code, 403)
        reporter = APIClient()
        reporter.credentials(HTTP_AUTHORIZATION=f"Bearer {raw_token}")
        self.assertEqual(reporter.post(reverse("token_usage_ingest"), {}, format="json").status_code, 401)

        access = deletion_tasks.claim_task(case.tasks.get(target="chat_access").pk)
        deletion_tasks.complete_task(access.pk, attempt=access.attempts, verified_counts={
            "remaining_rows": 0, "verified_targets": 1,
        })
        case.tasks.filter(pk=profile_task.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        self.assertTrue(deletion_profile.execute_task(profile_task.pk))
        self.assertFalse(deletion_profile.execute_task(profile_task.pk))
        self.assertFalse(CommunityMemberProfile.objects.filter(user=user).exists())
        self.assertFalse(AiConsentRecord.objects.filter(user=user).exists())
        self.assertFalse(TokenUsageAccount.objects.filter(user=user).exists())
        self.assertFalse(TokenUsageSession.objects.filter(account_id=usage.pk).exists())
        self.assertFalse(TokenUsageDailyBucket.objects.filter(account_id=usage.pk).exists())
        self.assertTrue(CommunityMemberProfile.objects.filter(user=other).exists())
        self.assertTrue(CommunityMemberConsent.objects.filter(user=user, granted=False).exists())
        self.assertTrue(user_model.objects.filter(pk=user.pk).exists())
        outfit.refresh_from_db()
        self.assertIsNone(outfit.fur)
        self.assertIn("blue_flier", outfit.owned_items)
