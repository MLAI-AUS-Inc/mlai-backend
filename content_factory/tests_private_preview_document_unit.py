"""Private SSR article previews retain their rendered copy in an opaque sandbox."""
from unittest.mock import patch
from types import SimpleNamespace
from urllib.parse import urlparse, parse_qs

from bs4 import BeautifulSoup
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from .article_preview_lease import ArticlePreviewLeaseProxyView, ArticlePreviewLeaseView, read_only_article_document


class PrivatePreviewDocumentTests(SimpleTestCase):
    def article(self):
        return b'''<!doctype html><html><head>
          <link rel="stylesheet" href="/assets/article.css">
          <script data-content-factory-inspector="true">window.inspectorReady=true</script>
          <script type="application/ld+json">{"headline":"Saved article"}</script>
          <script>sessionStorage.getItem('theme')</script>
          <script src="/assets/analytics.js"></script>
          </head><body><article data-cf-component-id="article">
          <h1>Saved article</h1><p data-cf-review-field="paragraph-1">Exact saved copy.</p>
          <img src="/assets/hero.png" alt="A pilot checklist" onload="document.cookie='seen=1'">
          <a href="/events">Compare the checklist with peers</a></article>
          <script type="module">hydrateRoot(document, siteRouter())</script></body></html>'''

    def test_ssr_copy_assets_inspector_and_structured_data_survive_without_site_runtime(self):
        result = read_only_article_document(self.article())
        document = BeautifulSoup(result, 'html.parser')
        self.assertEqual(document.find('h1').get_text(), 'Saved article')
        self.assertEqual(document.select_one('[data-cf-review-field]').get_text(), 'Exact saved copy.')
        self.assertEqual(document.find('img')['src'], '/assets/hero.png')
        self.assertEqual(document.find('a')['href'], '/events')
        self.assertEqual(document.find('link')['href'], '/assets/article.css')
        self.assertEqual(len(document.find_all('script')), 2)
        self.assertIsNotNone(document.select_one('script[data-content-factory-inspector=true]'))
        self.assertIsNotNone(document.select_one('script[type="application/ld+json"]'))
        for runtime in (b'sessionStorage', b'analytics.js', b'hydrateRoot', b'onload'):
            self.assertNotIn(runtime, result)

    def test_non_article_and_non_utf8_documents_are_unchanged(self):
        for body in (b'<main id="app"></main><script src="app.js"></script>', b'\xff'):
            self.assertEqual(read_only_article_document(body), body)

    def test_authorized_grant_explicitly_enables_the_comment_inspector(self):
        run = SimpleNamespace(run_id='draft-1', organization_id=7, domain='company.test',
            run_request={'delivery_mode': 'content_only', 'delivery_mode_confirmed': True})
        context = SimpleNamespace(organization=SimpleNamespace(id=7, domain=run.domain))
        request = SimpleNamespace(data={}, build_absolute_uri=lambda path: 'https://api.example' + path)
        with patch('content_factory.website_views._context', return_value=(context, object(), None)), \
                patch('workflow_runs.models.ContentFactoryRun.objects.filter') as runs, \
                patch('content_factory.article_preview_lease.views._resolve_context_or_response', return_value=(context, None)), \
                patch('content_factory.article_preview_lease.views.get_object_or_404', return_value=run):
            runs.return_value.first.return_value = run
            response = ArticlePreviewLeaseView().post(request, run.run_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(parse_qs(urlparse(response.data['url']).query), {'cfInspector': ['1']})
        self.assertEqual(response.data['expiresIn'], 900)

    @patch.object(ArticlePreviewLeaseProxyView, '_proxy')
    def test_private_proxy_retains_sandbox_and_does_not_grant_same_origin(self, proxy):
        proxy.return_value = HttpResponse(self.article(), content_type='text/html')
        response = ArticlePreviewLeaseProxyView().get(RequestFactory().get('/preview'),
            'draft-1', token='synthetic-grant')
        self.assertIn(b'Exact saved copy.', response.content)
        self.assertNotIn(b'hydrateRoot', response.content)
        self.assertIn('sandbox allow-scripts', response['Content-Security-Policy'])
        self.assertNotIn('allow-same-origin', response['Content-Security-Policy'])
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')
        self.assertEqual(response['Cache-Control'], 'private, no-store')
