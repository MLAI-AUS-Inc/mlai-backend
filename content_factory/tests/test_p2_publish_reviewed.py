"""Saved-draft publishing preserves content, tenant scope, consent and one charge."""
import hashlib
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import TransactionTestCase, SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from content_factory import publish_reviewed_draft as service
from content_factory.self_heal_testing import ALL_FLAGS_ON, make_p2_org
from content_factory.website_models import WebsiteConnection, WebsiteConnectionTarget
from workflow_runs.models import ContentFactoryRun


@override_settings(**ALL_FLAGS_ON, CONTENT_FACTORY_URL='https://cf.synthetic.test', CONTENT_FACTORY_API_KEY='synthetic-key',
                   ARTICLES_FREE_FIRST_ARTICLE_ENABLED=True, ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=False,
                   ARTICLES_FREE_FIRST_ARTICLE_DAILY_CAP=0)
class ReviewedPublishingTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.fixture = make_p2_org()
        self.org, self.user = self.fixture.organization, self.fixture.owner
        self.context = SimpleNamespace(organization=self.org)
        from content_factory.first_article_entitlement import reserve_entitlement, ENTITLEMENT
        reservation = reserve_entitlement(organization=self.org, user=self.user, client_request_id='saved-source',
            data={'expectedCostPoints': 0, 'delivery_mode': 'content_only', 'delivery_mode_confirmed': True},
            config=self.fixture.config)
        self.assertIsNotNone(reservation)
        self.connection = WebsiteConnection.objects.create(organization=self.org, repository_id=123,
            installation_id='45', github_repo='example/site', branch='main', verified_sha='a' * 40,
            capabilities={'publishingReady': True})
        WebsiteConnectionTarget.objects.create(connection=self.connection, generation=1, target_key='articles',
            source_sha='a' * 40, verified_at=timezone.now(), capabilities={'publishingReady': True})
        config = self.fixture.config
        config.website_connection = self.connection
        config.default_publish_target_id = 'articles'
        config.github_repo = 'example/site'
        config.save(update_fields=['website_connection', 'default_publish_target_id', 'github_repo'])
        self.run = ContentFactoryRun.objects.create(run_id='saved-source', organization=self.org,
            domain=self.org.domain, workflow='confirmed_topic', status='completed',
            run_request={'delivery_mode': 'content_only', 'client_request_id': 'saved-source',
                'roo_points_billing_status': 'entitled', 'roo_points_entitlement': ENTITLEMENT,
                'roo_points_ledger_id': str(reservation.pk)}, result={'delivery_mode': 'content_only', 'reviewFindings': []})
        from content_factory.first_article_entitlement import consume_entitlement
        consume_entitlement(organization=self.org, run=self.run)
        self.body = 'The founder\'s saved edit.\n\nKeep this paragraph exactly.'
        self.review = {'revision': 'revision-1', 'previewPending': True, 'reviewFindings': [],
            'articleExport': {'version': 1, 'status': 'ready', 'revision': 'revision-1', 'runId': self.run.run_id,
                              'format': 'markdown', 'markdown': self.body, 'metadata': {}, 'media': []}}
        self.marker = patch.object(service, 'read_marker', return_value={'publishAutomatically': {'enabledAt': '2026-11-02T00:00:00Z'}})
        self.marker.start(); self.addCleanup(self.marker.stop)
        review = patch('content_factory.article_review_views.remote_review', return_value=self.review)
        review.start(); self.addCleanup(review.stop)
        latest = patch('content_factory.vibe_marketing_views._latest_review_ready_component_revision', return_value=None)
        latest.start(); self.addCleanup(latest.stop)
        self.posts = []
        def post(url, *, json, **kwargs):
            self.posts.append((url, json))
            return SimpleNamespace(status_code=202, json=lambda: {'run_id': json['new_run_id'], 'status': 'queued'})
        remote = patch('integrations.http_client.post', side_effect=post)
        self.remote = remote.start(); self.addCleanup(remote.stop)

    def start(self, **kwargs):
        return service.start_publish(context=self.context, source_run=self.run, user=self.user, **kwargs)

    def test_one_click_publishes_exact_body_with_carried_charge_and_no_debit(self):
        from roo.models import Ledger
        count = Ledger.objects.count()
        run, created = self.start()
        self.assertTrue(created)
        self.assertEqual(len(self.posts), 1)
        payload = self.posts[0][1]
        self.assertEqual(payload['approved_body'], self.body)
        self.assertEqual(payload['approved_body_sha256'], hashlib.sha256(self.body.encode()).hexdigest())
        self.assertEqual(payload['merge_policy'], 'backend_worker')
        self.assertEqual(payload['source_run_id'], self.run.run_id)
        run.refresh_from_db()
        self.assertEqual(run.result['article_approval_v1']['source'], 'publish_reviewed')
        self.assertEqual(run.result['article_approval_v1']['claims_open'], 0)
        self.assertEqual(run.result['points_v1']['state'], 'carried')
        self.assertEqual(run.run_request['original_billing_source_run_id'], self.run.run_id)
        self.assertEqual(Ledger.objects.count(), count)

    def test_second_click_returns_same_publish_run_without_second_dispatch(self):
        first, _ = self.start()
        self.run.refresh_from_db()
        second, created = self.start()
        self.assertFalse(created)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(ContentFactoryRun.objects.count(), 2)
        self.assertIsNone(service.publish_offer(self.run))

    def test_dispatch_reply_does_not_erase_fast_callback(self):
        def post(url, *, json, **kwargs):
            run = ContentFactoryRun.objects.get(run_id=json['new_run_id'])
            ContentFactoryRun.objects.filter(pk=run.pk).update(result={**run.result, 'publish_pr_v1': {'number': 9}})
            return SimpleNamespace(status_code=202, json=lambda: {'run_id': run.run_id})
        self.remote.side_effect = post
        run, _ = self.start()
        run.refresh_from_db()
        self.assertEqual(run.result['publish_pr_v1'], {'number': 9})
        self.assertIn('article_approval_v1', run.result)

    def test_timeout_preserves_one_reservation_for_retry(self):
        self.remote.side_effect = TimeoutError('synthetic transport loss')
        with self.assertRaises(service.PublishReviewedError):
            self.start()
        self.run.refresh_from_db()
        reserved = self.run.result['publish_offer_v1']['run_id']
        self.remote.side_effect = lambda url, **kwargs: SimpleNamespace(status_code=202, json=lambda: {'run_id': reserved})
        run, created = self.start()
        self.assertFalse(created)
        self.assertEqual(run.run_id, reserved)
        self.assertEqual(ContentFactoryRun.objects.count(), 2)

    def test_claims_and_unreadable_review_never_create_a_run(self):
        for findings in [[{'kind': 'claim', 'id': 'claim-1'}], None, [{}]]:
            self.review['reviewFindings'] = findings
            with self.subTest(findings=findings), self.assertRaises(service.PublishReviewedError) as error:
                self.start()
            self.assertEqual(error.exception.reason, 'claims_open')
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        self.remote.assert_not_called()

    def test_foreign_context_has_no_reads_runs_or_calls(self):
        foreign = make_p2_org()
        with self.assertRaises(service.PublishReviewedError) as error:
            service.start_publish(context=SimpleNamespace(organization=foreign.organization), source_run=self.run, user=self.user)
        self.assertEqual(error.exception.status, 404)
        self.remote.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.count(), 1)

    def test_missing_provider_and_feature_off_are_inert(self):
        with patch.object(service, '_provider', return_value=None), self.assertRaises(service.PublishReviewedError) as error:
            self.start()
        self.assertEqual(error.exception.status, 503)
        with override_settings(ARTICLES_PUBLISH_REVIEWED_DRAFT_ENABLED=False):
            self.assertIsNone(service.publish_offer(self.run))
            with self.assertRaises(service.PublishReviewedError): self.start()
        self.remote.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.count(), 1)

    def test_dismiss_is_idempotent_and_never_publishes(self):
        self.assertIsNotNone(service.publish_offer(self.run))
        service.dismiss_offer(source_run=self.run, user=self.user)
        self.run.refresh_from_db(); first = self.run.result
        service.dismiss_offer(source_run=self.run, user=self.user)
        self.run.refresh_from_db()
        self.assertEqual(first, self.run.result)
        self.assertIsNone(service.publish_offer(self.run))
        self.remote.assert_not_called()

    def test_account_api_replays_one_key(self):
        from content_factory.publish_reviewed_draft_views import PublishReviewedDraftView
        data = {'company_id': str(self.fixture.company.pk), 'idempotencyKey': 'publish-key'}
        def send():
            request = APIRequestFactory().post('/synthetic', data, format='json')
            force_authenticate(request, self.user)
            return PublishReviewedDraftView.as_view()(request, run_id=self.run.run_id)
        first, second = send(), send()
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(first.data, second.data)
        self.assertEqual(len(self.posts), 1)

    def test_nonowner_without_publish_right_cannot_start(self):
        other = make_p2_org().owner
        with self.assertRaises(service.PublishReviewedError) as error:
            service.start_publish(context=self.context, source_run=self.run, user=other)
        self.assertEqual(error.exception.status, 403)
        self.remote.assert_not_called()

    def test_target_without_verified_publishing_capability_is_not_offered(self):
        self.connection.targets.update(capabilities={})
        self.assertIsNone(service.publish_offer(self.run))
        with self.assertRaises(service.PublishReviewedError):
            self.start()
        self.remote.assert_not_called()

    def test_unbacked_charge_rolls_back_child_before_any_dispatch(self):
        from roo.models import Ledger
        Ledger.objects.all().delete()
        with self.assertRaises(service.PublishReviewedError) as error:
            self.start()
        self.assertEqual(error.exception.reason, 'not_allowed')
        self.run.refresh_from_db()
        self.assertNotIn('publish_offer_v1', self.run.result)
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        self.remote.assert_not_called()

    @override_settings(ARTICLES_HOSTED_SITES_ENABLED=True)
    def test_active_hosted_uses_existing_hosted_pipeline_without_repository_dispatch_and_records_live(self):
        from content_factory.hosted_article_models import HostedArticleSite
        HostedArticleSite.objects.create(organization=self.org, domain=self.org.domain, state='active')
        child, created = service.start_publish(context=self.context, source_run=self.run, user=self.user)
        self.assertTrue(created)
        self.assertNotIn('hosted_site_id', child.run_request)
        self.assertEqual(child.github_repo, self.connection.github_repo)
        self.assertIn('/publish-reviewed-draft', self.posts[0][0])

    @override_settings(ARTICLES_HOSTED_SITES_ENABLED=True, HOSTED_PUBLISH_REQUIRES_RENDERED_PREVIEW=True)
    def test_hosted_publish_requires_the_current_rendered_revision_when_gate_on(self):
        from content_factory.hosted_article_models import HostedArticleSite
        HostedArticleSite.objects.create(organization=self.org, domain=self.org.domain, state='active')
        child, _ = service.start_publish(context=self.context, source_run=self.run, user=self.user)
        self.assertNotIn('hosted_site_id', child.run_request)

    def test_same_run_switched_channel_with_delivered_evidence_is_offered_without_mutating_bound_request(self):
        self.run.run_request['delivery_mode'] = 'review_draft'
        self.run.result = {'channel_v1': {'waiting_to_publish': True, 'channel': 'copy_ready'},
            'draft_ready_v1': {'version': 1}, 'reviewFindings': []}
        self.run.save(update_fields=['run_request', 'result'])
        self.assertIsNotNone(service.publish_offer(self.run))
        self.run.result.pop('draft_ready_v1')
        self.run.status = 'failed'
        self.assertIsNone(service.publish_offer(self.run))

    def test_specialized_retry_preserves_exact_approved_body_and_destination_without_draft_reads_or_debit(self):
        from roo.models import Ledger
        run, _ = self.start()
        before = Ledger.objects.count()
        with patch.object(service, "_read_draft", side_effect=AssertionError("Retry must use retained body")):
            response = service.retry_publish(run)
        self.assertEqual(response["run_id"], run.run_id)
        self.assertEqual(len(self.posts), 2)
        self.assertEqual({k:v for k,v in self.posts[1][1].items() if k != "publish_retry"}, self.posts[0][1])
        self.assertTrue(self.posts[1][1]["publish_retry"])
        self.assertEqual(Ledger.objects.count(), before)
        self.connection.branch = "changed"
        self.connection.save(update_fields=["branch"])
        with self.assertRaises(service.PublishReviewedError):
            service.retry_publish(run)
        self.assertEqual(len(self.posts), 2)

    def test_specialized_retry_rejects_modified_saved_body_and_revoked_actor(self):
        run, _ = self.start()
        ContentFactoryRun.objects.filter(pk=run.pk).update(run_request={**run.run_request, "approved_body": "forged"})
        with self.assertRaises(service.PublishReviewedError):
            service.retry_publish(run)
        self.assertEqual(len(self.posts), 1)

    def test_deployed_publish_child_updates_original_article_without_replacing_its_source(self):
        from content_factory.models import WrittenArticle
        article = WrittenArticle.objects.create(organization=self.org, source_run_id=self.run.run_id, slug="saved-source",
            title="Saved source", category="guides", primary_keyword="saved")
        run, _ = self.start()
        self.run.refresh_from_db()
        self.assertEqual(service.published_child_for_source(self.run).pk, run.pk)
        run.result["deploy_v1"] = {"url": "https://"+self.org.domain+"/blog/saved-source/", "verified_at": timezone.now().isoformat()}
        run.save(update_fields=["result"])
        self.assertEqual(service.sync_published_article(run).pk, article.pk)
        article.refresh_from_db()
        self.assertEqual(article.source_run_id, self.run.run_id)
        self.assertEqual(article.live_url, run.result["deploy_v1"]["url"])

    @override_settings(ARTICLES_HOSTED_SITES_ENABLED=True)
    def test_an_already_live_hosted_article_is_not_offered_a_duplicate_publish(self):
        from content_factory.hosted_article_models import HostedArticleSite, HostedArticlePublication
        site = HostedArticleSite.objects.create(organization=self.org, domain=self.org.domain, state='active')
        HostedArticlePublication.objects.create(site=site, run_id=self.run.run_id, slug='old', status='live')
        self.assertIsNotNone(service.publish_offer(self.run))
