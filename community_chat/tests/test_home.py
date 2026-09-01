import hashlib
import uuid
from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from community_chat.account_sessions import issue_account_session
from community_chat.authentication import USAGE_TOKEN_PREFIX
from community_chat.models import (
    CommunityChatEmailCodeChallenge,
    TokenUsageAccount,
)
from roo.models import PointsAccount, RewardsCatalog, Task, TaskAssignment


class CommunityHomeTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="home-member@example.com",
            first_name="Home",
            last_name="Member",
            slack_id="UHOME",
        )
        challenge = CommunityChatEmailCodeChallenge.objects.create(
            user=self.user,
            email_digest="a" * 64,
            code_digest="b" * 64,
            client_id="mlai-chat-web",
            installation_id=uuid.uuid4(),
            origin="https://chat.mlai.au",
            platform="web",
            device_name="Chrome",
            public_key="c" * 64,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        credentials = issue_account_session(self.user, challenge)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {credentials.access_token}"
        )
        self.url = reverse("community_chat_home")

        PointsAccount.objects.create(
            user=self.user,
            balance=17,
            earned_balance=17,
            lifetime_earned=25,
            balance_microroo=17_000_000,
            earned_balance_microroo=17_000_000,
            lifetime_earned_microroo=25_000_000,
            microroo_initialized=True,
        )

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
    def test_returns_own_balance_claimable_work_and_in_stock_rewards(self):
        visible = self.task("Help at an event", source_url="https://example.com/help")
        self.task("Internal planning", visibility="internal")
        self.task("Not published", volunteer_ready=False)
        claimed = self.task("Already claimed")
        TaskAssignment.objects.create(
            task=claimed,
            assigned_user=self.user,
            assigned_to_slack_id="UHOME",
            status="claimed",
            claimed_at=timezone.now(),
        )
        self.task("Past deadline", due_date=timezone.localdate() - timedelta(days=1))

        RewardsCatalog.objects.create(
            code="DAY_PASS",
            name="Coworking day pass",
            cost_points=4,
            stock_remaining=3,
        )
        RewardsCatalog.objects.create(
            code="UNLIMITED",
            name="Unlimited reward",
            cost_points=20,
            stock_remaining=None,
        )
        RewardsCatalog.objects.create(
            code="SOLD_OUT",
            name="Sold out",
            cost_points=1,
            stock_remaining=0,
        )
        RewardsCatalog.objects.create(
            code="INACTIVE",
            name="Inactive",
            cost_points=1,
            is_active=False,
            stock_remaining=10,
        )

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            set(response.data),
            {"points", "earn_actions", "rewards", "feature_flags"},
        )
        self.assertEqual(
            response.data["points"],
            {
                "balance": 17,
                "earned_balance": 17,
                "purchased_topup_balance": 0,
                "lifetime_earned": 25,
                "lifetime_spent": 0,
            },
        )
        self.assertEqual(
            [action["id"] for action in response.data["earn_actions"]],
            ["intro", "monthly_update", f"task:{visible.task_code}"],
        )
        self.assertEqual(response.data["earn_actions"][0]["points"], 4)
        self.assertEqual(response.data["earn_actions"][1]["points"], 10)
        self.assertEqual(
            response.data["earn_actions"][1]["description"],
            "Complete and save a ready monthly update for your verified company.",
        )
        self.assertEqual(
            response.data["earn_actions"][2]["command"],
            f"@Roo task claim {visible.task_code}",
        )
        rewards = {
            reward["code"]: reward for reward in response.data["rewards"]
        }
        self.assertIn("DAY_PASS", rewards)
        self.assertIn("UNLIMITED", rewards)
        self.assertNotIn("SOLD_OUT", rewards)
        self.assertNotIn("INACTIVE", rewards)
        self.assertTrue(rewards["DAY_PASS"]["can_afford"])
        self.assertFalse(rewards["UNLIMITED"]["can_afford"])
        self.assertEqual(
            response.data["feature_flags"],
            {"link_love": False, "meeting_rooms": True},
        )

    def test_response_does_not_expose_other_members_or_private_roo_fields(self):
        rival = get_user_model().objects.create_user(
            email="rival-secret@example.com",
            slack_id="URIVALSECRET",
        )
        PointsAccount.objects.create(
            user=rival,
            balance=999,
            balance_microroo=999_000_000,
            microroo_initialized=True,
        )
        self.task(
            "Public task",
            reviewer_slack_id="UREVIEWERSECRET",
            assigned_to_user_id="UASSIGNEESECRET",
            metadata={"private": "do-not-return"},
        )

        response = self.client.get(self.url)
        rendered = str(response.data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("rival-secret@example.com", rendered)
        self.assertNotIn("URIVALSECRET", rendered)
        self.assertNotIn("UREVIEWERSECRET", rendered)
        self.assertNotIn("UASSIGNEESECRET", rendered)
        self.assertNotIn("do-not-return", rendered)
        self.assertEqual(response.data["points"]["balance"], 17)

    @override_settings(ROO_POINTS_MONTHLY_UPDATE_REWARD=0)
    def test_disabled_monthly_update_reward_is_not_advertised(self):
        response = self.client.get(self.url)

        self.assertEqual(
            [action["id"] for action in response.data["earn_actions"]],
            ["intro"],
        )

    @override_settings(
        TOKEN_USAGE_TIME_ZONE="Australia/Melbourne",
        ROO_POINTS_MONTHLY_UPDATE_REWARD=0,
    )
    def test_task_expiry_uses_the_configured_melbourne_calendar_day(self):
        # 00:30 on 2 January in Melbourne is still 1 January in UTC. A task
        # that expired on the Melbourne 1st must no longer be advertised.
        report_time = datetime(
            2026,
            1,
            1,
            13,
            30,
            tzinfo=datetime_timezone.utc,
        )
        expired = self.task("Expired in Melbourne", due_date=report_time.date())
        available = self.task(
            "Available in Melbourne",
            due_date=report_time.date() + timedelta(days=1),
        )

        with patch(
            "community_chat.token_usage.timezone.now",
            return_value=report_time,
        ):
            response = self.client.get(self.url)

        action_ids = {
            action["id"] for action in response.data["earn_actions"]
        }
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn(f"task:{expired.task_code}", action_ids)
        self.assertIn(f"task:{available.task_code}", action_ids)

    def test_requires_a_member_session_and_rejects_reporter_credentials(self):
        anonymous = APIClient().get(self.url)
        raw_token = USAGE_TOKEN_PREFIX + "reporter-only-token"
        TokenUsageAccount.objects.create(
            user=self.user,
            token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
        )
        reporter = APIClient().get(
            self.url,
            HTTP_AUTHORIZATION=f"Bearer {raw_token}",
        )

        self.assertEqual(anonymous.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(reporter.status_code, status.HTTP_401_UNAUTHORIZED)
