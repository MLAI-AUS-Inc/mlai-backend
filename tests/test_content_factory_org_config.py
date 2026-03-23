import os

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from core.models import Organization, OrganizationContentConfig


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
                "article_path_pattern": "app/articles/content/{category}/{slug}.tsx",
                "registry_path": "app/articles/registry.ts",
                "publish_targets": publish_targets,
                "default_publish_target_id": publish_targets[0]["target_id"],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.config.refresh_from_db()
        self.assertEqual(self.config.article_path_pattern, "app/articles/content/{category}/{slug}.tsx")
        self.assertEqual(self.config.publish_targets, publish_targets)
        self.assertEqual(self.config.default_publish_target_id, publish_targets[0]["target_id"])

        get_response = self.client.get(
            "/api/content-factory/org/config/",
            {"domain": "mlai.au"},
        )

        self.assertEqual(get_response.status_code, status.HTTP_200_OK)
        self.assertEqual(get_response.data["article_path_pattern"], "app/articles/content/{category}/{slug}.tsx")
        self.assertEqual(get_response.data["publish_targets"], publish_targets)
        self.assertEqual(
            get_response.data["default_publish_target_id"],
            publish_targets[0]["target_id"],
        )
