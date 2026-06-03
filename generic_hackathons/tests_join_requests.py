"""Tests for the Watt team join-request (approval) flow."""
import datetime

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from core.models import Hackathon
from .models import GenericHackathonJoinRequest, GenericHackathonTeam


User = get_user_model()
SLUG = "watt-the-hack"
BASE = f"/api/v1/hackathons/{SLUG}/app"


class JoinRequestTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        # slug 'watt-the-hack' is seeded by migration 0001, so update_or_create (not create).
        self.hackathon, _ = Hackathon.objects.update_or_create(
            slug=SLUG,
            defaults={
                "name": "Watt The Hack",
                "description": "Energy hackathon",
                "start_date": "2026-06-01",
                "end_date": "2026-12-31",
            },
        )
        self.alice = User.objects.create_user(email="alice@example.com")  # leader
        self.bob = User.objects.create_user(email="bob@example.com")      # member
        self.carol = User.objects.create_user(email="carol@example.com")  # requester
        self.dave = User.objects.create_user(email="dave@example.com")
        self.team = GenericHackathonTeam.objects.create(
            hackathon=self.hackathon, team_name="Grid Builders", leader=self.alice,
        )
        self.team.members.add(self.alice, self.bob)

    def _auth(self, user):
        self.client.force_authenticate(user)

    def _request_join(self, user, name="Grid Builders"):
        self._auth(user)
        return self.client.post(f"{BASE}/teams/join/", {"code": name}, format="json")

    def test_join_creates_pending_request_not_membership(self):
        resp = self._request_join(self.carol)
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data["pending"])
        self.assertFalse(self.team.members.filter(id=self.carol.id).exists())
        self.assertTrue(GenericHackathonJoinRequest.objects.filter(team=self.team, user=self.carol).exists())

    def test_duplicate_request_is_idempotent(self):
        self._request_join(self.carol)
        resp = self._request_join(self.carol)
        self.assertIn(resp.status_code, (200, 201))
        self.assertEqual(GenericHackathonJoinRequest.objects.filter(team=self.team, user=self.carol).count(), 1)

    def test_request_blocked_if_already_on_a_team(self):
        other = GenericHackathonTeam.objects.create(
            hackathon=self.hackathon, team_name="Solar Squad", leader=self.dave,
        )
        other.members.add(self.dave)
        resp = self._request_join(self.bob, name="Solar Squad")  # bob already on Grid Builders
        self.assertEqual(resp.status_code, 409)

    def test_request_to_full_team_blocked(self):
        extra = [User.objects.create_user(email=f"full{i}@example.com") for i in range(4)]
        self.team.members.add(*extra)  # alice, bob + 4 = 6 (full)
        resp = self._request_join(self.carol)
        self.assertEqual(resp.status_code, 409)

    def test_accept_adds_member_and_clears_requests(self):
        self._request_join(self.carol)
        req = GenericHackathonJoinRequest.objects.get(team=self.team, user=self.carol)
        self._auth(self.alice)
        resp = self.client.post(f"{BASE}/team/requests/{req.id}/accept/", {}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(self.team.members.filter(id=self.carol.id).exists())
        self.assertFalse(GenericHackathonJoinRequest.objects.filter(id=req.id).exists())

    def test_accept_only_by_leader(self):
        self._request_join(self.carol)
        req = GenericHackathonJoinRequest.objects.get(team=self.team, user=self.carol)
        self._auth(self.bob)  # a member, but not the leader
        resp = self.client.post(f"{BASE}/team/requests/{req.id}/accept/", {}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(self.team.members.filter(id=self.carol.id).exists())

    def test_accept_capacity_guard(self):
        self._request_join(self.carol)
        req = GenericHackathonJoinRequest.objects.get(team=self.team, user=self.carol)
        extra = [User.objects.create_user(email=f"cap{i}@example.com") for i in range(4)]
        self.team.members.add(*extra)  # team fills to 6 before the leader acts
        self._auth(self.alice)
        resp = self.client.post(f"{BASE}/team/requests/{req.id}/accept/", {}, format="json")
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(self.team.members.filter(id=self.carol.id).exists())

    def test_reject_deletes_request_without_adding(self):
        self._request_join(self.carol)
        req = GenericHackathonJoinRequest.objects.get(team=self.team, user=self.carol)
        self._auth(self.alice)
        resp = self.client.post(f"{BASE}/team/requests/{req.id}/reject/", {}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(GenericHackathonJoinRequest.objects.filter(id=req.id).exists())
        self.assertFalse(self.team.members.filter(id=self.carol.id).exists())

    def test_cancel_only_by_owner(self):
        self._request_join(self.carol)
        req = GenericHackathonJoinRequest.objects.get(team=self.team, user=self.carol)
        self._auth(self.dave)  # not the requester
        resp = self.client.post(f"{BASE}/team/requests/{req.id}/cancel/", {}, format="json")
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(GenericHackathonJoinRequest.objects.filter(id=req.id).exists())
        self._auth(self.carol)
        resp = self.client.post(f"{BASE}/team/requests/{req.id}/cancel/", {}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(GenericHackathonJoinRequest.objects.filter(id=req.id).exists())

    def test_requests_listing_incoming_and_outgoing(self):
        self._request_join(self.carol)
        self._auth(self.alice)
        resp = self.client.get(f"{BASE}/team/requests/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["incoming"]), 1)
        self.assertEqual(resp.data["incoming"][0]["user"]["id"], self.carol.id)
        self.assertEqual(len(resp.data["outgoing"]), 0)
        self._auth(self.carol)
        resp = self.client.get(f"{BASE}/team/requests/")
        self.assertEqual(len(resp.data["outgoing"]), 1)
        self.assertEqual(len(resp.data["incoming"]), 0)

    def test_create_with_name_owned_by_others_is_409(self):
        self._auth(self.dave)
        resp = self.client.post(f"{BASE}/teams/", {"team_name": "Grid Builders"}, format="json")
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(self.team.members.filter(id=self.dave.id).exists())

    def test_create_while_on_a_team_is_blocked(self):
        self._auth(self.bob)  # bob is on Grid Builders
        resp = self.client.post(f"{BASE}/teams/", {"team_name": "Brand New"}, format="json")
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(GenericHackathonTeam.objects.filter(team_name="Brand New").exists())


# --- No-DB coverage of the request serializer ---

class _StubUser:
    def __init__(self, uid):
        self.id = uid
        self.full_name = "Carol"
        self.email = "carol@example.com"
        self.avatar_url = None


class _StubTeam:
    team_id = 3
    team_name = "Grid Builders"


class _StubRequest:
    id = 7
    created_at = datetime.datetime(2026, 6, 3, 12, 0, 0)

    def __init__(self):
        self.user = _StubUser(9)
        self.team = _StubTeam()


class SerializeJoinRequestTests(SimpleTestCase):
    def test_shape(self):
        from generic_hackathons.views import _serialize_join_request

        out = _serialize_join_request(_StubRequest())
        self.assertEqual(out["id"], 7)
        self.assertEqual(out["user"]["id"], 9)
        self.assertEqual(out["team_id"], 3)
        self.assertEqual(out["team_name"], "Grid Builders")
        self.assertTrue(out["created_at"].startswith("2026-06-03"))
