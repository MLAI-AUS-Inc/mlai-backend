import json
import os
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status
from django.urls import reverse
from requests import Response

from content_factory.delivery import build_content_factory_preview_signature
from content_factory.models import (
    ContentFactoryJob,
    OrganizationContentConfig,
    ResearchedKeyword,
    ScheduledDiscoveryDispatch,
    ScheduledDiscoveryDispatchState,
    TopicFeedback,
    WrittenArticle,
)
from organizations.models import Organization
from workflow_runs.models import (
    ContentFactoryApprovalState,
    ContentFactoryRun,
    ContentFactoryRunStatus,
    ContentFactoryRunStep,
    ContentFactoryStepStatus,
)
from integrations.models import UserIntegration
from roo.models import ChannelFirstPost, PointsAccount

User = get_user_model()

class ContentFactoryTestDataMixin:
    def _sample_content_package(self, *, long_section: bool = False, include_images: bool = True):
        long_paragraph = " ".join(["Reliable livestock weight data improves management decisions."] * 80)
        section_paragraphs = [
            "Accurate weighing helps track growth and make calmer handling decisions on farm.",
            long_paragraph if long_section else "Choose equipment that matches your yards, herd size, and handling flow.",
        ]
        hero_image = {
            "kind": "hero",
            "status": "generated",
            "url": "https://cdn.example.com/hero.jpg" if include_images else "",
            "alt_text": "Cattle scales in a livestock yard",
            "caption": "Reliable livestock weighing starts with the right setup.",
        }
        inline_images = [
            {
                "kind": "inline",
                "status": "generated",
                "url": "https://cdn.example.com/inline-1.jpg" if include_images else "",
                "alt_text": "Livestock scale beside cattle yards",
                "caption": "Compare layout and flow before choosing a system.",
                "section_id": "types-of-scales",
                "section_heading": "Common Types of Cattle Scales",
            }
        ]
        references = [
            {"source_id": "src-1", "title": "Farmstyle livestock scales guide", "url": "https://example.com/farmstyle"},
            {"source_id": "src-2", "title": "Gallagher auto weigher", "url": "https://example.com/gallagher"},
        ]
        article_json = {
            "title": "How to Choose Cattle Scales for Accurate Livestock Weighing",
            "meta_title": "How to Choose Cattle Scales for Accurate Livestock Weighing",
            "meta_description": "Compare cattle scales, weigh bars, and auto weighers to choose a setup that suits your farm.",
            "slug": "how-to-choose-cattle-scales-for-accurate-livestock-weighing",
            "category": "featured",
            "target_keyword": "cattle scales",
            "summary_items": [
                {"label": "Why weigh", "description": "Measured data beats estimates when tracking herd performance."},
                {"label": "What to compare", "description": "Capacity, build quality, flow, and record-keeping matter most."},
            ],
            "sections": [
                {
                    "section_id": "types-of-scales",
                    "heading": "Common Types of Cattle Scales",
                    "section_type": "prose",
                    "paragraphs": section_paragraphs,
                    "bullets": ["Weigh bars fit existing handling equipment.", "Auto systems reduce manual handling."],
                    "subsections": [
                        {
                            "heading": "When weigh bars make sense",
                            "paragraphs": ["They work well when you already have a crush or fixed weighing point."],
                            "bullets": [],
                        }
                    ],
                },
                {
                    "section_id": "accuracy-and-installation",
                    "heading": "Best Practices for Installation and Accuracy",
                    "section_type": "prose",
                    "paragraphs": ["A firm, level base helps the scale read consistently over time."],
                    "bullets": ["Keep debris away from the weighing points.", "Check calibration as part of maintenance."],
                    "subsections": [],
                },
            ],
            "faq_items": [
                {"question": "What is the difference between weigh bars and auto weighers?", "answer": "Auto systems reduce manual handling, while weigh bars suit existing setups."},
                {"question": "How often should cattle scales be checked?", "answer": "Check regularly and after moving equipment between sites."},
            ],
            "cta": {
                "title": "Compare your weighing setup before you buy",
                "body": "Match scale type, layout, and herd needs before choosing a system.",
                "button_text": "Review scale options",
                "button_href": "https://livestockmerchant.com.au",
            },
            "references": references,
        }
        return {
            "title": article_json["title"],
            "slug": article_json["slug"],
            "category": article_json["category"],
            "target_keyword": article_json["target_keyword"],
            "meta_title": article_json["meta_title"],
            "meta_description": article_json["meta_description"],
            "article_html": "<article><h1>How to Choose Cattle Scales for Accurate Livestock Weighing</h1><p>Compare cattle scales, weigh bars, and auto weighers to choose a setup that suits your farm.</p></article>",
            "article_markdown": "# How to Choose Cattle Scales for Accurate Livestock Weighing\n\nCompare cattle scales, weigh bars, and auto weighers.",
            "article_json": article_json,
            "hero_image": hero_image,
            "inline_images": inline_images,
            "references": references,
        }

    def _create_content_factory_run(self, run_id: str, *, content_package=None, domain: str = "mlai.au"):
        return ContentFactoryRun.objects.create(
            run_id=run_id,
            workflow="direct_generate",
            domain=domain,
            status="completed",
            current_step="finalize",
            artifact_root=f"/tmp/content-factory-runs/{run_id}",
            result={"status": "success", "content_package": content_package or self._sample_content_package()},
            run_request={"domain": domain},
            acceptance_summary={"content_packaged": True},
            verification_summary={"all_required_passed": True},
        )


