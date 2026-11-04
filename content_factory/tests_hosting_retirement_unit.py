"""The removed blog-hosting surface cannot be restored by old flags or requests."""
from types import SimpleNamespace
from unittest.mock import patch
from django.apps import apps
from django.test import SimpleTestCase, override_settings
from django.urls import resolve, Resolver404
from .publish_reviewed_draft import _send_dispatch, PublishReviewedError
from .failure_policy_engine import _rung
from .website_strategy import RUNGS

class BlogHostingRemovedTests(SimpleTestCase):
    def test_legacy_models_are_not_registered(self):
        for name in ['HostedArticleSite', 'HostedArticlePublication']:
            with self.assertRaises(LookupError): apps.get_model('content_factory', name)

    def test_legacy_service_and_publish_routes_are_absent(self):
        for path in ['/api/content-factory/hosted-sites/', '/api/content-factory/hosted-sites/source/',
                     '/api/content-factory/hosted-sites/publications/legacy']:
            with self.assertRaises(Resolver404): resolve(path)

    def test_old_publish_path_has_only_the_generic_control_fallback(self):
        match=resolve('/api/v1/vibe-marketing/runs/legacy/hosted-publish')
        self.assertEqual(match.func.view_class.__name__, 'VibeMarketingRunControlView')

    def test_saved_hosted_publication_cannot_reenter_repository_dispatch(self):
        with patch('integrations.http_client.post') as remote:
            with self.assertRaises(PublishReviewedError):
                _send_dispatch(SimpleNamespace(), SimpleNamespace(run_request={'hosted_site_id':'old'}))
        remote.assert_not_called()

    def test_saved_hosted_strategy_uses_the_repository_ladder(self):
        self.assertNotIn('hosted', RUNGS)
        self.assertEqual(_rung({}, {'strategy_pin':'hosted'}), 'native_collection')
