"""Article history survives removal of the separate blog-hosting models."""
from django.test import SimpleTestCase
from .run_ownership import public_run_value, HOSTED_PUBLICATION_OBSERVATION_KEY

class RetiredBlogHistoryTests(SimpleTestCase):
    def test_backend_only_historical_provider_observations_remain_private(self):
        history={HOSTED_PUBLICATION_OBSERVATION_KEY:{'status':'live'},'article_title':'Saved draft'}
        self.assertEqual(public_run_value(history), {'article_title':'Saved draft'})
