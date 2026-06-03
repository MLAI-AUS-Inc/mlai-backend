from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
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
        self.teammate = User.objects.create_user(email="watt-teammate@example.com")
        self.team = GenericHackathonTeam.objects.create(
            hackathon=self.hackathon,
            team_name="Grid Builders",
        )
        # A team needs 2..6 members to enter the game (see _team_size_gate).
        self.team.members.add(self.user, self.teammate)
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

    def test_current_blocked_when_team_too_small(self):
        self.team.members.remove(self.teammate)  # back down to a single member
        response = self.client.post("/api/v1/hackathons/watt/unity-sessions/current/", {}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["member_count"], 1)
        self.assertEqual(response.data["min_members"], 2)

    def test_participant_token_blocked_when_team_too_small(self):
        self.team.members.remove(self.teammate)  # back down to a single member
        response = self.client.post("/api/v1/hackathons/watt/firebase-token/", {}, format="json")
        self.assertEqual(response.status_code, 403)


class _StubMembers:
    def __init__(self, count):
        self._count = count

    def count(self):
        return self._count


class _StubTeam:
    def __init__(self, count):
        self.members = _StubMembers(count)


class TeamSizeGateTests(SimpleTestCase):
    """Pure (no-DB) coverage of the 2..6 team-size gate helper."""

    def test_too_small_is_blocked(self):
        from generic_hackathons.watt_views import _team_size_gate

        gate = _team_size_gate(_StubTeam(1))
        self.assertIsNotNone(gate)
        self.assertEqual(gate.status_code, 403)
        self.assertEqual(gate.data["member_count"], 1)

    def test_valid_sizes_pass(self):
        from generic_hackathons.watt_views import _team_size_gate

        for count in (2, 3, 6):
            self.assertIsNone(_team_size_gate(_StubTeam(count)))

    def test_too_large_is_blocked(self):
        from generic_hackathons.watt_views import _team_size_gate

        gate = _team_size_gate(_StubTeam(7))
        self.assertIsNotNone(gate)
        self.assertEqual(gate.status_code, 403)


class CurrentTeamResolutionTests(TestCase):
    """A user on >1 Watt team must always resolve to the SAME household."""

    def test_multi_team_user_resolves_to_earliest_team_and_is_stable(self):
        from generic_hackathons.watt_views import _current_team

        hackathon = Hackathon.objects.create(
            slug="watt",
            name="Watt The Hack",
            description="Energy hackathon",
            start_date="2026-06-01",
            end_date="2026-12-31",
        )
        user = User.objects.create_user(email="multi-team@example.com")
        first_team = GenericHackathonTeam.objects.create(hackathon=hackathon, team_name="Alpha")
        second_team = GenericHackathonTeam.objects.create(hackathon=hackathon, team_name="Bravo")
        # Add in reverse creation order to prove resolution is by pk, not insert order.
        second_team.members.add(user)
        first_team.members.add(user)
        self.assertLess(first_team.pk, second_team.pk)

        # Earliest (lowest-pk) team wins, and the answer is identical on repeated calls.
        resolved = {_current_team(user, hackathon).pk for _ in range(3)}
        self.assertEqual(resolved, {first_team.pk})


@override_settings(WATT_ALLOW_DEV_TOKEN=True)
class WattUnityDevTokenTests(SimpleTestCase):
    def setUp(self):
        self.client = APIClient()

    @patch("generic_hackathons.watt_views.create_firebase_custom_token", return_value="dev-unity-token")
    def test_dev_token_mints_watt_unity_claims(self, mint_token):
        response = self.client.post(
            "/api/v1/hackathons/watt/unity-sessions/dev-token/",
            {"class_id": "CLASS", "household_id": "household_001"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["firebase_custom_token"], "dev-unity-token")
        self.assertEqual(response.data["class_id"], "CLASS")
        self.assertEqual(response.data["household_id"], "household_001")
        _, claims = mint_token.call_args.args
        self.assertEqual(
            claims,
            {"role": "watt_unity", "class_id": "CLASS", "household_id": "household_001"},
        )

    @patch("generic_hackathons.watt_views.create_firebase_custom_token", return_value="dev-unity-token")
    def test_dev_token_defaults_class_and_household(self, mint_token):
        response = self.client.post(
            "/api/v1/hackathons/watt/unity-sessions/dev-token/", {}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        _, claims = mint_token.call_args.args
        self.assertEqual(
            claims,
            {"role": "watt_unity", "class_id": "CLASS", "household_id": "household_001"},
        )


@override_settings(WATT_ALLOW_DEV_TOKEN=False)
class WattUnityDevTokenDisabledTests(SimpleTestCase):
    def test_disabled_returns_403(self):
        client = APIClient()
        response = client.post(
            "/api/v1/hackathons/watt/unity-sessions/dev-token/", {}, format="json"
        )
        self.assertEqual(response.status_code, 403)
