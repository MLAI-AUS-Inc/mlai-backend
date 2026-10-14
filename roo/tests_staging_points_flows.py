"""Persistent synthetic fixtures and local purchases preserve domain accounting."""
from django.test import TestCase,override_settings
from django.contrib.auth import get_user_model
from community_chat.models import CommunityMemberProfile
from community_chat.tests.test_staging_password import PILOT
from roo.models import PointsAccount,Ledger
from roo.services import PointsService

@override_settings(**PILOT,STAGING_FIXTURE_DATABASE='staging_points_tests')
class FixtureTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user(email='ashwin@staging.mlai.au',password='Private-synthetic-424!')
        CommunityMemberProfile.objects.create(user=self.user,status='approved',review_reasons=['synthetic_staging_pilot'])
    def test_seed_repeat_preserves_spend_and_profile(self):
        from roo.staging_fixtures import seed_user,seed_catalogue
        seed_user(self.user);seed_catalogue()
        self.assertEqual(PointsService.get_available_microroo(self.user),12_000_000)
        self.assertEqual(PointsAccount.objects.get(user=self.user).digital_balance_microroo,24_000_000)
        PointsService.spend(user=self.user,delta=1,source='MEETING_ROOM',description='Test spending',created_by_slack_id='STAGING',idempotency_key='test-spend')
        self.user.first_name='Edited';self.user.save()
        seed_user(self.user);seed_catalogue()
        self.assertEqual(PointsService.get_available_microroo(self.user),11_000_000)
        self.assertEqual(PointsAccount.objects.get(user=self.user).digital_balance_microroo,24_000_000)
        self.user.refresh_from_db();self.assertEqual(self.user.first_name,'Edited')
        self.assertEqual(Ledger.objects.filter(user=self.user,source='MANUAL').count(),2)
    def test_unmarked_or_foreign_fixture_refused(self):
        from roo.staging_fixtures import seed_user
        CommunityMemberProfile.objects.filter(user=self.user).delete()
        with self.assertRaises(ValueError):seed_user(self.user)
    def test_purchase_paid_replay_and_owner_deny(self):
        from roo.staging_fixtures import seed_user
        from roo.staging_payments import create_staging_purchase,resolve_staging_purchase
        seed_user(self.user)
        purchase=create_staging_purchase(self.user,'topup_5','ad54a441-5667-4e50-94d1-025a823e42d0',True)
        self.assertIsNone(purchase.stripe_checkout_session_id)
        second=get_user_model().objects.create_user(email='other@staging.mlai.au')
        with self.assertRaises(ValueError):resolve_staging_purchase(second,purchase.pk,'paid')
        resolve_staging_purchase(self.user,purchase.pk,'paid');resolve_staging_purchase(self.user,purchase.pk,'paid')
        self.assertEqual(PointsService.get_available_microroo(self.user),22_000_000)
        with self.assertRaises(ValueError):resolve_staging_purchase(self.user,purchase.pk,'cancelled')
    def test_reward_manual_pending_auto_debit_and_exact_retry(self):
        from roo.staging_fixtures import seed_user,seed_catalogue
        from roo.staging_member_views import RewardsView
        from rest_framework.test import APIRequestFactory,force_authenticate
        seed_user(self.user);seed_catalogue()
        factory=APIRequestFactory()
        def redeem(code,key,quantity=1):
            request=factory.post('/api/',{'reward_code':code,'client_request_id':key,'quantity':quantity},format='json');force_authenticate(request,user=self.user)
            return RewardsView.as_view()(request)
        key='ad54a441-5667-4e50-94d1-025a823e42d0'
        first=redeem('STAGING_AUTO',key);self.assertEqual(first.status_code,200)
        self.assertEqual(redeem('STAGING_AUTO',key).data['id'],first.data['id'])
        self.assertEqual(redeem('STAGING_AUTO',key,2).status_code,409)
        self.assertEqual(PointsService.get_available_microroo(self.user),11_000_000)
        manual=redeem('STAGING_MANUAL','bd54a441-5667-4e50-94d1-025a823e42d0')
        self.assertEqual(manual.data['status'],'requested')
        self.assertEqual(PointsService.get_available_microroo(self.user),11_000_000)
    def test_eight_account_provision_replay_preserves_passwords_and_pilot(self):
        import tempfile,json,io
        from pathlib import Path
        from django.core.management import call_command
        from roo.staging_fixtures import EMAILS
        from django.core.management.base import CommandError
        # Existing marked Ashwin uses the same private fixture password.
        with tempfile.TemporaryDirectory() as directory:
            rows=[]
            for i,email in enumerate(EMAILS):
                password=Path(directory)/f'password-{i}'
                password.write_text('Private-synthetic-424!' if i==0 else f'Private-synthetic-{i}-947!');password.chmod(0o600)
                rows.append({'email':email,'first_name':'Synthetic','last_name':'Tester','password_file':str(password)})
            manifest=Path(directory)/'manifest.json';manifest.write_text(json.dumps(rows));manifest.chmod(0o600)
            pilot=get_user_model().objects.create_user(email='pilot@staging.mlai.au',password='Existing-pilot-982!')
            original=pilot.password
            for _ in range(2):
                call_command('provision_staging_pilot',manifest=str(manifest),apply=True,preserve_existing=True,stdout=io.StringIO())
                call_command('seed_staging_points',manifest=str(manifest),apply=True,stdout=io.StringIO())
            self.assertEqual(get_user_model().objects.filter(email__in=EMAILS).count(),8)
            hashes=set(get_user_model().objects.filter(email__in=EMAILS).values_list('password',flat=True));self.assertEqual(len(hashes),8)
            pilot.refresh_from_db();self.assertEqual(pilot.password,original)
            self.assertFalse(PointsAccount.objects.filter(user=pilot).exists())
            CommunityMemberProfile.objects.filter(user=self.user).update(review_reasons=[])
            with self.assertRaises(CommandError):call_command('provision_staging_pilot',manifest=str(manifest),apply=True,preserve_existing=True,stdout=io.StringIO())
