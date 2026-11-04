"""Old standalone setup entrypoints expose the actionable reviewed-setup hold."""
from unittest.mock import patch
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIRequestFactory
from workflow_runs.models import ContentFactoryRun
from .models import ContentFactoryJob
from .self_heal_testing import make_p2_org


@override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=True, ROO_API_KEY="synthetic-setup-boundary-key")
class StandaloneSetupBoundaryTests(TransactionTestCase):
    def setUp(self):
        fixture = make_p2_org()
        self.org, self.user, self.config = fixture.organization, fixture.owner, fixture.config
        self.config.github_repo, self.config.scan_summary = "synthetic/site", "Retained actual scan"
        self.config.save(update_fields=["github_repo", "scan_summary"])

    def test_both_actual_http_callers_hold_before_credentials_provider_job_or_background_thread(self):
        from integrations.api_views import GithubScaffoldView
        from .content_views import ArticleSystemDecisionView
        from integrations.services.github import standalone_reviewed_setup_required
        for view, extra in ((GithubScaffoldView, {}), (ArticleSystemDecisionView, {"decision": "scaffold"})):
            with self.subTest(view=view.__name__), \
                    patch("integrations.services.article_generation.get_github_credentials_for_domain") as credentials, \
                    patch("integrations.services.github.scaffold_articles_directory") as service, \
                    patch("threading.Thread") as thread:
                request = APIRequestFactory().post("/standalone-setup", {"domain": self.org.domain,
                    "slack_user_id": f"mlai_user:{self.user.pk}", **extra}, format="json",
                    HTTP_X_API_KEY="synthetic-setup-boundary-key")
                response = view.as_view()(request)
                self.assertEqual(response.status_code, 409, response.data)
                self.assertEqual(response.data, standalone_reviewed_setup_required())
                credentials.assert_not_called()
                service.assert_not_called()
                thread.assert_not_called()
        self.assertFalse(ContentFactoryRun.objects.exists())
        self.assertFalse(ContentFactoryJob.objects.exists())
