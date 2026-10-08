"""Consent, tenant isolation and retention guarantees for website integrations."""

from copy import deepcopy
from datetime import timedelta
import hashlib
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import uuid

from django.db import connection, connections, close_old_connections
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from .models import OrganizationContentConfig
from .website_contract import WebsiteAuthorityError, cleanup_plan, connection_contract, sanitized_evidence, template_validation
from .website_connections import authority_guard, bind_website, contract_for, record_scan_evidence, summary_for, transition_connection
from .website_models import WebsiteConnection, WebsiteConnectionOperation, WebsiteRepositoryMutation, WebsiteTemplateRevision
from .website_reconciliation import handle_website_github_event, process_website_connection_operations
from .website_views import WebsiteConnectionAuthorizeView, WebsiteMutationView

SHA = 'a' * 40


class WebsiteContractTests(SimpleTestCase):
    def test_pending_article_repair_is_active_without_reclassifying_terminal_failures(self):
        from .website_operations import workflow_operation_state
        run = SimpleNamespace(run_id='article-parent', workflow='article_generation', status='blocked',
            result={'precondition_status': 'precondition_failed', 'repair_status': 'queued', 'scan_run_id': 'repair-child'})
        for phase in ('queued', 'running', 'setup_queued', 'awaiting_approval', 'awaiting_confirmation', 'awaiting_merge'):
            run.result['repair_status'] = phase
            self.assertEqual(workflow_operation_state(run), 'running')
        for phase in ('failed', 'auth_required', 'manual_blocked', 'not_started', 'verification_required', ''):
            run.result['repair_status'] = phase
            self.assertEqual(workflow_operation_state(run), 'blocked')
        run.result['repair_status'] = 'queued'
        for status in ('cancelled', 'denied', 'failed', 'completed'):
            run.status = status
            self.assertEqual(workflow_operation_state(run), status)
        run.status = 'blocked'
        for change in ({'scan_run_id': ''}, {'scan_run_id': run.run_id}, {'precondition_status': 'other'}):
            original = dict(run.result)
            run.result.update(change)
            self.assertEqual(workflow_operation_state(run), 'blocked')
            run.result = original
        run.workflow = 'repo_scan'
        self.assertEqual(workflow_operation_state(run), 'blocked')

    def test_setup_revision_uses_original_run_consent_before_charge_or_feedback(self):
        from .vibe_marketing_views import VibeMarketingArticleSystemRevisionsView
        original = {'website_connection_id': str(uuid.uuid4()), 'connection_generation': 1}
        current = {**original, 'connection_generation': 2}
        run = SimpleNamespace(run_id='old-setup', run_request=original,
            domain='site.example.test', github_repo='example/site')
        for supplied in (current, {}):
            with (
                self.subTest(explicit=bool(supplied)),
                patch('content_factory.website_views._context', return_value=(object(), None, None)),
                patch('content_factory.vibe_marketing_views._run_belongs_to_context', return_value=True),
                patch('workflow_runs.models.ContentFactoryRun.objects.filter') as runs,
                patch('content_factory.website_views.authority_guard', side_effect=WebsiteAuthorityError('website_connection_changed', 'Changed.')) as guard,
                patch('content_factory.vibe_marketing_views._require_roo_points_for_ai_agent') as charge,
                patch('content_factory.vibe_marketing_views._call_content_factory_run_action') as dispatch,
            ):
                runs.return_value.first.return_value = run
                response = VibeMarketingArticleSystemRevisionsView().post(SimpleNamespace(data=supplied), run.run_id)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.data['code'], 'website_connection_changed')
                if supplied:
                    guard.assert_not_called()
                else:
                    self.assertEqual(guard.call_args.args[0]['connection_generation'], 1)
                    self.assertEqual(guard.call_args.kwargs['action'], 'setup')
                charge.assert_not_called()
                dispatch.assert_not_called()

    @patch('integrations.services.github_app._github_app_jwt', return_value='synthetic-jwt')
    @patch('integrations.services.github_app.http_requests.post')
    def test_installation_token_is_scoped_to_immutable_repository_id(self, post, jwt):
        from integrations.services.github_app import create_installation_access_token
        post.return_value = SimpleNamespace(status_code=201, json=lambda: {'token': 'synthetic-token', 'permissions': {'contents': 'read'}})
        create_installation_access_token(installation_id='45', repository='example/reused-name', repository_id=123, permission_mode='read', use_cache=False)
        self.assertEqual(post.call_args.kwargs['json']['repository_ids'], [123])
        self.assertNotIn('repositories', post.call_args.kwargs['json'])

    def test_generation_rejects_boolean_or_incomplete(self):
        for value in (True, 0, 1.1, None, 'x'):
            with self.subTest(value=value), self.assertRaises(WebsiteAuthorityError):
                connection_contract({'website_connection_id': str(uuid.uuid4()), 'connection_generation': value})

    def test_legacy_prompt_envelope_is_quarantined(self):
        self.assertFalse(template_validation('# EXISTING ARTIFACT\n\nold prompt')['valid'])
        self.assertTrue(template_validation('# Article template\nNative document structure')['valid'])

    def test_cleanup_keeps_edits_shared_files_and_dependencies(self):
        files = [
            {'path': 'new.md', 'kind': 'setup_scaffolding', 'ownership': 'created', 'after_sha256': 'same'},
            {'path': 'edited.md', 'kind': 'setup_scaffolding', 'ownership': 'created', 'after_sha256': 'old'},
            {'path': 'shared.md', 'kind': 'setup_scaffolding', 'ownership': 'shared', 'after_sha256': 'same'},
            {'path': 'published.md', 'kind': 'published_article', 'ownership': 'created', 'after_sha256': 'same', 'retained_dependencies': ['article']},
        ]
        result = cleanup_plan(files, {x['path']: 'same' for x in files})
        self.assertEqual(result['deletions'], [])
        self.assertEqual({x['path'] for x in result['conflicts']}, {'edited.md', 'shared.md'})
        self.assertEqual(set(result['retained']), {'published.md', 'new.md'})
        with self.assertRaises(WebsiteAuthorityError):
            cleanup_plan([{'path': '../escape'}], {})

    def test_evidence_removes_nested_credentials(self):
        value = sanitized_evidence({'github_token': 'synthetic', 'nested': [{'authorization': 'secret', 'path': 'a'}]})
        self.assertEqual(value, {'nested': [{'path': 'a'}]})


