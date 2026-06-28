from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from integrations.models import GoogleConnection
from integrations.services.external_connectors import (
    active_google_connection,
    google_connection_for_org,
)
from organizations.models import Organization

User = get_user_model()


class GmailOrgScopingTests(TestCase):
    """Phase 2: Gmail is per-startup — a founder can connect a separate mailbox
    per startup, and resolution follows the active startup."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="multi-gmail@example.com",
            password="password",
            first_name="Multi",
            last_name="Gmail",
            role="participant",
        )
        self.org_a = Organization.objects.create(name="Alpha", domain="alpha.example")
        self.org_b = Organization.objects.create(name="Beta", domain="beta.example")
        self.profile = VibeRaisingProfile.objects.create(
            user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER
        )
        self.company_a = VibeRaisingCompany.objects.create(
            profile=self.profile, name="Alpha", domain="alpha.example", organization=self.org_a
        )
        self.company_b = VibeRaisingCompany.objects.create(
            profile=self.profile, name="Beta", domain="beta.example", organization=self.org_b
        )
        self._set_active(self.company_a)

    def _set_active(self, company):
        self.profile.active_company = company
        self.profile.save(update_fields=["active_company", "updated_at"])

    def _connect(self, organization, email):
        return GoogleConnection.objects.create(
            user=self.user,
            organization=organization,
            google_email=email,
            refresh_token="token",
            scope="https://www.googleapis.com/auth/gmail.readonly",
        )

    def test_separate_mailbox_per_startup(self):
        conn_a = self._connect(self.org_a, "founder+alpha@example.com")
        conn_b = self._connect(self.org_b, "founder+beta@example.com")
        self.assertNotEqual(conn_a.id, conn_b.id)
        self.assertEqual(google_connection_for_org(self.user, self.org_a), conn_a)
        self.assertEqual(google_connection_for_org(self.user, self.org_b), conn_b)

    def test_active_connection_follows_active_company(self):
        conn_a = self._connect(self.org_a, "a@example.com")
        conn_b = self._connect(self.org_b, "b@example.com")

        self._set_active(self.company_a)
        self.assertEqual(active_google_connection(self.user), conn_a)
        self._set_active(self.company_b)
        self.assertEqual(active_google_connection(self.user), conn_b)

    def test_active_connection_does_not_bleed_to_sibling(self):
        # Only startup A has Gmail; viewing startup B must not see A's mailbox.
        self._connect(self.org_a, "a@example.com")
        self._set_active(self.company_b)
        self.assertIsNone(active_google_connection(self.user))

    def test_adopt_unassigned_claims_legacy_connection(self):
        legacy = self._connect(None, "legacy@example.com")  # org-less, pre-company
        adopted = google_connection_for_org(self.user, self.org_a, adopt_unassigned=True)
        self.assertEqual(adopted.id, legacy.id)
        legacy.refresh_from_db()
        self.assertEqual(legacy.organization_id, self.org_a.id)
        # A second org does not re-claim the now-owned connection.
        self.assertIsNone(google_connection_for_org(self.user, self.org_b, adopt_unassigned=True))

    def test_unique_constraint_one_mailbox_per_user_org(self):
        self._connect(self.org_a, "a@example.com")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._connect(self.org_a, "a2@example.com")
