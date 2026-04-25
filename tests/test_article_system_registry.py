from django.test import SimpleTestCase

from content_factory.article_system import (
    best_registry_driven_publish_target,
    is_registry_driven_publish_target,
    normalize_article_system,
    registry_publish_target_from_article_system,
    registry_target_publish_ready,
    registry_target_readiness,
)


class RegistryDrivenArticleSystemTests(SimpleTestCase):
    def test_normalize_article_system_preserves_registry_metadata(self):
        article_system = normalize_article_system(
            {
                "state": "existing",
                "directory_path": "shared/lib/seo/public-pages.ts",
                "confidence": "high",
                "source": "scan",
                "system_type": "registry_driven_seo",
                "route_template": "/resources/guides/{slug}",
                "readiness": {
                    "structure_ready": True,
                    "mapping_ready": True,
                    "routing_ready": True,
                    "safety_ready": True,
                },
                "registry": {
                    "path": "shared/lib/seo/public-pages.ts",
                    "export_name": "PUBLIC_PAGES",
                },
                "diagnostics": {
                    "detection_score_breakdown": {"route_usage": 3},
                },
                "registry_selection_cache": {
                    "scope": "org_repo",
                    "selected_target_id": "registry_driven_seo_shared_lib_seo_public_pages_ts",
                },
                "observability": {
                    "fallback_reason": "",
                },
            }
        )

        self.assertEqual(article_system["system_type"], "registry_driven_seo")
        self.assertEqual(article_system["route_template"], "/resources/guides/{slug}")
        self.assertEqual(article_system["registry"]["export_name"], "PUBLIC_PAGES")
        self.assertTrue(article_system["readiness"]["safety_ready"])
        self.assertEqual(article_system["registry_selection_cache"]["scope"], "org_repo")
        self.assertIn("fallback_reason", article_system["observability"])

    def test_registry_target_requires_all_readiness_substates(self):
        target = {
            "kind": "registry_driven_seo",
            "delivery_adapter": "registry_entry",
            "readiness": {
                "structure_ready": True,
                "mapping_ready": True,
                "routing_ready": True,
                "safety_ready": False,
            },
            "registration_strategy": {
                "type": "registry_entry_patch",
                "registry_path": "src/data/pages.ts",
            },
        }

        self.assertTrue(is_registry_driven_publish_target(target))
        self.assertFalse(registry_target_publish_ready(target))
        self.assertFalse(registry_target_readiness(target)["safety_ready"])

        target["readiness"]["safety_ready"] = True

        self.assertTrue(registry_target_publish_ready(target))

    def test_best_registry_target_prefers_publish_ready_target(self):
        pending_target = {
            "target_id": "registry_pending",
            "kind": "registry_driven_seo",
            "delivery_adapter": "registry_entry",
            "readiness": {
                "structure_ready": True,
                "mapping_ready": False,
                "routing_ready": True,
                "safety_ready": False,
            },
            "registration_strategy": {"type": "registry_entry_patch"},
        }
        ready_target = {
            "target_id": "registry_ready",
            "kind": "registry_driven_seo",
            "delivery_adapter": "registry_entry",
            "registry_status": "publish_ready",
            "registration_strategy": {"type": "registry_entry_patch"},
        }

        self.assertEqual(
            best_registry_driven_publish_target([pending_target, ready_target])["target_id"],
            "registry_ready",
        )

    def test_registry_target_can_be_synthesized_from_article_system(self):
        article_system = {
            "system_type": "registry_driven_seo",
            "directory_path": "shared/lib/seo/public-pages.ts",
            "route_template": "/resources/guides/{slug}",
            "readiness": {
                "publish_ready": True,
            },
            "diagnostics": {
                "issues": ["route field is ambiguous"],
            },
        }

        target = registry_publish_target_from_article_system(article_system)

        self.assertTrue(is_registry_driven_publish_target(target))
        self.assertTrue(registry_target_publish_ready(target))
        self.assertEqual(
            target["registration_strategy"]["registry_path"],
            "shared/lib/seo/public-pages.ts",
        )
        self.assertEqual(best_registry_driven_publish_target([], article_system), target)
