from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models import Hackathon
from .models import GenericHackathonTeam


User = get_user_model()


@override_settings(
    WATT_HACKATHON_CLASS_ID="CLASS",
    WATT_HACKATHON_API_BASE_URL="https://api.example.test",
    WATT_UNITY_SESSION_TICKET_TTL_SECONDS=300,
    VAGON_STREAM_ID="stream_123",
)
class WattUnitySessionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.hackathon = Hackathon.objects.create(
            slug="watt",
            name="Watt The Hack",
            description="Energy hackathon",
            start_date="2026-06-01",
            end_date="2026-12-31",
        )
        self.user = User.objects.create_user(email="watt@example.com")
        self.team = GenericHackathonTeam.objects.create(
            hackathon=self.hackathon,
            team_name="Grid Builders",
        )
        self.team.members.add(self.user)
        self.client.force_authenticate(self.user)

    @patch("generic_hackathons.watt_views.create_firebase_custom_token", return_value="unity-token")
    def test_current_returns_vagon_url_with_single_use_ticket(self, mint_token):
        response = self.client.post("/api/v1/hackathons/watt/unity-sessions/current/", {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["household_id"], "TEAM1")
        parsed = urlsplit(response.data["stream_url"])
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "app.vagon.io")
        self.assertEqual(parsed.path, "/streams/stream_123")

        query = parse_qs(parsed.query)
        self.assertEqual(query["newSession"], ["true"])
        launch_flags = query["launchFlags"][0]
        self.assertIn("--household-id TEAM1", launch_flags)
        self.assertIn("--backend-url https://api.example.test", launch_flags)
        ticket = launch_flags.split("--session-ticket ", 1)[1].split(" ", 1)[0]

        _, claims = mint_token.call_args.args
        self.assertEqual(claims, {"role": "watt_unity", "class_id": "CLASS", "household_id": "TEAM1"})

        redeem = self.client.post(
            "/api/v1/hackathons/watt/unity-sessions/redeem-ticket/",
            {"ticket": ticket, "household_id": "TEAM1"},
            format="json",
        )
        self.assertEqual(redeem.status_code, 200)
        self.assertEqual(redeem.data["firebase_custom_token"], "unity-token")
        self.assertEqual(redeem.data["class_id"], "CLASS")
        self.assertEqual(redeem.data["household_id"], "TEAM1")

        second_redeem = self.client.post(
            "/api/v1/hackathons/watt/unity-sessions/redeem-ticket/",
            {"ticket": ticket, "household_id": "TEAM1"},
            format="json",
        )
        self.assertEqual(second_redeem.status_code, 409)

    @patch("generic_hackathons.watt_views.create_firebase_custom_token", return_value="participant-token")
    def test_participant_token_is_household_scoped(self, mint_token):
        response = self.client.post("/api/v1/hackathons/watt/firebase-token/", {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["firebase_custom_token"], "participant-token")
        _, claims = mint_token.call_args.args
        self.assertEqual(
            claims,
            {"role": "watt_participant", "class_id": "CLASS", "household_id": "TEAM1"},
        )
