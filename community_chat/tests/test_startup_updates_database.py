"""Database integration tests. Require explicit approval of the migration inventory."""
from datetime import timedelta
import uuid
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from community_chat.account_sessions import issue_account_session
from community_chat.models import CommunityChatEmailCodeChallenge
from startup_updates.models import MonthlyUpdateApproval, MonthlyUpdateDraft

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

    def test_blank_update_cannot_be_approved(self):
        update = self.save(summary='')
        response = self.approve(update)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data['detail'], 'Write at least one section of your update before publishing.')
        self.assertIsNone(MonthlyUpdateDraft.objects.get(pk=update['id']).published_at)
        self.assertFalse(MonthlyUpdateApproval.objects.exists())

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

    def test_financial_history_save_preview_opt_in_and_cached_refresh_round_trip(self):
        from startup_updates.evidence_contract import content_hash
        from startup_updates.models import MonthlyEvidenceSnapshot
        from startup_updates.revisions import save_revision
        from startup_updates.tests_financial_history_unit import evidence_payload, replace_totals
        initial = self.save(month='August')
        draft = MonthlyUpdateDraft.objects.get(pk=initial['id'])
        payload = evidence_payload()
        snapshot = MonthlyEvidenceSnapshot.objects.create(organization=draft.organization, month=draft.month,
            payload=payload, content_hash=content_hash(payload))
        seeded = save_revision(draft, {'summary': 'We launched.'}, snapshot=snapshot,
            expected_revision=initial['revisionId'], validation={'groundedness_status': 'founder_asserted'})
        saved = self.save(month='August', updateId=draft.pk, expectedRevision=seeded.pk,
            metrics={}, audienceVisibility='community', financialHistorySelection={'enabled': True, 'shareAmounts': False})
        self.assertEqual(saved['financialHistorySelection'], {'enabled': True, 'shareAmounts': False})
        self.assertEqual(saved['financialHistory']['points'][-1]['revenue'], 100)
        detail = self.client.get(BASE + f'updates/{draft.pk}/', {'company_id': self.company})
        self.assertEqual(detail.data['communityPreview']['financialHistory'], saved['financialHistory'])
        self.assertNotIn('evidenceSnapshot', detail.data['communityPreview'])
        self.assertEqual(self.approve(saved).status_code, 200)
        published = self.client.get(BASE + 'community/').data['updates'][0]
        fresh_payload = evidence_payload()
        replace_totals(fresh_payload, -1, 9000, 300)
        latest = MonthlyEvidenceSnapshot.objects.create(organization=draft.organization, month=draft.month,
            payload=fresh_payload, content_hash=content_hash(fresh_payload))
        with patch('startup_updates.revisions.capture_snapshot', return_value=latest) as capture:
            amounts = self.save(month='August', updateId=draft.pk, expectedRevision=saved['revisionId'],
                metrics={}, audienceVisibility='community', financialHistorySelection={'enabled': True, 'shareAmounts': True})
            capture.assert_not_called()
            self.assertEqual(amounts['financialHistory']['points'][-1]['revenue'], 600)
            refreshed = self.save(month='August', updateId=draft.pk, expectedRevision=amounts['revisionId'],
                metrics={}, audienceVisibility='community', financialHistorySelection={'enabled': True, 'shareAmounts': True, 'refresh': True})
            capture.assert_called_once_with(draft.organization, draft.month)
        self.assertEqual(refreshed['financialHistory']['points'][-1]['revenue'], 9000)
        self.assertEqual(self.client.get(BASE + 'community/').data['updates'][0], published)
        detail = self.client.get(BASE + f'updates/{draft.pk}/', {'company_id': self.company})
        self.assertEqual(detail.data['communityPreview']['financialHistory'], refreshed['financialHistory'])
        self.assertEqual(self.approve(refreshed).status_code, 200)
        self.assertEqual(self.client.get(BASE + 'community/').data['updates'][0]['financialHistory']['points'][-1]['revenue'], 9000)
