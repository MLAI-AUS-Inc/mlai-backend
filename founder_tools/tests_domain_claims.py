"""A claimed domain is never a dead end (P0.10).

A different founder gets a typed company_domain_claimed 409 that offers to
carry on without the website and queues one ops alert after the refused save
rolls back. The founder who deleted a startup can add it again, and a hidden
research workspace stops holding its domain after the claim hold.
"""
import importlib.util
import json
from datetime import timedelta
from unittest import skipIf, skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from content_factory.models import OrganizationContentConfig
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from founder_tools.profile_fields import PROFILE_KEY, is_research_workspace, save_company_branding
from founder_tools.services import (
    CompanyDomainClaimedError,
    DomainOwnershipError,
    ensure_company_organization,
    organization_owner_user_id,
    previous_owner_matches,
    queue_domain_claim_alert,
)
from organizations.models import Organization

User = get_user_model()
COMPANIES_URL = "/api/v1/founder-tools/companies/"
ALERT = "founder_tools.views.queue_domain_claim_alert"
_REVOKE = "startup_updates.data_deletion._revoke_google_refresh_token"
_REVOKE_OK = {"requested": True, "succeeded": True, "warning": None}
HAS_ALERT_HELPER = importlib.util.find_spec("content_factory.founder_journey_alerts") is not None
CLAIMED_BODY = {
    "code": "company_domain_claimed",
    "detail": "This startup is already on MLAI.",
    "field": "domain",
    "canContinueWithoutDomain": True,
}


