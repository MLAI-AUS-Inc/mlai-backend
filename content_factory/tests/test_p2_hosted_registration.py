"""Legacy allowlists never create or reactivate retired blog hosting."""
from django.test import TestCase, override_settings
from content_factory.hosted_article_models import HostedArticleSite
from content_factory.hosted_auto_register import run_hosted_sweep, hosted_rung_ready


class HostedRegistrationTests(TestCase):
    @override_settings(ARTICLES_HOSTED_AUTO_REGISTER_ENABLED=True, ARTICLES_HOSTED_RUNG_ORGANIZATIONS='all')
    def test_allowlist_cannot_create_hosting(self):
        before = HostedArticleSite.objects.count()
        self.assertEqual(run_hosted_sweep(), {'status': 'retired'})
        self.assertFalse(hosted_rung_ready(None))
        self.assertEqual(HostedArticleSite.objects.count(), before)
