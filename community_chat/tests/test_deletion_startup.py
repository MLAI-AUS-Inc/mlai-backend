import io
import json

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from community_chat.deletion_startup import handoff_startup
from community_chat.models import AccountDeletionRequest
from founder_tools.models import StartupMembership, VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization


class StartupErasureHandoffTests(TestCase):
    def test_handoff_requires_an_existing_member_and_preserves_the_startup(self):
        owner, successor = [
            get_user_model().objects.create_user(email=f"{alias}@example.test")
            for alias in ("owner", "successor")
        ]
        owner_profile, next_profile = [
            VibeRaisingProfile.objects.create(user=user, role="founder")
            for user in (owner, successor)
        ]
        org = Organization.objects.create(name="Fixture", domain="fixture.example.test", billing_user=owner)
        company = VibeRaisingCompany.objects.create(
            name="Fixture", organization=org, profile=owner_profile,
            membership_policy=VibeRaisingCompany.SHARED_ACCESS,
        )
        original_access = StartupMembership.objects.create(
            company=company, user=owner, joined_at=timezone.now(), source="test",
        )
        case = AccountDeletionRequest.objects.create(
            user=owner, scope=AccountDeletionRequest.Scope.ACCOUNT, policy_version="fixture",
        )
        result = io.StringIO()
        call_command("review_account_erasure", str(case.pk), stdout=result)
        self.assertEqual(json.loads(result.getvalue())["founder_tools.startupmembership.user"], 1)
        self.assertNotIn(owner.email, result.getvalue())
        args = {"request_id": case.pk, "company_id": company.pk, "successor_id": successor.pk}
        with self.assertRaises(ValueError):
            handoff_startup(**args, apply=True)
        retained_access = StartupMembership.objects.create(
            company=company, user=successor, joined_at=timezone.now(), source="test",
        )
        self.assertEqual(handoff_startup(**args, apply=False), "ready")
        company.refresh_from_db()
        self.assertEqual(company.profile_id, owner_profile.pk)
        self.assertEqual(handoff_startup(**args, apply=True), "transferred")
        self.assertEqual(handoff_startup(**args, apply=True), "already_transferred")
        company.refresh_from_db()
        org.refresh_from_db()
        original_access.refresh_from_db()
        retained_access.refresh_from_db()
        self.assertEqual(company.profile_id, next_profile.pk)
        self.assertIsNone(org.billing_user_id)
        self.assertIsNotNone(original_access.removed_at)
        self.assertIsNone(retained_access.removed_at)
