from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from integrations import http_client
from content_factory.models import ArticlePublishStatus, GeneratedComponent, KeywordStatus, OrganizationContentConfig, ResearchedKeyword, VibeMarketingComponentComment, WrittenArticle
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from roo.models import PointsAccount
from workflow_runs.models import ContentFactoryApprovalState, ContentFactoryRun, ContentFactoryRunStep, ContentFactoryRunStatus
from content_factory.vibe_marketing_views import (
    _call_content_factory_live_preview,
    _live_preview_from_run,
    _resolve_article_root_run_id,
    _sync_local_run_from_remote,
)


User = get_user_model()


class _Response(SimpleNamespace):
    @property
    def content(self):
        return b"{}"

    def json(self):
        return self.payload


class VibeMarketingComponentCommentTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="founder-comments@example.com",
            password="password",
            role="participant",
        )
        self.profile = VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        self.company = VibeRaisingCompany.objects.create(
            profile=self.profile,
            organization=self.organization,
            name="MLAI",
            domain="mlai.au",
            registered=True,
        )
        self.profile.active_company = self.company
        self.profile.save(update_fields=["active_company", "updated_at"])
        OrganizationContentConfig.objects.create(organization=self.organization, github_repo="MLAI-AUS-Inc/mlai-au")
        self.run = ContentFactoryRun.objects.create(
            run_id="article-run-comments",
            workflow="article_generation",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.COMPLETED,
            result={},
        )
        self.client.force_authenticate(user=self.user)

    def _create_mlai_featured_components(self):
        for name in (
            "ArticleDisclaimer",
            "ArticleHeroHeader",
            "ArticleReferences",
            "ArticleResourceCTA",
            "ArticleStepList",
            "ArticleTocPlaceholder",
            "MLAITemplateResourceCTA",
        ):
            GeneratedComponent.objects.get_or_create(
                organization=self.organization,
                name=name,
                defaults={
                    "content": f"export function {name}() {{ return null; }}",
                    "source": "adapted",
                },
            )

    def _prepare_articles_setup_gate(self, *, status="preview_ready", rescan_run_id=""):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        pending = {
            "status": status,
            "setupStatus": status,
            "setup_run_id": "setup-gate-run",
            "setupRunId": "setup-gate-run",
            "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/12",
            "prUrl": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/12",
            "preview_url": "https://preview.example/articles",
            "previewUrl": "https://preview.example/articles",
        }
        if rescan_run_id:
            pending["rescan_run_id"] = rescan_run_id
            pending["rescanRunId"] = rescan_run_id
        config.github_token_encrypted = "encrypted-token"
        config.github_repo = "MLAI-AUS-Inc/mlai-au"
        config.company_context = "MLAI helps Australian founders adopt AI."
        config.article_delivery_mode = "publish_code"
        config.baseline_skipped_at = timezone.now()
        config.article_system = {
            "state": "missing",
            "confidence": "low",
            "pending_article_system_setup": pending,
        }
        config.publish_targets = []
        config.save(
            update_fields=[
                "github_token_encrypted",
                "github_repo",
                "company_context",
                "article_delivery_mode",
                "baseline_skipped_at",
                "article_system",
                "publish_targets",
                "updated_at",
            ]
        )
        self.organization.seed_keywords = ["australian founders"]
        self.organization.save(update_fields=["seed_keywords"])
        self._create_mlai_featured_components()
        self.run.status = ContentFactoryRunStatus.CANCELLED
        self.run.save(update_fields=["status", "updated_at"])
        return config

    def _prepare_billable_vibe_context(self, *, balance=6):
        self.organization.name = "Example"
        self.organization.domain = "example.com"
        self.organization.seed_keywords = ["startup automation"]
        self.organization.competitors = ["competitor.example"]
        self.organization.save(update_fields=["name", "domain", "seed_keywords", "competitors"])
        self.company.name = "Example"
        self.company.domain = "example.com"
        self.company.save(update_fields=["name", "domain", "updated_at"])
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.brand_name = "Example"
        config.company_context = "Example helps startups automate marketing operations."
        config.github_repo = "example/site"
        config.github_token_encrypted = "encrypted-token"
        config.baseline_skipped_at = timezone.now()
        config.article_system = {"state": "existing", "confidence": "high", "directory_name": "articles"}
        config.publish_targets = [{"id": "articles", "label": "Articles"}]
        config.last_scanned_at = timezone.now()
        config.save(
            update_fields=[
                "brand_name",
                "company_context",
                "github_repo",
                "github_token_encrypted",
                "baseline_skipped_at",
                "article_system",
                "publish_targets",
                "last_scanned_at",
                "updated_at",
            ]
        )
        account, _ = PointsAccount.objects.update_or_create(
            user=self.user,
            defaults={"balance": balance, "earned_balance": balance},
        )
        return config, account

    def test_vibe_zero_cost_ai_routes_require_six_roo_points_without_spending(self):
        _config, account = self._prepare_billable_vibe_context(balance=5)

        with patch(
            "content_factory.vibe_marketing_views._queue_content_factory_run",
            side_effect=AssertionError("AI route should not queue without enough Roo points"),
        ):
            requests = [
                (
                    "/api/v1/vibe-marketing/autofill/",
                    {"companyName": "Example", "domain": "example.com"},
                ),
                ("/api/v1/vibe-marketing/baseline/", {}),
                (
                    "/api/v1/vibe-marketing/scan/",
                    {"githubRepo": "example/site", "scanPurpose": "inventory"},
                ),
                (
                    "/api/v1/vibe-marketing/article-system-setup/",
                    {"githubRepo": "example/site", "articleSurfaceUrl": "/articles"},
                ),
                ("/api/v1/vibe-marketing/discovery/", {}),
            ]
            for path, payload in requests:
                response = self.client.post(path, payload, format="json")
                self.assertEqual(response.status_code, 402, path)
                self.assertEqual(response.data["error_code"], "INSUFFICIENT_ROO_POINTS")
                self.assertEqual(response.data["required_points"], 6)
                self.assertEqual(response.data["current_balance"], 5)
                self.assertEqual(response.data["cost_points"], 0)

        account.refresh_from_db()
        self.assertEqual(account.balance, 5)
        self.assertEqual(account.lifetime_spent, 0)

    def test_vibe_zero_cost_ai_routes_authorize_without_spending(self):
        _config, account = self._prepare_billable_vibe_context(balance=6)
        queued_payloads = {}

        def fake_queue(endpoint, workflow, context, config, payload, **kwargs):
            queued_payloads[endpoint] = dict(payload)
            return ContentFactoryRun.objects.create(
                run_id=f"{endpoint}-roo-gate-run",
                workflow=workflow,
                domain=context.organization.domain,
                github_repo=config.github_repo,
                status=ContentFactoryRunStatus.QUEUED,
                result={},
            )

        with patch("content_factory.vibe_marketing_views._queue_content_factory_run", side_effect=fake_queue):
            requests = [
                (
                    "autofill",
                    "/api/v1/vibe-marketing/autofill/",
                    {"companyName": "Example", "domain": "example.com"},
                ),
                ("baseline", "/api/v1/vibe-marketing/baseline/", {}),
                (
                    "scan",
                    "/api/v1/vibe-marketing/scan/",
                    {"githubRepo": "example/site", "scanPurpose": "inventory"},
                ),
                ("discovery", "/api/v1/vibe-marketing/discovery/", {}),
                (
                    "article-system-setup",
                    "/api/v1/vibe-marketing/article-system-setup/",
                    {"githubRepo": "example/site", "articleSurfaceUrl": "/articles"},
                ),
            ]
            for endpoint, path, payload in requests:
                response = self.client.post(path, payload, format="json")
                self.assertEqual(response.status_code, 202, path)
                self.assertIn(endpoint, queued_payloads)
                self.assertTrue(queued_payloads[endpoint]["roo_points_authorized"])
                self.assertEqual(queued_payloads[endpoint]["roo_points_cost"], 0)
                self.assertEqual(queued_payloads[endpoint]["roo_points_required"], 6)
                self.assertEqual(queued_payloads[endpoint]["roo_points_balance"], 6)

        account.refresh_from_db()
        self.assertEqual(account.balance, 6)
        self.assertEqual(account.lifetime_spent, 0)

    def test_completed_article_run_auto_prepares_live_preview(self):
        self.run.result = {
            "componentManifest": {
                "components": [
                    {
                        "id": "title",
                        "type": "title",
                        "label": "Title",
                        "selector": '[data-cf-component-id="title"]',
                    }
                ]
            }
        }
        self.run.save(update_fields=["result", "updated_at"])

        preview_payload = {
            "available": True,
            "status": "running",
            "previewUrl": "http://127.0.0.1:4321/articles/featured/generated?cfInspector=1",
            "exactRender": True,
            "inspectorProtocolVersion": 2,
            "inspectorMode": "comment",
            "verificationSkippedForPreview": True,
            "failedPhase": "verify",
            "failedCommand": "bun run typecheck",
            "logExcerpt": "[verify] command: bun run typecheck\nerror: script exited",
            "proofWarnings": ["Vite optimized dependencies during preview proof."],
            "browserWarnings": ["Failed to fetch dynamically imported module: /app/entry.client.tsx"],
            "assetWarnings": ["http://127.0.0.1:4321/node_modules/.vite/deps/react-dom_client.js"],
            "proofAttempts": [{"attempt": 1, "exact": True}],
            "proofAcceptedWithWarnings": True,
            "previewMode": "local_runtime",
            "previewClientMode": "ssr_static",
            "clientHydrationDisabledForPreview": True,
        }

        with (
            patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}),
            patch("content_factory.vibe_marketing_views._call_content_factory_live_preview", return_value=preview_payload) as preview_call,
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        preview_call.assert_called_once_with(run_id=self.run.run_id, method="POST", payload={"force": False})
        expected_preview_url = (
            f"{settings.DEFAULT_BACKEND_URL}/api/v1/vibe-marketing/runs/{self.run.run_id}"
            "/live-preview/proxy/articles/featured/generated?cfInspector=1"
        )
        self.assertEqual(response.data["livePreview"]["previewUrl"], expected_preview_url)
        self.assertEqual(response.data["livePreview"]["internalPreviewUrl"], preview_payload["previewUrl"])
        self.assertTrue(response.data["livePreview"]["verificationSkippedForPreview"])
        self.assertEqual(response.data["livePreview"]["failedPhase"], "verify")
        self.assertEqual(response.data["livePreview"]["failedCommand"], "bun run typecheck")
        self.assertIn("script exited", response.data["livePreview"]["logExcerpt"])
        self.assertEqual(response.data["livePreview"]["proofWarnings"], preview_payload["proofWarnings"])
        self.assertEqual(response.data["livePreview"]["browserWarnings"], preview_payload["browserWarnings"])
        self.assertEqual(response.data["livePreview"]["assetWarnings"], preview_payload["assetWarnings"])
        self.assertEqual(response.data["livePreview"]["proofAttempts"], preview_payload["proofAttempts"])
        self.assertTrue(response.data["livePreview"]["proofAcceptedWithWarnings"])
        self.assertEqual(response.data["livePreview"]["previewMode"], "local_runtime")
        self.assertEqual(response.data["livePreview"]["previewClientMode"], "ssr_static")
        self.assertTrue(response.data["livePreview"]["clientHydrationDisabledForPreview"])
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["livePreview"]["previewUrl"], expected_preview_url)
        self.assertEqual(self.run.result["livePreview"]["internalPreviewUrl"], preview_payload["previewUrl"])
        self.assertEqual(self.run.result["livePreview"]["failedCommand"], "bun run typecheck")

    def test_repo_scan_run_serializes_stale_retry_metadata(self):
        scan_run = ContentFactoryRun.objects.create(
            run_id="repo-scan-stale",
            workflow="repo_scan",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.BLOCKED,
            current_step="load_repo_context",
            resume_available=True,
            result={
                "stale": True,
                "stale_reason": "scan_queue_not_started",
                "retry_available": True,
                "queue_name": "scan",
                "queued_at": "2026-05-12T03:00:00+00:00",
            },
        )

        with patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{scan_run.run_id}")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["stale"])
        self.assertTrue(response.data["retryAvailable"])
        self.assertEqual(response.data["staleReason"], "scan_queue_not_started")
        self.assertEqual(response.data["queueName"], "scan")
        self.assertEqual(response.data["queuedAt"], "2026-05-12T03:00:00+00:00")

    def test_completed_repo_scan_status_ignores_stale_remote_processing(self):
        scan_run = ContentFactoryRun.objects.create(
            run_id="repo-scan-completed-local",
            workflow="repo_scan",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.COMPLETED,
            current_step="finalize",
            result={"status": "completed", "scaffold_status": "not_needed"},
        )
        remote_payload = {
            "run_id": scan_run.run_id,
            "workflow": "repo_scan",
            "status": "processing",
            "current_step": "scan_structure",
            "result": {"status": "processing"},
        }

        with (
            self.assertLogs("content_factory.vibe_marketing_views", level="INFO") as logs,
            patch(
                "content_factory.vibe_marketing_views._call_content_factory_run_status",
                return_value=remote_payload,
            ) as status_poll,
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{scan_run.run_id}?view=status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.COMPLETED)
        status_poll.assert_called_once_with(scan_run.run_id, workflow="repo_scan")
        scan_run.refresh_from_db()
        self.assertEqual(scan_run.status, ContentFactoryRunStatus.COMPLETED)
        self.assertEqual(scan_run.current_step, "finalize")
        self.assertIn("content_factory_scan_status_poll_preserved_local_terminal_state", "\n".join(logs.output))

    def test_awaiting_confirmation_repo_scan_status_ignores_stale_remote_processing(self):
        scan_run = ContentFactoryRun.objects.create(
            run_id="repo-scan-awaiting-local",
            workflow="repo_scan",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.AWAITING_CONFIRMATION,
            current_step="finalize",
            approval_state=ContentFactoryApprovalState.APPROVAL_REQUIRED,
            result={
                "status": "awaiting_confirmation",
                "requested_action": "scaffold_publish_route",
                "scaffold_status": "approval_required",
            },
        )
        remote_payload = {
            "run_id": scan_run.run_id,
            "workflow": "repo_scan",
            "status": "processing",
            "current_step": "scan_structure",
            "result": {"status": "processing"},
        }

        with patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value=remote_payload):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{scan_run.run_id}?view=status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.AWAITING_CONFIRMATION)
        self.assertEqual(response.data["approvalState"], ContentFactoryApprovalState.APPROVAL_REQUIRED)
        scan_run.refresh_from_db()
        self.assertEqual(scan_run.status, ContentFactoryRunStatus.AWAITING_CONFIRMATION)
        self.assertEqual(scan_run.current_step, "finalize")

    def test_active_repo_scan_status_updates_from_remote_completed(self):
        scan_run = ContentFactoryRun.objects.create(
            run_id="repo-scan-running-local",
            workflow="repo_scan",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.RUNNING,
            current_step="scan_structure",
            run_request={"scan_purpose": "inventory", "article_surface_mode": "not_sure"},
            result={"status": "processing", "scan_purpose": "inventory", "article_surface_mode": "not_sure"},
        )
        detected_candidates = [
            {
                "route": "/articles",
                "path": "app/routes/articles.tsx",
                "confidence": 0.72,
            }
        ]
        remote_payload = {
            "run_id": scan_run.run_id,
            "workflow": "repo_scan",
            "status": "completed",
            "current_step": "finalize",
            "result": {
                "status": "completed",
                "article_surface_hint": {},
                "detected_candidates": detected_candidates,
                "article_system_readiness": {"ready": False, "detected_candidates": detected_candidates},
                "matched_article_surface": detected_candidates[0],
                "scaffold_status": "not_needed",
            },
        }

        with patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value=remote_payload):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{scan_run.run_id}?view=status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.COMPLETED)
        scan_run.refresh_from_db()
        self.assertEqual(scan_run.status, ContentFactoryRunStatus.COMPLETED)
        self.assertEqual(scan_run.current_step, "finalize")
        self.assertEqual(scan_run.result["scan_purpose"], "inventory")
        self.assertEqual(scan_run.result["article_surface_mode"], "not_sure")
        self.assertEqual(scan_run.result["scaffold_status"], "not_needed")
        self.assertEqual(response.data["result"]["scan_purpose"], "inventory")
        self.assertEqual(response.data["result"]["article_surface_mode"], "not_sure")
        self.assertEqual(response.data["result"]["detected_candidates"], detected_candidates)
        self.assertEqual(response.data["result"]["article_system_readiness"]["detected_candidates"], detected_candidates)
        self.assertEqual(response.data["result"]["matched_article_surface"], detected_candidates[0])
        self.assertEqual(response.data["result"]["scaffold_status"], "not_needed")
        self.assertNotIn("article_surface_hint", response.data["result"])

    def test_bootstrap_blocks_mlai_article_system_when_featured_catalog_missing(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.article_system = {"state": "existing", "confidence": "high"}
        config.scan_summary = "{\"generated_components\": [{\"name\": \"ArticleHeroHeader\"}]}"
        config.save(update_fields=["article_system", "scan_summary", "updated_at"])

        with patch("content_factory.vibe_marketing_views.google_baseline_connection_status", return_value={}):
            response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        scaffold = response.data["checks"]["scaffold"]
        self.assertFalse(scaffold["passed"])
        self.assertFalse(scaffold["componentCatalogReady"])
        self.assertIn("ArticleDisclaimer", scaffold["missingComponents"])
        self.assertNotIn("ArticleHeroHeader", scaffold["missingComponents"])

    def test_bootstrap_allows_mlai_article_system_when_featured_catalog_present(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.article_system = {"state": "existing", "confidence": "high"}
        # A detected-only state needs a publish path to count as ready (the subject
        # of this test is the featured-component catalog gate, not detection).
        config.publish_targets = [{"id": "articles", "label": "Articles"}]
        config.save(update_fields=["article_system", "publish_targets", "updated_at"])
        for name in (
            "ArticleDisclaimer",
            "ArticleHeroHeader",
            "ArticleReferences",
            "ArticleResourceCTA",
            "ArticleStepList",
            "ArticleTocPlaceholder",
            "MLAITemplateResourceCTA",
        ):
            GeneratedComponent.objects.create(
                organization=self.organization,
                name=name,
                content=f"export function {name}() {{ return null; }}",
                source="adapted",
            )

        with patch("content_factory.vibe_marketing_views.google_baseline_connection_status", return_value={}):
            response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        scaffold = response.data["checks"]["scaffold"]
        self.assertTrue(scaffold["passed"])
        self.assertTrue(scaffold["componentCatalogReady"])
        self.assertEqual(scaffold["missingComponents"], [])
        self.assertFalse(response.data["hasCompletedArticleFlow"])
        self.assertEqual(response.data["startPageMode"], "topic_picker")

    def test_starting_new_scan_supersedes_stale_scan_run(self):
        stale_run = ContentFactoryRun.objects.create(
            run_id="repo-scan-stale-old",
            workflow="repo_scan",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.BLOCKED,
            current_step="load_repo_context",
            result={"stale": True, "stale_reason": "scan_queue_not_started", "retry_available": True},
        )
        queued_run = ContentFactoryRun.objects.create(
            run_id="repo-scan-new",
            workflow="repo_scan",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.QUEUED,
            current_step="load_repo_context",
            result={},
        )

        with (
            patch("content_factory.vibe_marketing_views._call_content_factory_run_action", return_value={"status": "cancelled"}),
            patch("content_factory.vibe_marketing_views._queue_content_factory_run", return_value=queued_run),
        ):
            response = self.client.post(
                "/api/v1/vibe-marketing/scan/",
                {
                    "githubRepo": "MLAI-AUS-Inc/mlai-au",
                    "articleSurfaceUrl": "/articles",
                    "autoSetupPreview": True,
                },
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        stale_run.refresh_from_db()
        queued_run.refresh_from_db()
        self.assertEqual(stale_run.status, ContentFactoryRunStatus.CANCELLED)
        self.assertEqual(stale_run.current_step, "cancelled")
        self.assertEqual(queued_run.result["superseded_scan_run_ids"], ["repo-scan-stale-old"])

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_scan_approval_persists_setup_run_id_and_local_setup_child(self):
        scan_run = ContentFactoryRun.objects.create(
            run_id="repo-scan-awaiting-setup",
            workflow="repo_scan",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.AWAITING_CONFIRMATION,
            current_step="finalize",
            approval_state=ContentFactoryApprovalState.APPROVAL_REQUIRED,
            result={
                "requested_action": "scaffold_publish_route",
                "scaffold_status": "approval_required",
                "article_system_setup": {
                    "status": "approval_required",
                    "requested_action": "article_system_setup",
                    "changed_files_preview": ["app/routes/articles.index.tsx"],
                },
                "result": {
                    "requested_action": "scaffold_publish_route",
                    "scaffold_status": "approval_required",
                },
            },
        )

        remote_payload = {
            "status": "queued",
            "workflow": "repo_scan",
            "setup_run_id": "setup-run-guided-1",
            "scaffold_job_id": "setup-run-guided-1",
            "article_system_setup": {
                "status": "queued",
                "setup_run_id": "setup-run-guided-1",
                "changed_files_preview": ["app/routes/articles.index.tsx"],
            },
        }
        with patch("content_factory.vibe_marketing_views._call_content_factory_run_action", return_value=remote_payload):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{scan_run.run_id}/approve", {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["result"]["setup_run_id"], "setup-run-guided-1")
        self.assertEqual(response.data["result"]["article_system_setup"]["setup_run_id"], "setup-run-guided-1")
        scan_run.refresh_from_db()
        self.assertEqual(scan_run.result["setup_run_id"], "setup-run-guided-1")
        setup_run = ContentFactoryRun.objects.get(run_id="setup-run-guided-1")
        self.assertEqual(setup_run.workflow, "article_system_setup")
        self.assertEqual(setup_run.run_request["scan_run_id"], scan_run.run_id)

    def test_platform_deployment_preview_url_is_not_rewritten_to_backend_proxy(self):
        self.run.result = {
            "livePreview": {
                "available": True,
                "status": "running",
                "previewUrl": "https://run-123.mlai-previews.com/articles/generated?cfInspector=1&cfPreviewMode=platform_deployment",
                "internalPreviewUrl": "https://abc.pages.dev",
                "routePath": "/articles/generated",
                "previewMode": "platform_deployment",
                "platformProvider": "cloudflare",
                "platformStatus": "ready",
                "deploymentUrl": "https://abc.pages.dev",
                "routeUrl": "https://run-123.mlai-previews.com/articles/generated?cfInspector=1&cfPreviewMode=platform_deployment",
                "logsUrl": "https://github.com/MLAI-AUS-Inc/content-factory/actions/runs/123",
                "commitSha": "abc123",
                "branchName": "cf-review/article-run-comments",
            }
        }

        preview = _live_preview_from_run(self.run)

        self.assertEqual(preview["previewUrl"], "https://run-123.mlai-previews.com/articles/generated?cfInspector=1&cfPreviewMode=platform_deployment")
        self.assertEqual(preview["internalPreviewUrl"], "https://abc.pages.dev")
        self.assertEqual(preview["previewMode"], "platform_deployment")
        self.assertEqual(preview["platformProvider"], "cloudflare")
        self.assertEqual(preview["platformStatus"], "ready")
        self.assertEqual(preview["logsUrl"], "https://github.com/MLAI-AUS-Inc/content-factory/actions/runs/123")
        self.assertEqual(preview["commitSha"], "abc123")

    def test_failed_platform_preview_payload_is_normalized(self):
        self.run.result = {
            "livePreview": {
                "available": False,
                "status": "running",
                "previewMode": "platform_deployment",
                "platformProvider": "cloudflare",
                "platformStatus": "failed",
                "nativePreviewFailure": {
                    "error": "Hosted preview dispatch failed: Not Found",
                    "errorCode": "platform_preview_dispatch_failed",
                    "retryable": True,
                    "failedPhase": "platform_deployment",
                    "failedCommand": "MLAI-AUS-Inc/content-factory/preview-builder.yml@main",
                    "logExcerpt": "GitHub workflow dispatch returned 404.",
                },
            }
        }

        preview = _live_preview_from_run(self.run)

        self.assertEqual(preview["status"], "failed")
        self.assertEqual(preview["previewMode"], "platform_deployment")
        self.assertEqual(preview["platformStatus"], "failed")
        self.assertEqual(preview["errorCode"], "platform_preview_dispatch_failed")
        self.assertEqual(preview["failedPhase"], "platform_deployment")
        self.assertIn("dispatch failed", preview["error"])
        self.assertIn("workflow dispatch", preview["logExcerpt"])
        self.assertTrue(preview["retryable"])

    def test_live_preview_serializes_visual_fallback_metadata(self):
        self.run.result = {
            "livePreview": {
                "available": True,
                "status": "running",
                "previewUrl": "/api/runs/article-run-comments/live-preview/proxy/articles/generated?cfInspector=1",
                "previewMode": "visual_static_fallback",
                "renderConfidence": "visual",
                "fallbackReason": "preview_proof_failed",
                "nativePreviewFailure": {"error": "Native failed.", "errorCode": "preview_proof_failed"},
                "visualFallback": {
                    "cssSources": ["fallback.css", "app/globals.css"],
                    "cssWarnings": ["Tailwind CSS source was included without compilation."],
                    "assetProxyEnabled": True,
                    "mockedRoutes": ["/api/article"],
                },
            }
        }
        self.run.save(update_fields=["result", "updated_at"])

        response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        preview = response.data["livePreview"]
        self.assertEqual(preview["previewMode"], "visual_static_fallback")
        self.assertEqual(preview["renderConfidence"], "visual")
        self.assertEqual(preview["fallbackReason"], "preview_proof_failed")
        self.assertEqual(preview["nativePreviewFailure"]["errorCode"], "preview_proof_failed")
        self.assertEqual(preview["visualFallback"]["cssSources"], ["fallback.css", "app/globals.css"])

    def test_completed_article_run_auto_prepare_forwards_org_github_token(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.github_token_encrypted = "org-live-preview-token"
        config.save(update_fields=["github_token_encrypted", "updated_at"])
        self.run.result = {
            "componentManifest": {
                "components": [
                    {
                        "id": "title",
                        "type": "title",
                        "label": "Title",
                    }
                ]
            }
        }
        self.run.save(update_fields=["result", "updated_at"])

        preview_payload = {"available": False, "status": "starting", "previewUrl": ""}

        with (
            patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}),
            patch("content_factory.vibe_marketing_views._call_content_factory_live_preview", return_value=preview_payload) as preview_call,
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        preview_call.assert_called_once_with(
            run_id=self.run.run_id,
            method="POST",
            payload={
                "force": False,
                "github_token": "org-live-preview-token",
                "token_source": "github_oauth_user_token",
            },
        )

    def test_completed_article_run_auto_prepare_prefers_github_app_installation_token(self):
        from integrations.services.github_app import GitHubInstallationToken

        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.github_token_encrypted = "legacy-org-live-preview-token"
        config.github_installation_id = "12345"
        config.save(update_fields=["github_token_encrypted", "github_installation_id", "updated_at"])
        self.run.result = {
            "componentManifest": {
                "components": [
                    {
                        "id": "title",
                        "type": "title",
                        "label": "Title",
                    }
                ]
            }
        }
        self.run.save(update_fields=["result", "updated_at"])
        app_token = GitHubInstallationToken(
            token="ghs_installation",
            expires_at=timezone.now() + timedelta(minutes=50),
            installation_id="12345",
            repository="MLAI-AUS-Inc/mlai-au",
        )
        preview_payload = {"available": False, "status": "starting", "previewUrl": ""}

        with (
            patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}),
            patch("content_factory.vibe_marketing_views.github_app_credentials_configured", return_value=True),
            patch(
                "content_factory.vibe_marketing_views.create_installation_access_token",
                return_value=app_token,
            ) as create_token,
            patch("content_factory.vibe_marketing_views._call_content_factory_live_preview", return_value=preview_payload) as preview_call,
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        preview_call.assert_called_once_with(
            run_id=self.run.run_id,
            method="POST",
            payload={
                "force": False,
                "github_token": "ghs_installation",
                "github_installation_id": "12345",
                "token_source": "github_app_installation",
            },
        )
        create_token.assert_called_once_with(
            installation_id="12345",
            repository="MLAI-AUS-Inc/mlai-au",
            permission_mode="write",
        )

    def test_completed_article_run_refreshes_starting_preview_failure(self):
        self.run.result = {
            "componentManifest": {
                "components": [
                    {
                        "id": "title",
                        "type": "title",
                        "label": "Title",
                    }
                ]
            },
            "livePreview": {
                "available": False,
                "status": "starting",
                "previewUrl": "",
                "error": "",
                "errorCode": "preview_start_timeout",
                "retryable": True,
            },
        }
        self.run.save(update_fields=["result", "updated_at"])

        preview_payload = {
            "available": False,
            "status": "failed",
            "previewUrl": "",
            "error": "Preview process was terminated by SIGKILL. This often indicates the container ran out of memory.",
            "errorCode": "dev_server_startup_failed",
            "retryable": True,
            "failedPhase": "preview",
            "failedCommand": "bun run dev -- --host 127.0.0.1 --port 40547",
            "logExcerpt": 'error: script "dev" was terminated by signal SIGKILL',
        }

        with (
            patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}),
            patch("content_factory.vibe_marketing_views._call_content_factory_live_preview", return_value=preview_payload) as preview_call,
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        preview_call.assert_called_once_with(run_id=self.run.run_id, method="GET")
        self.assertEqual(response.data["livePreview"]["status"], "failed")
        self.assertEqual(response.data["livePreview"]["errorCode"], "dev_server_startup_failed")
        self.assertIn("SIGKILL", response.data["livePreview"]["logExcerpt"])
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["livePreview"]["status"], "failed")

    def test_running_article_run_does_not_auto_prepare_live_preview(self):
        self.run.status = ContentFactoryRunStatus.RUNNING
        self.run.result = {
            "componentManifest": {
                "components": [
                    {
                        "id": "title",
                        "type": "title",
                        "label": "Title",
                    }
                ]
            }
        }
        self.run.save(update_fields=["status", "result", "updated_at"])

        with (
            patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}),
            patch("content_factory.vibe_marketing_views._call_content_factory_live_preview") as preview_call,
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        preview_call.assert_not_called()
        self.assertFalse(response.data["livePreview"]["previewUrl"])

    def test_completed_article_run_with_ready_preview_does_not_restart_preview(self):
        preview_url = "http://127.0.0.1:4321/articles/featured/generated?cfInspector=1"
        self.run.result = {
            "componentManifest": {
                "components": [
                    {
                        "id": "title",
                        "type": "title",
                        "label": "Title",
                    }
                ]
            },
            "livePreview": {
                "available": True,
                "status": "running",
                "previewUrl": preview_url,
                "exactRender": True,
            },
        }
        self.run.save(update_fields=["result", "updated_at"])

        with (
            patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}),
            patch("content_factory.vibe_marketing_views._call_content_factory_live_preview") as preview_call,
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        preview_call.assert_not_called()
        expected_preview_url = (
            f"{settings.DEFAULT_BACKEND_URL}/api/v1/vibe-marketing/runs/{self.run.run_id}"
            "/live-preview/proxy/articles/featured/generated?cfInspector=1"
        )
        self.assertEqual(response.data["livePreview"]["previewUrl"], expected_preview_url)
        self.assertEqual(response.data["livePreview"]["internalPreviewUrl"], preview_url)

    def test_completed_article_run_refreshes_running_live_preview(self):
        self.run.result = {
            "componentManifest": {
                "components": [
                    {
                        "id": "title",
                        "type": "title",
                        "label": "Title",
                    }
                ]
            },
            "livePreview": {
                "available": False,
                "status": "running",
                "previewUrl": "",
                "exactRender": True,
            },
        }
        self.run.save(update_fields=["result", "updated_at"])
        preview_payload = {
            "available": True,
            "status": "ready",
            "previewUrl": "http://127.0.0.1:4321/articles/featured/generated?cfInspector=1",
            "exactRender": True,
            "inspectorProtocolVersion": 2,
            "inspectorMode": "comment",
        }

        with (
            patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}),
            patch("content_factory.vibe_marketing_views._call_content_factory_live_preview", return_value=preview_payload) as preview_call,
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        preview_call.assert_called_once_with(run_id=self.run.run_id, method="GET")
        expected_preview_url = (
            f"{settings.DEFAULT_BACKEND_URL}/api/v1/vibe-marketing/runs/{self.run.run_id}"
            "/live-preview/proxy/articles/featured/generated?cfInspector=1"
        )
        self.assertEqual(response.data["livePreview"]["previewUrl"], expected_preview_url)
        self.assertEqual(response.data["livePreview"]["internalPreviewUrl"], preview_payload["previewUrl"])

    def test_live_preview_proxy_forwards_authenticated_run_asset_request(self):
        remote_response = SimpleNamespace(
            status_code=200,
            content=b"console.log('preview')",
            headers={
                "Content-Type": "text/javascript",
                "Content-Length": "999",
                "Content-Security-Policy": "frame-ancestors 'none'",
                "Content-Security-Policy-Report-Only": "frame-ancestors 'none'",
                "X-Frame-Options": "DENY",
                "X-Preview": "ok",
            },
        )

        with (
            patch(
                "content_factory.vibe_marketing_views._content_factory_remote_config",
                return_value={"enabled": True, "base_url": "http://content-factory-web:8000"},
            ),
            patch("content_factory.vibe_marketing_views._content_factory_headers", return_value={"X-API-Key": "test-key"}),
            patch("content_factory.vibe_marketing_views.http_client.request", return_value=remote_response) as request_call,
        ):
            response = self.client.get(
                f"/api/v1/vibe-marketing/runs/{self.run.run_id}"
                "/live-preview/proxy/node_modules/.vite/deps/react-dom_client.js?v=test",
                HTTP_ACCEPT="text/javascript",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"console.log('preview')")
        self.assertEqual(response["Content-Type"], "text/javascript")
        self.assertEqual(response["X-Preview"], "ok")
        self.assertFalse(response.has_header("X-Frame-Options"))
        self.assertFalse(response.has_header("Content-Security-Policy"))
        self.assertFalse(response.has_header("Content-Security-Policy-Report-Only"))
        request_call.assert_called_once()
        args, kwargs = request_call.call_args
        self.assertEqual(args[0], "GET")
        self.assertEqual(
            args[1],
            f"http://content-factory-web:8000/api/runs/{self.run.run_id}"
            "/live-preview/proxy/node_modules/.vite/deps/react-dom_client.js?v=test",
        )
        self.assertEqual(kwargs["headers"]["X-API-Key"], "test-key")

    def test_live_preview_proxy_rewrites_root_relative_html_assets(self):
        remote_response = SimpleNamespace(
            status_code=200,
            content=(
                b'<html><head><link rel="stylesheet" href="/@react-router/critical.css?pathname=/articles/featured/generated">'
                b'<link rel="modulepreload" href="/app/root.tsx">'
                b'<link rel="modulepreload" href="/node_modules/.vite/deps/react.js?v=test">'
                b'<script type="module" src="/app/entry.client.tsx"></script>'
                b'<script type="module" src="/@vite/client"></script>'
                b'<script type="module" src="/@id/__x00__virtual:react-router/inject-hmr-runtime"></script>'
                b"<script>window.__cfArticleInspectorInstalled = true;</script>"
                b'<script src=//@cdn.example.com/skip.js></script>'
                b'<img srcset="/assets/small.png 1x, https://cdn.example.com/hero.webp 2x" style="background-image:url(/assets/bg.png)">'
                b'</head><body><a href="/api/v1/auth/me/">api</a></body></html>'
            ),
            headers={
                "Content-Type": "text/html; charset=utf-8",
                "Content-Length": "999",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "frame-ancestors 'none'",
                "Content-Security-Policy-Report-Only": "default-src 'none'",
            },
        )

        with (
            patch(
                "content_factory.vibe_marketing_views._content_factory_remote_config",
                return_value={"enabled": True, "base_url": "http://content-factory-web:8000"},
            ),
            patch("content_factory.vibe_marketing_views._content_factory_headers", return_value={"X-API-Key": "test-key"}),
            patch("content_factory.vibe_marketing_views.http_client.request", return_value=remote_response),
        ):
            response = self.client.get(
                f"/api/v1/vibe-marketing/runs/{self.run.run_id}"
                "/live-preview/proxy/articles/featured/generated?cfInspector=1",
                HTTP_ACCEPT="text/html",
            )

        text = response.content.decode("utf-8")
        proxy_prefix = f"/api/v1/vibe-marketing/runs/{self.run.run_id}/live-preview/proxy"
        self.assertIn(f'href="{proxy_prefix}/@react-router/critical.css?pathname=/articles/featured/generated"', text)
        self.assertIn(f'srcset="{proxy_prefix}/assets/small.png 1x, /api/v1/vibe-marketing/runs/{self.run.run_id}/live-preview/resource?url=https%3A%2F%2Fcdn.example.com%2Fhero.webp 2x"', text)
        self.assertIn(f"background-image:url({proxy_prefix}/assets/bg.png)", text)
        self.assertIn("window.__cfArticleInspectorInstalled", text)
        self.assertIn("//@cdn.example.com/skip.js", text)
        self.assertIn('href="/api/v1/auth/me/"', text)
        self.assertNotIn("app/entry.client", text)
        self.assertNotIn("@vite/client", text)
        self.assertNotIn("__x00__virtual:react-router", text)
        self.assertNotIn("modulepreload", text)
        self.assertEqual(response["Content-Type"], "text/html; charset=utf-8")
        self.assertFalse(response.has_header("X-Frame-Options"))
        self.assertFalse(response.has_header("Content-Security-Policy"))
        self.assertFalse(response.has_header("Content-Security-Policy-Report-Only"))

    def test_live_preview_proxy_rewrites_root_relative_js_modules(self):
        remote_response = SimpleNamespace(
            status_code=200,
            content=(
                b'import "/app/root.tsx";\n'
                b'import route from "/app/routes/articles.slug.tsx";\n'
                b'import("/@id/__x00__virtual:react-router/inject-hmr-runtime");\n'
                b'import("/node_modules/.vite/deps/react-dom_client.js?v=test");\n'
                b'fetch("/api/v1/auth/me/");\n'
                b'const cdn = "//cdn.example.com/module.js";\n'
            ),
            headers={"Content-Type": "text/javascript"},
        )

        with (
            patch(
                "content_factory.vibe_marketing_views._content_factory_remote_config",
                return_value={"enabled": True, "base_url": "http://content-factory-web:8000"},
            ),
            patch("content_factory.vibe_marketing_views._content_factory_headers", return_value={"X-API-Key": "test-key"}),
            patch("content_factory.vibe_marketing_views.http_client.request", return_value=remote_response),
        ):
            response = self.client.get(
                f"/api/v1/vibe-marketing/runs/{self.run.run_id}/live-preview/proxy/app/root.tsx",
                HTTP_ACCEPT="text/javascript",
            )

        text = response.content.decode("utf-8")
        proxy_prefix = f"/api/v1/vibe-marketing/runs/{self.run.run_id}/live-preview/proxy"
        self.assertIn(f'import "{proxy_prefix}/app/root.tsx";', text)
        self.assertIn(f'import route from "{proxy_prefix}/app/routes/articles.slug.tsx";', text)
        self.assertIn(f'import("{proxy_prefix}/@id/__x00__virtual:react-router/inject-hmr-runtime");', text)
        self.assertIn(f'import("{proxy_prefix}/node_modules/.vite/deps/react-dom_client.js?v=test");', text)
        self.assertIn('fetch("/api/v1/auth/me/");', text)
        self.assertIn('const cdn = "//cdn.example.com/module.js";', text)
        self.assertEqual(response["Content-Type"], "text/javascript")

    def test_live_preview_proxy_returns_empty_client_runtime_module(self):
        with patch("content_factory.vibe_marketing_views._content_factory_remote_config") as remote_config:
            response = self.client.get(
                f"/api/v1/vibe-marketing/runs/{self.run.run_id}/live-preview/proxy/app/entry.client.tsx",
                HTTP_ACCEPT="text/javascript",
            )

        remote_config.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"export {};\n")
        self.assertEqual(response["Content-Type"], "text/javascript; charset=utf-8")
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_live_preview_proxy_rewrites_root_relative_css_urls(self):
        remote_response = SimpleNamespace(
            status_code=200,
            content=(
                b'@import "/@vite/client";\n'
                b".hero { background-image: url(/assets/hero.png); }\n"
                b".font { src: url('/src/fonts/site.woff2'); }\n"
                b".external { background: url(https://cdn.example.com/bg.png); }\n"
            ),
            headers={"Content-Type": "text/css", "X-Frame-Options": "DENY"},
        )

        with (
            patch(
                "content_factory.vibe_marketing_views._content_factory_remote_config",
                return_value={"enabled": True, "base_url": "http://content-factory-web:8000"},
            ),
            patch("content_factory.vibe_marketing_views._content_factory_headers", return_value={"X-API-Key": "test-key"}),
            patch("content_factory.vibe_marketing_views.http_client.request", return_value=remote_response),
        ):
            response = self.client.get(
                f"/api/v1/vibe-marketing/runs/{self.run.run_id}/live-preview/proxy/@react-router/critical.css",
                HTTP_ACCEPT="text/css",
            )

        text = response.content.decode("utf-8")
        proxy_prefix = f"/api/v1/vibe-marketing/runs/{self.run.run_id}/live-preview/proxy"
        resource_prefix = f"/api/v1/vibe-marketing/runs/{self.run.run_id}/live-preview/resource"
        self.assertIn(f'@import "{proxy_prefix}/@vite/client";', text)
        self.assertIn(f"url({proxy_prefix}/assets/hero.png)", text)
        self.assertIn(f"url('{proxy_prefix}/src/fonts/site.woff2')", text)
        self.assertIn(f"url({resource_prefix}?url=https%3A%2F%2Fcdn.example.com%2Fbg.png)", text)
        self.assertEqual(response["Content-Type"], "text/css")
        self.assertFalse(response.has_header("X-Frame-Options"))

    def test_live_preview_resource_proxy_forwards_external_assets_and_strips_frame_headers(self):
        remote_response = SimpleNamespace(
            status_code=200,
            content=b"image-bytes",
            headers={
                "Content-Type": "image/png",
                "Content-Length": "999",
                "Content-Security-Policy": "frame-ancestors 'none'",
                "X-Frame-Options": "DENY",
                "Cache-Control": "public, max-age=60",
            },
        )

        with (
            patch("content_factory.vibe_marketing_views._is_allowed_live_preview_resource_url", return_value=True),
            patch(
                "content_factory.vibe_marketing_views._content_factory_remote_config",
                return_value={"enabled": True, "base_url": "http://content-factory-web:8000"},
            ),
            patch("content_factory.vibe_marketing_views._content_factory_headers", return_value={"X-API-Key": "test-key"}),
            patch("content_factory.vibe_marketing_views.http_client.request", return_value=remote_response) as request_call,
        ):
            response = self.client.get(
                f"/api/v1/vibe-marketing/runs/{self.run.run_id}/live-preview/resource",
                {"url": "https://cdn.example.com/hero.png"},
                HTTP_ACCEPT="image/png",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"image-bytes")
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response["Cache-Control"], "public, max-age=60")
        self.assertFalse(response.has_header("X-Frame-Options"))
        self.assertFalse(response.has_header("Content-Security-Policy"))
        args, kwargs = request_call.call_args
        self.assertEqual(args[0], "GET")
        self.assertEqual(
            args[1],
            f"http://content-factory-web:8000/api/runs/{self.run.run_id}"
            "/live-preview/resource?url=https%3A%2F%2Fcdn.example.com%2Fhero.png",
        )
        self.assertEqual(kwargs["headers"]["X-API-Key"], "test-key")

    def test_live_preview_resource_proxy_blocks_private_external_targets(self):
        with patch("content_factory.vibe_marketing_views._content_factory_remote_config") as remote_config:
            response = self.client.get(
                f"/api/v1/vibe-marketing/runs/{self.run.run_id}/live-preview/resource",
                {"url": "http://169.254.169.254/latest/meta-data"},
            )

        remote_config.assert_not_called()
        self.assertEqual(response.status_code, 403)
        self.assertIn(b"not allowed", response.content)

    def test_remote_not_started_preview_does_not_overwrite_local_failed_preview(self):
        manifest = {
            "components": [
                {
                    "id": "title",
                    "type": "title",
                    "label": "Title",
                }
            ]
        }
        self.run.result = {
            "componentManifest": manifest,
            "livePreview": {
                "available": False,
                "status": "failed",
                "previewUrl": "",
                "error": "Missing required mlai.au featured components in catalog.",
            },
        }
        self.run.save(update_fields=["result", "updated_at"])
        remote_data = {
            "run_id": self.run.run_id,
            "workflow": self.run.workflow,
            "status": ContentFactoryRunStatus.COMPLETED,
            "result": {
                "componentManifest": manifest,
                "livePreview": {
                    "available": False,
                    "status": "not_started",
                    "previewUrl": "",
                    "error": "",
                },
            },
        }

        with (
            patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value=remote_data),
            patch("content_factory.vibe_marketing_views._call_content_factory_live_preview") as preview_call,
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        preview_call.assert_not_called()
        self.assertEqual(response.data["livePreview"]["status"], "failed")
        self.assertIn("Missing required", response.data["livePreview"]["error"])
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["livePreview"]["status"], "failed")

    def test_live_preview_retry_posts_force_restart(self):
        preview_payload = {
            "available": False,
            "status": "failed",
            "previewUrl": "",
            "error": "Preview failed.",
        }

        with patch("content_factory.vibe_marketing_views._call_content_factory_live_preview", return_value=preview_payload) as preview_call:
            response = self.client.post(
                f"/api/v1/vibe-marketing/runs/{self.run.run_id}/live-preview",
                {"force": True},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        preview_call.assert_called_once_with(
            run_id=self.run.run_id,
            method="POST",
            payload={"force": True, "local_repo_path": ""},
        )
        self.assertEqual(response.data["livePreview"]["status"], "failed")

    def test_article_system_live_preview_failure_blocks_setup_run(self):
        setup_run = ContentFactoryRun.objects.create(
            run_id="article-system-preview-failed",
            workflow="article_system_setup",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.AWAITING_APPROVAL,
            current_step="await_review",
            approval_state=ContentFactoryApprovalState.APPROVAL_REQUIRED,
            result={
                "article_system_setup": {"status": "preview_building"},
                "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/2",
            },
        )
        preview_payload = {
            "available": False,
            "status": "failed",
            "platformStatus": "failed",
            "previewUrl": "",
            "error": "MLAI GitHub App cannot access MLAI-AUS-Inc/mlai-au.",
            "errorCode": "platform_preview_failed",
            "builderRunUrl": "https://github.com/MLAI-AUS-Inc/content-factory/actions/runs/21",
            "retryable": True,
        }

        with patch("content_factory.vibe_marketing_views._call_content_factory_live_preview", return_value=preview_payload):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{setup_run.run_id}/live-preview")

        self.assertEqual(response.status_code, 200)
        setup_run.refresh_from_db()
        self.assertEqual(setup_run.status, ContentFactoryRunStatus.BLOCKED)
        self.assertEqual(setup_run.current_step, "preview_failed")
        self.assertEqual(setup_run.approval_state, ContentFactoryApprovalState.NOT_REQUIRED)
        self.assertEqual(setup_run.result["article_system_setup"]["status"], "preview_failed")
        self.assertEqual(response.data["livePreview"]["builderRunUrl"], "https://github.com/MLAI-AUS-Inc/content-factory/actions/runs/21")

    def test_live_preview_retry_forwards_org_github_token(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.github_token_encrypted = "org-live-preview-token"
        config.save(update_fields=["github_token_encrypted", "updated_at"])
        preview_payload = {
            "available": False,
            "status": "starting",
            "previewUrl": "",
        }

        with patch("content_factory.vibe_marketing_views._call_content_factory_live_preview", return_value=preview_payload) as preview_call:
            response = self.client.post(
                f"/api/v1/vibe-marketing/runs/{self.run.run_id}/live-preview",
                {"force": True},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        preview_call.assert_called_once_with(
            run_id=self.run.run_id,
            method="POST",
            payload={
                "force": True,
                "local_repo_path": "",
                "github_token": "org-live-preview-token",
                "token_source": "github_oauth_user_token",
            },
        )

    @override_settings(
        CONTENT_FACTORY_URL="https://content-factory.test",
        CONTENT_FACTORY_API_KEY="secret-key",
        CONTENT_FACTORY_LIVE_PREVIEW_START_READ_TIMEOUT_SECONDS=12,
        IS_LOCAL_ENV=False,
    )
    def test_live_preview_post_timeout_stays_starting(self):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["timeout"] = timeout
            raise http_client.exceptions.ReadTimeout("Read timed out.")

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            payload = _call_content_factory_live_preview(
                run_id=self.run.run_id,
                method="POST",
                payload={"force": False},
            )

        self.assertEqual(payload["status"], "starting")
        self.assertEqual(payload["errorCode"], "preview_start_timeout")
        self.assertEqual(payload["error"], "")
        self.assertTrue(payload["retryable"])
        self.assertEqual(captured["timeout"], (3, 12))

    def test_comment_crud_serializes_anchor(self):
        response = self.client.post(
            f"/api/v1/vibe-marketing/runs/{self.run.run_id}/comments",
            {
                "componentId": "toc",
                "componentType": "toc",
                "componentLabel": "Table of contents",
                "selector": '[data-cf-component-id="toc"]',
                "anchor": {"x": 0.42, "y": 0.23, "createdFrom": "live_preview_click"},
                "context": {
                    "domPath": "body > main:nth-of-type(1) > nav:nth-of-type(1)",
                    "textHash": "12345",
                    "textExcerpt": "Table of contents",
                    "rect": {"left": 10, "top": 20, "width": 300, "height": 64},
                    "click": {"x": 24, "y": 40, "pageX": 24, "pageY": 140},
                    "viewport": {"width": 1440, "height": 900, "scrollY": 100, "devicePixelRatio": 2},
                    "pageUrl": "http://127.0.0.1:4321/articles/test?cfInspector=1",
                    "previewMode": "exact",
                },
                "body": "Make this easier to scan.",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["anchor"]["x"], 0.42)
        self.assertEqual(response.data["anchor"]["y"], 0.23)
        self.assertEqual(response.data["context"]["domPath"], "body > main:nth-of-type(1) > nav:nth-of-type(1)")
        self.assertEqual(response.data["context"]["textExcerpt"], "Table of contents")
        self.assertEqual(response.data["context"]["rect"]["height"], 64.0)

        comment_id = response.data["id"]
        patch_response = self.client.patch(
            f"/api/v1/vibe-marketing/runs/{self.run.run_id}/comments/{comment_id}",
            {
                "componentId": "toc",
                "componentType": "toc",
                "componentLabel": "Table of contents",
                "selector": '[data-cf-component-id="toc"]',
                "body": "Make this easier to scan and shorter.",
            },
            format="json",
        )

        self.assertEqual(patch_response.status_code, 200)
        self.assertEqual(patch_response.data["anchor"]["x"], 0.42)
        self.assertEqual(patch_response.data["anchor"]["y"], 0.23)
        self.assertEqual(patch_response.data["context"]["textHash"], "12345")

        list_response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/comments")
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.data["comments"][0]["anchor"]["createdFrom"], "live_preview_click")
        self.assertEqual(list_response.data["comments"][0]["context"]["previewMode"], "exact")

    def test_comment_crud_accepts_fixed_review_target_missing_from_manifest(self):
        self.run.result = {
            "componentManifest": {"components": [{"id": "title", "type": "title", "label": "Title"}]},
        }
        self.run.save(update_fields=["result", "updated_at"])

        response = self.client.post(
            f"/api/v1/vibe-marketing/runs/{self.run.run_id}/comments",
            {
                "componentId": "authoritative-references",
                "componentType": "references",
                "componentLabel": "Authoritative References",
                "selector": '[data-cf-component-id="authoritative-references"]',
                "body": "Use the generated article sources here.",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["componentId"], "authoritative-references")
        self.assertEqual(response.data["componentType"], "references")
        self.assertEqual(response.data["selector"], '[data-cf-component-id="authoritative-references"]')

    def test_revision_run_does_not_serialize_source_run_comments(self):
        VibeMarketingComponentComment.objects.create(
            run=self.run,
            actor=self.user,
            component_id="title",
            component_type="title",
            component_label="Title",
            selector='[data-cf-component-id="title"]',
            body="Original title feedback.",
            status="submitted",
            batch_id="batch-original",
        )
        revision_run = ContentFactoryRun.objects.create(
            run_id="article-run-comments-revision",
            workflow="article_revision",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.COMPLETED,
            run_request={"source_run_id": self.run.run_id},
            result={"source_run_id": self.run.run_id, "feedback_batch_id": "batch-original"},
        )

        response = self.client.get(f"/api/v1/vibe-marketing/runs/{revision_run.run_id}/comments")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["comments"], [])
        self.assertEqual(response.data["latestBatch"]["id"], "batch-original")
        self.assertEqual(response.data["latestBatch"]["sourceRunId"], self.run.run_id)
        self.assertEqual(response.data["latestBatch"]["revisionRunId"], revision_run.run_id)
        self.assertEqual(response.data["latestBatch"]["status"], "completed")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_cancel_article_run_marks_tombstone_and_hides_from_bootstrap(self):
        self.run.status = ContentFactoryRunStatus.RUNNING
        self.run.current_step = "draft_article"
        self.run.run_request = {"target_keyword": "ai marketing"}
        self.run.result = {
            "target_keyword": "ai marketing",
            "delivery_package": {
                "title": "AI Marketing",
                "target_keyword": "ai marketing",
                "article_markdown": "steps/package/article.md",
            },
        }
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.save(update_fields=["status", "current_step", "run_request", "result", "acceptance_summary", "updated_at"])
        ContentFactoryRunStep.objects.create(
            run=self.run,
            step_key="draft_article",
            display_order=1,
            status="running",
            artifacts=["steps/draft_article/attempt-01/artifacts/article.md"],
        )
        VibeMarketingComponentComment.objects.create(
            run=self.run,
            actor=self.user,
            component_id="title",
            component_type="title",
            component_label="Title",
            selector='[data-cf-component-id="title"]',
            body="Tighten this title.",
        )
        ResearchedKeyword.objects.create(
            organization=self.organization,
            keyword="ai marketing",
            keyword_normalized="ai marketing",
            status=KeywordStatus.IN_PROGRESS,
        )

        def fake_post(url, json=None, headers=None, timeout=None):
            self.assertTrue(url.endswith(f"/api/runs/{self.run.run_id}/cancel"))
            return _Response(
                status_code=202,
                payload={"run_id": self.run.run_id, "status": "cancelled", "cleanup": {"artifacts": {"deleted_paths": ["article.md"]}}},
            )

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/cancel", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.CANCELLED)
        self.assertFalse(response.data["contentPackage"])
        self.assertFalse(VibeMarketingComponentComment.objects.filter(run=self.run).exists())
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, ContentFactoryRunStatus.CANCELLED)
        self.assertEqual(self.run.current_step, "cancelled")
        self.assertTrue(self.run.result["cancelled"])
        self.assertEqual(self.run.steps.get(step_key="draft_article").status, "cancelled")
        keyword = ResearchedKeyword.objects.get(organization=self.organization, keyword_normalized="ai marketing")
        self.assertEqual(keyword.status, KeywordStatus.PENDING)

        bootstrap = self.client.get("/api/v1/vibe-marketing/bootstrap/")
        self.assertEqual(bootstrap.status_code, 200)
        self.assertNotIn(self.run.run_id, [item["runId"] for item in bootstrap.data["latestRuns"]])

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_cancel_group_removes_all_dedup_attempts_and_releases_topic(self):
        source_run_id = "disc-ai-detectors"
        keyword = "how do ai detectors work"
        attempts = [
            ContentFactoryRun.objects.create(
                run_id=f"ai-detectors-{idx}",
                workflow="article_generation",
                domain="mlai.au",
                github_repo="MLAI-AUS-Inc/mlai-au",
                status=ContentFactoryRunStatus.BLOCKED,
                run_request={"source_run_id": source_run_id, "target_keyword": keyword},
                result={"target_keyword": keyword},
            )
            for idx in range(3)
        ]
        ResearchedKeyword.objects.create(
            organization=self.organization,
            keyword=keyword,
            keyword_normalized=keyword,
            status=KeywordStatus.IN_PROGRESS,
        )
        other = ContentFactoryRun.objects.create(
            run_id="unrelated-topic",
            workflow="article_generation",
            domain="mlai.au",
            status=ContentFactoryRunStatus.BLOCKED,
            run_request={"source_run_id": "disc-other", "target_keyword": "unrelated topic"},
            result={"target_keyword": "unrelated topic"},
        )

        def fake_post(url, json=None, headers=None, timeout=None):
            return _Response(status_code=202, payload={"status": "cancelled", "cleanup": {}})

        representative = attempts[0]
        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(
                f"/api/v1/vibe-marketing/runs/{representative.run_id}/cancel",
                {"cancel_group": True},
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(sorted(response.data["cancelledRunIds"]), sorted(a.run_id for a in attempts))
        self.assertEqual(response.data["protectedRunIds"], [])
        for attempt in attempts:
            attempt.refresh_from_db()
            self.assertEqual(attempt.status, ContentFactoryRunStatus.CANCELLED)
        other.refresh_from_db()
        self.assertEqual(other.status, ContentFactoryRunStatus.BLOCKED)
        released = ResearchedKeyword.objects.get(organization=self.organization, keyword_normalized=keyword)
        self.assertEqual(released.status, KeywordStatus.PENDING)

        bootstrap = self.client.get("/api/v1/vibe-marketing/bootstrap/?view=summary")
        self.assertEqual(bootstrap.status_code, 200)
        draft_run_ids = {item["runId"] for item in bootstrap.data["draftArticles"]}
        draft_source_run_ids = {item.get("sourceRunId") for item in bootstrap.data["draftArticles"]}
        self.assertTrue(draft_run_ids.isdisjoint({attempt.run_id for attempt in attempts}))
        self.assertNotIn(source_run_id, draft_source_run_ids)
        self.assertIn(other.run_id, draft_run_ids)

    # --- Revision lineage collapses to one dashboard card ------------------

    def _set_updated_at(self, run, when):
        # updated_at is auto_now; bypass it to control draft-scan ordering.
        ContentFactoryRun.objects.filter(pk=run.pk).update(updated_at=when)

    def _make_article_run(self, run_id, *, workflow, status, source_run_id="", keyword="", updated_at=None):
        run_request = {"target_keyword": keyword} if keyword else {}
        result = {"target_keyword": keyword} if keyword else {}
        if source_run_id:
            run_request["source_run_id"] = source_run_id
            result["source_run_id"] = source_run_id
        run = ContentFactoryRun.objects.create(
            run_id=run_id,
            workflow=workflow,
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=status,
            run_request=run_request,
            result=result,
        )
        if updated_at is not None:
            self._set_updated_at(run, updated_at)
        return run

    def _lineage_cards(self, drafts, run_ids, root_run_id):
        return [
            d
            for d in drafts
            if d["runId"] in run_ids or d.get("sourceRunId") == root_run_id
        ]

    def test_resolve_article_root_run_id_walks_chain_and_guards(self):
        # full chain C <- B <- A(root)
        chain = {"C": "B", "B": "A", "A": ""}
        self.assertEqual(_resolve_article_root_run_id("C", chain), "A")
        self.assertEqual(_resolve_article_root_run_id("A", chain), "A")
        # ancestor outside the scanned window: source present but not a key
        self.assertEqual(_resolve_article_root_run_id("B", {"B": "A"}), "A")
        # self-root (no source)
        self.assertEqual(_resolve_article_root_run_id("A", {"A": ""}), "A")
        # cycle must terminate and return a stable id
        self.assertIn(_resolve_article_root_run_id("A", {"A": "B", "B": "A"}), {"A", "B"})

    def test_chained_revisions_collapse_into_single_draft_card(self):
        now = timezone.now()
        keyword = "how do ai detectors work"
        root = self._make_article_run("ad-root", workflow="article_generation", status=ContentFactoryRunStatus.BLOCKED, keyword=keyword, updated_at=now - timedelta(minutes=10))
        rev1 = self._make_article_run("ad-rev1", workflow="article_revision", status=ContentFactoryRunStatus.COMPLETED, source_run_id=root.run_id, keyword=keyword, updated_at=now - timedelta(minutes=5))
        rev2 = self._make_article_run("ad-rev2", workflow="article_revision", status=ContentFactoryRunStatus.BLOCKED, source_run_id=rev1.run_id, keyword=keyword, updated_at=now - timedelta(minutes=1))

        bootstrap = self.client.get("/api/v1/vibe-marketing/bootstrap/?view=summary")
        self.assertEqual(bootstrap.status_code, 200)
        drafts = bootstrap.data["draftArticles"]
        lineage = self._lineage_cards(drafts, {root.run_id, rev1.run_id, rev2.run_id}, root.run_id)
        self.assertEqual(len(lineage), 1, lineage)
        card = lineage[0]
        self.assertEqual(card["runId"], rev2.run_id)  # newest member represents the lineage
        self.assertEqual(card["sourceRunId"], root.run_id)  # stable lineage key = root
        self.assertEqual(card["stageLabel"], "Needs attention")  # reflects the latest (blocked) edit

    def test_revision_card_reflects_latest_blocked_over_earlier_ready(self):
        now = timezone.now()
        keyword = "ai detector accuracy"
        root = self._make_article_run("acc-root", workflow="article_generation", status=ContentFactoryRunStatus.AWAITING_APPROVAL, keyword=keyword, updated_at=now - timedelta(minutes=10))
        rev = self._make_article_run("acc-rev", workflow="article_revision", status=ContentFactoryRunStatus.BLOCKED, source_run_id=root.run_id, keyword=keyword, updated_at=now - timedelta(minutes=1))

        drafts = self.client.get("/api/v1/vibe-marketing/bootstrap/?view=summary").data["draftArticles"]
        lineage = self._lineage_cards(drafts, {root.run_id, rev.run_id}, root.run_id)
        self.assertEqual(len(lineage), 1, lineage)
        self.assertEqual(lineage[0]["runId"], rev.run_id)
        self.assertEqual(lineage[0]["stageLabel"], "Needs attention")

    def test_written_original_hidden_revision_visible_single_card(self):
        now = timezone.now()
        keyword = "detector false positives"
        WrittenArticle.objects.create(
            organization=self.organization,
            title="Detector False Positives",
            slug="detector-false-positives",
            category="featured",
            primary_keyword=keyword,
        )
        root = self._make_article_run("fp-root", workflow="article_generation", status=ContentFactoryRunStatus.COMPLETED, keyword=keyword, updated_at=now - timedelta(minutes=10))
        rev = self._make_article_run("fp-rev", workflow="article_revision", status=ContentFactoryRunStatus.BLOCKED, source_run_id=root.run_id, keyword=keyword, updated_at=now - timedelta(minutes=1))

        drafts = self.client.get("/api/v1/vibe-marketing/bootstrap/?view=summary").data["draftArticles"]
        draft_run_ids = {d["runId"] for d in drafts}
        # the published/written original is hidden; only the revision remains, once.
        self.assertNotIn(root.run_id, draft_run_ids)
        lineage = self._lineage_cards(drafts, {root.run_id, rev.run_id}, root.run_id)
        self.assertEqual(len(lineage), 1, lineage)
        self.assertEqual(lineage[0]["runId"], rev.run_id)

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_cancel_group_cancels_full_revision_lineage(self):
        keyword = "ai detector lineage"
        root = self._make_article_run("lin-root", workflow="article_generation", status=ContentFactoryRunStatus.BLOCKED, keyword=keyword)
        rev1 = self._make_article_run("lin-rev1", workflow="article_revision", status=ContentFactoryRunStatus.BLOCKED, source_run_id=root.run_id, keyword=keyword)
        rev2 = self._make_article_run("lin-rev2", workflow="article_revision", status=ContentFactoryRunStatus.BLOCKED, source_run_id=rev1.run_id, keyword=keyword)
        unrelated = self._make_article_run("lin-unrelated", workflow="article_generation", status=ContentFactoryRunStatus.BLOCKED, keyword="different topic")

        def fake_post(url, json=None, headers=None, timeout=None):
            return _Response(status_code=202, payload={"status": "cancelled", "cleanup": {}})

        # Deleting the visible card (newest = rev2) must cancel the whole lineage.
        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(
                f"/api/v1/vibe-marketing/runs/{rev2.run_id}/cancel",
                {"cancel_group": True},
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            sorted(response.data["cancelledRunIds"]),
            sorted([root.run_id, rev1.run_id, rev2.run_id]),
        )
        for run in (root, rev1, rev2):
            run.refresh_from_db()
            self.assertEqual(run.status, ContentFactoryRunStatus.CANCELLED)
        unrelated.refresh_from_db()
        self.assertEqual(unrelated.status, ContentFactoryRunStatus.BLOCKED)

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_cancel_group_skips_publish_protected_attempts_and_reports_them(self):
        source_run_id = "disc-protected-ai-detectors"
        keyword = "how ai detector reports work"
        cancellable_one = ContentFactoryRun.objects.create(
            run_id="ai-detectors-cancellable-one",
            workflow="article_generation",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.BLOCKED,
            run_request={"source_run_id": source_run_id, "target_keyword": keyword},
            result={"target_keyword": keyword},
        )
        protected = ContentFactoryRun.objects.create(
            run_id="ai-detectors-protected",
            workflow="article_generation",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.BLOCKED,
            run_request={"source_run_id": source_run_id, "target_keyword": keyword},
            result={
                "target_keyword": keyword,
                "draft_pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/77",
            },
        )
        cancellable_two = ContentFactoryRun.objects.create(
            run_id="ai-detectors-cancellable-two",
            workflow="article_generation",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.FAILED,
            run_request={"source_run_id": source_run_id, "target_keyword": keyword},
            result={"target_keyword": keyword},
        )

        cancelled_urls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            cancelled_urls.append(url)
            return _Response(status_code=202, payload={"status": "cancelled", "cleanup": {}})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(
                f"/api/v1/vibe-marketing/runs/{cancellable_one.run_id}/cancel",
                {"cancel_group": True},
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            sorted(response.data["cancelledRunIds"]),
            sorted([cancellable_one.run_id, cancellable_two.run_id]),
        )
        self.assertEqual(response.data["protectedRunIds"], [protected.run_id])
        self.assertTrue(any(cancellable_one.run_id in url for url in cancelled_urls))
        self.assertTrue(any(cancellable_two.run_id in url for url in cancelled_urls))
        self.assertFalse(any(protected.run_id in url for url in cancelled_urls))

        cancellable_one.refresh_from_db()
        cancellable_two.refresh_from_db()
        protected.refresh_from_db()
        self.assertEqual(cancellable_one.status, ContentFactoryRunStatus.CANCELLED)
        self.assertEqual(cancellable_two.status, ContentFactoryRunStatus.CANCELLED)
        self.assertEqual(protected.status, ContentFactoryRunStatus.BLOCKED)

        bootstrap = self.client.get("/api/v1/vibe-marketing/bootstrap/?view=summary")
        self.assertEqual(bootstrap.status_code, 200)
        draft_run_ids = {item["runId"] for item in bootstrap.data["draftArticles"]}
        draft_source_run_ids = {item.get("sourceRunId") for item in bootstrap.data["draftArticles"]}
        self.assertIn(protected.run_id, draft_run_ids)
        self.assertIn(source_run_id, draft_source_run_ids)
        self.assertNotIn(cancellable_one.run_id, draft_run_ids)
        self.assertNotIn(cancellable_two.run_id, draft_run_ids)

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_submit_from_completed_revision_uses_revision_draft_comments(self):
        revision_run = ContentFactoryRun.objects.create(
            run_id="article-run-comments-revision",
            workflow="article_revision",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.COMPLETED,
            run_request={"source_run_id": self.run.run_id},
            result={"source_run_id": self.run.run_id, "feedback_batch_id": "batch-original"},
        )
        source_comment = VibeMarketingComponentComment.objects.create(
            run=self.run,
            actor=self.user,
            component_id="title",
            component_type="title",
            component_label="Title",
            selector='[data-cf-component-id="title"]',
            body="Original title feedback.",
            status="submitted",
            batch_id="batch-original",
        )
        revision_comment = VibeMarketingComponentComment.objects.create(
            run=revision_run,
            actor=self.user,
            component_id="section:section-2",
            component_type="section",
            component_label="Section 2",
            selector='[data-cf-component-id="section:section-2"]',
            body="Tighten the revised section.",
        )

        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            return _Response(status_code=202, payload={"run_id": "article-run-comments-revision-2", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{revision_run.run_id}/comments/submit", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["url"], "https://content-factory.test/api/runs/article-run-comments-revision/component-revisions")
        self.assertEqual(captured["payload"]["source_run_id"], revision_run.run_id)
        self.assertEqual(captured["payload"]["comments"][0]["comment_id"], str(revision_comment.id))
        self.assertNotEqual(captured["payload"]["comments"][0]["comment_id"], str(source_comment.id))

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_submit_sends_anchor_to_content_factory_revision(self):
        comment = VibeMarketingComponentComment.objects.create(
            run=self.run,
            actor=self.user,
            component_id="section:section-1",
            component_type="section",
            component_label="Intro section",
            selector='[data-cf-component-id="section:section-1"]',
            anchor={"x": 0.25, "y": 0.75, "createdFrom": "live_preview_click"},
            context={
                "domPath": "body > main:nth-of-type(1) > section:nth-of-type(1)",
                "textExcerpt": "Intro section visible copy",
                "click": {"x": 120, "y": 300},
                "viewport": {"width": 1280, "height": 800},
                "pageUrl": "http://127.0.0.1:4321/articles/test?cfInspector=1",
            },
            body="Tighten this section.",
        )

        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            captured["timeout"] = timeout
            return _Response(status_code=202, payload={"run_id": "article-run-comments-revision", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/comments/submit", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["url"], "https://content-factory.test/api/runs/article-run-comments/component-revisions")
        self.assertEqual(captured["timeout"], (5, 90))
        self.assertTrue(captured["payload"]["requested_run_id"].startswith("component-revision-"))
        remote_comment = captured["payload"]["comments"][0]
        self.assertEqual(remote_comment["comment_id"], str(comment.id))
        self.assertEqual(remote_comment["anchor"], {"x": 0.25, "y": 0.75, "createdFrom": "live_preview_click"})
        self.assertEqual(remote_comment["context"]["textExcerpt"], "Intro section visible copy")
        self.assertEqual(remote_comment["context"]["click"]["x"], 120)

        comment.refresh_from_db()
        self.assertEqual(comment.status, "submitted")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_submit_component_revision_reuses_original_article_billing_without_balance_gate(self):
        _config, account = self._prepare_billable_vibe_context(balance=0)
        self.run.domain = "example.com"
        self.run.github_repo = "example/site"
        self.run.run_request = {
            "topic": "Reliable Content Harnesses",
            "target_keyword": "content harness",
            "delivery_mode": "content_only",
            "roo_points_authorized": True,
            "roo_points_action": "article_generation",
            "roo_points_cost": 6,
            "roo_points_required": 6,
            "roo_points_billing_status": "charged",
            "roo_points_ledger_id": "ledger-article-original",
        }
        self.run.save(update_fields=["domain", "github_repo", "run_request", "updated_at"])
        comment = VibeMarketingComponentComment.objects.create(
            run=self.run,
            actor=self.user,
            component_id="title",
            component_type="title",
            component_label="Title",
            selector='[data-cf-component-id="title"]',
            body="Make the title sharper.",
        )
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            return _Response(status_code=202, payload={"run_id": "article-run-comments-revision-paid", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/comments/submit", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["url"], "https://content-factory.test/api/runs/article-run-comments/component-revisions")
        self.assertEqual(captured["payload"]["roo_points_action"], "article_generation")
        self.assertEqual(captured["payload"]["roo_points_billing_status"], "reused")
        self.assertEqual(captured["payload"]["roo_points_ledger_id"], "ledger-article-original")
        comment.refresh_from_db()
        self.assertEqual(comment.status, "submitted")
        account.refresh_from_db()
        self.assertEqual(account.balance, 0)
        self.assertEqual(account.lifetime_spent, 0)

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_submit_component_revision_requires_original_article_billing(self):
        _config, account = self._prepare_billable_vibe_context(balance=0)
        self.run.domain = "example.com"
        self.run.github_repo = "example/site"
        self.run.run_request = {
            "topic": "Legacy Content Harnesses",
            "target_keyword": "legacy content harness",
            "delivery_mode": "content_only",
        }
        self.run.save(update_fields=["domain", "github_repo", "run_request", "updated_at"])
        comment = VibeMarketingComponentComment.objects.create(
            run=self.run,
            actor=self.user,
            component_id="title",
            component_type="title",
            component_label="Title",
            selector='[data-cf-component-id="title"]',
            body="Make the title sharper.",
        )

        with patch("content_factory.vibe_marketing_views.http_client.post") as post:
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/comments/submit", {}, format="json")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "roo_points_billing_required")
        post.assert_not_called()
        comment.refresh_from_db()
        self.assertEqual(comment.status, "draft")
        account.refresh_from_db()
        self.assertEqual(account.balance, 0)

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_article_system_revision_submits_setup_pinned_comments(self):
        setup_run = ContentFactoryRun.objects.create(
            run_id="article-system-setup-comments",
            workflow="article_system_setup",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.AWAITING_APPROVAL,
            current_step="await_review",
            result={
                "livePreview": {
                    "available": True,
                    "status": "running",
                    "previewUrl": "https://preview.example/articles?cfInspector=1",
                    "inspectorProtocolVersion": 2,
                    "inspectorMode": "comment",
                },
            },
        )
        comment = VibeMarketingComponentComment.objects.create(
            run=setup_run,
            actor=self.user,
            component_id="article-system-boundary-main",
            component_type="section",
            component_label="Articles listing",
            selector='[data-cf-component-id="article-system-boundary-main"]',
            anchor={"x": 0.4, "y": 0.2, "createdFrom": "live_preview_click"},
            context={
                "domPath": "body > main:nth-of-type(1)",
                "textExcerpt": "Articles",
                "click": {"x": 500, "y": 240},
                "viewport": {"width": 1440, "height": 900},
                "pageUrl": "https://preview.example/articles?cfInspector=1",
                "previewMode": "platform_deployment",
            },
            body="Make the article cards denser.",
        )
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            return _Response(
                status_code=202,
                payload={
                    "run_id": setup_run.run_id,
                    "status": "revision_preview_building",
                    "livePreview": {
                        "available": True,
                        "status": "running",
                        "previewUrl": "https://preview.example/articles?cfInspector=1",
                        "inspectorProtocolVersion": 2,
                        "inspectorMode": "comment",
                    },
                },
            )

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(
                f"/api/v1/vibe-marketing/runs/{setup_run.run_id}/article-system-revisions",
                {},
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["url"], "https://content-factory.test/api/runs/article-system-setup-comments/article-system-revisions")
        self.assertEqual(captured["payload"]["source_run_id"], setup_run.run_id)
        self.assertEqual(captured["payload"]["request_source"], "founder_tools_article_system_feedback")
        remote_comment = captured["payload"]["comments"][0]
        self.assertEqual(remote_comment["comment_id"], str(comment.id))
        self.assertEqual(remote_comment["selector"], '[data-cf-component-id="article-system-boundary-main"]')
        self.assertEqual(remote_comment["anchor"]["createdFrom"], "live_preview_click")
        self.assertEqual(remote_comment["context"]["previewMode"], "platform_deployment")
        comment.refresh_from_db()
        self.assertEqual(comment.status, "submitted")
        setup_run.refresh_from_db()
        self.assertEqual(setup_run.status, ContentFactoryRunStatus.RUNNING)
        self.assertEqual(setup_run.current_step, "revision_preview_building")
        self.assertEqual(setup_run.result["component_feedback_latest_batch"]["status"], "running")
        self.assertEqual(response.data["componentFeedback"]["latestBatch"]["status"], "running")
        self.assertEqual(response.data["livePreview"]["inspectorMode"], "comment")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_article_system_revision_keeps_freeform_body_compatibility(self):
        setup_run = ContentFactoryRun.objects.create(
            run_id="article-system-setup-freeform",
            workflow="article_system_setup",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.AWAITING_APPROVAL,
            current_step="await_review",
        )
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["payload"] = json
            return _Response(status_code=202, payload={"run_id": setup_run.run_id, "status": "revision_preview_building"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(
                f"/api/v1/vibe-marketing/runs/{setup_run.run_id}/article-system-revisions",
                {
                    "body": "Add more spacing around the hero.",
                    "feedbackBatchId": "freeform-batch-1",
                    "selector": '[data-cf-component-id="article-system-hero"]',
                },
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["payload"]["feedback_batch_id"], "freeform-batch-1")
        self.assertEqual(captured["payload"]["comments"][0]["body"], "Add more spacing around the hero.")
        self.assertEqual(captured["payload"]["comments"][0]["selector"], '[data-cf-component-id="article-system-hero"]')

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_article_system_revision_surfaces_retryable_content_factory_failure(self):
        # A Content Factory 5xx is retryable; the view must NOT report it as a 202 (which the
        # frontend would treat as a successful send and silently redirect), but as a 502 while
        # still persisting the batch as submitted/retryable so "Retry revision comments" works.
        setup_run = ContentFactoryRun.objects.create(
            run_id="article-system-setup-cf-500",
            workflow="article_system_setup",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.AWAITING_APPROVAL,
            current_step="await_review",
            result={},
        )
        comment = VibeMarketingComponentComment.objects.create(
            run=setup_run,
            actor=self.user,
            component_id="article-system-boundary-main",
            component_type="section",
            component_label="Articles listing",
            selector='[data-cf-component-id="article-system-boundary-main"]',
            body="Match the dark forest chrome from arb-gen.com.",
        )

        def fake_post(url, json=None, headers=None, timeout=None):
            return _Response(
                status_code=500,
                payload={"detail": "No active attempt for step 'await_review'"},
            )

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(
                f"/api/v1/vibe-marketing/runs/{setup_run.run_id}/article-system-revisions",
                {},
                format="json",
            )

        self.assertEqual(response.status_code, 502)
        self.assertTrue(response.data.get("retryable"))
        self.assertIn("await_review", response.data.get("detail", ""))
        # The batch is preserved as retryable so the reviewer can retry once CF recovers.
        comment.refresh_from_db()
        self.assertEqual(comment.status, "submitted")
        setup_run.refresh_from_db()
        batch = setup_run.result["component_feedback_latest_batch"]
        self.assertEqual(batch["status"], "submitted")
        self.assertTrue(batch["retryable"])
        # The run must NOT be flipped to RUNNING/building on a failed send.
        self.assertEqual(setup_run.status, ContentFactoryRunStatus.AWAITING_APPROVAL)
        self.assertEqual(setup_run.current_step, "await_review")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_submit_retries_latest_submitted_batch(self):
        comment = VibeMarketingComponentComment.objects.create(
            run=self.run,
            actor=self.user,
            component_id="title",
            component_type="title",
            component_label="Title",
            selector='[data-cf-component-id="title"]',
            body="Make the title sharper.",
            status="submitted",
            batch_id="batch-retry-1",
        )

        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["payload"] = json
            return _Response(status_code=202, payload={"run_id": "article-run-comments-retry", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/comments/submit", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["payload"]["feedback_batch_id"], "batch-retry-1")
        self.assertEqual(captured["payload"]["comments"][0]["comment_id"], str(comment.id))

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_submit_from_failed_revision_retries_source_batch(self):
        revision_run = ContentFactoryRun.objects.create(
            run_id="article-run-comments-revision",
            workflow="article_revision",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.FAILED,
            run_request={"source_run_id": self.run.run_id},
            result={"source_run_id": self.run.run_id, "feedback_batch_id": "batch-retry-2"},
        )
        comment = VibeMarketingComponentComment.objects.create(
            run=self.run,
            actor=self.user,
            component_id="title",
            component_type="title",
            component_label="Title",
            selector='[data-cf-component-id="title"]',
            body="Make the title sharper.",
            status="submitted",
            batch_id="batch-retry-2",
        )

        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            return _Response(status_code=202, payload={"run_id": "article-run-comments-revision-retry-2", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{revision_run.run_id}/comments/submit", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["url"], "https://content-factory.test/api/runs/article-run-comments/component-revisions")
        self.assertEqual(captured["payload"]["source_run_id"], self.run.run_id)
        self.assertEqual(captured["payload"]["feedback_batch_id"], "batch-retry-2")
        self.assertEqual(captured["payload"]["comments"][0]["comment_id"], str(comment.id))
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["component_feedback_revision_run_id"], "article-run-comments-revision-retry-2")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_submit_keeps_retryable_timeout_batch_submitted(self):
        comment = VibeMarketingComponentComment.objects.create(
            run=self.run,
            actor=self.user,
            component_id="title",
            component_type="title",
            component_label="Title",
            selector='[data-cf-component-id="title"]',
            body="Make the title sharper.",
        )

        def fake_post(url, json=None, headers=None, timeout=None):
            raise http_client.RequestException("Read timed out.")

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/comments/submit", {}, format="json")

        self.assertEqual(response.status_code, 202)
        comment.refresh_from_db()
        self.assertEqual(comment.status, "submitted")
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["component_feedback_latest_batch"]["status"], "submitted")
        self.assertTrue(self.run.result["component_feedback_latest_batch"]["retryable"])

    def test_workflow_progress_marks_package_complete_before_publish(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.github_token_encrypted = "encrypted-token"
        config.github_repo = "MLAI-AUS-Inc/mlai-au"
        config.company_context = "MLAI helps Australian founders adopt AI."
        config.article_delivery_mode = "content_only"
        config.baseline_skipped_at = config.updated_at
        config.article_system = {"state": "existing", "confidence": "high", "directory_name": "articles"}
        config.publish_targets = [{"id": "articles", "label": "Articles"}]
        config.save(
            update_fields=[
                "github_token_encrypted",
                "github_repo",
                "company_context",
                "article_delivery_mode",
                "baseline_skipped_at",
                "article_system",
                "publish_targets",
                "updated_at",
            ]
        )
        self.organization.seed_keywords = ["australian founders"]
        self.organization.save(update_fields=["seed_keywords"])
        ResearchedKeyword.objects.create(
            organization=self.organization,
            keyword="australian founders",
            volume=700,
            difficulty=30,
            opportunity_index=80,
            status=KeywordStatus.PENDING,
        )
        ResearchedKeyword.objects.create(
            organization=self.organization,
            keyword="australian founders",
            volume=700,
            difficulty=30,
            opportunity_index=80,
            status=KeywordStatus.PENDING,
        )
        self.run.run_request = {"delivery_mode": "content_only"}
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.result = {
            "delivery_mode": "content_only",
            "promote_bundle_url": f"/api/runs/{self.run.run_id}/promote-bundle",
            "delivery_package": {
                "title": "Australian Founders and What the Term Means Today",
                "target_keyword": "australian founders",
                "article_markdown": "steps/package_content_delivery/attempt-01/artifacts/article.md",
            },
        }
        self.run.save(update_fields=["run_request", "acceptance_summary", "result", "updated_at"])

        with patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        progress = response.data["workflowProgress"]
        steps = {step["id"]: step for step in progress["steps"]}
        self.assertEqual(steps["package"]["status"], "complete")
        self.assertEqual(steps["publish"]["status"], "ready")
        self.assertEqual(steps["publish"]["primaryAction"]["intent"], "promote-bundle")
        self.assertNotEqual(steps["publish"]["status"], "complete")

    def test_workflow_progress_review_step_links_to_article_preview(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.github_token_encrypted = "encrypted-token"
        config.github_repo = "MLAI-AUS-Inc/mlai-au"
        config.company_context = "MLAI helps Australian founders adopt AI."
        config.article_delivery_mode = "content_only"
        config.baseline_skipped_at = config.updated_at
        config.article_system = {"state": "existing", "confidence": "high", "directory_name": "articles"}
        config.publish_targets = [{"id": "articles", "label": "Articles"}]
        config.save(
            update_fields=[
                "github_token_encrypted",
                "github_repo",
                "company_context",
                "article_delivery_mode",
                "baseline_skipped_at",
                "article_system",
                "publish_targets",
                "updated_at",
            ]
        )
        self.run.run_request = {"delivery_mode": "content_only"}
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.result = {
            "delivery_mode": "content_only",
            "promote_bundle_url": f"/api/runs/{self.run.run_id}/promote-bundle",
            "delivery_package": {
                "title": "Australian Founders and What the Term Means Today",
                "target_keyword": "australian founders",
                "article_markdown": "steps/package_content_delivery/attempt-01/artifacts/article.md",
            },
        }
        self.run.save(update_fields=["run_request", "acceptance_summary", "result", "updated_at"])

        with patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        steps = {step["id"]: step for step in response.data["workflowProgress"]["steps"]}
        # The review step (and its action) deep-links to the article preview so the
        # user can reopen it at any point in the flow.
        self.assertIn("articleStep=review", steps["review"]["primaryAction"]["href"])

    def test_workflow_progress_keeps_research_ready_when_only_stored_candidates_exist(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.github_token_encrypted = "encrypted-token"
        config.github_repo = "MLAI-AUS-Inc/mlai-au"
        config.company_context = "MLAI helps Australian founders adopt AI."
        config.article_delivery_mode = "content_only"
        config.baseline_skipped_at = config.updated_at
        config.article_system = {"state": "existing", "confidence": "high", "directory_name": "articles"}
        config.publish_targets = [{"id": "articles", "label": "Articles"}]
        config.save(
            update_fields=[
                "github_token_encrypted",
                "github_repo",
                "company_context",
                "article_delivery_mode",
                "baseline_skipped_at",
                "article_system",
                "publish_targets",
                "updated_at",
            ]
        )
        self.organization.seed_keywords = ["australian founders"]
        self.organization.save(update_fields=["seed_keywords"])
        self._create_mlai_featured_components()
        ResearchedKeyword.objects.create(
            organization=self.organization,
            keyword="australian founders",
            volume=700,
            difficulty=30,
            opportunity_index=80,
            status=KeywordStatus.PENDING,
        )
        self.run.status = ContentFactoryRunStatus.CANCELLED
        self.run.save(update_fields=["status", "updated_at"])

        with patch("content_factory.vibe_marketing_views.google_baseline_connection_status", return_value={}):
            response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(response.data["topicCandidates"]), 0)
        self.assertFalse(response.data["checks"]["research"]["passed"])
        progress = response.data["workflowProgress"]
        steps = {step["id"]: step for step in progress["steps"]}
        self.assertEqual(progress["currentStepId"], "research")
        self.assertEqual(steps["research"]["status"], "ready")
        self.assertEqual(steps["research"]["primaryAction"]["label"], "Start topic research")
        self.assertEqual(steps["choose_topic"]["status"], "locked")
        self.assertIsNone(steps["choose_topic"]["primaryAction"])

    def test_first_time_articles_setup_preview_blocks_research_and_article_actions(self):
        self._prepare_articles_setup_gate(status="preview_ready")
        ContentFactoryRun.objects.create(
            run_id="setup-gate-run",
            workflow="article_system_setup",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.AWAITING_APPROVAL,
            current_step="await_review",
            approval_state=ContentFactoryApprovalState.APPROVAL_REQUIRED,
            result={
                "status": "preview_ready",
                "setup_run_id": "setup-gate-run",
                "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/12",
                "preview_url": "https://preview.example/articles",
                "article_system_setup": {"status": "preview_ready", "setup_run_id": "setup-gate-run"},
            },
        )

        with patch("content_factory.vibe_marketing_views.google_baseline_connection_status", return_value={}):
            response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        scaffold = response.data["checks"]["scaffold"]
        self.assertFalse(scaffold["passed"])
        self.assertFalse(scaffold["published"])
        self.assertTrue(scaffold["setupBlocked"])
        progress = response.data["workflowProgress"]
        steps = {step["id"]: step for step in progress["steps"]}
        self.assertEqual(steps["research"]["status"], "locked")
        self.assertEqual(steps["choose_topic"]["status"], "locked")

        discovery_response = self.client.post("/api/v1/vibe-marketing/discovery/", {}, format="json")
        self.assertEqual(discovery_response.status_code, 409)
        self.assertEqual(discovery_response.data["code"], "article_system_setup_blocked")

        article_response = self.client.post(
            "/api/v1/vibe-marketing/article/",
            {"topic": "AI adoption", "targetKeyword": "ai adoption"},
            format="json",
        )
        self.assertEqual(article_response.status_code, 409)
        self.assertEqual(article_response.data["code"], "article_system_setup_blocked")

    def test_code_review_ready_setup_moves_wizard_to_review_step(self):
        # Server-rendered stacks get no hosted preview; the run parks in
        # code_review_ready (content-factory#599). The wizard must advance to
        # the review step with a link to the run page — previously this status
        # fell through to the else branch and the wizard sat on "Build setup
        # page" forever with no review affordance (arb-gen.com run ad1ea21b).
        config = self._prepare_articles_setup_gate(status="code_review_ready")
        article_system = dict(config.article_system)
        pending = dict(article_system["pending_article_system_setup"])
        for key in ("preview_url", "previewUrl", "pr_url", "prUrl"):
            pending.pop(key, None)
        pending["approveUrl"] = "/api/runs/setup-gate-run/approve"
        pending["previewRuntimeUnsupported"] = True
        article_system["pending_article_system_setup"] = pending
        config.article_system = article_system
        config.save(update_fields=["article_system", "updated_at"])
        ContentFactoryRun.objects.create(
            run_id="setup-gate-run",
            workflow="article_system_setup",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.AWAITING_APPROVAL,
            current_step="await_review",
            approval_state=ContentFactoryApprovalState.APPROVAL_REQUIRED,
            result={
                "status": "code_review_ready",
                "setup_run_id": "setup-gate-run",
                "preview_url": "",
                "approve_url": "/api/runs/setup-gate-run/approve",
                "preview_runtime_unsupported": True,
                "article_system_setup": {"status": "code_review_ready", "setup_run_id": "setup-gate-run"},
            },
        )

        with patch("content_factory.vibe_marketing_views.google_baseline_connection_status", return_value={}):
            response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        scaffold = response.data["checks"]["scaffold"]
        self.assertTrue(scaffold["setupBlocked"])
        steps = {step["id"]: step for step in response.data["workflowProgress"]["steps"]}
        self.assertEqual(steps["generate"]["status"], "complete")
        self.assertEqual(steps["review"]["status"], "needs_action")
        self.assertEqual(steps["review"]["primaryAction"]["label"], "Review setup changes")
        self.assertIn("setup-gate-run", steps["review"]["primaryAction"]["href"])

    def _create_code_review_ready_run(self):
        return ContentFactoryRun.objects.create(
            run_id="setup-gate-run",
            workflow="article_system_setup",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.AWAITING_APPROVAL,
            current_step="await_review",
            approval_state=ContentFactoryApprovalState.APPROVAL_REQUIRED,
            result={
                "setup_status": "code_review_ready",
                "setup_run_id": "setup-gate-run",
                "preview_url": "",
                "approve_url": "/api/runs/setup-gate-run/approve",
                "preview_runtime_unsupported": True,
                "article_system_setup": {"status": "code_review_ready", "setup_run_id": "setup-gate-run"},
            },
        )

    def test_code_review_ready_wizard_survives_premature_publish_target(self):
        # arb-gen.com: Content Factory registered a publish target synthesized
        # from the scaffold's setup cache while the setup run was still awaiting
        # approval. The org then computed generation_ready via "published",
        # which force-cleared setupBlocked — so the wizard never surfaced the
        # review step even though the founder had to approve the setup PR.
        config = self._prepare_articles_setup_gate(status="code_review_ready")
        article_system = dict(config.article_system)
        article_system["state"] = "roo_scaffolded"
        article_system["source"] = "scaffold"
        pending = dict(article_system["pending_article_system_setup"])
        for key in ("preview_url", "previewUrl", "pr_url", "prUrl"):
            pending.pop(key, None)
        pending["approveUrl"] = "/api/runs/setup-gate-run/approve"
        pending["previewRuntimeUnsupported"] = True
        article_system["pending_article_system_setup"] = pending
        config.article_system = article_system
        config.publish_targets = [
            {
                "target_id": "stack_native_collection_data_articles_json_python",
                "kind": "stack_native_collection",
                "source": "scaffold_cache",
                "content_path_pattern": "data/articles.json",
            }
        ]
        config.save(update_fields=["article_system", "publish_targets", "updated_at"])
        self._create_code_review_ready_run()

        with patch("content_factory.vibe_marketing_views.google_baseline_connection_status", return_value={}):
            response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        scaffold = response.data["checks"]["scaffold"]
        self.assertFalse(scaffold["published"])
        self.assertTrue(scaffold["setupBlocked"])
        steps = {step["id"]: step for step in response.data["workflowProgress"]["steps"]}
        self.assertEqual(steps["review"]["status"], "needs_action")
        self.assertEqual(steps["review"]["primaryAction"]["label"], "Review setup changes")

    def test_status_view_result_includes_setup_status(self):
        # The wizard's poller reads runs/<id>/?view=status (compact serializer)
        # and resolves the child status from result.setup_status. The compact
        # whitelist used to drop it, so a code_review_ready run read as its
        # current_step ("await_review") and the review CTA never rendered.
        self._prepare_articles_setup_gate(status="code_review_ready")
        self._create_code_review_ready_run()

        with patch(
            "content_factory.vibe_marketing_views._call_content_factory_run_status",
            return_value={},
        ):
            response = self.client.get("/api/v1/vibe-marketing/runs/setup-gate-run/?view=status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["result"].get("setup_status"), "code_review_ready")

    def test_scaffold_cache_publish_target_alone_is_not_published(self):
        from content_factory.vibe_marketing_views import _article_system_is_published

        config = SimpleNamespace(
            publish_targets=[{"kind": "stack_native_collection", "source": "scaffold_cache"}],
            articles_scaffolded=False,
        )
        self.assertFalse(_article_system_is_published(config, {"state": "roo_scaffolded"}))
        # Accepting/merging the scaffold sets articles_scaffolded — then the
        # same target counts again.
        config.articles_scaffolded = True
        self.assertTrue(_article_system_is_published(config, {"state": "roo_scaffolded"}))
        # Targets registered from a scan of a live article system still count
        # without the scaffolded flag.
        config.articles_scaffolded = False
        config.publish_targets = [{"kind": "react_article_system", "source": "scan"}]
        self.assertTrue(_article_system_is_published(config, {"state": "missing"}))

    def test_first_time_articles_setup_preview_failed_shows_review_diagnostics(self):
        self._prepare_articles_setup_gate(status="preview_failed")
        ContentFactoryRun.objects.create(
            run_id="setup-gate-run",
            workflow="article_system_setup",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.BLOCKED,
            current_step="preview_failed",
            approval_state=ContentFactoryApprovalState.NOT_REQUIRED,
            error="Hosted preview build failed. Inspect the build logs for details.",
            result={
                "status": "preview_failed",
                "setup_run_id": "setup-gate-run",
                "error": "Hosted preview build failed. Inspect the build logs for details.",
                "error_code": "platform_preview_failed",
                "article_system_setup": {
                    "status": "preview_failed",
                    "setup_run_id": "setup-gate-run",
                    "error": "Hosted preview build failed. Inspect the build logs for details.",
                    "error_code": "platform_preview_failed",
                },
            },
        )

        with patch("content_factory.vibe_marketing_views.google_baseline_connection_status", return_value={}):
            response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        progress = response.data["workflowProgress"]
        steps = {step["id"]: step for step in progress["steps"]}
        self.assertEqual(progress["currentStepId"], "review")
        self.assertEqual(steps["generate"]["status"], "complete")
        self.assertEqual(steps["review"]["status"], "blocked")
        self.assertEqual(steps["review"]["primaryAction"]["label"], "Open setup diagnostics")
        self.assertIn("build logs", steps["review"]["summary"])
        self.assertEqual(steps["publish"]["status"], "locked")

    def test_first_time_articles_setup_verification_rescan_unlocks_research(self):
        config = self._prepare_articles_setup_gate(status="merged_verifying", rescan_run_id="verify-setup-run")
        article_system = dict(config.article_system)
        article_system.update({"state": "existing", "confidence": "high", "directory_name": "articles"})
        config.article_system = article_system
        config.publish_targets = [{"id": "articles", "label": "Articles"}]
        config.save(update_fields=["article_system", "publish_targets", "updated_at"])
        ContentFactoryRun.objects.create(
            run_id="setup-gate-run",
            workflow="article_system_setup",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.COMPLETED,
            current_step="completed",
            approval_state=ContentFactoryApprovalState.APPROVED,
            result={
                "status": "completed",
                "setup_run_id": "setup-gate-run",
                "rescan_run_id": "verify-setup-run",
                "article_system_setup": {
                    "status": "merged_verifying",
                    "setup_run_id": "setup-gate-run",
                    "rescan_run_id": "verify-setup-run",
                },
            },
        )
        ContentFactoryRun.objects.create(
            run_id="verify-setup-run",
            workflow="repo_scan",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.COMPLETED,
            result={"status": "completed"},
        )

        with patch("content_factory.vibe_marketing_views.google_baseline_connection_status", return_value={}):
            response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        scaffold = response.data["checks"]["scaffold"]
        self.assertTrue(scaffold["passed"])
        self.assertTrue(scaffold["published"])
        self.assertFalse(scaffold["setupBlocked"])
        steps = {step["id"]: step for step in response.data["workflowProgress"]["steps"]}
        self.assertEqual(steps["research"]["status"], "ready")

    def test_workflow_progress_completes_research_after_discovery_run(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.github_token_encrypted = "encrypted-token"
        config.github_repo = "MLAI-AUS-Inc/mlai-au"
        config.company_context = "MLAI helps Australian founders adopt AI."
        config.article_delivery_mode = "content_only"
        config.baseline_skipped_at = config.updated_at
        config.article_system = {"state": "existing", "confidence": "high", "directory_name": "articles"}
        config.publish_targets = [{"id": "articles", "label": "Articles"}]
        config.save(
            update_fields=[
                "github_token_encrypted",
                "github_repo",
                "company_context",
                "article_delivery_mode",
                "baseline_skipped_at",
                "article_system",
                "publish_targets",
                "updated_at",
            ]
        )
        self.organization.seed_keywords = ["australian founders"]
        self.organization.save(update_fields=["seed_keywords"])
        self._create_mlai_featured_components()
        ResearchedKeyword.objects.create(
            organization=self.organization,
            keyword="australian founders",
            volume=700,
            difficulty=30,
            opportunity_index=80,
            status=KeywordStatus.PENDING,
        )
        self.run.status = ContentFactoryRunStatus.CANCELLED
        self.run.save(update_fields=["status", "updated_at"])
        discovery_run = ContentFactoryRun.objects.create(
            run_id="topic-discovery-complete",
            workflow="auto_discovery",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.COMPLETED,
            result={},
        )

        with patch("content_factory.vibe_marketing_views.google_baseline_connection_status", return_value={}):
            response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["checks"]["research"]["passed"])
        self.assertEqual(response.data["checks"]["research"]["runId"], discovery_run.run_id)
        progress = response.data["workflowProgress"]
        steps = {step["id"]: step for step in progress["steps"]}
        self.assertEqual(progress["currentStepId"], "choose_topic")
        self.assertEqual(steps["research"]["status"], "complete")
        self.assertEqual(steps["choose_topic"]["status"], "needs_action")
        self.assertEqual(steps["choose_topic"]["primaryAction"]["label"], "Choose article topic")

    def test_workflow_progress_keeps_completed_article_on_review_step(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.github_token_encrypted = "encrypted-token"
        config.github_repo = "MLAI-AUS-Inc/mlai-au"
        config.company_context = "MLAI helps Australian founders adopt AI."
        config.article_delivery_mode = "content_only"
        config.baseline_skipped_at = config.updated_at
        config.article_system = {"state": "existing", "confidence": "high", "directory_name": "articles"}
        config.publish_targets = [{"id": "articles", "label": "Articles"}]
        config.save(
            update_fields=[
                "github_token_encrypted",
                "github_repo",
                "company_context",
                "article_delivery_mode",
                "baseline_skipped_at",
                "article_system",
                "publish_targets",
                "updated_at",
            ]
        )
        self.organization.seed_keywords = ["australian founders"]
        self.organization.save(update_fields=["seed_keywords"])
        self._create_mlai_featured_components()
        ResearchedKeyword.objects.create(
            organization=self.organization,
            keyword="australian founders",
            volume=700,
            difficulty=30,
            opportunity_index=80,
            status=KeywordStatus.PENDING,
        )
        self.run.run_request = {"delivery_mode": "content_only"}
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.result = {
            "delivery_mode": "content_only",
            "promote_bundle_url": f"/api/runs/{self.run.run_id}/promote-bundle",
            "componentManifest": {
                "components": [
                    {
                        "id": "title",
                        "type": "title",
                        "label": "Title",
                    }
                ]
            },
            "livePreview": {
                "available": True,
                "status": "ready",
                "previewUrl": "http://127.0.0.1:4321/articles/featured/generated?cfInspector=1",
                "exactRender": True,
            },
            "delivery_package": {
                "title": "Australian Founders and What the Term Means Today",
                "target_keyword": "australian founders",
                "article_markdown": "steps/package_content_delivery/attempt-01/artifacts/article.md",
            },
        }
        self.run.save(update_fields=["run_request", "acceptance_summary", "result", "updated_at"])

        with patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        progress = response.data["workflowProgress"]
        steps = {step["id"]: step for step in progress["steps"]}
        self.assertEqual(progress["currentStepId"], "review")
        self.assertEqual(steps["research"]["status"], "complete")
        self.assertEqual(steps["choose_topic"]["status"], "complete")
        self.assertEqual(steps["generate"]["status"], "complete")
        self.assertEqual(steps["review"]["status"], "ready")
        self.assertEqual(steps["publish"]["status"], "ready")
        self.assertEqual(steps["publish"]["primaryAction"]["intent"], "promote-bundle")


    def test_preview_ready_publish_article_stays_on_review_before_approval(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.github_token_encrypted = "encrypted-token"
        config.github_repo = "MLAI-AUS-Inc/mlai-au"
        config.company_context = "MLAI helps Australian founders adopt AI."
        config.article_delivery_mode = "publish_code"
        config.baseline_skipped_at = config.updated_at
        config.article_system = {"state": "existing", "confidence": "high", "directory_name": "articles"}
        config.publish_targets = [{"id": "articles", "label": "Articles"}]
        config.save(
            update_fields=[
                "github_token_encrypted",
                "github_repo",
                "company_context",
                "article_delivery_mode",
                "baseline_skipped_at",
                "article_system",
                "publish_targets",
                "updated_at",
            ]
        )
        self.organization.seed_keywords = ["australian founders"]
        self.organization.save(update_fields=["seed_keywords"])
        self.run.run_request = {"delivery_mode": "publish_code"}
        self.run.status = ContentFactoryRunStatus.APPROVAL_REQUIRED
        self.run.current_step = "await_review"
        self.run.approval_state = ContentFactoryApprovalState.APPROVAL_REQUIRED
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.result = {
            "status": "preview_ready",
            "delivery_mode": "publish_code",
            "review_surface_kind": "component_live_preview",
            "preview_url": "https://preview.example/articles/generated",
            "promote_bundle_url": f"/api/runs/{self.run.run_id}/promote-bundle",
            "componentManifest": {
                "components": [{"id": "title", "type": "title", "label": "Title"}],
            },
            "livePreview": {
                "available": True,
                "status": "ready",
                "previewUrl": "https://preview.example/articles/generated",
                "exactRender": True,
            },
            "delivery_package": {
                "title": "Australian Founders and What the Term Means Today",
                "target_keyword": "australian founders",
                "article_markdown": "steps/package_content_delivery/attempt-01/artifacts/article.md",
            },
        }
        self.run.save(update_fields=["run_request", "status", "current_step", "approval_state", "acceptance_summary", "result", "updated_at"])

        with patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["previewUrl"], "https://preview.example/articles/generated")
        component_ids = {
            component["id"]
            for component in response.data["componentManifest"]["components"]
        }
        self.assertTrue({"title", "disclaimer", "references", "authoritative-references", "events-cta"}.issubset(component_ids))
        progress = response.data["workflowProgress"]
        steps = {step["id"]: step for step in progress["steps"]}
        self.assertEqual(progress["currentStepId"], "review")
        self.assertEqual(steps["review"]["status"], "ready")
        self.assertEqual(steps["publish"]["status"], "ready")
        self.assertNotEqual(steps["publish"]["status"], "complete")
        self.assertEqual(steps["publish"]["primaryAction"]["intent"], "promote-bundle")

    def test_preview_ready_article_augments_missing_fixed_review_manifest(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.github_token_encrypted = "encrypted-token"
        config.github_repo = "MLAI-AUS-Inc/mlai-au"
        config.company_context = "MLAI helps Australian founders adopt AI."
        config.article_delivery_mode = "publish_code"
        config.baseline_skipped_at = config.updated_at
        config.article_system = {"state": "existing", "confidence": "high", "directory_name": "articles"}
        config.publish_targets = [{"id": "articles", "label": "Articles"}]
        config.save(
            update_fields=[
                "github_token_encrypted",
                "github_repo",
                "company_context",
                "article_delivery_mode",
                "baseline_skipped_at",
                "article_system",
                "publish_targets",
                "updated_at",
            ]
        )
        self.run.run_request = {"delivery_mode": "publish_code"}
        self.run.status = ContentFactoryRunStatus.APPROVAL_REQUIRED
        self.run.current_step = "await_review"
        self.run.approval_state = ContentFactoryApprovalState.APPROVAL_REQUIRED
        self.run.result = {
            "status": "preview_ready",
            "delivery_mode": "publish_code",
            "review_surface_kind": "component_live_preview",
            "preview_url": "https://preview.example/articles/generated",
            "livePreview": {
                "available": True,
                "status": "ready",
                "previewUrl": "https://preview.example/articles/generated",
                "exactRender": True,
            },
        }
        self.run.save(update_fields=["run_request", "status", "current_step", "approval_state", "result", "updated_at"])

        with patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        component_ids = {
            component["id"]
            for component in response.data["componentManifest"]["components"]
        }
        self.assertTrue({"disclaimer", "references", "authoritative-references", "events-cta"}.issubset(component_ids))

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_approve_preview_ready_article_creates_local_publish_child_run(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.github_token_encrypted = "encrypted-token"
        config.github_repo = "MLAI-AUS-Inc/mlai-au"
        config.company_context = "MLAI helps Australian founders adopt AI."
        config.article_delivery_mode = "publish_code"
        config.baseline_skipped_at = config.updated_at
        config.article_system = {"state": "existing", "confidence": "high", "directory_name": "articles"}
        config.publish_targets = [{"id": "articles", "label": "Articles"}]
        config.save(
            update_fields=[
                "github_token_encrypted",
                "github_repo",
                "company_context",
                "article_delivery_mode",
                "baseline_skipped_at",
                "article_system",
                "publish_targets",
                "updated_at",
            ]
        )
        self.run.run_request = {
            "domain": "mlai.au",
            "topic": "Australian founders",
            "target_keyword": "australian founders",
            "delivery_mode": "publish_code",
        }
        self.run.status = ContentFactoryRunStatus.APPROVAL_REQUIRED
        self.run.current_step = "await_review"
        self.run.approval_state = ContentFactoryApprovalState.APPROVAL_REQUIRED
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.result = {
            "status": "preview_ready",
            "delivery_mode": "publish_code",
            "review_surface_kind": "component_live_preview",
            "preview_url": "https://preview.example/articles/generated",
            "componentManifest": {"components": [{"id": "title", "type": "title", "label": "Title"}]},
            "livePreview": {
                "available": True,
                "status": "ready",
                "previewUrl": "https://preview.example/articles/generated",
                "exactRender": True,
            },
            "delivery_package": {
                "title": "Australian Founders and What the Term Means Today",
                "article_markdown": "article.md",
            },
        }
        self.run.save(update_fields=["run_request", "status", "current_step", "approval_state", "acceptance_summary", "result", "updated_at"])

        def fake_post(url, json=None, headers=None, timeout=None):
            return _Response(
                status_code=202,
                payload={
                    "run_id": "article-publish-child-approved",
                    "job_id": "article-publish-child-approved",
                    "source_run_id": self.run.run_id,
                    "status": "queued",
                    "publish_child_status": "queued",
                },
            )

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/approve", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["runId"], "article-publish-child-approved")
        publish_run = ContentFactoryRun.objects.get(run_id="article-publish-child-approved")
        self.assertEqual(publish_run.workflow, "article_generation")
        self.assertEqual(publish_run.run_request["source_run_id"], self.run.run_id)
        self.assertEqual(publish_run.run_request["delivery_mode"], "publish_code")
        self.run.refresh_from_db()
        self.assertEqual(self.run.approval_state, ContentFactoryApprovalState.APPROVED)
        self.assertEqual(self.run.status, ContentFactoryRunStatus.COMPLETED)
        self.assertEqual(self.run.result["publish_child_run_id"], "article-publish-child-approved")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_promote_bundle_creates_local_publish_child_run(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.github_token_encrypted = "encrypted-token"
        config.github_repo = "MLAI-AUS-Inc/mlai-au"
        config.company_context = "MLAI helps Australian founders adopt AI."
        config.article_delivery_mode = "content_only"
        config.baseline_skipped_at = config.updated_at
        config.article_system = {"state": "existing", "confidence": "high", "directory_name": "articles"}
        config.publish_targets = [{"id": "articles", "label": "Articles"}]
        config.save(
            update_fields=[
                "github_token_encrypted",
                "github_repo",
                "company_context",
                "article_delivery_mode",
                "baseline_skipped_at",
                "article_system",
                "publish_targets",
                "updated_at",
            ]
        )
        self.organization.seed_keywords = ["australian founders"]
        self.organization.save(update_fields=["seed_keywords"])
        ResearchedKeyword.objects.create(
            organization=self.organization,
            keyword="australian founders",
            volume=700,
            difficulty=30,
            opportunity_index=80,
            status=KeywordStatus.PENDING,
        )
        self.run.run_request = {
            "domain": "mlai.au",
            "topic": "Australian founders",
            "target_keyword": "australian founders",
            "delivery_mode": "content_only",
        }
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.result = {
            "delivery_mode": "content_only",
            "componentManifest": {
                "components": [
                    {
                        "id": "title",
                        "type": "title",
                        "label": "Title",
                    }
                ]
            },
            "delivery_package": {
                "title": "Australian Founders and What the Term Means Today",
                "article_markdown": "article.md",
            },
        }
        self.run.save(update_fields=["run_request", "acceptance_summary", "result", "updated_at"])

        def fake_post(url, json=None, headers=None, timeout=None):
            return _Response(status_code=202, payload={"run_id": "article-publish-child-1", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/promote-bundle", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["runId"], "article-publish-child-1")
        publish_run = ContentFactoryRun.objects.get(run_id="article-publish-child-1")
        self.assertEqual(publish_run.workflow, "article_generation")
        self.assertEqual(publish_run.run_request["source_run_id"], self.run.run_id)
        self.assertEqual(publish_run.run_request["delivery_mode"], "publish_code")
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["publish_child_run_id"], "article-publish-child-1")
        steps = {step["id"]: step for step in response.data["workflowProgress"]["steps"]}
        self.assertEqual(response.data["workflowProgress"]["currentStepId"], "publish")
        self.assertEqual(steps["publish"]["status"], "running")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_promote_bundle_targets_accepted_component_revision(self):
        revision_run = ContentFactoryRun.objects.create(
            run_id="article-run-comments-revision-accepted",
            workflow="article_revision",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.COMPLETED,
            run_request={"source_run_id": self.run.run_id, "feedback_batch_id": "batch-accepted"},
            result={"source_run_id": self.run.run_id, "feedback_batch_id": "batch-accepted"},
        )
        self.run.run_request = {"delivery_mode": "review_draft"}
        self.run.result = {
            "delivery_mode": "review_draft",
            "component_feedback_latest_batch": {
                "id": "batch-accepted",
                "sourceRunId": self.run.run_id,
                "revisionRunId": revision_run.run_id,
                "status": "accepted",
            },
        }
        self.run.save(update_fields=["run_request", "result", "updated_at"])
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            captured["timeout"] = timeout
            return _Response(status_code=202, payload={"run_id": "article-publish-child-from-revision", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/promote-bundle", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            captured["url"],
            "https://content-factory.test/api/runs/article-run-comments-revision-accepted/promote-bundle",
        )
        self.assertEqual(captured["timeout"], (3, 20))
        publish_run = ContentFactoryRun.objects.get(run_id="article-publish-child-from-revision")
        self.assertEqual(publish_run.run_request["source_run_id"], revision_run.run_id)
        self.assertEqual(publish_run.run_request["review_source_run_id"], self.run.run_id)
        self.run.refresh_from_db()
        revision_run.refresh_from_db()
        self.assertEqual(self.run.result["publish_child_run_id"], "article-publish-child-from-revision")
        self.assertEqual(revision_run.result["publish_child_run_id"], "article-publish-child-from-revision")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_promote_bundle_timeout_preserves_completed_source_state(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.github_token_encrypted = "encrypted-token"
        config.github_repo = "MLAI-AUS-Inc/mlai-au"
        config.company_context = "MLAI helps Australian founders adopt AI."
        config.article_delivery_mode = "content_only"
        config.baseline_skipped_at = config.updated_at
        config.article_system = {"state": "existing", "confidence": "high", "directory_name": "articles"}
        config.publish_targets = [{"id": "articles", "label": "Articles"}]
        config.save(
            update_fields=[
                "github_token_encrypted",
                "github_repo",
                "company_context",
                "article_delivery_mode",
                "baseline_skipped_at",
                "article_system",
                "publish_targets",
                "updated_at",
            ]
        )
        self.organization.seed_keywords = ["australian founders"]
        self.organization.save(update_fields=["seed_keywords"])
        self.run.run_request = {
            "domain": "mlai.au",
            "topic": "Australian founders",
            "target_keyword": "australian founders",
            "delivery_mode": "content_only",
        }
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.result = {
            "delivery_mode": "content_only",
            "componentManifest": {"components": [{"id": "title", "type": "title", "label": "Title"}]},
            "delivery_package": {
                "title": "Australian Founders and What the Term Means Today",
                "article_markdown": "article.md",
            },
        }
        self.run.save(update_fields=["run_request", "acceptance_summary", "result", "updated_at"])

        timeout = http_client.exceptions.ReadTimeout(
            "HTTPConnectionPool(host='10.126.0.4', port=8000): Read timed out. (read timeout=60.0)"
        )
        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=timeout):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/promote-bundle", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.COMPLETED)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, ContentFactoryRunStatus.COMPLETED)
        self.assertTrue(self.run.result["publish_handoff_pending"])
        self.assertTrue(self.run.result["latest_control_response"]["content_factory_transport_error"])
        steps = {step["id"]: step for step in response.data["workflowProgress"]["steps"]}
        self.assertEqual(steps["publish"]["status"], "running")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_promote_bundle_pending_handoff_does_not_dispatch_again(self):
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.result = {
            "delivery_mode": "content_only",
            "publish_handoff_pending": True,
            "publish_handoff_started_at": timezone.now().isoformat(),
            "publish_handoff_last_attempt_at": timezone.now().isoformat(),
            "promote_bundle_requested_at": timezone.now().isoformat(),
            "componentManifest": {"components": [{"id": "title", "type": "title", "label": "Title"}]},
            "delivery_package": {
                "title": "Australian Founders and What the Term Means Today",
                "article_markdown": "article.md",
            },
        }
        self.run.save(update_fields=["acceptance_summary", "result", "updated_at"])

        with patch("content_factory.vibe_marketing_views.http_client.post") as post:
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/promote-bundle", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["runId"], self.run.run_id)
        post.assert_not_called()
        self.run.refresh_from_db()
        self.assertTrue(self.run.result["publish_handoff_pending"])
        self.assertFalse(self.run.result.get("publish_handoff_stale", False))

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_promote_bundle_stale_pending_handoff_retries_dispatch(self):
        stale_timestamp = (timezone.now() - timedelta(minutes=5)).isoformat()
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.result = {
            "delivery_mode": "content_only",
            "publish_handoff_pending": True,
            "publish_handoff_started_at": stale_timestamp,
            "publish_handoff_last_attempt_at": stale_timestamp,
            "promote_bundle_requested_at": stale_timestamp,
            "componentManifest": {"components": [{"id": "title", "type": "title", "label": "Title"}]},
            "delivery_package": {
                "title": "Australian Founders and What the Term Means Today",
                "article_markdown": "article.md",
            },
        }
        self.run.save(update_fields=["acceptance_summary", "result", "updated_at"])

        def fake_post(url, json=None, headers=None, timeout=None):
            return _Response(status_code=202, payload={"run_id": "article-publish-child-retry", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post) as post:
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/promote-bundle", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["runId"], "article-publish-child-retry")
        post.assert_called_once()
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["publish_child_run_id"], "article-publish-child-retry")
        self.assertFalse(self.run.result["publish_handoff_pending"])

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_completed_article_status_surfaces_stale_publish_handoff_retry(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.github_token_encrypted = "encrypted-token"
        config.github_repo = "MLAI-AUS-Inc/mlai-au"
        config.company_context = "MLAI helps Australian founders adopt AI."
        config.article_delivery_mode = "content_only"
        config.baseline_skipped_at = config.updated_at
        config.article_system = {"state": "existing", "confidence": "high", "directory_name": "articles"}
        config.publish_targets = [{"id": "articles", "label": "Articles"}]
        config.save(
            update_fields=[
                "github_token_encrypted",
                "github_repo",
                "company_context",
                "article_delivery_mode",
                "baseline_skipped_at",
                "article_system",
                "publish_targets",
                "updated_at",
            ]
        )
        stale_timestamp = (timezone.now() - timedelta(minutes=5)).isoformat()
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.result = {
            "delivery_mode": "content_only",
            "publish_handoff_pending": True,
            "publish_handoff_started_at": stale_timestamp,
            "publish_handoff_last_attempt_at": stale_timestamp,
            "promote_bundle_requested_at": stale_timestamp,
            "componentManifest": {"components": [{"id": "title", "type": "title", "label": "Title"}]},
            "delivery_package": {
                "title": "Australian Founders and What the Term Means Today",
                "article_markdown": "article.md",
            },
        }
        self.run.save(update_fields=["acceptance_summary", "result", "updated_at"])

        with patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["result"]["publish_handoff_stale"])
        steps = {step["id"]: step for step in response.data["workflowProgress"]["steps"]}
        self.assertEqual(steps["publish"]["status"], "ready")
        self.assertEqual(steps["publish"]["primaryAction"]["intent"], "promote-bundle")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_completed_article_status_surfaces_recoverable_publish_child(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.github_token_encrypted = "encrypted-token"
        config.github_repo = "MLAI-AUS-Inc/mlai-au"
        config.company_context = "MLAI helps Australian founders adopt AI."
        config.article_delivery_mode = "content_only"
        config.baseline_skipped_at = config.updated_at
        config.article_system = {"state": "existing", "confidence": "high", "directory_name": "articles"}
        config.publish_targets = [{"id": "articles", "label": "Articles"}]
        config.save(
            update_fields=[
                "github_token_encrypted",
                "github_repo",
                "company_context",
                "article_delivery_mode",
                "baseline_skipped_at",
                "article_system",
                "publish_targets",
                "updated_at",
            ]
        )
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.result = {
            "delivery_mode": "content_only",
            "publish_child_run_id": "article-publish-child-stuck",
            "componentManifest": {"components": [{"id": "title", "type": "title", "label": "Title"}]},
            "delivery_package": {
                "title": "Australian Founders and What the Term Means Today",
                "article_markdown": "article.md",
            },
        }
        self.run.save(update_fields=["acceptance_summary", "result", "updated_at"])
        ContentFactoryRun.objects.create(
            run_id="article-publish-child-stuck",
            workflow="article_generation",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.AWAITING_CONFIRMATION,
            run_request={
                "source_run_id": self.run.run_id,
                "delivery_mode": "publish_code",
                "delivery_mode_confirmed": True,
            },
            result={"status": "awaiting_confirmation"},
        )

        with patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["result"]["publish_child_status"], ContentFactoryRunStatus.AWAITING_CONFIRMATION)
        self.assertTrue(response.data["result"]["publish_child_recoverable"])
        self.assertEqual(response.data["result"]["publish_handoff_status"], "recoverable_wait")
        steps = {step["id"]: step for step in response.data["workflowProgress"]["steps"]}
        self.assertEqual(steps["publish"]["status"], "ready")
        self.assertEqual(steps["publish"]["primaryAction"]["intent"], "promote-bundle")
        self.assertEqual(steps["publish"]["primaryAction"]["label"], "Resume publish PR")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_promote_bundle_existing_child_id_dispatches_when_child_missing_locally(self):
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.result = {
            "delivery_mode": "content_only",
            "publish_child_run_id": "article-publish-child-existing",
            "componentManifest": {"components": [{"id": "title", "type": "title", "label": "Title"}]},
            "delivery_package": {
                "title": "Australian Founders and What the Term Means Today",
                "article_markdown": "article.md",
            },
        }
        self.run.save(update_fields=["acceptance_summary", "result", "updated_at"])

        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["timeout"] = timeout
            return _Response(
                status_code=202,
                payload={"run_id": "article-publish-child-existing", "status": "queued", "publish_child_status": "queued"},
            )

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post) as post:
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/promote-bundle", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["runId"], "article-publish-child-existing")
        post.assert_called_once()
        self.assertEqual(captured["url"], "https://content-factory.test/api/runs/article-run-comments/promote-bundle")
        self.assertEqual(captured["timeout"], (3, 20))
        publish_run = ContentFactoryRun.objects.get(run_id="article-publish-child-existing")
        self.assertEqual(publish_run.run_request["source_run_id"], self.run.run_id)
        self.assertEqual(publish_run.run_request["delivery_mode"], "publish_code")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_completed_article_status_surfaces_recoverable_missing_publish_child(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.github_token_encrypted = "encrypted-token"
        config.github_repo = "MLAI-AUS-Inc/mlai-au"
        config.company_context = "MLAI helps Australian founders adopt AI."
        config.article_delivery_mode = "content_only"
        config.baseline_skipped_at = config.updated_at
        config.article_system = {"state": "existing", "confidence": "high", "directory_name": "articles"}
        config.publish_targets = [{"id": "articles", "label": "Articles"}]
        config.save(
            update_fields=[
                "github_token_encrypted",
                "github_repo",
                "company_context",
                "article_delivery_mode",
                "baseline_skipped_at",
                "article_system",
                "publish_targets",
                "updated_at",
            ]
        )
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.result = {
            "delivery_mode": "content_only",
            "publish_child_run_id": "article-publish-child-missing",
            "componentManifest": {"components": [{"id": "title", "type": "title", "label": "Title"}]},
            "delivery_package": {
                "title": "Australian Founders and What the Term Means Today",
                "article_markdown": "article.md",
            },
        }
        self.run.save(update_fields=["acceptance_summary", "result", "updated_at"])
        ContentFactoryRun.objects.create(
            run_id="article-publish-child-missing",
            workflow="article_generation",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.BLOCKED,
            run_request={
                "source_run_id": self.run.run_id,
                "delivery_mode": "publish_code",
                "delivery_mode_confirmed": True,
            },
            result={
                "error": "Content Factory run article-publish-child-missing was not found.",
                "diagnostics": {"content_factory_status_code": 404},
            },
            error="Content Factory run article-publish-child-missing was not found.",
        )

        with patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.COMPLETED)
        self.assertEqual(response.data["result"]["publish_child_status"], ContentFactoryRunStatus.BLOCKED)
        self.assertTrue(response.data["result"]["publish_child_recoverable"])
        self.assertEqual(response.data["result"]["publish_handoff_status"], "recoverable_missing_child")
        self.assertIn("queued but did not start", response.data["result"]["publish_child_wait_reason"])
        steps = {step["id"]: step for step in response.data["workflowProgress"]["steps"]}
        self.assertEqual(steps["publish"]["status"], "ready")
        self.assertEqual(steps["publish"]["primaryAction"]["intent"], "promote-bundle")
        self.assertEqual(steps["publish"]["primaryAction"]["label"], "Retry creating PR")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_missing_publish_child_route_does_not_poll_remote_repeatedly(self):
        child = ContentFactoryRun.objects.create(
            run_id="article-publish-child-missing-route",
            workflow="article_generation",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.BLOCKED,
            current_step="queued",
            run_request={
                "source_run_id": self.run.run_id,
                "delivery_mode": "publish_code",
                "delivery_mode_confirmed": True,
            },
            result={
                "error": "Content Factory run article-publish-child-missing-route was not found.",
                "diagnostics": {"content_factory_status_code": 404},
            },
            error="Content Factory run article-publish-child-missing-route was not found.",
        )
        self.run.result = {"publish_child_run_id": child.run_id}
        self.run.save(update_fields=["result", "updated_at"])

        with patch("content_factory.vibe_marketing_views._call_content_factory_run_status") as status_mock:
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{child.run_id}")

        self.assertEqual(response.status_code, 200)
        status_mock.assert_not_called()
        self.assertTrue(response.data["publishChildRecoverable"])
        self.assertIn("queued but did not start", response.data["publishChildWaitReason"])

    def test_failed_article_system_setup_status_skips_remote_poll(self):
        setup_run = ContentFactoryRun.objects.create(
            run_id="article-system-setup-failed",
            workflow="article_system_setup",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.FAILED,
            current_step="validate_directory_dependencies",
            result={
                "status": "failed",
                "error": "Directory scaffold dependency validation failed: Missing required directory component slots: article_list",
                "error_code": "DIRECTORY_DEPENDENCY_VALIDATION_FAILED",
                "article_system_setup": {
                    "status": "failed",
                    "error": "Directory scaffold dependency validation failed: Missing required directory component slots: article_list",
                    "error_code": "DIRECTORY_DEPENDENCY_VALIDATION_FAILED",
                },
            },
            error="Directory scaffold dependency validation failed: Missing required directory component slots: article_list",
        )

        with patch("content_factory.vibe_marketing_views._call_content_factory_run_status") as status_mock:
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{setup_run.run_id}?view=status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.FAILED)
        status_mock.assert_not_called()

    def test_preview_failed_article_system_setup_status_skips_remote_poll_even_if_local_status_is_running(self):
        setup_run = ContentFactoryRun.objects.create(
            run_id="article-system-setup-preview-failed",
            workflow="article_system_setup",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.RUNNING,
            current_step="verify_directory_browser",
            result={
                "status": "preview_failed",
                "error": "Directory browser verification failed before setup approval.",
                "error_code": "DIRECTORY_BROWSER_VERIFICATION_FAILED",
                "article_system_setup": {
                    "status": "preview_failed",
                    "error": "Directory browser verification failed before setup approval.",
                    "error_code": "DIRECTORY_BROWSER_VERIFICATION_FAILED",
                },
            },
            error="",
        )

        with patch("content_factory.vibe_marketing_views._call_content_factory_run_status") as status_mock:
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{setup_run.run_id}?view=status")

        self.assertEqual(response.status_code, 200)
        status_mock.assert_not_called()

    def test_sync_local_run_from_remote_maps_preview_failed_to_blocked(self):
        setup_run = ContentFactoryRun.objects.create(
            run_id="article-system-setup-preview-failed-remote",
            workflow="article_system_setup",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.RUNNING,
            current_step="start_hosted_preview",
            result={"status": "preview_building"},
        )
        remote_data = {
            "status": "preview_failed",
            "current_step": "verify_directory_browser",
            "result": {
                "status": "preview_failed",
                "error": "Directory browser verification failed before setup approval.",
                "article_system_setup": {"status": "preview_failed"},
            },
            "error": "Directory browser verification failed before setup approval.",
        }

        _sync_local_run_from_remote(setup_run, remote_data)

        self.assertEqual(setup_run.status, ContentFactoryRunStatus.BLOCKED)
        self.assertEqual(setup_run.current_step, "verify_directory_browser")
        self.assertEqual(setup_run.result["status"], "preview_failed")

    def test_sync_local_run_from_remote_does_not_save_identical_terminal_payload(self):
        setup_run = ContentFactoryRun.objects.create(
            run_id="article-system-setup-same-failure",
            workflow="article_system_setup",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.FAILED,
            current_step="validate_directory_dependencies",
            result={
                "status": "failed",
                "error": "Directory scaffold dependency validation failed: Missing required directory component slots: article_list",
                "error_code": "DIRECTORY_DEPENDENCY_VALIDATION_FAILED",
            },
            error="Directory scaffold dependency validation failed: Missing required directory component slots: article_list",
        )
        remote_data = {
            "status": ContentFactoryRunStatus.FAILED,
            "current_step": "validate_directory_dependencies",
            "result": {
                "status": "failed",
                "error": "Directory scaffold dependency validation failed: Missing required directory component slots: article_list",
                "error_code": "DIRECTORY_DEPENDENCY_VALIDATION_FAILED",
            },
            "error": "Directory scaffold dependency validation failed: Missing required directory component slots: article_list",
        }

        with patch.object(setup_run, "save", wraps=setup_run.save) as save_mock:
            _sync_local_run_from_remote(setup_run, remote_data)

        save_mock.assert_not_called()

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_promote_bundle_retries_local_child_missing_remotely(self):
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.result = {
            "delivery_mode": "content_only",
            "publish_child_run_id": "article-publish-child-ghost",
            "componentManifest": {"components": [{"id": "title", "type": "title", "label": "Title"}]},
            "delivery_package": {
                "title": "Australian Founders and What the Term Means Today",
                "article_markdown": "article.md",
            },
        }
        self.run.save(update_fields=["acceptance_summary", "result", "updated_at"])
        ContentFactoryRun.objects.create(
            run_id="article-publish-child-ghost",
            workflow="article_generation",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.QUEUED,
            run_request={
                "source_run_id": self.run.run_id,
                "delivery_mode": "publish_code",
                "delivery_mode_confirmed": True,
            },
            result={"status": "queued"},
        )
        captured = {}

        def fake_status(run_id, workflow=""):
            if run_id == "article-publish-child-ghost":
                return {
                    "status": ContentFactoryRunStatus.BLOCKED,
                    "error": "Content Factory run article-publish-child-ghost was not found.",
                    "diagnostics": {"content_factory_status_code": 404},
                }
            return {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            return _Response(
                status_code=202,
                payload={"run_id": "article-publish-child-ghost", "status": "queued", "publish_child_status": "queued"},
            )

        with (
            patch("content_factory.vibe_marketing_views._call_content_factory_run_status", side_effect=fake_status),
            patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post),
        ):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/promote-bundle", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["url"], "https://content-factory.test/api/runs/article-run-comments/promote-bundle")
        self.assertEqual(response.data["runId"], "article-publish-child-ghost")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_promote_bundle_from_missing_publish_child_route_dispatches_source(self):
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.result = {
            "delivery_mode": "content_only",
            "publish_child_run_id": "article-publish-child-route",
            "componentManifest": {"components": [{"id": "title", "type": "title", "label": "Title"}]},
            "delivery_package": {
                "title": "Australian Founders and What the Term Means Today",
                "article_markdown": "article.md",
            },
        }
        self.run.save(update_fields=["acceptance_summary", "result", "updated_at"])
        ContentFactoryRun.objects.create(
            run_id="article-publish-child-route",
            workflow="article_generation",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.BLOCKED,
            run_request={
                "source_run_id": self.run.run_id,
                "delivery_mode": "publish_code",
                "delivery_mode_confirmed": True,
            },
            result={
                "error": "Content Factory run article-publish-child-route was not found.",
                "diagnostics": {"content_factory_status_code": 404},
            },
            error="Content Factory run article-publish-child-route was not found.",
        )
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            return _Response(
                status_code=202,
                payload={"run_id": "article-publish-child-route", "status": "queued", "publish_child_status": "queued"},
            )

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post("/api/v1/vibe-marketing/runs/article-publish-child-route/promote-bundle", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["url"], "https://content-factory.test/api/runs/article-run-comments/promote-bundle")
        self.assertEqual(captured["payload"]["source_run_id"], self.run.run_id)
        self.assertEqual(captured["payload"]["publish_child_run_id"], "article-publish-child-route")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_promote_bundle_retries_recoverable_existing_child(self):
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.result = {
            "delivery_mode": "content_only",
            "publish_child_run_id": "article-publish-child-stuck",
            "componentManifest": {"components": [{"id": "title", "type": "title", "label": "Title"}]},
            "delivery_package": {
                "title": "Australian Founders and What the Term Means Today",
                "article_markdown": "article.md",
            },
        }
        self.run.save(update_fields=["acceptance_summary", "result", "updated_at"])
        ContentFactoryRun.objects.create(
            run_id="article-publish-child-stuck",
            workflow="article_generation",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.AWAITING_CONFIRMATION,
            run_request={
                "source_run_id": self.run.run_id,
                "delivery_mode": "publish_code",
                "delivery_mode_confirmed": True,
            },
            result={"status": "awaiting_confirmation"},
        )

        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["timeout"] = timeout
            return _Response(
                status_code=202,
                payload={
                    "run_id": "article-publish-child-stuck",
                    "status": "queued",
                    "publish_child_status": "queued",
                    "repaired": True,
                    "previous_status": "awaiting_confirmation",
                },
            )

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/promote-bundle", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["url"], "https://content-factory.test/api/runs/article-run-comments/promote-bundle")
        self.assertEqual(captured["timeout"], (3, 20))
        self.assertEqual(response.data["runId"], "article-publish-child-stuck")
        publish_run = ContentFactoryRun.objects.get(run_id="article-publish-child-stuck")
        self.assertEqual(publish_run.status, ContentFactoryRunStatus.QUEUED)
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["publish_child_status"], ContentFactoryRunStatus.QUEUED)
        self.assertFalse(self.run.result["publish_child_recoverable"])

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_completed_article_status_uses_local_state_without_remote_poll(self):
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.result = {
            "delivery_mode": "content_only",
            "componentManifest": {"components": [{"id": "title", "type": "title", "label": "Title"}]},
            "delivery_package": {
                "title": "Australian Founders and What the Term Means Today",
                "article_markdown": "article.md",
            },
            "livePreview": {
                "available": True,
                "status": "running",
                "previewUrl": "https://preview.example/articles/australian-founders?cfInspector=1",
            },
        }
        self.run.save(update_fields=["acceptance_summary", "result", "updated_at"])

        with patch("content_factory.vibe_marketing_views._call_content_factory_run_status") as status_poll:
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        status_poll.assert_not_called()

    def test_merge_publish_pr_refuses_pending_checks(self):
        self.run.result = {
            "draft_pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/123",
            "draft_pr_number": 123,
        }
        self.run.save(update_fields=["result", "updated_at"])

        with (
            patch("content_factory.vibe_marketing_views.ensure_valid_org_token", return_value="github-token"),
            patch(
                "content_factory.vibe_marketing_views._github_pull_checks_state",
                return_value=(
                    {"state": "open", "merged": False},
                    {"ready": False, "state": "pending", "message": "GitHub Actions checks are still running."},
                ),
            ),
        ):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/merge-publish-pr", {}, format="json")

        self.assertEqual(response.status_code, 409)
        self.assertIn("GitHub Actions checks are still running", response.data["detail"])
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["merge_status"], "pending")
        self.assertEqual(self.run.result["checks_status"], "pending")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_direct_article_system_setup_approve_persists_pr_created_state(self):
        config = self._prepare_articles_setup_gate(status="preview_ready")
        setup_run = ContentFactoryRun.objects.create(
            run_id="setup-direct-approve",
            workflow="article_system_setup",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.AWAITING_APPROVAL,
            current_step="await_review",
            approval_state=ContentFactoryApprovalState.APPROVAL_REQUIRED,
            result={
                "status": "preview_ready",
                "setup_status": "preview_ready",
                "preview_url": "https://preview.example/articles",
                "article_system_setup": {"status": "preview_ready", "setup_run_id": "setup-direct-approve"},
            },
        )
        remote_payload = {
            "run_id": "setup-direct-approve",
            "workflow": "article_system_setup",
            "status": "setup_pr_created",
            "setup_status": "pr_created",
            "current_step": "create_pull_request",
            "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/44",
            "pr_number": 44,
            "merge_status": "not_merged",
            "article_system_setup": {
                "status": "pr_created",
                "setup_run_id": "setup-direct-approve",
                "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/44",
                "pr_number": 44,
                "merge_status": "not_merged",
            },
        }

        with patch("content_factory.vibe_marketing_views._call_content_factory_run_action", return_value=remote_payload):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{setup_run.run_id}/approve", {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.COMPLETED)
        self.assertEqual(response.data["currentStep"], "create_pull_request")
        self.assertEqual(response.data["approvalState"], ContentFactoryApprovalState.APPROVED)
        self.assertEqual(response.data["result"]["status"], "setup_pr_created")
        self.assertEqual(response.data["result"]["article_system_setup"]["status"], "pr_created")
        setup_run.refresh_from_db()
        self.assertEqual(setup_run.status, ContentFactoryRunStatus.COMPLETED)
        self.assertEqual(setup_run.current_step, "create_pull_request")
        self.assertEqual(setup_run.result["pr_number"], 44)
        config.refresh_from_db()
        pending = config.article_system["pending_article_system_setup"]
        self.assertEqual(pending["status"], "pr_created")
        self.assertEqual(pending["pr_number"], 44)

    def test_merge_setup_pr_publishes_via_auto_merge_when_direct_merge_blocked(self):
        setup_run = ContentFactoryRun.objects.create(
            run_id="setup-merge-pending",
            workflow="article_system_setup",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.COMPLETED,
            current_step="create_pull_request",
            approval_state=ContentFactoryApprovalState.APPROVED,
            result={
                "status": "setup_pr_created",
                "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/45",
                "pr_number": 45,
                "article_system_setup": {
                    "status": "pr_created",
                    "setup_run_id": "setup-merge-pending",
                    "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/45",
                    "pr_number": 45,
                },
            },
        )

        with (
            patch("content_factory.vibe_marketing_views._github_token_for_repo_operation", return_value=("github-token", "test")),
            patch(
                "content_factory.vibe_marketing_views._github_pull_checks_state_lenient",
                return_value=(
                    {"state": "open", "merged": False, "head": {"sha": "s"}, "node_id": "PR_x"},
                    {"ready": True, "state": "unknown", "message": "Checks visibility unavailable; relying on GitHub merge enforcement."},
                ),
            ),
            patch("content_factory.vibe_marketing_views._github_api_request", side_effect=ValueError("At least 1 approving review is required.")),
            patch("content_factory.vibe_marketing_views._enable_native_auto_merge", return_value={"status": "enabled", "message": "ok"}),
        ):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{setup_run.run_id}/merge-setup-pr", {}, format="json")

        # An unmergeable direct merge no longer refuses — it publishes via GitHub native
        # auto-merge (mirrors article publish), so the run goes to "publishing".
        self.assertEqual(response.status_code, 200)
        setup_run.refresh_from_db()
        self.assertEqual(setup_run.result["merge_status"], "publishing")
        self.assertTrue(setup_run.result["article_system_setup"]["native_auto_merge_enabled"])

    def test_merge_setup_pr_records_manual_merge_required_when_github_blocks_api_merge(self):
        config = self._prepare_articles_setup_gate(status="pr_created")
        setup_run = ContentFactoryRun.objects.create(
            run_id="setup-merge-protected",
            workflow="article_system_setup",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.COMPLETED,
            current_step="create_pull_request",
            approval_state=ContentFactoryApprovalState.APPROVED,
            result={
                "status": "setup_pr_created",
                "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/47",
                "pr_number": 47,
                "merge_status": "not_merged",
                "article_system_setup": {
                    "status": "pr_created",
                    "setup_run_id": "setup-merge-protected",
                    "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/47",
                    "pr_number": 47,
                    "merge_status": "not_merged",
                },
            },
        )

        with (
            patch("content_factory.vibe_marketing_views._github_token_for_repo_operation", return_value=("github-token", "test")),
            patch(
                "content_factory.vibe_marketing_views._github_pull_checks_state_lenient",
                return_value=(
                    {"state": "open", "merged": False, "head": {"sha": "s"}},
                    {"ready": True, "state": "success", "message": "Checks are passing."},
                ),
            ),
            patch("content_factory.vibe_marketing_views._github_api_request", side_effect=ValueError("Merge blocked by branch protection.")),
            patch(
                "content_factory.vibe_marketing_views._enable_native_auto_merge",
                return_value={"status": "unavailable", "message": "Auto-merge is not allowed for this repository"},
            ),
        ):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{setup_run.run_id}/merge-setup-pr", {}, format="json")

        self.assertEqual(response.status_code, 409)
        self.assertIn("Merge blocked by branch protection", response.data["detail"])
        setup_run.refresh_from_db()
        self.assertEqual(setup_run.result["merge_status"], "manual_merge_required")
        self.assertEqual(setup_run.result["article_system_setup"]["merge_status"], "manual_merge_required")
        config.refresh_from_db()
        pending = config.article_system["pending_article_system_setup"]
        self.assertEqual(pending["merge_status"], "manual_merge_required")
        self.assertFalse(config.articles_scaffolded)

    def test_refresh_setup_pr_status_detects_manual_github_merge_without_rescan(self):
        config = self._prepare_articles_setup_gate(status="pr_created")
        setup_run = ContentFactoryRun.objects.create(
            run_id="setup-manual-refresh",
            workflow="article_system_setup",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.COMPLETED,
            current_step="create_pull_request",
            approval_state=ContentFactoryApprovalState.APPROVED,
            result={
                "status": "setup_pr_created",
                "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/48",
                "pr_number": 48,
                "merge_status": "not_merged",
                "article_system_setup": {
                    "status": "pr_created",
                    "setup_run_id": "setup-manual-refresh",
                    "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/48",
                    "pr_number": 48,
                    "merge_status": "not_merged",
                },
            },
        )
        article_calls = []

        with (
            patch("content_factory.vibe_marketing_views._github_token_for_repo_operation", return_value=("github-token", "test")),
            patch("content_factory.vibe_marketing_views._github_api_request", return_value={"number": 48, "merged": True}),
            patch("content_factory.vibe_marketing_views._queue_content_factory_run", side_effect=lambda **kwargs: article_calls.append(kwargs)),
            patch("content_factory.vibe_marketing_views.google_baseline_connection_status", return_value={}),
        ):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{setup_run.run_id}/refresh-setup-pr-status", {}, format="json")
            bootstrap_response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["result"]["merge_status"], "merged")
        setup_run.refresh_from_db()
        self.assertEqual(setup_run.result["merge_status"], "merged")
        config.refresh_from_db()
        self.assertTrue(config.articles_scaffolded)
        self.assertEqual(config.article_system["state"], "roo_scaffolded")
        self.assertEqual(bootstrap_response.status_code, 200)
        scaffold = bootstrap_response.data["checks"]["scaffold"]
        self.assertFalse(scaffold["setupBlocked"])
        self.assertTrue(scaffold["generationReady"])
        self.assertFalse(bootstrap_response.data["articleSetupState"]["setupBlocked"])
        self.assertTrue(bootstrap_response.data["articleSetupState"]["generationReady"])
        self.assertTrue(bootstrap_response.data["hasCompletedArticleFlow"])
        self.assertEqual(bootstrap_response.data["startPageMode"], "topic_picker")
        self.assertEqual(article_calls, [])

    def test_written_article_history_overrides_stale_setup_blocker(self):
        config = self._prepare_articles_setup_gate(status="pr_created")
        WrittenArticle.objects.create(
            organization=self.organization,
            title="How AI Founders Choose Startup Tools",
            slug="how-ai-founders-choose-startup-tools",
            category="startups",
            primary_keyword="ai founder tools",
            article_url="https://mlai.au/articles/how-ai-founders-choose-startup-tools",
            published_at=timezone.now(),
        )

        with (
            patch("content_factory.vibe_marketing_views._github_token_for_repo_operation", return_value=("github-token", "test")),
            patch("content_factory.vibe_marketing_views._github_api_request", return_value={"number": 12, "merged": False}),
            patch("content_factory.vibe_marketing_views.google_baseline_connection_status", return_value={}),
        ):
            response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        scaffold = response.data["checks"]["scaffold"]
        self.assertFalse(scaffold["setupBlocked"])
        self.assertTrue(scaffold["generationReady"])
        self.assertFalse(response.data["articleSetupState"]["setupBlocked"])
        self.assertTrue(response.data["articleSetupState"]["generationReady"])
        self.assertTrue(response.data["hasCompletedArticleFlow"])
        self.assertEqual(response.data["startPageMode"], "topic_picker")
        config.refresh_from_db()
        self.assertFalse(config.articles_scaffolded)

    def test_merge_setup_pr_marks_setup_merged_and_daily_ready(self):
        config = self._prepare_articles_setup_gate(status="pr_created")
        config.connected_slack_user_id = "U123"
        config.save(update_fields=["connected_slack_user_id", "updated_at"])
        setup_run = ContentFactoryRun.objects.create(
            run_id="setup-merge-success",
            workflow="article_system_setup",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.COMPLETED,
            current_step="create_pull_request",
            approval_state=ContentFactoryApprovalState.APPROVED,
            result={
                "status": "setup_pr_created",
                "setup_status": "pr_created",
                "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/46",
                "pr_number": 46,
                "merge_status": "not_merged",
                "article_system_setup": {
                    "status": "pr_created",
                    "setup_run_id": "setup-merge-success",
                    "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/46",
                    "pr_number": 46,
                    "merge_status": "not_merged",
                },
            },
        )

        with (
            patch("content_factory.vibe_marketing_views._github_token_for_repo_operation", return_value=("github-token", "test")),
            patch(
                "content_factory.vibe_marketing_views._github_pull_checks_state",
                return_value=(
                    {"state": "open", "merged": False},
                    {"ready": True, "state": "success", "message": "Checks are passing."},
                ),
            ),
            patch("content_factory.vibe_marketing_views._github_api_request", return_value={"merged": True, "sha": "abc123"}),
        ):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{setup_run.run_id}/merge-setup-pr", {}, format="json")

        self.assertEqual(response.status_code, 200)
        setup_run.refresh_from_db()
        self.assertEqual(setup_run.status, ContentFactoryRunStatus.COMPLETED)
        self.assertEqual(setup_run.current_step, "merged")
        self.assertEqual(setup_run.result["merge_status"], "merged")
        self.assertEqual(setup_run.result["article_system_setup"]["status"], "merged")
        config.refresh_from_db()
        pending = config.article_system["pending_article_system_setup"]
        self.assertEqual(pending["status"], "merged")
        self.assertEqual(pending["merge_status"], "merged")
        self.assertTrue(config.articles_scaffolded)
        self.assertEqual(config.articles_scaffold_pr_url, "https://github.com/MLAI-AUS-Inc/mlai-au/pull/46")
        self.assertEqual(config.article_system["state"], "roo_scaffolded")

        with patch("content_factory.vibe_marketing_views.google_baseline_connection_status", return_value={}):
            bootstrap_response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(bootstrap_response.status_code, 200)
        scaffold = bootstrap_response.data["checks"]["scaffold"]
        self.assertTrue(scaffold["passed"])
        self.assertFalse(scaffold["setupBlocked"])
        self.assertTrue(scaffold["setupMerged"])
        self.assertTrue(scaffold["generationReady"])
        self.assertTrue(bootstrap_response.data["articleSetupState"]["generationReady"])
        self.assertTrue(bootstrap_response.data["hasCompletedArticleFlow"])
        self.assertEqual(bootstrap_response.data["startPageMode"], "topic_picker")
        self.assertTrue(bootstrap_response.data["checks"]["dailyAutomation"]["ready"])
        workflow_steps = {step["id"]: step for step in bootstrap_response.data["workflowProgress"]["steps"]}
        self.assertEqual(workflow_steps["research"]["status"], "ready")

        queued_article = ContentFactoryRun.objects.create(
            run_id="article-after-setup-merge",
            workflow="article_generation",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.QUEUED,
            result={},
        )
        with patch("content_factory.vibe_marketing_views._queue_content_factory_run", return_value=queued_article):
            article_response = self.client.post(
                "/api/v1/vibe-marketing/article/",
                {"topic": "AI adoption", "targetKeyword": "ai adoption"},
                format="json",
            )

        self.assertEqual(article_response.status_code, 202)
        self.assertEqual(article_response.data["runId"], "article-after-setup-merge")

        enable_response = self.client.post(
            f"/api/v1/vibe-marketing/runs/{setup_run.run_id}/enable-daily-automation",
            {"defaultTimezone": "Australia/Melbourne"},
            format="json",
        )

        self.assertEqual(enable_response.status_code, 200)
        config.refresh_from_db()
        self.assertTrue(config.daily_discovery_enabled)
        self.assertEqual(config.default_timezone, "Australia/Melbourne")


class VibeMarketingPublishFlowTests(TestCase):
    """One-button publish flow: poll-driven child refresh, evidence mirroring, auto-merge."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="founder-publish@example.com",
            password="password",
            role="participant",
        )
        self.profile = VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        self.company = VibeRaisingCompany.objects.create(
            profile=self.profile,
            organization=self.organization,
            name="MLAI",
            domain="mlai.au",
            registered=True,
        )
        self.profile.active_company = self.company
        self.profile.save(update_fields=["active_company", "updated_at"])
        OrganizationContentConfig.objects.create(organization=self.organization, github_repo="MLAI-AUS-Inc/mlai-au")
        self.run = ContentFactoryRun.objects.create(
            run_id="article-run-publish",
            workflow="article_generation",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.COMPLETED,
            result={"status": "completed"},
        )
        self.client.force_authenticate(user=self.user)

    def _create_publish_child(self, *, status=ContentFactoryRunStatus.QUEUED, result=None, auto_merge=False, run_id="article-publish-child"):
        run_request = {
            "source_run_id": self.run.run_id,
            "delivery_mode": "publish_code",
            "delivery_mode_confirmed": True,
        }
        if auto_merge:
            run_request["publish_auto_merge"] = True
        child = ContentFactoryRun.objects.create(
            run_id=run_id,
            workflow="article_generation",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=status,
            run_request=run_request,
            result={"source_run_id": self.run.run_id, **(result or {})},
        )
        article_result = dict(self.run.result or {})
        article_result["publish_child_run_id"] = child.run_id
        article_result["promoted_publish_job_id"] = child.run_id
        self.run.result = article_result
        self.run.save(update_fields=["result", "updated_at"])
        return child

    def _create_written_article(self, slug="ai-adoption-guide"):
        return WrittenArticle.objects.create(
            organization=self.organization,
            title="AI adoption guide",
            slug=slug,
            category="guides",
            primary_keyword="ai adoption",
        )

    def test_article_status_poll_refreshes_running_publish_child_from_remote(self):
        child = self._create_publish_child()
        article_row = self._create_written_article()
        remote_by_run_id = {
            child.run_id: {
                "run_id": child.run_id,
                "status": "completed",
                "current_step": "finalize",
                "result": {
                    "status": "completed",
                    "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/77",
                    "pr_number": 77,
                    "slug": "ai-adoption-guide",
                },
            },
        }

        with patch(
            "content_factory.vibe_marketing_views._call_content_factory_run_status",
            side_effect=lambda run_id, workflow="": remote_by_run_id.get(run_id, {}),
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        child.refresh_from_db()
        self.assertEqual(child.status, ContentFactoryRunStatus.COMPLETED)
        self.assertEqual(child.result["pr_url"], "https://github.com/MLAI-AUS-Inc/mlai-au/pull/77")
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["publish_child_status"], ContentFactoryRunStatus.COMPLETED)
        self.assertEqual(self.run.result["pr_url"], "https://github.com/MLAI-AUS-Inc/mlai-au/pull/77")
        self.assertEqual(response.data["prUrl"], "https://github.com/MLAI-AUS-Inc/mlai-au/pull/77")
        article_row.refresh_from_db()
        self.assertEqual(article_row.publish_status, ArticlePublishStatus.PR_OPEN)
        self.assertEqual(article_row.pr_url, "https://github.com/MLAI-AUS-Inc/mlai-au/pull/77")

    def test_article_status_poll_marks_lost_publish_child_recoverable(self):
        child = self._create_publish_child()
        status_calls = []

        def fake_status(run_id, workflow=""):
            status_calls.append(run_id)
            if run_id == child.run_id:
                return {
                    "status": ContentFactoryRunStatus.BLOCKED,
                    "error": f"Content Factory run {child.run_id} was not found.",
                    "diagnostics": {"content_factory_status_code": 404},
                }
            return {}

        with patch("content_factory.vibe_marketing_views._call_content_factory_run_status", side_effect=fake_status):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["publishChildRecoverable"])
        self.run.refresh_from_db()
        self.assertTrue(self.run.result["publish_child_recoverable"])
        child_status_calls = status_calls.count(child.run_id)
        self.assertEqual(child_status_calls, 1)

        cache.clear()
        with patch("content_factory.vibe_marketing_views._call_content_factory_run_status", side_effect=fake_status):
            second = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(second.status_code, 200)
        self.assertEqual(status_calls.count(child.run_id), child_status_calls)

    def test_article_status_poll_auto_merges_when_checks_pass(self):
        child = self._create_publish_child(
            status=ContentFactoryRunStatus.COMPLETED,
            result={
                "status": "completed",
                "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/88",
                "pr_number": 88,
                "slug": "ai-adoption-guide",
            },
            auto_merge=True,
        )
        article_row = self._create_written_article()

        with (
            patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}),
            patch("content_factory.vibe_marketing_views._github_token_for_repo_operation", return_value=("github-token", "test")),
            patch(
                "content_factory.vibe_marketing_views._github_pull_checks_state",
                return_value=(
                    {"state": "open", "merged": False},
                    {"ready": True, "state": "success", "message": "Checks are passing."},
                ),
            ),
            patch("content_factory.vibe_marketing_views._github_api_request", return_value={"merged": True, "sha": "abc123"}) as merge_mock,
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        merge_mock.assert_called_once()
        child.refresh_from_db()
        self.assertEqual(child.result["merge_status"], "merged")
        self.assertEqual(child.status, ContentFactoryRunStatus.COMPLETED)
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["merge_status"], "merged")
        article_row.refresh_from_db()
        self.assertEqual(article_row.publish_status, ArticlePublishStatus.MERGED)

    def test_auto_merge_waits_and_throttles_on_pending_checks(self):
        self._create_publish_child(
            status=ContentFactoryRunStatus.COMPLETED,
            result={
                "status": "completed",
                "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/89",
                "pr_number": 89,
            },
            auto_merge=True,
        )

        with (
            patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}),
            patch("content_factory.vibe_marketing_views._github_token_for_repo_operation", return_value=("github-token", "test")),
            patch(
                "content_factory.vibe_marketing_views._github_pull_checks_state",
                return_value=(
                    {"state": "open", "merged": False},
                    {"ready": False, "state": "pending", "message": "GitHub Actions checks are still running."},
                ),
            ) as checks_mock,
            patch("content_factory.vibe_marketing_views._github_api_request") as merge_mock,
        ):
            first = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")
            second = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        merge_mock.assert_not_called()
        self.assertEqual(checks_mock.call_count, 1)
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["checks_status"], "pending")
        self.assertEqual(self.run.result["publish_auto_merge_state"], "waiting_checks")

    def test_auto_merge_blocks_on_failed_checks_without_retry_storm(self):
        child = self._create_publish_child(
            status=ContentFactoryRunStatus.COMPLETED,
            result={
                "status": "completed",
                "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/90",
                "pr_number": 90,
            },
            auto_merge=True,
        )

        with (
            patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}),
            patch("content_factory.vibe_marketing_views._github_token_for_repo_operation", return_value=("github-token", "test")),
            patch(
                "content_factory.vibe_marketing_views._github_pull_checks_state",
                return_value=(
                    {"state": "open", "merged": False},
                    {"ready": False, "state": "failed", "message": "One or more GitHub Actions checks failed."},
                ),
            ) as checks_mock,
        ):
            first = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")
            cache.clear()
            second = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(checks_mock.call_count, 1)
        child.refresh_from_db()
        self.assertEqual(child.result["publish_auto_merge_state"], "blocked")
        self.assertEqual(child.result["merge_blocked_reason"], "One or more GitHub Actions checks failed.")
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["publish_auto_merge_state"], "blocked")
        self.assertEqual(self.run.result["merge_blocked_reason"], "One or more GitHub Actions checks failed.")

    def test_auto_merge_skipped_without_flag(self):
        self._create_publish_child(
            status=ContentFactoryRunStatus.COMPLETED,
            result={
                "status": "completed",
                "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/91",
                "pr_number": 91,
            },
        )

        with (
            patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}),
            patch("content_factory.vibe_marketing_views._github_pull_checks_state") as checks_mock,
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        checks_mock.assert_not_called()
        self.assertEqual(response.data["prUrl"], "https://github.com/MLAI-AUS-Inc/mlai-au/pull/91")

    def test_publish_child_preview_url_propagates_without_pr(self):
        self._create_publish_child(
            status=ContentFactoryRunStatus.COMPLETED,
            result={
                "status": "completed",
                "preview_url": "https://mlai.au/articles/ai-adoption-guide",
                "slug": "ai-adoption-guide",
            },
            auto_merge=True,
        )

        with (
            patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}),
            patch("content_factory.vibe_marketing_views._github_pull_checks_state") as checks_mock,
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        checks_mock.assert_not_called()
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["publish_child_preview_url"], "https://mlai.au/articles/ai-adoption-guide")

    def test_merge_publish_pr_action_merges_child_from_article_page(self):
        child = self._create_publish_child(
            status=ContentFactoryRunStatus.COMPLETED,
            result={
                "status": "completed",
                "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/92",
                "pr_number": 92,
                "slug": "ai-adoption-guide",
                "publish_auto_merge_state": "blocked",
                "merge_blocked_reason": "One or more GitHub Actions checks failed.",
            },
        )
        article_row = self._create_written_article()

        with (
            patch("content_factory.vibe_marketing_views._github_token_for_repo_operation", return_value=("github-token", "test")),
            patch(
                "content_factory.vibe_marketing_views._github_pull_checks_state",
                return_value=(
                    {"state": "open", "merged": False},
                    {"ready": True, "state": "success", "message": "Checks are passing."},
                ),
            ),
            patch("content_factory.vibe_marketing_views._github_api_request", return_value={"merged": True, "sha": "abc123"}),
        ):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/merge-publish-pr", {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["runId"], self.run.run_id)
        child.refresh_from_db()
        self.assertEqual(child.result["merge_status"], "merged")
        self.assertNotIn("merge_blocked_reason", child.result)
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["merge_status"], "merged")
        article_row.refresh_from_db()
        self.assertEqual(article_row.publish_status, ArticlePublishStatus.MERGED)

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_promote_bundle_records_auto_merge_flag(self):
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.result = {
            "status": "completed",
            "delivery_mode": "content_only",
            "componentManifest": {"components": [{"id": "title", "type": "title", "label": "Title"}]},
            "delivery_package": {"title": "AI adoption guide", "article_markdown": "article.md"},
        }
        self.run.save(update_fields=["acceptance_summary", "result", "updated_at"])

        def fake_post(url, json=None, headers=None, timeout=None):
            return _Response(
                status_code=202,
                payload={"run_id": "article-publish-child-flag", "status": "queued", "publish_child_status": "queued"},
            )

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(
                f"/api/v1/vibe-marketing/runs/{self.run.run_id}/promote-bundle",
                {"autoMerge": True},
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["runId"], "article-publish-child-flag")
        child = ContentFactoryRun.objects.get(run_id="article-publish-child-flag")
        self.assertTrue(child.result["publish_auto_merge"])
        self.assertTrue(child.run_request["publish_auto_merge"])
        self.run.refresh_from_db()
        self.assertTrue(self.run.result["publish_auto_merge"])
        self.assertTrue(self.run.run_request["publish_auto_merge"])

    def test_sync_local_run_from_remote_preserves_merge_evidence(self):
        child = self._create_publish_child(
            status=ContentFactoryRunStatus.COMPLETED,
            result={
                "status": "completed",
                "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/93",
                "pr_number": 93,
                "merge_status": "merged",
                "checks_status": "success",
                "publish_auto_merge": True,
            },
        )
        remote_data = {
            "run_id": child.run_id,
            "status": "completed",
            "current_step": "finalize",
            "result": {"status": "completed", "preview_url": "https://mlai.au/articles/ai-adoption-guide"},
        }

        synced = _sync_local_run_from_remote(child, remote_data)

        self.assertEqual(synced.result["merge_status"], "merged")
        self.assertEqual(synced.result["checks_status"], "success")
        self.assertEqual(synced.result["pr_url"], "https://github.com/MLAI-AUS-Inc/mlai-au/pull/93")
        self.assertTrue(synced.result["publish_auto_merge"])
        self.assertEqual(synced.result["preview_url"], "https://mlai.au/articles/ai-adoption-guide")

    def test_status_view_compact_includes_merge_keys(self):
        self._create_publish_child(
            status=ContentFactoryRunStatus.COMPLETED,
            result={
                "status": "completed",
                "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/94",
                "pr_number": 94,
                "merge_status": "merged",
                "checks_status": "success",
            },
        )

        with patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}?view=status")

        self.assertEqual(response.status_code, 200)
        result = response.data["result"]
        self.assertEqual(result["merge_status"], "merged")
        self.assertEqual(result["checks_status"], "success")
        self.assertEqual(result["pr_number"], 94)
