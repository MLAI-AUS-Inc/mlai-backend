"""Tests for Watt team leadership: leader-on-create, leave guard, transfer, disband."""
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from core.models import Hackathon
from .models import GenericHackathonTeam
from .serializers import GenericHackathonTeamSerializer


User = get_user_model()
SLUG = "watt-the-hack"
BASE = f"/api/v1/hackathons/{SLUG}/app"


class TeamLeadershipTests(TestCase):
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
        self.alice = User.objects.create_user(email="alice@example.com")
        self.bob = User.objects.create_user(email="bob@example.com")
        self.carol = User.objects.create_user(email="carol@example.com")

    def _create_team(self, user, name="Grid Builders"):
        self.client.force_authenticate(user)
        return self.client.post(f"{BASE}/teams/", {"team_name": name}, format="json")

    def _join_team(self, user, name="Grid Builders"):
        # Phase 3 made the API join a request; for these leadership tests add the member directly.
        team = GenericHackathonTeam.objects.get(team_name=name)
        team.members.add(user)
        return team

    def test_creator_becomes_leader(self):
        resp = self._create_team(self.alice)
        self.assertEqual(resp.status_code, 201)
        team = GenericHackathonTeam.objects.get(team_name="Grid Builders")
        self.assertEqual(team.leader_id, self.alice.id)
        self.assertEqual(resp.data["team"]["leader_id"], self.alice.id)
        roles = {m["id"]: m["role"] for m in resp.data["team"]["members"]}
        self.assertEqual(roles[self.alice.id], "leader")

    def test_join_keeps_original_leader(self):
        self._create_team(self.alice)
        self._join_team(self.bob)  # bob added as a member
        team = GenericHackathonTeam.objects.get(team_name="Grid Builders")
        self.assertEqual(team.leader_id, self.alice.id)
        self.assertTrue(team.members.filter(id=self.bob.id).exists())

    def test_leader_cannot_leave_populated_team(self):
        self._create_team(self.alice)
        self._join_team(self.bob)
        self.client.force_authenticate(self.alice)
        resp = self.client.post(f"{BASE}/team/leave/", {}, format="json")
        self.assertEqual(resp.status_code, 409)

    def test_member_leave_blocked_below_min(self):
        self._create_team(self.alice)
        self._join_team(self.bob)  # 2-member team
        self.client.force_authenticate(self.bob)
        resp = self.client.post(f"{BASE}/team/leave/", {}, format="json")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["min_members"], 2)

    def test_member_can_leave_when_team_stays_valid(self):
        self._create_team(self.alice)
        self._join_team(self.bob)
        self._join_team(self.carol)  # 3-member team
        self.client.force_authenticate(self.carol)
        resp = self.client.post(f"{BASE}/team/leave/", {}, format="json")
        self.assertEqual(resp.status_code, 200)
        team = GenericHackathonTeam.objects.get(team_name="Grid Builders")
        self.assertEqual(team.members.count(), 2)
        self.assertFalse(team.members.filter(id=self.carol.id).exists())

    def test_transfer_lead(self):
        self._create_team(self.alice)
        self._join_team(self.bob)
        self.client.force_authenticate(self.alice)
        resp = self.client.post(f"{BASE}/team/transfer-lead/", {"member_id": self.bob.id}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["leader_id"], self.bob.id)
        team = GenericHackathonTeam.objects.get(team_name="Grid Builders")
        self.assertEqual(team.leader_id, self.bob.id)

    def test_non_leader_cannot_transfer(self):
        self._create_team(self.alice)
        self._join_team(self.bob)
        self.client.force_authenticate(self.bob)
        resp = self.client.post(f"{BASE}/team/transfer-lead/", {"member_id": self.bob.id}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_transfer_to_non_member_rejected(self):
        self._create_team(self.alice)
        self._join_team(self.bob)
        self.client.force_authenticate(self.alice)
        resp = self.client.post(f"{BASE}/team/transfer-lead/", {"member_id": self.carol.id}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_leader_can_leave_after_transfer(self):
        self._create_team(self.alice)
        self._join_team(self.bob)
        self._join_team(self.carol)  # 3 members, alice leader
        self.client.force_authenticate(self.alice)
        self.client.post(f"{BASE}/team/transfer-lead/", {"member_id": self.bob.id}, format="json")
        resp = self.client.post(f"{BASE}/team/leave/", {}, format="json")
        self.assertEqual(resp.status_code, 200)  # now a non-leader; team stays at 2

    def test_disband_empties_team_without_deleting_record(self):
        self._create_team(self.alice)
        self._join_team(self.bob)
        self.client.force_authenticate(self.alice)
        resp = self.client.post(f"{BASE}/team/disband/", {}, format="json")
        self.assertEqual(resp.status_code, 200)
        team = GenericHackathonTeam.objects.get(team_name="Grid Builders")
        self.assertEqual(team.members.count(), 0)
        self.assertIsNone(team.leader_id)

    def test_non_leader_cannot_disband(self):
        self._create_team(self.alice)
        self._join_team(self.bob)
        self.client.force_authenticate(self.bob)
        resp = self.client.post(f"{BASE}/team/disband/", {}, format="json")
        self.assertEqual(resp.status_code, 403)


# --- No-DB coverage of the serializer's leader-role logic ---

class _StubUser:
    def __init__(self, uid, email="x@example.com", full_name="X", avatar_url=None):
        self.id = uid
        self.email = email
        self.full_name = full_name
        self.avatar_url = avatar_url


class _StubMembers:
    def __init__(self, users):
        self._users = users

    def all(self):
        return self._users

    def count(self):
        return len(self._users)


class _StubTeam:
    def __init__(self, users, leader_id):
        self.members = _StubMembers(users)
        self.leader_id = leader_id


class TeamSerializerRoleTests(SimpleTestCase):
    def test_leader_member_gets_leader_role(self):
        team = _StubTeam([_StubUser(1), _StubUser(2)], leader_id=1)
        ser = GenericHackathonTeamSerializer()
        roles = {m["id"]: m["role"] for m in ser.get_members(team)}
        self.assertEqual(roles[1], "leader")
        self.assertEqual(roles[2], "participant")
        self.assertEqual(ser.get_leader_id(team), 1)
        self.assertEqual(ser.get_member_count(team), 2)

    def test_no_leader_all_participant(self):
        team = _StubTeam([_StubUser(1), _StubUser(2)], leader_id=None)
        members = GenericHackathonTeamSerializer().get_members(team)
        self.assertTrue(all(m["role"] == "participant" for m in members))