class WebsiteSetupProjectionTests(SimpleTestCase):
    """Historical repository runs never restore current-generation setup state."""

    def setUp(self):
        self.org = Organization(pk=7, domain='site.example.test', name='Synthetic')
        self.website = WebsiteConnection(organization=self.org, github_repo='example/site',
            repository_id=123, installation_id='45', branch='main', generation=2)
        self.config = OrganizationContentConfig(organization=self.org, github_repo=self.website.github_repo,
            website_connection=self.website, company_context='Retained company details')
        self.binding = contract_for(self.website)

    def run_fixture(self, workflow, *, binding=None, run_id='historical', **kwargs):
        return ContentFactoryRun(run_id=run_id, organization=self.org, domain=self.org.domain,
            workflow=workflow, github_repo=self.website.github_repo, status='completed',
            run_request=self.binding if binding is None else binding, **kwargs)

    def test_prior_generation_and_unbound_scans_and_builds_are_ignored(self):
        from .article_setup_reset import article_setup_reset_ignores_run
        cases = (
            {},
            'invalid legacy snapshot',
            {**self.binding, 'connection_generation': 1},
            {**self.binding, 'website_connection_id': str(uuid.uuid4())},
            {**self.binding, 'repository_id': 999},
            {**self.binding, 'connection_generation': True},
        )
        for workflow in ('repo_scan', 'content_factory_scan', 'article_system_setup'):
            for binding in cases:
                with self.subTest(workflow=workflow, binding=binding):
                    run = self.run_fixture(workflow, binding=binding)
                    run.updated_at = timezone.now() + timedelta(hours=1)
                    self.assertTrue(article_setup_reset_ignores_run(self.config, run))
        run = self.run_fixture('repo_scan')
        run.github_repo = 'example/other'
        self.assertTrue(article_setup_reset_ignores_run(self.config, run))
        run.github_repo = self.website.github_repo
        run.organization_id = 99
        self.assertTrue(article_setup_reset_ignores_run(self.config, run))

    def test_current_generation_and_company_research_remain_available(self):
        from .article_setup_reset import article_setup_reset_ignores_run
        for workflow in ('repo_scan', 'content_factory_scan', 'article_system_setup'):
            self.assertFalse(article_setup_reset_ignores_run(self.config, self.run_fixture(workflow)))
        self.assertFalse(article_setup_reset_ignores_run(self.config, self.run_fixture('topic_discovery', binding={})))
        self.config.website_connection = None
        self.assertFalse(article_setup_reset_ignores_run(self.config, self.run_fixture('repo_scan', binding={})))

    def test_nested_worker_snapshot_can_retain_a_valid_string_generation(self):
        from contextlib import contextmanager
        from . import website_connections as lifecycle
        run = self.run_fixture('repo_scan')
        run.save = MagicMock()
        request = {'workflow': 'repo_scan', 'domain': self.org.domain,
            'run_request': {**self.binding, 'connection_generation': '2'}}

        @contextmanager
        def authorize(payload, **kwargs):
            self.assertEqual(connection_contract(lifecycle._payload_with_context(payload)),
                connection_contract(self.binding))
            yield self.website

        @lifecycle.guarded_service_write('config_write', only_repository=True)
        def sync(_view, incoming, run_id):
            run.run_request = dict(incoming.data['run_request'])
            return SimpleNamespace(status_code=200)

        with patch.object(lifecycle, 'authority_guard', side_effect=authorize), \
             patch.object(lifecycle, 'record_scan_evidence'), \
             patch.object(ContentFactoryRun.objects, 'filter') as rows:
            rows.return_value.first.return_value = run
            sync(object(), SimpleNamespace(method='PUT', data=request), run_id=run.run_id)
        self.assertEqual(connection_contract(run.run_request), connection_contract(self.binding))
        self.assertEqual(run.run_request['connection_generation'], '2')

    def test_persisted_lookup_accepts_valid_representations_after_a_newer_old_run(self):
        from . import vibe_marketing_views as marketing
        old = self.run_fixture('repo_scan', binding={**self.binding, 'connection_generation': 1})
        representations = (
            {**self.binding, 'connection_generation': '2'},
            {**self.binding, 'connection_generation': '002'},
            {'connectionId': str(self.website.pk), 'connectionGeneration': '2', 'repositoryId': 123},
        )
        for binding in representations:
            with self.subTest(binding=binding), patch.object(ContentFactoryRun.objects, 'filter') as rows:
                query = rows.return_value
                query.exclude.return_value = query
                query.only.return_value = query
                query.order_by.return_value = query
                hydration = query.filter.return_value
                hydration.prefetch_related.return_value = hydration
                current = self.run_fixture('repo_scan', binding=binding, run_id='current-scan')
                query.iterator.return_value = iter([old, current])
                hydration.first.return_value = current
                selected = marketing._latest_persisted_run_for_article_setup(self.config, {'repo_scan'})
                self.assertIs(selected, current)

    def test_durable_lookup_skips_heavy_history_and_hydrates_only_an_eligible_run(self):
        from django.db.backends.sqlite3.base import DatabaseWrapper
        from django.db.models.query import QuerySet
        from . import vibe_marketing_views as marketing
        old = self.run_fixture('repo_scan', binding={**self.binding, 'connection_generation': 1})
        current = self.run_fixture('repo_scan', run_id='current-scan')
        current.pk = 23
        reads = []
        compiler_connection = DatabaseWrapper({'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'})

        def candidates(query, **kwargs):
            sql, params = query.query.get_compiler(connection=compiler_connection).as_sql()
            for field in ('result', 'acceptance_summary', 'verification_summary', 'step_order', 'error'):
                self.assertNotIn(f'"content_factory_run"."{field}"', sql)
            self.assertEqual(query._prefetch_related_lookups, ())
            self.assertIn('"content_factory_run"."organization_id" =', sql)
            self.assertIn(self.org.pk, params)
            self.assertIn(self.org.domain, params)
            self.assertIn(self.website.github_repo, params)
            reads.append('consent')
            return iter([old, current])

        def hydrate(query):
            sql, params = query.query.get_compiler(connection=compiler_connection).as_sql()
            self.assertIn('"content_factory_run"."result"', sql)
            self.assertEqual(query._prefetch_related_lookups, ('steps',))
            self.assertIn(current.pk, params)
            reads.append('full')
            return current

        with patch.object(QuerySet, 'iterator', autospec=True, side_effect=candidates), \
             patch.object(QuerySet, 'first', autospec=True, side_effect=hydrate):
            selected = marketing._latest_persisted_run_for_article_setup(self.config, {'repo_scan'})
        self.assertIs(selected, current)
        self.assertEqual(reads, ['consent', 'full'])

    def test_durable_lookup_rechecks_consent_after_hydrating_a_candidate(self):
        from . import vibe_marketing_views as marketing
        candidate = self.run_fixture('repo_scan', run_id='changed-scan')
        changed = self.run_fixture('repo_scan', run_id=candidate.run_id,
            binding={**self.binding, 'connection_generation': 1})
        current = self.run_fixture('repo_scan', run_id='current-scan')
        with patch.object(ContentFactoryRun.objects, 'filter') as rows:
            query = rows.return_value
            query.exclude.return_value = query
            query.only.return_value = query
            query.order_by.return_value = query
            query.iterator.return_value = iter([candidate, current])
            hydration = query.filter.return_value
            hydration.prefetch_related.return_value = hydration
            hydration.first.side_effect = [changed, current]
            selected = marketing._latest_persisted_run_for_article_setup(self.config, {'repo_scan'})
        self.assertIs(selected, current)
        self.assertEqual(hydration.first.call_count, 2)

    def test_reset_projection_cannot_reuse_an_explicit_old_scan_or_its_setup_reference(self):
        from . import vibe_marketing_views as marketing
        old = {**self.binding, 'connection_generation': 1}
        scan = self.run_fixture('repo_scan', binding=old, result={
            'scan_purpose': 'setup', 'setup_run_id': 'old-setup',
            'article_system_setup': {'setup_run_id': 'old-setup', 'status': 'preview_failed'},
        })
        setup = self.run_fixture('article_system_setup', binding=old, run_id='old-setup')
        with patch.object(marketing, '_latest_persisted_run_for_article_setup', return_value=None), \
             patch.object(marketing, 'website_summary', return_value={'connectionGeneration': 2}):
            state = marketing._article_setup_state_for_config(self.config, organization=self.org,
                run=scan, latest_runs=[scan, setup])
        for key in ('scanRunId', 'scanStatus', 'setupRunId', 'setupStatus', 'previewUrl'):
            self.assertIsNone(state[key], key)
        self.assertFalse(state['setupBlocked'])
        self.assertFalse(state['generationReady'])
        self.assertEqual(state['source'], 'none')
        self.assertEqual(self.config.company_context, 'Retained company details')

    def test_fresh_scan_is_visible_without_historical_setup_evidence(self):
        from . import vibe_marketing_views as marketing
        current = self.run_fixture('repo_scan', run_id='current-scan')
        old = self.run_fixture('repo_scan', binding={**self.binding, 'connection_generation': 1})
        with patch.object(marketing, '_latest_persisted_run_for_article_setup', return_value=None), \
             patch.object(marketing, 'website_summary', return_value={'connectionGeneration': 2}):
            state = marketing._article_setup_state_for_config(self.config, organization=self.org,
                latest_runs=[old, current])
        self.assertEqual(state['scanRunId'], 'current-scan')
        self.assertEqual(state['scanStatus'], 'completed')
        self.assertIsNone(state['setupRunId'])

    def test_workflow_progress_keeps_company_research_and_omits_historical_scan(self):
        from . import vibe_marketing_views as marketing
        old = self.run_fixture('repo_scan', binding={**self.binding, 'connection_generation': 1})
        current = self.run_fixture('repo_scan', run_id='current-scan')
        research = self.run_fixture('topic_discovery', binding={}, run_id='company-research')
        with patch.object(marketing, '_get_config', return_value=self.config):
            result = marketing._workflow_progress_context(context=SimpleNamespace(organization=self.org),
                latest_runs=[old, current, research], checks={})
        self.assertEqual(result[2], [current, research])


class WebsiteDatabaseFixture:
    def setUp(self):
        self.org = Organization.objects.create(domain='site.example.test', name='Synthetic')
        self.website = WebsiteConnection.objects.create(organization=self.org, github_repo='example/site',
            repository_id=123, installation_id='45', branch='main', site_url=self.org.domain,
            capabilities={'inventoryReady': True, 'generationReady': True, 'publishingReady': True})
        self.config = OrganizationContentConfig.objects.create(organization=self.org, github_repo=self.website.github_repo,
            website_connection=self.website, article_template='# Article\nTemplate', design_guide='# Design\nGuide',
            company_context='Retain private company context', publish_targets=[{'target_id': 'native'}], auto_publish=True)
        self.binding = contract_for(self.website)
        head_check = patch('content_factory.website_connections.verify_repository_head', side_effect=lambda connection, sha: sha)
        head_check.start()
        self.addCleanup(head_check.stop)
        native_metadata = patch("content_factory.website_connections.read_repository_native_target",
            side_effect=lambda connection: {"id": connection.repository_id, "full_name": connection.github_repo, "default_branch": "main"})
        native_metadata.start()
        self.addCleanup(native_metadata.stop)


