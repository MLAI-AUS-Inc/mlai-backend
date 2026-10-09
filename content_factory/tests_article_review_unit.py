"""Draft review authorization and approval tests; storage and network are mocked."""
from contextlib import nullcontext
from types import SimpleNamespace
import uuid
from unittest.mock import MagicMock, patch

from django.core import signing
from django.http import HttpResponse
from django.test import SimpleTestCase, RequestFactory
from rest_framework.response import Response

from .article_review_views import check_approval_comments, remote_review, VibeMarketingArticleReviewView
from .article_preview_lease import ArticlePreviewLeaseProxyView, SALT, LIFETIME


class ArticleReviewTests(SimpleTestCase):
    def setUp(self):
        self.run = SimpleNamespace(run_id='article-1', workflow='article_generation', result={}, organization_id=7, domain='company.test', run_request={'delivery_mode': 'content_only'})

    @patch('content_factory.article_review_views.views.VibeMarketingComponentComment')
    def test_comments_require_exact_waiver_including_body(self, model):
        model.objects.filter.return_value.order_by.return_value = [SimpleNamespace(id='comment-1', body='Please revise')]
        self.assertEqual(check_approval_comments(self.run, {}) .status_code, 409)
        self.assertEqual(check_approval_comments(self.run, {'waivedComments': [{'id': 'comment-1', 'body': 'Old text'}]}).status_code, 409)
        self.assertIsNone(check_approval_comments(self.run, {'waivedComments': [{'id': 'comment-1', 'body': 'Please revise'}]}))

    @patch('content_factory.article_review_views.views.VibeMarketingComponentComment')
    def test_comment_added_after_dialog_and_inflight_revision_reject_approval(self, model):
        model.objects.filter.return_value.order_by.return_value = []
        self.assertIsNone(check_approval_comments(self.run, {}))
        self.run.result = {'component_feedback_latest_batch': {'status': 'running'}}
        self.assertEqual(check_approval_comments(self.run, {}).status_code, 409)

    @patch('content_factory.article_review_views.remote_review')
    def test_failed_ownership_never_calls_remote(self, remote):
        view = VibeMarketingArticleReviewView()
        view._resolve_run = MagicMock(return_value=(None, None, Response({'detail': 'Run not found.'}, status=404)))
        result = view.post(SimpleNamespace(data={'action': 'editText'}), 'foreign-run')
        self.assertEqual(result.status_code, 404)
        remote.assert_not_called()

    @patch('content_factory.article_review_views.views._component_feedback_from_run', return_value={})
    @patch('content_factory.article_review_views.views._latest_review_ready_component_revision', return_value=SimpleNamespace(run_id='revision-2'))
    @patch('content_factory.article_review_views.remote_review')
    def test_saved_export_is_denied_when_owner_has_a_newer_ready_revision(self, remote, latest, feedback):
        remote.return_value = {'revision': 'saved', 'previewPending': False, 'articleExport': {
            'version': 1, 'status': 'ready', 'runId': self.run.run_id, 'revision': 'saved',
            'format': 'markdown', 'markdown': '# Exact saved text', 'metadata': {}, 'media': []}}
        view = VibeMarketingArticleReviewView()
        context = SimpleNamespace(organization=SimpleNamespace(domain=self.run.domain))
        view._resolve_run = MagicMock(return_value=(context, self.run, None))
        result = view.get(SimpleNamespace(user=SimpleNamespace(pk=1)), self.run.run_id)
        self.assertEqual(result.data['articleExport']['status'], 'unavailable')
        self.assertEqual(result.data['articleExport']['reasonCode'], 'revision_superseded')
        self.assertEqual(result.data['articleExport']['markdown'], '')
        self.assertEqual(result['Cache-Control'], 'private, no-store')
        latest.assert_called_once_with(self.run, context)

    @patch('content_factory.article_review_views.views._latest_review_ready_component_revision')
    @patch('content_factory.article_review_views.views._run_has_external_publish_evidence', return_value=False)
    @patch('content_factory.article_review_views.remote_review')
    def test_superseded_run_cannot_be_edited(self, remote, published, latest):
        latest.return_value = SimpleNamespace(run_id='revision-2')
        view = VibeMarketingArticleReviewView()
        view._resolve_run = MagicMock(return_value=(object(), self.run, None))
        result = view.post(SimpleNamespace(data={'action': 'editText'}), self.run.run_id)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.data['latestRunId'], 'revision-2')
        remote.assert_not_called()

    @patch('content_factory.article_review_views.views._content_factory_headers', return_value={'X-API-Key': 'service-only'})
    @patch('content_factory.article_review_views.views._content_factory_remote_config', return_value={'enabled': True, 'base_url': 'https://factory.test'})
    @patch('content_factory.article_review_views.views.http_client.request')
    def test_revision_conflict_survives_facade(self, request, config, headers):
        request.return_value.status_code = 409
        request.return_value.json.return_value = {'detail': 'The article changed.'}
        result = remote_review(self.run, payload={'action': 'editText'})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.data['detail'], 'The article changed.')
        self.assertEqual(request.call_args.kwargs['headers'], {'X-API-Key': 'service-only'})

    @patch('content_factory.article_preview_lease.authority_guard', side_effect=lambda *args, **kwargs: nullcontext())
    @patch('content_factory.article_preview_lease.views.get_object_or_404')
    def test_preview_grant_is_scoped_to_run_and_company(self, lookup, guard):
        binding = {'website_connection_id': str(uuid.uuid4()), 'connection_generation': 1}
        self.run.run_request = binding
        token = signing.dumps({'run': 'article-1', 'organization': 7, 'domain': 'company.test', **binding}, salt=SALT)
        view = ArticlePreviewLeaseProxyView()
        view.kwargs = {'token': token}
        request = RequestFactory().get('/preview')
        lookup.return_value = self.run
        self.assertIsNone(view._resolve_run(request, 'article-1')[2])
        self.assertEqual(guard.call_args.kwargs['action'], 'read')
        self.assertEqual(guard.call_args.args[0]['connection_generation'], 1)
        self.assertEqual(view._resolve_run(request, 'article-2')[2].status_code, 404)
        self.run.organization_id = 8
        self.assertEqual(view._resolve_run(request, 'article-1')[2].status_code, 404)

    @patch('content_factory.article_preview_lease.views.get_object_or_404')
    def test_expired_or_tampered_grants_never_lookup_article(self, lookup):
        view = ArticlePreviewLeaseProxyView()
        request = RequestFactory().get('/preview')
        view.kwargs = {'token': 'tampered'}
        self.assertEqual(view._resolve_run(request, 'article-1')[2].status_code, 401)
        with patch('django.core.signing.time.time', return_value=1):
            token = signing.dumps({'run': 'article-1', 'organization': 7, 'domain': 'company.test'}, salt=SALT)
        view.kwargs = {'token': token}
        with patch('django.core.signing.time.time', return_value=LIFETIME + 5):
            self.assertEqual(view._resolve_run(request, 'article-1')[2].status_code, 401)
        lookup.assert_not_called()

    @patch.object(ArticlePreviewLeaseProxyView, '_proxy')
    def test_preview_response_rewrites_links_and_restricts_headers(self, proxy):
        original = '/api/v1/vibe-marketing/runs/article-1/live-preview/'
        response = HttpResponse(
            f'<a href="{original}proxy/next">Next</a>'
            f'<img src="{original}resource?url=https%3A%2F%2Fexample.test%2Fimage.png">',
            content_type='text/html',
        )
        response['Set-Cookie'] = 'session=secret'
        response['Location'] = 'https://example.test'
        response['Content-Length'] = str(len(response.content))
        proxy.return_value = response

        result = ArticlePreviewLeaseProxyView().get(
            RequestFactory().get('/preview'), 'article-1', token='preview-grant',
        )

        self.assertIs(result, response)
        self.assertIn(b'/article-preview/preview-grant/article-1/next', result.content)
        self.assertIn(b'/article-preview/preview-grant/article-1/__resource?url=', result.content)
        self.assertNotIn(original.encode(), result.content)
        self.assertEqual(result['Referrer-Policy'], 'no-referrer')
        self.assertEqual(result['Cache-Control'], 'private, no-store')
        for header in ('Set-Cookie', 'Location', 'Content-Length'):
            self.assertNotIn(header, result)

    @patch.object(ArticlePreviewLeaseProxyView, '_proxy')
    def test_preview_error_preserves_unrendered_response(self, proxy):
        proxy.return_value = Response({'detail': 'Preview access expired.'}, status=401)

        result = ArticlePreviewLeaseProxyView().get(
            RequestFactory().get('/preview'), 'article-1', token='expired-grant',
        )

        self.assertIs(result, proxy.return_value)
        self.assertEqual(result.status_code, 401)
        self.assertEqual(result['Referrer-Policy'], 'no-referrer')
        self.assertEqual(result['Cache-Control'], 'private, no-store')


    @patch.object(ArticlePreviewLeaseProxyView, '_proxy')
    def test_next_setup_assets_stay_inside_the_scoped_preview_lease(self, proxy):
        from . import vibe_marketing_views as views

        body = (
            '<link rel="stylesheet" href="/_next/static/chunks/site.css">'
            '<script src="/_next/static/chunks/site.js"></script>'
            '<img src="/_next/image?url=%2Fbrand%2Flogo.png&amp;w=64&amp;q=75">'
            '<a href="/articles/example-article">Example</a>'
        ).encode()
        rewritten = views._rewrite_live_preview_proxy_body('article-1', body, 'text/html')
        proxy.return_value = HttpResponse(rewritten, content_type='text/html')
        result = ArticlePreviewLeaseProxyView().get(
            RequestFactory().get('/preview'), 'article-1', token='synthetic-grant',
        )
        prefix = b'/api/v1/vibe-marketing/article-preview/synthetic-grant/article-1/'
        for asset in (b'_next/static/chunks/site.css', b'_next/static/chunks/site.js',
                      b'_next/image?url=%2Fbrand%2Flogo.png'):
            self.assertIn(prefix + asset, result.content)
        self.assertIn(b'href="/articles/example-article"', result.content)
        self.assertNotIn(b'/live-preview/proxy/', result.content)
        self.assertIn('sandbox allow-scripts', result['Content-Security-Policy'])
        self.assertEqual(result['Referrer-Policy'], 'no-referrer')

    def test_next_css_fonts_imports_and_images_use_the_same_run_proxy(self):
        from . import vibe_marketing_views as views

        body = (b'@import "/_next/static/chunks/layout.css";'
                b'@font-face{src:url(/_next/static/media/font.woff2)}'
                b'.hero{background:url("/_next/static/media/hero.webp")}'
                b'.outside{background:url("/unrelated/path")}')
        rewritten = views._rewrite_live_preview_proxy_body('article-1', body, 'text/css')
        prefix = b'/api/v1/vibe-marketing/runs/article-1/live-preview/proxy/'
        for asset in (b'_next/static/chunks/layout.css', b'_next/static/media/font.woff2',
                      b'_next/static/media/hero.webp'):
            self.assertIn(prefix + asset, rewritten)
        self.assertIn(b'url("/unrelated/path")', rewritten)
        self.assertEqual(
            views._rewrite_live_preview_proxy_body('article-1', rewritten, 'text/css'), rewritten,
        )


