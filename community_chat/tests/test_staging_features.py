"""Focused validation and isolated existing-schema fixture behavior."""
import uuid
from unittest.mock import patch
from django.test import SimpleTestCase, TestCase, override_settings
from community_chat.volunteer.staging_fixtures import validate_receipt, synthetic_public_event, seed_features, NAMES
from community_chat.volunteer.events import public_event
HOST='chat-staging.mlai.au'
def receipt():
    names=NAMES
    return dict(version=1,community_host=HOST,manifest_sha256='a'*64,channels=[dict(name=n,id=('a613b1cf-5492-53e6-931d-94592926df78' if n=='general' else str(uuid.uuid5(uuid.NAMESPACE_DNS,HOST+'/public-channel/v1/'+n))),starter_event_id=hex(i)[2:].zfill(64)) for i,n in enumerate(names,1)])
class FixtureValidationTests(SimpleTestCase):
    def test_receipt_targets_and_event_never_falls_back_to_provider(self):
        self.assertEqual(len(validate_receipt(receipt())),17)
        for r in [dict(receipt(),community_host='chat.mlai.au'),dict(receipt(),channels=[])]:
            with self.assertRaises(ValueError):validate_receipt(r)
        r=receipt();r['channels'][0]['id']=str(uuid.uuid4())
        with self.assertRaises(ValueError):validate_receipt(r)
        with override_settings(SETTINGS_MODULE='mlai.settings_staging_chat',APP_ENV='chat-staging'):
            with patch('community_chat.volunteer.events.LumaAttendeeReportService') as provider:
                self.assertIsNone(public_event('unknown'))
                event=public_event('staging-volunteer-event-v1')
                self.assertEqual(event['id'],'staging-volunteer-event-v1')
                self.assertIn(HOST,event['url']);provider.assert_not_called()
        with override_settings(SETTINGS_MODULE='mlai.settings',APP_ENV='production'):
            self.assertIsNone(synthetic_public_event('staging-volunteer-event-v1'))

from community_chat.tests.test_staging_password import PILOT
from django.contrib.auth import get_user_model
from community_chat.models import CommunityMemberProfile
from roo.staging_fixtures import EMAILS, seed_user
from roo.models import Ledger, PointsAccount
from founder_tools.models import VibeRaisingCompany
from startup_updates.models import MonthlyUpdateDraft
@override_settings(**PILOT,STAGING_FIXTURE_DATABASE='staging_points_tests',COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED=True,COMMUNITY_CHAT_VOLUNTEER_COMMUNITY=HOST)
class FeatureDatabaseTests(TestCase):
    def setUp(self):
        self.network=patch('requests.sessions.Session.request',side_effect=AssertionError('External provider invoked'));self.network.start();self.addCleanup(self.network.stop)
        self.users=[]
        for email in EMAILS:
            user=get_user_model().objects.create_user(email=email,password='Synthetic-test-only-412!')
            CommunityMemberProfile.objects.create(user=user,status='approved',review_reasons=['synthetic_staging_pilot'])
            seed_user(user);self.users.append(user)
    def test_seed_is_additive_preserves_edits_private_drafts_roles_and_ledger(self):
        ledger=list(Ledger.objects.values_list('pk','delta','digital_delta_microroo'))
        dry=seed_features(receipt());self.assertTrue(dry['dry_run']);self.assertEqual(VibeRaisingCompany.objects.count(),0)
        first=seed_features(receipt(),apply=True);self.assertEqual(first['created'],21)
        self.assertEqual(VibeRaisingCompany.objects.count(),9);self.assertEqual(MonthlyUpdateDraft.objects.count(),9)
        company=VibeRaisingCompany.objects.first();company.name='Edited by tester';company.save(update_fields=['name'])
        replay=seed_features(receipt(),apply=True);self.assertEqual(replay['created'],0);company.refresh_from_db();self.assertEqual(company.name,'Edited by tester')
        self.assertEqual(list(Ledger.objects.values_list('pk','delta','digital_delta_microroo')),ledger)
        self.assertFalse(get_user_model().objects.filter(is_staff=True).exists());self.assertFalse(get_user_model().objects.filter(is_superuser=True).exists())
        for d in MonthlyUpdateDraft.objects.all():self.assertIsNone(d.published_revision_id);self.assertEqual(d.current_revision.audience,'private')
    def test_unmarked_or_admin_actor_cannot_seed_any_feature(self):
        self.users[0].is_staff=True;self.users[0].save(update_fields=['is_staff'])
        with self.assertRaises(ValueError):seed_features(receipt(),apply=True)
        self.assertEqual(VibeRaisingCompany.objects.count(),0)