@override_settings(ROO_API_KEY='synthetic-test-key', INTERNAL_API_KEY='synthetic-test-key')
class WebsiteLifecycleTests(WebsiteDatabaseFixture, TestCase):
    def test_source_change_can_record_setup_merge_without_promoting_readiness(self):
        from . import vibe_marketing_views as marketing
        from .website_connections import owner_write_guard, scoped_run_contract
        from .website_models import WebsiteScanSnapshot
        self.website.verified_sha = SHA
        self.website.save(update_fields=['verified_sha'])
        WebsiteScanSnapshot.objects.create(connection=self.website, generation=self.website.generation,
            run_id='new-head', source_sha='b' * 40, detector_version='github_head', fingerprint='new-head')
        operation = WebsiteConnectionOperation.objects.create(connection=self.website,
            generation=self.website.generation, action='workflow', state='completed',
            idempotency_key='merge-observation', payload={'attempt': 1})
        request = {**self.binding, 'source_sha': SHA, 'expected_source_sha': SHA,
            'operation_id': str(operation.pk), 'operation_attempt': 1, 'deletion_epoch': 0}
        run = ContentFactoryRun.objects.create(run_id='merged-setup', organization=self.org,
            domain=self.org.domain, github_repo=self.website.github_repo, workflow='article_system_setup',
            status='completed', current_step='create_pull_request', run_request=request,
            result={'pr_url': 'https://github.com/example/site/pull/37', 'pr_number': 37,
                'merge_status': 'not_merged', 'article_system_setup': {'status': 'pr_created'}})
        pull = {'merged': True, 'number': 37, 'html_url': run.result['pr_url'], 'merge_commit_sha': 'b' * 40,
            'base': {'ref': 'main', 'repo': {'id': 123, 'full_name': 'example/site'}},
            'head': {'sha': 'c' * 40, 'repo': {'id': 123, 'full_name': 'example/site'}}}
        with patch('content_factory.website_connections.verify_repository_head',
                side_effect=WebsiteAuthorityError('website_source_changed', 'Verify current source')):
            with self.assertRaises(WebsiteAuthorityError):
                with owner_write_guard(scoped_run_contract(run)):
                    self.fail('Original-source writes must still fail')
            with patch('content_factory.website_connections.read_setup_merge_pull', return_value=pull), \
                    patch.object(marketing, '_link_built_scaffold_publish_target') as promote:
                marketing._apply_setup_merge_result(run=run, context=SimpleNamespace(organization=self.org))
                marketing._persist_setup_merged_verification(run, {'status': 'verification_required'})
                promote.assert_not_called()
        run.refresh_from_db()
        self.config.refresh_from_db()
        self.website.refresh_from_db()
        self.assertEqual(run.result['merge_status'], 'merged')
        self.assertEqual(run.result['merge_response']['pull']['merge_commit_sha'], 'b' * 40)
        self.assertEqual(run.result['merged_setup_verification']['status'], 'verification_required')
        self.assertEqual(run.run_request, request)
        self.assertEqual(self.website.verified_sha, SHA)
        self.assertEqual(self.config.publish_targets, [{'target_id': 'native'}])
        self.assertFalse(self.config.article_system.get('generationReady'))
        self.assertFalse(self.config.article_system['pending_article_system_setup'].get('generationReady'))
        self.assertFalse(self.config.articles_scaffolded)
        # A later observation also cannot reset independently verified state.
        self.config.article_system.update(generationReady=True, state='verified')
        self.config.save(update_fields=['article_system'])
        with patch('content_factory.website_connections.read_setup_merge_pull', return_value=pull):
            marketing._apply_setup_merge_result(run=run, context=SimpleNamespace(organization=self.org))
        self.config.refresh_from_db()
        self.assertTrue(self.config.article_system['generationReady'])
        self.assertEqual(self.config.article_system['state'], 'verified')

    def test_setup_merge_observation_rechecks_cancel_after_github_read(self):
        from . import vibe_marketing_views as marketing
        from .website_connections import owner_operation_scope
        operation = WebsiteConnectionOperation.objects.create(connection=self.website,
            generation=self.website.generation, action='workflow', state='completed',
            idempotency_key='cancel-merge-observation', payload={'attempt': 1})
        request = {**self.binding, 'source_sha': SHA, 'operation_id': str(operation.pk), 'operation_attempt': 1, 'deletion_epoch': 0}
        run = ContentFactoryRun.objects.create(run_id='cancelled-merge-observation', organization=self.org,
            domain=self.org.domain, github_repo=self.website.github_repo, workflow='article_system_setup',
            status='completed', run_request=request, result={'pr_url': 'https://github.com/example/site/pull/37'})
        def merged_then_cancelled(*args):
            operation.state = 'cancelled'
            operation.save(update_fields=['state'])
            return {'merged': True, 'number': 37, 'html_url': run.result['pr_url'], 'merge_commit_sha': 'b' * 40,
                'base': {'ref': 'main', 'repo': {'id': 123, 'full_name': 'example/site'}},
                'head': {'sha': 'c' * 40, 'repo': {'id': 123, 'full_name': 'example/site'}}}
        with patch('content_factory.website_connections.read_setup_merge_pull', side_effect=merged_then_cancelled), \
                owner_operation_scope(request), self.assertRaises(WebsiteAuthorityError) as caught:
            marketing._apply_setup_merge_result(run=run, context=SimpleNamespace(organization=self.org))
        self.assertEqual(caught.exception.code, 'website_operation_cancelled')
        run.refresh_from_db()
        self.assertNotIn('merge_status', run.result)

    def test_pending_repair_keeps_child_checkpoints_authorized_then_fences_failure_or_cancel(self):
        from .website_operations import bind_operation_run, observe_workflow_status, validate_operation, cancel_operation
        for outcome in ('failed', 'cancelled'):
            with self.subTest(outcome=outcome):
                operation = WebsiteConnectionOperation.objects.create(connection=self.website,
                    generation=self.website.generation, action='workflow', state='running',
                    idempotency_key=f'repair:{outcome}', payload={'workflow': 'article_generation', 'attempt': 1})
                binding = {**self.binding, 'operation_id': str(operation.pk), 'operation_attempt': 1, 'deletion_epoch': 0}
                parent = ContentFactoryRun.objects.create(run_id=f'article-{outcome}', organization=self.org,
                    domain=self.org.domain, github_repo=self.website.github_repo, workflow='article_generation',
                    status='blocked', run_request=binding,
                    result={'precondition_status': 'precondition_failed', 'repair_status': 'queued',
                        'scan_run_id': f'scan-{outcome}'})
                bind_operation_run(operation, parent)
                operation.refresh_from_db()
                self.assertEqual(operation.state, 'running')
                child = ContentFactoryRun.objects.create(run_id=f'scan-{outcome}', organization=self.org,
                    domain=self.org.domain, github_repo=self.website.github_repo, workflow='repo_scan',
                    status='running', run_request=binding)
                checkpoint = {**binding, 'run_id': child.run_id, 'status': 'running'}
                self.assertEqual(validate_operation(self.website, checkpoint).pk, operation.pk)
                observe_workflow_status(child, checkpoint)
                operation.refresh_from_db()
                self.assertEqual(operation.payload['run_id'], parent.run_id)
                self.assertEqual(operation.state, 'running')
                if outcome == 'failed':
                    parent.result['repair_status'] = 'failed'
                    parent.save(update_fields=['result'])
                    observe_workflow_status(parent, {'status': 'blocked'})
                else:
                    cancel_operation(self.config, data=binding, idempotency_key=f'cancel-{outcome}')
                    child.refresh_from_db()
                    self.assertEqual(child.status, 'cancelled')
                with self.assertRaises(WebsiteAuthorityError):
                    validate_operation(self.website, checkpoint)
                operation.refresh_from_db()
                self.assertEqual(operation.state, 'blocked' if outcome == 'failed' else 'cancelled')

    def test_legacy_scaffold_late_response_cannot_create_active_child_after_disconnect(self):
        from types import SimpleNamespace
        from integrations.services.github import decide_scan_scaffold
        from .models import ContentFactoryJob
        source = ContentFactoryJob.objects.create(job_id='legacy-scaffold-source', domain=self.org.domain,
            slack_user_id='synthetic', status='awaiting_confirmation', request_meta=self.binding)
        def accepted_after_disconnect(*args, **kwargs):
            transition_connection(self.config, action='disconnect', expected=self.binding)
            return SimpleNamespace(status_code=202, content=b'{}', json=lambda: {
                'job_id': source.job_id, 'scaffold_job_id': 'late-scaffold-child', 'status': 'queued'})
        with patch('integrations.services.github.http_requests.post', side_effect=accepted_after_disconnect):
            with self.assertRaises(WebsiteAuthorityError):
                decide_scan_scaffold(scan_run_id=source.job_id, decision='approve', domain=self.org.domain, slack_user_id='synthetic')
        source.refresh_from_db()
        self.assertNotEqual(source.status, 'confirmed')
        self.assertFalse(ContentFactoryJob.objects.filter(job_id='late-scaffold-child', status='queued').exists())
        child = ContentFactoryRun.objects.get(run_id='late-scaffold-child')
        self.assertEqual(child.status, 'cancelled')
        self.assertTrue(WebsiteConnectionOperation.objects.filter(
            idempotency_key=f'{self.website.pk}:late-dispatch:late-scaffold-child').exists())

    def test_legacy_scan_completion_cannot_restore_config_after_disconnect(self):
        from integrations.models import UserIntegration
        from integrations.services.github import _record_bound_scan_completion
        integration = UserIntegration.objects.create(slack_user_id='legacy-scan', github_repo=self.website.github_repo)
        saved_template = self.config.article_template
        evidence = {'source_sha': SHA, 'article_template': '# EXISTING ARTIFACT\nUntrusted legacy result'}
        self.assertEqual(_record_bound_scan_completion(self.binding, evidence, integration), SHA)
        self.config.refresh_from_db()
        self.assertEqual(self.config.article_template, saved_template)
        integration.refresh_from_db()
        self.assertEqual(integration.last_scanned_sha, SHA)
        transition_connection(self.config, action='disconnect', expected=self.binding)
        with self.assertRaises(WebsiteAuthorityError):
            _record_bound_scan_completion(self.binding, {**evidence, 'source_sha': 'c' * 40}, integration)
        integration.refresh_from_db()
        self.assertEqual(integration.last_scanned_sha, SHA)

    def test_reconciliation_cannot_resurrect_disconnected_run(self):
        from .reconciliation import _adopt_remote_payload
        run = ContentFactoryRun.objects.create(run_id='reconcile-race', workflow='article_generation',
            domain=self.org.domain, organization=self.org, github_repo=self.website.github_repo,
            run_request=self.binding, status='running')
        transition_connection(self.config, action='disconnect', expected=self.binding)
        self.assertEqual(_adopt_remote_payload(run, {'status': 'completed', 'workflow': run.workflow}), 'cancelled')
        run.refresh_from_db()
        self.assertEqual(run.status, 'cancelled')

    def test_setup_merge_requires_exact_verified_preview_commit(self):
        from .website_connections import validate_setup_merge_source
        run = ContentFactoryRun.objects.create(run_id='setup-merge', workflow='article_system_setup',
            domain=self.org.domain, organization=self.org, github_repo=self.website.github_repo, run_request=self.binding)
        preview_sha = 'b' * 40
        record_scan_evidence(self.website, {'source_sha': SHA, 'publish_targets': [{'target_id': 'native',
            'publish_capability': 'direct', 'verification': {'status': 'preview_verified', 'source_sha': preview_sha, 'base_sha': SHA, 'preview_capable': True}}]})
        validate_setup_merge_source(run, preview_sha)
        with self.assertRaises(WebsiteAuthorityError):
            validate_setup_merge_source(run, 'c' * 40)

    def test_preview_rechecks_live_base_head_even_without_webhook(self):
        with patch('content_factory.website_connections.verify_repository_head', side_effect=WebsiteAuthorityError('website_source_changed', 'Source changed.')) as verify:
            with self.assertRaises(WebsiteAuthorityError):
                with authority_guard({**self.binding, 'expected_source_sha': SHA}, action='preview'):
                    self.fail('Stale preview authorized')
        verify.assert_called_once()

    def test_article_merge_requires_this_run_generation_and_exact_owned_head(self):
        from .website_connections import validate_publish_merge_source
        run = ContentFactoryRun.objects.create(run_id='publish-merge', workflow='publish_article',
            domain=self.org.domain, organization=self.org, github_repo=self.website.github_repo, run_request=self.binding)
        head = 'b' * 40
        branch = 'cf/publish-merge'
        for invalid in ('missing', 'intent', 'other_run', 'stale_generation', 'other_branch', 'changed_head'):
            with self.subTest(invalid=invalid):
                if invalid != 'missing':
                    WebsiteRepositoryMutation.objects.create(connection=self.website,
                        generation=self.website.generation + (1 if invalid == 'stale_generation' else 0),
                        operation_id=f'fixture:{invalid}', run_id='other' if invalid == 'other_run' else run.run_id,
                        base_sha=SHA, head_sha='c' * 40 if invalid == 'changed_head' else head,
                        branch='cf/other' if invalid == 'other_branch' else branch,
                        status='proposed' if invalid == 'intent' else 'applied', patch_digest='d' * 64)
                with self.assertRaises(WebsiteAuthorityError):
                    validate_publish_merge_source(run, head, branch)
                self.website.repository_mutations.all().delete()
        WebsiteRepositoryMutation.objects.create(connection=self.website, generation=self.website.generation,
            operation_id='fixture:applied', run_id=run.run_id, base_sha=SHA, head_sha=head,
            branch=branch, status='applied', patch_digest='d' * 64)
        validate_publish_merge_source(run, head, branch)

    @patch('content_factory.website_tokens.revoke_generation_tokens', return_value={'revoked': 1, 'pending': 0})
    def test_company_purge_erases_source_then_removes_authority_after_reconciliation(self, revoke):
        from .website_connections import offboard_website_connections
        record_scan_evidence(self.website, {'repository_inventory': {'source_sha': SHA, 'discovery_complete': True}, 'article_template': '# Saved article'})
        offboard_website_connections(self.org, purge=True)
        self.website.refresh_from_db()
        self.assertEqual(self.website.state, 'revoked')
        self.assertFalse(self.website.scan_snapshots.exists())
        self.assertFalse(self.website.template_revisions.exists())
        self.assertFalse(self.website.targets.exists())
        receipt = self.website.operations.get(state='pending').receipt
        self.assertTrue(receipt['website_database_evidence_erased'])
        self.assertFalse(receipt['artifact_retention']['erasure_performed'])
        self.assertIn('worker_checkpoints', receipt['artifact_retention']['categories'])
        with self.assertRaises(WebsiteAuthorityError):
            with authority_guard(self.binding):
                self.fail('Offboarded authority accepted')
        process_website_connection_operations()
        self.assertFalse(WebsiteConnection.objects.filter(pk=self.website.pk).exists())
        revoke.assert_called_once()

    def test_shared_company_offboard_preserves_another_founders_authority(self):
        from django.contrib.auth import get_user_model
        from .website_connections import offboard_website_connections
        user = get_user_model().objects.create_user(email='departing@example.test', password='synthetic-password')
        offboard_website_connections(self.org, user=user)
        self.website.refresh_from_db()
        self.assertEqual(self.website.state, 'connected')
        self.website.authorized_by = user
        self.website.save(update_fields=['authorized_by'])
        offboard_website_connections(self.org, user=user)
        self.website.refresh_from_db()
        self.assertEqual(self.website.state, 'revoked')
        self.assertTrue(OrganizationContentConfig.objects.filter(pk=self.config.pk).exists())

    def test_disconnect_fences_tokens_config_callbacks_and_keeps_history(self):
        run = ContentFactoryRun.objects.create(run_id='run-1', workflow='article_generation', domain=self.org.domain,
            organization=self.org, github_repo=self.website.github_repo, run_request=self.binding, status='running')
        op = transition_connection(self.config, action='disconnect', expected=self.binding, idempotency_key='test')
        self.website.refresh_from_db(); self.config.refresh_from_db(); run.refresh_from_db()
        self.assertEqual(self.website.generation, 2)
        self.assertEqual(self.website.state, 'disconnected')
        self.assertEqual(run.status, 'cancelled')
        self.assertEqual(self.config.company_context, 'Retain private company context')
        self.assertTrue(self.config.article_template)
        self.assertFalse(self.config.auto_publish)
        for action in ('read', 'scan', 'config_write', 'preview', 'publish', 'merge'):
            with self.subTest(action=action), self.assertRaises(WebsiteAuthorityError):
                with authority_guard(self.binding, action=action):
                    self.fail('old consent accepted')
        retry = transition_connection(self.config, action='disconnect', expected=self.binding, idempotency_key='test')
        self.assertEqual(retry.pk, op.pk)
        self.assertEqual(WebsiteConnectionOperation.objects.count(), 1)

    def test_pause_allows_new_generation_inventory_and_drafts_only(self):
        transition_connection(self.config, action='pause', expected=self.binding)
        self.website.refresh_from_db()
        current = contract_for(self.website)
        with authority_guard(current, action='scan'):
            pass
        self.assertTrue(self.website.capabilities['generationReady'])
        for action in ('setup', 'publish', 'merge'):
            with self.subTest(action=action), self.assertRaises(WebsiteAuthorityError):
                with authority_guard(current, action=action):
                    self.fail('paused write accepted')

    def test_repair_rechecks_template_after_acquiring_lock(self):
        from io import StringIO
        from django.core.management import call_command
        self.config.article_template = '# EXISTING ARTIFACT\nOld invalid seed'
        self.config.save(update_fields=['article_template'])
        lock = OrganizationContentConfig.objects.select_for_update
        def replace_then_lock(*args, **kwargs):
            OrganizationContentConfig.objects.filter(pk=self.config.pk).update(article_template='# Fresh valid template')
            return lock(*args, **kwargs)
        output = StringIO()
        with patch.object(OrganizationContentConfig.objects, 'select_for_update', side_effect=replace_then_lock):
            call_command('repair_website_connections', domain=self.org.domain, apply=True, stdout=output)
        self.config.refresh_from_db()
        self.assertEqual(self.config.article_template, '# Fresh valid template')
        self.assertFalse(WebsiteTemplateRevision.objects.filter(status='quarantined').exists())

    @patch('integrations.services.github_app.create_installation_access_token', return_value=SimpleNamespace(token='synthetic'))
    @patch('integrations.http_client.get')
    def test_cleanup_retains_intents_and_changes_not_merged_into_selected_branch(self, get, mint):
        from .website_reconciliation import _cleanup_proposal
        def response(value):
            return SimpleNamespace(status_code=200, json=lambda: value, raise_for_status=lambda: None)
        get.side_effect = [response({'sha': SHA}), response({'status': 'diverged'})]
        entries = []
        for index, status_value in enumerate(('proposed', 'applied')):
            entries.append(WebsiteRepositoryMutation.objects.create(connection=self.website, generation=1,
                operation_id=f'cleanup-{index}', base_sha=SHA, head_sha='b' * 40, status=status_value,
                patch_digest=str(index), files=[{'path': f'content/{index}.md', 'ownership': 'created', 'kind': 'setup_scaffolding', 'after_sha256': 'c' * 64}]))
        operation = WebsiteConnectionOperation.objects.create(connection=self.website, generation=1, action='cleanup',
            idempotency_key='cleanup-proof', payload={'mutation_ids': [str(row.pk) for row in entries]})
        result = _cleanup_proposal(operation)
        self.assertEqual(result['deletions'], [])
        self.assertEqual(result['retained'], ['content/0.md', 'content/1.md'])
        self.assertEqual(get.call_count, 2)

    def test_reset_archives_invalid_templates_and_preserves_run_history(self):
        self.config.article_template = '# EXISTING ARTIFACT\ncontaminated'
        self.config.save()
        ContentFactoryRun.objects.create(run_id='old', workflow='article_system_setup', organization=self.org,
            domain=self.org.domain, github_repo=self.website.github_repo, status='completed', run_request=self.binding)
        transition_connection(self.config, action='reset', expected=self.binding)
        self.config.refresh_from_db()
        self.assertFalse(self.config.article_template)
        self.assertEqual(self.config.company_context, 'Retain private company context')
        self.assertTrue(WebsiteTemplateRevision.objects.filter(status='quarantined').exists())
        self.assertTrue(ContentFactoryRun.objects.filter(run_id='old').exists())

    def test_reset_excludes_retained_old_scans_and_selects_a_fresh_generation(self):
        from .vibe_marketing_views import _article_setup_state_for_config
        scan = ContentFactoryRun.objects.create(run_id='before-reset-scan', workflow='repo_scan',
            organization=self.org, domain=self.org.domain, github_repo=self.website.github_repo,
            run_request=self.binding, status='completed', result={'scan_purpose': 'setup',
                'setup_run_id': 'before-reset-setup', 'article_system_setup': {
                    'setup_run_id': 'before-reset-setup', 'status': 'preview_failed'}})
        setup = ContentFactoryRun.objects.create(run_id='before-reset-setup', workflow='article_system_setup',
            organization=self.org, domain=self.org.domain, github_repo=self.website.github_repo,
            run_request=self.binding, status='failed')
        operation = transition_connection(self.config, action='reset', expected=self.binding)
        self.config.refresh_from_db()
        self.website.refresh_from_db()
        ContentFactoryRun.objects.filter(pk=scan.pk).update(updated_at=timezone.now() + timedelta(hours=1))
        scan.refresh_from_db()
        state = _article_setup_state_for_config(self.config, organization=self.org, run=scan, latest_runs=[scan, setup])
        self.assertIsNone(state['scanRunId'])
        self.assertIsNone(state['scanStatus'])
        self.assertIsNone(state['setupRunId'])
        self.assertFalse(state['setupBlocked'])
        self.assertEqual(state['source'], 'none')
        self.assertEqual(ContentFactoryRun.objects.filter(pk__in=[scan.pk, setup.pk]).count(), 2)
        self.assertEqual(self.config.company_context, 'Retain private company context')
        self.assertIn(scan.run_id, operation.payload['stop_preview_run_ids'])
        fresh = ContentFactoryRun.objects.create(run_id='after-reset-scan', workflow='repo_scan',
            organization=self.org, domain=self.org.domain, github_repo=self.website.github_repo,
            run_request=contract_for(self.website), status='completed')
        # The old scan has a newer timestamp. The persisted lookup must apply
        # the generation filter before selecting its first candidate.
        state = _article_setup_state_for_config(self.config, organization=self.org)
        self.assertEqual(state['scanRunId'], fresh.run_id)
        self.assertEqual(state['scanStatus'], 'completed')
        self.assertIsNone(state['setupRunId'])

    def test_current_string_generation_snapshot_remains_visible_in_persisted_lookup(self):
        from .vibe_marketing_views import _latest_persisted_run_for_article_setup
        for binding in (
            {**self.binding, 'connection_generation': '1'},
            {'connectionId': str(self.website.pk), 'connectionGeneration': '1', 'repositoryId': 123},
        ):
            with self.subTest(binding=binding):
                scan = ContentFactoryRun.objects.create(run_id=str(uuid.uuid4()), workflow='repo_scan',
                    organization=self.org, domain=self.org.domain, github_repo=self.website.github_repo,
                    run_request=binding, status='completed')
                self.assertEqual(_latest_persisted_run_for_article_setup(self.config, {'repo_scan'}).pk, scan.pk)

    def test_cross_tenant_and_wrong_repository_denied(self):
        for payload in ({**self.binding, 'domain': 'other.example.test'}, {**self.binding, 'repository_id': 999}, {**self.binding, 'github_repo': 'example/other'}):
            with self.assertRaises(WebsiteAuthorityError):
                with authority_guard(payload):
                    pass

    @patch('content_factory.website_connections.verify_repository_access')
    def test_repository_rebind_clears_derived_state(self, verify):
        verify.return_value = {'repository_id': 987, 'github_repo': 'other/site', 'installation_id': '99', 'branch': 'main'}
        old_id = self.website.pk
        new = bind_website(self.config, user=None, repo='other/site', expected=self.binding)
        self.website.refresh_from_db(); self.config.refresh_from_db()
        self.assertEqual(self.website.state, 'disconnected')
        self.assertNotEqual(new.pk, old_id)
        self.assertFalse(self.config.article_template)
        self.assertEqual(self.config.publish_targets, [])
        self.assertEqual(self.config.company_context, 'Retain private company context')
        with self.assertRaises(WebsiteAuthorityError):
            with authority_guard(self.binding):
                pass

    def test_inventory_only_keeps_templates_and_never_promotes_publication(self):
        self.website.capabilities = {}; self.website.save()
        record_scan_evidence(self.website, {'repository_inventory': {'source_sha': SHA, 'discovery_complete': True, 'framework': 'unknown'}})
        self.website.refresh_from_db(); self.config.refresh_from_db()
        self.assertTrue(self.website.capabilities['inventoryReady'])
        self.assertFalse(self.website.capabilities.get('publishingReady', False))
        self.assertEqual(self.config.article_template, '# Article\nTemplate')
        self.assertEqual(self.website.scan_snapshots.count(), 1)

    def test_detection_proof_is_not_publish_verification(self):
        record_scan_evidence(self.website, {'source_sha': SHA, 'publish_targets': [{'target_id': 'native', 'publish_capability': 'direct', 'proof_mode': 'proven'}]})
        self.assertFalse(self.website.capabilities['publishingReady'])
        with self.assertRaises(WebsiteAuthorityError):
            with authority_guard(self.binding, action='publish'):
                pass
        record_scan_evidence(self.website, {'source_sha': SHA, 'publish_targets': [{'target_id': 'native', 'publish_capability': 'direct', 'verification': {'status': 'passed', 'source_sha': SHA, 'preview_capable': True}}]})
        self.assertTrue(self.website.capabilities['publishingReady'])

    def test_webhooks_revocation_and_reinstall_never_restore_consent(self):
        payload = {'action': 'removed', 'installation': {'id': 45}, 'repositories_removed': [{'id': 123}]}
        result = handle_website_github_event('installation_repositories', payload)
        self.assertEqual(result['revoked'], 1)
        self.website.refresh_from_db()
        self.assertEqual(self.website.state, 'revoked')
        handle_website_github_event('installation', {'action': 'created', 'installation': {'id': 45}})
        self.website.refresh_from_db()
        self.assertEqual(self.website.state, 'revoked')

    def test_authorize_api_never_mints_a_token_on_denial(self):
        factory = APIRequestFactory()
        request = factory.get('/authorize', {**self.binding, 'connection_generation': 20}, HTTP_X_API_KEY='synthetic-test-key')
        response = WebsiteConnectionAuthorizeView.as_view()(request)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'website_connection_changed')
        self.assertNotIn('github_token', response.data)

    def test_ledger_retry_deduplicates_same_patch_and_rejects_different_patch(self):
        payload = {**self.binding, 'operation_id': 'run:patch', 'base_sha': SHA,
            'files': [{'path': 'content/new.md', 'ownership': 'created', 'after_sha256': 'b' * 64}]}
        factory = APIRequestFactory()
        def post(data):
            return WebsiteMutationView.as_view()(factory.post('/mutations', data, format='json', HTTP_X_API_KEY='synthetic-test-key'))
        self.assertEqual(post(payload).status_code, 201)
        self.assertEqual(post({**payload, 'head_sha': 'c' * 40, 'status': 'applied'}).status_code, 200)
        changed = deepcopy(payload); changed['files'][0]['path'] = 'other.md'
        self.assertEqual(post(changed).status_code, 409)
        self.assertEqual(WebsiteRepositoryMutation.objects.count(), 1)
        self.assertEqual(post({**payload, 'status': 'proposed'}).status_code, 200)
        self.assertEqual(WebsiteRepositoryMutation.objects.get().status, 'applied')

    def _fenced_mutation_payload(self, state):
        operation = WebsiteConnectionOperation.objects.create(connection=self.website,
            generation=self.website.generation, action='workflow', state=state,
            idempotency_key=f'preview-ledger:{state}',
            payload={'workflow': 'article_generation', 'attempt': 1, 'run_id': f'preview-ledger-{state}'})
        binding = {**self.binding, 'operation_id': str(operation.pk), 'operation_attempt': 1,
            'deletion_epoch': 0}
        run = ContentFactoryRun.objects.create(run_id=operation.payload['run_id'],
            workflow='article_generation', domain=self.org.domain, organization=self.org,
            github_repo=self.website.github_repo, run_request=binding, status=state)
        return operation, {**binding, 'run_id': run.run_id, 'mutation_id': f'preview-patch:{state}',
            'base_sha': SHA, 'expected_source_sha': SHA,
            'branch': f'cf-review/{run.run_id}', 'status': 'proposed',
            'files': [{'path': 'content/new.md', 'ownership': 'created', 'after_sha256': 'b' * 64}]}

    def _post_mutation(self, payload):
        return WebsiteMutationView.as_view()(APIRequestFactory().post('/mutations', payload,
            format='json', HTTP_X_API_KEY='synthetic-test-key'))

    def test_preview_ledger_states_do_not_transition_terminal_workflows(self):
        for state in ('failed', 'blocked', 'completed'):
            with self.subTest(state=state):
                operation, payload = self._fenced_mutation_payload(state)
                response = self._post_mutation(payload)
                self.assertEqual(response.status_code, 201, response.data)
                response = self._post_mutation({**payload, 'status': 'applied', 'head_sha': 'c' * 40})
                self.assertEqual(response.status_code, 200, response.data)
                response = self._post_mutation(payload)
                self.assertEqual(response.status_code, 200, response.data)
                row = WebsiteRepositoryMutation.objects.get(pk=response.data['id'])
                self.assertEqual(row.status, 'applied')
                self.assertEqual(row.head_sha, 'c' * 40)
                operation.refresh_from_db()
                self.assertEqual(operation.state, state)

    def test_mutation_ledger_still_rejects_cancelled_and_superseded_attempts(self):
        for state in ('cancelled', 'failed'):
            with self.subTest(state=state):
                operation, payload = self._fenced_mutation_payload(state)
                if state == 'failed':
                    operation.payload = {**operation.payload, 'attempt': 2}
                    operation.save(update_fields=['payload'])
                response = self._post_mutation(payload)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.data['code'],
                    'website_operation_cancelled' if state == 'cancelled' else 'website_operation_changed')
        self.assertFalse(WebsiteRepositoryMutation.objects.exists())

    def test_terminal_workflow_callback_is_still_rejected_after_ledger_receipt(self):
        operation, payload = self._fenced_mutation_payload('failed')
        self.assertEqual(self._post_mutation(payload).status_code, 201)
        with self.assertRaises(WebsiteAuthorityError) as error:
            with authority_guard({**payload, 'status': 'completed', 'event_type': 'article_review_ready'}, action='read'):
                self.fail('A late callback cannot revive a failed workflow.')
        self.assertEqual(error.exception.code, 'website_operation_terminal')
        operation.refresh_from_db()
        self.assertEqual(operation.state, 'failed')

    @patch('content_factory.website_tokens.revoke_generation_tokens', return_value={'revoked': 1, 'pending': 0})
    @patch('content_factory.vibe_marketing_views._content_factory_remote_config', return_value={'enabled': True, 'base_url': 'https://worker.example.test'})
    @patch('integrations.http_client.post')
    def test_terminal_preview_cleanup_requires_confirmed_stop_without_cancelling_content(self, post, remote, revoke):
        from datetime import timedelta
        from django.utils import timezone
        run = ContentFactoryRun.objects.create(run_id='completed-preview', workflow='article_generation',
            domain=self.org.domain, organization=self.org, github_repo=self.website.github_repo,
            run_request=self.binding, status='completed')
        operation = transition_connection(self.config, action='disconnect', expected=self.binding)
        self.assertEqual(operation.payload['cancel_run_ids'], [])
        self.assertEqual(operation.payload['stop_preview_run_ids'], [run.run_id])
        now = timezone.now()
        for code, body, state in ((409, {'cleanup_success': True}, 'pending'),
                (200, {'cleanup_pending': True}, 'pending'),
                (200, {'cleanup_success': True, 'cleanup_pending': False, 'status': 'stopped'}, 'completed')):
            post.return_value = SimpleNamespace(status_code=code, json=lambda: body)
            process_website_connection_operations(now=now)
            operation.refresh_from_db(); run.refresh_from_db()
            self.assertEqual(operation.state, state)
            self.assertEqual(run.status, 'completed')
            self.assertTrue(post.call_args.args[0].endswith('/preview/stop'))
            now = operation.next_attempt_at + timedelta(seconds=1)
        self.assertFalse(operation.receipt['remote_cleanup_pending'])

    @patch('content_factory.website_tokens.revoke_generation_tokens', return_value={'revoked': 1, 'pending': 0})
    def test_revocation_outbox_retries_and_completes(self, revoke):
        op = transition_connection(self.config, action='disconnect', expected=self.binding)
        self.assertEqual(op.state, 'pending')
        result = process_website_connection_operations()
        op.refresh_from_db()
        self.assertEqual(result['completed'], 1)
        self.assertEqual(op.state, 'completed')
        revoke.assert_called_once_with(self.website.pk, 1)


