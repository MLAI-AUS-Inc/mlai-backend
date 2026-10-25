"""Real HTTP/ORM own-AI journey on an explicitly approved disposable database."""
import copy
from datetime import timedelta
import json
from pathlib import Path
import time
import uuid

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from django.utils.html import escape
from rest_framework.test import APIClient

from community_chat.account_sessions import issue_account_session
from community_chat.models import CommunityChatEmailCodeChallenge
from startup_updates.mcp import config, oauth
from startup_updates.models import MonthlyUpdateApproval, MonthlyUpdateDraft, StartupProfile
from vibe_raising.models import VibeRaisingCompany

BASE = '/api/v1/community-chat/startups/'
ORIGIN = 'https://chat.example'
FIXTURE = json.loads((Path(__file__).parent / 'fixtures/pulse_readability_sample.json').read_text())
# The dedicated runner may export this synthetic trace after the suite succeeds.
SAMPLE_FLOW = {}


@override_settings(COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED=True,
    COMMUNITY_CHAT_ALLOWED_ORIGINS=[ORIGIN], COMMUNITY_CHAT_FRONTEND_URL=ORIGIN,
    VALLEY_MCP_ENABLED=True, VALLEY_MCP_PUBLIC_BASE_URL='https://api.example.test',
    VALLEY_MCP_ALLOWED_ORIGINS=[], VALLEY_MCP_UNIFIED_ALIAS_ENABLED=False, DEBUG=True)
class OwnAiReadabilityJourneyTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(email='routenest-founder@example.invalid', first_name='Fictional founder')
        challenge = CommunityChatEmailCodeChallenge.objects.create(
            user=self.user, email_digest='a' * 64, code_digest='b' * 64,
            client_id='mlai-chat-web', installation_id=uuid.uuid4(), origin=ORIGIN,
            platform='web', device_name='Synthetic test browser', public_key='c' * 64,
            expires_at=timezone.now() + timedelta(minutes=10))
        self.credentials = issue_account_session(self.user, challenge)
        self.client = APIClient()
        self.client.cookies['mlai_chat_access'] = self.credentials.access_token
        self.client.credentials(HTTP_ORIGIN=ORIGIN)
        startup = FIXTURE['startup']
        response = self.client.post(BASE + 'companies/', {
            'name': 'RouteNest', 'createNew': True, 'shortDescription': startup['short_description'],
            'problemSolved': startup['problem_solved'], 'targetAudience': startup['target_audience'],
            'audienceVisibility': 'just_me'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.company = response.data['id']
        self.organization = VibeRaisingCompany.objects.get(pk=self.company).organization
        self.grant_id = str(uuid.uuid4())
        grant = {'id': self.grant_id, 'user_id': self.user.pk, 'auth_version': self.user.auth_version,
            'session_id': str(self.credentials.session.pk), 'company_id': self.company,
            'client_id': 'synthetic-own-ai', 'clientName': 'Synthetic own-AI client',
            'scopes': sorted(config.SCOPES), 'resource': config.mcp_url(),
            'redirectOrigin': 'https://agent.example.invalid', 'createdAt': timezone.now().isoformat(),
            'expiresAt': int(time.time()) + 3600, 'revoked': False}
        cache.set(oauth.key('grant', self.grant_id), grant, timeout=3600)
        token = oauth.issue_tokens(self.grant_id)['access_token']
        self.agent = APIClient()
        self.agent.credentials(HTTP_AUTHORIZATION='Bearer ' + token,
            HTTP_ACCEPT='application/json, text/event-stream')

    def tool(self, name, arguments, *, error=False):
        response = self.agent.post('/mcp/valley', {'jsonrpc': '2.0', 'id': 1,
            'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()['result']
        self.assertEqual(result['isError'], error, result)
        return result['structuredContent']

    def arguments(self, **changes):
        return {'companyId': self.company, 'month': FIXTURE['month'], 'requestId': str(uuid.uuid4()),
            'narrative': copy.deepcopy(FIXTURE['narrative']),
            'sources': [{'provider': 'manual', 'title': 'Fictional October founder notes',
                'url': 'https://private.example.invalid/founder-notes', 'occurredAt': '2026-10-20T00:00:00Z'}],
            'coverageNotes': 'SYNTHETIC PRIVATE COVERAGE: founder notes only; no provider retrieval.', **changes}

    def detail(self, update_id, **query):
        response = self.client.get(BASE + f'updates/{update_id}/', {'company_id': self.company, **query})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def choose_audience(self, update, audience='community'):
        response = self.client.post(BASE + 'updates/', {'companyId': self.company, 'updateId': update['id'],
            'month': 'October', 'year': 2026, 'expectedRevision': update['revisionId'], 'saveMode': 'draft',
            'audienceVisibility': audience, **{key: update[key] for key in FIXTURE['narrative']}}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        return response.data['update']

    def approve(self, update, *, reviewed=True):
        return self.client.post(BASE + f'updates/{update["id"]}/publish/', {'companyId': self.company,
            'reviewed': reviewed, 'revisionId': update['revisionId'], 'revisionHash': update['revisionHash'],
            'audienceVisibility': update['audienceVisibility']}, format='json')

    def test_full_own_ai_brief_save_review_and_published_projection(self):
        brief = self.tool('get_monthly_update_brief', {'companyId': self.company, 'month': FIXTURE['month']})
        self.assertEqual(brief['startup']['description'], FIXTURE['startup']['short_description'])
        self.assertEqual(brief['startup']['problemSolved'], FIXTURE['startup']['problem_solved'])
        self.assertEqual(brief['startup']['targetAudience'], FIXTURE['startup']['target_audience'])
        self.assertIn('startupOverview', brief['sections'])
        args = self.arguments()
        saved = self.tool('save_narrative_draft', args)
        owner = self.detail(saved['updateId'])
        update = owner['update']
        self.assertEqual(update['startupOverview'], FIXTURE['narrative']['startupOverview'])
        self.assertEqual(update['validation']['groundedness_status'], 'needs_review')
        self.assertEqual(update['agentProvenance']['kind'], 'agent_supplied')
        self.assertEqual(self.client.get(BASE + 'community/').data['updates'], [])
        self.assertEqual(self.approve(update, reviewed=False).status_code, 400)
        update = self.choose_audience(update, 'public')
        review = self.detail(update['id'])
        self.assertEqual(review['update']['validation']['groundedness_status'], 'needs_review')
        self.assertEqual(self.approve(update).status_code, 200)
        publication = self.client.get(BASE + 'community/').data['updates'][0]
        self.assertEqual(publication['startupOverview'], FIXTURE['narrative']['startupOverview'])
        self.assertEqual(publication['revisionHash'], update['revisionHash'])
        for private in ('evidenceSnapshot', 'agentSources', 'agentProvenance', 'manualSummary'):
            self.assertNotIn(private, publication)
        self.assertNotIn('SYNTHETIC PRIVATE COVERAGE', json.dumps(publication))
        self.assertNotIn('private.example.invalid', json.dumps(publication))
        anonymous = APIClient().get(BASE + f'public/{update["id"]}/')
        self.assertEqual(anonymous.status_code, 200)
        html = anonymous.content.decode()
        self.assertLess(html.index('What we do'), html.index('Summary'))
        self.assertIn(escape(FIXTURE['narrative']['startupOverview']), html)
        self.assertNotIn('SYNTHETIC PRIVATE COVERAGE', html)
        # Source profile changes and private agent edits do not rewrite approval.
        StartupProfile.objects.filter(organization=self.organization).update(short_description='Changed live profile')
        amended = self.tool('save_narrative_draft', self.arguments(updateId=update['id'],
            expectedRevision=update['revisionId'], narrative={**FIXTURE['narrative'], 'startupOverview': 'A private new overview.'}))
        self.assertNotEqual(amended['revisionHash'], update['revisionHash'])
        self.assertEqual(self.approve(update).status_code, 409)
        self.assertEqual(self.client.get(BASE + 'community/').data['updates'][0], publication)
        self.assertEqual(self.detail(update['id'], version='published')['update']['startupOverview'], FIXTURE['narrative']['startupOverview'])
        draft = MonthlyUpdateDraft.objects.get(pk=update['id'])
        self.assertEqual(draft.published_revision.content_hash, update['revisionHash'])
        self.assertEqual(MonthlyUpdateApproval.objects.get(revision=draft.published_revision).content_hash, update['revisionHash'])
        SAMPLE_FLOW.update({'synthetic': True, 'authorship': FIXTURE['authorship'], 'brief': brief,
            'saveResult': saved, 'ownerDraft': owner, 'founderReview': review, 'community': publication,
            'publicHtml': html, 'staleApprovalStatus': 409, 'modelProvenance': FIXTURE.get('model_provenance')})

    def test_scalar_overview_retry_omission_blank_and_financial_permissions(self):
        args = self.arguments()
        saved = self.tool('save_narrative_draft', args)
        replay = self.tool('save_narrative_draft', args)
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['revisionId'], saved['revisionId'])
        draft = MonthlyUpdateDraft.objects.get(pk=saved['updateId'])
        self.assertEqual(draft.revisions.count(), 1)
        omitted = self.tool('save_narrative_draft', self.arguments(updateId=draft.pk,
            expectedRevision=saved['revisionId'], narrative={'summary': FIXTURE['narrative']['summary']}))
        self.assertEqual(self.detail(draft.pk)['update']['startupOverview'], FIXTURE['narrative']['startupOverview'])
        cleared = self.tool('save_narrative_draft', self.arguments(updateId=draft.pk,
            expectedRevision=omitted['revisionId'], narrative={'summary': FIXTURE['narrative']['summary'], 'startupOverview': ''}))
        self.assertEqual(self.detail(draft.pk)['update']['startupOverview'], '')
        self.assertNotEqual(cleared['revisionHash'], omitted['revisionHash'])
        invalid = self.tool('save_narrative_draft', self.arguments(updateId=draft.pk,
            expectedRevision=cleared['revisionId'], narrative={'summary': 'Claimed revenue', 'metrics': {'revenue': 100}}), error=True)
        self.assertEqual(invalid['status'], 400)
        draft.refresh_from_db()
        self.assertEqual(draft.current_revision_id, cleared['revisionId'])

    def test_own_ai_intro_edits_preserve_frozen_history_and_prior_publication(self):
        from datetime import date
        from startup_updates.evidence_contract import content_hash
        from startup_updates.models import MonthlyEvidenceSnapshot
        from startup_updates.revisions import save_revision
        from startup_updates.tests_financial_history_unit import evidence_payload
        payload = evidence_payload()
        month = date(2026, 8, 1)
        draft = MonthlyUpdateDraft.objects.create(organization=self.organization, month=month)
        snapshot = MonthlyEvidenceSnapshot.objects.create(organization=self.organization, month=month,
            payload=payload, content_hash=content_hash(payload))
        first = save_revision(draft, {'summary': 'We tested delivery routes.',
            'startupOverview': 'We help shops plan deliveries.',
            '_financial_history_selection': {'enabled': True, 'shareAmounts': False}},
            snapshot=snapshot, audience='public', validation={'groundedness_status': 'founder_asserted'})
        update = self.detail(draft.pk)['update']
        self.assertEqual(self.approve(update).status_code, 200)
        published = self.client.get(BASE + 'community/').data['updates'][0]
        self.assertFalse(published['financialHistory']['shareAmounts'])
        self.assertIsNone(published['financialHistory']['currency'])
        expected_revision = first.pk
        for overview in ('We help local shops plan deliveries.', ''):
            edited = self.tool('save_narrative_draft', self.arguments(month='2026-08', sources=[],
                updateId=draft.pk, expectedRevision=expected_revision,
                narrative={'summary': 'We tested another route.', 'startupOverview': overview}))
            current = self.detail(draft.pk)['update']
            self.assertEqual(current['startupOverview'], overview)
            self.assertEqual(current['validation']['groundedness_status'], 'needs_review')
            self.assertEqual(current['financialHistory'], published['financialHistory'])
            draft.refresh_from_db()
            self.assertEqual(draft.current_revision.snapshot_id, snapshot.pk)
            self.assertEqual(draft.current_revision.structured_memo['financial_history'], first.structured_memo['financial_history'])
            self.assertEqual(self.client.get(BASE + 'community/').data['updates'][0], published)
            self.assertEqual(self.approve(update).status_code, 409)
            expected_revision = edited['revisionId']
        rejected = self.tool('save_narrative_draft', self.arguments(month='2026-08', sources=[],
            updateId=draft.pk, expectedRevision=expected_revision,
            financialHistorySelection={'enabled': True, 'shareAmounts': True}), error=True)
        self.assertEqual(rejected['status'], 400)
        draft.refresh_from_db()
        self.assertEqual(draft.current_revision_id, expected_revision)

    def test_background_only_cannot_be_published(self):
        response = self.client.post(BASE + 'updates/', {'companyId': self.company, 'month': 'October',
            'year': 2026, 'summary': '', 'startupOverview': FIXTURE['narrative']['startupOverview'],
            'saveMode': 'draft'}, format='json')
        self.assertIn(response.status_code, (200, 201), response.data)
        self.assertEqual(self.approve(response.data['update']).status_code, 400)
        self.assertFalse(MonthlyUpdateApproval.objects.exists())

    def test_worker_workflow_notes_and_metric_suggestions_never_enter_reader_projection(self):
        from startup_updates.revisions import save_revision
        saved = self.tool('save_narrative_draft', self.arguments())
        draft = MonthlyUpdateDraft.objects.get(pk=saved['updateId'])
        current = draft.current_revision
        # The worker can retain these internal memo fields. They must not become
        # narrative merely because the memo later receives a public approval.
        memo = {**current.structured_memo,
            'source_notes': ['PRIVATE SOURCE WORKFLOW'],
            'groundedness_notes': 'PRIVATE GROUNDING WORKFLOW',
            'metric_suggestions': [{'metric_key': 'customerInterviews', 'reason': 'PRIVATE METRIC SUGGESTION'}]}
        save_revision(draft, memo, snapshot=current.snapshot, audience='public',
            expected_revision=current.pk, validation=current.validation)
        update = self.detail(draft.pk)['update']
        self.assertEqual(self.approve(update).status_code, 200)
        shared = self.client.get(BASE + 'community/').data['updates'][0]
        html = APIClient().get(BASE + f'public/{draft.pk}/').content.decode()
        for field in ('source_notes', 'groundedness_notes', 'metric_suggestions'):
            self.assertNotIn(field, shared)
        for marker in ('PRIVATE SOURCE WORKFLOW', 'PRIVATE GROUNDING WORKFLOW', 'PRIVATE METRIC SUGGESTION'):
            self.assertNotIn(marker, json.dumps(shared))
            self.assertNotIn(marker, html)
        draft.refresh_from_db()
        self.assertEqual(draft.published_revision.structured_memo['source_notes'], ['PRIVATE SOURCE WORKFLOW'])
