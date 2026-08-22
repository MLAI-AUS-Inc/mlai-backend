from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Barrier
from urllib.parse import parse_qs, urlparse

from django.db import IntegrityError, close_old_connections, transaction
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from core.models import (
    SlackFounderAccountLink,
    SlackFounderLinkRequest,
    User,
)
from core.slack_founder_links import (
    ConflictingSlackFounderLinkError,
    SlackFounderLinkError,
    UsedSlackFounderLinkError,
    complete_slack_founder_link,
    create_slack_founder_link_request,
    digest_link_token,
    start_slack_founder_link,
)


@override_settings(
    ROO_API_KEY="roo-link-key",
    FOUNDER_TOOLS_URL="https://mlai.test",
    ROO_FOUNDER_LINK_TTL_SECONDS=1800,
)
class SlackFounderLinkApiTests(APITestCase):
    def setUp(self):
        self.slack_user = User.objects.create_user(
            email="slack-account@example.com",
            slack_id="ULINK12345",
            first_name="Slack",
            last_name="Founder",
        )
        self.founder_user = User.objects.create_user(
            email="founder-tools@example.com",
        )
        self.start_url = reverse("slack_founder_link_start")
        self.status_url = reverse("slack_founder_link_status")
        self.preview_url = reverse("slack_founder_link_preview")
        self.complete_url = reverse("slack_founder_link_complete")

    def _start(self):
        return self.client.post(
            self.start_url,
            {"slack_user_id": self.slack_user.slack_id},
            format="json",
            HTTP_X_API_KEY="roo-link-key",
        )

    def _token_from_response(self, response):
        return parse_qs(urlparse(response.data["link_url"]).query)["token"][0]

    def test_start_requires_strict_roo_key(self):
        response = self.client.post(
            self.start_url,
            {"slack_user_id": self.slack_user.slack_id},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_start_returns_hashed_expiring_single_use_token(self):
        response = self._start()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], "link_required")
        self.assertTrue(
            response.data["link_url"].startswith(
                "https://mlai.test/founder-tools/link-roo?token="
            )
        )
        token = self._token_from_response(response)
        link_request = SlackFounderLinkRequest.objects.get()
        self.assertNotEqual(link_request.token_digest, token)
        self.assertEqual(link_request.token_digest, digest_link_token(token))
        self.assertGreater(link_request.expires_at, timezone.now())
        self.assertLessEqual(
            link_request.expires_at,
            timezone.now() + timedelta(minutes=31),
        )

    def test_new_start_invalidates_previous_unused_token(self):
        first = self._start()
        first_token = self._token_from_response(first)
        second = self._start()

        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        first_request = SlackFounderLinkRequest.objects.get(
            token_digest=digest_link_token(first_token)
        )
        self.assertIsNotNone(first_request.invalidated_at)

        self.client.force_authenticate(self.founder_user)
        preview = self.client.post(
            self.preview_url,
            {"token": first_token},
            format="json",
        )
        self.assertEqual(preview.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(preview.data["code"], "invalid_token")

    def test_start_returns_not_found_for_unregistered_slack_user(self):
        response = self.client.post(
            self.start_url,
            {"slack_user_id": "UMISSING1"},
            format="json",
            HTTP_X_API_KEY="roo-link-key",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data["code"], "slack_user_not_found")

    def test_preview_and_complete_require_authenticated_user(self):
        response = self._start()
        token = self._token_from_response(response)

        preview = self.client.post(
            self.preview_url,
            {"token": token},
            format="json",
        )
        complete = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(preview.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(complete.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_status_requires_authenticated_user(self):
        response = self.client.get(self.status_url)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_status_reports_unconnected_founder_without_identity_details(self):
        self.client.force_authenticate(self.founder_user)

        response = self.client.get(self.status_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            {
                "status": "not_connected",
                "connection_type": None,
                "can_link_separate_account": True,
                "slack_display_name": None,
                "verified_at": None,
            },
        )
        self.assertNotIn("slack_id", response.data)
        self.assertNotIn("email", response.data)

    def test_status_treats_existing_same_account_identity_as_connected(self):
        self.client.force_authenticate(self.slack_user)

        response = self.client.get(self.status_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "connected")
        self.assertEqual(response.data["connection_type"], "direct")
        self.assertTrue(response.data["can_link_separate_account"])
        self.assertEqual(response.data["slack_display_name"], "Slack Founder")
        self.assertIsNone(response.data["verified_at"])
        self.assertNotIn("slack_id", response.data)

    def test_status_reports_explicit_verified_connection(self):
        link = SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=self.founder_user,
        )
        self.client.force_authenticate(self.founder_user)

        response = self.client.get(self.status_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "connected")
        self.assertEqual(response.data["connection_type"], "explicit")
        self.assertFalse(response.data["can_link_separate_account"])
        self.assertEqual(response.data["slack_display_name"], "Slack Founder")
        self.assertEqual(response.data["verified_at"], link.verified_at.isoformat())
        self.assertNotIn("slack_id", response.data)
        self.assertNotIn("email", response.data)

    def test_malformed_token_is_rejected_before_lookup(self):
        self.client.force_authenticate(self.founder_user)

        preview = self.client.post(
            self.preview_url,
            {"token": "not-a-valid-token"},
            format="json",
        )

        self.assertEqual(preview.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(preview.data["code"], "invalid_token")

    def test_authenticated_user_previews_and_confirms_link(self):
        response = self._start()
        token = self._token_from_response(response)
        self.client.force_authenticate(self.founder_user)

        preview = self.client.post(
            self.preview_url,
            {"token": token},
            format="json",
        )
        complete = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(preview.status_code, status.HTTP_200_OK)
        self.assertEqual(preview.data["status"], "ready")
        self.assertEqual(preview.data["slack_display_name"], "Slack Founder")
        self.assertEqual(complete.status_code, status.HTTP_201_CREATED)
        self.assertEqual(complete.data["status"], "linked")
        link = SlackFounderAccountLink.objects.get()
        self.assertEqual(link.slack_user, self.slack_user)
        self.assertEqual(link.founder_user, self.founder_user)

    def test_same_account_completion_is_a_noop_without_creating_a_trapping_link(self):
        response = self._start()
        token = self._token_from_response(response)
        self.client.force_authenticate(self.slack_user)

        preview = self.client.post(
            self.preview_url,
            {"token": token},
            format="json",
        )
        complete = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(preview.status_code, status.HTTP_200_OK)
        self.assertEqual(preview.data["status"], "already_connected")
        self.assertEqual(complete.status_code, status.HTTP_200_OK)
        self.assertEqual(complete.data, {"status": "already_connected"})
        self.assertFalse(SlackFounderAccountLink.objects.exists())
        self.assertIsNotNone(
            SlackFounderLinkRequest.objects.get().consumed_at
        )

        # A no-op same-account confirmation must not prevent a later request
        # for the user's actual separate Founder Tools account.
        follow_up = self._start()
        self.assertEqual(follow_up.status_code, status.HTTP_201_CREATED)
        self.assertEqual(follow_up.data["status"], "link_required")

    def test_linking_does_not_transfer_identity_or_points_records(self):
        from roo.models import CoworkingBooking, Ledger, PointsAccount

        slack_account = PointsAccount.objects.create(
            user=self.slack_user,
            balance=17,
            earned_balance=17,
            lifetime_earned=17,
        )
        founder_account = PointsAccount.objects.create(
            user=self.founder_user,
            balance=91,
            earned_balance=91,
            lifetime_earned=91,
        )
        ledger = Ledger.objects.create(
            user=self.slack_user,
            delta=-8,
            kind="SPEND",
            source="COWORKING",
            created_by_slack_id=self.slack_user.slack_id,
            idempotency_key="pre-link-ledger",
        )
        booking = CoworkingBooking.objects.create(
            user=self.slack_user,
            date=date.today() + timedelta(days=1),
            points_cost=8,
            ledger_entry=ledger,
        )
        original_identity = {
            "slack_email": self.slack_user.email,
            "slack_id": self.slack_user.slack_id,
            "founder_email": self.founder_user.email,
            "founder_slack_id": self.founder_user.slack_id,
        }

        response = self._start()
        self.client.force_authenticate(self.founder_user)
        complete = self.client.post(
            self.complete_url,
            {"token": self._token_from_response(response)},
            format="json",
        )

        self.assertEqual(complete.status_code, status.HTTP_201_CREATED)
        self.slack_user.refresh_from_db()
        self.founder_user.refresh_from_db()
        slack_account.refresh_from_db()
        founder_account.refresh_from_db()
        ledger.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual(
            {
                "slack_email": self.slack_user.email,
                "slack_id": self.slack_user.slack_id,
                "founder_email": self.founder_user.email,
                "founder_slack_id": self.founder_user.slack_id,
            },
            original_identity,
        )
        self.assertEqual(slack_account.balance, 17)
        self.assertEqual(founder_account.balance, 91)
        self.assertEqual(ledger.user, self.slack_user)
        self.assertEqual(booking.user, self.slack_user)

    def test_consumed_token_replay_is_rejected(self):
        response = self._start()
        token = self._token_from_response(response)
        self.client.force_authenticate(self.founder_user)
        first = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )
        second = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(second.data["code"], "token_already_used")

    def test_expired_token_is_rejected(self):
        response = self._start()
        token = self._token_from_response(response)
        SlackFounderLinkRequest.objects.update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.client.force_authenticate(self.founder_user)

        complete = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(complete.status_code, status.HTTP_410_GONE)
        self.assertEqual(complete.data["code"], "expired_token")

    def test_different_valid_request_for_same_pair_is_idempotent(self):
        first = self._start()
        self.client.force_authenticate(self.founder_user)
        self.client.post(
            self.complete_url,
            {"token": self._token_from_response(first)},
            format="json",
        )
        second_request, second_token = create_slack_founder_link_request(
            self.slack_user
        )

        complete = self.client.post(
            self.complete_url,
            {"token": second_token},
            format="json",
        )

        self.assertEqual(complete.status_code, status.HTTP_200_OK)
        self.assertEqual(complete.data["status"], "already_linked")
        second_request.refresh_from_db()
        self.assertIsNotNone(second_request.consumed_at)
        self.assertEqual(SlackFounderAccountLink.objects.count(), 1)

    def test_start_reports_existing_explicit_link_without_new_token(self):
        SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=self.founder_user,
        )

        response = self._start()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"status": "already_linked"})
        self.assertFalse(SlackFounderLinkRequest.objects.exists())

    def test_start_service_checks_existing_link_under_the_user_lock(self):
        SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=self.founder_user,
        )

        result = start_slack_founder_link(self.slack_user)

        self.assertEqual(result.status, "already_linked")
        self.assertIsNone(result.request)
        self.assertIsNone(result.raw_token)
        self.assertFalse(SlackFounderLinkRequest.objects.exists())

    def test_conflicting_explicit_slack_link_is_rejected(self):
        other_founder = User.objects.create_user(email="other-founder@example.com")
        SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=other_founder,
        )
        _, token = create_slack_founder_link_request(self.slack_user)
        self.client.force_authenticate(self.founder_user)

        complete = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(complete.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(complete.data["code"], "link_conflict")
        self.assertEqual(SlackFounderAccountLink.objects.count(), 1)

    def test_conflicting_founder_link_is_rejected(self):
        other_slack = User.objects.create_user(
            email="other-slack@example.com",
            slack_id="UOTHER1234",
        )
        SlackFounderAccountLink.objects.create(
            slack_user=other_slack,
            founder_user=self.founder_user,
        )
        _, token = create_slack_founder_link_request(self.slack_user)
        self.client.force_authenticate(self.founder_user)

        complete = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(complete.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(complete.data["code"], "link_conflict")

    def test_cross_role_explicit_link_is_rejected(self):
        other_slack = User.objects.create_user(
            email="cross-role-slack@example.com",
            slack_id="UCROSS1234",
        )
        SlackFounderAccountLink.objects.create(
            slack_user=other_slack,
            founder_user=self.slack_user,
        )
        _, token = create_slack_founder_link_request(self.slack_user)
        self.client.force_authenticate(self.founder_user)

        complete = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(complete.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(complete.data["code"], "link_conflict")

    def test_founder_with_different_direct_slack_identity_is_rejected(self):
        self.founder_user.slack_id = "UDIRECT123"
        self.founder_user.save(update_fields=["slack_id"])
        _, token = create_slack_founder_link_request(self.slack_user)
        self.client.force_authenticate(self.founder_user)

        complete = self.client.post(
            self.complete_url,
            {"token": token},
            format="json",
        )

        self.assertEqual(complete.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(complete.data["code"], "link_conflict")

    def test_database_enforces_unique_slack_and_founder_references(self):
        link = SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=self.founder_user,
        )
        other_slack = User.objects.create_user(
            email="constraint-slack@example.com",
            slack_id="UUNIQUE123",
        )
        other_founder = User.objects.create_user(
            email="constraint-founder@example.com",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            SlackFounderAccountLink.objects.create(
                slack_user=self.slack_user,
                founder_user=other_founder,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            SlackFounderAccountLink.objects.create(
                slack_user=other_slack,
                founder_user=self.founder_user,
            )

        self.assertEqual(SlackFounderAccountLink.objects.get(), link)

    def test_legacy_link_slack_cannot_add_a_second_identity_to_linked_founder(self):
        SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=self.founder_user,
        )

        response = self.client.post(
            reverse("link_slack"),
            {
                "slack_id": "USECOND123",
                "email": self.founder_user.email,
            },
            format="json",
            HTTP_X_API_KEY="roo-link-key",
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "link_conflict")
        self.founder_user.refresh_from_db()
        self.assertIsNone(self.founder_user.slack_id)

    def test_slack_registration_keeps_explicit_founder_as_a_separate_account(self):
        SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=self.founder_user,
        )

        response = self.client.post(
            reverse("get_or_create_slack_user"),
            {
                "slack_id": "USECOND123",
                "email": self.founder_user.email,
                "first_name": "Second",
            },
            format="json",
            HTTP_X_API_KEY="roo-link-key",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            response.data["email"],
            "usecond123@slack.placeholder.com",
        )
        self.assertEqual(response.data["slack_id"], "USECOND123")
        self.founder_user.refresh_from_db()
        self.assertIsNone(self.founder_user.slack_id)


@override_settings(
    COWORKING_DAY_COST_POINTS=8,
    COWORKING_DAY_DISCOUNT_COST_POINTS=4,
    DEFAULT_COWORKING_CAPACITY=20,
    INTERNAL_API_KEY="internal-key",
    ROO_API_KEY="roo-link-key",
)
class LinkedCoworkingEligibilityTests(APITestCase):
    def setUp(self):
        from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
        from organizations.models import Organization
        from roo.models import PointsAccount, PointsAdmin
        from startup_updates.models import (
            MonthlyUpdateDraft,
            MonthlyUpdateDraftStatus,
            UserStartupBinding,
        )

        self.slack_user = User.objects.create_user(
            email="roo-points@example.com",
            slack_id="UCOWLINK12",
        )
        self.founder_user = User.objects.create_user(
            email="monthly-updates@example.com",
        )
        self.admin_user = User.objects.create_user(
            email="points-admin@example.com",
            slack_id="ULINKADMIN",
        )
        PointsAdmin.objects.create(
            user=self.admin_user,
            slack_user_id=self.admin_user.slack_id,
            role="admin",
        )
        PointsAccount.objects.create(
            user=self.slack_user,
            balance=20,
            earned_balance=20,
            lifetime_earned=20,
        )
        PointsAccount.objects.create(
            user=self.founder_user,
            balance=99,
            earned_balance=99,
            lifetime_earned=99,
        )
        organization = Organization.objects.create(
            name="Linked Founder Pty Ltd",
            domain="linked-founder.example",
        )
        profile = VibeRaisingProfile.objects.create(
            user=self.founder_user,
            role=VibeRaisingProfile.ROLE_FOUNDER,
        )
        VibeRaisingCompany.objects.create(
            profile=profile,
            organization=organization,
            name="Linked Founder Pty Ltd",
            registered=True,
            abn="89000000019",
            acn="000000019",
            abr_verified_at=timezone.now(),
        )
        UserStartupBinding.objects.create(
            user=self.founder_user,
            organization=organization,
            coworking_discount_eligible=True,
        )
        self.booking_date = date.today() + timedelta(days=1)
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            month=self.booking_date.replace(day=1),
            status=MonthlyUpdateDraftStatus.READY,
        )
        SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=self.founder_user,
        )

    def test_linked_founder_eligibility_discounts_without_moving_points(self):
        from roo.models import Ledger, PointsAccount
        from roo.services import CoworkingService

        booking, created = CoworkingService.book(
            user=self.slack_user,
            booking_date=self.booking_date,
            created_by_slack_id=self.slack_user.slack_id,
        )

        self.assertTrue(created)
        self.assertEqual(booking.user, self.slack_user)
        self.assertEqual(booking.points_cost, 4)
        self.assertEqual(booking.ledger_entry.user, self.slack_user)
        self.assertEqual(
            Ledger.objects.get(pk=booking.ledger_entry_id).created_by_slack_id,
            self.slack_user.slack_id,
        )
        self.assertEqual(
            PointsAccount.objects.get(user=self.slack_user).balance,
            16,
        )
        self.assertEqual(
            PointsAccount.objects.get(user=self.founder_user).balance,
            99,
        )

    def test_availability_and_booking_api_use_linked_eligibility(self):
        availability = self.client.get(
            reverse("coworking-availability"),
            {
                "days": 1,
                "date": self.booking_date.isoformat(),
                "slack_user_id": self.slack_user.slack_id,
            },
            HTTP_X_API_KEY="internal-key",
        )
        booking = self.client.post(
            reverse("coworking-book"),
            {
                "slack_user_id": self.slack_user.slack_id,
                "date": self.booking_date.isoformat(),
            },
            format="json",
            HTTP_X_API_KEY="internal-key",
        )

        self.assertEqual(availability.status_code, status.HTTP_200_OK)
        self.assertEqual(availability.data[0]["cost_points"], 4)
        self.assertEqual(booking.status_code, status.HTTP_201_CREATED)
        self.assertEqual(booking.data["points_cost"], 4)
        self.assertTrue(booking.data["monthly_update_discount_applied"])
        self.assertEqual(
            booking.data["founder_tools_connection_type"],
            "explicit",
        )
        self.assertTrue(booking.data["founder_tools_account_linked"])
        self.assertTrue(booking.data["founder_tools_explicitly_linked"])

    def test_linking_adds_founder_eligibility_without_removing_slack_user_eligibility(self):
        from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
        from organizations.models import Organization
        from roo.services import CoworkingService
        from startup_updates.models import (
            MonthlyUpdateDraft,
            MonthlyUpdateDraftStatus,
            UserStartupBinding,
        )

        # Remove the linked founder's eligibility so only the Slack-side
        # account can qualify for the discount.
        UserStartupBinding.objects.filter(user=self.founder_user).update(
            coworking_discount_eligible=False
        )
        organization = Organization.objects.create(
            name="Slack Account Founder Pty Ltd",
            domain="slack-account-founder.example",
        )
        profile = VibeRaisingProfile.objects.create(
            user=self.slack_user,
            role=VibeRaisingProfile.ROLE_FOUNDER,
        )
        VibeRaisingCompany.objects.create(
            profile=profile,
            organization=organization,
            name="Slack Account Founder Pty Ltd",
            registered=True,
            abn="89000000020",
            acn="000000020",
            abr_verified_at=timezone.now(),
        )
        UserStartupBinding.objects.create(
            user=self.slack_user,
            organization=organization,
            coworking_discount_eligible=True,
        )
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            month=self.booking_date.replace(day=1),
            status=MonthlyUpdateDraftStatus.READY,
        )

        self.assertEqual(
            CoworkingService.get_coworking_cost(
                user=self.slack_user,
                booking_date=self.booking_date,
            ),
            4,
        )

    def test_existing_booking_is_not_repriced_after_linking(self):
        from roo.models import CoworkingBooking, Ledger, PointsAccount

        SlackFounderAccountLink.objects.all().delete()
        ledger = Ledger.objects.create(
            user=self.slack_user,
            delta=-8,
            kind="SPEND",
            source="COWORKING",
            created_by_slack_id=self.slack_user.slack_id,
            idempotency_key=f"legacy-booking-{self.slack_user.pk}",
        )
        existing = CoworkingBooking.objects.create(
            user=self.slack_user,
            date=self.booking_date,
            points_cost=8,
            ledger_entry=ledger,
        )
        SlackFounderAccountLink.objects.create(
            slack_user=self.slack_user,
            founder_user=self.founder_user,
        )
        balance_before = PointsAccount.objects.get(user=self.slack_user).balance

        response = self.client.post(
            reverse("coworking-book"),
            {
                "slack_user_id": self.slack_user.slack_id,
                "date": self.booking_date.isoformat(),
            },
            format="json",
            HTTP_X_API_KEY="internal-key",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["already_booked"])
        self.assertEqual(response.data["points_cost"], 8)
        self.assertEqual(response.data["founder_tools_connection_type"], "explicit")
        self.assertTrue(response.data["founder_tools_explicitly_linked"])
        existing.refresh_from_db()
        self.assertEqual(existing.points_cost, 8)
        self.assertEqual(
            PointsAccount.objects.get(user=self.slack_user).balance,
            balance_before,
        )

    def test_admin_batch_booking_uses_targets_linked_eligibility(self):
        from roo.models import PointsAccount

        response = self.client.post(
            reverse("coworking-book-many"),
            {
                "admin_slack_user_id": self.admin_user.slack_id,
                "target_slack_user_ids": [self.slack_user.slack_id],
                "date": self.booking_date.isoformat(),
            },
            format="json",
            HTTP_X_API_KEY="roo-link-key",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        result = response.data["results"][0]
        self.assertEqual(result["points_cost"], 4)
        self.assertTrue(result["monthly_update_discount_applied"])
        self.assertEqual(result["founder_tools_connection_type"], "explicit")
        self.assertTrue(result["founder_tools_explicitly_linked"])
        self.assertEqual(
            PointsAccount.objects.get(user=self.slack_user).balance,
            16,
        )
        self.assertEqual(
            PointsAccount.objects.get(user=self.founder_user).balance,
            99,
        )

    def test_direct_same_account_response_is_not_misreported_as_explicit_link(self):
        SlackFounderAccountLink.objects.all().delete()

        response = self.client.post(
            reverse("coworking-book"),
            {
                "slack_user_id": self.slack_user.slack_id,
                "date": self.booking_date.isoformat(),
            },
            format="json",
            HTTP_X_API_KEY="internal-key",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["founder_tools_connection_type"], "direct")
        self.assertTrue(response.data["founder_tools_account_linked"])
        self.assertFalse(response.data["founder_tools_explicitly_linked"])

    def test_linked_update_cannot_discount_a_second_slack_identity(self):
        from core.slack_users import ensure_slack_user
        from roo.services import CoworkingService

        second_registration = ensure_slack_user(
            slack_id="USECOND123",
            email=self.founder_user.email,
            first_name="Second",
        )

        self.assertNotEqual(second_registration.user, self.founder_user)
        self.assertTrue(
            second_registration.user.email.endswith("@slack.placeholder.com")
        )
        self.assertEqual(
            CoworkingService.get_coworking_cost(
                user=second_registration.user,
                booking_date=self.booking_date,
            ),
            8,
        )


@override_settings(ROO_FOUNDER_LINK_TTL_SECONDS=1800)
class SlackFounderLinkConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_completion_creates_one_link_and_rejects_replay(self):
        slack_user = User.objects.create_user(
            email="concurrent-slack@example.com",
            slack_id="UCONCUR123",
        )
        founder_user = User.objects.create_user(
            email="concurrent-founder@example.com",
        )
        _, token = create_slack_founder_link_request(slack_user)
        barrier = Barrier(2)

        def complete():
            close_old_connections()
            barrier.wait()
            try:
                completion = complete_slack_founder_link(
                    token,
                    founder_user=founder_user,
                )
                return "created" if completion.created else "existing"
            except UsedSlackFounderLinkError:
                return "used"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _: complete(), range(2)))

        self.assertCountEqual(outcomes, ["created", "used"])
        self.assertEqual(SlackFounderAccountLink.objects.count(), 1)

    @skipUnlessDBFeature("has_select_for_update")
    def test_swapped_role_completions_lock_users_in_the_same_order(self):
        first_user = User.objects.create_user(
            email="lock-order-first@example.com",
            slack_id="ULOCKFIRST",
        )
        second_user = User.objects.create_user(
            email="lock-order-second@example.com",
            slack_id="ULOCKSECOND",
        )
        _, first_token = create_slack_founder_link_request(first_user)
        _, second_token = create_slack_founder_link_request(second_user)
        barrier = Barrier(2)

        def complete(token, founder_user):
            close_old_connections()
            barrier.wait()
            try:
                complete_slack_founder_link(
                    token,
                    founder_user=founder_user,
                )
                return "completed"
            except ConflictingSlackFounderLinkError:
                return "conflict"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(complete, first_token, second_user),
                executor.submit(complete, second_token, first_user),
            ]
            outcomes = [future.result(timeout=5) for future in futures]

        self.assertEqual(outcomes, ["conflict", "conflict"])
        self.assertFalse(SlackFounderAccountLink.objects.exists())

    @skipUnlessDBFeature("has_select_for_update")
    def test_start_and_completion_share_a_deadlock_free_lock_order(self):
        slack_user = User.objects.create_user(
            email="start-race-slack@example.com",
            slack_id="USTARTRACE",
        )
        founder_user = User.objects.create_user(
            email="start-race-founder@example.com",
        )
        _, token = create_slack_founder_link_request(slack_user)
        barrier = Barrier(2)

        def start():
            close_old_connections()
            barrier.wait()
            try:
                return f"start:{start_slack_founder_link(slack_user).status}"
            finally:
                close_old_connections()

        def complete():
            close_old_connections()
            barrier.wait()
            try:
                result = complete_slack_founder_link(
                    token,
                    founder_user=founder_user,
                )
                return f"complete:{result.status}"
            except SlackFounderLinkError as exc:
                return f"complete:{exc.code}"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(start), executor.submit(complete)]
            outcomes = [future.result(timeout=5) for future in futures]

        self.assertIn(
            outcomes,
            [
                ["start:already_linked", "complete:linked"],
                ["start:link_required", "complete:invalid_token"],
            ],
        )