class WebsiteConcurrentDisconnectTests(WebsiteDatabaseFixture, TransactionTestCase):
    def test_disconnect_serializes_with_config_write(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Real row locks require PostgreSQL')
        started, release = threading.Event(), threading.Event()
        errors = []
        def write():
            close_old_connections()
            try:
                with authority_guard(self.binding, action='config_write'):
                    started.set()
                    if not release.wait(5):
                        raise AssertionError('release not signalled')
                    OrganizationContentConfig.objects.filter(pk=self.config.pk).update(scan_summary='before disconnect')
            except Exception as exc:
                errors.append(exc)
            finally:
                connections.close_all()
        thread = threading.Thread(target=write); thread.start()
        self.assertTrue(started.wait(5))
        timer = threading.Timer(0.1, release.set); timer.start()
        transition_connection(self.config, action='disconnect', expected=self.binding)
        thread.join(5); timer.join()
        self.assertFalse(errors)
        with self.assertRaises(WebsiteAuthorityError):
            with authority_guard(self.binding, action='config_write'):
                OrganizationContentConfig.objects.filter(pk=self.config.pk).update(scan_summary='late write')
        self.config.refresh_from_db()
        self.assertEqual(self.config.scan_summary, 'before disconnect')


@override_settings(ROO_API_KEY='synthetic-test-key', INTERNAL_API_KEY='synthetic-test-key')
class WebsiteCheckpointSourceTests(WebsiteDatabaseFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        record_scan_evidence(self.website, {'source_sha': SHA, 'publish_targets': [{
            'target_id': 'native', 'publish_capability': 'direct',
            'verification': {'status': 'passed', 'source_sha': SHA, 'preview_capable': True},
        }]})

    def _checkpoint(self, **changes):
        from .service_views import ContentFactoryOrgConfigView
        data = {**self.binding, 'repo_head_sha': SHA,
            'repo_execution_contract': {'framework': 'nextjs', 'source_sha': SHA}, **changes}
        return ContentFactoryOrgConfigView.as_view()(APIRequestFactory().put(
            '/org-config', data, format='json', HTTP_X_API_KEY='synthetic-test-key'))

    def test_source_checkpoint_preserves_accepted_proof_and_verifies_outside_transaction(self):
        proof = self.website.targets.get(target_key='native')
        def verify(current, sha):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(current.pk, self.website.pk)
            self.assertEqual(sha, SHA)
            return sha
        with patch('content_factory.website_connections.verify_repository_head', side_effect=verify) as head:
            response = self._checkpoint()
        self.assertEqual(response.status_code, 200, response.data)
        head.assert_called_once()
        self.config.refresh_from_db()
        self.website.refresh_from_db()
        retained = self.website.targets.get(target_key='native')
        self.assertEqual(retained.contract, proof.contract)
        self.assertEqual(retained.verified_at, proof.verified_at)
        self.assertEqual(self.website.verified_sha, SHA)
        self.assertTrue(self.website.capabilities['publishingReady'])
        self.assertEqual(self.config.repo_execution_contract['framework'], 'nextjs')
        self.assertEqual(self.config.article_template, '# Article\nTemplate')
        self.assertEqual(self.config.design_guide, '# Design\nGuide')

    def test_changed_provider_source_rejects_checkpoint_without_partial_writes(self):
        before = deepcopy(self.config.repo_execution_contract)
        snapshot_count = self.website.scan_snapshots.count()
        with patch('content_factory.website_connections.verify_repository_head',
                side_effect=WebsiteAuthorityError('website_source_changed', 'Source changed.')) as head:
            response = self._checkpoint()
        self.assertEqual(response.status_code, 409, response.data)
        head.assert_called_once()
        self.config.refresh_from_db()
        self.website.refresh_from_db()
        self.assertEqual(self.config.repo_execution_contract, before)
        self.assertEqual(self.website.scan_snapshots.count(), snapshot_count)
        self.assertEqual(self.website.verified_sha, SHA)
        self.assertTrue(self.website.targets.get(target_key='native').capabilities['publishingReady'])


@override_settings(ROO_API_KEY='synthetic-test-key', INTERNAL_API_KEY='synthetic-test-key')
class WebsiteServiceBoundaryTests(WebsiteDatabaseFixture, TestCase):
    def _preview_token_request(self, **changes):
        from .service_views import ContentFactoryTokenView
        data = {**self.binding, 'run_id': 'article-preview', 'expected_source_sha': SHA,
            'permission_mode': 'write', 'action': 'preview', **changes}
        return ContentFactoryTokenView.as_view()(APIRequestFactory().get('/token', data, HTTP_X_API_KEY='synthetic-test-key'))

    def _preview_token(self):
        return SimpleNamespace(token='synthetic-preview-token', as_content_factory_payload=lambda **kwargs: {
            'github_token': 'synthetic-preview-token', 'github_repo': self.website.github_repo,
            'token_source': 'github_app_installation'})

    @patch('integrations.services.github_app.create_installation_access_token')
    def test_preview_write_token_does_not_require_publication_receipt(self, mint):
        self.website.capabilities = {'publishingReady': False, 'generationReady': True}
        self.website.save(update_fields=['capabilities'])
        ContentFactoryRun.objects.create(run_id='article-preview', workflow='article_generation',
            organization=self.org, domain=self.org.domain, github_repo=self.website.github_repo, run_request=self.binding)
        mint.return_value = self._preview_token()
        response = self._preview_token_request()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['permission_mode'], 'write')
        self.assertEqual(response.data['permission_profile'], 'repository')
        mint.assert_called_once_with(installation_id='45', repository='example/site', repository_id=123,
            permission_mode='write', use_cache=False)
        self.assertEqual(self._preview_token_request(action='publish').status_code, 409)
        self.assertEqual(mint.call_count, 1)

    @patch('integrations.services.github_app.create_installation_access_token')
    def test_preview_write_requires_source_and_run_and_forbids_workflow_profile(self, mint):
        for changes in ({'expected_source_sha': ''}, {'expected_source_sha': 'main'}, {'run_id': ''},
                {'permission_profile': 'workflow_files'}, {'permission_profile': 'ci_evidence'}):
            with self.subTest(changes=changes):
                response = self._preview_token_request(**changes)
                self.assertEqual(response.status_code, 400, response.data)
        mint.assert_not_called()

    @patch('integrations.services.github_app.create_installation_access_token')
    def test_preview_write_rechecks_source_and_disconnection(self, mint):
        with patch('content_factory.website_connections.verify_repository_head', side_effect=WebsiteAuthorityError(
                'website_source_changed', 'Source changed.')):
            self.assertEqual(self._preview_token_request().status_code, 409)
        transition_connection(self.config, action='disconnect', expected=self.binding)
        self.assertEqual(self._preview_token_request().status_code, 409)
        mint.assert_not_called()

    @patch('integrations.http_client.delete')
    @patch('integrations.services.github_app.create_installation_access_token')
    def test_preview_token_minted_during_disconnect_is_revoked_before_delivery(self, mint, revoke):
        def disconnect(**kwargs):
            transition_connection(self.config, action='disconnect', expected=self.binding)
            return self._preview_token()
        mint.side_effect = disconnect
        response = self._preview_token_request()
        self.assertEqual(response.status_code, 409, response.data)
        revoke.assert_called_once()

    def _put_config(self, data):
        from .service_views import ContentFactoryOrgConfigView
        return ContentFactoryOrgConfigView.as_view()(APIRequestFactory().put('/org-config', data, format='json', HTTP_X_API_KEY='synthetic-test-key'))

    def test_legacy_config_write_is_rejected_without_connection_contract(self):
        response = self._put_config({'domain': self.org.domain, 'article_template': '# New template'})
        self.assertEqual(response.status_code, 409)
        self.config.refresh_from_db()
        self.assertEqual(self.config.article_template, '# Article\nTemplate')

    def test_contaminated_template_never_replaces_valid_template(self):
        response = self._put_config({**self.binding, 'article_template': '# EXISTING ARTIFACT\nBad'})
        self.assertEqual(response.status_code, 422)
        self.config.refresh_from_db()
        self.assertEqual(self.config.article_template, '# Article\nTemplate')

    def test_inventory_partial_config_write_preserves_targets_and_templates(self):
        response = self._put_config({**self.binding, 'repository_inventory': {'source_sha': SHA, 'discovery_complete': True}})
        self.assertEqual(response.status_code, 200)
        self.config.refresh_from_db()
        self.assertEqual(self.config.publish_targets, [{'target_id': 'native'}])
        self.assertEqual(self.config.article_template, '# Article\nTemplate')
        self.assertEqual(self.website.scan_snapshots.count(), 1)

    def test_late_sync_cannot_restore_cancelled_run(self):
        from .service_views import ContentFactoryRunView
        ContentFactoryRun.objects.create(run_id='late', workflow='article_generation', domain=self.org.domain,
            organization=self.org, github_repo=self.website.github_repo, run_request=self.binding, status='running')
        transition_connection(self.config, action='disconnect', expected=self.binding)
        data = {**self.binding, 'run_id': 'late', 'workflow': 'article_generation', 'status': 'completed', 'result': {'pr_url': 'https://github.com/example/site/pull/1'}}
        response = ContentFactoryRunView.as_view()(APIRequestFactory().put('/run/late', data, format='json', HTTP_X_API_KEY='synthetic-test-key'), run_id='late')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(ContentFactoryRun.objects.get(run_id='late').status, 'cancelled')

    @patch('integrations.services.github_app.create_installation_access_token')
    def test_revoked_token_request_never_falls_back_to_oauth(self, mint):
        from .service_views import ContentFactoryTokenView
        transition_connection(self.config, action='disconnect', expected=self.binding)
        response = ContentFactoryTokenView.as_view()(APIRequestFactory().get('/token', {**self.binding, 'permission_mode': 'write', 'action': 'setup'}, HTTP_X_API_KEY='synthetic-test-key'))
        self.assertEqual(response.status_code, 409)
        mint.assert_not_called()

    def test_original_job_binding_is_required_for_resumption(self):
        from .models import ContentFactoryJob
        from .website_connections import dispatch_contract
        job = ContentFactoryJob.objects.create(job_id='saved-job', domain=self.org.domain, status='queued', request_meta=self.binding)
        transition_connection(self.config, action='pause', expected=self.binding)
        with self.assertRaises(WebsiteAuthorityError):
            dispatch_contract(self.org.domain, {}, source_run_id=job.job_id)

    @patch('content_factory.website_reconciliation._cleanup_proposal')
    def test_cleanup_proposal_has_no_write_side_effects(self, proposal):
        proposal.return_value = {'status': 'review_required', 'source_sha': SHA, 'proposal_digest': 'digest', 'deletions': ['integration/support.md'], 'conflicts': [], 'retained': [], 'repository_modified': False}
        op = transition_connection(self.config, action='cleanup', expected=self.binding)
        process_website_connection_operations()
        op.refresh_from_db()
        self.assertEqual(op.state, 'review_required')
        self.assertFalse(op.receipt['repository_modified'])
        self.assertEqual(self.website.generation, 1)

    def test_normalization_preserves_unlink_marker(self):
        from .article_system import normalize_article_system, merge_article_system
        current = {'state': 'existing', 'publish_disconnected_at': '2026-10-04T00:00:00Z'}
        self.assertEqual(normalize_article_system(current)['publish_disconnected_at'], current['publish_disconnected_at'])
        self.assertEqual(merge_article_system(current, {'state': 'existing'})['publish_disconnected_at'], current['publish_disconnected_at'])


class WebsiteCleanupExecutionTests(WebsiteDatabaseFixture, TestCase):
    @patch('content_factory.website_reconciliation._cleanup_proposal')
    @patch('content_factory.website_connections.verify_repository_access')
    @patch('integrations.services.github_app.create_installation_access_token')
    @patch('integrations.http_client.delete')
    @patch('integrations.http_client.get')
    @patch('integrations.http_client.request')
    def test_explicit_review_opens_deletion_pr_and_never_merges(self, request, get, delete, mint, verify, proposal):
        from .website_reconciliation import approve_cleanup_proposal
        receipt = {'status': 'review_required', 'source_sha': SHA, 'proposal_digest': 'digest', 'deletions': ['integration/support.md'], 'conflicts': [], 'retained': [], 'repository_modified': False}
        op = WebsiteConnectionOperation.objects.create(connection=self.website, generation=1, idempotency_key='cleanup:test', action='cleanup', state='review_required', receipt=receipt)
        verify.return_value = {'repository_id': 123, 'installation_id': '45'}
        mint.return_value = SimpleNamespace(token='synthetic-ephemeral-token')
        proposal.return_value = receipt
        def response(data, status=200):
            return SimpleNamespace(status_code=status, json=lambda: data, raise_for_status=lambda: None)
        get.side_effect = [response({}, 404), response([])]
        request.side_effect = [response({'tree': {'sha': 'base-tree'}}), response({'sha': 'cleanup-tree'}), response({'sha': 'b' * 40}), response({'ref': 'created'}), response({'html_url': 'https://github.com/example/site/pull/12'})]
        result = approve_cleanup_proposal(self.config, user=SimpleNamespace(pk=17), data={**self.binding, 'operation_id': str(op.pk), 'source_sha': SHA, 'proposal_digest': 'digest'})
        self.assertEqual(result.state, 'awaiting_merge')
        self.assertEqual(result.payload['approved_cleanup'], {'source_sha': SHA, 'proposal_digest': 'digest',
            'deletions': ['integration/support.md'], 'approved_by_user_id': '17'})
        self.assertFalse(result.receipt['default_branch_modified'])
        self.assertEqual(result.receipt['pr_url'], 'https://github.com/example/site/pull/12')
        self.assertEqual(request.call_count, 5)
        self.assertTrue(all('/merge' not in call.args[1] for call in request.call_args_list))
        deletion = request.call_args_list[1].kwargs['json']['tree']
        self.assertEqual(deletion, [{'path': 'integration/support.md', 'mode': '100644', 'type': 'blob', 'sha': None}])
        delete.assert_called_once()
        # Identical approved action is idempotent, without another Git mutation.
        retried = approve_cleanup_proposal(self.config, user=SimpleNamespace(pk=17), data={**self.binding, 'operation_id': str(op.pk), 'source_sha': SHA, 'proposal_digest': 'digest'})
        self.assertEqual(retried.pk, op.pk)
        self.assertEqual(request.call_count, 5)

    @patch('content_factory.website_connections.verify_repository_access')
    def test_cleanup_requires_exact_review_digest(self, verify):
        from .website_reconciliation import approve_cleanup_proposal
        verify.return_value = {'repository_id': 123, 'installation_id': '45'}
        op = WebsiteConnectionOperation.objects.create(connection=self.website, generation=1, idempotency_key='cleanup:digest', action='cleanup', state='review_required', receipt={'source_sha': SHA, 'proposal_digest': 'expected'})
        with self.assertRaises(WebsiteAuthorityError):
            approve_cleanup_proposal(self.config, user=SimpleNamespace(pk=17), data={**self.binding, 'operation_id': str(op.pk), 'source_sha': SHA, 'proposal_digest': 'stale'})

    def test_default_branch_push_fences_old_proof_idempotently(self):
        payload = {'repository': {'id': 123}, 'ref': 'refs/heads/main', 'after': SHA}
        handle_website_github_event('push', payload)
        self.website.refresh_from_db()
        self.assertEqual(self.website.generation, 1)
        self.assertEqual(self.website.configuration_version, 2)
        self.assertFalse(self.website.capabilities['publishingReady'])
        self.config.refresh_from_db()
        self.assertEqual(self.config.article_template, '# Article\nTemplate')
        handle_website_github_event('push', payload)
        self.website.refresh_from_db()
        self.assertEqual(self.website.generation, 1)

    def test_other_branch_push_keeps_selected_source(self):
        handle_website_github_event('push', {'repository': {'id': 123}, 'ref': 'refs/heads/other', 'after': SHA})
        self.website.refresh_from_db()
        self.assertEqual(self.website.generation, 1)


class WebsiteSourceFenceTests(WebsiteDatabaseFixture, TestCase):
    def test_old_verified_callback_cannot_restore_after_push(self):
        from .website_connections import guarded_service_write
        from rest_framework.response import Response
        new_sha = 'c' * 40
        handle_website_github_event('push', {'repository': {'id': 123}, 'ref': 'refs/heads/main', 'after': new_sha})
        self.website.refresh_from_db()
        class Endpoint:
            @guarded_service_write('config_write', only_repository=True)
            def put(self, request):
                OrganizationContentConfig.objects.filter(pk=self_config.pk).update(publish_targets=request.data['publish_targets'])
                return Response({'saved': True})
        self_config = self.config
        data = {**self.binding, 'source_sha': SHA, 'publish_targets': [{'target_id': 'native', 'publish_capability': 'direct', 'verification': {'status': 'passed', 'source_sha': SHA}}]}
        from rest_framework.request import Request
        from rest_framework.parsers import JSONParser
        request = Request(APIRequestFactory().put('/config', data, format='json'), parsers=[JSONParser()])
        with patch('content_factory.website_connections.verify_repository_head', side_effect=WebsiteAuthorityError('website_source_changed', 'Current source differs.')):
            response = Endpoint().put(request)
        self.assertEqual(response.status_code, 409)
        self.website.refresh_from_db()
        self.assertFalse(self.website.capabilities['publishingReady'])
        self.assertEqual(self.website.generation, 1)

    def test_current_verified_default_source_can_promote_after_push(self):
        new_sha = 'c' * 40
        handle_website_github_event('push', {'repository': {'id': 123}, 'ref': 'refs/heads/main', 'after': new_sha})
        self.website.refresh_from_db()
        record_scan_evidence(self.website, {'source_sha': new_sha, 'publish_targets': [{'target_id': 'native', 'publish_capability': 'direct', 'verification': {'status': 'passed', 'source_sha': new_sha}}]})
        self.assertTrue(self.website.capabilities['publishingReady'])
        self.assertEqual(self.website.generation, 1)
        self.assertFalse(any(item.get('code') == 'repository_source_changed' for item in self.website.blockers))


class WebsiteReviewRegressionTests(WebsiteDatabaseFixture, TestCase):
    @patch('content_factory.website_connections.verify_repository_access')
    def test_archives_old_repository_seeds_under_original_connection(self, verify):
        verify.return_value = {'repository_id': 987, 'github_repo': 'other/site', 'installation_id': '99', 'branch': 'main'}
        new = bind_website(self.config, user=None, repo='other/site', expected=self.binding)
        self.assertTrue(WebsiteTemplateRevision.objects.filter(connection=self.website, purpose='article_template').exists())
        self.assertFalse(WebsiteTemplateRevision.objects.filter(connection=new).exists())

    @patch('content_factory.website_connections.verify_repository_access')
    def test_reconnect_requires_reviewed_binding(self, verify):
        verify.return_value = {'repository_id': 123, 'github_repo': 'example/site', 'installation_id': '45', 'branch': 'main'}
        transition_connection(self.config, action='disconnect', expected=self.binding)
        with self.assertRaises(WebsiteAuthorityError):
            bind_website(self.config, user=None, repo='example/site', reconnect=True, expected={})
        self.website.refresh_from_db()
        self.assertEqual(self.website.state, 'disconnected')

    @patch('content_factory.website_connections.verify_repository_access')
    def test_explicit_default_branch_selection_rebinds_custom_branch(self, verify):
        self.website.branch = 'custom'; self.website.save()
        verify.return_value = {'repository_id': 123, 'github_repo': 'example/site', 'installation_id': '45', 'branch': 'main'}
        selected = bind_website(self.config, user=None, repo='example/site', branch='', expected=self.binding)
        self.assertEqual(selected.branch, 'main')
        self.assertNotEqual(selected.pk, self.website.pk)

    def test_new_inventory_sha_invalidates_previous_publish_capability(self):
        self.website.verified_sha = SHA; self.website.save()
        record_scan_evidence(self.website, {'repository_inventory': {'source_sha': 'd' * 40, 'discovery_complete': True}})
        self.assertFalse(self.website.capabilities['publishingReady'])
        self.assertFalse(self.website.capabilities['previewSupported'])

    def test_background_backend_merge_is_fenced_by_original_run(self):
        from .website_connections import guarded_backend_run_action
        run = ContentFactoryRun.objects.create(run_id='background', workflow='article_system_setup', domain=self.org.domain,
            organization=self.org, github_repo=self.website.github_repo, run_request=self.binding, status='running')
        transition_connection(self.config, action='disconnect', expected=self.binding)
        calls = []
        @guarded_backend_run_action('setup')
        def merge(*, run):
            calls.append(run.pk)
            return {'outcome': 'merged'}
        result = merge(run=run)
        self.assertEqual(result['outcome'], 'error')
        self.assertFalse(calls)
