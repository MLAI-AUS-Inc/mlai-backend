import os

from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from core.models import Organization, OrganizationContentConfig
from integrations.models import UserIntegration


@override_settings(SCHEDULED_DISCOVERY_MAX_TARGETS=1)
class ContentFactoryOrgConfigTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "test_roo_key"
        os.environ["ROO_API_KEY"] = self.api_key
        os.environ["INTERNAL_API_KEY"] = self.api_key

        from django.conf import settings

        settings.ROO_API_KEY = self.api_key
        settings.INTERNAL_API_KEY = self.api_key
        self.client.credentials(HTTP_X_API_KEY=self.api_key)

        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        self.config = OrganizationContentConfig.objects.create(
            organization=self.organization,
            article_path_pattern="app/articles/{category}/{slug}.tsx",
            registry_path="app/articles/registry.ts",
        )

    def test_org_config_round_trips_publish_target_metadata(self):
        publish_targets = [
            {
                "target_id": "react_article_system_app_articles_content_{category}_{slug}_tsx__tsx",
                "kind": "react_article_system",
                "content_path_pattern": "app/articles/content/{category}/{slug}.tsx",
                "route_template": "/articles/{category}/{slug}",
                "registration_strategy": {
                    "type": "registry_seo_patch",
                    "registry_path": "app/articles/registry.ts",
                    "seo_config_path": "app/articles/seo-config.ts",
                },
            }
        ]

        response = self.client.put(
            "/api/content-factory/org/config/",
            {
                "domain": "mlai.au",
                "default_timezone": "Australia/Melbourne",
                "article_delivery_mode": "content_only",
                "article_path_pattern": "app/articles/content/{category}/{slug}.tsx",
                "registry_path": "app/articles/registry.ts",
                "publish_targets": publish_targets,
                "default_publish_target_id": publish_targets[0]["target_id"],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.config.refresh_from_db()
        self.assertEqual(self.config.default_timezone, "Australia/Melbourne")
        self.assertEqual(self.config.article_delivery_mode, "content_only")
        self.assertEqual(self.config.article_path_pattern, "app/articles/content/{category}/{slug}.tsx")
        self.assertEqual(self.config.publish_targets, publish_targets)
        self.assertEqual(self.config.default_publish_target_id, publish_targets[0]["target_id"])

        get_response = self.client.get(
            "/api/content-factory/org/config/",
            {"domain": "mlai.au"},
        )

        self.assertEqual(get_response.status_code, status.HTTP_200_OK)
        self.assertEqual(get_response.data["default_timezone"], "Australia/Melbourne")
        self.assertEqual(get_response.data["article_delivery_mode"], "content_only")
        self.assertEqual(get_response.data["article_path_pattern"], "app/articles/content/{category}/{slug}.tsx")
        self.assertEqual(get_response.data["publish_targets"], publish_targets)
        self.assertEqual(
            get_response.data["default_publish_target_id"],
            publish_targets[0]["target_id"],
        )

    def test_org_config_round_trips_build_healing_hints(self):
        hints = [
            {
                "failure_family_key": "vite-transform-tsx",
                "applies_to": ["article_module"],
                "summary": "Prefer build-safe JSON-LD rendering in article modules.",
                "snippet_or_rule": "Use dangerouslySetInnerHTML for JSON-LD script tags and keep FAQ answers serializable.",
            }
        ]

        response = self.client.put(
            "/api/content-factory/org/config/",
            {
                "domain": "mlai.au",
                "build_healing_hints": hints,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.config.refresh_from_db()
        self.assertEqual(self.config.build_healing_hints, hints)

        get_response = self.client.get(
            "/api/content-factory/org/config/",
            {"domain": "mlai.au"},
        )

        self.assertEqual(get_response.status_code, status.HTTP_200_OK)
        self.assertEqual(get_response.data["build_healing_hints"], hints)

    def test_org_config_round_trips_repo_execution_contract(self):
        execution_contract = {
            "runtime_family": "python",
            "workspace_subdir": "apps/content",
            "install_command": ["pip", "install", "-r", "requirements.txt"],
            "typecheck_command": ["python", "-m", "py_compile", "main.py"],
            "test_command": ["pytest", "-q"],
            "build_command": ["python", "build.py"],
            "preview_command": ["python", "-m", "http.server", "8000"],
            "browser_entry_url": "http://127.0.0.1:8000",
            "required_env_keys": ["OPENAI_API_KEY"],
            "system_packages": ["libpq-dev"],
            "registry_auth_refs": ["pypi-internal"],
            "supports_direct_publish": False,
            "fallback_mode": "draft_pr",
        }

        response = self.client.put(
            "/api/content-factory/org/config/",
            {
                "domain": "mlai.au",
                "repo_execution_contract": execution_contract,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.config.refresh_from_db()
        self.assertEqual(self.config.repo_execution_contract, execution_contract)

        get_response = self.client.get(
            "/api/content-factory/org/config/",
            {"domain": "mlai.au"},
        )

        self.assertEqual(get_response.status_code, status.HTTP_200_OK)
        self.assertEqual(get_response.data["repo_execution_contract"], execution_contract)

    def test_org_config_round_trips_daily_discovery_fields(self):
        response = self.client.put(
            "/api/content-factory/org/config/",
            {
                "domain": "mlai.au",
                "connected_slack_user_id": "U-MLAI",
                "daily_discovery_enabled": True,
                "daily_discovery_priority": 3,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.config.refresh_from_db()
        self.assertEqual(self.config.connected_slack_user_id, "U-MLAI")
        self.assertTrue(self.config.daily_discovery_enabled)
        self.assertEqual(self.config.daily_discovery_priority, 3)

        get_response = self.client.get(
            "/api/content-factory/org/config/",
            {"domain": "mlai.au"},
        )

        self.assertEqual(get_response.status_code, status.HTTP_200_OK)
        self.assertEqual(get_response.data["connected_slack_user_id"], "U-MLAI")
        self.assertTrue(get_response.data["daily_discovery_enabled"])
        self.assertEqual(get_response.data["daily_discovery_priority"], 3)

    def test_enabling_daily_discovery_requires_owner(self):
        response = self.client.put(
            "/api/content-factory/org/config/",
            {
                "domain": "mlai.au",
                "daily_discovery_enabled": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("connected_slack_user_id", response.data["error"])

    def test_enabling_daily_discovery_infers_owner_from_repo_and_enforces_max_targets(self):
        UserIntegration.objects.create(
            slack_user_id="U-OWNER",
            github_repo="owner/mlai-au",
        )
        response = self.client.put(
            "/api/content-factory/org/config/",
            {
                "domain": "mlai.au",
                "github_repo": "owner/mlai-au",
                "daily_discovery_enabled": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.config.refresh_from_db()
        self.assertEqual(self.config.connected_slack_user_id, "U-OWNER")
        self.assertTrue(self.config.daily_discovery_enabled)

        second_org = Organization.objects.create(name="Beta", domain="beta.example.com")
        OrganizationContentConfig.objects.create(
            organization=second_org,
            connected_slack_user_id="U-BETA",
        )
        response = self.client.put(
            "/api/content-factory/org/config/",
            {
                "domain": "beta.example.com",
                "daily_discovery_enabled": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("No more than 1 organizations", response.data["error"])