class FeedbackOutcomeTests(SimpleTestCase):
    def source_comment(self, **overrides):
        values = dict(id='one', component_id='paragraph', component_type='text', component_label='Paragraph', source_section_id='', selector='', anchor={}, context={}, body='Change it', status='submitted', batch_id='batch', created_at=None, updated_at=None, actor=None)
        values.update(overrides)
        return SimpleNamespace(**values)

    @patch('content_factory.article_review_feedback.views.VibeMarketingComponentComment')
    @patch('content_factory.article_review_feedback.views.ContentFactoryRun')
    def test_unaddressed_and_late_comments_carry_forward_without_duplicates(self, runs, comments):
        from .article_review_feedback import inherited_feedback
        run = SimpleNamespace(run_id='child', workflow='article_revision', status='completed', organization_id=7, domain='company.test', run_request={'source_run_id':'source', 'feedback_batch_id':'batch'}, result={'comment_outcomes':[{'commentId':'one', 'status':'unaddressed', 'summary':'Saved text preserved'}]})
        runs.objects.filter.return_value.first.return_value = SimpleNamespace(organization_id=7)
        comments.objects.filter.return_value.order_by.return_value = [self.source_comment(), self.source_comment(id='late', status='draft', batch_id='')]
        carried = inherited_feedback(run, [])
        self.assertEqual([record['id'] for _, record in carried], ['one', 'late'])
        self.assertEqual(carried[0][1]['status'], 'draft')
        self.assertEqual(carried[0][1]['outcome'], 'Saved text preserved')
        self.assertEqual(len(inherited_feedback(run, [self.source_comment(context={'sourceCommentId':'one'})])), 1)
        runs.objects.filter.return_value.first.return_value.organization_id = 8
        self.assertEqual(inherited_feedback(run, []), [])

    @patch('content_factory.article_review_feedback.views._promote_editorial_feedback_batch')
    @patch('content_factory.article_review_feedback.views.VibeMarketingComponentComment')
    def test_partial_revision_accepts_only_addressed_and_never_learns_unresolved(self, model, promote):
        from .article_review_feedback import accept_addressed_feedback
        model.objects.filter.return_value.values_list.return_value = ['one', 'two']
        run = SimpleNamespace(run_id='child', result={'comment_outcomes':[{'commentId':'one', 'status':'addressed'}, {'commentId':'two', 'status':'unaddressed'}]})
        self.assertEqual(accept_addressed_feedback(run, object(), 'batch'), (0, 0))
        model.objects.filter.return_value.filter.assert_called_once_with(id__in=['one'])
        promote.assert_not_called()

    @patch('content_factory.article_review_feedback.inherited_feedback')
    @patch('content_factory.article_review_views.views.VibeMarketingComponentComment')
    def test_approval_requires_exact_waiver_for_inherited_unresolved_comments(self, model, inherited):
        model.objects.filter.return_value.order_by.return_value = []
        inherited.return_value = [(None, {'id':'one', 'body':'Preserve it', 'status':'draft'})]
        run = SimpleNamespace(run_id='child', workflow='article_revision', result={})
        self.assertEqual(check_approval_comments(run, {}).status_code, 409)
        self.assertIsNone(check_approval_comments(run, {'waivedComments':[{'id':'one', 'body':'Preserve it'}]}))


