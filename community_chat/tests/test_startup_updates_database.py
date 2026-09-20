"""Database integration tests. Require explicit approval of the migration inventory."""
from datetime import timedelta
import uuid
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from community_chat.account_sessions import issue_account_session
from community_chat.models import CommunityChatEmailCodeChallenge

BASE = '/api/v1/community-chat/startups/'
ORIGIN = 'https://chat.example'


@override_settings(COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED=True, COMMUNITY_CHAT_ALLOWED_ORIGINS=[ORIGIN])
class ChatStartupJourneyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email='startup-chat@example.invalid', first_name='Founder')
        challenge = CommunityChatEmailCodeChallenge.objects.create(
            user=self.user, email_digest='a' * 64, code_digest='b' * 64,
            client_id='mlai-chat-web', installation_id=uuid.uuid4(), origin=ORIGIN,
            platform='web', device_name='Test browser', public_key='c' * 64,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        self.credentials = issue_account_session(self.user, challenge)
        self.client = APIClient()
        self.client.cookies['mlai_chat_access'] = self.credentials.access_token
        self.client.credentials(HTTP_ORIGIN=ORIGIN)
        response = self.client.post(BASE + 'companies/', {'name': 'Chat Startup', 'createNew': True,
            'shortDescription': 'A domainless startup', 'audienceVisibility': 'just_me'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.company = response.data['id']

    def save(self, **extra):
        response = self.client.post(BASE + 'updates/', {'companyId': self.company, 'month': 'March',
            'year': 2026, 'summary': 'We launched.', 'metrics': {'customerInterviews': '0'},
            'saveMode': 'draft', **extra}, format='json')
        self.assertIn(response.status_code, (200, 201), response.data)
        return response.data['update']

    def approve(self, update):
        return self.client.post(BASE + f'updates/{update["id"]}/publish/', {
            'companyId': self.company, 'reviewed': True, 'revisionId': update['revisionId'],
            'revisionHash': update['revisionHash'], 'audienceVisibility': update['audienceVisibility'],
        }, format='json')

    def test_private_journey_and_revocation(self):
        update = self.save()
        self.assertEqual(update['metrics']['customerInterviews'], '0')
        detail = self.client.get(BASE + f'updates/{update["id"]}/', {'company_id': self.company})
        self.assertEqual(detail.data['update']['validation']['groundedness_status'], 'founder_asserted')
        self.assertEqual(self.approve(update).status_code, 200)
        self.assertEqual(self.client.get(BASE + 'community/').data['updates'], [])
        self.credentials.session.revoked_at = timezone.now()
        self.credentials.session.save(update_fields=['revoked_at'])
        self.assertEqual(self.client.get(BASE + 'bootstrap/').status_code, 401)

    def test_community_uses_approved_revision_and_omits_private_inputs(self):
        update = self.save(audienceVisibility='community', manualSummary='PRIVATE SOURCE NOTES')
        self.assertEqual(self.approve(update).status_code, 200)
        revised = self.save(expectedRevision=update['revisionId'], summary='Private revision under review.')
        self.assertNotEqual(update['revisionId'], revised['revisionId'])
        self.assertEqual(self.approve(update).status_code, 409)
        community = self.client.get(BASE + 'community/').data['updates'][0]
        self.assertEqual(community['summary'], 'We launched.')
        self.assertEqual(community['revisionHash'], update['revisionHash'])
        self.assertNotIn('manualSummary', community)
        self.assertNotIn('evidenceSnapshot', community)
        self.assertNotIn('PRIVATE SOURCE NOTES', str(community))

    def test_sibling_company_cannot_read_or_approve_update(self):
        update = self.save()
        other = self.client.post(BASE + 'companies/', {'name': 'Sibling', 'createNew': True}, format='json').data['id']
        self.assertEqual(self.client.get(BASE + f'updates/{update["id"]}/', {'company_id': other}).status_code, 404)
        self.company = other
        self.assertEqual(self.approve(update).status_code, 404)

    def test_cookie_mutation_requires_matching_origin(self):
        self.client.credentials(HTTP_ORIGIN='https://untrusted.invalid')
        response = self.client.post(BASE + 'companies/', {'name': 'Untrusted'}, format='json')
        self.assertEqual(response.status_code, 401)

    def test_imported_financial_values_cannot_be_replaced_by_manual_input(self):
        response = self.client.post(BASE + 'updates/', {
            'companyId': self.company, 'month': 'March', 'year': 2026,
            'summary': 'We launched.', 'metrics': {'revenue': '100'}, 'saveMode': 'draft',
        }, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('read-only', str(response.data))

    def test_explicit_update_identity_and_operating_metric_clearing(self):
        update = self.save(creationKey=str(uuid.uuid4()), updateDate='2026-03-12')
        edited = self.save(updateId=update['id'], expectedRevision=update['revisionId'],
            metrics={'customerInterviews': ''}, summary='Corrected operating update.')
        self.assertEqual(edited['id'], update['id'])
        self.assertNotEqual(edited['revisionId'], update['revisionId'])
        self.assertNotIn('customerInterviews', edited['metrics'])
