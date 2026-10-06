"""Combined activation and durable website consent on synthetic database rows."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from .activation import article_capabilities, founder_context_for_domain, integration_evidence
from .models import OrganizationContentConfig
from .portable_drafts import explicit_portable_request, original_portable_run
from .service_views import ContentFactoryOrgConfigView
from .vibe_marketing_views import VibeMarketingArticleView, VibeMarketingRunControlView, _queue_content_factory_run, _setup_blocked_response_for_generation
from .vibe_marketing_views import _sync_local_run_from_remote
from .website_connections import authority_guard, contract_for
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnection, WebsiteConnectionTarget, WebsiteScanSnapshot
from .website_tokens import mint_website_token
from .website_views import WebsiteConnectionAuthorizeView

SHA = 'a' * 40


@override_settings(ROO_API_KEY='synthetic-test-key', INTERNAL_API_KEY='synthetic-test-key')
class ActivationConnectionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email='activation@example.test')
        self.profile = VibeRaisingProfile.objects.create(user=self.user, role='founder')
        self.org = Organization.objects.create(domain='activation.example.test', name='Synthetic activation')
        self.company = VibeRaisingCompany.objects.create(profile=self.profile, organization=self.org, domain=self.org.domain, name='Synthetic')
        self.config = OrganizationContentConfig.objects.create(organization=self.org, github_repo='example/site',
            default_publish_target_id='native', article_delivery_mode='content_only', pillar_strategy={})
        self.context = founder_context_for_domain(self.user, self.org.domain)
        self.factory = APIRequestFactory()

    def website(self, **changes):
        values = dict(organization=self.org, authorized_by=self.user, github_repo=self.config.github_repo,
            repository_id=123, installation_id='42', branch='main', verified_sha=SHA,
            last_verified_at=timezone.now(), capabilities={'publishingReady': True, 'generationReady': True})
        values.update(changes)
        self.connection = WebsiteConnection.objects.create(**values)
        self.config.website_connection = self.connection
        self.config.save(update_fields=['website_connection'])
        WebsiteConnectionTarget.objects.create(connection=self.connection, target_key='native', generation=self.connection.generation,
            source_sha=SHA, verified_at=timezone.now(), contract={'route_path': '/stories'}, capabilities={'publishingReady': True})
        return self.connection

    def admission(self, **data):
        params = {'article_admission': '1', 'domain': self.org.domain,
            'requested_by_slack_user_id': f'mlai_user:{self.user.pk}', **data}
        request = self.factory.get('/synthetic', params, HTTP_X_API_KEY='synthetic-test-key')
        return ContentFactoryOrgConfigView.as_view()(request)

    def test_confirmed_portable_admission_has_no_repository_or_github_requirement(self):
        with patch('content_factory.vibe_marketing_views._verify_github_repository_access', side_effect=AssertionError('Portable admission must not contact GitHub')):
            response = self.admission(delivery_mode='content_only', delivery_mode_confirmed='true')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data['articleCapabilities']['canGeneratePortableDraft'])
        self.assertFalse(response.data['articleCapabilities']['canGenerateArticle'])
        self.assertEqual(response.data['github_repo'], '')
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        for values in ({}, {'delivery_mode': 'content_only'}, {'delivery_mode': 'content_only', 'delivery_mode_confirmed': 'false'}):
            self.assertNotEqual(self.admission(**values).status_code, 200)
        for partial in ({'repository_id': 123}, {'connection_target_id': 'native'}):
            response = self.admission(delivery_mode='content_only', delivery_mode_confirmed='true', **partial)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.data['code'], 'invalid_connection_contract')

    def test_portable_admission_uses_real_founder_ownership_not_actor_or_domain_alone(self):
        other = get_user_model().objects.create_user(email='other-founder@example.test')
        VibeRaisingProfile.objects.create(user=other, role='founder')
        response = self.admission(delivery_mode='content_only', delivery_mode_confirmed='true', requested_by_slack_user_id=f'mlai_user:{other.pk}')
        self.assertEqual(response.status_code, 403)

    def test_current_disconnected_review_can_admit_portable_but_stale_review_cannot(self):
        website = self.website(state='disconnected')
        binding = contract_for(website)
        response = self.admission(delivery_mode='content_only', delivery_mode_confirmed='true', **binding)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['website_connection_id'], str(website.pk))
        self.assertEqual(response.data['connection_generation'], website.generation)
        response = self.admission(delivery_mode='content_only', delivery_mode_confirmed='true', **{**binding, 'connection_generation': website.generation + 1})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'website_connection_changed')

    def test_portable_authorization_never_grants_read_token_or_source_refresh(self):
        website = self.website(state='disconnected')
        binding = contract_for(website)
        WebsiteScanSnapshot.objects.create(connection=website, generation=website.generation, run_id='observed',
            source_sha=SHA, detector_version='github_head', fingerprint='synthetic', evidence={})
        with patch('content_factory.website_connections.verify_repository_head', side_effect=AssertionError('No remote portable check')):
            request = self.factory.get('/synthetic', {**binding, 'action': 'portable', 'expected_source_sha': SHA.upper()}, HTTP_X_API_KEY='synthetic-test-key')
            response = WebsiteConnectionAuthorizeView.as_view()(request)
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data['permission_mode'], 'none')
            self.assertEqual(response.data['capabilities'], {})
            self.assertEqual(response.data['expected_source_sha'], SHA)
            response = self.admission(delivery_mode='content_only', delivery_mode_confirmed='true', expected_source_sha=SHA.upper(), **binding)
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data['expected_source_sha'], SHA)
            with self.assertRaises(WebsiteAuthorityError) as error:
                with authority_guard({**binding, 'expected_source_sha': 'b' * 40}, action='portable'):
                    pass
            self.assertEqual(error.exception.code, 'website_source_changed')
        with patch('integrations.services.github_app.create_installation_access_token') as mint:
            with self.assertRaises(WebsiteAuthorityError) as error:
                mint_website_token(binding, action='portable')
            self.assertEqual(error.exception.code, 'portable_repository_access_denied')
            mint.assert_not_called()
        with self.assertRaises(WebsiteAuthorityError):
            with authority_guard(binding, action='read'):
                pass

    def test_current_durable_target_replaces_legacy_scan_authority(self):
        website = self.website()
        # No legacy ready scan is needed once a durable target is verified.
        evidence = integration_evidence(self.config)
        self.assertTrue(evidence['verified'], evidence)
        self.assertEqual(evidence['routePath'], '/stories')
        access = {'verified': True, 'branch': 'main', 'sha': SHA}
        caps = article_capabilities(self.config, domain=self.org.domain, account={'saved': True, 'owned': True}, evidence=evidence, repository_access=access)
        self.assertTrue(caps['canGenerateArticle'])
        self.assertFalse(caps['canPublishArticle'])
        for update in ({'state': 'disconnected'}, {'state': 'paused'}, {'generation': 2},
            {'verified_sha': 'b' * 40}, {'last_verified_at': timezone.now() - timedelta(days=8)}, {'app_root': 'apps/site'}):
            original = {key: getattr(website, key) for key in update}
            for key, value in update.items():
                setattr(website, key, value)
            self.assertFalse(integration_evidence(self.config)['verified'], update)
            for key, value in original.items():
                setattr(website, key, value)

    def test_operator_pause_prevents_repository_admission_without_blocking_portable(self):
        self.website()
        with patch.dict('os.environ', {'WEBSITE_CONNECTION_WRITE_MODE': 'disabled'}):
            evidence = integration_evidence(self.config)
            self.assertFalse(evidence['verified'])
            self.assertEqual(evidence['reasonCode'], 'website_writes_paused')
            self.assertEqual(self.admission(delivery_mode='content_only', delivery_mode_confirmed='true').status_code, 200)

    def test_durable_admission_checks_actual_provider_head_before_repository_generation(self):
        from .vibe_marketing_views import _article_capabilities_for_context
        from integrations.services.github_app import GitHubInstallationToken
        website = self.website()
        credential = GitHubInstallationToken(token='synthetic-only', expires_at=timezone.now() + timedelta(minutes=30),
            installation_id=website.installation_id, repository=website.github_repo)
        def provider(url, **kwargs):
            if url.endswith('/commits/main'):
                return SimpleNamespace(status_code=200, json=lambda: {'sha': SHA if not changed[0] else 'b' * 40})
            return SimpleNamespace(status_code=200, json=lambda: {'id': website.repository_id, 'full_name': website.github_repo,
                'default_branch': 'main', 'permissions': {'push': True}})
        changed = [False]
        with patch('integrations.services.github_app.create_installation_access_token', return_value=credential), \
             patch('content_factory.website_connections.read_repository_native_target', return_value={'id': website.repository_id,
                 'full_name': website.github_repo, 'default_branch': 'main'}), \
             patch('content_factory.vibe_marketing_views.http_client.delete') as revoke, \
             patch('content_factory.vibe_marketing_views.http_client.get', side_effect=provider) as reads:
            caps = _article_capabilities_for_context(self.context, self.config, latest_runs=[], force=True)
            self.assertTrue(caps['canGenerateArticle'], caps)
            changed[0] = True
            caps = _article_capabilities_for_context(self.context, self.config, latest_runs=[], force=True)
            self.assertFalse(caps['canGenerateArticle'])
            self.assertEqual(caps['reasonCode'], 'verification_stale')
            self.assertEqual(reads.call_count, 4)
            self.assertTrue(revoke.called)
            self.assertEqual(revoke.call_args.args[0], "https://api.github.com/installation/token")

    def test_legacy_charge_admission_preserves_founder_ownership_for_portable(self):
        from integrations.services.article_generation import ArticleGenerationError, require_article_activation
        request = {'delivery_mode': 'content_only', 'delivery_mode_confirmed': True}
        admitted = require_article_activation(domain=self.org.domain, actor_id=f'mlai_user:{self.user.pk}', user=self.user,
            article_request=request)
        self.assertEqual(admitted.pk, self.config.pk)
        other = get_user_model().objects.create_user(email='legacy-other@example.test')
        VibeRaisingProfile.objects.create(user=other, role='founder')
        with self.assertRaises(ArticleGenerationError):
            require_article_activation(domain=self.org.domain, actor_id=f'mlai_user:{other.pk}', user=other, article_request=request)

    def test_repository_worker_requires_matching_reviewed_contract_and_fresh_readiness(self):
        website = self.website()
        binding = contract_for(website)
        response = self.admission(github_repo=website.github_repo)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'website_connection_required')
        with patch('content_factory.vibe_marketing_views._article_capabilities_for_context', return_value={'canGenerateArticle': True, 'canPublishArticle': True}) as caps:
            self.assertEqual(self.admission(**binding).status_code, 200)
            self.assertTrue(caps.call_args.kwargs['force'])
            self.assertEqual(self.admission(**{**binding, 'connection_generation': 2}).status_code, 409)
            self.assertEqual(caps.call_count, 1)

    def test_owner_portable_wrapper_checks_review_before_any_billing_or_dispatch(self):
        website = self.website(state='disconnected')
        data = {'delivery_mode': 'content_only', 'delivery_mode_explicit': True, **contract_for(website)}
        for generation, expected_status in ((website.generation, 400), (website.generation + 1, 409)):
            request = self.factory.post('/synthetic', {**data, 'connection_generation': generation}, format='json')
            force_authenticate(request, user=self.user)
            with patch('content_factory.vibe_marketing_views._resolve_context_or_response', return_value=(self.context, None)), \
                 patch('content_factory.vibe_marketing_views._get_config', return_value=self.config), \
                 patch('content_factory.vibe_marketing_views._article_capabilities_for_context', side_effect=AssertionError('No repository capability probe')), \
                 patch('content_factory.vibe_marketing_views._charge_roo_points_for_article') as charge, \
                 patch('content_factory.vibe_marketing_views._queue_content_factory_run') as queue:
                response = VibeMarketingArticleView.as_view(authentication_classes=[])(request)
                self.assertEqual(response.status_code, expected_status, response.data)
                charge.assert_not_called()
                queue.assert_not_called()

    def test_portable_dispatch_strips_reviewed_repository_authority(self):
        website = self.website(state='disconnected')
        payload = {'delivery_mode': 'content_only', 'delivery_mode_confirmed': True, 'expected_source_sha': SHA, **contract_for(website)}
        with patch('content_factory.vibe_marketing_views._queue_content_factory_run_authorized', return_value='queued') as queue:
            self.assertEqual(_queue_content_factory_run(endpoint='article', workflow='direct_generate', context=self.context, config=self.config, payload=payload), 'queued')
        admitted = queue.call_args.kwargs['payload']
        self.assertEqual(admitted['github_repo'], '')
        for key in ('website_connection_id', 'connection_generation', 'repository_id', 'expected_source_sha'):
            self.assertNotIn(key, admitted)

    def test_original_portable_resume_does_not_upgrade_bound_run_or_allow_publication(self):
        run = ContentFactoryRun.objects.create(run_id='portable-resume', domain=self.org.domain, organization=self.org,
            workflow='direct_generate', run_request={'delivery_mode': 'content_only', 'delivery_mode_confirmed': True}, github_repo='')
        self.assertTrue(original_portable_run(run))
        self.assertIsNone(_setup_blocked_response_for_generation(self.context, self.config, run=run))
        blocked = {'canGenerateArticle': False, 'canPublishArticle': False, 'reasonCode': 'integration_required', 'reason': 'Verify.', 'nextStep': 'articles'}
        with patch('content_factory.vibe_marketing_views._article_capabilities_for_context', return_value=blocked):
            self.assertEqual(_setup_blocked_response_for_generation(self.context, self.config, run=run, publishing=True).status_code, 409)
            website = self.website(state='disconnected')
            run.run_request.update(contract_for(website))
            self.assertFalse(original_portable_run(run))
            self.assertEqual(_setup_blocked_response_for_generation(self.context, self.config, run=run).status_code, 409)

    def test_explicit_portable_confirmation_cannot_be_truthy_false_or_upgrade_result(self):
        for confirmed in (False, 'false', '0', None, '', {}, []):
            self.assertFalse(explicit_portable_request({'delivery_mode': 'content_only', 'delivery_mode_confirmed': confirmed}))
        self.assertFalse(explicit_portable_request({'delivery_mode': 'content_only', 'delivery_mode_confirmed': True, 'resolved_delivery_mode': 'publish_code'}))

    def test_portable_control_cannot_upgrade_mode_before_worker_or_local_write(self):
        run = ContentFactoryRun.objects.create(run_id='portable-mode-upgrade', domain=self.org.domain, organization=self.org,
            workflow='direct_generate', run_request={'delivery_mode': 'content_only', 'delivery_mode_confirmed': True}, github_repo='', result={})
        for data in ({'delivery_mode': 'review_draft'}, {'deliveryMode': 'publish_code'},
                     {'delivery_mode': 'content_only', 'resolved_delivery_mode': 'review_draft'}):
            request = self.factory.post('/synthetic', data, format='json')
            force_authenticate(request, user=self.user)
            with patch('content_factory.vibe_marketing_views._resolve_context_or_response', return_value=(self.context, None)), \
                 patch('content_factory.vibe_marketing_views._get_config', return_value=self.config), \
                 patch('content_factory.vibe_marketing_views._call_content_factory_run_action') as remote:
                response = VibeMarketingRunControlView.as_view(authentication_classes=[])(request, run_id=run.run_id, action='delivery-mode')
            self.assertEqual(response.status_code, 409, response.data)
            self.assertEqual(response.data['code'], 'portable_repository_access_denied')
            remote.assert_not_called()
            run.refresh_from_db()
            self.assertEqual(run.result, {})
            self.assertEqual(run.run_request['delivery_mode'], 'content_only')

    def test_portable_resume_uses_owner_api_and_existing_paid_intent_without_repository(self):
        original = {'delivery_mode': 'content_only', 'delivery_mode_confirmed': True,
                    'roo_points_ledger_id': 'synthetic-existing-charge', 'roo_points_cost': 6}
        run = ContentFactoryRun.objects.create(run_id='portable-paid-resume', domain=self.org.domain,
            organization=self.org, workflow='direct_generate', status='failed', error='Corpus failed',
            run_request=original, github_repo='', result={})
        request = self.factory.post('/synthetic', {}, format='json')
        force_authenticate(request, user=self.user)
        remote = SimpleNamespace(status_code=202, content=b'{}', json=lambda: {'status': 'queued', 'run_id': run.run_id})
        with patch('content_factory.vibe_marketing_views._resolve_context_or_response', return_value=(self.context, None)), \
             patch('content_factory.vibe_marketing_views._get_config', return_value=self.config), \
             patch('content_factory.vibe_marketing_views._content_factory_remote_config', return_value={'enabled': True, 'base_url': 'https://factory.example'}), \
             patch('content_factory.vibe_marketing_views._content_factory_headers', return_value={}), \
             patch('content_factory.vibe_marketing_views.http_client.post', return_value=remote) as post, \
             patch('content_factory.vibe_marketing_views.scoped_run_contract', side_effect=AssertionError('No website authority')), \
             patch('content_factory.vibe_marketing_views._serialize_run', return_value={'runId': run.run_id}):
            response = VibeMarketingRunControlView.as_view(authentication_classes=[])(request, run_id=run.run_id, action='resume')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(post.call_args.args[0], f'https://factory.example/api/runs/{run.run_id}/resume')
        run.refresh_from_db()
        self.assertEqual(run.status, 'queued')
        self.assertEqual(run.run_request, original)
        self.assertIsNone(self.config.website_connection_id)

    def test_rejected_portable_resume_keeps_persisted_failure_and_poll_repairs_phantom_queue(self):
        run = ContentFactoryRun.objects.create(run_id='portable-rejected-resume', domain=self.org.domain,
            organization=self.org, workflow='direct_generate', status='failed', error='Corpus failed', github_repo='',
            run_request={'delivery_mode': 'content_only', 'delivery_mode_confirmed': True}, result={})
        request = self.factory.post('/synthetic', {}, format='json')
        force_authenticate(request, user=self.user)
        rejection = {'status': 'blocked', 'allowed': False, 'code': 'admission_denied',
                     'detail': 'Admission needs review.', 'content_factory_status_code': 409}
        with patch('content_factory.vibe_marketing_views._resolve_context_or_response', return_value=(self.context, None)), \
             patch('content_factory.vibe_marketing_views._get_config', return_value=self.config), \
             patch('content_factory.vibe_marketing_views._call_content_factory_run_action', return_value=rejection):
            response = VibeMarketingRunControlView.as_view(authentication_classes=[])(request, run_id=run.run_id, action='resume')
        self.assertEqual(response.status_code, 409, response.data)
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertEqual(run.error, 'Corpus failed')
        self.assertEqual(run.result, {})
        run.status = 'queued'
        run.save(update_fields=['status'])
        with patch('content_factory.vibe_marketing_views.scoped_run_contract', side_effect=AssertionError('No website authority')):
            _sync_local_run_from_remote(run, {'workflow': 'direct_generate', 'domain': self.org.domain,
                'status': 'failed', 'current_step': 'plan_article', 'error': 'Corpus failed', 'resume_available': True})
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertEqual(run.current_step, 'plan_article')
        self.assertTrue(run.resume_available)