class PreviewRefreshAttemptTests(SimpleTestCase):
    def exercise(self, *, snapshot=None, denial=None):
        run = SimpleNamespace(run_id='saved-article', status='blocked', workflow='article_revision',
            domain='example.test', github_repo='owner/site', run_request={
                'delivery_mode': 'content_only', 'website_connection_id': str(uuid.uuid4()),
                'connection_generation': 1, 'operation_id': str(uuid.uuid4()),
                'operation_attempt': 2, 'deletion_epoch': 0})
        snapshot = snapshot if snapshot is not None else {'revision': 'canonical-copy', 'previewPending': True, 'refreshError': 'Retry preview'}
        responses = [SimpleNamespace(status_code=200, json=lambda: snapshot),
                     SimpleNamespace(status_code=200, json=lambda: {'revision': 'canonical-copy', 'previewPending': True})]
        with patch('content_factory.article_review_views.views.authority_guard', side_effect=denial or (lambda *args, **kwargs: nullcontext())), \
             patch('content_factory.article_review_views.views._content_factory_remote_config', return_value={'enabled': True, 'base_url': 'https://factory.test'}), \
             patch('content_factory.article_review_views.views._content_factory_headers', return_value={'X-API-Key': 'synthetic'}), \
             patch('content_factory.article_review_views.views.http_client.request', side_effect=responses) as request, \
             patch('content_factory.website_operations.advance_workflow_attempt', return_value={
                 'operation_id': run.run_request['operation_id'], 'operation_attempt': 3, 'deletion_epoch': 0}) as advance:
            result = remote_review(run, payload={'action': 'refresh'})
        return result, run, request, advance

    def test_failed_saved_refresh_reserves_original_operation_before_dispatch(self):
        result, run, request, advance = self.exercise()
        advance.assert_called_once_with(run)
        self.assertEqual([call.args[0] for call in request.call_args_list], ['GET', 'POST'])
        payload = request.call_args.kwargs['json']
        self.assertEqual(payload['operation_attempt'], 3)
        self.assertEqual(payload['operation_id'], run.run_request['operation_id'])
        self.assertEqual(payload['website_connection_id'], run.run_request['website_connection_id'])
        self.assertEqual(payload['expectedRevision'], 'canonical-copy')
        self.assertEqual(result['revision'], 'canonical-copy')

    def test_ready_refresh_is_a_noop_without_an_operation_attempt(self):
        result, run, request, advance = self.exercise(snapshot={'revision': 'saved', 'previewPending': False})
        advance.assert_not_called()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result['revision'], 'saved')

    def test_revoked_connection_cannot_reserve_or_dispatch_refresh(self):
        from .website_contract import WebsiteAuthorityError
        result, run, request, advance = self.exercise(denial=WebsiteAuthorityError('website_connection_changed', 'Reload the website.'))
        self.assertEqual(result.status_code, 409)
        advance.assert_not_called()
        request.assert_not_called()
