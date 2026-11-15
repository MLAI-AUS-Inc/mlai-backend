"""Admin → Users and teams without a database: routes, the gate, the wire shape."""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from django.urls import resolve
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat import admin_team
from community_chat.admin_views import (
    AdminTeamMemberView,
    AdminTeamSearchView,
    AdminTeamView,
    MlaiAdminView,
)
from community_chat.throttles import CommunityChatScopedThrottle
from core.models import User

PREFIX = "/api/v1/community-chat/admin/team/"


def user(pk=1, **fields):
    base = dict(
        pk=pk, is_active=True, is_superuser=False, email=f"u{pk}@example.test",
        full_name=f"User {pk}", avatar_url="", community_chat_profile_id=uuid.UUID(int=pk),
    )
    base.update(fields)
    return SimpleNamespace(**base)


def row(pk=10, role="committee", account=None, portfolio=None):
    return SimpleNamespace(
        pk=pk, role=role, portfolio=portfolio, slack_user_id="U123", user=account,
        user_id=getattr(account, "pk", None),
        created_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
    )


class RouteTests(SimpleTestCase):
    def test_every_team_route_is_behind_the_admin_gate(self):
        for path, view in (
            (PREFIX, AdminTeamView),
            (PREFIX + "search/", AdminTeamSearchView),
            (PREFIX + "7/", AdminTeamMemberView),
        ):
            with self.subTest(path=path):
                resolved = resolve(path).func.view_class
                self.assertIs(resolved, view)
                self.assertTrue(issubclass(resolved, MlaiAdminView))
                self.assertIs(resolved.admin_only, True)


@patch.object(CommunityChatScopedThrottle, "allow_request", return_value=True)
class GateTests(SimpleTestCase):
    def call(self, view, method, path, data=None, *, admin=True, **kwargs):
        factory = APIRequestFactory()
        request = getattr(factory, method)(path, data or {}, format="json")
        force_authenticate(request, user=User(email="a@example.test", is_active=True))
        request.community_chat_public_key = "a" * 64
        request.community_chat_installation_id = uuid.uuid4()
        with patch("community_chat.admin_access.has_verified_installation", return_value=True), \
                patch("community_chat.admin_access.is_mlai_admin_user", return_value=admin):
            response = view.as_view()(request, **kwargs)
        response.render()
        return response

    def test_non_admins_get_403_before_any_service_call(self, _throttle):
        with patch.object(admin_team, "list_team") as listing, patch.object(admin_team, "grant") as grant:
            self.assertEqual(self.call(AdminTeamView, "get", PREFIX, admin=False).status_code, 403)
            self.assertEqual(self.call(AdminTeamView, "post", PREFIX, admin=False).status_code, 403)
        listing.assert_not_called()
        grant.assert_not_called()

    def test_refusals_carry_their_code_and_status(self, _throttle):
        with patch.object(admin_team, "grant", side_effect=admin_team.TeamError("not_allowed", 403)):
            response = self.call(AdminTeamView, "post", PREFIX, {"account_id": "x", "role": "admin"})
        self.assertEqual((response.status_code, response.data), (403, {"error": "not_allowed"}))
        with patch.object(admin_team, "remove", side_effect=admin_team.TeamError("last_admin", 409)):
            response = self.call(AdminTeamMemberView, "delete", PREFIX + "3/", member_id=3)
        self.assertEqual((response.status_code, response.data), (409, {"error": "last_admin"}))

    def test_patch_passes_only_the_fields_sent(self, _throttle):
        with patch.object(admin_team, "update", return_value={"id": 3}) as update:
            response = self.call(AdminTeamMemberView, "patch", PREFIX + "3/", {"team": None}, member_id=3)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(update.call_args.kwargs, {"team": None})
        self.assertEqual(response["Cache-Control"], "no-store")


class RuleTests(SimpleTestCase):
    def test_only_superusers_and_admins_may_manage(self):
        for role, allowed in (("superuser", True), ("admin", True), ("committee", False), (None, False)):
            with self.subTest(role=role), patch.object(admin_team, "mlai_admin_role", return_value=role):
                self.assertIs(admin_team.can_manage(user()), allowed)
                if not allowed:
                    with self.assertRaises(admin_team.TeamError) as caught:
                        admin_team.search_accounts(user(), "sam")
                    self.assertEqual((caught.exception.code, caught.exception.status), ("not_allowed", 403))

    def test_only_admin_and_committee_can_be_granted_and_teams_are_known(self):
        self.assertEqual(admin_team._clean_role("committee"), "committee")
        for bad in ("superuser", "portfolio_lead", "partner", "", None):
            with self.subTest(role=bad), self.assertRaises(admin_team.TeamError):
                admin_team._clean_role(bad)
        self.assertEqual(admin_team._clean_team("tech"), "tech")
        self.assertIsNone(admin_team._clean_team(""))
        with self.assertRaises(admin_team.TeamError):
            admin_team._clean_team("finance")

    def test_member_shape_for_a_committee_member(self):
        member = admin_team._member(row(account=user(2), portfolio="tech"), user(2), user(1))
        self.assertEqual(member["role"], "committee")
        self.assertEqual(member["team"], "tech")
        self.assertTrue(member["admin_access"])
        self.assertTrue(member["can_change_role"] and member["can_remove"])
        self.assertEqual(member["account_id"], str(uuid.UUID(int=2)))

    def test_you_cannot_change_your_own_role_or_remove_yourself(self):
        me = user(1)
        member = admin_team._member(row(account=me), me, me)
        self.assertTrue(member["is_self"])
        self.assertFalse(member["can_change_role"])
        self.assertFalse(member["can_remove"])
        self.assertTrue(member["can_change_team"])

    def test_superusers_are_read_only(self):
        boss = user(5, is_superuser=True)
        for points_row in (None, row(account=boss)):
            member = admin_team._member(points_row, boss, user(1))
            self.assertEqual(member["role"], "superuser")
            self.assertTrue(member["admin_access"])
            self.assertFalse(member["can_change_role"] or member["can_change_team"] or member["can_remove"])

    def test_other_roo_roles_and_unlinked_rows_have_no_admin_access(self):
        lead = admin_team._member(row(role="portfolio_lead", account=user(3)), user(3), user(1))
        self.assertFalse(lead["admin_access"])
        unlinked = admin_team._member(row(role="admin"), None, user(1))
        self.assertFalse(unlinked["admin_access"])
        self.assertEqual(unlinked["name"], "U123")
        inactive = user(4, is_active=False)
        self.assertFalse(admin_team._member(row(role="admin", account=inactive), inactive, user(1))["admin_access"])

    def test_admins_sort_first_then_by_role_and_name(self):
        members = [
            {"admin_access": False, "role": "portfolio_lead", "name": "Al"},
            {"admin_access": True, "role": "committee", "name": "bo"},
            {"admin_access": True, "role": "superuser", "name": "Zed"},
            {"admin_access": True, "role": "committee", "name": "Ann"},
        ]
        members.sort(key=admin_team._sort_key)
        self.assertEqual([m["name"] for m in members], ["Zed", "Ann", "bo", "Al"])
