"""Admin → Users and teams on PostgreSQL: granting, changing and removing access."""

from django.test import TestCase

from community_chat import admin_team
from core.models import User
from crm.models import CrmAuditEvent
from roo.models import PointsAdmin
from roo.permissions import mlai_admin_role


class AdminTeamDatabaseTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create(email="manager@example.test", first_name="Mia", slack_id="UMANAGER")
        PointsAdmin.objects.create(slack_user_id="UMANAGER", user=self.manager, role="admin")
        self.committee = User.objects.create(email="committee@example.test", first_name="Cal", slack_id="UCOMM")
        self.committee_row = PointsAdmin.objects.create(slack_user_id="UCOMM", user=self.committee, role="committee")
        self.member = User.objects.create(email="member@example.test", first_name="Max")

    def error(self, call, *args, **kwargs):
        with self.assertRaises(admin_team.TeamError) as caught:
            call(*args, **kwargs)
        return caught.exception.code

    def test_list_shows_admins_and_superusers_once_each(self):
        boss = User.objects.create(email="boss@example.test", is_superuser=True, slack_id="UBOSS")
        PointsAdmin.objects.create(slack_user_id="UBOSS", user=boss, role="committee")
        PointsAdmin.objects.create(slack_user_id="UOLD", user=self.member, role="admin", is_active=False)
        team = admin_team.list_team(self.manager)
        emails = [member["email"] for member in team["members"]]
        self.assertEqual(sorted(emails), sorted([boss.email, self.manager.email, self.committee.email]))
        self.assertEqual(team["members"][0]["role"], "superuser")
        self.assertTrue(team["can_manage"])
        self.assertEqual(team["admin_count"], 3)
        self.assertFalse(admin_team.list_team(self.committee)["can_manage"])

    def test_granting_a_new_account_without_slack_creates_a_linked_row(self):
        member = admin_team.grant(self.manager, str(self.member.community_chat_profile_id), "committee", "tech")
        self.assertEqual((member["role"], member["team"], member["admin_access"]), ("committee", "tech", True))
        row = PointsAdmin.objects.get(user=self.member)
        self.assertTrue(row.slack_user_id.startswith("account:"))
        self.assertEqual(row.added_by_slack_id, "UMANAGER")
        self.assertEqual(mlai_admin_role(self.member), "committee")
        audit = CrmAuditEvent.objects.get(target_type="admin_team_member")
        self.assertEqual((audit.actor, audit.detail["change"]), (self.manager, "grant"))

    def test_granting_reactivates_an_old_row_and_adopts_a_new_slack_id(self):
        old = PointsAdmin.objects.create(
            slack_user_id=f"account:{self.member.community_chat_profile_id}", user=self.member,
            role="partner", is_active=False,
        )
        self.member.slack_id = "UNEW"
        self.member.save()
        admin_team.grant(self.manager, str(self.member.community_chat_profile_id), "admin")
        old.refresh_from_db()
        self.assertEqual((old.is_active, old.role, old.slack_user_id), (True, "admin", "UNEW"))
        self.assertEqual(PointsAdmin.objects.filter(user=self.member).count(), 1)

    def test_committee_cannot_change_anyone(self):
        account = str(self.member.community_chat_profile_id)
        self.assertEqual(self.error(admin_team.grant, self.committee, account, "committee"), "not_allowed")
        self.assertEqual(self.error(admin_team.remove, self.committee, self.committee_row.pk), "not_allowed")
        self.assertFalse(PointsAdmin.objects.filter(user=self.member).exists())

    def test_no_self_changes_and_superusers_are_managed_in_django(self):
        own = PointsAdmin.objects.get(user=self.manager)
        self.assertEqual(self.error(admin_team.remove, self.manager, own.pk), "self_change")
        self.assertEqual(self.error(admin_team.update, self.manager, own.pk, role="committee"), "self_change")
        self.assertEqual(admin_team.update(self.manager, own.pk, team="ops")["team"], "ops")
        boss = User.objects.create(email="boss@example.test", is_superuser=True)
        self.assertEqual(
            self.error(admin_team.grant, self.manager, str(boss.community_chat_profile_id), "committee"),
            "superuser_managed",
        )

    def test_role_and_team_changes_and_removal(self):
        member = admin_team.update(self.manager, self.committee_row.pk, role="admin", team="events")
        self.assertEqual((member["role"], member["team"]), ("admin", "events"))
        admin_team.update(self.manager, self.committee_row.pk, team=None)
        self.committee_row.refresh_from_db()
        self.assertIsNone(self.committee_row.portfolio)
        admin_team.remove(self.manager, self.committee_row.pk)
        self.committee_row.refresh_from_db()
        self.assertFalse(self.committee_row.is_active)
        self.assertIsNone(mlai_admin_role(self.committee))
        self.assertEqual(self.error(admin_team.remove, self.manager, self.committee_row.pk), "not_found")
        self.assertEqual(CrmAuditEvent.objects.filter(target_type="admin_team_member").count(), 3)

    def test_the_last_admin_is_never_stranded_while_no_superuser_exists(self):
        own = PointsAdmin.objects.get(user=self.manager)
        self.assertTrue(admin_team._would_strand_admins(own))
        self.assertFalse(admin_team._would_strand_admins(self.committee_row))
        second = User.objects.create(email="second@example.test", slack_id="USECOND")
        second_row = PointsAdmin.objects.create(slack_user_id="USECOND", user=second, role="admin")
        self.assertFalse(admin_team._would_strand_admins(own))
        admin_team.remove(self.manager, second_row.pk)
        self.assertTrue(admin_team._would_strand_admins(own))
        User.objects.create(email="root@example.test", is_superuser=True)
        self.assertFalse(admin_team._would_strand_admins(own))

    def test_search_finds_active_accounts_by_name_or_email(self):
        User.objects.create(email="gone@example.test", first_name="Maxine", is_active=False)
        results = admin_team.search_accounts(self.manager, "max")
        self.assertEqual([r["email"] for r in results], [self.member.email])
        self.assertEqual(admin_team.search_accounts(self.manager, "m"), [])
        self.assertEqual(admin_team.search_accounts(self.manager, "cal")[0]["role"], "committee")
