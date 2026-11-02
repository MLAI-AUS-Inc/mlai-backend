"""Actual review HTTP projects future voice without changing progress or a draft."""
from copy import deepcopy
import json
import os
from pathlib import Path
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from core.models import User
from roo.models import Ledger
from workflow_runs.models import ContentFactoryRun
from . import tests_run_current_review as paired
from .article_voice import article_voice_payload, save_article_voice, validate_article_voice
from .founder_article_preferences import review_preferences
from .models import OrganizationContentConfig
from .tests_founder_article_review_contract import FounderReviewContractTests


VOICE = {'schemaVersion': 1, 'preset': 'calm_guide', 'category': 'general',
    'dimensions': {'register': 0, 'stance': 1, 'energy': 0, 'humour': 0, 'convention': 0},
    'readerExpertise': 'practitioner', 'narrator': 'brand_we',
    'voiceCard': [{'is': 'Clear', 'isNot': 'blunt'}], 'stances': [],
    'avoidWords': ['leverage'], 'preferWords': ['builders']}


@override_settings(**paired.FLAGS, **paired.ALL_P1_FLAGS_ON, ARTICLES_FREE_FIRST_ARTICLE_ENABLED=True,
    MY_STARTUP_ARTICLE_IMPROVEMENTS_ENABLED=True, MY_STARTUP_TEAM_ENABLED=True,
    CONTENT_FACTORY_URL='https://cf.fixture.invalid', CONTENT_FACTORY_API_KEY='synthetic-preferences-dto-key')
