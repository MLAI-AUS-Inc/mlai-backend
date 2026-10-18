"""Owned review route through the real partial journal and public receipt.

Only ORM/transaction and the Content Factory service boundary are replaced.
These records are unit fixtures, never exhaustive action or nightly evidence.
"""
from copy import deepcopy
import json
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate

from founder_tools.models import VibeRaisingProfile
from workflow_runs.models import ContentFactoryRun
from . import article_review_views as review, tests_website_goal_audit_unit as fixtures
from . import website_goal as goals, website_goal_audit as audit
from .models import OrganizationContentConfig
from .website_connections import contract_for
from .website_models import WebsiteConnection, WebsiteConnectionOperation
from .website_views import WebsiteConnectionOperationView


class Query:
    def __init__(self, values, model=None):
        self.values, self.model, self.filters = values, model, []

    def filter(self, *args, **kwargs):
        self.filters.append((args, kwargs))
        return self

    def first(self): return self.values[0] if self.values else None
    def get(self, **kwargs): return self.first()
    def select_for_update(self): return self
    def all(self): return self
    def select_related(self, *args): return self
    def order_by(self, *args): return self
    def __iter__(self): return iter(self.values)
    def __getitem__(self, key): return self.values[key]


@override_settings(ARTICLES_WEBSITE_DOCTOR_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"], ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
    ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"], ARTICLES_SETUP_CONSENT_ENABLED=False)
class ReviewActionAuditTests(SimpleTestCase):
    def setUp(self):
        fixtures.GoalAuditTests.setUp(self)
        self.op.receipt['goal']['current_source_sha'] = self.connection.verified_sha
        self.run = ContentFactoryRun(pk=17, run_id='article-review-fixture', organization=self.organization,
            domain=self.organization.domain, github_repo=self.connection.github_repo,
            workflow='article_generation', status='awaiting_approval', result={}, run_request={
                **contract_for(self.connection), 'first_article_goal_operation_id': str(self.op.pk),
                'expected_source_sha': self.connection.verified_sha,
                'goal_budget': {'version': 1, 'goal_id': str(self.op.pk),
                    'scope_digest': goals.digest(self.op.receipt['goal']['scope'])}})
        self.operations = Query([self.op], WebsiteConnectionOperation)
        self.enterContext(patch.object(WebsiteConnection.objects, 'select_for_update', return_value=Query([self.connection])))
        self.enterContext(patch.object(WebsiteConnectionOperation.objects, 'select_for_update', return_value=self.operations))
        self.enterContext(patch.object(WebsiteConnectionOperation.objects, 'filter', return_value=self.operations))
        self.enterContext(patch.object(VibeRaisingProfile.objects, 'get_or_create', return_value=(self.profile, False)))
        self.enterContext(patch.object(VibeRaisingProfile, 'companies', fixtures.rows(self.company)))
        self.enterContext(patch.object(OrganizationContentConfig.objects, 'get_or_create', return_value=(self.config, False)))
        self.enterContext(patch.object(OrganizationContentConfig.objects, 'filter', return_value=Query([self.config])))
        self.enterContext(patch.object(ContentFactoryRun.objects, 'prefetch_related', return_value=Query([self.run], ContentFactoryRun)))
        self.enterContext(patch.object(ContentFactoryRun, 'steps', Query([])))
        self.enterContext(patch.object(ContentFactoryRun.objects, 'filter', return_value=Query([], ContentFactoryRun)))
        self.enterContext(patch.object(ContentFactoryRun.objects, 'select_for_update', return_value=Query([self.run], ContentFactoryRun)))
        self.enterContext(patch.object(ContentFactoryRun, 'save', lambda *args, **kwargs: None))
        self.remote = self.enterContext(patch.object(review, 'remote_review', return_value={
            'articleId': self.run.run_id, 'revision': 'after', 'fields': [], 'previewPending': False}))

    def body(self, **changes):
        return {'action': 'editText', 'operationId': 'edit-operation-1', 'expectedRevision': 'before',
            'fieldId': 'article-title', 'value': 'A revised fixture title', **changes}

    def post(self, data=None, *, user=None, header='same-header'):
        request = APIRequestFactory().post('/review/?company_id=9', data or self.body(), format='json',
            HTTP_IDEMPOTENCY_KEY=header)
        force_authenticate(request, user=user or self.user)
        return review.VibeMarketingArticleReviewView.as_view()(request, run_id=self.run.run_id)

    def public(self):
        request = fixtures.GoalAuditTests.request(self, key='read')
        response = WebsiteConnectionOperationView().get(request, self.op.pk)
        self.assertEqual(response.status_code, 200)
        return response.data['operation']['receipt']['goal_audit']

    def test_owned_edit_intent_reaches_original_public_audit_and_replay_counts_once(self):
        for _ in range(2):
            self.assertEqual(self.post().status_code, 200)
        public = self.public()
        self.assertEqual(public['observed_owner_actions'], 1)
        self.assertFalse(public['complete'])
        self.assertIsNone(public['human_actions'])
        self.assertEqual(public['goal_id'], str(self.op.pk))

    def test_validated_attempt_is_observed_even_when_service_rejects_or_is_unavailable(self):
        for code in (409, 502):
            with self.subTest(status=code):
                self.remote.return_value = Response({'detail': 'Synthetic refusal'}, status=code)
                self.assertEqual(self.post(self.body(operationId=f'operation-{code}')).status_code, code)
        self.assertEqual(self.public()['observed_owner_actions'], 2)

    def test_actual_normalized_batch_updates_are_observed_and_retry_deduplicated(self):
        data = {'action': 'applyUpdate', 'operationId': 'batch-operation-1', 'expectedRevision': 'before',
            'textEdits': [{'fieldId': 'article-title', 'originalValue': 'Old', 'value': 'New'}], 'commentIds': []}
        for _ in range(2):
            self.assertEqual(self.post(data).status_code, 200)
        self.assertEqual(self.public()['observed_owner_actions'], 1)

    def test_distinct_repeated_edits_and_changed_intent_under_reused_id_are_not_dropped(self):
        for data in (self.body(), self.body(operationId='edit-operation-2'),
                self.body(value='A different intended change')):
            self.assertEqual(self.post(data).status_code, 200)
        self.assertEqual(self.public()['observed_owner_actions'], 3)

    def test_retry_with_a_new_observed_revision_keeps_the_original_intent(self):
        for revision in ('before', 'after'):
            self.assertEqual(self.post(self.body(expectedRevision=revision)).status_code, 200)
        self.assertEqual(self.public()['observed_owner_actions'], 1)

    def test_discard_has_no_replay_identity_and_repeated_intents_both_count(self):
        data = self.body(action='discardImage', candidateId='candidate-1')
        for _ in range(2):
            self.assertEqual(self.post(data, header='same-header').status_code, 200)
        self.assertEqual(self.public()['observed_owner_actions'], 2)

    def test_refresh_remains_automation_ambiguous_and_cannot_add_owner_intents(self):
        self.assertEqual(self.post(self.body(action='refresh')).status_code, 200)
        self.assertEqual(self.public()['observed_owner_actions'], 0)
        self.assertIsNone(self.public()['human_actions'])

    def test_invalid_or_missing_replay_identity_is_unknown_without_affecting_service_result(self):
        for identity in (None, True, '', 'short', 'x' * 101):
            with self.subTest(identity=identity):
                self.assertEqual(self.post(self.body(operationId=identity)).status_code, 200)
        self.assertEqual(self.public()['observed_owner_actions'], 0)
        self.assertIsNone(self.public()['human_actions'])

    def test_original_explicit_goal_must_agree_with_the_saved_budget_goal(self):
        self.run.run_request['goal_budget']['goal_id'] = str(uuid4())
        with self.assertLogs('content_factory.website_review_action_audit', level='INFO') as logs:
            self.assertEqual(self.post().status_code, 200)
        self.assertIn('reason=lineage', logs.output[0])
        self.assertEqual(self.public()['observed_owner_actions'], 0)

    def test_other_producer_goal_ids_must_agree_and_cannot_select_a_parent_alone(self):
        for alias in ('first_article_goal_id', 'strategy_goal_id'):
            with self.subTest(alias=alias):
                self.run.run_request[alias] = 'another-goal'
                with self.assertLogs('content_factory.website_review_action_audit', level='INFO'):
                    self.assertEqual(self.post().status_code, 200)
                self.run.run_request.pop(alias)
        self.run.run_request.pop('first_article_goal_operation_id')
        self.run.run_request.pop('goal_budget')
        self.run.run_request['first_article_goal_id'] = str(self.op.pk)
        with self.assertLogs('content_factory.website_review_action_audit', level='INFO'):
            self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.public()['observed_owner_actions'], 0)

    def test_saved_setup_reference_is_used_only_when_exactly_one_parent_matches(self):
        self.run.run_request.pop('first_article_goal_operation_id')
        self.run.run_request.pop('goal_budget')
        self.op.receipt['setup_run_id'] = self.run.run_id
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.public()['observed_owner_actions'], 1)
        duplicate = deepcopy(self.op)
        duplicate.pk = uuid4()
        duplicate.receipt['goal']['goal_id'] = str(duplicate.pk)
        self.operations.values.append(duplicate)
        with self.assertLogs('content_factory.website_review_action_audit', level='INFO') as logs:
            self.assertEqual(self.post(self.body(operationId='ambiguous-edit-1')).status_code, 200)
        self.assertIn('reason=lineage', logs.output[0])
        self.assertEqual(self.public()['observed_owner_actions'], 1)

    def test_orphan_run_cannot_fall_back_to_the_latest_goal(self):
        self.run.run_request.pop('first_article_goal_operation_id')
        self.run.run_request.pop('goal_budget')
        with self.assertLogs('content_factory.website_review_action_audit', level='INFO') as logs:
            self.assertEqual(self.post().status_code, 200)
        self.assertIn('reason=lineage', logs.output[0])
        self.assertEqual(self.public()['observed_owner_actions'], 0)

    def test_wrong_run_source_or_scope_never_acquires_an_observation(self):
        original = deepcopy(self.run.run_request)
        changes = [('connection_generation', True), ('connection_generation', 2),
            ('repository_id', 22), ('website_connection_id', str(uuid4())), ('branch', 'other'),
            ('app_root', 'apps/other'), ('domain', 'other.test'), ('expected_source_sha', 'b' * 40)]
        for key, value in changes:
            with self.subTest(field=key, value=value):
                self.run.run_request = {**deepcopy(original), key: value}
                with self.assertLogs('content_factory.website_review_action_audit', level='INFO'):
                    self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.public()['observed_owner_actions'], 0)

    def test_latest_ready_revision_without_original_parent_binding_remains_unattributed(self):
        original = deepcopy(self.run)
        initial = original.run_id
        self.op.receipt['first_article_run_id'] = initial
        self.run.pk = 18
        self.run.run_id, self.run.workflow = 'latest-review-revision', 'article_revision'
        self.run.result = {'preview_url': 'https://fixture.test/review/'}
        self.run.run_request.pop('first_article_goal_operation_id')
        self.run.run_request.pop('goal_budget')
        self.run.run_request['source_run_id'] = initial
        # The real latest-review reader sees this ready descendant. Its HTTP
        # route is valid, but source_run_id alone cannot select a goal for audit.
        with patch.object(ContentFactoryRun.objects, 'filter', return_value=Query([self.run], ContentFactoryRun)), \
                self.assertLogs('content_factory.website_review_action_audit', level='INFO') as logs:
            self.assertIs(review.views._latest_review_ready_component_revision(original, self.context), self.run)
            self.assertEqual(self.post().status_code, 200)
        self.assertIn('reason=lineage', logs.output[0])
        self.assertEqual(self.public()['observed_owner_actions'], 0)
        self.assertIsNone(self.public()['human_actions'])

    def test_older_saved_source_of_active_parent_is_explicitly_unavailable(self):
        self.op.receipt['first_article_run_id'] = self.run.run_id
        self.op.receipt['goal']['current_source_sha'] = 'b' * 40
        self.assertEqual(self.op.state, 'pending')
        with self.assertLogs('content_factory.website_review_action_audit', level='INFO') as logs:
            self.assertEqual(self.post().status_code, 200)
        self.assertIn('reason=source', logs.output[0])
        self.assertEqual(self.public()['observed_owner_actions'], 0)
        self.assertIsNone(self.public()['human_actions'])

    def test_action_specific_rejection_still_counts_the_authenticated_mutation_intent(self):
        self.remote.return_value = Response({'detail': 'Unknown component'}, status=409)
        data = self.body()
        data.pop('fieldId')
        self.assertEqual(self.post(data).status_code, 409)
        self.assertEqual(self.public()['observed_owner_actions'], 1)
        self.assertFalse(self.public()['complete'])
        self.assertIsNone(self.public()['human_actions'])

    def test_changed_current_configuration_and_erasure_epoch_leave_audit_unknown(self):
        for change in ('configuration', 'erasure'):
            with self.subTest(change=change):
                if change == 'configuration': self.connection.configuration_version += 1
                else: self.connection.blockers = [{'deletion_epoch': 1}]
                with self.assertLogs('content_factory.website_review_action_audit', level='INFO'):
                    self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.public()['observed_owner_actions'], 0)

    def test_terminal_parent_has_explicit_unavailable_diagnostic_and_never_gains_fresh_coverage(self):
        for state in ('completed', 'cancelled', 'failed', 'needs_attention'):
            with self.subTest(state=state):
                self.op.state = state
                with self.assertLogs('content_factory.website_review_action_audit', level='INFO') as logs:
                    self.assertEqual(self.post().status_code, 200)
                self.assertIn('reason=terminal', logs.output[0])
                self.assertFalse(self.public()['complete'])
                self.assertIsNone(self.public()['human_actions'])

    def test_foreign_owned_run_is_refused_before_review_or_audit(self):
        self.run.organization_id = 99
        self.assertEqual(self.post().status_code, 404)
        self.remote.assert_not_called()
        self.assertEqual(self.public()['observed_owner_actions'], 0)

    def test_disabled_audit_adds_no_observation_lookup(self):
        with self.settings(ARTICLES_WEBSITE_DOCTOR_ENABLED=False), patch.object(
                OrganizationContentConfig.objects, 'filter', side_effect=AssertionError('Audit lookup forbidden')):
            self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.public()['observed_owner_actions'], 0)

    def test_private_intent_actor_and_text_never_appear_in_the_public_receipt(self):
        self.assertEqual(self.post(self.body(value='private-review-canary')).status_code, 200)
        public = self.public()
        encoded = json.dumps(public)
        for value in ('private-review-canary', 'edit-operation-1', self.user.email, 'event_ids', 'fingerprint'):
            self.assertNotIn(value, encoded)
        self.assertTrue(all(item['update_fields'] == ['receipt'] for item in self.saved))
        self.assertEqual(self.op.updated_at, self.now)

    def test_optional_observer_failure_does_not_block_the_review_or_export_error_text(self):
        with (patch.object(WebsiteConnection.objects, 'select_for_update', side_effect=RuntimeError('private-error-canary')),
                self.assertLogs('content_factory.website_review_action_audit', level='WARNING') as logs):
            self.assertEqual(self.post().status_code, 200)
        self.assertEqual(logs.output, ['WARNING:content_factory.website_review_action_audit:articles_review_action_audit_observation_unavailable'])
        self.assertIsNone(self.public()['human_actions'])
