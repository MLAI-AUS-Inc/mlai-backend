from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from unittest.mock import MagicMock, patch
import requests

from content_factory.article_system import resolve_article_system_with_source
from content_factory.models import ContentFactoryJob, OrganizationContentConfig
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from core.models import User
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from integrations.models import UserIntegration
from integrations.services.article_generation import (
    ArticleGenerationError,
    ContentFactoryBackendUnavailableError,
    GitHubReconnectRequiredError,
    InsufficientRooPointsError,
    _append_refund_instruction,
    check_generation_status,
    confirm_topic,
    get_content_factory_article_cost_points,
    publish_article,
    publish_article_as_pr,
    promote_article_bundle,
    trigger_article_generation,
)
from roo.models import Ledger, PointsAccount
from roo.services import PointsService
from tests.website_fixtures import bind_verified_target_fixture, install_github_provider_fixture


class ArticleGenerationServiceTest(TestCase):
    def setUp(self):
        self.slack_user_id = "U_TEST_123"
        self.repo_name = "test/repo"
        self.user_email = "writer@example.com"
        self.user = User.objects.create_user(
            email=self.user_email,
            password="password",
            slack_id=self.slack_user_id,
            first_name="Test",
            last_name="Writer",
        )
        self.requester_actor_id = f"mlai_user:{self.user.pk}"
        PointsAccount.objects.create(user=self.user, balance=20)

        self.integration = UserIntegration.objects.create(
            slack_user_id=self.slack_user_id,
            github_repo=self.repo_name,
            github_access_token="gh_token_123",
            github_token_expires_at=timezone.now() + timezone.timedelta(days=1),
            project_scanned=True,
        )

        self.org = Organization.objects.create(domain="mlai.au", name="Test Org")
        self.config = OrganizationContentConfig.objects.create(
            organization=self.org,
            github_repo=self.repo_name,
            article_template="## Template Content",
            github_token_encrypted="org-token",
            github_token_expires_at=timezone.now() + timedelta(hours=1),
            scan_summary="scan complete",
            article_system={
                "state": "existing",
                "directory_name": "articles",
                "directory_path": "app/articles/content",
                "confidence": "high",
                "reason": "Detected existing article system",
                "source": "scan",
                "verified_at": "2026-03-08T00:00:00+00:00",
            },
        )
        self.profile = VibeRaisingProfile.objects.create(user=self.user, role='founder')
        self._own_verified_website(self.config)
        install_github_provider_fixture(self, self.config.website_connection)

    def _own_verified_website(self, config):
        company = VibeRaisingCompany.objects.create(profile=self.profile,
            organization=config.organization, name=config.organization.name,
            domain=config.organization.domain)
        if not self.profile.active_company_id:
            self.profile.active_company = company
            self.profile.save(update_fields=['active_company'])
        return bind_verified_target_fixture(config, user=self.user)

    def _source_job(self, **fields):
        metadata = dict(fields.pop('request_meta', {}) or {})
        if not (metadata.get('delivery_mode') == 'content_only' and metadata.get('delivery_mode_confirmed') is True):
            from content_factory.website_connections import contract_for
            metadata = {**contract_for(self.config.website_connection),
                        'expected_source_sha': self.config.website_connection.verified_sha, **metadata}
        return ContentFactoryJob.objects.create(**fields, request_meta=metadata)

    def _article_request(self, **overrides):
        request = {
            "domain": "mlai.au",
            "topic": "AI Agents",
            "target_keyword": "agentic",
            "context": "Context info",
            "request_source": "roo_slackbot",
            "client_request_id": "content-factory-service-request",
            "user_email": self.user_email,
            "user_first_name": "Test",
            "user_last_name": "Writer",
            "user_avatar_url": "https://avatar.test/writer.png",
        }
        request.update(overrides)
        if request.get('topic'):
            request.setdefault('expected_cost_points', 12)
        return request

    def _record_article_payment(self, job, *, amount=12):
        key = f'paid:{job.job_id}'
        ledger, _ = PointsService.spend(user=self.user, delta=amount, source='CONTENT_FACTORY',
            description='Confirmed article fixture', idempotency_key=f'content_factory:charge:{key}',
            reference_type='CONTENT_FACTORY', reference_id=key)
        job.client_request_id = key
        job.billing_ledger = ledger
        job.billing_amount = amount
        job.billing_status = 'charged'
        job.request_meta = {**job.request_meta, 'roo_points_action': 'article_generation'}
        job.save()
        return ledger

    @patch("integrations.services.article_generation.http_requests.post")
    def test_new_article_without_quote_cannot_spend_or_dispatch(self, post):
        with self.assertRaises(ArticleGenerationError) as error:
            trigger_article_generation(self.slack_user_id, self._article_request(expected_cost_points=None))
        self.assertEqual(error.exception.payload['code'], 'roo_points_quote_changed')
        self.assertFalse(Ledger.objects.filter(kind='SPEND').exists())
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 20)
        post.assert_not_called()

    @patch("integrations.services.article_generation.http_requests.post")
    def test_raw_article_debit_needs_same_company_proof_and_cannot_reuse_a_refund(self, post):
        from integrations.services.article_generation import _charge_content_factory_user, _refund_content_factory_request
        from content_factory.dispatch_models import ContentFactoryDispatchOutbox
        for shape in ('unbound', 'other-company', 'refunded', 'valid-six', 'valid-twelve'):
            with self.subTest(shape=shape):
                amount = 12 if shape == 'valid-twelve' else 6
                PointsService.credit_purchased_topup(user=self.user, delta=amount,
                    description='Replay fixture funding', idempotency_key=f'funding-{shape}')
                key = f'raw-{shape}'
                original, _ = PointsService.spend(user=self.user, delta=amount, source='CONTENT_FACTORY',
                    description='Original article fixture', idempotency_key=f'content_factory:charge:{key}',
                    reference_type='CONTENT_FACTORY', reference_id=key)
                if shape != 'unbound':
                    if shape == 'other-company':
                        other_org = Organization.objects.create(domain='other.test', name='Other startup')
                        VibeRaisingCompany.objects.create(profile=self.profile, organization=other_org,
                            name='Other startup', domain=other_org.domain)
                    ContentFactoryJob.objects.create(job_id=f'proof-{shape}', client_request_id=key,
                        domain='other.test' if shape == 'other-company' else 'mlai.au',
                        billing_ledger=original, billing_amount=amount)
                payload = {'client_request_id': key, 'topic': 'AI Agents'}
                if shape == 'refunded':
                    refund = _refund_content_factory_request(user=self.user, slack_user_id=self.slack_user_id,
                        article_request=payload, resolved_domain='mlai.au', reason='Original failed request')
                    # No refunded job may be required to detect the ledger marker.
                    ContentFactoryJob.objects.filter(client_request_id=key).delete()
                    ContentFactoryDispatchOutbox.objects.create(client_request_id=key, organization=self.org,
                        payload={'billing_user_id': str(self.user.pk), 'request': {
                            'domain': 'mlai.au', 'roo_points_ledger_id': str(original.pk)}})
                    repeated = _refund_content_factory_request(user=self.user, slack_user_id=self.slack_user_id,
                        article_request=payload, resolved_domain='mlai.au', reason='Repeated failed request')
                    self.assertEqual(refund.pk, repeated.pk)
                before = PointsService.get_balance(self.user)['balance']
                spends = Ledger.objects.filter(kind='SPEND').count()
                if shape.startswith('valid-'):
                    _, replay, points = _charge_content_factory_user(user=self.user, created_by_slack_id=self.slack_user_id,
                        article_request=payload, resolved_domain='mlai.au', prevalidated_admission=True)
                    self.assertEqual((replay.pk, points), (original.pk, amount))
                else:
                    with self.assertRaises(ArticleGenerationError) as error:
                        _charge_content_factory_user(user=self.user, created_by_slack_id=self.slack_user_id,
                            article_request=payload, resolved_domain='mlai.au', prevalidated_admission=True)
                    self.assertEqual(error.exception.code, 'content_factory_billing_key_conflict')
                self.assertEqual(PointsService.get_balance(self.user)['balance'], before)
                self.assertEqual(Ledger.objects.filter(kind='SPEND').count(), spends)
        post.assert_not_called()

    def test_quote_and_paid_source_reject_unavailable_configured_payer_with_typed_error(self):
        from types import SimpleNamespace
        from content_factory.content_views import ContentQuoteView
        from integrations.services.article_generation import _require_article_job_payment
        source = self._source_job(job_id='original-payer-source', domain='mlai.au', slack_user_id=self.slack_user_id)
        self._record_article_payment(source, amount=6)
        for active in (False, True):
            with self.subTest(active=active):
                payer = User.objects.create_user(email=f'unavailable-{active}@example.test', is_active=active)
                self.org.billing_user = payer
                self.org.save(update_fields=['billing_user'])
                response = ContentQuoteView().post(SimpleNamespace(data={
                    'domain': 'mlai.au', 'requested_by_slack_user_id': self.requester_actor_id}))
                self.assertEqual((response.status_code, response.data['code']), (409, 'company_billing_founder_unavailable'))
                with self.assertRaises(ArticleGenerationError) as error:
                    _require_article_job_payment(source, user=self.user, domain='mlai.au')
                self.assertEqual(error.exception.payload['code'], 'company_billing_founder_unavailable')
        self.assertEqual(Ledger.objects.filter(kind='SPEND').count(), 1)
        self.assertEqual(PointsService.get_balance(self.user)['balance'], 14)

    def test_shared_charge_revalidates_a_concurrently_inserted_other_company_debit(self):
        from integrations.services.article_generation import _charge_content_factory_user
        spend = PointsService.spend
        key = 'concurrent-other-company'
        other_org = Organization.objects.create(domain='other.test', name='Other startup')
        VibeRaisingCompany.objects.create(profile=self.profile, organization=other_org, name='Other startup', domain=other_org.domain)
        originals = []
        def competing_spend(**kwargs):
            original, _ = spend(**kwargs)
            originals.append(original)
            ContentFactoryJob.objects.create(job_id='other-company-concurrent-proof', domain='other.test',
                client_request_id=key, billing_ledger=original, billing_amount=12)
            return spend(**kwargs)
        with patch.object(PointsService, 'spend', side_effect=competing_spend):
            with self.assertRaises(ArticleGenerationError) as error:
                _charge_content_factory_user(user=self.user, created_by_slack_id=self.slack_user_id,
                    article_request={'client_request_id': key, 'topic': 'AI Agents', 'expected_cost_points': 12},
                    resolved_domain='mlai.au', prevalidated_admission=True)
        self.assertEqual(error.exception.code, 'content_factory_billing_key_conflict')
        self.assertEqual(Ledger.objects.filter(kind='SPEND').count(), 1)
        self.assertEqual(Ledger.objects.get(kind='SPEND').pk, originals[0].pk)
        self.assertEqual(PointsService.get_balance(self.user)['balance'], 8)
        self.assertFalse(Ledger.objects.filter(kind='REFUND').exists())
        self.assertEqual(ContentFactoryJob.objects.get(client_request_id=key).domain, 'other.test')

    @patch("integrations.services.article_generation.http_requests.post")
    def test_source_only_six_point_replay_preserves_key_and_rejects_changed_scope_payer_or_refund(self, post):
        from integrations.services.article_generation import quote_content_factory_article
        source = self._source_job(job_id='source-only-six', domain='mlai.au', slack_user_id=self.slack_user_id)
        original = self._record_article_payment(source, amount=6)
        self.assertEqual(quote_content_factory_article(actor_id=self.requester_actor_id, source_run_id=source.job_id), 6)
        post.return_value.status_code = 202
        post.return_value.json.return_value = {'job_id': 'source-only-child', 'status': 'queued'}
        kwargs = dict(domain='mlai.au', confirmed_keyword='AI Agents', slack_user_id=self.slack_user_id, source_run_id=source.job_id)
        first = confirm_topic(**kwargs)
        self.assertEqual(confirm_topic(**kwargs)['job_id'], first['job_id'])
        self.assertEqual(ContentFactoryJob.objects.get(job_id=first['job_id']).client_request_id, source.client_request_id)
        post.assert_called_once()
        with self.assertRaises(PermissionError):
            quote_content_factory_article(actor_id=self.requester_actor_id, source_run_id=source.job_id, client_request_id='different-key')
        with self.assertRaises(ArticleGenerationError):
            confirm_topic(**kwargs, client_request_id='different-key')
        with self.assertRaises(PermissionError):
            quote_content_factory_article(actor_id=self.requester_actor_id, source_run_id=source.job_id, domain='other.test')
        with self.assertRaises(ArticleGenerationError):
            confirm_topic(**{**kwargs, 'domain': 'other.test'})
        payer = User.objects.create_user(email='changed-payer@example.test')
        profile = VibeRaisingProfile.objects.create(user=payer, role='founder')
        VibeRaisingCompany.objects.create(profile=profile, organization=self.org, name='Test Org', domain='mlai.au')
        self.org.billing_user = payer
        self.org.save(update_fields=['billing_user'])
        with self.assertRaises(ArticleGenerationError):
            quote_content_factory_article(actor_id=self.requester_actor_id, source_run_id=source.job_id)
        with self.assertRaises(ArticleGenerationError):
            confirm_topic(**kwargs)
        self.assertEqual(Ledger.objects.filter(kind='SPEND').count(), 1)
        self.assertEqual(PointsService.get_balance(self.user)['balance'], 14)
        self.org.billing_user = None
        self.org.save(update_fields=['billing_user'])
        PointsService.refund(user=self.user, delta=6, source='CONTENT_FACTORY', description='Original refund',
            idempotency_key=f'content_factory:refund:{source.client_request_id}', original_spend_key=original.idempotency_key,
            reference_type='CONTENT_FACTORY', reference_id=source.client_request_id)
        with self.assertRaises(ArticleGenerationError):
            quote_content_factory_article(actor_id=self.requester_actor_id, source_run_id=source.job_id)
        with self.assertRaises(ArticleGenerationError):
            confirm_topic(**kwargs)
        self.assertEqual(Ledger.objects.filter(kind='SPEND').count(), 1)
        self.assertEqual(Ledger.objects.filter(kind='REFUND').count(), 1)
        self.assertEqual(PointsService.get_balance(self.user)['balance'], 20)
        post.assert_called_once()

    @patch("integrations.services.article_generation.http_requests.post")
    def test_uncharged_job_status_cannot_bypass_missing_article_quote(self, post):
        self._source_job(job_id='fabricated-paid-job', domain='mlai.au', slack_user_id=self.slack_user_id,
            client_request_id='content-factory-service-request', billing_status='charged')
        with self.assertRaises(ArticleGenerationError) as error:
            trigger_article_generation(self.slack_user_id, self._article_request(expected_cost_points=None))
        self.assertEqual(error.exception.payload['code'], 'roo_points_quote_changed')
        self.assertFalse(Ledger.objects.filter(kind='SPEND').exists())
        post.assert_not_called()

    @patch("integrations.services.article_generation.http_requests.post")
    def test_verified_original_six_point_job_replays_without_quote_or_second_debit(self, post):
        job = self._source_job(job_id='original-paid-job', domain='mlai.au', slack_user_id=self.slack_user_id,
            status='queued', request_meta={'topic': 'AI Agents'})
        ledger = self._record_article_payment(job, amount=6)
        for _ in range(2):
            result = trigger_article_generation(self.slack_user_id,
                self._article_request(client_request_id=job.client_request_id, expected_cost_points=None))
            self.assertEqual(result['job_id'], job.job_id)
        self.assertEqual(Ledger.objects.filter(kind='SPEND').count(), 1)
        self.assertEqual(ContentFactoryJob.objects.get(job_id=job.job_id).billing_ledger_id, ledger.pk)
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 14)
        post.assert_not_called()

    @patch("integrations.services.article_generation.http_requests.post")
    def test_paid_research_confirmation_without_article_quote_cannot_spend_or_dispatch(self, post):
        ledger, _ = PointsService.spend(user=self.user, delta=3, source='CONTENT_FACTORY',
            description='Research payment', idempotency_key='content_factory:topic_generation:charge:research-key',
            reference_type='CONTENT_FACTORY', reference_id='research-key')
        job = self._source_job(job_id='paid-research', domain='mlai.au', slack_user_id=self.slack_user_id,
            client_request_id='research-key', billing_status='charged', billing_ledger=ledger, billing_amount=3,
            request_meta={'roo_points_action': 'content_island_topic_generation', 'expectedCostPoints': 3})
        with self.assertRaises(ArticleGenerationError) as error:
            confirm_topic(domain='mlai.au', confirmed_keyword='agentic', slack_user_id=self.slack_user_id,
                source_run_id=job.job_id, client_request_id='content-factory-confirm-paid-research')
        self.assertEqual(error.exception.payload['code'], 'roo_points_quote_changed')
        self.assertEqual(Ledger.objects.filter(kind='SPEND').count(), 1)
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 17)
        post.assert_not_called()

    @patch("integrations.services.article_generation.http_requests.post")
    def test_confirmed_research_retains_new_article_key_and_replays_without_quote(self, post):
        ledger, _ = PointsService.spend(user=self.user, delta=3, source='CONTENT_FACTORY',
            description='Research payment', idempotency_key='content_factory:topic_generation:charge:research-key',
            reference_type='CONTENT_FACTORY', reference_id='research-key')
        job = self._source_job(job_id='paid-research', domain='mlai.au', slack_user_id=self.slack_user_id,
            client_request_id='research-key', billing_status='charged', billing_ledger=ledger, billing_amount=3,
            request_meta={'roo_points_action': 'content_island_topic_generation', 'expectedCostPoints': 3})
        post.return_value.status_code = 202
        post.return_value.json.return_value = {'job_id': 'confirmed-research-child', 'status': 'queued'}
        kwargs = dict(domain='mlai.au', confirmed_keyword='agentic', slack_user_id=self.slack_user_id,
            source_run_id=job.job_id, client_request_id='content-factory-confirm-paid-research')
        first = confirm_topic(**kwargs, expected_cost_points=12)
        repeated = confirm_topic(**kwargs)
        self.assertEqual(first['job_id'], repeated['job_id'])
        post.assert_called_once()
        job.refresh_from_db()
        child = ContentFactoryJob.objects.get(job_id=first['job_id'])
        self.assertEqual(job.client_request_id, kwargs['client_request_id'])
        self.assertEqual(child.client_request_id, job.client_request_id)
        self.assertEqual(child.billing_ledger_id, job.billing_ledger_id)
        self.assertNotEqual(child.billing_ledger_id, ledger.pk)
        self.assertEqual(Ledger.objects.filter(kind='SPEND').count(), 2)
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 5)

    @patch("integrations.services.article_generation.http_requests.post")
    def test_research_cannot_adopt_or_refund_a_delivered_articles_key(self, post):
        article = self._source_job(job_id='delivered-article', domain='mlai.au', slack_user_id=self.slack_user_id, status='completed')
        self._record_article_payment(article, amount=6)
        source = self._source_job(job_id='new-research', domain='mlai.au', slack_user_id=self.slack_user_id,
            client_request_id='new-research-key', billing_status='deferred', request_meta={})
        with self.assertRaises(ArticleGenerationError) as error:
            confirm_topic(domain='mlai.au', confirmed_keyword='another article', slack_user_id=self.slack_user_id,
                source_run_id=source.job_id, client_request_id=article.client_request_id)
        self.assertEqual(error.exception.code, 'content_factory_billing_key_conflict')
        self.assertEqual(Ledger.objects.filter(kind='SPEND').count(), 1)
        self.assertFalse(Ledger.objects.filter(kind='REFUND').exists())
        source.refresh_from_db()
        self.assertEqual(source.billing_status, 'deferred')
        self.assertIsNone(source.billing_ledger_id)
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 14)
        post.assert_not_called()

    @patch("integrations.services.article_generation.http_requests.post")
    def test_existing_unpaid_or_conflicting_child_cannot_leave_rejected_confirmation_charged(self, post):
        for research in (False, True):
            for conflicting_child in (False, True):
                with self.subTest(research=research, conflicting_child=conflicting_child):
                    suffix = f'{research}-{conflicting_child}'
                    key = f'research-{suffix}'
                    research_ledger = None
                    if research:
                        research_ledger, _ = PointsService.spend(user=self.user, delta=3, source='CONTENT_FACTORY',
                            description='Research fixture', idempotency_key=f'content_factory:topic_generation:charge:{key}',
                            reference_type='CONTENT_FACTORY', reference_id=key)
                    source = self._source_job(job_id=key, domain='mlai.au', slack_user_id=self.slack_user_id,
                        client_request_id=key, billing_status='charged' if research else 'deferred',
                        billing_ledger=research_ledger, billing_amount=3 if research else 0,
                        request_meta={'roo_points_action': 'content_island_topic_generation'} if research else {})
                    child = self._source_job(job_id=f'child-{suffix}', domain='mlai.au', slack_user_id=self.slack_user_id,
                        status='queued', request_meta={'source_run_id': source.job_id, 'topic': 'Agentic AI'})
                    if conflicting_child:
                        self._record_article_payment(child, amount=6)
                    original = (source.client_request_id, source.billing_ledger_id, source.billing_amount,
                        source.billing_status, dict(source.request_meta))
                    before_balance = PointsService.get_balance(self.user)['balance']
                    before_spends = Ledger.objects.filter(kind='SPEND').count()
                    with self.assertRaises(ArticleGenerationError) as error:
                        confirm_topic(domain='mlai.au', confirmed_keyword='Agentic AI', slack_user_id=self.slack_user_id,
                            source_run_id=source.job_id, client_request_id=f'article-{suffix}', expected_cost_points=12)
                    self.assertEqual(error.exception.code, 'content_factory_billing_key_conflict')
                    source.refresh_from_db()
                    self.assertEqual((source.client_request_id, source.billing_ledger_id, source.billing_amount,
                        source.billing_status, source.request_meta), original)
                    self.assertEqual(PointsService.get_balance(self.user)['balance'], before_balance)
                    self.assertEqual(Ledger.objects.filter(kind='SPEND').count(), before_spends)
                    self.assertFalse(Ledger.objects.filter(kind='REFUND').exists())
        post.assert_not_called()

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_payload(self, mock_post):
        self.config.article_delivery_mode = "publish_code"
        self.config.save(update_fields=["article_delivery_mode"])
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"job_id": "job_123", "status": "queued"}
        mock_post.return_value = mock_response

        article_request = self._article_request()

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            result = trigger_article_generation(self.slack_user_id, article_request)

        self.assertEqual(result["job_id"], "job_123")

        args, kwargs = mock_post.call_args
        self.assertIn("/api/runs/article", args[0])
        payload = kwargs["json"]

        self.assertEqual(payload["domain"], "mlai.au")
        self.assertEqual(payload["topic"], "AI Agents")
        self.assertEqual(payload["target_keyword"], "agentic")
        self.assertEqual(payload["context"], "Context info")
        self.assertEqual(payload["github_repo"], self.repo_name)
        self.assertEqual(payload["slack_user_id"], self.slack_user_id)
        self.assertEqual(payload["delivery_mode"], "publish_code")
        self.assertFalse(payload["delivery_mode_confirmed"])
        self.assertEqual(payload["request_source"], "roo_slackbot")
        self.assertNotIn("github_token", payload)

        job = ContentFactoryJob.objects.get(job_id="job_123")
        self.assertEqual(job.client_request_id, "content-factory-service-request")
        self.assertEqual(job.billing_status, "charged")
        self.assertEqual(job.billing_amount, 12)
        self.assertEqual(job.billing_source_job_id, "job_123")
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 8)
        self.assertEqual(mock_post.call_args.kwargs["timeout"], (3, 8))

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_payload_includes_requested_by_slack_user_id(self, mock_post):
        self.config.article_delivery_mode = "publish_code"
        self.config.save(update_fields=["article_delivery_mode"])
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"job_id": "job_delegated_123", "status": "queued"}
        mock_post.return_value = mock_response

        article_request = self._article_request(requested_by_slack_user_id=self.requester_actor_id)

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            result = trigger_article_generation(self.slack_user_id, article_request)

        self.assertEqual(result["job_id"], "job_delegated_123")
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["slack_user_id"], self.slack_user_id)
        self.assertEqual(payload["requested_by_slack_user_id"], self.requester_actor_id)

        job = ContentFactoryJob.objects.get(job_id="job_delegated_123")
        self.assertEqual(job.request_meta["requested_by_slack_user_id"], self.requester_actor_id)

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_payload_includes_resolved_author(self, mock_post):
        self.config.authors = [
            {"id": "priya-nair", "name": "Priya Nair", "role": "Head of Content"},
            {"id": "sam-donegan", "name": "Sam Donegan", "role": "Founder"},
        ]
        self.config.default_author_id = "sam-donegan"
        self.config.save(update_fields=["authors", "default_author_id"])
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"job_id": "job_author_123", "status": "queued"}
        mock_post.return_value = mock_response

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            trigger_article_generation(self.slack_user_id, self._article_request())

        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual([a["id"] for a in payload["authors"]], ["priya-nair", "sam-donegan"])
        # No per-article override -> the org default byline is used.
        self.assertEqual(payload["author_id"], "sam-donegan")
        self.assertEqual(payload["default_author_name"], "Sam Donegan")
        self.assertEqual(payload["default_author_profile"]["name"], "Sam Donegan")

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_payload_honors_per_article_author_override(self, mock_post):
        self.config.authors = [
            {"id": "priya-nair", "name": "Priya Nair", "role": "Head of Content"},
            {"id": "sam-donegan", "name": "Sam Donegan", "role": "Founder"},
        ]
        self.config.default_author_id = "sam-donegan"
        self.config.save(update_fields=["authors", "default_author_id"])
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"job_id": "job_author_override", "status": "queued"}
        mock_post.return_value = mock_response

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            trigger_article_generation(
                self.slack_user_id,
                self._article_request(author_id="priya-nair"),
            )

        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["author_id"], "priya-nair")
        self.assertEqual(payload["default_author_name"], "Priya Nair")

    def test_article_cost_is_twelve_points_for_every_domain(self):
        self.assertEqual(get_content_factory_article_cost_points("example.com"), 12)
        self.assertEqual(get_content_factory_article_cost_points("mlai.au"), 12)

    def test_paid_article_refund_instruction_uses_twelve_points(self):
        message = _append_refund_instruction("The article run failed.", "example.com")

        self.assertIn("12 Roo points", message)

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_paid_domain_charges_twelve_points(self, mock_post):
        paid_org = Organization.objects.create(domain="example.com", name="Paid Org")
        paid_config = OrganizationContentConfig.objects.create(
            organization=paid_org,
            github_repo=self.repo_name,
            article_template="## Template Content",
            github_token_encrypted="org-token",
            github_token_expires_at=timezone.now() + timedelta(hours=1),
            scan_summary="scan complete",
            article_system={
                "state": "existing",
                "directory_name": "articles",
                "directory_path": "app/articles/content",
                "confidence": "high",
                "source": "test",
            },
        )
        self._own_verified_website(paid_config)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"job_id": "job_paid_123", "status": "queued"}
        mock_post.return_value = mock_response

        article_request = self._article_request(
            domain="example.com",
            client_request_id="content-factory-paid-request",
        )

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            result = trigger_article_generation(self.slack_user_id, article_request)

        self.assertEqual(result["job_id"], "job_paid_123")
        job = ContentFactoryJob.objects.get(job_id="job_paid_123")
        self.assertEqual(job.billing_status, "charged")
        self.assertEqual(job.billing_amount, 12)
        self.assertEqual(job.billing_ledger.delta, -12)
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 8)

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_discovery_charges_for_four_requested_topics(self, mock_post):
        paid_org = Organization.objects.create(domain="example.com", name="Paid Org")
        OrganizationContentConfig.objects.create(
            organization=paid_org,
            github_repo=self.repo_name,
            scan_summary="scan complete",
        )
        self.user.points_account.balance = 6
        self.user.points_account.save(update_fields=["balance"])
        mock_response = MagicMock()
        mock_response.status_code = 202
        mock_response.json.return_value = {"job_id": "job_discovery_paid_123", "status": "queued"}
        mock_post.return_value = mock_response

        article_request = self._article_request(
            domain="example.com",
            topic=None,
            target_keyword="",
            client_request_id="content-factory-paid-discovery",
        )

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            result = trigger_article_generation(self.slack_user_id, article_request)

        self.assertEqual(result["job_id"], "job_discovery_paid_123")
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["roo_points_cost"], 4)
        self.assertEqual(payload["roo_points_required"], 4)
        self.assertEqual(payload["roo_points_balance"], 2)
        job = ContentFactoryJob.objects.get(job_id="job_discovery_paid_123")
        self.assertEqual(job.billing_status, "charged")
        self.assertEqual(job.billing_amount, 4)
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 2)

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_discovery_rejects_balance_below_four_topic_quote(self, mock_post):
        paid_org = Organization.objects.create(domain="example.com", name="Paid Org")
        OrganizationContentConfig.objects.create(organization=paid_org, github_repo=self.repo_name)
        self.user.points_account.balance = 3
        self.user.points_account.save(update_fields=["balance"])

        article_request = self._article_request(
            domain="example.com",
            topic=None,
            target_keyword="",
            client_request_id="content-factory-paid-discovery-insufficient",
        )

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            with self.assertRaises(InsufficientRooPointsError) as exc:
                trigger_article_generation(self.slack_user_id, article_request)

        self.assertEqual(exc.exception.payload["error_code"], "INSUFFICIENT_ROO_POINTS")
        self.assertEqual(exc.exception.payload["required_points"], 4)
        self.assertEqual(exc.exception.payload["current_balance"], 3)
        mock_post.assert_not_called()

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_stores_thread_context_without_forwarding_it(self, mock_post):
        self.config.article_delivery_mode = "publish_code"
        self.config.save(update_fields=["article_delivery_mode"])
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"job_id": "job_thread_123", "status": "queued"}
        mock_post.return_value = mock_response

        article_request = self._article_request(
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            slack_root_message_ts="123.456",
        )

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            result = trigger_article_generation(self.slack_user_id, article_request)

        self.assertEqual(result["job_id"], "job_thread_123")

        payload = mock_post.call_args.kwargs["json"]
        self.assertNotIn("slack_channel_id", payload)
        self.assertNotIn("slack_thread_ts", payload)
        self.assertNotIn("slack_root_message_ts", payload)

        job = ContentFactoryJob.objects.get(job_id="job_thread_123")
        self.assertEqual(job.slack_channel_id, "C123")
        self.assertEqual(job.slack_thread_ts, "123.456")
        self.assertEqual(job.slack_root_message_ts, "123.456")
        self.assertEqual(job.request_meta["slack_thread_ts"], "123.456")

    @patch("integrations.services.article_generation.http_requests.post")
    def test_confirm_topic_payload_includes_delivery_mode(self, mock_post):
        self.config.article_delivery_mode = "publish_code"
        self.config.save(update_fields=["article_delivery_mode"])
        mock_response = MagicMock()
        mock_response.status_code = 202
        mock_response.json.return_value = {"job_id": "job_confirm_123", "status": "queued"}
        mock_post.return_value = mock_response

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            result = confirm_topic(
                domain="mlai.au",
                confirmed_keyword="agentic ai",
                slack_user_id=self.slack_user_id,
                custom_title="Agentic AI",
                request_source="roo_slackbot",
                expected_cost_points=12,
            )

        self.assertEqual(result["job_id"], "job_confirm_123")

        args, kwargs = mock_post.call_args
        self.assertIn("/api/runs/article", args[0])
        payload = kwargs["json"]
        self.assertEqual(payload["delivery_mode"], "publish_code")
        self.assertFalse(payload["delivery_mode_confirmed"])
        self.assertEqual(payload["request_source"], "roo_slackbot")
        self.assertEqual(kwargs["timeout"], (3, 8))
        self.assertTrue(payload["client_request_id"])
        self.assertEqual(payload["roo_points_cost"], 12)
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 8)

    @patch("integrations.services.article_generation.http_requests.post")
    def test_confirm_topic_reuses_a_supplied_request_id_without_another_charge(self, mock_post):
        mock_post.return_value.status_code = 202
        mock_post.return_value.json.return_value = {"job_id": "confirmed-paid-article", "status": "queued"}
        kwargs = dict(domain="mlai.au", confirmed_keyword="agentic ai", slack_user_id=self.slack_user_id,
            request_source="roo_slackbot", client_request_id="confirmed-paid-request")
        first = confirm_topic(**kwargs, expected_cost_points=12)
        repeated = confirm_topic(**kwargs)
        self.assertEqual(first["job_id"], repeated["job_id"])
        mock_post.assert_called_once()
        job = ContentFactoryJob.objects.get(job_id=first["job_id"])
        self.assertEqual(job.client_request_id, "confirmed-paid-request")
        self.assertEqual(job.billing_amount, 12)
        self.assertIsNotNone(job.billing_ledger_id)
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 8)

    @patch("integrations.services.article_generation.http_requests.post")
    def test_confirm_topic_payload_includes_requested_by_slack_user_id(self, mock_post):
        self.config.article_delivery_mode = "publish_code"
        self.config.save(update_fields=["article_delivery_mode"])
        mock_response = MagicMock()
        mock_response.status_code = 202
        mock_response.json.return_value = {"job_id": "job_confirm_delegated_123", "status": "queued"}
        mock_post.return_value = mock_response

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            result = confirm_topic(
                domain="mlai.au",
                confirmed_keyword="agentic ai",
                slack_user_id=self.slack_user_id,
                requested_by_slack_user_id=self.requester_actor_id,
                custom_title="Agentic AI",
                request_source="roo_slackbot",
                expected_cost_points=12,
            )

        self.assertEqual(result["job_id"], "job_confirm_delegated_123")
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["slack_user_id"], self.slack_user_id)
        self.assertEqual(payload["requested_by_slack_user_id"], self.requester_actor_id)

    @patch("integrations.services.article_generation.http_requests.post")
    def test_confirm_topic_reuses_existing_non_terminal_child_without_post(self, mock_post):
        source_job = self._source_job(
            job_id="job-source-active-123",
            domain="mlai.au",
            slack_user_id=self.slack_user_id,
            status="awaiting_confirmation",
        )
        self._record_article_payment(source_job, amount=6)
        child_job = self._source_job(
            job_id="job-child-active-123",
            domain="mlai.au",
            slack_user_id=self.slack_user_id,
            status="queued",
            request_meta={
                "source_run_id": source_job.job_id,
                "topic": "Agentic AI",
                "target_keyword": "agentic ai",
            },
        )

        result = confirm_topic(
            domain="mlai.au",
            confirmed_keyword="agentic ai",
            slack_user_id=self.slack_user_id,
            custom_title="Agentic AI",
            source_run_id=source_job.job_id,
            request_source="roo_slackbot",
        )

        self.assertEqual(result["job_id"], child_job.job_id)
        self.assertEqual(result["status"], "queued")
        self.assertIsNone(child_job.client_request_id)
        self.assertIsNone(child_job.billing_ledger_id)
        self.assertEqual(PointsService.get_balance(self.user)["balance"], 14)
        self.assertEqual(Ledger.objects.filter(kind="SPEND", source="CONTENT_FACTORY").count(), 1)
        mock_post.assert_not_called()

    @patch("integrations.services.article_generation.http_requests.post")
    def test_confirm_topic_raises_structured_backend_unavailable_on_queue_timeout(self, mock_post):
        mock_post.side_effect = requests.exceptions.ReadTimeout("timed out")

        with self.assertRaises(ContentFactoryBackendUnavailableError) as exc:
            confirm_topic(
                domain="mlai.au",
                confirmed_keyword="agentic ai",
                slack_user_id=self.slack_user_id,
                request_source="roo_slackbot",
                expected_cost_points=12,
            )

        self.assertEqual(exc.exception.payload["error_code"], "CONTENT_FACTORY_UNAVAILABLE")
        self.assertEqual(exc.exception.payload["operation"], "confirm_topic")
        self.assertEqual(mock_post.call_count, 2)
        request_ids = [call.kwargs["json"]["client_request_id"] for call in mock_post.call_args_list]
        self.assertTrue(request_ids[0])
        self.assertEqual(len(set(request_ids)), 1)

    @patch("integrations.services.article_generation.http_requests.post")
    def test_publish_article(self, mock_post):
        self._source_job(job_id="job_123", domain="mlai.au", slack_user_id=self.slack_user_id,
                         status="awaiting_approval")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "queued",
            "approval_state": "approved",
            "job_id": "job_123",
        }
        mock_post.return_value = mock_response

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            result = publish_article("job_123", self.slack_user_id, domain="mlai.au")

        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["approval_state"], "approved")

        args, _ = mock_post.call_args
        self.assertIn("/api/runs/job_123/approve", args[0])

    @patch("integrations.services.article_generation.http_requests.post")
    def test_promote_article_bundle_copies_thread_context_to_child_job(self, mock_post):
        self._source_job(
            job_id="job_content_ready",
            domain="mlai.au",
            slack_user_id=self.slack_user_id,
            status="completed",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            slack_root_message_ts="123.456",
            request_meta={"requested_by_slack_user_id": self.requester_actor_id},
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "queued",
            "job_id": "job_publish_child",
            "source_run_id": "job_content_ready",
        }
        mock_post.return_value = mock_response

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            result = promote_article_bundle("job_content_ready", slack_user_id=self.slack_user_id, domain="mlai.au")

        self.assertEqual(result["job_id"], "job_publish_child")
        args, _ = mock_post.call_args
        self.assertIn("/api/runs/job_content_ready/promote-bundle", args[0])

        child_job = ContentFactoryJob.objects.get(job_id="job_publish_child")
        self.assertEqual(child_job.slack_channel_id, "C123")
        self.assertEqual(child_job.slack_thread_ts, "123.456")
        self.assertEqual(child_job.slack_root_message_ts, "123.456")
        self.assertEqual(child_job.request_meta["source_run_id"], "job_content_ready")
        self.assertEqual(child_job.request_meta["promotion_source"], "promote_bundle")
        self.assertEqual(child_job.request_meta["requested_by_slack_user_id"], self.requester_actor_id)

    @patch("integrations.services.article_generation.promote_article_bundle")
    def test_publish_article_as_pr_delegates_to_promote_bundle(self, mock_promote_article_bundle):
        mock_promote_article_bundle.return_value = {
            "status": "queued",
            "job_id": "job_publish_child",
        }

        result = publish_article_as_pr(
            "job_content_ready",
            slack_user_id=self.slack_user_id,
            requested_by_slack_user_id=self.requester_actor_id,
            domain="mlai.au",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            slack_root_message_ts="123.456",
        )
        self.assertEqual(result["job_id"], "job_publish_child")
        mock_promote_article_bundle.assert_called_once_with(
            "job_content_ready",
            slack_user_id=self.slack_user_id,
            requested_by_slack_user_id=self.requester_actor_id,
            domain="mlai.au",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            slack_root_message_ts="123.456",
        )

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_raises_structured_auth_required_on_content_factory_412(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 412
        mock_response.json.return_value = {
            "status": "precondition_failed",
            "error_code": "AUTH_REQUIRED",
            "missing_step": "github_auth",
            "next_action": "reconnect_github",
            "requires_user_action": True,
            "resume_hint": "reconnect_github_then_retry",
            "domain": "mlai.au",
            "github_repo": self.repo_name,
            "reason_code": "missing_or_expired_credentials",
            "message": "Reconnect GitHub before publishing.",
        }
        mock_post.return_value = mock_response

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            with self.assertRaises(GitHubReconnectRequiredError) as exc_info:
                trigger_article_generation(self.slack_user_id, self._article_request())

        payload = exc_info.exception.payload
        self.assertEqual(payload["error_code"], "AUTH_REQUIRED")
        self.assertEqual(payload["missing_step"], "github_auth")
        self.assertEqual(payload["next_action"], "reconnect_github")
        self.assertEqual(payload["domain"], "mlai.au")
        self.assertEqual(payload["github_repo"], self.repo_name)
        self.assertTrue(payload["auth_url"])

    @patch("integrations.services.article_generation.http_requests.post")
    def test_promote_article_bundle_raises_structured_auth_required_on_content_factory_412(self, mock_post):
        self._source_job(
            job_id="job_content_ready_auth",
            domain="mlai.au",
            slack_user_id=self.slack_user_id,
            status="completed",
        )

        mock_response = MagicMock()
        mock_response.status_code = 412
        mock_response.json.return_value = {
            "status": "precondition_failed",
            "error_code": "AUTH_REQUIRED",
            "missing_step": "github_auth",
            "next_action": "reconnect_github",
            "requires_user_action": True,
            "resume_hint": "reconnect_github_then_retry",
            "domain": "mlai.au",
            "github_repo": self.repo_name,
            "reason_code": "missing_or_expired_credentials",
            "message": "Reconnect GitHub before promoting this bundle.",
        }
        mock_post.return_value = mock_response

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            with self.assertRaises(GitHubReconnectRequiredError) as exc_info:
                promote_article_bundle("job_content_ready_auth", slack_user_id=self.slack_user_id, domain="mlai.au")

        payload = exc_info.exception.payload
        self.assertEqual(payload["error_code"], "AUTH_REQUIRED")
        self.assertEqual(payload["domain"], "mlai.au")
        self.assertEqual(payload["github_repo"], self.repo_name)
        self.assertTrue(payload["auth_url"])

    @patch("content_factory.content_views.trigger_article_generation")
    def test_content_generate_view_returns_structured_auth_required(self, mock_trigger):
        mock_trigger.side_effect = GitHubReconnectRequiredError(
            {
                "status": "precondition_failed",
                "error_code": "AUTH_REQUIRED",
                "missing_step": "github_auth",
                "next_action": "reconnect_github",
                "requires_user_action": True,
                "resume_hint": "reconnect_github_then_retry",
                "domain": "mlai.au",
                "github_repo": self.repo_name,
                "reason_code": "missing_or_expired_credentials",
                "message": "Reconnect GitHub before continuing.",
                "auth_url": "https://github.example/reconnect",
            }
        )

        with self.settings(ROO_API_KEY="roo-test-key"):
            response = self.client.post(
                "/api/v1/content/generate",
                data={
                    "slack_user_id": self.slack_user_id,
                    "domain": "mlai.au",
                    "topic": "AI Agents",
                    "target_keyword": "agentic ai",
                    "request_source": "roo_slackbot",
                    "client_request_id": "content-generate-auth-required",
                },
                HTTP_X_API_KEY="roo-test-key",
            )

        self.assertEqual(response.status_code, 412)
        payload = response.json()
        self.assertEqual(payload["error_code"], "AUTH_REQUIRED")
        self.assertEqual(payload["domain"], "mlai.au")
        self.assertEqual(payload["github_repo"], self.repo_name)
        self.assertEqual(payload["auth_url"], "https://github.example/reconnect")
        self.assertTrue(payload["pending_intent_stored"])

    @patch("content_factory.content_views.trigger_article_generation")
    def test_content_generate_view_delegated_auth_required_does_not_store_pending_intent(self, mock_trigger):
        mock_trigger.side_effect = GitHubReconnectRequiredError(
            {
                "status": "precondition_failed",
                "error_code": "AUTH_REQUIRED",
                "missing_step": "github_auth",
                "next_action": "reconnect_github",
                "requires_user_action": True,
                "resume_hint": "reconnect_github_then_retry",
                "domain": "mlai.au",
                "github_repo": self.repo_name,
                "reason_code": "missing_or_expired_credentials",
                "message": "Reconnect GitHub before continuing.",
                "auth_url": "https://github.example/reconnect",
            }
        )

        with self.settings(ROO_API_KEY="roo-test-key"):
            response = self.client.post(
                "/api/v1/content/generate",
                data={
                    "slack_user_id": self.slack_user_id,
                    "requested_by_slack_user_id": self.requester_actor_id,
                    "domain": "mlai.au",
                    "topic": "AI Agents",
                    "target_keyword": "agentic ai",
                    "request_source": "roo_slackbot",
                    "client_request_id": "content-generate-auth-required-delegated",
                },
                HTTP_X_API_KEY="roo-test-key",
            )

        self.assertEqual(response.status_code, 412)
        payload = response.json()
        self.assertEqual(payload["requested_by_slack_user_id"], self.requester_actor_id)
        self.assertFalse(payload["pending_intent_stored"])

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_omits_delivery_mode_when_no_preference_exists(self, mock_post):
        self.config.article_system = {}
        self.config.article_delivery_mode = None
        self.config.save(update_fields=["article_system", "article_delivery_mode"])

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"job_id": "job_content_only_123", "status": "queued"}
        mock_post.return_value = mock_response

        article_request = self._article_request()
        result = trigger_article_generation(self.slack_user_id, article_request)

        self.assertEqual(result["job_id"], "job_content_only_123")
        payload = mock_post.call_args.kwargs["json"]
        self.assertIsNone(payload.get("delivery_mode"))
        self.assertIsNone(payload.get("delivery_mode_confirmed"))

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_explicitly_confirms_portable_mode_without_repo(self, mock_post):
        self.config.github_repo = ""
        self.config.article_delivery_mode = "content_only"
        self.config.save(update_fields=["github_repo", "article_delivery_mode"])

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"job_id": "job_saved_content_only_123", "status": "queued"}
        mock_post.return_value = mock_response

        article_request = self._article_request(delivery_mode="content_only", delivery_mode_confirmed=True)
        result = trigger_article_generation(self.slack_user_id, article_request)

        self.assertEqual(result["job_id"], "job_saved_content_only_123")
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["delivery_mode"], "content_only")
        self.assertTrue(payload["delivery_mode_confirmed"])
        self.assertEqual(payload.get("github_repo"), "")

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_allows_repo_less_domain_without_scan_summary(self, mock_post):
        self.config.github_repo = ""
        self.config.scan_summary = None
        self.config.article_system = {}
        self.config.article_delivery_mode = "content_only"
        self.config.save(
            update_fields=[
                "github_repo",
                "scan_summary",
                "article_system",
                "article_delivery_mode",
            ]
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"job_id": "job_repo_less_direct_123", "status": "queued"}
        mock_post.return_value = mock_response

        result = trigger_article_generation(self.slack_user_id,
            self._article_request(delivery_mode="content_only", delivery_mode_confirmed=True))

        self.assertEqual(result["job_id"], "job_repo_less_direct_123")
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["delivery_mode"], "content_only")
        self.assertTrue(payload["delivery_mode_confirmed"])
        self.assertEqual(payload.get("github_repo"), "")

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_discovery_allows_repo_less_domain_without_scan_summary(self, mock_post):
        self.config.github_repo = ""
        self.config.scan_summary = None
        self.config.article_system = {}
        self.config.article_delivery_mode = "content_only"
        self.config.save(
            update_fields=[
                "github_repo",
                "scan_summary",
                "article_system",
                "article_delivery_mode",
            ]
        )

        mock_response = MagicMock()
        mock_response.status_code = 202
        mock_response.json.return_value = {"job_id": "job_repo_less_discovery_123", "status": "queued"}
        mock_post.return_value = mock_response

        result = trigger_article_generation(self.slack_user_id, self._article_request(topic=None))

        self.assertEqual(result["job_id"], "job_repo_less_discovery_123")
        args, kwargs = mock_post.call_args
        self.assertIn("/api/runs/discovery", args[0])
        payload = kwargs["json"]
        self.assertEqual(payload["domain"], "mlai.au")
        self.assertEqual(payload["slack_user_id"], self.slack_user_id)

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_preserves_explicit_delivery_mode_confirmation(self, mock_post):
        self.config.article_system = {}
        self.config.save(update_fields=["article_system"])

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"job_id": "job_explicit_publish_123", "status": "queued"}
        mock_post.return_value = mock_response

        article_request = self._article_request(
            delivery_mode="publish_code",
            delivery_mode_confirmed=True,
        )
        result = trigger_article_generation(self.slack_user_id, article_request)

        self.assertEqual(result["job_id"], "job_explicit_publish_123")
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["delivery_mode"], "publish_code")
        self.assertTrue(payload["delivery_mode_confirmed"])

    @patch("integrations.services.article_generation.http_requests.post")
    def test_confirm_topic_reuses_saved_delivery_mode_from_source_job(self, mock_post):
        source_job = self._source_job(
            job_id="job-source-123",
            domain="mlai.au",
            slack_user_id=self.slack_user_id,
            status="awaiting_confirmation",
            request_meta={
                "domain": "mlai.au",
                "delivery_mode": "content_only",
                "delivery_mode_confirmed": True,
            },
        )
        self._record_article_payment(source_job)

        mock_response = MagicMock()
        mock_response.status_code = 202
        mock_response.json.return_value = {"job_id": "job-confirm-source-123", "status": "queued"}
        mock_post.return_value = mock_response

        result = confirm_topic(
            domain="mlai.au",
            confirmed_keyword="agentic ai",
            slack_user_id=self.slack_user_id,
            source_run_id=source_job.job_id,
            request_source="roo_slackbot",
        )

        self.assertEqual(result["job_id"], "job-confirm-source-123")
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["delivery_mode"], "content_only")
        self.assertTrue(payload["delivery_mode_confirmed"])

    @patch("integrations.services.article_generation.get_github_credentials_for_domain")
    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_uses_scan_summary_article_system_fallback(self, mock_post, mock_get_credentials):
        self.config.article_system = {}
        self.config.scan_summary = {
            "articles_status": {
                "has_articles_system": True,
                "directory_name": "articles",
                "directory_path": "app/articles/content",
                "detected_type": "tsx",
                "existing_files": ["app/articles/content/index.tsx"],
            }
        }
        self.config.save(update_fields=["article_system", "scan_summary"])

        resolved, source = resolve_article_system_with_source(self.config)
        self.assertEqual(resolved["state"], "existing")
        self.assertEqual(source, "scan_summary_fallback")

        mock_get_credentials.return_value = {
            "token": "gh_token_123",
            "repo": self.repo_name,
            "source": "user",
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "job_id": "job_scan_fallback",
            "status": "queued",
        }
        mock_post.return_value = mock_response

        article_request = self._article_request()

        with self.settings(
            CONTENT_FACTORY_API_KEY="test-key",
            CONTENT_FACTORY_DEFAULT_ARTICLE_DELIVERY_MODE="publish_code",
        ):
            result = trigger_article_generation(self.slack_user_id, article_request)

        self.assertEqual(result["job_id"], "job_scan_fallback")

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_sync_failure_auto_refunds(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "boom"
        mock_post.return_value = mock_response

        with self.settings(
            CONTENT_FACTORY_API_KEY="test-key",
            CONTENT_FACTORY_DEFAULT_ARTICLE_DELIVERY_MODE="publish_code",
        ):
            with self.assertRaises(ArticleGenerationError):
                trigger_article_generation(self.slack_user_id, self._article_request())

        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 20)

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_reuses_existing_billed_job_for_same_client_request_id(self, mock_post):
        paid_org = Organization.objects.create(domain="example.com", name="Research before website setup")
        VibeRaisingCompany.objects.create(profile=self.profile, organization=paid_org,
                                         name=paid_org.name, domain=paid_org.domain)
        mock_post.return_value.status_code = 202
        mock_post.return_value.json.return_value = {
            "job_id": "existing-job-123", "status": "awaiting_confirmation"
        }
        request = self._article_request(topic=None, domain=paid_org.domain)
        first = trigger_article_generation(self.slack_user_id, dict(request))
        self.assertEqual(first["job_id"], "existing-job-123")
        first_job = ContentFactoryJob.objects.get(job_id=first["job_id"])
        self.assertEqual(first_job.billing_status, "charged")
        self.assertEqual(first_job.billing_amount, 4)
        ledger_id = first_job.billing_ledger_id
        mock_post.reset_mock()

        result = trigger_article_generation(self.slack_user_id, dict(request))

        self.assertEqual(result["job_id"], "existing-job-123")
        mock_post.assert_not_called()
        first_job.refresh_from_db()
        self.assertEqual(first_job.billing_ledger_id, ledger_id)
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 16)

    @patch("integrations.services.article_generation.http_requests.post")
    def test_unowned_requester_cannot_use_another_founders_verified_website(self, mock_post):
        with self.assertRaises(ArticleGenerationError):
            trigger_article_generation(self.slack_user_id,
                self._article_request(requested_by_slack_user_id="U_UNAUTHORIZED"))
        mock_post.assert_not_called()
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 20)

    @patch("integrations.services.article_generation.http_requests.get")
    def test_check_generation_status_preserves_awaiting_delivery_mode(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "job_id": "job_waiting_mode",
            "status": "awaiting_delivery_mode",
        }
        mock_get.return_value = mock_response

        result = check_generation_status("job_waiting_mode")

        self.assertEqual(result["status"], "awaiting_delivery_mode")

    @patch("integrations.services.article_generation.http_requests.get")
    def test_check_generation_status_falls_back_to_local_run_when_cf_unavailable(self, mock_get):
        ContentFactoryRun.objects.create(
            run_id="run-local-1",
            workflow="direct_generate",
            domain="mlai.au",
            status="awaiting_delivery_mode",
            current_step="awaiting_delivery_mode",
            run_request={"domain": "mlai.au"},
        )
        mock_get.side_effect = requests.exceptions.RequestException("boom")

        result = check_generation_status("run-local-1")

        self.assertEqual(result["job_id"], "run-local-1")
        self.assertEqual(result["status"], "awaiting_delivery_mode")
