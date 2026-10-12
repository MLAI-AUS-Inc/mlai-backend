import uuid
from urllib.parse import urlsplit, parse_qs
from unittest.mock import patch
from coincurve import PrivateKey
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

ROOT='/api/v1/community-chat/'
STAGE='https://chat-staging.mlai.au'
ISSUER='https://api.mlai.au'


@override_settings(ROOT_URLCONF='mlai.urls_staging_chat',
    COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED=True, COMMUNITY_CHAT_HOST_ONLY_COOKIES=True,
    COMMUNITY_CHAT_CLEAR_LEGACY_COOKIES=False,
    COMMUNITY_CHAT_ACCESS_COOKIE_NAME='__Host-mlai_staging_access',
    COMMUNITY_CHAT_REFRESH_COOKIE_NAME='__Host-mlai_staging_refresh',
    COMMUNITY_CHAT_FEDERATION_CLIENT_SECRET='synthetic-confidential-client-credential',
    COMMUNITY_CHAT_FEDERATION_ISSUER=ISSUER,
    COMMUNITY_CHAT_FRONTEND_URL=STAGE, COMMUNITY_CHAT_ALLOWED_ORIGINS=[STAGE],
    COMMUNITY_CHAT_RELAY_URL='wss://chat-staging.mlai.au', COMMUNITY_CHAT_SIGNUP_ENABLED=False,
    COMMUNITY_CHAT_SESSION_ACCESS_TTL_SECONDS=300)
