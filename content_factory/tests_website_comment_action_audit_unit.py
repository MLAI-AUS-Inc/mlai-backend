"""Actual authenticated comment routes with isolated ORM/transaction seams."""
import json
from unittest.mock import patch

from django.db import transaction
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate
from workflow_runs.models import ContentFactoryRun

from . import tests_website_review_action_audit_unit as fixtures
from . import vibe_marketing_views as views
from .models import VibeMarketingComponentComment
from .website_models import WebsiteConnection
from . import website_comment_action_audit as observer


class Comments:
    def __init__(self):
        self.values = []

    def create(self, **values):
        comment = VibeMarketingComponentComment(**values)
        self.values.append(comment)
        return comment

    def get_or_create(self, *, id, defaults):
        for comment in self.values:
            if comment.pk == id:
                return comment, False
        return self.create(id=id, **defaults), True

    def select_for_update(self): return self

    def filter(self, **values):
        return fixtures.Query([comment for comment in self.values if all(
            str(comment.pk) == str(value) if key == 'id' else getattr(comment, key, None) == value
            for key, value in values.items())])


@override_settings(ARTICLES_WEBSITE_DOCTOR_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=['7'], ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
    ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=['7'], ARTICLES_SETUP_CONSENT_ENABLED=False)
class CommentActionAuditTests(SimpleTestCase):
    def setUp(self):
        fixtures.ReviewActionAuditTests.setUp(self)
        self.comments, self.depth, self.exits, self.lock_depths, self.lock_exits = Comments(), 0, [], [], []
        connection = transaction.get_connection()
        self.enterContext(patch.object(connection, 'in_atomic_block', False))
        self.enterContext(patch.object(connection, 'needs_rollback', False))

        def enter(atomic):
            self.depth += 1
            connection.in_atomic_block = True

        def leave(atomic, exc_type, exc, traceback):
            self.exits.append((self.depth, exc_type))
            self.depth -= 1
            connection.in_atomic_block = self.depth > 0
            return False

        def lock():
            self.lock_depths.append(self.depth)
            self.lock_exits.append(len(self.exits))
            return fixtures.Query([self.connection])

        self.enterContext(patch.object(transaction.Atomic, '__enter__', enter))
        self.enterContext(patch.object(transaction.Atomic, '__exit__', leave))
        self.enterContext(patch.object(WebsiteConnection.objects, 'select_for_update', side_effect=lock))
        for method in ('create', 'get_or_create', 'select_for_update', 'filter'):
            self.enterContext(patch.object(VibeMarketingComponentComment.objects, method, getattr(self.comments, method)))
        self.enterContext(patch.object(VibeMarketingComponentComment, 'save', lambda *args, **kwargs: None))
        self.enterContext(patch.object(VibeMarketingComponentComment, 'delete',
            lambda comment: self.comments.values.remove(comment)))
        self.enterContext(patch.object(ContentFactoryRun.objects, 'filter', side_effect=lambda **kwargs:
            fixtures.Query([self.run] if kwargs.get('pk') == self.run.pk else [])))

    def body(self, **changes):
        return {'componentId': 'article-title', 'body': 'Improve this title',
            'operationId': 'comment-operation-1', **changes}

    def request(self, method='post', data=None, *, comment=None, user=None):
        raw = getattr(APIRequestFactory(), method)('/comments/?company_id=9',
            data if data is not None else self.body(), format='json', HTTP_IDEMPOTENCY_KEY='shared-header')
        force_authenticate(raw, user=user or self.user)
        self.last_raw = raw
        view = views.VibeMarketingRunCommentDetailView if comment else views.VibeMarketingRunCommentsView
        kwargs = {'run_id': self.run.run_id}
        if comment:
            kwargs['comment_id'] = comment.pk
        return view.as_view()(raw, **kwargs)

    def public(self):
        return fixtures.ReviewActionAuditTests.public(self)

    def test_actual_create_route_and_supported_replay_record_one_intent(self):
        self.assertEqual(self.request().status_code, 201)
        self.assertEqual(self.request().status_code, 200)
        self.assertEqual(len(self.comments.values), 1)
        self.assertEqual(self.public()['observed_owner_actions'], 1)
        self.assertEqual(self.lock_depths, [1, 1])
        self.assertTrue(all(index > 0 and self.exits[index - 1][0] == 1 for index in self.lock_exits))

    def test_actual_edit_and_delete_routes_record_distinct_attempts(self):
        comment = self.comments.create(run=self.run, actor=self.user, component_id='article-title', body='Old')
        self.assertEqual(self.request('patch', {'body': 'New'}, comment=comment).status_code, 200)
        self.assertEqual(comment.body, 'New')
        self.assertEqual(self.request('delete', {}, comment=comment).status_code, 200)
        self.assertEqual(self.comments.values, [])
        self.assertEqual(self.public()['observed_owner_actions'], 2)

    def test_attempt_survives_view_exception_and_transaction_exit(self):
        with patch.object(VibeMarketingComponentComment.objects, 'get_or_create',
                side_effect=ValueError('private-comment-failure')):
            with self.assertRaisesRegex(ValueError, 'private-comment-failure'):
                self.request()
        self.assertEqual(self.depth, 0)
        self.assertTrue(any(kind is ValueError for _, kind in self.exits))
        self.assertEqual(self.comments.values, [])
        self.assertEqual(self.public()['observed_owner_actions'], 1)
        self.assertEqual(len(self.lock_exits), 1)
        self.assertEqual(self.exits[self.lock_exits[0] - 1], (1, ValueError))

    def test_changed_create_intent_and_new_supported_identity_count_separately(self):
        self.assertEqual(self.request().status_code, 201)
        self.assertEqual(self.request(data=self.body(body='Another suggestion')).status_code, 409)
        self.assertEqual(self.request(data=self.body(operationId='comment-operation-2')).status_code, 201)
        self.assertEqual(self.public()['observed_owner_actions'], 3)

    def test_create_without_protocol_identity_and_repeated_edits_are_not_header_deduplicated(self):
        body = self.body()
        body.pop('operationId')
        for _ in range(2):
            self.assertEqual(self.request(data=body).status_code, 201)
        comment = self.comments.values[0]
        for _ in range(2):
            self.assertEqual(self.request('patch', {'body': 'New'}, comment=comment).status_code, 200)
        self.assertEqual(self.public()['observed_owner_actions'], 4)

    def test_rejected_edit_and_delete_attempts_still_count_without_changing_the_comment(self):
        comment = self.comments.create(run=self.run, actor=self.user, component_id='article-title',
            body='Submitted', status='submitted')
        self.assertEqual(self.request('patch', {'body': 'Rejected'}, comment=comment).status_code, 400)
        self.assertEqual(self.request('delete', {}, comment=comment).status_code, 400)
        self.assertEqual(comment.body, 'Submitted')
        self.assertEqual(self.public()['observed_owner_actions'], 2)

    def test_resolve_and_reopen_are_owned_mutation_intents(self):
        comment = self.comments.create(run=self.run, actor=self.user, component_id='article-title', body='Draft')
        for action in ('resolve', 'reopen'):
            self.assertEqual(self.request('patch', {'action': action}, comment=comment).status_code, 200)
        self.assertEqual(comment.status, 'draft')
        self.assertEqual(self.public()['observed_owner_actions'], 2)

    def test_scope_is_frozen_before_mutation_and_cannot_be_rebound_afterward(self):
        original = self.comments.get_or_create
        def changed(**kwargs):
            self.run.run_request['first_article_goal_operation_id'] = 'newer-goal'
            return original(**kwargs)
        with patch.object(VibeMarketingComponentComment.objects, 'get_or_create', side_effect=changed), \
                self.assertLogs(observer.logger.name, level='INFO') as logs:
            self.assertEqual(self.request().status_code, 201)
        self.assertIn('reason=run_changed', logs.output[0])
        self.assertEqual(self.lock_depths, [])
        self.assertIsNone(self.public()['human_actions'])

    def test_goal_aliases_are_frozen_without_becoming_parent_resolution_fallbacks(self):
        original = self.comments.get_or_create
        for alias in ('first_article_goal_id', 'strategy_goal_id'):
            with self.subTest(alias=alias):
                self.run.run_request[alias] = str(self.op.pk)
                def changed(**kwargs):
                    self.run.run_request[alias] = 'another-goal'
                    return original(**kwargs)
                with patch.object(VibeMarketingComponentComment.objects, 'get_or_create', side_effect=changed), \
                        self.assertLogs(observer.logger.name, level='INFO') as logs:
                    self.assertIn(self.request(data=self.body(operationId='operation-' + alias)).status_code, (200, 201))
                self.assertIn('reason=run_changed', logs.output[0])
                self.run.run_request.pop(alias)
        self.assertEqual(self.lock_depths, [])
        self.assertEqual(self.public()['observed_owner_actions'], 0)

    def test_preexisting_goal_alias_conflict_remains_unknown(self):
        for alias in ('first_article_goal_id', 'strategy_goal_id'):
            with self.subTest(alias=alias):
                self.run.run_request[alias] = 'another-goal'
                with self.assertLogs('content_factory.website_review_action_audit', level='INFO') as logs:
                    self.assertIn(self.request().status_code, (200, 201))
                self.assertIn('reason=lineage', logs.output[0])
                self.run.run_request.pop(alias)
        self.assertEqual(self.public()['observed_owner_actions'], 0)

    def test_connection_scope_and_terminal_parent_are_rechecked_after_mutation(self):
        original = self.comments.get_or_create
        for change in ('connection', 'terminal'):
            def changed(**kwargs):
                if change == 'connection': self.connection.configuration_version += 1
                else: self.op.state = 'completed'
                return original(**kwargs)
            self.connection.configuration_version = 1
            with patch.object(VibeMarketingComponentComment.objects, 'get_or_create', side_effect=changed), \
                    self.assertLogs('content_factory.website_review_action_audit', level='INFO'):
                self.assertIn(self.request(data=self.body(operationId=f'operation-{change}')).status_code, (200, 201))
        self.assertEqual(self.public()['observed_owner_actions'], 0)
        self.assertFalse(self.public()['complete'])

    def test_unexpected_outer_transaction_is_unknown_and_acquires_no_observer_lock(self):
        with transaction.atomic(), self.assertLogs(observer.logger.name, level='INFO') as logs:
            self.assertEqual(self.request().status_code, 201)
        self.assertIn('reason=outer_transaction', logs.output[0])
        self.assertEqual(self.lock_depths, [])
        self.assertIsNone(self.public()['human_actions'])

    def test_capture_has_only_intent_hashes_and_is_removed_even_after_exception(self):
        original = self.comments.get_or_create
        def inspect(**kwargs):
            pending = getattr(self.last_raw, observer._PENDING)
            self.assertEqual(set(pending[-1]), {'action', 'operation', 'fingerprint'})
            self.assertNotIn('private-comment-canary', json.dumps(pending[-1]))
            self.assertNotIn('comment-operation-1', json.dumps(pending[-1]))
            return original(**kwargs)
        with patch.object(VibeMarketingComponentComment.objects, 'get_or_create', side_effect=inspect):
            self.assertEqual(self.request(data=self.body(body='private-comment-canary')).status_code, 201)
        self.assertFalse(hasattr(self.last_raw, observer._PENDING))
        encoded = json.dumps(self.public())
        for private in ('private-comment-canary', 'comment-operation-1', 'event_ids', 'fingerprint'):
            self.assertNotIn(private, encoded)
        with patch.object(VibeMarketingComponentComment.objects, 'get_or_create', side_effect=ValueError('private')):
            with self.assertRaises(ValueError): self.request()
        self.assertFalse(hasattr(self.last_raw, observer._PENDING))

    def test_optional_observer_error_does_not_replace_response_or_original_exception(self):
        with patch.object(WebsiteConnection.objects, 'select_for_update', side_effect=ValueError('private-observer')), \
                self.assertLogs('content_factory.website_review_action_audit', level='INFO') as logs:
            self.assertEqual(self.request().status_code, 201)
        self.assertNotIn('private-observer', repr(logs.output))
        with patch.object(VibeMarketingComponentComment.objects, 'get_or_create', side_effect=ValueError('original-failure')), \
                patch.object(observer.review_audit, 'record_intent', side_effect=ValueError('private-observer')), \
                self.assertLogs(observer.logger.name, level='WARNING') as logs:
            with self.assertRaisesRegex(ValueError, 'original-failure'): self.request()
        self.assertNotIn('private-observer', repr(logs.output))

    def test_trusted_internal_dispatch_is_excluded_but_body_cannot_supply_capture_state(self):
        original = views.VibeMarketingRunCommentsView.post
        def internal(view, request, *args, **kwargs):
            request._website_prepare_dispatch = True
            return original(view, request, *args, **kwargs)
        with patch.object(views.VibeMarketingRunCommentsView, 'post', internal):
            self.assertEqual(self.request().status_code, 201)
        self.assertEqual(self.public()['observed_owner_actions'], 0)
        self.assertEqual(self.request(data=self.body(_website_prepare_dispatch=True,
            _website_comment_audit_capture=['forged'], operationId='comment-operation-2')).status_code, 201)
        self.assertEqual(self.public()['observed_owner_actions'], 1)

    def test_default_off_and_get_add_no_observer_lookup_or_write(self):
        with self.settings(ARTICLES_WEBSITE_DOCTOR_ENABLED=False), patch.object(
                observer, 'scope_digest', side_effect=AssertionError('Disabled observer must not capture')):
            self.assertEqual(self.request().status_code, 201)
        self.assertEqual(self.request('get', {}).status_code, 200)
        self.assertEqual(self.lock_depths, [])
        self.assertEqual(self.public()['observed_owner_actions'], 0)

    def test_foreign_run_and_invalid_create_identity_do_not_acquire_an_observation(self):
        self.run.organization_id = 99
        self.assertEqual(self.request().status_code, 404)
        self.run.organization_id = self.organization.pk
        self.assertEqual(self.request(data=self.body(operationId='bad')).status_code, 400)
        self.assertEqual(self.lock_depths, [])
        self.assertIsNone(self.public()['human_actions'])

    def test_malformed_action_does_not_bypass_the_existing_missing_comment_response(self):
        missing = VibeMarketingComponentComment(run=self.run, actor=self.user, component_id='title', body='Missing')
        self.assertEqual(self.request('patch', {'action': {'unsupported': True}}, comment=missing).status_code, 404)
        self.assertEqual(self.public()['observed_owner_actions'], 1)

    def test_oversized_intent_remains_unavailable_without_changing_route_result(self):
        with self.assertLogs(observer.logger.name, level='INFO') as logs:
            self.assertEqual(self.request(data=self.body(body='x' * (1024 * 1024 + 1))).status_code, 201)
        self.assertIn('reason=intent_size', logs.output[0])
        self.assertEqual(self.lock_depths, [])
        self.assertIsNone(self.public()['human_actions'])