class DomainClaimTests(TestCase):
    def setUp(self):
        mock = patch("founder_tools.services.reconcile_user_slack_id_from_email")
        mock.start()
        self.addCleanup(mock.stop)
        self.owner = User.objects.create_user(
            email="claim-owner@example.test", first_name="Olive", last_name="Owner", role="participant")
        self.claimant = User.objects.create_user(
            email="claim-cofounder@example.test", first_name="Cory", last_name="Claimant", role="participant")
        self.owner_client = self._client(self.owner)
        self.claimant_client = self._client(self.claimant)

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _create(self, client, name="Acme", domain="acme.test", expected=200, **body):
        payload = {"name": name, "createNew": True, **body}
        if domain is not None:
            payload["domain"] = domain
        response = client.post(COMPANIES_URL, payload, format="json")
        self.assertEqual(response.status_code, expected, response.data)
        return response

    def _research_workspace(self, user, *, age, domain="acme.test"):
        """The hidden company "Research my startup" makes before any save."""
        profile, _ = VibeRaisingProfile.objects.get_or_create(user=user, defaults={"role": "founder"})
        company = VibeRaisingCompany.objects.create(profile=profile, name="Acme", domain=domain)
        organization = ensure_company_organization(company)
        config, _ = OrganizationContentConfig.objects.get_or_create(organization=organization)
        config.pillar_strategy = {PROFILE_KEY: {"researchDraft": True}}
        config.save()
        VibeRaisingCompany.objects.filter(pk=company.pk).update(created_at=timezone.now() - age)
        company.refresh_from_db()
        self.assertTrue(is_research_workspace(company))
        return company

    def assert_claimed_body(self, response):
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data, CLAIMED_BODY)
        raw = json.loads(response.content)
        self.assertIs(raw["canContinueWithoutDomain"], True)
        text = response.content.decode()
        for leaked in ("already linked to another account", "Olive", "Owner", "claim-owner@example.test"):
            self.assertNotIn(leaked, text)

    def test_a_different_founder_gets_the_typed_claim_and_one_alert(self):
        self._create(self.owner_client)
        organization = Organization.objects.get(domain="acme.test")

        with patch(ALERT) as alert:
            response = self._create(self.claimant_client, name="Acme Cofounder", expected=409)

        self.assert_claimed_body(response)
        alert.assert_called_once_with(organization=organization, claimant_user_id=self.claimant.id)
        self.assertFalse(VibeRaisingCompany.objects.filter(profile__user=self.claimant).exists())

    def test_a_claim_refused_after_company_writes_rolls_them_back_before_the_alert(self):
        self._create(self.owner_client)
        organization = Organization.objects.get(domain="acme.test")
        seen = []

        def alert(**kwargs):
            # Queued only after the refused save's company row was rolled back.
            seen.append(VibeRaisingCompany.objects.filter(profile__user=self.claimant).count())
            return "alert-id"

        # Pretend the up-front availability check raced a concurrent claim, so
        # the company is written before ensure_company_organization refuses it.
        with patch("founder_tools.views.domain_is_available_to", return_value=True), \
                patch(ALERT, side_effect=alert) as queued:
            response = self._create(self.claimant_client, name="Acme Cofounder", expected=409)

        self.assert_claimed_body(response)
        queued.assert_called_once_with(organization=organization, claimant_user_id=self.claimant.id)
        self.assertEqual(seen, [0])
        self.assertFalse(VibeRaisingCompany.objects.filter(profile__user=self.claimant).exists())
        self.assertEqual(organization_owner_user_id(organization), self.owner.id)

    def test_carry_on_without_the_website_saves_the_startup(self):
        self._create(self.owner_client)
        with patch(ALERT):
            self._create(self.claimant_client, name="Acme Cofounder", expected=409)

        response = self._create(self.claimant_client, name="Acme Cofounder", domain=None)

        company = VibeRaisingCompany.objects.select_related("organization").get(pk=response.data["id"])
        self.assertEqual(company.organization.domain, f"startup-{company.pk}.invalid")
        self.assertEqual(company.profile.user_id, self.claimant.id)

    def test_the_same_founder_can_add_a_deleted_startup_again(self):
        first = self._create(self.owner_client)
        organization = Organization.objects.get(domain="acme.test")
        with patch(_REVOKE, return_value=_REVOKE_OK):
            deleted = self.owner_client.delete(f"{COMPANIES_URL}{first.data['id']}/")
        self.assertEqual(deleted.status_code, 200)
        record = OrganizationContentConfig.objects.get(organization=organization).pillar_strategy["previous_owner"]
        self.assertEqual(record["user_id"], self.owner.id)
        self.assertEqual(record["company_id"], first.data["id"])
        self.assertEqual(record["reason"], "company_deleted")
        self.assertTrue(record["offboarded_at"])
        self.assertTrue(previous_owner_matches(self.owner, organization))
        self.assertFalse(previous_owner_matches(self.claimant, organization))

        with patch(ALERT) as alert:
            self.assert_claimed_body(self._create(self.claimant_client, expected=409))
        alert.assert_called_once()

        again = self._create(self.owner_client, name="Acme Again")

        company = VibeRaisingCompany.objects.get(pk=again.data["id"])
        self.assertEqual(company.organization_id, organization.pk)
        self.assertEqual(organization_owner_user_id(organization), self.owner.id)
        strategy = OrganizationContentConfig.objects.get(organization=organization).pillar_strategy
        self.assertNotIn("previous_owner", strategy)

    def test_previous_owner_reclaims_through_ensure_company_organization(self):
        organization = Organization.objects.create(name="Acme", domain="acme.test")
        OrganizationContentConfig.objects.create(organization=organization, pillar_strategy={
            "previous_owner": {"user_id": self.owner.id, "company_id": "gone", "offboarded_at": "2026-10-01T00:00:00+00:00",
                               "reason": "company_deleted"}})
        profile = VibeRaisingProfile.objects.create(user=self.owner, role="founder")
        stranger_profile = VibeRaisingProfile.objects.create(user=self.claimant, role="founder")

        with self.assertRaises(CompanyDomainClaimedError):
            ensure_company_organization(
                VibeRaisingCompany.objects.create(profile=stranger_profile, name="Acme", domain="acme.test"))
        company = VibeRaisingCompany.objects.create(profile=profile, name="Acme", domain="acme.test")
        self.assertEqual(ensure_company_organization(company), organization)
        self.assertNotIn("previous_owner",
                         OrganizationContentConfig.objects.get(organization=organization).pillar_strategy)

    def test_a_stale_research_workspace_no_longer_blocks_a_real_claim(self):
        workspace = self._research_workspace(self.owner, age=timedelta(hours=25))
        organization = workspace.organization

        with patch(ALERT) as alert:
            response = self._create(self.claimant_client, name="Acme Cofounder")

        alert.assert_not_called()
        company = VibeRaisingCompany.objects.get(pk=response.data["id"])
        self.assertEqual(company.organization_id, organization.pk)
        self.assertEqual(organization_owner_user_id(organization), self.claimant.id)
        self.assertFalse(VibeRaisingCompany.objects.filter(pk=workspace.pk).exists())
        strategy = OrganizationContentConfig.objects.get(organization=organization).pillar_strategy
        self.assertNotIn("researchDraft", strategy.get(PROFILE_KEY, {}))
        listed = self.claimant_client.get(COMPANIES_URL)
        self.assertEqual([row["id"] for row in listed.data], [str(company.pk)])
        self.assertEqual(self.owner_client.get(COMPANIES_URL).data, [])

    def test_a_recent_research_workspace_still_holds_its_domain(self):
        workspace = self._research_workspace(self.owner, age=timedelta(hours=2))

        with patch(ALERT) as alert:
            self.assert_claimed_body(self._create(self.claimant_client, name="Acme Cofounder", expected=409))

        alert.assert_called_once_with(organization=workspace.organization, claimant_user_id=self.claimant.id)
        self.assertTrue(VibeRaisingCompany.objects.filter(pk=workspace.pk).exists())
        self.assertEqual(organization_owner_user_id(workspace.organization), self.owner.id)

    @override_settings(FOUNDER_RESEARCH_WORKSPACE_CLAIM_HOLD_HOURS=1)
    def test_the_claim_hold_is_configurable(self):
        workspace = self._research_workspace(self.owner, age=timedelta(hours=2))
        self._create(self.claimant_client, name="Acme Cofounder")
        self.assertFalse(VibeRaisingCompany.objects.filter(pk=workspace.pk).exists())

    def test_the_researcher_keeps_their_own_stale_workspace(self):
        workspace = self._research_workspace(self.owner, age=timedelta(hours=48))
        self.assertEqual(organization_owner_user_id(workspace.organization), self.owner.id)

        response = self._create(self.owner_client)

        self.assertEqual(response.data["id"], str(workspace.pk))
        self.assertEqual(organization_owner_user_id(workspace.organization), self.owner.id)

    def test_other_tenancy_guards_keep_the_old_body(self):
        self._create(self.owner_client)
        VibeRaisingProfile.objects.create(user=self.claimant, role=VibeRaisingProfile.ROLE_FOUNDER)
        response = self.claimant_client.post(
            "/api/v1/vibe-raising/companies/", {"name": "Acme Cofounder", "domain": "acme.test"}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(str(response.data["detail"]), "This domain is already linked to another account.")
        self.assertNotIn("canContinueWithoutDomain", response.data)

        owned = VibeRaisingCompany.objects.get(profile__user=self.owner)
        stranger_profile = VibeRaisingProfile.objects.get(user=self.claimant)
        stranger_company = VibeRaisingCompany.objects.create(
            profile=stranger_profile, name="Elsewhere", organization=owned.organization)
        with self.assertRaises(DomainOwnershipError) as caught:
            save_company_branding(stranger_company, self.claimant, "https://example.test/logo.png")
        self.assertNotIsInstance(caught.exception, CompanyDomainClaimedError)
        self.assertEqual(caught.exception.default_code, "domain_already_claimed")

    @skipIf(HAS_ALERT_HELPER, "the founder-journey alert helper is installed")
    def test_without_the_alert_helper_the_claim_is_logged_for_ops(self):
        organization = Organization.objects.create(name="Acme", domain="acme.test")
        with self.assertLogs("founder_tools.services", level="WARNING") as logs:
            alert_id = queue_domain_claim_alert(organization=organization, claimant_user_id=self.claimant.id)
        self.assertTrue(alert_id)
        [line] = logs.output
        self.assertIn("founder_journey_alert", line)
        self.assertIn("alert_reason=domain_claim_disputed", line)
        self.assertNotIn("claim-cofounder", line)

    @skipUnless(HAS_ALERT_HELPER, "needs the founder-journey alert helper (IF-15)")
    def test_exactly_one_alert_receipt_survives_the_rollback(self):
        self._create(self.owner_client)
        organization = Organization.objects.get(domain="acme.test")
        for _ in range(2):
            self._create(self.claimant_client, name="Acme Cofounder", expected=409)
        strategy = OrganizationContentConfig.objects.get(organization=organization).pillar_strategy
        receipts = [receipt for receipt in strategy.get("ops_alerts", {}).values()
                    if receipt.get("alert_reason", "domain_claim_disputed") == "domain_claim_disputed"]
        self.assertEqual(len(receipts), 1)
