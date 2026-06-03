"""Tests for the Approval Gate: admin action + serializer member-gating.

Two surfaces under test:

1. `approve_teams_for_eval` — the bulk admin action that calls the FastAPI
   eval gateway. Validates payload, response capture, idempotency, error
   isolation between teams, and 409 handling. `requests.post` is fully
   mocked; no network calls leak.

2. `GenericHackathonTeamSerializer.get_eval_*` — the member-only gating on
   the credential pair. Confirms a non-member never sees either half via
   the serializer, even if they hit a team-list endpoint.
"""

from unittest.mock import Mock, patch
from uuid import UUID

import requests
from django.contrib import messages
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory, TestCase
from django.utils import timezone

from core.models import Hackathon, User

from .admin import GenericHackathonTeamAdmin
from .models import GenericHackathonTeam
from .serializers import GenericHackathonTeamSerializer


class _TeamProvisioningBase(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.site = AdminSite()
        self.admin = GenericHackathonTeamAdmin(GenericHackathonTeam, self.site)

        self.user = User.objects.create(email="test@example.com")
        now = timezone.now()
        self.hackathon, _ = Hackathon.objects.get_or_create(
            slug="watt-the-hack",
            defaults={
                "name": "Watt The Hack",
                "start_date": now,
                "end_date": now + timezone.timedelta(days=1),
            },
        )
        self.team1 = GenericHackathonTeam.objects.create(
            team_name="Team Alpha", hackathon=self.hackathon,
        )
        self.team2 = GenericHackathonTeam.objects.create(
            team_name="Team Beta", hackathon=self.hackathon,
        )

    def _request(self):
        request = self.factory.post("/")
        request.user = self.user
        return request


class AdminActionTests(_TeamProvisioningBase):
    @patch("generic_hackathons.admin.os.environ.get")
    @patch("generic_hackathons.admin.requests.post")
    def test_successful_provisioning_captures_both_id_and_token(self, mock_post, mock_env):
        """Happy path — both fields populated atomically from the response."""
        mock_env.return_value = "fake-admin-token"
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.ok = True
        mock_response.json.return_value = {
            "id": "123e4567-e89b-12d3-a456-426614174000",
            "name": "Team Alpha",
            "token": "fake-eval-token",
            "message": "Team created.",
        }
        mock_post.return_value = mock_response

        with patch.object(self.admin, "message_user") as mock_msg:
            self.admin.approve_teams_for_eval(
                self._request(),
                GenericHackathonTeam.objects.filter(id=self.team1.id),
            )

        self.team1.refresh_from_db()
        self.assertEqual(self.team1.eval_token, "fake-eval-token")
        self.assertEqual(
            str(self.team1.eval_team_uuid), "123e4567-e89b-12d3-a456-426614174000",
        )
        # Exactly one success summary, no warnings/errors.
        last = mock_msg.call_args_list[-1]
        self.assertIn("provisioned 1", last.args[1])
        self.assertEqual(last.kwargs.get("level"), messages.SUCCESS)

    @patch("generic_hackathons.admin.os.environ.get")
    @patch("generic_hackathons.admin.requests.post")
    def test_payload_is_name_only_not_team_name_or_team_id(self, mock_post, mock_env):
        """Regression for Bug #1: gateway expects {name, email}, not {team_name, team_id}."""
        mock_env.return_value = "fake-admin-token"
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.ok = True
        mock_response.json.return_value = {
            "id": "123e4567-e89b-12d3-a456-426614174000",
            "name": "Team Alpha",
            "token": "fake-eval-token",
            "message": "Team created.",
        }
        mock_post.return_value = mock_response

        with patch.object(self.admin, "message_user"):
            self.admin.approve_teams_for_eval(
                self._request(),
                GenericHackathonTeam.objects.filter(id=self.team1.id),
            )

        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload, {"name": "Team Alpha", "email": None})
        self.assertNotIn("team_name", payload)
        self.assertNotIn("team_id", payload)

    @patch("generic_hackathons.admin.os.environ.get")
    @patch("generic_hackathons.admin.requests.post")
    def test_conflict_409_surfaces_warning_and_does_not_save(self, mock_post, mock_env):
        mock_env.return_value = "fake-admin-token"
        mock_response = Mock()
        mock_response.status_code = 409
        mock_response.ok = False
        mock_post.return_value = mock_response

        with patch.object(self.admin, "message_user") as mock_msg:
            self.admin.approve_teams_for_eval(
                self._request(),
                GenericHackathonTeam.objects.filter(id=self.team1.id),
            )

        self.team1.refresh_from_db()
        self.assertIsNone(self.team1.eval_token)
        self.assertIsNone(self.team1.eval_team_uuid)
        # 409 message + summary "failed 1" — both should appear.
        levels = [c.kwargs.get("level") for c in mock_msg.call_args_list]
        self.assertIn(messages.WARNING, levels)

    @patch("generic_hackathons.admin.os.environ.get")
    @patch("generic_hackathons.admin.requests.post")
    def test_already_provisioned_team_is_skipped_without_api_call(self, mock_post, mock_env):
        """Idempotency: a team with both halves populated skips the request entirely."""
        mock_env.return_value = "fake-admin-token"
        self.team1.eval_token = "existing-token"
        self.team1.eval_team_uuid = UUID("11111111-1111-1111-1111-111111111111")
        self.team1.save()

        with patch.object(self.admin, "message_user") as mock_msg:
            self.admin.approve_teams_for_eval(
                self._request(),
                GenericHackathonTeam.objects.filter(id=self.team1.id),
            )

        mock_post.assert_not_called()
        # Existing values untouched.
        self.team1.refresh_from_db()
        self.assertEqual(self.team1.eval_token, "existing-token")
        # Summary mentions the skip count.
        last = mock_msg.call_args_list[-1]
        self.assertIn("skipped 1", last.args[1])

    @patch("generic_hackathons.admin.os.environ.get")
    @patch("generic_hackathons.admin.requests.post")
    def test_network_error_on_one_team_does_not_block_others(self, mock_post, mock_env):
        """A timeout on team1 must not prevent team2 from being provisioned."""
        mock_env.return_value = "fake-admin-token"

        ok_response = Mock()
        ok_response.status_code = 201
        ok_response.ok = True
        ok_response.json.return_value = {
            "id": "22222222-2222-2222-2222-222222222222",
            "name": "Team Beta",
            "token": "beta-token",
            "message": "Team created.",
        }
        # First call (team1) raises a timeout; second (team2) succeeds.
        mock_post.side_effect = [requests.Timeout("read timeout"), ok_response]

        with patch.object(self.admin, "message_user"):
            self.admin.approve_teams_for_eval(
                self._request(),
                GenericHackathonTeam.objects.filter(
                    id__in=[self.team1.id, self.team2.id]
                ).order_by("id"),
            )

        self.team1.refresh_from_db()
        self.team2.refresh_from_db()
        self.assertIsNone(self.team1.eval_token)
        self.assertEqual(self.team2.eval_token, "beta-token")

    @patch("generic_hackathons.admin.os.environ.get")
    @patch("generic_hackathons.admin.requests.post")
    def test_missing_admin_token_aborts_before_api_call(self, mock_post, mock_env):
        mock_env.return_value = ""

        with patch.object(self.admin, "message_user") as mock_msg:
            self.admin.approve_teams_for_eval(
                self._request(),
                GenericHackathonTeam.objects.filter(id=self.team1.id),
            )

        mock_post.assert_not_called()
        # Single error message — admin can't proceed without the token.
        self.assertEqual(mock_msg.call_args.kwargs.get("level"), messages.ERROR)

    @patch("generic_hackathons.admin.os.environ.get")
    @patch("generic_hackathons.admin.requests.post")
    def test_response_missing_id_does_not_partial_save(self, mock_post, mock_env):
        """Defensive: if the gateway returns token but no id (bug or schema drift),
        do NOT save half the credentials. The team stays unprovisioned so a retry
        can recover."""
        mock_env.return_value = "fake-admin-token"
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.ok = True
        mock_response.json.return_value = {
            "name": "Team Alpha", "token": "only-the-token", "message": "...",
        }  # no `id`
        mock_post.return_value = mock_response

        with patch.object(self.admin, "message_user"):
            self.admin.approve_teams_for_eval(
                self._request(),
                GenericHackathonTeam.objects.filter(id=self.team1.id),
            )

        self.team1.refresh_from_db()
        self.assertIsNone(self.team1.eval_token)
        self.assertIsNone(self.team1.eval_team_uuid)