class FederationStagingTests(TestCase):
    def setUp(self):
        self.client=APIClient()
        self.client.credentials(HTTP_ORIGIN=STAGE)
        self.key=PrivateKey.from_int(91).public_key.format(compressed=True)[1:].hex()
        self.installation=str(uuid.uuid4())
        self.subject=str(uuid.uuid4())

    def identity(self, **changes):
        result=dict(issuer=ISSUER, subject=self.subject, display_name='Approved Tester', eligible=True)
        result.update(changes)
        return result

    def start(self):
        request_id=str(uuid.uuid4())
        with patch('community_chat.federation_relying.issuer_post', return_value=dict(
                request_id=request_id, authorization_url='https://chat.mlai.au/auth/staging?request_id='+request_id)) as upstream:
            response=self.client.post(ROOT+'federation/start/', dict(public_key=self.key,
                installation_id=self.installation, platform='web', name='Staging Browser'), format='json')
        self.assertEqual(response.status_code,201)
        state=upstream.call_args.args[1]['state']
        return state

    def complete(self, **identity_changes):
        state=self.start()
        with patch('community_chat.federation_relying.issuer_post',return_value=self.identity(**identity_changes)):
            return self.client.get(ROOT+'federation/callback/',dict(code='c'*64,state=state))

    def test_handoff_creates_local_unprivileged_user_and_separate_cookies(self):
        response=self.complete()
        self.assertEqual(response.status_code,302)
        self.assertEqual(response['Location'],STAGE+'/auth/staging/complete')
        self.assertIn('__Host-mlai_staging_access',response.cookies)
        self.assertNotIn('mlai_chat_access',response.cookies)
        user=get_user_model().objects.get()
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(user.full_name,'Approved Tester')
        response=self.client.get(ROOT+'account/')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data['session']['public_key'],self.key)

    def test_wrong_state_cannot_exchange_or_issue_a_session(self):
        self.start()
        with patch('community_chat.federation_relying.issuer_post') as upstream:
            response=self.client.get(ROOT+'federation/callback/',dict(code='c'*64,state='wrong'))
        self.assertEqual(response.status_code,400)
        upstream.assert_not_called()
        self.assertFalse(get_user_model().objects.exists())

    def test_missing_transaction_cookie_fails_closed(self):
        state=self.start()
        self.client.cookies.clear()
        with patch('community_chat.federation_relying.issuer_post') as upstream:
            response=self.client.get(ROOT+'federation/callback/',dict(code='c'*64,state=state))
        self.assertEqual(response.status_code,400)
        upstream.assert_not_called()

    def test_callback_replay_cannot_create_second_session(self):
        state=self.start()
        with patch('community_chat.federation_relying.issuer_post',return_value=self.identity()):
            self.assertEqual(self.client.get(ROOT+'federation/callback/',dict(code='c'*64,state=state)).status_code,302)
            self.assertEqual(self.client.get(ROOT+'federation/callback/',dict(code='c'*64,state=state)).status_code,400)
        from community_chat.models import CommunityChatAccountSession
        self.assertEqual(CommunityChatAccountSession.objects.count(),1)

    def test_wrong_issuer_and_ineligible_identity_are_rejected(self):
        for changes in ({'issuer':'https://evil.example'}, {'eligible':False}, {'subject':'../bad'}):
            with self.subTest(changes=changes):
                self.assertEqual(self.complete(**changes).status_code,403)
        self.assertFalse(get_user_model().objects.exists())

    def test_secondary_production_routes_are_absent(self):
        for route in ('account/deletion/','auth/password/','account/onboarding/','slack/','startups/','coding/','admins/key/'):
            self.assertEqual(self.client.post(ROOT+route,{},format='json').status_code,404)
        self.assertEqual(self.client.get('/api/v1/auth/me/').status_code,404)

    def test_refresh_denies_removed_tester_and_upstream_outage(self):
        self.assertEqual(self.complete().status_code,302)
        with patch('community_chat.federation_relying.issuer_post',return_value=self.identity(eligible=False)):
            self.assertEqual(self.client.post(ROOT+'auth/session/refresh/',{},format='json').status_code,401)

    def test_production_cookie_names_are_not_accepted_by_staging(self):
        self.assertEqual(self.complete().status_code,302)
        token=self.client.cookies['__Host-mlai_staging_access'].value
        self.client.cookies.clear()
        self.client.cookies['__Host-mlai_chat_access']=token
        self.assertEqual(self.client.get(ROOT+'account/').status_code,401)

    def test_refresh_outage_keeps_current_cookie_and_denies_refresh(self):
        self.assertEqual(self.complete().status_code,302)
        from community_chat.federation import FederationUnavailable
        with patch('community_chat.federation_relying.issuer_post',side_effect=FederationUnavailable):
            response=self.client.post(ROOT+'auth/session/refresh/',{},format='json')
        self.assertEqual(response.status_code,503)
        self.assertFalse(response.cookies)

    def test_revoke_device_changes_only_local_membership_and_credentials(self):
        self.assertEqual(self.complete().status_code,302)
        from community_chat.models import CommunityChatDevice,CommunityChatAccountSession
        user=get_user_model().objects.get()
        CommunityChatDevice.objects.create(user=user,public_key=self.key,installation_id=self.installation,status='verified')
        with patch('community_chat.adapter.revoke_relay_membership',return_value=('revoked',uuid.uuid4())):
            response=self.client.delete(ROOT+'devices/'+self.key+'/',{},format='json')
        self.assertEqual(response.status_code,200)
        self.assertFalse(CommunityChatAccountSession.objects.filter(revoked_at__isnull=True).exists())
        self.assertEqual(self.client.get(ROOT+'account/').status_code,401)

    def test_session_is_five_minutes_and_cannot_cross_staging_origin(self):
        self.assertEqual(self.complete().status_code,302)
        from community_chat.models import CommunityChatAccountSession
        session=CommunityChatAccountSession.objects.get()
        self.assertLessEqual((session.access_expires_at-session.created_at).total_seconds(),300)
        self.client.credentials(HTTP_ORIGIN='https://chat.mlai.au')
        self.assertEqual(self.client.post(ROOT+'bootstrap/challenge/',{'public_key':self.key},format='json').status_code,401)

    def test_operator_removal_disables_local_account_even_when_adapter_fails(self):
        self.assertEqual(self.complete().status_code,302)
        from django.core.management import call_command
        from django.core.management.base import CommandError
        from io import StringIO
        from community_chat.models import CommunityChatDevice
        from community_chat.adapter import MembershipAdapterUnavailable
        user=get_user_model().objects.get()
        CommunityChatDevice.objects.create(user=user,public_key=self.key,installation_id=self.installation,status='verified')
        with patch('community_chat.adapter.revoke_relay_membership',side_effect=MembershipAdapterUnavailable):
            with self.assertRaises(CommandError):
                call_command('remove_staging_identity',subject=self.subject,apply=True,stdout=StringIO())
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_signed_key_proof_enrolls_only_through_staging_adapter(self):
        self.assertEqual(self.complete().status_code,302)
        from community_chat.tests.test_api import sign_challenge
        from community_chat.adapter import IssuedInvite, RelayMembership
        from django.utils import timezone
        from datetime import timedelta
        challenge=self.client.post(ROOT+'bootstrap/challenge/',{'public_key':self.key},format='json')
        self.assertEqual(challenge.status_code,201)
        proof=sign_challenge(PrivateKey.from_int(91),challenge.data)
        with patch('community_chat.views.issue_member_invite',return_value=IssuedInvite('test-invite','test-id',timezone.now()+timedelta(minutes=5),uuid.uuid4())):
            invite=self.client.post(ROOT+'bootstrap/invite/',dict(challenge_id=challenge.data['challenge_id'],nonce=challenge.data['nonce'],event=proof),format='json')
        self.assertEqual(invite.status_code,200)
        with patch('community_chat.adapter.get_relay_membership',return_value=RelayMembership(True,'member',timezone.now())):
            confirmed=self.client.post(ROOT+'bootstrap/confirm/',{'public_key':self.key},format='json')
        self.assertEqual(confirmed.status_code,200)
        self.assertEqual(confirmed.data['device']['status'],'verified')

    def test_missing_refresh_cookie_is_unauthorized(self):
        self.assertEqual(self.client.post(ROOT+'auth/session/refresh/',{},format='json').status_code,401)
