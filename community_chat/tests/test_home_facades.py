from datetime import timedelta
import uuid
from unittest.mock import Mock, patch

from coincurve import PrivateKey
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from community_chat.account_sessions import issue_account_session
from community_chat.models import CommunityChatEmailCodeChallenge
from integrations.services.luma import LumaAPIError, LumaConfigurationError
from roo.models import PointsAccount, RewardsCatalog, Task, TaskAssignment


def _public_key(private_int):
    return PrivateKey.from_int(private_int).public_key.format(compressed=True)[1:].hex()


@override_settings(
    COMMUNITY_CHAT_ALLOWED_ORIGINS=["https://chat.mlai.au"],
    COMMUNITY_CHAT_SESSION_ACCESS_TTL_SECONDS=900,
    COMMUNITY_CHAT_SESSION_REFRESH_TTL_DAYS=30,
    LUMA_API_KEY="configured-for-test",
    LUMA_CALENDAR_URL="https://luma.com/mlai_au",
    LUMA_API_TIMEOUT_SECONDS=2,
    LUMA_UPCOMING_EVENTS_CACHE_SECONDS=300,
)
class CommunityChatHomeFacadeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(
            email="home-facade@example.com",
            first_name="Home",
            last_name="Member",
            slack_id="UHOMEFACADE",
        )
        challenge = CommunityChatEmailCodeChallenge.objects.create(
            user=self.user,
            email_digest="a" * 64,
            code_digest="b" * 64,
            client_id="mlai-chat-desktop",
            installation_id=uuid.uuid4(),
            origin="https://chat.mlai.au",
            platform="macos",
            device_name="Mac",
            public_key=_public_key(81),
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        credentials = issue_account_session(self.user, challenge)
        self.client = APIClient()
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {credentials.access_token}"
        )

    def tearDown(self):
        cache.clear()

    def task(self, title, **overrides):
        values = {
            "title": title,
            "description": f"Description for {title}",
            "created_by_user_id": "UADMIN",
            "status": "open",
            "visibility": "volunteer",
            "volunteer_ready": True,
            "points": 4,
            "points_estimate": 4,
            "points_min": 3,
            "points_max": 5,
        }
        values.update(overrides)
        return Task.objects.create(**values)

    @override_settings(
        ROO_POINTS_MONTHLY_UPDATE_REWARD=10,
        MEETING_ROOM_BOOKING_ENABLED=True,
    )
    def test_home_returns_only_the_authenticated_users_roo_data(self):
        PointsAccount.objects.create(
            user=self.user,
            balance=17,
            earned_balance=12,
            purchased_topup_balance=5,
            lifetime_earned=30,
            lifetime_purchased_topup=10,
            lifetime_spent=13,
            expired_or_reversed_points=2,
        )
        other_user = get_user_model().objects.create_user(
            email="other-home@example.com",
            slack_id="UOTHERHOME",
        )
        PointsAccount.objects.create(user=other_user, balance=999)
        visible_task = self.task("Help at an event")
        self.task("Internal planning", visibility="internal")
        claimed_task = self.task("Already claimed")
        TaskAssignment.objects.create(
            task=claimed_task,
            assigned_user=self.user,
            assigned_to_slack_id=self.user.slack_id,
            status="claimed",
            claimed_at=timezone.now(),
        )
        RewardsCatalog.objects.update_or_create(
            code="HOME_VISIBLE",
            defaults={
                "name": "Visible reward",
                "description": "Available to members",
                "cost_points": 10,
                "fulfillment": "manual",
                "is_active": True,
                "stock_remaining": 3,
            },
        )
        RewardsCatalog.objects.update_or_create(
            code="HOME_SOLD_OUT",
            defaults={
                "name": "Sold out reward",
                "cost_points": 1,
                "fulfillment": "manual",
                "is_active": True,
                "stock_remaining": 0,
            },
        )
        RewardsCatalog.objects.update_or_create(
            code="HOME_HIDDEN",
            defaults={
                "name": "Hidden reward",
                "cost_points": 1,
                "fulfillment": "manual",
                "is_active": False,
            },
        )

        response = self.client.get(
            reverse("community_chat_home"),
            {"slack_user_id": other_user.slack_id},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            set(response.data),
            {"points", "earn_actions", "rewards", "feature_flags"},
        )
        self.assertEqual(
            response.data["points"],
            {
                "balance": 17,
                "earned_balance": 12,
                "purchased_topup_balance": 5,
                "lifetime_earned": 30,
                "lifetime_spent": 13,
            },
        )
        action_ids = [action["id"] for action in response.data["earn_actions"]]
        self.assertEqual(
            action_ids,
            ["intro", "monthly_update", f"task:{visible_task.task_code}"],
        )
        task_action = response.data["earn_actions"][2]
        self.assertEqual(task_action["points"], 4)
        self.assertEqual(
            task_action["command"],
            f"@Roo task claim {visible_task.task_code}",
        )
        rewards = {
            reward["code"]: reward for reward in response.data["rewards"]
        }
        self.assertNotIn("HOME_HIDDEN", rewards)
        self.assertNotIn("HOME_SOLD_OUT", rewards)
        self.assertEqual(
            rewards["HOME_VISIBLE"],
            {
                "code": "HOME_VISIBLE",
                "name": "Visible reward",
                "description": "Available to members",
                "cost_points": 10,
                "stock_remaining": 3,
                "can_afford": True,
            },
        )
        self.assertEqual(
            response.data["feature_flags"],
            {"link_love": False, "meeting_rooms": True},
        )
        self.assertNotIn("email", response.data["points"])
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_facades_require_a_chat_account_session(self):
        anonymous = APIClient()

        home_response = anonymous.get(reverse("community_chat_home"))
        events_response = anonymous.get(reverse("community_chat_upcoming_events"))

        self.assertEqual(home_response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(events_response.status_code, status.HTTP_401_UNAUTHORIZED)

    @override_settings(ROO_POINTS_MONTHLY_UPDATE_REWARD=0)
    def test_home_does_not_advertise_a_disabled_monthly_update_reward(self):
        response = self.client.get(reverse("community_chat_home"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [action["id"] for action in response.data["earn_actions"]],
            ["intro"],
        )

    def test_upcoming_events_are_cached_and_defensively_allowlisted(self):
        service = Mock()
        service.list_upcoming_events.return_value = [
            {
                "id": "evt-1",
                "name": "Founder Coffee",
                "url": "https://lu.ma/founder-coffee",
                "start_at": "2026-09-01T23:00:00Z",
                "end_at": "2026-09-02T00:00:00Z",
                "timezone": "Australia/Melbourne",
                "visibility": "public",
                "meeting_url": "https://meet.example/secret",
                "registration_questions": [{"label": "Private"}],
            },
            {
                "id": "evt-2",
                "name": "Office Hours",
                "url": "https://lu.ma/office-hours",
                "start_at": "2026-09-03T02:00:00Z",
                "end_at": "2026-09-03T03:00:00Z",
                "timezone": "Australia/Melbourne",
                "geo_address_json": {"address": "Private address"},
            },
        ]

        with patch(
            "community_chat.views.LumaAttendeeReportService",
            return_value=service,
        ) as service_class:
            first = self.client.get(
                reverse("community_chat_upcoming_events"),
                {"limit": 1},
            )
            second = self.client.get(reverse("community_chat_upcoming_events"))

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(first.data["calendar_url"], "https://luma.com/mlai_au")
        self.assertEqual(len(first.data["events"]), 1)
        self.assertEqual(len(second.data["events"]), 2)
        self.assertEqual(
            set(second.data["events"][0]),
            {"id", "name", "url", "start_at", "end_at", "timezone"},
        )
        self.assertEqual(second["Cache-Control"], "private, max-age=60")
        service_class.assert_called_once_with(timeout=2)
        service.list_upcoming_events.assert_called_once_with(limit=10)

    def test_upcoming_events_rejects_invalid_limits_without_calling_luma(self):
        with patch("community_chat.views.LumaAttendeeReportService") as service_class:
            response = self.client.get(
                reverse("community_chat_upcoming_events"),
                {"limit": "not-a-number"},
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {"error": "invalid_limit"})
        service_class.assert_not_called()

    def test_upcoming_events_uses_generic_configuration_failure(self):
        service = Mock()
        service.list_upcoming_events.side_effect = LumaConfigurationError(
            "LUMA_API_KEY contains internal configuration detail"
        )

        with patch(
            "community_chat.views.LumaAttendeeReportService",
            return_value=service,
        ):
            response = self.client.get(reverse("community_chat_upcoming_events"))

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data, {"error": "upcoming_events_unavailable"})

    def test_upcoming_events_maps_upstream_errors_without_exposing_details(self):
        service = Mock()
        service.list_upcoming_events.side_effect = LumaAPIError(
            "sensitive upstream detail",
            status_code=401,
        )

        with patch(
            "community_chat.views.LumaAttendeeReportService",
            return_value=service,
        ):
            response = self.client.get(reverse("community_chat_upcoming_events"))

        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(response.data, {"error": "upcoming_events_unavailable"})

    def test_upcoming_events_preserves_upstream_rate_limit_status(self):
        service = Mock()
        service.list_upcoming_events.side_effect = LumaAPIError(
            "sensitive upstream detail",
            status_code=429,
        )

        with patch(
            "community_chat.views.LumaAttendeeReportService",
            return_value=service,
        ):
            response = self.client.get(reverse("community_chat_upcoming_events"))

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(response.data, {"error": "upcoming_events_unavailable"})