class FounderArticlePreferenceTests(TransactionTestCase):
    setUp = paired.CurrentReviewTests.setUp
    reads = paired.CurrentReviewTests.reads
    prepared = FounderReviewContractTests.prepared

    def get_review(self):
        return self.client.get(f'/api/v1/my-startup/vibe-marketing/runs/{self.run.run_id}/article-review?company_id={self.company.pk}')

    def voice(self):
        save_article_voice(self.org, validate_article_voice(VOICE))
        config = OrganizationContentConfig.objects.get(organization=self.org)
        return article_voice_payload(config.pillar_strategy)

    def http(self, *, body=None):
        before = list(ContentFactoryRun.objects.order_by('pk').values())
        configs = list(OrganizationContentConfig.objects.order_by('pk').values())
        ledgers = list(Ledger.objects.order_by('pk').values())
        with patch('content_factory.vibe_marketing_views.http_client.request',
                return_value=paired.response(body or self.fixture['article_review_response'])) as remote, \
                patch('requests.get', side_effect=AssertionError('No new provider read for preferences')), \
                patch('content_factory.founder_improvements.summary', side_effect=AssertionError('GET must not update progress')):
            with CaptureQueriesContext(connection) as queries:
                response = self.get_review()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(remote.call_count, 1)
        self.assertFalse(any(row['sql'].lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE ')) for row in queries))
        self.assertEqual(list(ContentFactoryRun.objects.order_by('pk').values()), before)
        self.assertEqual(list(OrganizationContentConfig.objects.order_by('pk').values()), configs)
        self.assertEqual(list(Ledger.objects.order_by('pk').values()), ledgers)
        envelope = response.data['articleReview']
        self.assertEqual(set(envelope['preferences']), {'voice'})
        self.assertEqual(set(envelope['preferences']['voice']), {'canEdit', 'value'})
        # The added preference never rewrites canonical review/body/revision.
        self.assertEqual(envelope['review'], {key: value for key, value in response.data.items() if key != 'articleReview'})
        return response.data

    def test_actual_owner_http_projects_current_valid_voice_with_zero_get_effects(self):
        self.prepared()
        expected = self.voice()
        payload = self.http()
        self.assertEqual(payload['articleReview']['preferences']['voice'], {'canEdit': True, 'value': expected})
        self.assertEqual(expected['readerExpertise'], 'practitioner')
        target = os.environ.get('FOUNDER_EXPORT_R5_VOICE')
        if target:
            Path(target).write_text(json.dumps({'source': 'actual authenticated review HTTP, actual verified canonical first draft and saved voice, no GET effects',
                'companyId': str(self.company.pk), 'runId': self.run.run_id, 'payload': payload}, indent=2) + '\n')

    def test_actual_owner_empty_voice_is_optional_and_never_blocks_current_article(self):
        self.prepared()
        payload = self.http()
        self.assertEqual(payload['articleReview']['preferences']['voice'], {'canEdit': True, 'value': None})
        self.assertEqual(payload['articleExport']['status'], 'ready')

    def test_flagoff_preserves_saved_voice_but_never_grants_edit(self):
        self.prepared()
        expected = self.voice()
        with override_settings(MY_STARTUP_ARTICLE_IMPROVEMENTS_ENABLED=False):
            payload = self.http()
        self.assertEqual(payload['articleReview']['preferences']['voice'], {'canEdit': False, 'value': expected})

    def test_article_readonly_is_independent_of_current_owner_future_voice_preference(self):
        self.prepared()
        body = {**self.fixture['article_review_response'], 'readOnly': True}
        payload = self.http(body=body)
        self.assertFalse(payload['articleReview']['access']['canEdit'])
        self.assertTrue(payload['articleReview']['preferences']['voice']['canEdit'])

    def test_actual_member_article_writer_is_never_the_settings_owner(self):
        self.prepared()
        expected = self.voice()
        from founder_tools.services import get_or_create_founder_profile
        member = User.objects.create_user(email='synthetic-voice-member@example.test')
        get_or_create_founder_profile(member)
        config = OrganizationContentConfig.objects.get(organization=self.org)
        roster = {'version': 1, 'owner_user_id': self.user.pk, 'frozen': False, 'members': [
            {'memberId': 'voice-member', 'user_id': member.pk, 'state': 'active', 'rights': {'publish': True}}],
            'requests': [], 'invite': None, 'history': []}
        OrganizationContentConfig.objects.filter(pk=config.pk).update(pillar_strategy={**config.pillar_strategy, 'startup_team': roster})
        self.client.force_authenticate(member)
        payload = self.http()
        self.assertTrue(payload['articleReview']['access']['canEdit'])
        self.assertEqual(payload['articleReview']['preferences']['voice'], {'canEdit': False, 'value': expected})

    def test_frozen_or_invalid_roster_and_fresh_inactive_owner_never_grant_voice_edit(self):
        self.prepared()
        config = OrganizationContentConfig.objects.get(organization=self.org)
        original = deepcopy(config.pillar_strategy)
        for roster in ({'version': 1, 'owner_user_id': self.user.pk, 'frozen': True}, ['invalid']):
            OrganizationContentConfig.objects.filter(pk=config.pk).update(pillar_strategy={**original, 'startup_team': roster})
            # The actual review read may be hidden before projection for an
            # invalid owner. The pure projection independently fails closed.
            self.assertFalse(review_preferences(organization=self.org, user=self.user)['voice']['canEdit'])
            if isinstance(roster, dict):
                self.assertFalse(self.http()['articleReview']['preferences']['voice']['canEdit'])
        OrganizationContentConfig.objects.filter(pk=config.pk).update(pillar_strategy=original)
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertTrue(self.user.is_active)  # caller's cached user is stale
        self.assertFalse(review_preferences(organization=self.org, user=self.user)['voice']['canEdit'])

    def test_no_earned_draft_cannot_edit_but_protected_prior_available_survives_ordinary_removal(self):
        config = OrganizationContentConfig.objects.get(organization=self.org)
        self.assertFalse(review_preferences(organization=self.org, user=self.user)['voice']['canEdit'])
        strategy = deepcopy(config.pillar_strategy)
        for prior, expected in (({'version': True, 'available': True}, False),
                ({'version': 1, 'available': True}, True)):
            OrganizationContentConfig.objects.filter(pk=config.pk).update(pillar_strategy={**strategy,
                'self_heal': {'article_improvements_v1': prior}})
            before = OrganizationContentConfig.objects.values().get(pk=config.pk)
            self.assertEqual(review_preferences(organization=self.org, user=self.user)['voice']['canEdit'], expected)
            self.assertEqual(OrganizationContentConfig.objects.values().get(pk=config.pk), before)
