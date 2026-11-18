from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from unittest import skipUnless
from urllib.parse import urlsplit

from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from content_factory.models import OrganizationContentConfig
from core.models import User
from organizations.models import Organization

from . import membership, startup_team
from .models import StartupMembership, VibeRaisingCompany, VibeRaisingProfile


@override_settings(
    MY_STARTUP_SHARED_ACCESS_ENABLED=True,
    MY_STARTUP_TEAM_ENABLED=True,
    MY_STARTUP_TRANSITION_NOTIFICATIONS_ENABLED=False,
    COMMUNITY_CHAT_FRONTEND_URL="https://chat.synthetic.test",
)
class SharedStartupTests(TestCase):
    def setUp(self):
        self.alice = self.user("alice")
        self.ben = self.user("ben")
        self.cara = self.user("cara")
        self.atlas = self.company("Atlas", self.alice)
        self.beacon = self.company("Beacon", self.alice)
        self.client = APIClient()

    def user(self, name):
        return User.objects.create_user(
            email=f"{name}@synthetic.test", first_name=name, email_verified_at=timezone.now(),
        )

    def company(self, name, creator, *, shared=True):
        org = Organization.objects.create(name=name, domain=f"{name.lower()}.synthetic.test")
        profile, _ = VibeRaisingProfile.objects.get_or_create(user=creator, defaults={"role": "founder"})
        company = VibeRaisingCompany.objects.create(name=name, profile=profile, organization=org, domain=org.domain)
        if shared:
            membership.activate_new_company(company, creator)
        return company

    def invite(self, company, sender):
        url = startup_team.invite_link(organization=company.organization, user=sender, action="create")["url"]
        return urlsplit(url).fragment

    def join(self, company, sender, recipient):
        return startup_team.join_invite(user=recipient, token=self.invite(company, sender))

    def test_join_edit_switch_and_invite_as_an_equal_teammate(self):
        self.assertEqual(self.join(self.atlas, self.alice, self.ben)["companyId"], str(self.atlas.pk))
        self.join(self.atlas, self.ben, self.cara)
        self.client.force_authenticate(self.ben)
        base = "/api/v1/my-startup/founder-tools"
        response = self.client.get(f"{base}/companies/")
        self.assertEqual([row["id"] for row in response.data], [str(self.atlas.pk)])
        response = self.client.post(f"{base}/companies/", {
            "companyId": str(self.atlas.pk), "name": "Atlas Labs", "domain": self.atlas.domain,
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.atlas.refresh_from_db()
        self.assertEqual(self.atlas.name, "Atlas Labs")
        response = self.client.post(f"{base}/active-company/", {"companyId": str(self.atlas.pk)}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(VibeRaisingProfile.objects.get(user=self.ben).active_company_id, self.atlas.pk)
        self.assertFalse(VibeRaisingProfile.objects.get(user=self.alice).active_company_id)
        response = self.client.post(f"{base}/companies/", {
            "companyId": str(self.beacon.pk), "name": "Foreign edit",
        }, format="json")
        self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual(VibeRaisingCompany.objects.count(), 2)

    def test_creator_removal_and_profile_deletion_preserve_shared_workspace(self):
        self.join(self.atlas, self.alice, self.ben)
        alice_id = StartupMembership.objects.get(company=self.atlas, user=self.alice).pk
        older_invite = self.invite(self.atlas, self.ben)
        startup_team.update_member(organization=self.atlas.organization, user=self.ben, member_id=alice_id, remove=True)
        self.assertFalse(membership.companies_for_user(self.alice).filter(pk=self.atlas.pk).exists())
        with self.assertRaises(startup_team.TeamError):
            startup_team.join_invite(user=self.alice, token=older_invite)
        self.assertTrue(membership.companies_for_user(self.alice).filter(pk=self.beacon.pk).exists())
        self.atlas.profile.delete()
        self.atlas.refresh_from_db()
        self.assertIsNone(self.atlas.profile_id)
        team = startup_team.team_payload(organization=self.atlas.organization, user=self.ben)
        self.assertEqual(team["accessPolicy"], "shared")
        self.assertTrue(team["canManage"])
        self.assertTrue(team["members"][0]["isYou"])
        with self.assertRaises(startup_team.TeamError) as refused:
            startup_team.update_member(organization=self.atlas.organization, user=self.ben,
                                       member_id=team["members"][0]["memberId"], remove=True)
        self.assertEqual(refused.exception.reason, "last_member")

    def test_existing_roster_cutover_preserves_ids_and_closes_old_invites(self):
        company = self.company("Existing", self.alice, shared=False)
        token = self.invite(company, self.alice)
        startup_team.join_invite(user=self.ben, token=token)
        config = OrganizationContentConfig.objects.get(organization=company.organization)
        member_id = config.pillar_strategy["startup_team"]["members"][0]["memberId"]
        for apply in [False, True, True]:
            call_command("migrate_startup_membership", company=str(company.pk), apply=apply, stdout=StringIO())
        self.assertEqual(str(StartupMembership.objects.get(company=company, user=self.ben).pk), member_id)
        self.assertTrue(startup_team.member_may_publish(self.ben, company.organization))
        with self.assertRaises(startup_team.TeamError):
            startup_team.join_invite(user=self.cara, token=token)

    def test_close_requires_distinct_confirmation_and_revokes_team(self):
        self.join(self.atlas, self.alice, self.ben)
        self.client.force_authenticate(self.ben)
        route = f"/api/v1/my-startup/founder-tools/companies/{self.atlas.pk}/"
        self.assertEqual(self.client.delete(route).status_code, 409)
        response = self.client.delete(route, {"confirmCloseForEveryone": True}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.atlas.refresh_from_db()
        self.assertIsNotNone(self.atlas.closed_at)
        self.assertFalse(membership.companies_for_user(self.alice).filter(pk=self.atlas.pk).exists())
        self.assertFalse(membership.companies_for_user(self.ben).filter(pk=self.atlas.pk).exists())
        self.assertTrue(Organization.objects.filter(pk=self.atlas.organization_id).exists())

    def test_agent_grant_does_not_revive_after_readmission(self):
        from startup_updates.mcp.oauth import AccessProblem, company_for

        self.join(self.atlas, self.alice, self.ben)
        grant = {"company_id": str(self.atlas.pk), "createdAt": timezone.now().isoformat()}
        self.assertEqual(company_for(self.ben, self.atlas.pk, grant), self.atlas)
        member_id = StartupMembership.objects.get(company=self.atlas, user=self.ben).pk
        startup_team.update_member(organization=self.atlas.organization, user=self.alice, member_id=member_id, remove=True)
        self.join(self.atlas, self.alice, self.ben)
        with self.assertRaises(AccessProblem):
            company_for(self.ben, self.atlas.pk, grant)

    def test_account_merge_keeps_target_revocation(self):
        self.join(self.atlas, self.alice, self.ben)
        self.join(self.atlas, self.alice, self.cara)
        target = StartupMembership.objects.get(company=self.atlas, user=self.cara)
        startup_team.update_member(organization=self.atlas.organization, user=self.alice, member_id=target.pk, remove=True)
        membership.merge_account_memberships(self.ben, self.cara, actor=self.alice)
        self.assertFalse(membership.has_membership(self.cara, self.atlas))
        self.assertFalse(StartupMembership.objects.filter(company=self.atlas, user=self.ben).exists())


@skipUnless(connection.vendor == "postgresql", "Workspace lock concurrency requires PostgreSQL")
class SharedRemovalConcurrencyTests(TransactionTestCase):
    def test_simultaneous_removals_keep_one_active_teammate(self):
        first = User.objects.create_user(email="first@synthetic.test")
        second = User.objects.create_user(email="second@synthetic.test")
        organization = Organization.objects.create(name="Atlas", domain="concurrency.synthetic.test")
        company = VibeRaisingCompany.objects.create(
            name="Atlas", organization=organization, membership_policy=VibeRaisingCompany.SHARED_ACCESS,
        )
        first_row, _ = membership.admit(company, first, source="creation")
        second_row, _ = membership.admit(company, second, source="request")

        def remove(actor_id, member_id):
            close_old_connections()
            try:
                return startup_team.update_member(
                    organization=organization, user=User.objects.get(pk=actor_id),
                    member_id=member_id, remove=True,
                )["status"]
            except startup_team.TeamError as exc:
                return exc.reason
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            left = executor.submit(remove, first.pk, second_row.pk)
            right = executor.submit(remove, second.pk, first_row.pk)
            self.assertCountEqual([left.result(timeout=15), right.result(timeout=15)], ["removed", "not_found"])
        self.assertEqual(membership.active_memberships(company).count(), 1)
