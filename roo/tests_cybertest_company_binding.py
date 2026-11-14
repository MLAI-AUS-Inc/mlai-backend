"""Company mapping acceptance; disposable database only."""
import uuid
from django.test import TestCase, override_settings
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone
from core.models import User
from founder_tools.models import VibeRaisingProfile, VibeRaisingCompany
from roo.cybertest_models import CybertestCompanyBinding
from roo.cybertest_entry_views import workspace_for_company
from roo.cybertest_entry import EntryError

class CompanyBindingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email='binding@example.invalid')
        self.profile = VibeRaisingProfile.objects.create(user=self.user)
        self.company = VibeRaisingCompany.objects.create(profile=self.profile, name='Binding test')
        self.workspace = uuid.uuid4()

    def create(self, **kwargs):
        return CybertestCompanyBinding.objects.create(company=self.company,
            created_by=self.user, workspace_id=self.workspace, **kwargs)

    @override_settings(CYBERTEST_PERSISTENT_BINDINGS_ENABLED=True)
    def test_inactive_and_missing_mapping_are_denied(self):
        with self.assertRaises(EntryError): workspace_for_company(self.company)
        row = self.create()
        self.assertFalse(row.is_active)
        with self.assertRaises(EntryError): workspace_for_company(self.company)

    @override_settings(CYBERTEST_PERSISTENT_BINDINGS_ENABLED=True,
        CYBERTEST_LOCAL_COMPANY_WORKSPACES={})
    def test_verified_mapping_survives_reload_and_revocation(self):
        row = self.create(is_active=True, verified_at=timezone.now())
        self.assertEqual(workspace_for_company(self.company), str(row.workspace_id))
        CybertestCompanyBinding.objects.filter(pk=row.pk).update(is_active=False)
        with self.assertRaises(EntryError): workspace_for_company(self.company)

    def test_database_rejects_activation_without_verification(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.create(is_active=True)

    def test_mapping_identity_is_unique_and_provenance_protected(self):
        self.create()
        with self.assertRaises(IntegrityError), transaction.atomic(): self.create()
        other = VibeRaisingCompany.objects.create(profile=self.profile, name='Other')
        with self.assertRaises(IntegrityError), transaction.atomic():
            CybertestCompanyBinding.objects.create(company=other,
                created_by=self.user, workspace_id=self.workspace)
        with self.assertRaises(ProtectedError): self.company.delete()
        with self.assertRaises(ProtectedError): self.user.delete()

    @override_settings(DEBUG=False, CYBERTEST_PERSISTENT_BINDINGS_ENABLED=False,
        CYBERTEST_ENTRY_LOCAL_ENABLED=True)
    def test_fixture_mapping_cannot_be_used_outside_debug(self):
        with override_settings(CYBERTEST_LOCAL_COMPANY_WORKSPACES={str(self.company.pk): str(self.workspace)}):
            with self.assertRaises(EntryError): workspace_for_company(self.company)

    @override_settings(DEBUG=True, CYBERTEST_ENTRY_LOCAL_ENABLED=True,
        CYBERTEST_PERSISTENT_BINDINGS_ENABLED=False)
    def test_local_fixture_remains_explicit(self):
        with override_settings(CYBERTEST_LOCAL_COMPANY_WORKSPACES={str(self.company.pk): str(self.workspace)}):
            self.assertEqual(workspace_for_company(self.company), str(self.workspace))

    @override_settings(DEBUG=True, CYBERTEST_ENTRY_LOCAL_ENABLED=True,
        CYBERTEST_PERSISTENT_BINDINGS_ENABLED=True)
    def test_persistent_mode_never_falls_back_to_fixture(self):
        with override_settings(CYBERTEST_LOCAL_COMPANY_WORKSPACES={str(self.company.pk): str(self.workspace)}):
            with self.assertRaises(EntryError): workspace_for_company(self.company)

    @override_settings(CYBERTEST_PERSISTENT_BINDINGS_ENABLED=True,
        CYBERTEST_ENTRY_PRODUCTION_ENABLED=True, CYBERTEST_PRODUCTION_ALL_COMPANIES=True)
    def test_general_access_creates_one_company_owned_binding_and_never_reactivates(self):
        first = workspace_for_company(self.company)
        self.assertEqual(workspace_for_company(self.company), first)
        row = CybertestCompanyBinding.objects.get(company=self.company)
        self.assertEqual(row.created_by, self.user)
        self.assertTrue(row.is_active)
        self.assertIsNotNone(row.verified_at)
        other = VibeRaisingCompany.objects.create(profile=self.profile, name='Separate company')
        self.assertNotEqual(workspace_for_company(other), first)
        row.is_active = False
        row.save(update_fields=['is_active'])
        with self.assertRaises(EntryError): workspace_for_company(self.company)
        row.refresh_from_db()
        self.assertFalse(row.is_active)

    @override_settings(CYBERTEST_PERSISTENT_BINDINGS_ENABLED=True,
        CYBERTEST_ENTRY_PRODUCTION_ENABLED=True, CYBERTEST_PRODUCTION_ALL_COMPANIES=True)
    def test_general_access_never_replaces_existing_pending_binding(self):
        row = self.create()
        with self.assertRaises(EntryError): workspace_for_company(self.company)
        row.refresh_from_db()
        self.assertFalse(row.is_active)
        self.assertEqual(row.workspace_id, self.workspace)
