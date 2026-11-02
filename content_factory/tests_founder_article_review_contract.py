"""Actual scoped review HTTP preserves old success data and current access."""
from copy import deepcopy
from unittest.mock import patch
from django.test import TransactionTestCase, override_settings
from rest_framework.response import Response
from workflow_runs.models import ContentFactoryRun
from . import tests_run_current_review as paired
from .run_verified_delivery import project_verified_delivery
from .founder_article_review_contract import scoped_review_response


@override_settings(**paired.FLAGS, **paired.ALL_P1_FLAGS_ON, ARTICLES_FREE_FIRST_ARTICLE_ENABLED=True,
    CONTENT_FACTORY_URL='https://cf.fixture.invalid', CONTENT_FACTORY_API_KEY='synthetic-review-dto-key')
class FounderReviewContractTests(TransactionTestCase):
    setUp = paired.CurrentReviewTests.setUp
    reads = paired.CurrentReviewTests.reads

    def prepared(self):
        with self.reads():
            project_verified_delivery(self.run.run_id)
        self.run.refresh_from_db()

    def get_review(self, remote=None):
        return self.client.get(f'/api/v1/vibe-marketing/runs/{self.run.run_id}/article-review/?company_id={self.company.pk}')

    def test_actual_get_adds_exact_parent_scope_deepcopy_and_real_owner_access_without_extra_provider_reads(self):
        self.prepared()
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        originals = []
        def capture(response, **kwargs):
            originals.append(deepcopy(response.data))
            return scoped_review_response(response, **kwargs)
        with patch('content_factory.article_review_views.scoped_review_response', side_effect=capture), \
                patch('content_factory.vibe_marketing_views.http_client.request',
                    return_value=paired.response(self.fixture['article_review_response'])) as remote, \
                patch('requests.get', side_effect=AssertionError('No extra provider reads')):
            value = self.get_review()
        self.assertEqual(value.status_code, 200, value.data)
        self.assertEqual(remote.call_count, 1)
        envelope = value.data['articleReview']
        self.assertEqual(set(envelope), {'contractVersion', 'companyId', 'runId', 'review', 'access', 'candidateFailures', 'pollAfterMs'})
        self.assertEqual((envelope['contractVersion'], envelope['companyId'], envelope['runId']),
            (2, str(self.company.pk), self.run.run_id))
        self.assertEqual(envelope['review'], originals[0])
        self.assertEqual({key: item for key, item in value.data.items() if key != 'articleReview'}, originals[0])
        self.assertEqual(envelope['access'], {'canEdit': True, 'canApprove': True})
        self.assertEqual(envelope['candidateFailures'], [])
        self.assertEqual(envelope['pollAfterMs'], 2000 if originals[0].get('previewPending') is True else 60000)
        envelope['review']['revision'] = 'mutated-private-copy'
        self.assertEqual(value.data['revision'], originals[0]['revision'])
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)

    def test_frozen_and_stopped_review_stays_readable_without_edit_or_consent_access(self):
        self.prepared()
        from .models import OrganizationContentConfig
        config = OrganizationContentConfig.objects.get(organization=self.org)
        original = deepcopy(config.pillar_strategy)
        config.pillar_strategy = {**original, 'startup_team': {'version': 1,
            'owner_user_id': self.user.pk, 'frozen': True, 'members': [], 'requests': [], 'invite': None, 'history': []}}
        config.save(update_fields=['pillar_strategy'])
        with patch('content_factory.vibe_marketing_views.http_client.request', return_value=paired.response(self.fixture['article_review_response'])):
            frozen = self.get_review()
        self.assertEqual(frozen.status_code, 200, frozen.data)
        self.assertEqual(frozen.data['articleReview']['access'], {'canEdit': False, 'canApprove': False})
        config.pillar_strategy = original; config.save(update_fields=['pillar_strategy'])
        from .run_user_stop import record_user_denial
        record_user_denial(self.run, user=self.user, actor_id=f'mlai_user:{self.user.pk}')
        with patch('content_factory.vibe_marketing_views.http_client.request', return_value=paired.response(self.fixture['article_review_response'])):
            stopped = self.get_review()
        self.assertEqual(stopped.status_code, 200, stopped.data)
        self.assertEqual(stopped.data['articleReview']['access'], {'canEdit': False, 'canApprove': False})
        self.assertEqual(stopped.data['articleExport']['status'], 'ready')

    def test_mutation_envelope_keeps_resolved_parent_and_candidate_failure_copy_never_uses_raw_error(self):
        self.prepared()
        from . import article_review_views as review_views
        result = deepcopy(self.fixture['article_review_response'])
        result.update(revisionRunId='separately-admitted-child', candidates={
            'actual-failed-id': {'fieldId': 'image-one', 'status': 'failed', 'reasonCode': 'unknown-synthetic', 'error': 'raw sk-secret-do-not-render'},
            'actual-codeless-id': {'fieldId': 'image-two', 'status': 'failed', 'error': 'raw provider trace'},
            'actual-ready-id': {'fieldId': 'image-three', 'status': 'ready'},
            'actual-pending-id': {'fieldId': 'image-four', 'status': 'queued'}})
        with patch.object(review_views.VibeMarketingArticleReviewView, '_apply_update', return_value=Response(result, status=202)):
            value = self.client.post(f'/api/v1/vibe-marketing/runs/{self.run.run_id}/article-review/?company_id={self.company.pk}',
                {'action': 'applyUpdate', 'operationId': 'real-http-review-operation'}, format='json')
        self.assertEqual(value.status_code, 202, value.data)
        envelope = value.data['articleReview']
        self.assertEqual(envelope['runId'], self.run.run_id)
        self.assertEqual(envelope['review'], result)
        self.assertEqual(envelope['review']['revisionRunId'], 'separately-admitted-child')
        self.assertEqual({item['candidateId'] for item in envelope['candidateFailures']}, {'actual-failed-id', 'actual-codeless-id'})
        self.assertTrue(all(item['failure']['code'] == 'UNREGISTERED_FAILURE_CODE' for item in envelope['candidateFailures']))
        self.assertNotIn('sk-secret', str(envelope['candidateFailures']))
        self.assertNotIn('provider trace', str(envelope['candidateFailures']))
        self.assertEqual(value.data['candidates'], result['candidates'])
        self.assertEqual(envelope['pollAfterMs'], 2000)

    def test_foreign_and_missing_scopes_never_add_a_review_envelope_or_read_provider(self):
        from .founder_journey_testing import make_new_journey_org
        foreign = make_new_journey_org('review-foreign.fixture.invalid')
        with patch('content_factory.vibe_marketing_views.http_client.request') as remote:
            for run_id, company in ((self.run.run_id, foreign.company), ('missing-review', self.company)):
                value = self.client.get(f'/api/v1/vibe-marketing/runs/{run_id}/article-review/?company_id={company.pk}')
                self.assertGreaterEqual(value.status_code, 400)
                self.assertNotIn('articleReview', value.data)
        remote.assert_not_called()

    def test_actual_legacy_cohort_success_has_no_new_envelope(self):
        from .founder_journey_testing import make_named_legacy_org
        legacy = make_named_legacy_org('la_vie')
        run = ContentFactoryRun.objects.create(organization=legacy.organization, domain=legacy.organization.domain,
            workflow='article_generation', run_id='legacy-review-contract', run_request={'delivery_mode': 'content_only'})
        self.client.force_authenticate(legacy.owner)
        body = {'articleId': run.run_id, 'revision': 'legacy-review-revision', 'fields': [], 'previewPending': True}
        with patch('content_factory.vibe_marketing_views.http_client.request', return_value=paired.response(body)):
            value = self.client.get(f'/api/v1/vibe-marketing/runs/{run.run_id}/article-review/?company_id={legacy.company.pk}')
        self.assertEqual(value.status_code, 200, value.data)
        self.assertNotIn('articleReview', value.data)
        self.assertEqual(value.data['revision'], body['revision'])