class EndpointTests(ContentFactoryTestDataMixin, TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "test_api_key"
        os.environ['ROO_API_KEY'] = self.api_key
        os.environ['INTERNAL_API_KEY'] = self.api_key
        from django.conf import settings
        settings.INTERNAL_API_KEY = self.api_key
        settings.ROO_API_KEY = self.api_key
        self.client.credentials(HTTP_X_API_KEY=self.api_key)
        
        # Create a test user
        self.user = User.objects.create_user(email="test@example.com", password="password")

    def test_integrations_endpoints(self):
        # 1. Create Token
        url = reverse('github_integration_list')
        data = {
            "slack_user_id": "U123",
            "token": "gh_token",
            "user_name": "gh_user",
            "scopes": ["repo"]
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(UserIntegration.objects.filter(slack_user_id="U123").exists())

        # 2. Get Integration
        url_detail = reverse('github_integration_detail', args=["U123"])
        response = self.client.get(url_detail)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['token'], "gh_token")

        # 3. Patch Integration
        response = self.client.patch(url_detail, {"project_scanned": True}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(UserIntegration.objects.get(slack_user_id="U123").project_scanned)

    def test_pending_intent_endpoints(self):
        # 1. Save Intent
        url = reverse('pending_intent_list')
        data = {"slack_user_id": "U456", "intent": "some intent"}
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(UserIntegration.objects.get(slack_user_id="U456").pending_intent, "some intent")

        # 2. Clear Intent
        url_clear = reverse('pending_intent_detail', args=["U456"])
        response = self.client.delete(url_clear)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(UserIntegration.objects.get(slack_user_id="U456").pending_intent)

    def test_pending_intent_endpoint_accepts_legacy_intent_data_payload(self):
        url = reverse('pending_intent_list')
        data = {
            "slack_user_id": "U789",
            "intent_data": json.dumps({"type": "write_article", "article_request": {"domain": "mlai.au"}}),
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            UserIntegration.objects.get(slack_user_id="U789").pending_intent,
            {"type": "write_article", "article_request": {"domain": "mlai.au"}},
        )

    def test_content_factory_token_prefers_github_app_installation_token(self):
        from integrations.services.github_app import GitHubInstallationToken

        organization = Organization.objects.create(name="Acme", domain="acme.com")
        OrganizationContentConfig.objects.create(
            organization=organization,
            github_repo="acme/site",
            github_token_encrypted="legacy-user-token",
            github_installation_id="12345",
        )
        app_token = GitHubInstallationToken(
            token="ghs_installation",
            expires_at=timezone.now() + timedelta(minutes=50),
            installation_id="12345",
            repository="acme/site",
            permissions={"contents": "write", "pull_requests": "write"},
        )

        with patch("integrations.services.github_app.github_app_credentials_configured", return_value=True), patch(
            "integrations.services.github_app.create_installation_access_token",
            return_value=app_token,
        ) as create_token:
            response = self.client.get(
                reverse("content_factory_token"),
                {"domain": "acme.com", "github_repo": "acme/site"},
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["github_token"], "ghs_installation")
        self.assertEqual(response.data["github_repo"], "acme/site")
        self.assertEqual(response.data["github_installation_id"], "12345")
        self.assertEqual(response.data["token_source"], "github_app_installation")
        self.assertEqual(response.data["github_permissions"], {"contents": "write", "pull_requests": "write"})
        create_token.assert_called_once_with(
            installation_id="12345",
            repository="acme/site",
            permission_mode="write",
        )

    def test_github_app_installation_token_cache_preserves_permissions(self):
        from django.core.cache import cache

        from integrations.services.github_app import create_installation_access_token

        cache.clear()
        expires_at = (timezone.now() + timedelta(minutes=50)).isoformat()
        response = MagicMock(status_code=201)
        response.json.return_value = {
            "token": "ghs_installation",
            "expires_at": expires_at,
            "permissions": {"contents": "write", "pull_requests": "write"},
        }

        with patch("integrations.services.github_app._github_app_jwt", return_value="jwt"), patch(
            "integrations.services.github_app.http_requests.post",
            return_value=response,
        ) as post:
            first = create_installation_access_token(
                installation_id="12345",
                repository="acme/site",
                permission_mode="write",
            )
            second = create_installation_access_token(
                installation_id="12345",
                repository="acme/site",
                permission_mode="write",
            )

        self.assertEqual(first.permissions, {"contents": "write", "pull_requests": "write"})
        self.assertEqual(second.permissions, {"contents": "write", "pull_requests": "write"})
        self.assertEqual(second.as_content_factory_payload()["github_permissions"], {"contents": "write", "pull_requests": "write"})
        post.assert_called_once()
        cache.clear()

    def test_content_factory_token_blocks_when_installation_app_credentials_missing(self):
        organization = Organization.objects.create(name="Acme", domain="acme.com")
        OrganizationContentConfig.objects.create(
            organization=organization,
            github_repo="acme/site",
            github_token_encrypted="legacy-user-token",
            github_installation_id="12345",
        )

        with patch("integrations.services.github_app.github_app_credentials_configured", return_value=False):
            response = self.client.get(reverse("content_factory_token"), {"domain": "acme.com"})

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data["action_required"], "server_configuration_required")
        self.assertEqual(response.data["github_installation_id"], "12345")
        self.assertNotIn("legacy-user-token", str(response.data))

    @patch('integrations.services.github.http_requests.post')
    def test_scaffold_decision_endpoint_queues_scaffold_job(self, mock_post):
        ContentFactoryJob.objects.create(
            job_id="scan-run-approval-1",
            domain="mlai.au",
            slack_user_id="U123",
            status="awaiting_confirmation",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            request_meta={"type": "scan"},
        )

        mock_response = MagicMock()
        mock_response.status_code = status.HTTP_200_OK
        mock_response.content = b'{"status":"queued"}'
        mock_response.json.return_value = {
            "status": "queued",
            "job_id": "scan-run-approval-1",
            "workflow": "repo_scan",
            "scaffold_job_id": "scaffold-job-99",
            "message": "Scaffold approval recorded and scaffold PR creation queued.",
        }
        mock_post.return_value = mock_response

        response = self.client.post(
            reverse('github_scaffold_decision'),
            {
                "scan_run_id": "scan-run-approval-1",
                "decision": "approve",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "slack_channel_id": "C123",
                "slack_thread_ts": "123.456",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["scaffold_job_id"], "scaffold-job-99")

        parent_job = ContentFactoryJob.objects.get(job_id="scan-run-approval-1")
        self.assertEqual(parent_job.status, "confirmed")
        self.assertEqual(parent_job.request_meta["scaffold_decision"], "approve")

        scaffold_job = ContentFactoryJob.objects.get(job_id="scaffold-job-99")
        self.assertEqual(scaffold_job.domain, "mlai.au")
        self.assertEqual(scaffold_job.status, "queued")
        self.assertEqual(scaffold_job.slack_channel_id, "C123")
        self.assertEqual(scaffold_job.slack_thread_ts, "123.456")
        self.assertEqual(scaffold_job.request_meta["parent_scan_run_id"], "scan-run-approval-1")

        args, _kwargs = mock_post.call_args
        self.assertIn('/api/runs/scan-run-approval-1/approve', args[0])

    def test_github_scaffold_returns_existing_preview_when_already_scaffolded(self):
        organization = Organization.objects.create(name="Bird Psychology", domain="birdpsychology.com.au")
        OrganizationContentConfig.objects.create(
            organization=organization,
            scan_summary="scan complete",
            articles_scaffolded=True,
            articles_scaffold_pr_url="https://github.com/acme/site/pull/1",
            articles_scaffold_preview_url="https://preview.example.com/articles",
        )

        response = self.client.post(
            reverse('github_scaffold'),
            {
                "domain": "birdpsychology.com.au",
                "slack_user_id": "U123",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "already_scaffolded")
        self.assertEqual(response.data["pr_url"], "https://github.com/acme/site/pull/1")
        self.assertEqual(response.data["preview_url"], "https://preview.example.com/articles")

    def test_channel_activity_endpoints(self):
        # Setup: Link user to Slack ID
        self.user.slack_id = "U789"
        self.user.save()

        # 1. Record First Post
        url = reverse('first_post_record')
        data = {"slack_user_id": "U789", "channel_id": "C123"}
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(ChannelFirstPost.objects.filter(slack_user_id="U789", channel_id="C123").exists())
        self.assertTrue(response.data['points_awarded'])

        # 2. Duplicate Check
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

        # 3. Check Status
        url_check = reverse('first_post_check', args=["U789", "C123"])
        response = self.client.get(url_check)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['has_posted'])

    def test_channel_activity_allows_roo_api_key_when_internal_differs(self):
        """
        Roo should be able to call channel activity endpoints even if
        INTERNAL_API_KEY is different from ROO_API_KEY.
        """
        from django.conf import settings

        os.environ['ROO_API_KEY'] = 'roo_only_key'
        os.environ['INTERNAL_API_KEY'] = 'internal_only_key'
        settings.ROO_API_KEY = 'roo_only_key'
        settings.INTERNAL_API_KEY = 'internal_only_key'

        client = APIClient()
        client.credentials(HTTP_X_API_KEY='roo_only_key')

        url_check = reverse('first_post_check', args=["U000", "C000"])
        response = client.get(url_check)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['has_posted'])

    def test_user_linking_endpoint(self):
        # 1. Link Valid User
        url = reverse('link_slack')
        data = {"slack_id": "U999", "email": "test@example.com"}
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.slack_id, "U999")

        # 2. Invalid User
        data = {"slack_id": "U888", "email": "notfound@example.com"}
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ContentGenerateAutoWriteTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "test_roo_key"
        os.environ['ROO_API_KEY'] = self.api_key
        os.environ['INTERNAL_API_KEY'] = "test_internal_key"
        os.environ['MLAI_API_KEY'] = "test_mlai_key"

        from django.conf import settings
        settings.ROO_API_KEY = self.api_key
        settings.INTERNAL_API_KEY = "test_internal_key"
        settings.MLAI_API_KEY = "test_mlai_key"
        settings.CONTENT_FACTORY_URL = "http://content-factory.test"

        self.client.credentials(HTTP_X_API_KEY=self.api_key)
        self.user_email = "auto@example.com"
        self.user = User.objects.create_user(
            email=self.user_email,
            password="password",
            slack_id="U-AUTO",
        )
        PointsAccount.objects.create(user=self.user, balance=20)

        self.integration = UserIntegration.objects.create(
            slack_user_id="U-AUTO",
            github_access_token="gh_token",
            github_repo="owner/repo",
        )
        self.organization = Organization.objects.create(
            name="MLAI",
            domain="mlai.au",
        )
        OrganizationContentConfig.objects.create(
            organization=self.organization,
            github_repo="owner/repo",
            github_token_encrypted="org-token",
            github_token_expires_at=timezone.now() + timedelta(hours=1),
            scan_summary="scan complete",
        )

    def _generate_request_data(self, **overrides):
        data = {
            "slack_user_id": self.integration.slack_user_id,
            "domain": self.organization.domain,
            "request_source": "roo_slackbot",
            "client_request_id": "content-factory-test-request",
            "user_email": self.user_email,
            "user_first_name": "Auto",
            "user_last_name": "Writer",
            "user_avatar_url": "https://avatar.test/auto.png",
        }
        data.update(overrides)
        return data

    def _mock_response(self, status_code, body):
        response = Response()
        response.status_code = status_code
        response._content = json.dumps(body).encode()
        response.headers['Content-Type'] = 'application/json'
        return response

    def test_generate_auto_write_uses_discovery_endpoint_after_scan(self):
        url = reverse('content_generate')
        data = self._generate_request_data(topic=None)

        with patch('integrations.services.article_generation.http_requests.post') as mock_post:
            mock_post.side_effect = [
                self._mock_response(202, {"job_id": "job-123"}),  # generate
            ]

            response = self.client.post(url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data['job_id'], "job-123")
        self.assertEqual(response.data['workflow'], "auto_discovery")
        
        # Verify call count (should be 1 call to discovery)
        self.assertEqual(mock_post.call_count, 1)

        generate_call = mock_post.call_args_list[0]
        self.assertIn("/api/runs/discovery", generate_call.args[0])

        generate_payload = generate_call.kwargs.get('json') or {}
        self.assertEqual(generate_payload.get('domain'), self.organization.domain)
        self.assertEqual(generate_payload.get('slack_user_id'), self.integration.slack_user_id)
        self.assertEqual(generate_payload.get('request_source'), 'roo_slackbot')

    def test_generate_with_topic_omits_delivery_mode_when_no_preference_exists(self):
        url = reverse('content_generate')
        data = self._generate_request_data(topic="best ai coding agents")

        with patch('integrations.services.article_generation.http_requests.post') as mock_post:
            mock_post.return_value = self._mock_response(202, {"job_id": "job-article-456"})
            response = self.client.post(url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        payload = mock_post.call_args.kwargs.get('json') or {}
        self.assertIsNone(payload.get('delivery_mode'))
        self.assertIsNone(payload.get('delivery_mode_confirmed'))

    def test_generate_with_topic_allows_repo_less_domain_without_scan_summary(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.github_repo = ""
        config.scan_summary = None
        config.article_system = {}
        config.article_delivery_mode = "content_only"
        config.save(
            update_fields=[
                "github_repo",
                "scan_summary",
                "article_system",
                "article_delivery_mode",
            ]
        )

        url = reverse('content_generate')
        data = self._generate_request_data(topic="best ai coding agents")

        with patch('integrations.services.article_generation.http_requests.post') as mock_post:
            mock_post.return_value = self._mock_response(202, {"job_id": "job-article-repo-less-456"})
            response = self.client.post(url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        payload = mock_post.call_args.kwargs.get('json') or {}
        self.assertEqual(payload.get('delivery_mode'), 'content_only')
        self.assertFalse(payload.get('delivery_mode_confirmed'))
        self.assertIsNone(payload.get('github_repo'))

    @patch('content_factory.content_views.trigger_article_generation')
    def test_generate_passes_slack_thread_context_to_service(self, mock_trigger):
        mock_trigger.return_value = {"job_id": "job-thread", "status": "queued"}

        url = reverse('content_generate')
        data = self._generate_request_data(
            topic="best ai coding agents",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
        )

        response = self.client.post(url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_trigger.assert_called_once_with(
            self.integration.slack_user_id,
            {
                "domain": self.organization.domain,
                "topic": "best ai coding agents",
                "target_keyword": None,
                "context": None,
                "delivery_mode": None,
                "delivery_mode_confirmed": None,
                "slack_channel_id": "C123",
                "slack_thread_ts": "123.456",
                "slack_root_message_ts": "123.456",
                "progress_message_ts": None,
                "request_source": "roo_slackbot",
                "client_request_id": "content-factory-test-request",
                "user_email": self.user_email,
                "user_first_name": "Auto",
                "user_last_name": "Writer",
                "user_avatar_url": "https://avatar.test/auto.png",
            },
        )

    @patch('integrations.services.article_generation.get_github_credentials_for_domain')
    @patch('integrations.services.article_generation.http_requests.post')
    def test_generate_with_topic_uses_scan_summary_article_system_fallback(self, mock_post, mock_get_credentials):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.scan_summary = {
            "articles_status": {
                "has_articles_system": True,
                "directory_name": "articles",
                "directory_path": "app/articles/content",
                "detected_type": "tsx",
            }
        }
        config.save(update_fields=['scan_summary'])

        mock_get_credentials.return_value = {
            "token": "gh_token",
            "repo": "owner/repo",
            "source": "org",
        }
        mock_response = self._mock_response(202, {"job_id": "job-article-123"})
        mock_post.return_value = mock_response

        url = reverse('content_generate')
        data = self._generate_request_data(topic="best ai coding agents")

        response = self.client.post(url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data['job_id'], 'job-article-123')

    def test_generate_rejects_missing_request_source(self):
        response = self.client.post(
            reverse('content_generate'),
            {
                "slack_user_id": self.integration.slack_user_id,
                "domain": self.organization.domain,
                "client_request_id": "content-factory-test-request",
                "user_email": self.user_email,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_generate_rejects_non_roo_key(self):
        other_client = APIClient()
        other_client.credentials(HTTP_X_API_KEY="test_internal_key")

        response = other_client.post(
            reverse('content_generate'),
            self._generate_request_data(),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ArticleSystemDecisionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "test_roo_key"
        os.environ['ROO_API_KEY'] = self.api_key
        os.environ['INTERNAL_API_KEY'] = self.api_key

        from django.conf import settings
        settings.ROO_API_KEY = self.api_key
        settings.INTERNAL_API_KEY = self.api_key

        self.client.credentials(HTTP_X_API_KEY=self.api_key)
        self.integration = UserIntegration.objects.create(
            slack_user_id="U-DECIDE",
            pending_intent={
                "type": "write_article",
                "article_request": {
                    "domain": "mlai.au",
                    "topic": "best ai coding agents",
                },
            },
        )
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        self.config = OrganizationContentConfig.objects.create(
            organization=self.organization,
            scan_summary="scan complete",
            article_system={
                "state": "ambiguous",
                "directory_name": "articles",
                "directory_path": "app/articles/content",
                "confidence": "low",
                "reason": "Possible article directory detected",
                "source": "scan",
                "verified_at": "2026-03-08T00:00:00+00:00",
            },
        )

    @patch('content_factory.content_views.trigger_article_generation')
    def test_article_system_decision_use_detected_resumes_pending_intent(self, mock_trigger):
        mock_trigger.return_value = {"job_id": "job-123", "status": "queued"}
        url = reverse('content_article_system_decision')

        response = self.client.post(
            url,
            {
                "domain": "mlai.au",
                "slack_user_id": "U-DECIDE",
                "decision": "use_detected",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.config.refresh_from_db()
        self.integration.refresh_from_db()
        self.assertEqual(self.config.article_system['state'], 'existing')
        self.assertEqual(self.config.article_system['source'], 'manual_confirmed')
        self.assertIsNone(self.integration.pending_intent)
        self.assertTrue(response.data['resume_triggered'])
        mock_trigger.assert_called_once()


class ContentFactoryCallbackTests(ContentFactoryTestDataMixin, TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "test_roo_key"
        os.environ['ROO_API_KEY'] = self.api_key
        from django.conf import settings
        settings.ROO_API_KEY = self.api_key
        self.client.credentials(HTTP_X_API_KEY=self.api_key)

    @patch("integrations.services.github.refresh_github_token", side_effect=Exception("no stored token"))
    @patch("content_factory.service_views.ContentFactoryCallbackView._send_auth_required_notification")
    def test_auth_required_callback_updates_job_without_import_error(self, mock_notify, mock_refresh):
        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "auth_required",
                "job_id": "setup-run-auth-required",
                "domain": "studynash.co",
                "slack_user_id": "U123",
                "github_repo": "drsamdonegan/studynash",
                "workflow": "article_system_setup",
                "message": "Access denied to repository drsamdonegan/studynash",
                "reason_code": "missing_or_expired_credentials",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="setup-run-auth-required")
        self.assertEqual(job.status, "auth_required")
        self.assertEqual(job.domain, "studynash.co")
        self.assertEqual(job.slack_user_id, "U123")
        self.assertEqual(job.error_message, "Access denied to repository drsamdonegan/studynash")
        mock_refresh.assert_called_once_with("U123")
        mock_notify.assert_called_once()

    @patch('integrations.services.slack.SlackService.send_message')
    @patch('integrations.services.slack.SlackService.get_channel_id_by_name')
    @patch('integrations.services.slack.SlackService.send_dm')
    def test_topic_selection_callback_posts_scheduled_daily_to_shared_channel(
        self,
        mock_send_dm,
        mock_get_channel_id,
        mock_send_message,
    ):
        mock_get_channel_id.return_value = "C-VIBE"
        mock_send_message.return_value = (True, "171234.567")
        organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        OrganizationContentConfig.objects.create(
            organization=organization,
            connected_slack_user_id="U123",
            scan_summary="scan ready",
        )
        ScheduledDiscoveryDispatch.objects.create(
            slack_user_id="U123",
            domain="mlai.au",
            timezone="Australia/Melbourne",
            local_date=timezone.now().date(),
            state=ScheduledDiscoveryDispatchState.QUEUED,
            content_factory_job_id="job-123",
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "topic_selection",
                "job_id": "job-123",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "selection": {
                    "selected_keyword": "ai agents",
                    "selection_reason": "High volume",
                    "options": [
                        {
                            "keyword": "ai agents",
                            "volume": 2400,
                            "difficulty": 35,
                            "opportunity_index": 85.2,
                        }
                    ],
                },
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(ContentFactoryJob.objects.filter(job_id="job-123").exists())

        job = ContentFactoryJob.objects.get(job_id="job-123")
        self.assertEqual(job.status, 'awaiting_confirmation')
        self.assertEqual(job.selected_keyword, "ai agents")
        self.assertEqual(job.slack_user_id, "U123")
        self.assertEqual(job.billing_status, "deferred")
        self.assertEqual(job.request_meta["trigger_source"], "scheduled_daily")
        self.assertEqual(job.slack_channel_id, "C-VIBE")
        self.assertEqual(job.slack_root_message_ts, "171234.567")
        self.assertEqual(job.slack_thread_ts, "171234.567")
        dispatch = ScheduledDiscoveryDispatch.objects.get(content_factory_job_id="job-123")
        self.assertEqual(dispatch.state, ScheduledDiscoveryDispatchState.TOPIC_SELECTION_SENT)
        self.assertEqual(dispatch.slack_channel_id, "C-VIBE")
        self.assertEqual(dispatch.slack_message_ts, "171234.567")
        self.assertEqual(dispatch.slack_thread_ts, "171234.567")

        mock_get_channel_id.assert_called_once_with("vibe-marketing")
        mock_send_message.assert_called_once()
        self.assertEqual(mock_send_message.call_args[0][0], "C-VIBE")
        self.assertNotIn("thread_ts", mock_send_message.call_args[1])
        blocks = mock_send_message.call_args[1]["blocks"]
        self.assertIn("<@U123> your scheduled research for *mlai.au* is ready.", blocks[1]["text"]["text"])
        mock_send_dm.assert_not_called()

    @patch('integrations.services.slack.SlackService.send_message')
    @patch('integrations.services.slack.SlackService.send_dm')
    def test_topic_selection_callback_posts_to_thread_when_context_exists(self, mock_send_dm, mock_send_message):
        mock_send_message.return_value = (True, "live-topic-card")
        ContentFactoryJob.objects.create(
            job_id="job-topic-thread",
            domain="mlai.au",
            slack_user_id="U123",
            status="researching",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "topic_selection",
                "job_id": "job-topic-thread",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "selection": {
                    "selected_keyword": "ai agents",
                    "options": [
                        {"keyword": "option 1", "volume": 1000},
                        {"keyword": "option 2", "volume": 800},
                    ],
                },
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(mock_send_message.call_count, 2)
        self.assertEqual(mock_send_message.call_args_list[0][0][0], "C123")
        self.assertEqual(mock_send_message.call_args_list[0][1]["thread_ts"], "123.456")
        self.assertIn(
            "Research complete. Choose one of the topic options below to continue.",
            mock_send_message.call_args_list[0][1]["blocks"][0]["text"]["text"],
        )
        self.assertEqual(mock_send_message.call_args_list[1][0][0], "C123")
        self.assertEqual(mock_send_message.call_args_list[1][1]["thread_ts"], "123.456")
        mock_send_dm.assert_not_called()

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_topic_selection_multi_options(self, mock_send_dm):
        url = reverse('content_factory_callback')
        data = {
            "event_type": "topic_selection",
            "job_id": "job-multi",
            "domain": "mlai.au",
            "slack_user_id": "U123",
            "selection": {
                "selected_keyword": "top pick",
                "options": [
                    {"keyword": "option 1", "volume": 1000},
                    {"keyword": "option 2", "volume": 2000},
                ]
            }
        }
        
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Verify job stores options
        job = ContentFactoryJob.objects.get(job_id="job-multi")
        self.assertEqual(len(job.selection_data['options']), 2)
        
        # Verify Slack message contains buttons for options
        # We need to find the blocks argument. It might be in kwargs 'blocks' or positional arg
        call_args = mock_send_dm.call_args
        blocks = call_args[1].get('blocks')
        if not blocks and len(call_args[0]) > 2:
            blocks = call_args[0][2]
        
        # Check action elements
        found_actions = False
        if blocks:
            for block in blocks:
                if block['type'] == 'actions':
                    found_actions = True
                    elements = block['elements']
                    # Should have 2 options + 1 cancel
                    self.assertEqual(len(elements), 3)
                    self.assertEqual(elements[0]['action_id'], "confirm_topic_btn_0")
                    self.assertEqual(elements[1]['action_id'], "confirm_topic_btn_1")
                    self.assertEqual(elements[2]['action_id'], "cancel_topic_btn")
        self.assertTrue(found_actions, "Action block not found in Slack message")

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_article_complete_callback(self, mock_send_dm):
        # Create job first
        ContentFactoryJob.objects.create(job_id="job-456", domain="mlai.au", status="generating")
        
        url = reverse('content_factory_callback')
        data = {
            "event_type": "article_complete",
            "job_id": "job-456",
            "domain": "mlai.au",
            "slack_user_id": "U123",
            "article_url": "https://mlai.au/article",
            "pr_url": "https://github.com/pr/1",
        }
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-456")
        self.assertEqual(job.status, 'completed')
        self.assertEqual(job.pr_url, "https://github.com/pr/1")
        
        mock_send_dm.assert_called_once()
        self.assertEqual(mock_send_dm.call_args[0][0], "U123")

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_scan_complete_callback_mentions_scaffold_when_already_queued(self, mock_send_dm):
        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "scan_complete",
                "job_id": "scan-run-queued",
                "run_id": "scan-run-queued",
                "workflow": "repo_scan",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "components_generated": True,
                "components_count": 3,
                "component_names": ["ArticleHeroHeader", "ArticleFAQ", "ArticleFooterNav"],
                "scaffold_queued": True,
                "scaffold_job_id": "scaffold-run-1",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_send_dm.assert_called_once()
        message = mock_send_dm.call_args[0][1]
        self.assertIn("queued article-directory setup", message)
        self.assertNotIn("Create Articles Directory", message)
        self.assertIsNone(mock_send_dm.call_args[1].get("blocks"))

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_scan_complete_callback_posts_scaffold_approval_buttons(self, mock_send_dm):
        ContentFactoryJob.objects.create(
            job_id="scan-run-awaiting-approval",
            domain="mlai.au",
            slack_user_id="U123",
            status="researching",
            request_meta={"type": "scan"},
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "scan_complete",
                "job_id": "scan-run-awaiting-approval",
                "run_id": "scan-run-awaiting-approval",
                "workflow": "repo_scan",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "components_generated": True,
                "components_count": 3,
                "component_names": ["ArticleHeroHeader", "ArticleFAQ", "ArticleFooterNav"],
                "pillar_count": 2,
                "pillar_names": ["SEO", "Trust"],
                "requested_action": "scaffold_publish_route",
                "scaffold_status": "approval_required",
                "approve_url": "/api/runs/scan-run-awaiting-approval/approve",
                "deny_url": "/api/runs/scan-run-awaiting-approval/deny",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="scan-run-awaiting-approval")
        self.assertEqual(job.status, "awaiting_confirmation")
        self.assertEqual(job.request_meta["requested_action"], "scaffold_publish_route")

        mock_send_dm.assert_called_once()
        blocks = mock_send_dm.call_args[1]["blocks"]
        self.assertIsNotNone(blocks)
        self.assertEqual(blocks[1]["type"], "actions")
        confirm_value = json.loads(blocks[1]["elements"][0]["value"])
        skip_value = json.loads(blocks[1]["elements"][1]["value"])
        self.assertEqual(confirm_value["scan_run_id"], "scan-run-awaiting-approval")
        self.assertEqual(skip_value["scan_run_id"], "scan-run-awaiting-approval")

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_scan_complete_callback_ignores_cancelled_scan_run(self, mock_send_dm):
        ContentFactoryJob.objects.create(
            job_id="scan-run-cancelled",
            domain="mlai.au",
            slack_user_id="U123",
            status="cancelled",
            request_meta={"type": "scan", "cancelled": True},
        )
        ContentFactoryRun.objects.create(
            run_id="scan-run-cancelled",
            workflow="repo_scan",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.CANCELLED,
            current_step="cancelled",
            approval_state=ContentFactoryApprovalState.NOT_REQUIRED,
            result={"status": "cancelled", "cancelled": True},
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "scan_complete",
                "job_id": "scan-run-cancelled",
                "run_id": "scan-run-cancelled",
                "workflow": "repo_scan",
                "domain": "mlai.au",
                "github_repo": "MLAI-AUS-Inc/mlai-au",
                "slack_user_id": "U123",
                "requested_action": "scaffold_publish_route",
                "scaffold_required": True,
                "scaffold_status": "approval_required",
                "approve_url": "/api/runs/scan-run-cancelled/approve",
                "deny_url": "/api/runs/scan-run-cancelled/deny",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "ignored")
        job = ContentFactoryJob.objects.get(job_id="scan-run-cancelled")
        self.assertEqual(job.status, "cancelled")
        run = ContentFactoryRun.objects.get(run_id="scan-run-cancelled")
        self.assertEqual(run.status, ContentFactoryRunStatus.CANCELLED)
        self.assertEqual(run.current_step, "cancelled")
        mock_send_dm.assert_not_called()

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_scan_complete_callback_persists_article_surface_metadata_to_run(self, mock_send_dm):
        ContentFactoryRun.objects.create(
            run_id="scan-run-surface-1",
            workflow="repo_scan",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.RUNNING,
            step_order=["load_repo_context", "scan_structure"],
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "scan_complete",
                "job_id": "scan-run-surface-1",
                "run_id": "scan-run-surface-1",
                "workflow": "repo_scan",
                "domain": "mlai.au",
                "github_repo": "MLAI-AUS-Inc/mlai-au",
                "slack_user_id": "U123",
                "requested_action": "scaffold_publish_route",
                "scaffold_required": True,
                "scaffold_status": "approval_required",
                "article_surface_hint": {"source": "user_input", "route_path": "/articles"},
                "article_surface_hint_status": "matched",
                "matched_article_surface": {"path_or_locator": "app/routes/articles.index.tsx"},
                "detected_candidates": [
                    {
                        "candidate_group": "listing_surface_candidates",
                        "path_or_locator": "app/routes/articles.index.tsx",
                        "route_template": "/articles",
                        "confidence": 0.92,
                    }
                ],
                "article_system_readiness": {
                    "status": "upgrade_required",
                    "missing_support_files": ["app/articles/resources.ts"],
                    "required_support_files": ["app/articles/registry.ts", "app/articles/resources.ts"],
                },
                "article_system_setup": {
                    "status": "upgrade_required",
                    "missing_support_files": ["app/articles/resources.ts"],
                },
                "scaffold_plan": {
                    "detected_candidates": [
                        {
                            "candidate_group": "listing_surface_candidates",
                            "path_or_locator": "app/routes/articles.index.tsx",
                            "route_template": "/articles",
                        }
                    ]
                },
                "approve_url": "/api/runs/scan-run-surface-1/approve",
                "deny_url": "/api/runs/scan-run-surface-1/deny",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        run = ContentFactoryRun.objects.get(run_id="scan-run-surface-1")
        self.assertEqual(run.status, ContentFactoryRunStatus.AWAITING_CONFIRMATION)
        self.assertEqual(run.approval_state, "approval_required")
        self.assertEqual(run.result["article_surface_hint_status"], "matched")
        self.assertEqual(run.result["article_surface_hint"]["route_path"], "/articles")
        self.assertEqual(run.result["detected_candidates"][0]["route_template"], "/articles")
        self.assertEqual(run.result["article_system_readiness"]["missing_support_files"], ["app/articles/resources.ts"])

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_scan_complete_callback_persists_auto_setup_preview_queue(self, mock_send_dm):
        Organization.objects.create(name="MLAI", domain="mlai.au")
        ContentFactoryRun.objects.create(
            run_id="scan-run-setup-queued",
            workflow="repo_scan",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.RUNNING,
            step_order=["load_repo_context", "scan_structure"],
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "scan_complete",
                "job_id": "scan-run-setup-queued",
                "run_id": "scan-run-setup-queued",
                "workflow": "repo_scan",
                "domain": "mlai.au",
                "github_repo": "MLAI-AUS-Inc/mlai-au",
                "scaffold_required": True,
                "scaffold_status": "queued",
                "scaffold_queued": True,
                "scaffold_job_id": "setup-run-1",
                "setup_run_id": "setup-run-1",
                "article_surface_hint": {"source": "user_input", "route_path": "/articles"},
                "article_surface_hint_status": "matched",
                "article_system_setup": {"status": "queued", "setup_run_id": "setup-run-1"},
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        run = ContentFactoryRun.objects.get(run_id="scan-run-setup-queued")
        self.assertEqual(run.status, ContentFactoryRunStatus.RUNNING)
        self.assertEqual(run.result["setup_run_id"], "setup-run-1")
        setup_run = ContentFactoryRun.objects.get(run_id="setup-run-1")
        self.assertEqual(setup_run.workflow, "article_system_setup")
        self.assertEqual(setup_run.status, ContentFactoryRunStatus.RUNNING)

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_article_system_setup_preview_callback_creates_review_run(self, mock_send_dm):
        Organization.objects.create(name="MLAI", domain="mlai.au")
        ContentFactoryRun.objects.create(
            run_id="scan-run-parent",
            workflow="repo_scan",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.RUNNING,
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "article_system_setup_preview_ready",
                "job_id": "setup-run-2",
                "run_id": "setup-run-2",
                "workflow": "article_system_setup",
                "domain": "mlai.au",
                "github_repo": "MLAI-AUS-Inc/mlai-au",
                "parent_run_id": "scan-run-parent",
                "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/1",
                "preview_url": "https://preview.example/articles",
                "live_preview": {
                    "available": True,
                    "previewUrl": "https://preview.example/articles",
                    "inspectorProtocolVersion": 2,
                    "inspectorMode": "comment",
                },
                "live_preview_url": "/api/runs/setup-run-2/live-preview",
                "article_system_setup": {"status": "preview_ready", "setup_run_id": "setup-run-2"},
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        setup_run = ContentFactoryRun.objects.get(run_id="setup-run-2")
        self.assertEqual(setup_run.status, ContentFactoryRunStatus.AWAITING_APPROVAL)
        self.assertEqual(setup_run.result["livePreview"]["previewUrl"], "https://preview.example/articles")
        self.assertEqual(setup_run.result["livePreview"]["inspectorMode"], "comment")
        parent = ContentFactoryRun.objects.get(run_id="scan-run-parent")
        self.assertEqual(parent.result["setup_run_id"], "setup-run-2")

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_article_system_setup_completed_waits_for_verification_scan(self, mock_send_dm):
        organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        config = OrganizationContentConfig.objects.create(
            organization=organization,
            github_repo="MLAI-AUS-Inc/mlai-au",
            article_system={
                "pending_article_system_setup": {
                    "status": "preview_ready",
                    "setup_run_id": "setup-run-complete",
                    "setupRunId": "setup-run-complete",
                }
            },
        )

        completed_response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "article_system_setup_completed",
                "job_id": "setup-run-complete",
                "run_id": "setup-run-complete",
                "workflow": "article_system_setup",
                "domain": "mlai.au",
                "github_repo": "MLAI-AUS-Inc/mlai-au",
                "parent_run_id": "scan-parent-complete",
                "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/9",
                "rescan_run_id": "verify-setup-complete",
                "merge_status": "merged",
            },
            format='json',
        )

        self.assertEqual(completed_response.status_code, status.HTTP_200_OK)
        config.refresh_from_db()
        pending = config.article_system["pending_article_system_setup"]
        self.assertEqual(pending["status"], "merged_verifying")
        self.assertEqual(pending["rescan_run_id"], "verify-setup-complete")
        self.assertFalse(config.articles_scaffolded)

        scan_response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "scan_complete",
                "job_id": "verify-setup-complete",
                "run_id": "verify-setup-complete",
                "workflow": "repo_scan",
                "domain": "mlai.au",
                "github_repo": "MLAI-AUS-Inc/mlai-au",
                "article_system": {"state": "existing", "confidence": "high", "directory_name": "articles"},
                "publish_targets": [{"id": "articles", "label": "Articles"}],
                "default_publish_target_id": "articles",
            },
            format='json',
        )

        self.assertEqual(scan_response.status_code, status.HTTP_200_OK)
        config.refresh_from_db()
        self.assertNotIn("pending_article_system_setup", config.article_system)
        self.assertTrue(config.articles_scaffolded)
        self.assertEqual(config.article_system["state"], "existing")

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_scaffold_complete_persists_preview_metadata_without_publishing(self, mock_send_dm):
        organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        config = OrganizationContentConfig.objects.create(
            organization=organization,
            github_repo="MLAI-AUS-Inc/mlai-au",
            article_system={
                "pending_article_system_setup": {
                    "status": "pending_generation",
                    "setup_run_id": "setup-run-scaffold",
                    "setupRunId": "setup-run-scaffold",
                }
            },
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "scaffold_complete",
                "job_id": "setup-run-scaffold",
                "parent_run_id": "scan-run-scaffold",
                "domain": "mlai.au",
                "github_repo": "MLAI-AUS-Inc/mlai-au",
                "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/10",
                "preview_url": "https://preview.example/articles",
                "already_exists": False,
                "build_verified": True,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        config.refresh_from_db()
        pending = config.article_system["pending_article_system_setup"]
        self.assertEqual(pending["status"], "preview_ready")
        self.assertEqual(pending["pr_url"], "https://github.com/MLAI-AUS-Inc/mlai-au/pull/10")
        self.assertFalse(config.articles_scaffolded)
        self.assertNotEqual(config.article_system.get("state"), "roo_scaffolded")

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_article_system_setup_preview_failed_callback_blocks_run(self, mock_send_dm):
        Organization.objects.create(name="MLAI", domain="mlai.au")
        ContentFactoryRun.objects.create(
            run_id="scan-run-parent-failed-preview",
            workflow="repo_scan",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.RUNNING,
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "article_system_setup_preview_failed",
                "job_id": "setup-run-preview-failed",
                "run_id": "setup-run-preview-failed",
                "workflow": "article_system_setup",
                "domain": "mlai.au",
                "github_repo": "MLAI-AUS-Inc/mlai-au",
                "parent_run_id": "scan-run-parent-failed-preview",
                "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/2",
                "live_preview": {
                    "available": False,
                    "status": "failed",
                    "platformStatus": "failed",
                    "error": "MLAI GitHub App cannot access MLAI-AUS-Inc/mlai-au.",
                    "errorCode": "platform_preview_failed",
                    "builderRunUrl": "https://github.com/MLAI-AUS-Inc/content-factory/actions/runs/21",
                    "retryable": True,
                },
                "live_preview_url": "/api/runs/setup-run-preview-failed/live-preview",
                "article_system_setup": {
                    "status": "preview_failed",
                    "setup_run_id": "setup-run-preview-failed",
                    "error": "MLAI GitHub App cannot access MLAI-AUS-Inc/mlai-au.",
                },
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        setup_run = ContentFactoryRun.objects.get(run_id="setup-run-preview-failed")
        self.assertEqual(setup_run.status, ContentFactoryRunStatus.BLOCKED)
        self.assertEqual(setup_run.current_step, "preview_failed")
        self.assertEqual(setup_run.error, "MLAI GitHub App cannot access MLAI-AUS-Inc/mlai-au.")
        self.assertEqual(setup_run.result["livePreview"]["status"], "failed")
        self.assertEqual(setup_run.result["livePreview"]["builderRunUrl"], "https://github.com/MLAI-AUS-Inc/content-factory/actions/runs/21")
        parent = ContentFactoryRun.objects.get(run_id="scan-run-parent-failed-preview")
        self.assertEqual(parent.current_step, "article_system_setup_preview_failed")
        self.assertEqual(parent.result["article_system_setup"]["status"], "preview_failed")

    def test_article_system_live_preview_retry_progress_clears_stale_failure_state(self):
        from content_factory.vibe_marketing_views import _persist_live_preview_payload

        setup_run = ContentFactoryRun.objects.create(
            run_id="setup-run-preview-retry",
            workflow="article_system_setup",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.BLOCKED,
            current_step="preview_failed",
            approval_state=ContentFactoryApprovalState.NOT_REQUIRED,
            error="Hosted preview workflow failed.",
            result={
                "status": "preview_failed",
                "error": "Hosted preview workflow failed.",
                "article_system_setup": {
                    "status": "preview_failed",
                    "setup_run_id": "setup-run-preview-retry",
                    "error": "Hosted preview workflow failed.",
                },
                "livePreview": {
                    "available": False,
                    "status": "failed",
                    "platformStatus": "failed",
                    "error": "Hosted preview workflow failed.",
                },
            },
        )

        _persist_live_preview_payload(
            setup_run,
            {
                "available": False,
                "status": "building",
                "platformStatus": "queued",
                "previewMode": "platform_deployment",
                "previewUrl": "",
            },
        )

        setup_run.refresh_from_db()
        self.assertEqual(setup_run.status, ContentFactoryRunStatus.RUNNING)
        self.assertEqual(setup_run.current_step, "start_hosted_preview")
        self.assertEqual(setup_run.error, "")
        self.assertEqual(setup_run.result["status"], "preview_building")
        self.assertNotIn("error", setup_run.result)
        self.assertNotIn("error", setup_run.result["article_system_setup"])
        self.assertEqual(setup_run.result["article_system_setup"]["status"], "preview_building")

        _persist_live_preview_payload(
            setup_run,
            {
                "available": True,
                "status": "running",
                "platformStatus": "ready",
                "previewMode": "platform_deployment",
                "previewUrl": "https://preview.example/articles",
            },
        )

        setup_run.refresh_from_db()
        self.assertEqual(setup_run.status, ContentFactoryRunStatus.AWAITING_APPROVAL)
        self.assertEqual(setup_run.current_step, "await_review")
        self.assertEqual(setup_run.approval_state, ContentFactoryApprovalState.APPROVAL_REQUIRED)
        self.assertEqual(setup_run.error, "")
        self.assertEqual(setup_run.result["status"], "preview_ready")
        self.assertEqual(setup_run.result["preview_url"], "https://preview.example/articles")
        self.assertEqual(setup_run.result["article_system_setup"]["status"], "preview_ready")

        _persist_live_preview_payload(
            setup_run,
            {
                "available": False,
                "status": "failed",
                "platformStatus": "failed",
                "previewMode": "platform_deployment",
                "previewUrl": "",
                "error": "Hosted preview workflow failed again.",
                "retryable": True,
            },
        )

        setup_run.refresh_from_db()
        self.assertEqual(setup_run.status, ContentFactoryRunStatus.BLOCKED)
        self.assertEqual(setup_run.current_step, "preview_failed")
        self.assertEqual(setup_run.error, "Hosted preview workflow failed again.")
        self.assertEqual(setup_run.result["article_system_setup"]["status"], "preview_failed")
        self.assertEqual(setup_run.result["article_system_setup"]["error"], "Hosted preview workflow failed again.")

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_scan_complete_overwrites_stale_scan_article_system_metadata(self, mock_send_dm):
        organization = Organization.objects.create(name="Woofya", domain="woofya.com.au")
        config = OrganizationContentConfig.objects.create(
            organization=organization,
            scan_summary="scan complete",
            article_system={
                "state": "existing",
                "directory_name": "articles",
                "directory_path": "app/articles",
                "confidence": "high",
                "reason": "Detected existing article system",
                "source": "scan",
                "verified_at": "2026-03-20T00:00:00+00:00",
            },
            publish_targets=[
                {
                    "target_id": "react:app/articles",
                    "kind": "react_article_system",
                }
            ],
            default_publish_target_id="react:app/articles",
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "scan_complete",
                "job_id": "scan-woofya-1",
                "run_id": "scan-woofya-1",
                "workflow": "repo_scan",
                "domain": "woofya.com.au",
                "slack_user_id": "U123",
                "components_generated": False,
                "components_count": 0,
                "article_system": {
                    "state": "missing",
                    "directory_name": None,
                    "directory_path": None,
                    "confidence": "low",
                    "reason": "No existing article or blog system detected",
                    "source": "scan",
                    "verified_at": "2026-03-23T07:00:00+00:00",
                },
                "publish_targets": [],
                "default_publish_target_id": None,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        config.refresh_from_db()
        self.assertEqual(config.article_system["state"], "missing")
        self.assertEqual(config.publish_targets, [])
        self.assertIsNone(config.default_publish_target_id)

        mock_send_dm.assert_called_once()
        message = mock_send_dm.call_args[0][1]
        self.assertNotIn("app/articles", message)
        self.assertNotIn("existing article system", message)

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_scan_complete_persists_and_announces_registry_driven_target(self, mock_send_dm):
        organization = Organization.objects.create(name="Skedy", domain="skedy.io")
        config = OrganizationContentConfig.objects.create(
            organization=organization,
            scan_summary="scan pending",
            article_system={},
            publish_targets=[],
        )
        readiness = {
            "structure_ready": True,
            "mapping_ready": True,
            "routing_ready": True,
            "safety_ready": True,
        }
        publish_targets = [
            {
                "target_id": "registry_driven_seo_shared_lib_seo_public_pages_ts",
                "kind": "registry_driven_seo",
                "delivery_adapter": "registry_entry",
                "publish_capability": "direct",
                "registry_status": "publish_ready",
                "readiness": readiness,
                "registration_strategy": {
                    "type": "registry_entry_patch",
                    "registry_path": "shared/lib/seo/public-pages.ts",
                    "route_template": "/resources/guides/{slug}",
                    "field_mapping": {
                        "path": "canonicalPath",
                        "title": "title",
                        "description": "description",
                        "content": "sections",
                    },
                    "content_adapter": {"type": "sections_array"},
                },
            }
        ]

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "scan_complete",
                "job_id": "scan-skedy-registry-1",
                "run_id": "scan-skedy-registry-1",
                "workflow": "repo_scan",
                "domain": "skedy.io",
                "slack_user_id": "U123",
                "components_generated": False,
                "components_count": 0,
                "article_system": {
                    "state": "existing",
                    "directory_name": "registry",
                    "directory_path": "shared/lib/seo/public-pages.ts",
                    "confidence": "high",
                    "reason": "Detected registry-driven SEO system",
                    "source": "scan",
                    "verified_at": "2026-04-23T00:00:00+00:00",
                    "system_type": "registry_driven_seo",
                    "route_template": "/resources/guides/{slug}",
                    "readiness": readiness,
                    "registry": {
                        "path": "shared/lib/seo/public-pages.ts",
                        "export_name": "PUBLIC_PAGES",
                    },
                },
                "publish_targets": publish_targets,
                "default_publish_target_id": publish_targets[0]["target_id"],
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        config.refresh_from_db()
        self.assertEqual(config.article_system["system_type"], "registry_driven_seo")
        self.assertEqual(config.publish_targets, publish_targets)
        self.assertEqual(config.default_publish_target_id, publish_targets[0]["target_id"])

        mock_send_dm.assert_called_once()
        message = mock_send_dm.call_args[0][1]
        self.assertIn("registry-driven SEO system", message)
        self.assertIn("shared/lib/seo/public-pages.ts", message)
        self.assertIn("typed registry entries", message)

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_scan_complete_persists_registry_target_without_article_system_payload(self, mock_send_dm):
        organization = Organization.objects.create(name="Registry Co", domain="registry.example")
        config = OrganizationContentConfig.objects.create(
            organization=organization,
            scan_summary="scan pending",
            article_system={},
            publish_targets=[],
        )
        publish_targets = [
            {
                "target_id": "registry_driven_seo_src_data_pages_ts",
                "kind": "registry_driven_seo",
                "delivery_adapter": "registry_entry",
                "publish_capability": "bundle_only",
                "readiness": {
                    "structure_ready": True,
                    "mapping_ready": False,
                    "routing_ready": True,
                    "safety_ready": False,
                },
                "registration_strategy": {
                    "type": "registry_entry_patch",
                    "registry_path": "src/data/pages.ts",
                    "diagnostics": {
                        "issues": ["route field is ambiguous"],
                    },
                },
            }
        ]

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "scan_complete",
                "job_id": "scan-registry-no-article-system",
                "run_id": "scan-registry-no-article-system",
                "workflow": "repo_scan",
                "domain": "registry.example",
                "slack_user_id": "U123",
                "components_generated": False,
                "components_count": 0,
                "publish_targets": publish_targets,
                "default_publish_target_id": publish_targets[0]["target_id"],
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        config.refresh_from_db()
        self.assertEqual(config.publish_targets, publish_targets)
        self.assertEqual(config.default_publish_target_id, publish_targets[0]["target_id"])

        mock_send_dm.assert_called_once()
        message = mock_send_dm.call_args[0][1]
        self.assertIn("registry-driven SEO system", message)
        self.assertIn("not safe to patch automatically yet", message)
        self.assertIn("route field is ambiguous", message)

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_generation_failed_auto_discovery_no_opportunities_mentions_research_scope(self, mock_send_dm):
        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "generation_failed",
                "job_id": "discovery-run-1",
                "run_id": "discovery-run-1",
                "workflow": "auto_discovery",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "error_code": "NO_OPPORTUNITIES",
                "error": "No relevant keywords found after filtering.",
                "diagnostics": {
                    "seed_count": 20,
                    "competitor_count": 5,
                    "keyword_ideas_count": 0,
                    "keyword_suggestions_count": 0,
                    "related_keywords_count": 0,
                    "ai_question_count": 0,
                    "competitor_candidate_count": 7,
                    "competitor_relevance_rejected_count": 7,
                    "remaining_opportunity_count": 0,
                },
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_send_dm.assert_called_once()
        message = mock_send_dm.call_args[0][1]
        self.assertIn("Research for mlai.au", message)
        self.assertIn("Checked 20 seed keywords and 5 competitors.", message)
        self.assertIn("write an article for mlai.au about [topic]", message)
        self.assertIn("doesn't affect any scan or scaffold work already in progress", message)
        self.assertNotIn("Task failed for", message)

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_generation_failed_publish_target_action_required_auto_refunds(self, mock_send_dm):
        user = User.objects.create_user(email="refund@example.com", password="password", slack_id="U123")
        PointsAccount.objects.create(user=user, balance=14)
        ContentFactoryJob.objects.create(
            job_id="publish-target-run-1",
            domain="woofya.com.au",
            slack_user_id="U123",
            status="generating",
            client_request_id="publish-target-request-1",
            billing_source_job_id="publish-target-run-1",
            billing_amount=4,
            billing_status="charged",
            request_meta={
                "domain": "woofya.com.au",
                "client_request_id": "publish-target-request-1",
            },
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "generation_failed",
                "job_id": "publish-target-run-1",
                "run_id": "publish-target-run-1",
                "workflow": "direct_generate",
                "domain": "woofya.com.au",
                "slack_user_id": "U123",
                "error_code": "PUBLISH_TARGET_ACTION_REQUIRED",
                "error": "No compatible delivery adapter could be resolved from the repository/article-system signals. Use content_only until a supported publish target is configured.",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_send_dm.assert_called_once()
        message = mock_send_dm.call_args[0][1]
        self.assertIn("needs a supported publish target", message)
        self.assertIn("`.content-factory/target.yml`", message)
        self.assertIn("refunded automatically", message)

        user.points_account.refresh_from_db()
        self.assertEqual(user.points_account.balance, 18)
        job = ContentFactoryJob.objects.get(job_id="publish-target-run-1")
        self.assertEqual(job.billing_status, "refunded")

    @patch('integrations.services.slack.SlackService.send_message')
    @patch('integrations.services.slack.SlackService.send_dm')
    def test_generation_failed_scheduled_daily_updates_dispatch_and_suppresses_user_message(self, mock_send_dm, mock_send_message):
        ContentFactoryJob.objects.create(
            job_id="scheduled-failure-1",
            domain="mlai.au",
            slack_user_id="U123",
            status="researching",
            request_meta={
                "domain": "mlai.au",
                "trigger_source": "scheduled_daily",
            },
        )
        dispatch = ScheduledDiscoveryDispatch.objects.create(
            slack_user_id="U123",
            domain="mlai.au",
            timezone="Australia/Melbourne",
            local_date=timezone.now().date(),
            state=ScheduledDiscoveryDispatchState.QUEUED,
            content_factory_job_id="scheduled-failure-1",
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "generation_failed",
                "job_id": "scheduled-failure-1",
                "run_id": "scheduled-failure-1",
                "workflow": "auto_discovery",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "error_code": "NO_OPPORTUNITIES",
                "error": "No relevant opportunities today.",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.state, ScheduledDiscoveryDispatchState.FAILED)
        mock_send_dm.assert_not_called()
        mock_send_message.assert_not_called()

    @patch('integrations.services.article_generation.upsert_live_progress_card')
    @patch('integrations.services.slack.SlackService.send_message')
    @patch('integrations.services.slack.SlackService.send_dm')
    def test_generation_blocked_marks_job_blocked_without_terminal_notification(
        self,
        mock_send_dm,
        mock_send_message,
        mock_upsert_live_progress_card,
    ):
        user = User.objects.create_user(email="blocked@example.com", password="password", slack_id="U123")
        PointsAccount.objects.create(user=user, balance=14)
        ContentFactoryJob.objects.create(
            job_id="blocked-run-1",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            billing_status="charged",
            billing_amount=4,
            request_meta={"domain": "mlai.au"},
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "generation_blocked",
                "job_id": "blocked-run-1",
                "run_id": "blocked-run-1",
                "workflow": "direct_generate",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "blocked_step": "verify_build",
                "error_code": "verifier_capacity_unavailable",
                "error": "Dedicated verifier worker `build-verifier` is unavailable; verify_build is blocked until capacity returns.",
                "preferred_queue": "build-verifier",
                "fallback_policy": "auto_fallback",
                "retry_after_seconds": 60,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="blocked-run-1")
        self.assertEqual(job.status, "blocked")
        self.assertIn("verifier_capacity_unavailable", job.error_message)
        self.assertEqual(job.billing_status, "charged")
        self.assertEqual(job.request_meta.get("blocked_step"), "verify_build")
        self.assertEqual(job.request_meta.get("blocked_preferred_queue"), "build-verifier")
        run = ContentFactoryRun.objects.get(run_id="blocked-run-1")
        self.assertEqual(run.status, ContentFactoryRunStatus.BLOCKED)
        self.assertEqual(run.current_step, "verify_build")
        self.assertIn("Dedicated verifier worker", run.error)
        step = ContentFactoryRunStep.objects.get(run=run, step_key="verify_build")
        self.assertEqual(step.status, ContentFactoryStepStatus.BLOCKED)
        mock_send_dm.assert_not_called()
        mock_send_message.assert_not_called()

    @patch('integrations.services.article_generation.upsert_live_progress_card')
    @patch('integrations.services.slack.SlackService.send_message')
    @patch('integrations.services.slack.SlackService.send_dm')
    def test_generation_failed_updates_durable_run_state(
        self,
        mock_send_dm,
        mock_send_message,
        mock_upsert_live_progress_card,
    ):
        ContentFactoryJob.objects.create(
            job_id="failed-run-1",
            domain="mlai.au",
            status="generating",
            request_meta={"domain": "mlai.au"},
        )
        ContentFactoryRun.objects.create(
            run_id="failed-run-1",
            workflow="direct_generate",
            domain="mlai.au",
            status=ContentFactoryRunStatus.RUNNING,
            current_step="synthesize_repository_contract",
            step_order=["fetch_org_config", "synthesize_repository_contract"],
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "generation_failed",
                "job_id": "failed-run-1",
                "run_id": "failed-run-1",
                "workflow": "direct_generate",
                "domain": "mlai.au",
                "failed_step": "synthesize_repository_contract",
                "error_code": "INTERNAL_ERROR",
                "error": "Task failed with unhandled exception: TimeLimitExceeded(5600)",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        run = ContentFactoryRun.objects.get(run_id="failed-run-1")
        self.assertEqual(run.status, ContentFactoryRunStatus.FAILED)
        self.assertEqual(run.current_step, "synthesize_repository_contract")
        self.assertIn("TimeLimitExceeded", run.error)
        step = ContentFactoryRunStep.objects.get(run=run, step_key="synthesize_repository_contract")
        self.assertEqual(step.status, ContentFactoryStepStatus.FAILED)
        mock_send_dm.assert_not_called()
        mock_send_message.assert_not_called()

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_generation_failed_catalog_missing_components_points_to_repo_scan(self, mock_send_dm):
        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "generation_failed",
                "job_id": "catalog-missing-run-1",
                "run_id": "catalog-missing-run-1",
                "workflow": "article_revision",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "error_code": "CATALOG_MISSING_REQUIRED_COMPONENTS",
                "error": "Missing required mlai.au featured components in catalog: ArticleDisclaimer",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_send_dm.assert_called_once()
        message = mock_send_dm.call_args.args[1]
        self.assertIn("article component catalog refreshed", message)
        self.assertIn("Connect repo & articles location", message)
        self.assertNotIn("contact support", message)

    @patch('integrations.services.article_generation.upsert_live_progress_card')
    @patch('integrations.services.slack.SlackService.send_message')
    @patch('integrations.services.slack.SlackService.send_dm')
    def test_generation_blocked_dependency_recovery_exhausted_posts_one_thread_message(
        self,
        mock_send_dm,
        mock_send_message,
        mock_upsert_live_progress_card,
    ):
        ContentFactoryJob.objects.create(
            job_id="blocked-exhausted-run-1",
            domain="skedy.io",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            request_meta={"domain": "skedy.io"},
        )

        payload = {
            "event_type": "generation_blocked",
            "job_id": "blocked-exhausted-run-1",
            "run_id": "blocked-exhausted-run-1",
            "workflow": "direct_generate",
            "domain": "skedy.io",
            "slack_user_id": "U123",
            "blocked_step": "validate_render_dependencies",
            "error_code": "article_dependency_strategy_unresolved",
            "error": "Article dependency strategy is unresolved",
            "next_step": "synthesize_repository_contract",
            "rerunnable_step": "synthesize_repository_contract",
            "recovery_attempt": 2,
            "recovery_exhausted": True,
        }

        first = self.client.post(reverse('content_factory_callback'), payload, format='json')
        second = self.client.post(reverse('content_factory_callback'), payload, format='json')

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="blocked-exhausted-run-1")
        self.assertEqual(job.status, "blocked")
        self.assertEqual(job.request_meta.get("blocked_rerunnable_step"), "synthesize_repository_contract")
        self.assertEqual(job.request_meta.get("blocked_recovery_attempt"), 2)
        self.assertTrue(job.request_meta.get("blocked_recovery_exhausted"))
        self.assertTrue(job.request_meta.get("blocked_visible_notification_key"))
        mock_upsert_live_progress_card.assert_called()
        mock_send_dm.assert_not_called()
        self.assertEqual(mock_send_message.call_count, 1)
        self.assertEqual(mock_send_message.call_args.kwargs["thread_ts"], "123.456")
        self.assertIn("skedy.io", mock_send_message.call_args.args[1])

    @patch('integrations.services.slack.SlackService.send_message')
    def test_scaffold_complete_uses_parent_run_thread_context(self, mock_send_message):
        ContentFactoryJob.objects.create(
            job_id="scan-parent-1",
            domain="mlai.au",
            slack_user_id="U123",
            status="completed",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "scaffold_complete",
                "job_id": "scaffold-run-1",
                "run_id": "scaffold-run-1",
                "workflow": "scaffold",
                "parent_run_id": "scan-parent-1",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "already_exists": True,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_send_message.assert_called_once()
        self.assertEqual(mock_send_message.call_args[0][0], "C123")
        self.assertEqual(mock_send_message.call_args[1]["thread_ts"], "123.456")

    @patch('integrations.services.slack.SlackService.send_message')
    def test_scaffold_complete_callback_with_reused_pr_posts_preview_and_build_details(self, mock_send_message):
        organization = Organization.objects.create(name="Bird Psychology", domain="birdpsychology.com.au")
        config = OrganizationContentConfig.objects.create(
            organization=organization,
            scan_summary="scan complete",
            article_system={"state": "incomplete"},
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "scaffold_complete",
                "job_id": "scaffold-run-2",
                "run_id": "scaffold-run-2",
                "workflow": "scaffold",
                "domain": "birdpsychology.com.au",
                "slack_user_id": "U123",
                "slack_channel_id": "C123",
                "slack_thread_ts": "123.456",
                "pr_url": "https://github.com/acme/site/pull/2",
                "preview_url": "https://preview.example.com/articles",
                "build_verified": True,
                "files_created": 0,
                "pillar_count": 4,
                "component_count": 20,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_send_message.assert_called_once()
        blocks = mock_send_message.call_args[1]["blocks"]
        self.assertIn("Reused the existing scaffold branch/PR", blocks[0]["text"]["text"])
        self.assertIn("Build passed", blocks[0]["text"]["text"])
        self.assertIn("https://preview.example.com/articles", blocks[0]["text"]["text"])
        config.refresh_from_db()
        self.assertEqual(config.articles_scaffold_pr_url, "https://github.com/acme/site/pull/2")
        self.assertEqual(config.articles_scaffold_preview_url, "https://preview.example.com/articles")

    def test_error_callback(self):
        url = reverse('content_factory_callback')
        data = {
            "event_type": "error",
            "job_id": "job-error",
            "domain": "mlai.au",
            "error_message": "Generation failed"
        }
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-error")
        self.assertEqual(job.status, 'error')
        self.assertEqual(job.error_message, "Generation failed")

    def test_delivery_mode_required_callback_keeps_job_waiting_for_user_choice(self):
        url = reverse('content_factory_callback')
        data = {
            "event_type": "delivery_mode_required",
            "job_id": "job-delivery-mode",
            "domain": "mlai.au",
            "slack_user_id": "U123",
            "recommended_delivery_mode": "content_only",
        }

        response = self.client.post(url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-delivery-mode")
        self.assertEqual(job.status, "awaiting_delivery_mode")
        self.assertEqual(job.request_meta.get("recommended_delivery_mode"), "content_only")

    @patch('integrations.services.slack.SlackService.send_message')
    def test_draft_pr_created_callback_posts_thread_reply_once(self, mock_send_message):
        mock_send_message.return_value = (True, "message-ts")
        ContentFactoryJob.objects.create(
            job_id="job-draft-pr",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
        )

        url = reverse('content_factory_callback')
        data = {
            "event_type": "draft_pr_created",
            "job_id": "job-draft-pr",
            "domain": "mlai.au",
            "slack_user_id": "U123",
            "pr_url": "https://github.com/example/pr/1",
            "pr_number": 1,
            "review_surface_kind": "preview_route",
            "primary_review_url": "https://github.com/example/pr/1",
            "primary_review_label": "Open PR",
            "route_is_live": False,
            "route_path": "",
            "intended_route_path": "/articles/ifs",
            "dedupe_key": "draft-pr-1",
        }

        first_response = self.client.post(url, data, format='json')
        second_response = self.client.post(url, data, format='json')

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertEqual(second_response.data["status"], "ignored")

        job = ContentFactoryJob.objects.get(job_id="job-draft-pr")
        self.assertEqual(job.status, "generating")
        self.assertEqual(job.pr_url, "https://github.com/example/pr/1")
        self.assertEqual(job.request_meta.get("publish_stage"), "awaiting_preview")
        self.assertEqual(job.request_meta["callback_notifications"]["draft_pr_created"], ["draft-pr-1"])
        mock_send_message.assert_called_once()
        self.assertEqual(mock_send_message.call_args[0][0], "C123")
        self.assertEqual(mock_send_message.call_args[1]["thread_ts"], "123.456")
        blocks = mock_send_message.call_args[1]["blocks"]
        self.assertIn("Draft PR created", blocks[0]["text"]["text"])
        self.assertNotIn("Preview route:", blocks[0]["text"]["text"])
        self.assertEqual(blocks[1]["elements"][0]["text"]["text"], "Open PR")

    @patch('integrations.services.slack.SlackService.send_message')
    def test_draft_pr_created_callback_uses_artifact_preview_when_repo_preview_missing(self, mock_send_message):
        mock_send_message.return_value = (True, "message-ts")
        ContentFactoryJob.objects.create(
            job_id="job-draft-pr-preview",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
        )
        self._create_content_factory_run("job-draft-pr-preview")

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "draft_pr_created",
                "job_id": "job-draft-pr-preview",
                "run_id": "job-draft-pr-preview",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "pr_url": "https://github.com/example/pr/12",
                "pr_number": 12,
                "review_surface_kind": "preview_route",
                "primary_review_url": "https://github.com/example/pr/12",
                "primary_review_label": "Open PR",
                "route_is_live": False,
                "route_path": "",
                "intended_route_path": "/articles/featured/how-to-build-an-ai-harness",
                "preview_screenshot_urls": [
                    "https://storage.example.com/previews/job-draft-pr-preview.png",
                ],
                "dedupe_key": "draft-pr-preview-12",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-draft-pr-preview")
        self.assertEqual(job.request_meta.get("publish_stage"), "awaiting_preview")
        self.assertEqual(job.request_meta.get("preview_surface_kind"), "artifact_preview")
        self.assertEqual(job.request_meta.get("preview_url", ""), "")
        self.assertIn(
            "/api/content-factory/runs/job-draft-pr-preview/preview/",
            job.request_meta.get("artifact_preview_url", ""),
        )
        self.assertIn("sig=", job.request_meta.get("artifact_preview_url", ""))
        self.assertEqual(job.request_meta.get("primary_action_url"), "https://github.com/example/pr/12")
        self.assertEqual(job.request_meta.get("primary_action_label"), "Open PR")
        self.assertEqual(
            job.request_meta.get("preview_screenshot_urls"),
            ["https://storage.example.com/previews/job-draft-pr-preview.png"],
        )
        mock_send_message.assert_called_once()
        self.assertIn("Draft PR ready for mlai.au:", mock_send_message.call_args[0][1])
        blocks = mock_send_message.call_args[1]["blocks"]
        self.assertIn("Draft PR created", blocks[0]["text"]["text"])
        self.assertNotIn("Preview route:", blocks[0]["text"]["text"])
        button_texts = [element["text"]["text"] for element in blocks[1]["elements"]]
        self.assertEqual(button_texts[:2], ["Open PR", "Open Evidence Preview"])
        self.assertEqual(blocks[2]["type"], "image")
        self.assertEqual(
            blocks[2]["image_url"],
            "https://storage.example.com/previews/job-draft-pr-preview.png",
        )
        self.assertIn(
            "/api/content-factory/runs/job-draft-pr-preview/preview/",
            blocks[1]["elements"][1]["url"],
        )
        self.assertIn("sig=", blocks[1]["elements"][1]["url"])

    @patch('integrations.services.slack.SlackService.send_message')
    def test_draft_pr_created_callback_downgrades_unverified_repo_preview_to_artifact_preview(self, mock_send_message):
        mock_send_message.return_value = (True, "message-ts")
        ContentFactoryJob.objects.create(
            job_id="job-draft-pr-unverified-preview",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
        )
        self._create_content_factory_run("job-draft-pr-unverified-preview")

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "draft_pr_created",
                "job_id": "job-draft-pr-unverified-preview",
                "run_id": "job-draft-pr-unverified-preview",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "pr_url": "https://github.com/example/pr/13",
                "pr_number": 13,
                "preview_url": "https://preview.example.com/articles/featured/test-article",
                "preview_surface_kind": "repo_preview",
                "preview_content_verified": False,
                "repo_preview_candidate_url": "https://preview.example.com/articles/featured/test-article",
                "review_surface_kind": "preview_route",
                "primary_review_url": "https://preview.example.com/articles/featured/test-article",
                "primary_review_label": "Open Preview",
                "route_is_live": True,
                "route_path": "/articles/featured/test-article",
                "dedupe_key": "draft-pr-unverified-preview-13",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-draft-pr-unverified-preview")
        self.assertEqual(job.request_meta.get("preview_surface_kind"), "repo_preview")
        self.assertFalse(job.request_meta.get("preview_content_verified"))
        self.assertEqual(
            job.request_meta.get("repo_preview_candidate_url"),
            "https://preview.example.com/articles/featured/test-article",
        )
        self.assertEqual(job.request_meta.get("preview_url", ""), "")
        self.assertIn(
            "/api/content-factory/runs/job-draft-pr-unverified-preview/preview/",
            job.request_meta.get("artifact_preview_url", ""),
        )
        self.assertEqual(job.request_meta.get("primary_action_url"), "https://github.com/example/pr/13")
        self.assertEqual(job.request_meta.get("primary_action_label"), "Open PR")
        self.assertEqual(job.request_meta.get("primary_action_kind"), "pull_request")
        blocks = mock_send_message.call_args[1]["blocks"]
        button_texts = [element["text"]["text"] for element in blocks[1]["elements"]]
        self.assertEqual(button_texts[:3], ["Open PR", "Open Evidence Preview", "Open Candidate Preview"])

    @patch('integrations.services.slack.SlackService.send_message')
    def test_generation_pr_opened_callback_marks_job_needs_review(self, mock_send_message):
        mock_send_message.return_value = (True, "message-ts")
        ContentFactoryJob.objects.create(
            job_id="job-pr-opened",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "generation_pr_opened",
                "job_id": "job-pr-opened",
                "run_id": "job-pr-opened",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "pr_url": "https://github.com/example/pr/44",
                "pr_number": 44,
                "review_surface_kind": "fallback_bundle",
                "primary_review_url": "https://github.com/example/pr/44",
                "primary_review_label": "Open review PR",
                "route_is_live": False,
                "route_path": "",
                "intended_route_path": "/articles/how-to-build-an-ai-harness",
                "bundle_primary_path": ".content-factory/drafts/how-to-build-an-ai-harness/README.md",
                "review_required": True,
                "verification_state": "build_failed_after_repair_budget",
                "reason_code": "repair_budget_exhausted",
                "artifact_links": {
                    "verification_report": "/api/runs/job-pr-opened/artifacts/verification_report.json",
                },
                "dedupe_key": "generation-pr-opened-44",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-pr-opened")
        self.assertEqual(job.status, "needs_review")
        self.assertEqual(job.pr_url, "https://github.com/example/pr/44")
        self.assertEqual(job.request_meta.get("publish_stage"), "needs_review")
        self.assertTrue(job.request_meta.get("review_required"))
        self.assertEqual(job.request_meta.get("verification_state"), "build_failed_after_repair_budget")
        self.assertEqual(job.request_meta.get("reason_code"), "repair_budget_exhausted")
        self.assertEqual(
            job.request_meta.get("artifact_links", {}).get("verification_report"),
            "/api/runs/job-pr-opened/artifacts/verification_report.json",
        )
        self.assertEqual(job.progress_message_ts, "message-ts")
        self.assertEqual(mock_send_message.call_count, 2)

        live_card_call = mock_send_message.call_args_list[0]
        self.assertEqual(live_card_call[0][0], "C123")
        self.assertEqual(live_card_call[0][1], "Review bundle PR opened and ready for human review.")
        self.assertEqual(live_card_call[1]["thread_ts"], "123.456")
        self.assertIn(
            "Review bundle PR opened and ready for human review.",
            live_card_call[1]["blocks"][0]["text"]["text"],
        )

        pr_notification_call = mock_send_message.call_args_list[1]
        self.assertEqual(pr_notification_call[0][0], "C123")
        self.assertEqual(
            pr_notification_call[0][1],
            "Review bundle ready for mlai.au: https://github.com/example/pr/44",
        )
        self.assertEqual(pr_notification_call[1]["thread_ts"], "123.456")
        self.assertIn("Review PR created", pr_notification_call[1]["blocks"][0]["text"]["text"])
        self.assertIn(".content-factory/drafts/how-to-build-an-ai-harness/README.md", pr_notification_call[1]["blocks"][0]["text"]["text"])
        self.assertNotIn("Preview route:", pr_notification_call[1]["blocks"][0]["text"]["text"])
        self.assertEqual(pr_notification_call[1]["blocks"][1]["elements"][0]["text"]["text"], "Open review PR")

    @patch('integrations.services.slack.SlackService.send_message')
    def test_generation_pr_opened_callback_uses_artifact_preview_for_review_bundle(self, mock_send_message):
        mock_send_message.return_value = (True, "message-ts")
        ContentFactoryJob.objects.create(
            job_id="job-pr-opened-preview",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
        )
        self._create_content_factory_run("job-pr-opened-preview")

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "generation_pr_opened",
                "job_id": "job-pr-opened-preview",
                "run_id": "job-pr-opened-preview",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "pr_url": "https://github.com/example/pr/77",
                "pr_number": 77,
                "review_surface_kind": "fallback_bundle",
                "primary_review_url": "https://github.com/example/pr/77",
                "primary_review_label": "Open review PR",
                "route_is_live": False,
                "intended_route_path": "/articles/featured/how-to-build-an-ai-harness",
                "bundle_primary_path": ".content-factory/drafts/how-to-build-an-ai-harness/README.md",
                "review_required": True,
                "verification_state": "build_failed_review_pr",
                "reason_code": "environment_bundler_unstable",
                "preview_screenshot_urls": [
                    "https://storage.example.com/previews/job-pr-opened-preview.png",
                ],
                "dedupe_key": "generation-pr-opened-preview-77",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-pr-opened-preview")
        self.assertEqual(job.status, "needs_review")
        self.assertEqual(job.request_meta.get("preview_surface_kind"), "artifact_preview")
        self.assertEqual(job.request_meta.get("preview_url", ""), "")
        self.assertIn(
            "/api/content-factory/runs/job-pr-opened-preview/preview/",
            job.request_meta.get("artifact_preview_url", ""),
        )
        self.assertIn("sig=", job.request_meta.get("artifact_preview_url", ""))
        self.assertEqual(job.request_meta.get("primary_action_url"), "https://github.com/example/pr/77")
        self.assertEqual(job.request_meta.get("primary_action_label"), "Open review PR")
        self.assertEqual(
            job.request_meta.get("preview_screenshot_urls"),
            ["https://storage.example.com/previews/job-pr-opened-preview.png"],
        )
        self.assertEqual(mock_send_message.call_count, 2)
        pr_notification_call = mock_send_message.call_args_list[1]
        self.assertIn("Review bundle ready for mlai.au:", pr_notification_call[0][1])
        blocks = pr_notification_call[1]["blocks"]
        self.assertIn("Review PR created", blocks[0]["text"]["text"])
        button_texts = [element["text"]["text"] for element in blocks[1]["elements"]]
        self.assertEqual(button_texts[:2], ["Open review PR", "Open Evidence Preview"])
        self.assertEqual(blocks[2]["type"], "image")
        self.assertEqual(
            blocks[2]["image_url"],
            "https://storage.example.com/previews/job-pr-opened-preview.png",
        )

    @patch('integrations.services.slack.SlackService.send_message')
    def test_generation_pr_opened_callback_downgrades_raw_repo_preview_url_for_review_bundle(self, mock_send_message):
        mock_send_message.return_value = (True, "message-ts")
        ContentFactoryJob.objects.create(
            job_id="job-pr-opened-raw-preview",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
        )
        self._create_content_factory_run("job-pr-opened-raw-preview")

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "generation_pr_opened",
                "job_id": "job-pr-opened-raw-preview",
                "run_id": "job-pr-opened-raw-preview",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "pr_url": "https://github.com/example/pr/78",
                "pr_number": 78,
                "preview_url": "https://content-how-to-raise-your-first-million.example.dev/articles/featured/how-to-raise-your-first-million",
                "review_surface_kind": "fallback_bundle",
                "primary_review_url": "https://github.com/example/pr/78",
                "primary_review_label": "Open review PR",
                "route_is_live": False,
                "intended_route_path": "/articles/featured/how-to-raise-your-first-million",
                "bundle_primary_path": ".content-factory/drafts/how-to-raise-your-first-million/README.md",
                "review_required": True,
                "verification_state": "build_failed_review_pr",
                "reason_code": "environment_bundler_unstable",
                "dedupe_key": "generation-pr-opened-raw-preview-78",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-pr-opened-raw-preview")
        self.assertEqual(job.status, "needs_review")
        self.assertEqual(job.request_meta.get("preview_surface_kind"), "repo_preview")
        self.assertFalse(job.request_meta.get("preview_content_verified"))
        self.assertEqual(
            job.request_meta.get("repo_preview_candidate_url"),
            "https://content-how-to-raise-your-first-million.example.dev/articles/featured/how-to-raise-your-first-million",
        )
        self.assertEqual(job.request_meta.get("preview_url", ""), "")
        self.assertIn(
            "/api/content-factory/runs/job-pr-opened-raw-preview/preview/",
            job.request_meta.get("artifact_preview_url", ""),
        )
        self.assertEqual(job.request_meta.get("primary_action_url"), "https://github.com/example/pr/78")
        self.assertEqual(job.request_meta.get("primary_action_label"), "Open review PR")
        self.assertEqual(mock_send_message.call_count, 2)
        pr_notification_call = mock_send_message.call_args_list[1]
        self.assertIn("Review bundle ready for mlai.au:", pr_notification_call[0][1])
        blocks = pr_notification_call[1]["blocks"]
        self.assertIn("Review PR created", blocks[0]["text"]["text"])
        self.assertNotIn("Preview route:", blocks[0]["text"]["text"])
        button_texts = [element["text"]["text"] for element in blocks[1]["elements"]]
        self.assertEqual(button_texts[:3], ["Open review PR", "Open Evidence Preview", "Open Candidate Preview"])
        self.assertIn(
            "/api/content-factory/runs/job-pr-opened-raw-preview/preview/",
            blocks[1]["elements"][1]["url"],
        )
        self.assertIn("sig=", blocks[1]["elements"][1]["url"])

    @patch('integrations.services.slack.SlackService.send_message')
    @patch('integrations.services.article_generation.publish_article')
    def test_preview_ready_callback_posts_preview_reply_and_auto_approves_once(
        self,
        mock_publish_article,
        mock_send_message,
    ):
        mock_publish_article.return_value = {
            "job_id": "job-preview-ready",
            "status": "queued",
            "approval_state": "approved",
        }
        mock_send_message.return_value = (True, "message-ts")
        ContentFactoryJob.objects.create(
            job_id="job-preview-ready",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
        )

        url = reverse('content_factory_callback')
        data = {
            "event_type": "preview_ready",
            "job_id": "job-preview-ready",
            "domain": "mlai.au",
            "slack_user_id": "U123",
            "preview_url": "https://preview.example.com",
            "preview_surface_kind": "repo_preview",
            "preview_content_verified": True,
            "pr_url": "https://github.com/example/pr/1",
            "pr_number": 1,
            "review_surface_kind": "preview_route",
            "primary_review_url": "https://preview.example.com",
            "primary_review_label": "Open Preview",
            "route_is_live": True,
            "route_path": "/articles/ifs",
            "preview_screenshot_urls": [
                "https://storage.example.com/previews/job-preview-ready.png",
            ],
            "dedupe_key": "preview-ready-1",
        }

        response = self.client.post(url, data, format='json')
        duplicate_response = self.client.post(url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(duplicate_response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-preview-ready")
        self.assertEqual(job.status, "generating")
        self.assertEqual(job.pr_url, "https://github.com/example/pr/1")
        self.assertEqual(job.request_meta.get("publish_stage"), "auto_approved")
        self.assertEqual(job.request_meta.get("preview_surface_kind"), "repo_preview")
        self.assertEqual(
            job.request_meta.get("preview_screenshot_urls"),
            ["https://storage.example.com/previews/job-preview-ready.png"],
        )
        self.assertEqual(job.request_meta["callback_notifications"]["preview_ready"], ["preview-ready-1"])
        self.assertEqual(job.request_meta["callback_actions"]["preview_ready_auto_approve"], ["preview-ready-1"])
        mock_publish_article.assert_called_once_with(
            "job-preview-ready",
            slack_user_id="U123",
            domain="mlai.au",
        )
        mock_send_message.assert_called_once()
        self.assertEqual(mock_send_message.call_args[0][0], "C123")
        self.assertEqual(mock_send_message.call_args[1]["thread_ts"], "123.456")
        blocks = mock_send_message.call_args[1]["blocks"]
        self.assertIn("Preview ready", blocks[0]["text"]["text"])
        self.assertIn("Preview route: `/articles/ifs`", blocks[0]["text"]["text"])
        self.assertEqual(blocks[1]["elements"][0]["text"]["text"], "Open Preview")
        self.assertEqual(blocks[1]["elements"][1]["text"]["text"], "Open PR")
        self.assertEqual(blocks[2]["type"], "image")
        self.assertEqual(
            blocks[2]["image_url"],
            "https://storage.example.com/previews/job-preview-ready.png",
        )

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_content_ready_callback_marks_job_completed(self, mock_send_dm):
        url = reverse('content_factory_callback')
        data = {
            "event_type": "content_ready",
            "job_id": "job-content-ready",
            "domain": "mlai.au",
            "slack_user_id": "U123",
            "title": "How to Find a Technical Cofounder",
            "article_markdown_path": "/tmp/run/article.md",
        }

        response = self.client.post(url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-content-ready")
        self.assertEqual(job.status, "completed")
        mock_send_dm.assert_called_once()
        self.assertNotIn("/tmp/run/article.md", mock_send_dm.call_args[0][1])
        blocks = mock_send_dm.call_args[1]["blocks"]
        self.assertIn("How to Find a Technical Cofounder", blocks[0]["text"]["text"])
        self.assertNotIn("Artifact", blocks[0]["text"]["text"])

    @patch('integrations.services.article_generation.publish_article_as_pr')
    def test_run_control_promote_bundle_proxies_to_content_factory(self, mock_publish_article_as_pr):
        mock_publish_article_as_pr.return_value = {
            "status": "queued",
            "job_id": "job-publish-child",
            "source_run_id": "job-content-ready-promote",
        }
        self._create_content_factory_run("job-content-ready-promote")
        ContentFactoryJob.objects.create(
            job_id="job-content-ready-promote",
            domain="mlai.au",
            slack_user_id="U123",
            status="completed",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            slack_root_message_ts="123.456",
        )

        response = self.client.post(
            reverse('content_factory_run_control', args=["job-content-ready-promote", "promote-bundle"]),
            {},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["job_id"], "job-publish-child")
        mock_publish_article_as_pr.assert_called_once_with(
            "job-content-ready-promote",
            slack_user_id="U123",
            domain="mlai.au",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            slack_root_message_ts="123.456",
        )

    @patch('content_factory.content_views.publish_article_as_pr')
    def test_publish_pr_endpoint_proxies_to_child_publish_run(self, mock_publish_article_as_pr):
        mock_publish_article_as_pr.return_value = {
            "status": "queued",
            "job_id": "job-publish-child",
            "source_run_id": "job-content-ready-promote",
        }
        ContentFactoryJob.objects.create(
            job_id="job-content-ready-promote",
            domain="mlai.au",
            slack_user_id="U123",
            status="completed",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            slack_root_message_ts="123.456",
        )

        response = self.client.post(
            reverse('content_job_publish_pr', args=["job-content-ready-promote"]),
            {"slack_user_id": "U123"},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["job_id"], "job-publish-child")
        mock_publish_article_as_pr.assert_called_once_with(
            "job-content-ready-promote",
            slack_user_id="U123",
            domain="mlai.au",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            slack_root_message_ts="123.456",
        )

    @patch('content_factory.content_views.publish_article_as_pr')
    def test_publish_pr_endpoint_forwards_requested_by_for_delegated_job(self, mock_publish_article_as_pr):
        mock_publish_article_as_pr.return_value = {
            "status": "queued",
            "job_id": "job-publish-child",
            "source_run_id": "job-content-ready-promote",
        }
        ContentFactoryJob.objects.create(
            job_id="job-content-ready-promote",
            domain="mlai.au",
            slack_user_id="U_EFFECTIVE",
            status="completed",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            slack_root_message_ts="123.456",
        )

        response = self.client.post(
            reverse('content_job_publish_pr', args=["job-content-ready-promote"]),
            {"slack_user_id": "U_EFFECTIVE", "requested_by_slack_user_id": "U_REQUESTER"},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_publish_article_as_pr.assert_called_once_with(
            "job-content-ready-promote",
            slack_user_id="U_EFFECTIVE",
            requested_by_slack_user_id="U_REQUESTER",
            domain="mlai.au",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            slack_root_message_ts="123.456",
        )

    @patch('content_factory.content_views.publish_article_as_pr')
    def test_publish_pr_endpoint_omits_requested_by_when_same_as_effective_user(self, mock_publish_article_as_pr):
        mock_publish_article_as_pr.return_value = {
            "status": "queued",
            "job_id": "job-publish-child",
            "source_run_id": "job-content-ready-promote",
        }
        ContentFactoryJob.objects.create(
            job_id="job-content-ready-promote",
            domain="mlai.au",
            slack_user_id="U123",
            status="completed",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            slack_root_message_ts="123.456",
            request_meta={"requested_by_slack_user_id": "U_REQUESTER"},
        )

        response = self.client.post(
            reverse('content_job_publish_pr', args=["job-content-ready-promote"]),
            {"slack_user_id": "U123", "requested_by_slack_user_id": "U123"},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_publish_article_as_pr.assert_called_once_with(
            "job-content-ready-promote",
            slack_user_id="U123",
            domain="mlai.au",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            slack_root_message_ts="123.456",
        )

    @patch('integrations.services.article_generation.publish_article_as_pr')
    def test_run_control_promote_bundle_forwards_requested_by_from_job_request_meta(self, mock_publish_article_as_pr):
        mock_publish_article_as_pr.return_value = {
            "status": "queued",
            "job_id": "job-publish-child",
            "source_run_id": "job-content-ready-promote",
        }
        self._create_content_factory_run("job-content-ready-promote")
        ContentFactoryJob.objects.create(
            job_id="job-content-ready-promote",
            domain="mlai.au",
            slack_user_id="U_EFFECTIVE",
            status="completed",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            slack_root_message_ts="123.456",
            request_meta={"requested_by_slack_user_id": "U_REQUESTER"},
        )

        response = self.client.post(
            reverse('content_factory_run_control', args=["job-content-ready-promote", "promote-bundle"]),
            {},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_publish_article_as_pr.assert_called_once_with(
            "job-content-ready-promote",
            slack_user_id="U_EFFECTIVE",
            requested_by_slack_user_id="U_REQUESTER",
            domain="mlai.au",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            slack_root_message_ts="123.456",
        )

    @patch('integrations.services.article_generation.publish_article_as_pr')
    def test_run_control_promote_bundle_omits_requested_by_when_job_request_meta_matches_effective_user(self, mock_publish_article_as_pr):
        mock_publish_article_as_pr.return_value = {
            "status": "queued",
            "job_id": "job-publish-child",
            "source_run_id": "job-content-ready-promote",
        }
        self._create_content_factory_run("job-content-ready-promote")
        ContentFactoryJob.objects.create(
            job_id="job-content-ready-promote",
            domain="mlai.au",
            slack_user_id="U123",
            status="completed",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            slack_root_message_ts="123.456",
            request_meta={"requested_by_slack_user_id": "U123"},
        )

        response = self.client.post(
            reverse('content_factory_run_control', args=["job-content-ready-promote", "promote-bundle"]),
            {},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_publish_article_as_pr.assert_called_once_with(
            "job-content-ready-promote",
            slack_user_id="U123",
            domain="mlai.au",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            slack_root_message_ts="123.456",
        )

    @patch('integrations.services.slack.SlackService.send_message')
    def test_article_progress_callback_posts_thread_reply_and_records_progress(self, mock_send_message):
        mock_send_message.return_value = (True, "live-progress-card")
        ContentFactoryJob.objects.create(
            job_id="job-progress",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
        )

        url = reverse('content_factory_callback')
        data = {
            "event_type": "article_progress",
            "job_id": "job-progress",
            "domain": "mlai.au",
            "slack_user_id": "U123",
            "progress_id": "job-progress:research_locked",
            "milestone_key": "research_locked",
            "milestone_index": 1,
            "milestone_count": 3,
            "message": "Research complete. Sources are gathered and the outline is locked.",
        }

        response = self.client.post(url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-progress")
        self.assertEqual(job.posted_progress_ids, ["job-progress:research_locked"])
        self.assertEqual(job.last_progress_milestone_index, 1)
        self.assertEqual(mock_send_message.call_count, 2)
        self.assertEqual(mock_send_message.call_args_list[0][0][0], "C123")
        self.assertIn(
            "Research complete. Sources are gathered and the outline is locked.",
            mock_send_message.call_args_list[0][0][1],
        )
        self.assertEqual(mock_send_message.call_args_list[0][1]["thread_ts"], "123.456")
        blocks = mock_send_message.call_args_list[0][1]["blocks"]
        self.assertIn("Research locked", blocks[0]["text"]["text"])
        self.assertIn(
            "Research complete. Sources are gathered and the outline is locked.",
            blocks[0]["text"]["text"],
        )
        live_card_blocks = mock_send_message.call_args_list[1][1]["blocks"]
        self.assertIn("Content Factory for mlai.au", live_card_blocks[0]["text"]["text"])

    @patch('integrations.services.slack.SlackService.send_message')
    def test_discovery_progress_callback_posts_thread_reply_and_records_progress(self, mock_send_message):
        mock_send_message.return_value = (True, "live-discovery-card")
        ContentFactoryJob.objects.create(
            job_id="job-discovery-progress",
            domain="mlai.au",
            slack_user_id="U123",
            status="queued",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "discovery_progress",
                "job_id": "job-discovery-progress",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "progress_id": "job-discovery-progress:research_started",
                "milestone_key": "research_started",
                "milestone_index": 1,
                "milestone_count": 2,
                "message": "Research started. Context, competitors, and prior topic history are loaded. I'm now researching candidate topics.",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-discovery-progress")
        self.assertEqual(job.status, "researching")
        self.assertEqual(job.posted_progress_ids, ["job-discovery-progress:research_started"])
        self.assertEqual(job.last_progress_milestone_index, 1)
        self.assertEqual(mock_send_message.call_count, 2)
        self.assertEqual(mock_send_message.call_args_list[0][0][0], "C123")
        self.assertIn(
            "Research started. Context, competitors, and prior topic history are loaded.",
            mock_send_message.call_args_list[0][0][1],
        )
        self.assertEqual(mock_send_message.call_args_list[0][1]["thread_ts"], "123.456")
        self.assertIn(
            "Research started. Context, competitors, and prior topic history are loaded.",
            mock_send_message.call_args_list[0][1]["blocks"][0]["text"]["text"],
        )
        self.assertIn(
            "Content Factory for mlai.au",
            mock_send_message.call_args_list[1][1]["blocks"][0]["text"]["text"],
        )

    @patch('integrations.services.slack.SlackService.send_message')
    def test_discovery_progress_callback_dedupes_progress_id(self, mock_send_message):
        ContentFactoryJob.objects.create(
            job_id="job-discovery-dup",
            domain="mlai.au",
            slack_user_id="U123",
            status="researching",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            posted_progress_ids=["job-discovery-dup:research_started"],
            last_progress_milestone_index=1,
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "discovery_progress",
                "job_id": "job-discovery-dup",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "progress_id": "job-discovery-dup:research_started",
                "milestone_key": "research_started",
                "milestone_index": 1,
                "message": "Research started. Context, competitors, and prior topic history are loaded. I'm now researching candidate topics.",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reason"], "duplicate_progress_id")
        mock_send_message.assert_not_called()

    @patch('integrations.services.slack.SlackService.send_message')
    def test_scan_progress_callback_posts_thread_reply_and_records_progress(self, mock_send_message):
        mock_send_message.return_value = (True, "live-scan-card")
        ContentFactoryJob.objects.create(
            job_id="job-scan-progress",
            domain="mlai.au",
            slack_user_id="U123",
            status="queued",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            request_meta={"type": "scan", "github_repo": "acme/site"},
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "scan_progress",
                "job_id": "job-scan-progress",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "progress_id": "job-scan-progress:repo_analysis",
                "milestone_key": "repo_analysis",
                "milestone_index": 1,
                "milestone_count": 3,
                "message": "Scan started. Inspecting repository structure and dependencies.",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-scan-progress")
        self.assertEqual(job.status, "researching")
        self.assertEqual(job.posted_progress_ids, ["job-scan-progress:repo_analysis"])
        self.assertEqual(job.last_progress_milestone_index, 1)
        self.assertEqual(job.last_progress_milestone_key, "repo_analysis")
        self.assertEqual(mock_send_message.call_count, 2)
        self.assertEqual(mock_send_message.call_args_list[0][0][0], "C123")
        self.assertIn(
            "Scan started. Inspecting repository structure and dependencies.",
            mock_send_message.call_args_list[0][0][1],
        )
        self.assertEqual(mock_send_message.call_args_list[0][1]["thread_ts"], "123.456")
        blocks = mock_send_message.call_args_list[0][1]["blocks"]
        self.assertIn("Inspecting repository", blocks[0]["text"]["text"])
        self.assertIn("Scan started. Inspecting repository structure and dependencies.", blocks[0]["text"]["text"])
        live_card_blocks = mock_send_message.call_args_list[1][1]["blocks"]
        self.assertIn("Content Factory for mlai.au", live_card_blocks[0]["text"]["text"])
        self.assertIn("Preparing scan", live_card_blocks[0]["text"]["text"])
        self.assertIn("Inspecting repo", live_card_blocks[0]["text"]["text"])
        self.assertNotIn("Writing draft", live_card_blocks[0]["text"]["text"])

    @patch('integrations.services.slack.SlackService.update_message')
    @patch('integrations.services.slack.SlackService.send_message')
    def test_article_progress_callback_posts_thread_reply_and_updates_existing_live_card(
        self,
        mock_send_message,
        mock_update_message,
    ):
        mock_send_message.return_value = (True, "thread-reply-ts")
        mock_update_message.return_value = True
        ContentFactoryJob.objects.create(
            job_id="job-progress-followup",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            progress_message_ts="live-progress-card",
            posted_progress_ids=["job-progress-followup:research_locked"],
            last_progress_milestone_index=1,
            last_progress_milestone_key="research_locked",
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "article_progress",
                "job_id": "job-progress-followup",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "progress_id": "job-progress-followup:draft_grounded",
                "milestone_key": "draft_grounded",
                "milestone_index": 2,
                "milestone_count": 3,
                "message": "Draft written and grounded to sources.",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-progress-followup")
        self.assertEqual(
            job.posted_progress_ids,
            ["job-progress-followup:research_locked", "job-progress-followup:draft_grounded"],
        )
        self.assertEqual(job.last_progress_milestone_index, 2)
        mock_send_message.assert_called_once()
        self.assertIn("Draft written and grounded to sources.", mock_send_message.call_args[0][1])
        self.assertEqual(mock_send_message.call_args[1]["thread_ts"], "123.456")
        self.assertEqual(mock_update_message.call_count, 1)
        self.assertEqual(mock_update_message.call_args[0][0], "C123")
        self.assertEqual(mock_update_message.call_args[0][1], "live-progress-card")
        self.assertIn("Draft written and grounded to sources.", mock_update_message.call_args[0][2])

    @patch('integrations.services.slack.SlackService.send_message')
    def test_discovery_progress_callback_ignores_stale_milestone(self, mock_send_message):
        ContentFactoryJob.objects.create(
            job_id="job-discovery-stale",
            domain="mlai.au",
            slack_user_id="U123",
            status="researching",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            posted_progress_ids=["job-discovery-stale:candidate_pool_ready"],
            last_progress_milestone_index=2,
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "discovery_progress",
                "job_id": "job-discovery-stale",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "progress_id": "job-discovery-stale:research_started",
                "milestone_key": "research_started",
                "milestone_index": 1,
                "message": "Research started. Context, competitors, and prior topic history are loaded. I'm now researching candidate topics.",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reason"], "stale_milestone")
        mock_send_message.assert_not_called()

    @patch('integrations.services.slack.SlackService.send_message')
    def test_discovery_progress_callback_missing_thread_context_is_safe(self, mock_send_message):
        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "discovery_progress",
                "job_id": "job-discovery-missing-thread",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "progress_id": "job-discovery-missing-thread:research_started",
                "milestone_key": "research_started",
                "milestone_index": 1,
                "message": "Research started. Context, competitors, and prior topic history are loaded. I'm now researching candidate topics.",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reason"], "missing_thread_context")
        mock_send_message.assert_not_called()

    @patch('integrations.services.slack.SlackService.send_message')
    def test_article_progress_callback_dedupes_progress_id(self, mock_send_message):
        ContentFactoryJob.objects.create(
            job_id="job-progress-dup",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            posted_progress_ids=["job-progress-dup:research_locked"],
            last_progress_milestone_index=1,
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "article_progress",
                "job_id": "job-progress-dup",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "progress_id": "job-progress-dup:research_locked",
                "milestone_key": "research_locked",
                "milestone_index": 1,
                "message": "Research complete. Sources are gathered and the outline is locked.",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reason"], "duplicate_progress_id")
        mock_send_message.assert_not_called()

    @patch('integrations.services.slack.SlackService.send_message')
    def test_article_progress_callback_ignores_stale_milestone(self, mock_send_message):
        ContentFactoryJob.objects.create(
            job_id="job-progress-stale",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            posted_progress_ids=["job-progress-stale:draft_grounded"],
            last_progress_milestone_index=2,
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "article_progress",
                "job_id": "job-progress-stale",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "progress_id": "job-progress-stale:research_locked",
                "milestone_key": "research_locked",
                "milestone_index": 1,
                "message": "Research complete. Sources are gathered and the outline is locked.",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reason"], "stale_milestone")
        mock_send_message.assert_not_called()

    @patch('integrations.services.slack.SlackService.send_message')
    def test_article_progress_callback_missing_thread_context_is_safe(self, mock_send_message):
        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "article_progress",
                "job_id": "job-progress-missing-thread",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "progress_id": "job-progress-missing-thread:research_locked",
                "milestone_key": "research_locked",
                "milestone_index": 1,
                "message": "Research complete. Sources are gathered and the outline is locked.",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reason"], "missing_thread_context")
        mock_send_message.assert_not_called()

    @patch('integrations.services.slack.SlackService.update_message')
    @patch('integrations.services.slack.SlackService.send_message')
    def test_content_ready_callback_uses_root_message_ts_as_thread_fallback(self, mock_send_message, mock_update_message):
        mock_send_message.return_value = (True, "live-content-card")
        mock_update_message.return_value = True
        self._create_content_factory_run("job-content-threaded", content_package=self._sample_content_package(long_section=True))
        ContentFactoryJob.objects.create(
            job_id="job-content-threaded",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="",
            progress_message_ts="progress-ts",
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "content_ready",
                "job_id": "job-content-threaded",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "title": "How to Find a Technical Cofounder",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(mock_update_message.called)
        self.assertGreaterEqual(mock_send_message.call_count, 5)
        first_call = mock_send_message.call_args_list[0]
        self.assertEqual(first_call[0][0], "C123")
        self.assertEqual(first_call[1]["thread_ts"], "123.456")
        first_blocks = first_call[1]["blocks"]
        self.assertIn("Article content ready", first_blocks[0]["text"]["text"])
        publish_block = next(block for block in first_blocks if block.get("block_id") == "content_ready_publish_actions")
        publish_button = publish_block["elements"][0]
        self.assertEqual(publish_button["action_id"], "publish_content_pr")
        publish_value = json.loads(publish_button["value"])
        self.assertEqual(publish_value["job_id"], "job-content-threaded")
        self.assertEqual(publish_value["domain"], "mlai.au")
        self.assertEqual(publish_value["slack_user_id"], "U123")
        self.assertEqual(publish_value["channel_id"], "C123")
        self.assertEqual(publish_value["thread_ts"], "123.456")
        self.assertEqual(first_blocks[-1]["elements"][0]["text"]["text"], "Open Preview")
        self.assertIn("/api/content-factory/runs/job-content-threaded/preview", first_blocks[-1]["elements"][0]["url"])
        combined_text = "\n".join(
            json.dumps(call[1].get("blocks") or [])
            for call in mock_send_message.call_args_list
        )
        self.assertNotIn("/tmp/run/article.md", combined_text)
        self.assertIn("inline-1.jpg", combined_text)
        self.assertIn("Quick Summary", combined_text)

    @patch('integrations.services.slack.SlackService.update_message')
    @patch('integrations.services.slack.SlackService.send_message')
    def test_content_ready_callback_prefers_callback_content_package_when_run_result_is_not_yet_mirrored(
        self,
        mock_send_message,
        mock_update_message,
    ):
        mock_send_message.return_value = (True, "live-content-card")
        mock_update_message.return_value = True
        ContentFactoryRun.objects.create(
            run_id="job-content-callback-package",
            workflow="direct_generate",
            domain="mlai.au",
            status="completed",
            current_step="finalize",
            artifact_root="/tmp/content-factory-runs/job-content-callback-package",
            result={"status": "success"},
            run_request={"domain": "mlai.au"},
            acceptance_summary={"content_packaged": True},
            verification_summary={"all_required_passed": True},
        )
        ContentFactoryJob.objects.create(
            job_id="job-content-callback-package",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            progress_message_ts="progress-ts",
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "content_ready",
                "job_id": "job-content-callback-package",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "title": "How to Find a Technical Cofounder",
                "content_package": self._sample_content_package(long_section=True),
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(mock_update_message.called)
        self.assertGreaterEqual(mock_send_message.call_count, 5)
        first_blocks = mock_send_message.call_args_list[0][1]["blocks"]
        publish_block = next(block for block in first_blocks if block.get("block_id") == "content_ready_publish_actions")
        publish_button = publish_block["elements"][0]
        self.assertEqual(publish_button["action_id"], "publish_content_pr")
        combined_text = "\n".join(
            json.dumps(call[1].get("blocks") or [])
            for call in mock_send_message.call_args_list
        )
        self.assertIn("inline-1.jpg", combined_text)
        self.assertIn("Quick Summary", combined_text)

    def test_resolve_thread_returns_ready_source_job(self):
        ContentFactoryJob.objects.create(
            job_id="job-content-ready",
            domain="mlai.au",
            slack_user_id="U123",
            status="completed",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            request_meta={"publish_stage": "content_ready"},
        )

        response = self.client.post(
            reverse('content_job_resolve_thread'),
            {
                "slack_user_id": "U123",
                "slack_channel_id": "C123",
                "slack_thread_ts": "123.456",
                "requested_action": "publish_pr",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["resolution"], "ready")
        self.assertEqual(response.data["job_id"], "job-content-ready")
        self.assertEqual(response.data["publish_stage"], "content_ready")

    def test_resolve_thread_returns_existing_publish_child_when_promotion_in_progress(self):
        ContentFactoryJob.objects.create(
            job_id="job-content-ready",
            domain="mlai.au",
            slack_user_id="U123",
            status="completed",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            request_meta={
                "publish_stage": "promotion_requested",
                "promoted_publish_job_id": "job-publish-child",
            },
        )
        ContentFactoryJob.objects.create(
            job_id="job-publish-child",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            request_meta={
                "source_run_id": "job-content-ready",
                "publish_stage": "awaiting_preview",
            },
        )

        response = self.client.post(
            reverse('content_job_resolve_thread'),
            {
                "slack_user_id": "U123",
                "slack_channel_id": "C123",
                "slack_thread_ts": "123.456",
                "requested_action": "publish_pr",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["resolution"], "in_progress")
        self.assertEqual(response.data["job_id"], "job-content-ready")
        self.assertEqual(response.data["promoted_publish_job_id"], "job-publish-child")
        self.assertEqual(response.data["publish_stage"], "awaiting_preview")

    def test_resolve_thread_returns_existing_publish_child_when_review_is_required(self):
        ContentFactoryJob.objects.create(
            job_id="job-content-ready",
            domain="mlai.au",
            slack_user_id="U123",
            status="completed",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            request_meta={
                "publish_stage": "needs_review",
                "promoted_publish_job_id": "job-publish-child",
            },
        )
        ContentFactoryJob.objects.create(
            job_id="job-publish-child",
            domain="mlai.au",
            slack_user_id="U123",
            status="completed",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            request_meta={
                "source_run_id": "job-content-ready",
                "publish_stage": "needs_review",
            },
        )

        response = self.client.post(
            reverse('content_job_resolve_thread'),
            {
                "slack_user_id": "U123",
                "slack_channel_id": "C123",
                "slack_thread_ts": "123.456",
                "requested_action": "publish_pr",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["resolution"], "in_progress")
        self.assertEqual(response.data["job_id"], "job-content-ready")
        self.assertEqual(response.data["promoted_publish_job_id"], "job-publish-child")
        self.assertEqual(response.data["publish_stage"], "needs_review")

    def test_resolve_thread_prefers_existing_publish_child_over_older_content_ready_job(self):
        ContentFactoryJob.objects.create(
            job_id="job-content-ready-older",
            domain="mlai.au",
            slack_user_id="U123",
            status="completed",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            request_meta={"publish_stage": "content_ready"},
        )
        ContentFactoryJob.objects.create(
            job_id="job-content-ready-current",
            domain="mlai.au",
            slack_user_id="U123",
            status="completed",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            request_meta={
                "publish_stage": "pr_opened",
                "promoted_publish_job_id": "job-publish-child-current",
            },
        )
        ContentFactoryJob.objects.create(
            job_id="job-publish-child-current",
            domain="mlai.au",
            slack_user_id="U123",
            status="completed",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            request_meta={
                "source_run_id": "job-content-ready-current",
                "publish_stage": "pr_opened",
            },
        )

        response = self.client.post(
            reverse('content_job_resolve_thread'),
            {
                "slack_user_id": "U123",
                "slack_channel_id": "C123",
                "slack_thread_ts": "123.456",
                "requested_action": "publish_pr",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["resolution"], "in_progress")
        self.assertEqual(response.data["job_id"], "job-content-ready-current")
        self.assertEqual(response.data["promoted_publish_job_id"], "job-publish-child-current")
        self.assertEqual(response.data["publish_stage"], "pr_opened")

    def test_resolve_thread_returns_404_when_no_promotable_source_job_exists(self):
        ContentFactoryJob.objects.create(
            job_id="job-publish-child",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            request_meta={
                "source_run_id": "job-content-ready",
                "publish_stage": "awaiting_preview",
            },
        )

        response = self.client.post(
            reverse('content_job_resolve_thread'),
            {
                "slack_user_id": "U123",
                "slack_channel_id": "C123",
                "slack_thread_ts": "123.456",
                "requested_action": "publish_pr",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("No promotable content-ready article", response.data["error"])

    def test_resolve_thread_returns_active_confirm_child_for_confirm_topic(self):
        ContentFactoryJob.objects.create(
            job_id="job-parent-confirm",
            domain="mlai.au",
            slack_user_id="U123",
            status="confirmed",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
        )
        ContentFactoryJob.objects.create(
            job_id="job-child-confirm",
            domain="mlai.au",
            slack_user_id="U123",
            status="blocked",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            error_message="[token_refresh_unavailable] Token refresh is temporarily unavailable.",
            request_meta={
                "source_run_id": "job-parent-confirm",
                "blocked_error_code": "token_refresh_unavailable",
            },
        )

        response = self.client.post(
            reverse('content_job_resolve_thread'),
            {
                "slack_user_id": "U123",
                "slack_channel_id": "C123",
                "slack_thread_ts": "123.456",
                "requested_action": "confirm_topic",
                "job_id": "job-parent-confirm",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["requested_action"], "confirm_topic")
        self.assertEqual(response.data["job_id"], "job-child-confirm")
        self.assertEqual(response.data["status"], "blocked")
        self.assertEqual(response.data["error_code"], "token_refresh_unavailable")

    def test_seo_written_article_create_updates_existing_slug_instead_of_raising(self):
        org = Organization.objects.create(name="MLAI", domain="mlai.au")
        article = WrittenArticle.objects.create(
            organization=org,
            title="Original title",
            slug="how-to-find-a-technical-cofounder",
            category="featured",
            primary_keyword="technical cofounder",
        )

        response = self.client.post(
            reverse('seo_article_create'),
            {
                "domain": "mlai.au",
                "title": "Updated title",
                "slug": "how-to-find-a-technical-cofounder",
                "category": "news",
                "primary_keyword": "technical cofounder checklist",
                "article_url": "https://mlai.au/articles/how-to-find-a-technical-cofounder",
                "pr_url": "https://github.com/example/repo/pull/12",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "updated")
        self.assertEqual(WrittenArticle.objects.filter(organization=org, slug=article.slug).count(), 1)
        article.refresh_from_db()
        self.assertEqual(article.title, "Updated title")
        self.assertEqual(article.category, "news")
        self.assertEqual(article.primary_keyword, "technical cofounder checklist")
        self.assertEqual(article.article_url, "https://mlai.au/articles/how-to-find-a-technical-cofounder")
        self.assertEqual(article.pr_url, "https://github.com/example/repo/pull/12")

    def test_seo_written_article_list_returns_written_article_records(self):
        org = Organization.objects.create(name="MLAI", domain="mlai.au")
        WrittenArticle.objects.create(
            organization=org,
            title="What Is Artificial Intelligence With Example",
            slug="what-is-artificial-intelligence-with-example",
            category="featured",
            primary_keyword="what is artificial intelligence with example",
            article_url="https://mlai.au/articles/what-is-artificial-intelligence-with-example",
        )

        response = self.client.get("/api/seo/articles/?domain=mlai.au")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["articles"][0]["primary_keyword"], "what is artificial intelligence with example")
        self.assertEqual(response.data["articles"][0]["title"], "What Is Artificial Intelligence With Example")

    def test_content_preview_route_renders_signed_article_html(self):
        self._create_content_factory_run("run-preview-1")
        signature = build_content_factory_preview_signature("run-preview-1")

        response = self.client.get(
            reverse("content_factory_run_preview", args=["run-preview-1"]),
            {"sig": signature},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertContains(response, "How to Choose Cattle Scales for Accurate Livestock Weighing")
        self.assertContains(response, "Delivered by MLAI Content Factory")

    def test_content_preview_route_rejects_invalid_signature(self):
        self._create_content_factory_run("run-preview-invalid")

        response = self.client.get(
            reverse("content_factory_run_preview", args=["run-preview-invalid"]),
            {"sig": "bad-signature"},
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertContains(response, "preview link is invalid", status_code=status.HTTP_403_FORBIDDEN)

    @patch("content_factory.service_views.validate_content_factory_preview_signature", side_effect=signing.SignatureExpired("expired"))
    def test_content_preview_route_rejects_expired_signature(self, _mock_validate):
        self._create_content_factory_run("run-preview-expired")

        response = self.client.get(
            reverse("content_factory_run_preview", args=["run-preview-expired"]),
            {"sig": "expired"},
        )

        self.assertEqual(response.status_code, status.HTTP_410_GONE)
        self.assertContains(response, "preview link has expired", status_code=status.HTTP_410_GONE)

    def test_content_preview_route_handles_missing_images(self):
        self._create_content_factory_run(
            "run-preview-no-images",
            content_package=self._sample_content_package(include_images=False),
        )
        signature = build_content_factory_preview_signature("run-preview-no-images")

        response = self.client.get(
            reverse("content_factory_run_preview", args=["run-preview-no-images"]),
            {"sig": signature},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertContains(response, "How to Choose Cattle Scales for Accurate Livestock Weighing")

    def test_content_preview_route_supports_nested_result_content_package(self):
        run = self._create_content_factory_run("run-preview-nested")
        run.result = {
            "status": "needs_review",
            "result": {
                "content_package": self._sample_content_package(),
            },
        }
        run.save(update_fields=["result", "updated_at"])
        signature = build_content_factory_preview_signature("run-preview-nested")

        response = self.client.get(
            reverse("content_factory_run_preview", args=["run-preview-nested"]),
            {"sig": signature},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertContains(response, "How to Choose Cattle Scales for Accurate Livestock Weighing")

    @patch('integrations.services.slack.SlackService.send_message')
    def test_publish_bundle_ready_callback_posts_thread_reply(self, mock_send_message):
        mock_send_message.return_value = (True, "live-bundle-card")
        ContentFactoryJob.objects.create(
            job_id="job-bundle-ready",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "publish_bundle_ready",
                "job_id": "job-bundle-ready",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "title": "How to Find a Technical Cofounder",
                "publish_resolution": "patch_bundle",
                "suggested_target_path": "content/blog/how-to-find-a-technical-cofounder.mdx",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-bundle-ready")
        self.assertEqual(job.status, "completed")
        self.assertEqual(mock_send_message.call_count, 2)
        self.assertEqual(mock_send_message.call_args_list[0][0][0], "C123")
        self.assertEqual(mock_send_message.call_args_list[0][1]["thread_ts"], "123.456")
        self.assertIn(
            "Publish bundle is ready.",
            mock_send_message.call_args_list[0][1]["blocks"][0]["text"]["text"],
        )
        self.assertEqual(mock_send_message.call_args_list[1][0][0], "C123")
        self.assertEqual(mock_send_message.call_args_list[1][1]["thread_ts"], "123.456")

    def test_article_review_ready_callback_is_processed(self):
        ContentFactoryJob.objects.create(
            job_id="job-review-ready",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "article_review_ready",
                "job_id": "job-review-ready",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "live_preview_url": "/api/runs/job-review-ready/live-preview",
                "component_manifest_path": "steps/render_article/artifacts/article_component_manifest.json",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-review-ready")
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.request_meta["publish_stage"], "article_review_ready")
        self.assertEqual(job.request_meta["live_preview_url"], "/api/runs/job-review-ready/live-preview")


class ArticleGenerationStatusTests(TestCase):
    @patch('integrations.services.article_generation.upsert_live_progress_card')
    @patch('integrations.services.slack.SlackService.send_message')
    @patch('integrations.services.slack.SlackService.send_dm')
    @patch('integrations.services.article_generation._handle_status_failure')
    def test_check_generation_status_marks_blocked_run_without_terminal_failure(
        self,
        mock_handle_status_failure,
        mock_send_dm,
        mock_send_message,
        mock_upsert_live_progress_card,
    ):
        from integrations.services import article_generation

        ContentFactoryJob.objects.create(
            job_id="blocked-poll-run-1",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
        )

        class _FakeResponse:
            status_code = 200
            text = "ok"

            def json(self):
                return {
                    "job_id": "blocked-poll-run-1",
                    "run_id": "blocked-poll-run-1",
                    "status": "blocked",
                    "current_step": "verify_build",
                    "error_code": "verifier_capacity_unavailable",
                    "error": "Dedicated verifier worker `build-verifier` is unavailable; verify_build is blocked until capacity returns.",
                    "preferred_queue": "build-verifier",
                    "fallback_policy": "auto_fallback",
                    "retry_after_seconds": 60,
                }

        with patch('integrations.services.article_generation.http_requests.get', return_value=_FakeResponse()):
            result = article_generation.check_generation_status("blocked-poll-run-1")

        self.assertEqual(result["status"], "blocked")
        mock_handle_status_failure.assert_not_called()
        job = ContentFactoryJob.objects.get(job_id="blocked-poll-run-1")
        self.assertEqual(job.status, "blocked")
        self.assertIn("verifier_capacity_unavailable", job.error_message)
        self.assertEqual(job.request_meta.get("blocked_step"), "verify_build")
        mock_upsert_live_progress_card.assert_called_once()
        mock_send_dm.assert_not_called()
        mock_send_message.assert_not_called()

    @patch('integrations.services.article_generation.upsert_live_progress_card')
    @patch('integrations.services.slack.SlackService.send_message')
    @patch('integrations.services.slack.SlackService.send_dm')
    @patch('integrations.services.article_generation._handle_status_failure')
    def test_check_generation_status_notifies_once_when_dependency_recovery_is_exhausted(
        self,
        mock_handle_status_failure,
        mock_send_dm,
        mock_send_message,
        mock_upsert_live_progress_card,
    ):
        from integrations.services import article_generation

        ContentFactoryJob.objects.create(
            job_id="blocked-poll-exhausted-1",
            domain="skedy.io",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            request_meta={"domain": "skedy.io"},
        )

        class _FakeResponse:
            status_code = 200
            text = "ok"

            def json(self):
                return {
                    "job_id": "blocked-poll-exhausted-1",
                    "run_id": "blocked-poll-exhausted-1",
                    "status": "blocked",
                    "current_step": "validate_render_dependencies",
                    "blocked_step": "validate_render_dependencies",
                    "error_code": "article_dependency_strategy_unresolved",
                    "error": "Article dependency strategy is unresolved",
                    "next_step": "synthesize_repository_contract",
                    "rerunnable_step": "synthesize_repository_contract",
                    "recovery_attempt": 2,
                    "recovery_exhausted": True,
                }

        with patch('integrations.services.article_generation.http_requests.get', return_value=_FakeResponse()):
            first = article_generation.check_generation_status("blocked-poll-exhausted-1")
            second = article_generation.check_generation_status("blocked-poll-exhausted-1")

        self.assertEqual(first["status"], "blocked")
        self.assertEqual(second["status"], "blocked")
        mock_handle_status_failure.assert_not_called()
        job = ContentFactoryJob.objects.get(job_id="blocked-poll-exhausted-1")
        self.assertEqual(job.status, "blocked")
        self.assertEqual(job.request_meta.get("blocked_next_step"), "synthesize_repository_contract")
        self.assertEqual(job.request_meta.get("blocked_rerunnable_step"), "synthesize_repository_contract")
        self.assertEqual(job.request_meta.get("blocked_recovery_attempt"), 2)
        self.assertTrue(job.request_meta.get("blocked_recovery_exhausted"))
        self.assertTrue(job.request_meta.get("blocked_visible_notification_key"))
        self.assertEqual(mock_send_message.call_count, 1)
        self.assertEqual(mock_send_message.call_args.kwargs["thread_ts"], "123.456")
        self.assertIn("skedy.io", mock_send_message.call_args.args[1])
        mock_send_dm.assert_not_called()
        self.assertEqual(mock_upsert_live_progress_card.call_count, 2)

    @patch('integrations.services.article_generation._handle_status_failure')
    def test_check_generation_status_marks_needs_review_run_without_terminal_failure(self, mock_handle_status_failure):
        from integrations.services import article_generation

        ContentFactoryJob.objects.create(
            job_id="needs-review-run-1",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
        )

        class _FakeResponse:
            status_code = 200
            text = "ok"

            def json(self):
                return {
                    "job_id": "needs-review-run-1",
                    "run_id": "needs-review-run-1",
                    "status": "needs_review",
                    "pr_url": "https://github.com/example/pr/55",
                    "verification_state": "unsupported_runtime",
                    "reason_code": "unsupported_runtime",
                }

        with patch('integrations.services.article_generation.http_requests.get', return_value=_FakeResponse()):
            result = article_generation.check_generation_status("needs-review-run-1")

        self.assertEqual(result["status"], "needs_review")
        mock_handle_status_failure.assert_not_called()
        job = ContentFactoryJob.objects.get(job_id="needs-review-run-1")
        self.assertEqual(job.status, "needs_review")
        self.assertEqual(job.pr_url, "https://github.com/example/pr/55")
        self.assertEqual(job.request_meta.get("publish_stage"), "needs_review")
        self.assertEqual(job.request_meta.get("reason_code"), "unsupported_runtime")


class TopicConfirmTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "test_roo_key"
        os.environ['ROO_API_KEY'] = self.api_key
        
        from django.conf import settings
        settings.ROO_API_KEY = self.api_key
        settings.CONTENT_FACTORY_URL = "http://content-factory.test"
        
        self.client.credentials(HTTP_X_API_KEY=self.api_key)
        
        self.integration = UserIntegration.objects.create(
            slack_user_id="U-CONFIRM",
            github_access_token="gh_token",
            github_repo="owner/repo",
        )
        self.organization = Organization.objects.create(
            name="MLAI",
            domain="mlai.au",
        )
        OrganizationContentConfig.objects.create(
            organization=self.organization,
            github_repo="owner/repo",
        )

    def _mock_response(self, status_code, body):
        response = Response()
        response.status_code = status_code
        response._content = json.dumps(body).encode()
        response.headers['Content-Type'] = 'application/json'
        return response

    def test_confirm_topic_success(self):
        url = reverse('content_confirm')
        data = {
            "domain": "mlai.au",
            "confirmed_keyword": "ai agents",
            "slack_user_id": "U-CONFIRM",
            "request_source": "roo_slackbot",
        }
        
        with patch('content_factory.content_views.confirm_topic') as mock_confirm_topic:
            mock_confirm_topic.return_value = {"job_id": "job-new", "status": "queued"}
            
            response = self.client.post(url, data, format='json')
            
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data['job_id'], "job-new")
        
        mock_confirm_topic.assert_called_once_with(
            domain="mlai.au",
            confirmed_keyword="ai agents",
            slack_user_id="U-CONFIRM",
            custom_title=None,
            skip_alternatives=None,
            source_run_id=None,
            slack_channel_id=None,
            slack_thread_ts=None,
            slack_root_message_ts=None,
            progress_message_ts=None,
            delivery_mode=None,
            delivery_mode_confirmed=None,
            request_source="roo_slackbot",
        )

    def test_confirm_topic_with_index(self):
        # Setup job with options
        job = ContentFactoryJob.objects.create(
            job_id="job-index",
            domain="mlai.au",
            slack_user_id="U-CONFIRM",
            status="awaiting_confirmation",
            selection_data={
                "options": [
                    {"keyword": "keyword 0"},
                    {"keyword": "keyword 1"}
                ]
            }
        )

        url = reverse('content_job_confirm', args=["job-index"])
        data = {
            "slack_user_id": "U-CONFIRM",
            "option_index": 1,
            "request_source": "roo_slackbot",
        }

        job.billing_status = "charged"
        job.billing_amount = 0
        job.billing_source_job_id = "job-index"
        job.save(update_fields=["billing_status", "billing_amount", "billing_source_job_id"])

        with patch('integrations.services.article_generation.confirm_topic') as mock_confirm_topic:
            mock_confirm_topic.return_value = {"job_id": "job-article", "status": "queued"}

            response = self.client.post(url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["job_id"], "job-article")
        self.assertEqual(response.data["run_id"], "job-article")
        self.assertEqual(response.data["source_run_id"], "job-index")
        job.refresh_from_db()
        self.assertEqual(job.selected_keyword, "keyword 1")
        self.assertEqual(job.status, "confirmed")
        article_job = ContentFactoryJob.objects.get(job_id="job-article")
        self.assertEqual(article_job.request_meta["source_run_id"], "job-index")

        mock_confirm_topic.assert_called_once()
        call_args = mock_confirm_topic.call_args
        self.assertEqual(call_args.kwargs['confirmed_keyword'], "keyword 1")

    @patch('content_factory.content_views.set_article_delivery_mode')
    def test_set_delivery_mode_endpoint_updates_job_request_meta(self, mock_set_delivery_mode):
        job = ContentFactoryJob.objects.create(
            job_id="job-delivery-mode-set",
            domain="mlai.au",
            slack_user_id="U-CONFIRM",
            status="awaiting_delivery_mode",
            request_meta={"domain": "mlai.au"},
        )
        mock_set_delivery_mode.return_value = {
            "job_id": "job-delivery-mode-set",
            "status": "queued",
            "delivery_mode": "content_only",
            "status_code": 200,
        }

        response = self.client.post(
            reverse('content_job_delivery_mode', args=["job-delivery-mode-set"]),
            {
                "delivery_mode": "content_only",
                "request_source": "roo_slackbot",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job.refresh_from_db()
        self.assertEqual(job.status, "generating")
        self.assertEqual(job.request_meta["delivery_mode"], "content_only")
        self.assertTrue(job.request_meta["delivery_mode_confirmed"])
        mock_set_delivery_mode.assert_called_once_with("job-delivery-mode-set", "content_only")


class SEOResearchMemoryTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "test_roo_key"
        os.environ['ROO_API_KEY'] = self.api_key
        os.environ['INTERNAL_API_KEY'] = self.api_key
        from django.conf import settings
        settings.ROO_API_KEY = self.api_key
        settings.INTERNAL_API_KEY = self.api_key
        self.client.credentials(HTTP_X_API_KEY=self.api_key)

        self.organization = Organization.objects.create(
            name="MLAI",
            domain="mlai.au",
        )
        self.keyword_a = ResearchedKeyword.objects.create(
            organization=self.organization,
            keyword="ai agents",
            volume=2400,
            difficulty=35,
            opportunity_index=82.0,
            cluster_fingerprint="ai-agents",
        )
        self.keyword_b = ResearchedKeyword.objects.create(
            organization=self.organization,
            keyword="agent workflows",
            volume=1800,
            difficulty=28,
            opportunity_index=74.0,
        )

    def test_research_feedback_updates_memory_fields(self):
        payload = {
            "domain": self.organization.domain,
            "session_id": "00000000-0000-0000-0000-000000000001",
            "shown_keywords": ["ai agents", "agent workflows"],
            "selected_keyword": "ai agents",
            "rejected_keywords": ["agent workflows"],
        }

        response = self.client.post("/api/seo/keywords/research-feedback/", payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.keyword_a.refresh_from_db()
        self.keyword_b.refresh_from_db()

        self.assertEqual(self.keyword_a.times_shown, 1)
        self.assertEqual(self.keyword_a.times_selected, 1)
        self.assertIsNotNone(self.keyword_a.last_shown_at)
        self.assertIsNotNone(self.keyword_a.last_selected_at)

        self.assertEqual(self.keyword_b.times_shown, 1)
        self.assertEqual(self.keyword_b.times_rejected, 1)
        self.assertIsNotNone(self.keyword_b.cooldown_until)

    def test_topic_feedback_persists_lists_and_restores_declines(self):
        payload = {
            "domain": self.organization.domain,
            "session_id": "discovery-selection-1",
            "keyword": "how to calculate equity in a house",
            "feedback_type": "declined",
            "reason_code": "not_appropriate",
            "reason_text": None,
            "decline_scope": "similar",
            "source": "homepage_topic_card",
        }

        response = self.client.post("/api/seo/topic-feedback/", payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["keyword"], "how to calculate equity in a house")
        self.assertTrue(response.data["active"])
        feedback = TopicFeedback.objects.get(organization=self.organization)
        self.assertEqual(feedback.keyword_normalized, "how to calculate equity in a house")

        duplicate = self.client.post(
            "/api/seo/topic-feedback/",
            {**payload, "keyword": "How To Calculate Equity In A House", "reason_code": "off_topic"},
            format='json',
        )
        self.assertEqual(duplicate.status_code, status.HTTP_200_OK)
        self.assertEqual(TopicFeedback.objects.filter(organization=self.organization, restored_at__isnull=True).count(), 1)
        feedback.refresh_from_db()
        self.assertEqual(feedback.reason_code, "off_topic")

        list_response = self.client.get(
            f"/api/seo/topic-feedback/?domain={self.organization.domain}&feedback_type=declined"
        )
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(list_response.data["count"], 1)
        self.assertEqual(list_response.data["feedback"][0]["id"], str(feedback.id))

        restore_response = self.client.post(f"/api/seo/topic-feedback/{feedback.id}/restore/")
        self.assertEqual(restore_response.status_code, status.HTTP_200_OK)
        feedback.refresh_from_db()
        self.assertIsNotNone(feedback.restored_at)

        active_response = self.client.get(f"/api/seo/topic-feedback/?domain={self.organization.domain}")
        self.assertEqual(active_response.status_code, status.HTTP_200_OK)
        self.assertEqual(active_response.data["count"], 0)

    def test_keyword_list_supports_offset(self):
        response = self.client.get(
            f"/api/seo/keywords/?domain={self.organization.domain}&limit=1&offset=1"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["keywords"]), 1)
