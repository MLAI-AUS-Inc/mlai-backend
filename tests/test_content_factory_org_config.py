import os

from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from content_factory.models import OrganizationContentConfig, WebsiteDesignSnapshot
from organizations.models import Organization
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

    def test_org_config_round_trips_generated_component_library(self):
        """Phase 0: PUT persists each generated component's import_statement + assembly metadata,
        and the GET surfaces a bounded `generated_components` library (name + import + metadata, NOT
        the component source) so content-factory can import + compose the real components instead of
        inlining generic helpers."""
        from content_factory.models import GeneratedComponent

        components = [
            {
                "name": "ArticleCallout",
                "content": "export function ArticleCallout() { return null }",  # must NOT leak to GET
                "source": "generated",
                "import_statement": "import { ArticleCallout } from '~/components/articles/ArticleCallout'",
                "metadata": {
                    "supported_section_types": ["callout"],
                    "prop_schema": {"title": "string", "body": "string"},
                    "default_export": False,
                },
            },
            {
                "name": "ArticleHeroHeader",
                "content": "export default function ArticleHeroHeader() { return null }",
                "source": "generated",
                "import_statement": "import ArticleHeroHeader from '~/components/articles/ArticleHeroHeader'",
                "metadata": {"supported_section_types": ["hero"], "default_export": True},
            },
        ]

        put_response = self.client.put(
            "/api/content-factory/org/config/",
            {"domain": "mlai.au", "generated_components": components},
            format="json",
        )
        self.assertEqual(put_response.status_code, status.HTTP_200_OK)

        # Persisted on the model with import_statement + structured metadata.
        callout = GeneratedComponent.objects.get(organization=self.organization, name="ArticleCallout")
        self.assertEqual(
            callout.import_statement,
            "import { ArticleCallout } from '~/components/articles/ArticleCallout'",
        )
        self.assertEqual(callout.metadata["supported_section_types"], ["callout"])
        self.assertEqual(callout.metadata["prop_schema"], {"title": "string", "body": "string"})

        # GET surfaces a bounded library: name + import_statement + flattened metadata, never `content`.
        get_response = self.client.get("/api/content-factory/org/config/", {"domain": "mlai.au"})
        self.assertEqual(get_response.status_code, status.HTTP_200_OK)
        library = {c["name"]: c for c in get_response.data["generated_components"]}
        self.assertEqual(set(library), {"ArticleCallout", "ArticleHeroHeader"})
        self.assertEqual(
            library["ArticleCallout"]["import_statement"],
            "import { ArticleCallout } from '~/components/articles/ArticleCallout'",
        )
        self.assertEqual(library["ArticleCallout"]["supported_section_types"], ["callout"])
        self.assertTrue(library["ArticleHeroHeader"]["default_export"])
        for entry in get_response.data["generated_components"]:
            self.assertNotIn("content", entry)  # bounded: source code never surfaced in the GET

    def test_org_config_round_trips_component_reuse_fields(self):
        """Component-reuse fields must survive PUT->GET so content-factory's SHA
        short-circuit and reuse decision stop regenerating components every scan."""
        setup_cache = {
            "schema_version": 1,
            "managed_files": ["app/components/articles/ArticleLayout.tsx"],
            "component_inventory": [
                {"name": "ArticleLayout", "path": "app/components/articles/ArticleLayout.tsx"}
            ],
            "context_fingerprint": "abc123",
        }
        specs = {"ArticleLayout": "# spec"}
        head_sha = "deadbeef" * 5  # 40 chars

        put_response = self.client.put(
            "/api/content-factory/org/config/",
            {
                "domain": "mlai.au",
                "article_template": "TEMPLATE",
                "repo_head_sha": head_sha,
                "scan_request_fingerprint": "fingerprint-xyz",
                "article_system_setup_cache": setup_cache,
                "framework_component_specs": specs,
            },
            format="json",
        )
        self.assertEqual(put_response.status_code, status.HTTP_200_OK)

        self.config.refresh_from_db()
        self.assertEqual(self.config.scan_request_fingerprint, "fingerprint-xyz")
        self.assertEqual(self.config.article_system_setup_cache, setup_cache)
        self.assertEqual(self.config.framework_component_specs, specs)
        self.assertEqual(self.config.last_scanned_sha, head_sha)

        get_response = self.client.get(
            "/api/content-factory/org/config/",
            {"domain": "mlai.au"},
        )
        self.assertEqual(get_response.status_code, status.HTTP_200_OK)
        data = get_response.data
        self.assertEqual(data["repo_head_sha"], head_sha)
        self.assertEqual(data["commit_sha"], head_sha)
        self.assertEqual(data["scan_request_fingerprint"], "fingerprint-xyz")
        self.assertEqual(data["article_system_setup_cache"], setup_cache)
        self.assertEqual(data["framework_component_specs"], specs)
        self.assertTrue(data["scan_completed_at"])  # set from last_scanned_at on PUT

    def test_org_config_round_trips_registry_driven_publish_target_metadata(self):
        publish_targets = [
            {
                "target_id": "registry_driven_seo_shared_lib_seo_public_pages_ts",
                "kind": "registry_driven_seo",
                "delivery_adapter": "registry_entry",
                "publish_capability": "direct",
                "registry_status": "publish_ready",
                "readiness": {
                    "structure_ready": True,
                    "mapping_ready": True,
                    "routing_ready": True,
                    "safety_ready": True,
                },
                "registration_strategy": {
                    "type": "registry_entry_patch",
                    "registry_path": "shared/lib/seo/public-pages.ts",
                    "registry_export_name": "PUBLIC_PAGES",
                    "parser_family": "typescript_static_array",
                    "route_template": "/resources/guides/{slug}",
                    "canonical_identity": {
                        "primary": "canonicalPath",
                        "secondary": "path",
                        "fallback": "slug",
                    },
                    "field_mapping": {
                        "slug": "id",
                        "path": "canonicalPath",
                        "title": "title",
                        "description": "description",
                        "content": "sections",
                    },
                    "content_adapter": {"type": "sections_array"},
                    "insertion_strategy": {
                        "type": "append_end_only",
                        "order_semantics": "irrelevant",
                    },
                    "route_validation": {
                        "registry_lookup": ["matches_detected_registry_lookup"],
                    },
                },
                "observability": {
                    "detection_score_breakdown": {"route_usage": 3, "field_match": 1},
                    "fallback_reason": "",
                },
            }
        ]
        article_system = {
            "state": "existing",
            "directory_name": "registry",
            "directory_path": "shared/lib/seo/public-pages.ts",
            "confidence": "high",
            "reason": "Detected registry-driven SEO system",
            "source": "scan",
            "verified_at": "2026-04-23T00:00:00+00:00",
            "system_type": "registry_driven_seo",
            "route_template": "/resources/guides/{slug}",
            "readiness": publish_targets[0]["readiness"],
            "registry": {
                "path": "shared/lib/seo/public-pages.ts",
                "export_name": "PUBLIC_PAGES",
            },
            "diagnostics": {
                "detection_score_breakdown": {"route_usage": 3, "field_match": 1},
            },
        }

        response = self.client.put(
            "/api/content-factory/org/config/",
            {
                "domain": "mlai.au",
                "article_system": article_system,
                "publish_targets": publish_targets,
                "default_publish_target_id": publish_targets[0]["target_id"],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.config.refresh_from_db()
        self.assertEqual(self.config.article_system["system_type"], "registry_driven_seo")
        self.assertEqual(self.config.article_system["readiness"], publish_targets[0]["readiness"])
        self.assertEqual(self.config.publish_targets, publish_targets)

        get_response = self.client.get(
            "/api/content-factory/org/config/",
            {"domain": "mlai.au"},
        )

        self.assertEqual(get_response.status_code, status.HTTP_200_OK)
        self.assertEqual(get_response.data["article_system"]["registry"]["export_name"], "PUBLIC_PAGES")
        self.assertEqual(get_response.data["publish_targets"], publish_targets)

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

    def test_org_config_round_trips_visual_design_fields(self):
        """The four live-site visual fields must survive PUT->GET. Previously the PUT
        whitelist dropped them, breaking the design-memory loop so articles and the
        scaffolded directory lost the target site's look."""
        visual_context = {
            "consolidated_tokens": {
                "background_color": "rgb(251, 243, 219)",
                "text_color": "rgb(17, 17, 17)",
                "primary_font": "Inter",
                "content_width": "1024px",
            },
            "pages": [{"page_path": "/", "screenshot_url": "https://cdn.example.com/home.png"}],
        }
        renderer_style_profile = {
            "section": "mt-10",
            "h2": "text-3xl font-semibold",
            "paragraph": "text-base leading-7",
        }
        reference_screenshots = [
            "https://cdn.example.com/home.png",
            "https://cdn.example.com/articles.png",
        ]
        directory_style_feedback = {
            "renderer_style_profile": renderer_style_profile,
            "directory_page_spec": {"sections": ["hero", "list"]},
            "accepted_at": "2026-06-21T00:00:00+00:00",
        }

        response = self.client.put(
            "/api/content-factory/org/config/",
            {
                "domain": "mlai.au",
                "visual_context": visual_context,
                "renderer_style_profile": renderer_style_profile,
                "reference_screenshots": reference_screenshots,
                "directory_style_feedback": directory_style_feedback,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.config.refresh_from_db()
        self.assertEqual(self.config.visual_context, visual_context)
        self.assertEqual(self.config.renderer_style_profile, renderer_style_profile)
        self.assertEqual(self.config.reference_screenshots, reference_screenshots)
        self.assertEqual(self.config.directory_style_feedback, directory_style_feedback)

        get_response = self.client.get(
            "/api/content-factory/org/config/",
            {"domain": "mlai.au"},
        )
        self.assertEqual(get_response.status_code, status.HTTP_200_OK)
        data = get_response.data
        self.assertEqual(data["visual_context"], visual_context)
        self.assertEqual(data["renderer_style_profile"], renderer_style_profile)
        self.assertEqual(data["reference_screenshots"], reference_screenshots)
        self.assertEqual(data["directory_style_feedback"], directory_style_feedback)

    def test_org_config_design_snapshot_lifecycle(self):
        """A design_snapshot payload creates exactly one active WebsiteDesignSnapshot,
        round-trips on GET as active_design_snapshot, and is superseded by the next."""
        first = self.client.put(
            "/api/content-factory/org/config/",
            {
                "domain": "mlai.au",
                "design_snapshot": {
                    "schema_version": 1,
                    "repo_head_sha": "a" * 40,
                    "source_urls": ["https://mlai.au/", "https://mlai.au/articles"],
                    "screenshot_urls": ["https://cdn.example.com/home.png"],
                    "captured_at": "2026-06-21T00:00:00+00:00",
                    "spec": {"color_system": {"background": "#fbf3db", "accent": "#ffd400"}},
                    "design_spec_md": "# Design Spec\nBold black display type.",
                },
            },
            format="json",
        )
        self.assertEqual(first.status_code, status.HTTP_200_OK)

        snapshots = WebsiteDesignSnapshot.objects.filter(organization=self.organization)
        self.assertEqual(snapshots.count(), 1)
        self.assertEqual(snapshots.filter(is_active=True).count(), 1)
        active = snapshots.get(is_active=True)
        self.assertEqual(active.spec["color_system"]["accent"], "#ffd400")
        self.assertEqual(active.repo_head_sha, "a" * 40)
        self.assertIsNotNone(active.captured_at)

        get_response = self.client.get(
            "/api/content-factory/org/config/",
            {"domain": "mlai.au"},
        )
        self.assertEqual(get_response.status_code, status.HTTP_200_OK)
        snapshot_payload = get_response.data["active_design_snapshot"]
        self.assertIsNotNone(snapshot_payload)
        self.assertEqual(snapshot_payload["schema_version"], 1)
        self.assertEqual(snapshot_payload["spec"]["color_system"]["background"], "#fbf3db")
        self.assertEqual(snapshot_payload["design_spec_md"], "# Design Spec\nBold black display type.")

        # A second snapshot supersedes the first; exactly one stays active.
        second = self.client.put(
            "/api/content-factory/org/config/",
            {
                "domain": "mlai.au",
                "design_snapshot": {
                    "schema_version": 2,
                    "spec": {"color_system": {"background": "#ffffff"}},
                },
            },
            format="json",
        )
        self.assertEqual(second.status_code, status.HTTP_200_OK)

        snapshots = WebsiteDesignSnapshot.objects.filter(organization=self.organization)
        self.assertEqual(snapshots.count(), 2)
        self.assertEqual(snapshots.filter(is_active=True).count(), 1)
        newest = snapshots.get(is_active=True)
        self.assertEqual(newest.schema_version, 2)
        self.assertEqual(snapshots.filter(status="superseded").count(), 1)

        get_response = self.client.get(
            "/api/content-factory/org/config/",
            {"domain": "mlai.au"},
        )
        self.assertEqual(get_response.data["active_design_snapshot"]["schema_version"], 2)