class SerializerGatingTests(_TeamProvisioningBase):
    """The credential pair must NEVER leak to non-members via the serializer."""

    def setUp(self):
        super().setUp()
        self.team1.members.add(self.user)
        self.team1.eval_token = "secret-token"
        self.team1.eval_team_uuid = UUID("33333333-3333-3333-3333-333333333333")
        self.team1.save()

        self.stranger = User.objects.create(email="stranger@example.com")

    def _serialize(self, requesting_user):
        request = self.factory.get("/")
        request.user = requesting_user
        return GenericHackathonTeamSerializer(
            self.team1, context={"request": request},
        ).data

    def test_member_sees_both_eval_credentials(self):
        data = self._serialize(self.user)
        self.assertEqual(data["eval_token"], "secret-token")
        self.assertEqual(data["eval_team_uuid"], "33333333-3333-3333-3333-333333333333")

    def test_non_member_sees_neither_credential(self):
        """The critical leak case: another logged-in user must not see either half."""
        data = self._serialize(self.stranger)
        self.assertIsNone(data["eval_token"])
        self.assertIsNone(data["eval_team_uuid"])

    def test_anonymous_request_sees_neither_credential(self):
        """Anonymous (unauthenticated) requests also must not see either half."""
        from django.contrib.auth.models import AnonymousUser
        data = self._serialize(AnonymousUser())
        self.assertIsNone(data["eval_token"])
        self.assertIsNone(data["eval_team_uuid"])
