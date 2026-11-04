"""The old Slack scaffold command cannot invent a current typed setup recipe."""
from copy import deepcopy
from unittest.mock import Mock, patch
from django.test import TransactionTestCase, override_settings
from .self_heal_testing import make_p2_org
from .models import ContentFactoryJob
from workflow_runs.models import ContentFactoryRun
from roo.models import Ledger
from integrations.services.github import scaffold_articles_directory, standalone_reviewed_setup_required


class StandaloneSetupBoundaryTests(TransactionTestCase):
    def setUp(self):
        fixture = make_p2_org()
        self.org, self.user, self.config = fixture.organization, fixture.owner, fixture.config
        ContentFactoryJob.objects.bulk_create([ContentFactoryJob(job_id=f"historical-job-{index}", domain=self.org.domain,
            slack_user_id="legacy", status="completed", request_meta={"historical": index}) for index in range(254)])
        self.before = list(ContentFactoryJob.objects.order_by("pk").values())

    @override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=True)
    def test_on_refuses_missing_review_surface_and_spend_before_any_http_or_history_write(self):
        with patch("integrations.services.github.http_requests.post") as post:
            outcome = scaffold_articles_directory(domain=self.org.domain, slack_user_id=f"mlai_user:{self.user.pk}",
                github_token="synthetic-unused-token", github_repo="synthetic/site")
        self.assertEqual(outcome, standalone_reviewed_setup_required())
        self.assertEqual(outcome["next_action"], "review_website_setup")
        post.assert_not_called()
        self.assertEqual(list(ContentFactoryJob.objects.order_by("pk").values()), self.before)
        self.assertEqual(ContentFactoryRun.objects.count(), 0)
        self.assertEqual(Ledger.objects.count(), 0)

    @override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False)
    def test_off_keeps_original_endpoint_body_result_and_254_historical_jobs(self):
        binding = {"website_connection_id": "2c72f72b-5478-4c6a-b8ad-77acbcfbb7ea", "connection_generation": 1,
            "repository_id": 42, "expected_source_sha": "a" * 40}
        response = Mock(status_code=202)
        response.json.return_value = {"job_id": "new-legacy-job", "status": "queued"}
        from contextlib import nullcontext
        with patch("content_factory.website_connections.dispatch_contract", return_value=binding), \
                patch("content_factory.vibe_marketing_views._remote_response_write_guard", return_value=nullcontext()), \
                patch("integrations.services.github.http_requests.post", return_value=response) as post:
            result = scaffold_articles_directory(domain=self.org.domain, slack_user_id="legacy-requester",
                github_token="synthetic-unused-token", github_repo="synthetic/site")
        self.assertEqual(result, {"job_id": "new-legacy-job", "status": "queued"})
        self.assertTrue(post.call_args.args[0].endswith("/api/runs/scaffold"))
        self.assertEqual(post.call_args.kwargs["json"], {"domain": self.org.domain, "slack_user_id": "legacy-requester",
            "github_repo": "synthetic/site", **binding})
        self.assertEqual(list(ContentFactoryJob.objects.exclude(job_id="new-legacy-job").order_by("pk").values()), self.before)
        self.assertEqual(ContentFactoryJob.objects.count(), 255)
        self.assertEqual(ContentFactoryRun.objects.count(), 0)
