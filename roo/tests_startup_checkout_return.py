"""Test-mode checkout return URLs retain opaque UI intent without trusting it."""
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from django.test import SimpleTestCase, override_settings
from roo.services import PointsPurchaseService
from roo.startup_checkout_return import article_return_path


@override_settings(COMMUNITY_CHAT_FRONTEND_URL='https://chat.mlai.test', DEFAULT_FRONTEND_URL='https://mlai.test')
class StartupCheckoutReturnTests(SimpleTestCase):
    def setUp(self):
        self.target = f'/my-startup/articles?company_id={uuid4()}&articlePlan={uuid4()}'
        self.purchase = SimpleNamespace(id=uuid4(), purchase_from={'surface': 'my-startup', 'return_path': self.target})

    def test_browser_and_native_return_keep_same_plan_after_success_or_cancel(self):
        for runtime in ('browser', 'tauri'):
            self.purchase.purchase_from['return_runtime'] = runtime
            for outcome in ('success', 'cancelled'):
                result = urlsplit(PointsPurchaseService.stripe_return_url(self.purchase, outcome))
                self.assertEqual(result.netloc, 'chat.mlai.test')
                self.assertEqual(result.path, f'/my-startup/credits/{self.purchase.id}')
                self.assertEqual(parse_qs(result.query), {'returnTo': [self.target], 'returnRuntime': [runtime], 'checkout': [outcome]})

    def test_external_duplicate_extra_or_malformed_targets_cannot_be_checkout_redirects(self):
        for target in ('https://evil.test' + self.target, '//evil.test/my-startup/articles', self.target + '&next=https://evil.test',
                       self.target + '&company_id=' + str(uuid4()), self.target + '#fragment', '/my-startup/settings',
                       '/my-startup/articles?company_id=1&articlePlan=2', '/my-startup/articles?company_id=%0a&articlePlan=x'):
            self.assertIsNone(article_return_path(target), target)
            self.purchase.purchase_from['return_path'] = target
            result = PointsPurchaseService.stripe_return_url(self.purchase, 'success')
            self.assertEqual(parse_qs(urlsplit(result).query), {'checkout': ['success']})

    def test_ordinary_purchase_url_is_unchanged(self):
        self.purchase.purchase_from = {'source': 'web'}
        self.assertEqual(PointsPurchaseService.stripe_return_url(self.purchase, 'success'),
            f'https://mlai.test/roo/topup/{self.purchase.id}?checkout=success')
