import json

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from .models import PointsAccount, PointsAdmin


User = get_user_model()


@override_settings(ROO_API_KEY="roo-committee-test-key", INTERNAL_API_KEY="")
class CommitteeCandidateEmailApiTests(APITestCase):
    def setUp(self):
        self.client.credentials(HTTP_X_API_KEY="roo-committee-test-key")
        self.admin = User.objects.create_user(
            email="committee-admin@example.com",
            slack_id="UADMIN1763",
        )
        self.admin_role = PointsAdmin.objects.create(
            user=self.admin,
            slack_user_id=self.admin.slack_id,
            role="admin",
            is_active=True,
        )
        self.url = reverse("committee-candidate-emails")

    def create_points_user(
        self,
        suffix,
        *,
        lifetime_earned,
        email=None,
        slack_id=None,
        is_active=True,
        balance=0,
        earned_balance=0,
        purchased_balance=0,
    ):
        user = User.objects.create_user(
            email=email or f"{suffix.lower()}@example.com",
            slack_id=slack_id,
        )
        if not is_active:
            user.is_active = False
            user.save(update_fields=["is_active"])
        PointsAccount.objects.create(
            user=user,
            balance=balance,
            earned_balance=earned_balance,
            purchased_topup_balance=purchased_balance,
            lifetime_earned=lifetime_earned,
            lifetime_purchased_topup=purchased_balance,
        )
        return user

    def post(self, requester_slack_id=None):
        return self.client.post(
            self.url,
            {"requester_slack_id": requester_slack_id or self.admin.slack_id},
            format="json",
        )

    def test_uses_inclusive_lifetime_earned_threshold_only(self):
        self.create_points_user("NINETYNINE", lifetime_earned=99)
        self.create_points_user(
            "EXACTLY",
            lifetime_earned=100,
            balance=0,
            earned_balance=0,
        )
        self.create_points_user(
            "ABOVE",
            lifetime_earned=101,
            balance=500,
            earned_balance=1,
            purchased_balance=499,
        )
        self.create_points_user(
            "PURCHASED",
            lifetime_earned=0,
            balance=500,
            purchased_balance=500,
        )

        response = self.post()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["threshold"], 100)
        self.assertEqual(response.data["metric"], "lifetime_earned")
        self.assertEqual(response.data["eligible_count"], 2)
        self.assertEqual(
            response.data["emails"],
            ["above@example.com", "exactly@example.com"],
        )

    def test_includes_unlinked_users_and_excludes_unusable_accounts(self):
        self.create_points_user(
            "UNLINKED",
            lifetime_earned=100,
            email="Unlinked.Member@Example.com",
            slack_id=None,
        )
        self.create_points_user(
            "PLACEHOLDER",
            lifetime_earned=200,
            email="UPLACEHOLDER@slack.placeholder.com",
            slack_id="UPLACEHOLDER",
        )
        self.create_points_user(
            "INACTIVE",
            lifetime_earned=300,
            is_active=False,
        )
        blank_email_user = User(email="", slack_id="UBLANKEMAIL")
        blank_email_user.set_unusable_password()
        blank_email_user.save()
        PointsAccount.objects.create(
            user=blank_email_user,
            lifetime_earned=400,
        )
        invalid_email_user = User(email="not-an-email", slack_id="UINVALIDEMAIL")
        invalid_email_user.set_unusable_password()
        invalid_email_user.save()
        PointsAccount.objects.create(
            user=invalid_email_user,
            lifetime_earned=500,
        )

        response = self.post()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["emails"], ["unlinked.member@example.com"])

    def test_response_contains_only_email_export_metadata(self):
        self.create_points_user(
            "PRIVATE",
            lifetime_earned=100,
            slack_id="UPRIVATE",
        )

        response = self.post()
        serialized = json.dumps(response.data).lower()

        self.assertEqual(
            set(response.data),
            {"eligible_count", "threshold", "metric", "emails"},
        )
        self.assertNotIn("uprivate", serialized)
        self.assertNotIn("slack", serialized)
        self.assertNotIn("points", serialized)
        self.assertNotIn("name", serialized)

    def test_admin_and_committee_roles_are_allowed(self):
        for role in ("admin", "committee"):
            with self.subTest(role=role):
                self.admin_role.role = role
                self.admin_role.is_active = True
                self.admin_role.save(update_fields=["role", "is_active"])
                self.assertEqual(self.post().status_code, status.HTTP_200_OK)

    def test_other_roles_inactive_admins_and_members_are_denied(self):
        for role in ("partner", "portfolio_lead"):
            with self.subTest(role=role):
                self.admin_role.role = role
                self.admin_role.is_active = True
                self.admin_role.save(update_fields=["role", "is_active"])
                self.assertEqual(self.post().status_code, status.HTTP_403_FORBIDDEN)

        self.admin_role.role = "admin"
        self.admin_role.is_active = False
        self.admin_role.save(update_fields=["role", "is_active"])
        self.assertEqual(self.post().status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.post("UORDINARY").status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(POINTS_BOOTSTRAP_ADMIN_SLACK_IDS=["UBOOTSTRAP"])
    def test_bootstrap_only_admin_is_denied(self):
        self.assertEqual(self.post("UBOOTSTRAP").status_code, status.HTTP_403_FORBIDDEN)

    def test_requester_and_strict_roo_key_are_required(self):
        missing_requester = self.client.post(self.url, {}, format="json")
        self.assertEqual(missing_requester.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(missing_requester.data["code"], "requester_required")

        self.client.credentials()
        missing_key = self.client.post(
            self.url,
            {"requester_slack_id": self.admin.slack_id},
            format="json",
        )
        self.assertEqual(missing_key.status_code, status.HTTP_401_UNAUTHORIZED)

        self.client.credentials(HTTP_X_API_KEY="wrong-key")
        wrong_key = self.client.post(
            self.url,
            {"requester_slack_id": self.admin.slack_id},
            format="json",
        )
        self.assertEqual(wrong_key.status_code, status.HTTP_401_UNAUTHORIZED)
