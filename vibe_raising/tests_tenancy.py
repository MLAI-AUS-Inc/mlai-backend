"""Cross-tenant isolation tests for Vibe Raising (Crit-2 containment).

The requirement: only the founder who created a startup (or an mlai admin) may
read or write its updates. Tenancy is keyed on ``Organization.domain`` -- a
unique, shared row -- and the company ``domain`` is founder-supplied. Before this
fix, a founder could set their company domain to a rival's and read/overwrite the
rival's tenant. These tests lock that down via first-claim-wins ownership.
"""
from datetime import date, datetime, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from founder_tools.services import ensure_company_organization
from startup_updates.models import MonthlyUpdateDraft, MonthlyUpdateDraftStatus
from vibe_raising.views import _resolve_owned_organization

User = get_user_model()

COMPANIES_URL = "/api/v1/vibe-raising/companies/"
UPDATES_URL = "/api/v1/vibe-raising/updates/"


class VibeRaisingTenancyTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.victim = User.objects.create_user(
            email="victim@acme.com", password="pw",
            first_name="V", last_name="Ictim", role="participant",
        )
        self.attacker = User.objects.create_user(
            email="attacker@evil.com", password="pw",
            first_name="A", last_name="Ttacker", role="participant",
        )

    # -- helpers ----------------------------------------------------------

    def _make_profile(self, user):
        return VibeRaisingProfile.objects.create(user=user, role=VibeRaisingProfile.ROLE_FOUNDER)

    def _make_founder(self, user, *, name, domain, created_at=None):
        profile = self._make_profile(user)
        company = VibeRaisingCompany.objects.create(profile=profile, name=name, domain=domain)
        if created_at is not None:
            # auto_now_add can't be set on create; first-claim-wins orders by
            # created_at, so pin victim earlier to make ownership deterministic.
            VibeRaisingCompany.objects.filter(pk=company.pk).update(created_at=created_at)
            company.refresh_from_db()
        profile.active_company = company
        profile.save(update_fields=["active_company", "updated_at"])
        return profile, company

    def _seed_victim_owning_acme_with_draft(self):
        earlier = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        profile, company = self._make_founder(
            self.victim, name="Acme Inc.", domain="acme.com", created_at=earlier,
        )
        organization = ensure_company_organization(company)  # links victim -> owner
        draft = MonthlyUpdateDraft.objects.create(
            organization=organization,
            month=date(2026, 5, 1),
            status=MonthlyUpdateDraftStatus.READY,
            structured_memo={"highlights": ["Victim secret highlight"]},
        )
        return profile, company, organization, draft

    # -- ownership primitive ---------------------------------------------

    def test_resolve_owned_organization_gates_by_owner_and_admin(self):
        _p, _c, organization, _d = self._seed_victim_owning_acme_with_draft()

        # Owner resolves their org; a non-owner resolves nothing (no leak).
        self.assertEqual(_resolve_owned_organization(user=self.victim, domain="acme.com"), organization)
        self.assertIsNone(_resolve_owned_organization(user=self.attacker, domain="acme.com"))

        # mlai admin bypasses ownership.
        admin = User.objects.create_user(
            email="admin@mlai.au", password="pw", first_name="Ad", last_name="Min", role="participant",
        )
        admin.is_staff = True
        admin.save(update_fields=["is_staff"])
        self.assertEqual(_resolve_owned_organization(user=admin, domain="acme.com"), organization)

    # -- claim guard ------------------------------------------------------

    def test_claiming_a_rivals_domain_is_rejected(self):
        self._seed_victim_owning_acme_with_draft()
        self._make_profile(self.attacker)  # founder profile, no company yet
        self.client.force_authenticate(user=self.attacker)

        resp = self.client.post(COMPANIES_URL, {"name": "Evil Inc.", "domain": "acme.com"}, format="json")

        self.assertEqual(resp.status_code, 409)
        self.assertFalse(
            VibeRaisingCompany.objects.filter(profile__user=self.attacker, domain="acme.com").exists()
        )

    def test_first_claim_of_a_fresh_domain_succeeds(self):
        self._make_profile(self.victim)
        self.client.force_authenticate(user=self.victim)

        resp = self.client.post(COMPANIES_URL, {"name": "Fresh Co", "domain": "fresh.io"}, format="json")

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            VibeRaisingCompany.objects.filter(profile__user=self.victim, domain="fresh.io").exists()
        )

    # -- read / write isolation (simulating legacy contamination) ---------

    def test_non_owner_cannot_read_another_tenants_updates(self):
        self._seed_victim_owning_acme_with_draft()
        # Legacy contamination: attacker's company already points at acme.com
        # (created later, so victim remains the first-claim owner).
        self._make_founder(self.attacker, name="Evil Inc.", domain="acme.com")

        self.client.force_authenticate(user=self.attacker)
        resp = self.client.get(UPDATES_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["updates"], [])

        # The legitimate owner still sees their own update.
        self.client.force_authenticate(user=self.victim)
        owner_resp = self.client.get(UPDATES_URL)
        self.assertEqual(owner_resp.status_code, 200)
        self.assertEqual(len(owner_resp.data["updates"]), 1)

    def test_non_owner_cannot_overwrite_another_tenants_updates(self):
        _p, _c, _org, draft = self._seed_victim_owning_acme_with_draft()
        self._make_founder(self.attacker, name="Evil Inc.", domain="acme.com")

        self.client.force_authenticate(user=self.attacker)
        resp = self.client.post(
            UPDATES_URL, {"month": "May", "year": 2026, "highlights": "pwned"}, format="json",
        )

        self.assertEqual(resp.status_code, 409)
        draft.refresh_from_db()
        self.assertEqual(draft.structured_memo.get("highlights"), ["Victim secret highlight"])

    def test_owner_can_write_and_read_their_update(self):
        self._make_founder(self.victim, name="Acme Inc.", domain="acme.com")
        self.client.force_authenticate(user=self.victim)

        post = self.client.post(
            UPDATES_URL, {"month": "May", "year": 2026, "highlights": "Closed two pilots"}, format="json",
        )
        self.assertIn(post.status_code, (200, 201))

        get = self.client.get(UPDATES_URL)
        self.assertEqual(get.status_code, 200)
        self.assertEqual(len(get.data["updates"]), 1)
