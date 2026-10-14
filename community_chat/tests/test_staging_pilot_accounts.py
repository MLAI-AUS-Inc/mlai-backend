import json
import tempfile
import uuid
from unittest.mock import patch
from pathlib import Path
from io import StringIO
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from community_chat.models import CommunityMemberConsent, CommunityMemberProfile
from community_chat.models import CommunityChatDevice,CommunityChatAccountSession,CommunityChatBootstrapToken
from community_chat.adapter import MembershipAdapterUnavailable
from .test_staging_password import PILOT

@override_settings(**PILOT)
class StagingPilotAccountsTests(TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.manifest=Path(self.temporary.name)/'accounts.json'
        self.secret=Path(self.temporary.name)/'password'
        self.secret.write_text('Synthetic-private-pilot-472!\n');self.secret.chmod(0o600)
        self.write_manifest()

    def write_manifest(self,**changes):
        item=dict(email='pilot@staging.mlai.au',first_name='Staging',last_name='Pilot',password_file=str(self.secret))
        item.update(changes)
        self.manifest.write_text(json.dumps([item]));self.manifest.chmod(0o600)

    def run_command(self,**kwargs):
        output=StringIO();call_command('provision_staging_pilot',manifest=str(self.manifest),stdout=output,**kwargs)
        self.assertNotIn(self.secret.read_text().strip(),output.getvalue());return output.getvalue()

    def test_preview_then_apply_creates_only_ordinary_account_without_consent(self):
        self.assertIn('No changes',self.run_command())
        self.assertIn('No changes',self.run_command());self.assertFalse(get_user_model().objects.exists())
        self.run_command(apply=True)
        user=get_user_model().objects.get(email='pilot@staging.mlai.au')
        self.assertTrue(user.check_password(self.secret.read_text().strip()))
        self.assertFalse(user.is_staff);self.assertFalse(user.is_superuser)
        profile=CommunityMemberProfile.objects.get(user=user)
        self.assertEqual(profile.status,'approved');self.assertIsNone(profile.adult_confirmed_at)
        self.assertFalse(CommunityMemberConsent.objects.exists())
        with self.assertRaises(CommandError):self.run_command(apply=True)

    def test_unsafe_secret_manifest_roles_existing_and_duplicates_refused(self):
        self.secret.chmod(0o644)
        with self.assertRaises(CommandError):self.run_command(apply=True)
        self.secret.chmod(0o600);self.manifest.chmod(0o644)
        with self.assertRaises(CommandError):self.run_command(apply=True)
        self.write_manifest(is_staff=True)
        with self.assertRaises(CommandError):self.run_command(apply=True)
        self.write_manifest();row=json.loads(self.manifest.read_text())[0]
        self.manifest.write_text(json.dumps([row,row]))
        with self.assertRaises(CommandError):self.run_command(apply=True)
        self.assertFalse(get_user_model().objects.exists())

    @override_settings(ROOT_URLCONF='mlai.urls')
    def test_production_profile_refused(self):
        with self.assertRaises(CommandError):self.run_command(apply=True)

    def test_operator_removal_fails_closed_and_cleanup_can_be_retried(self):
        self.run_command(apply=True)
        user=get_user_model().objects.get(email='pilot@staging.mlai.au')
        CommunityChatDevice.objects.create(user=user,public_key='1'*64,installation_id=uuid.uuid4(),status='verified')
        output=StringIO()
        with patch('community_chat.adapter.revoke_relay_membership',side_effect=MembershipAdapterUnavailable):
            with self.assertRaises(CommandError):call_command('remove_staging_identity',email=user.email,apply=True,stdout=output)
        user.refresh_from_db();self.assertFalse(user.is_active)
        with patch('community_chat.adapter.revoke_relay_membership',return_value=('revoked',uuid.uuid4())):
            call_command('remove_staging_identity',email=user.email,apply=True,stdout=output)
        self.assertFalse(CommunityChatDevice.objects.filter(revoked_at__isnull=True).exists())
