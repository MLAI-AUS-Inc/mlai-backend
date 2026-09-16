"""Run with unittest: in-memory persistence, no database or migrations."""
import sys
import unittest
from contextlib import nullcontext
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from django.conf import settings
if not settings.configured:
    settings.configure(SECRET_KEY="custom-island-unit-only", USE_TZ=True, USE_I18N=False,
                       DATABASES={"default": {"ENGINE": "django.db.backends.dummy"}},
                       REST_FRAMEWORK={"UNAUTHENTICATED_USER": None, "DEFAULT_AUTHENTICATION_CLASSES": []})
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from .custom_island_views import CustomContentIslandView
from .custom_islands import validate_custom_island, custom_island_description, custom_island_slug, resolve_island_discovery_scope


BRIEF = {"subject": "AI transformation", "description": "Integrate AI into business workflows and train teams to run their first pilot.",
         "audience": "Business owners and operations leaders", "focus": "Practical implementation and adoption",
         "name": "AI integration in practice", "keyword": "AI integration for businesses"}


class CustomIslandUnitTests(unittest.TestCase):
    def test_accepts_noncommercial_subject_and_custom_direction_without_a_product(self):
        brief = {**BRIEF, "subject": "Community volunteering",
                 "description": "Share local volunteers' stories and highlight ways people can take part.",
                 "audience": "Local residents", "focus": "Inspire participation through personal stories, not buying guides."}
        clean = validate_custom_island(brief)
        self.assertNotIn("productName", clean)
        self.assertEqual(clean["subject"], "Community volunteering")
        self.assertIn(brief["focus"], custom_island_description(clean))
        self.assertTrue(custom_island_description(clean).startswith("Subject: Community volunteering"))
        self.assertEqual(self.post(brief).status_code, 201)

    def test_legacy_product_alias_preserves_retry_identity_and_rejects_conflicts(self):
        legacy = {"productName" if key == "subject" else key: value for key, value in BRIEF.items()}
        self.assertEqual(validate_custom_island(legacy), validate_custom_island(BRIEF))
        self.assertEqual(custom_island_slug(validate_custom_island(BRIEF)), custom_island_slug(legacy))
        self.assertEqual(self.post(legacy).status_code, 201)
        self.assertEqual(self.post(BRIEF).status_code, 200)
        with self.assertRaisesRegex(ValueError, "one subject"):
            validate_custom_island({**BRIEF, "productName": "An unrelated product"})

    def test_short_subjects_and_unicode_are_valid_islands(self):
        for subject in ("AI", "UX", "R", "園芸"):
            with self.subTest(subject=subject):
                clean = validate_custom_island({**BRIEF, "subject": subject, "name": subject, "keyword": subject})
                self.assertEqual(clean["subject"], subject)

    def test_brief_round_trip_retains_all_product_and_audience_context(self):
        clean = validate_custom_island({**BRIEF, "description": f"  {BRIEF['description']}  "})
        description = custom_island_description(clean)
        for key in ("subject", "description", "audience", "focus"):
            self.assertIn(BRIEF[key], description)

    def test_invalid_briefs_rejected_with_useful_field_names(self):
        for key in BRIEF:
            for value in (None, {}, "   ", "a" * 5001):
                with self.subTest(key=key, value=type(value)), self.assertRaises(ValueError):
                    validate_custom_island({**BRIEF, key: value})

    def test_retry_identity_is_stable_but_distinguishes_different_briefs(self):
        clean = validate_custom_island(BRIEF)
        self.assertEqual(custom_island_slug(clean), custom_island_slug(dict(reversed(list(clean.items())))))
        self.assertNotEqual(custom_island_slug(clean), custom_island_slug({**clean, "audience": "Doctors"}))
        self.assertLessEqual(len(custom_island_slug({**clean, "name": "x" * 160})), 80)
        self.assertTrue(custom_island_slug({**clean, "name": "人工智能"}).startswith("custom-island-"))

    def setUp(self):
        self.selection_patch = patch("content_factory.island_selection.selection_runs", return_value=[])
        self.selection_patch.start()
        self.addCleanup(self.selection_patch.stop)
        self.org = SimpleNamespace(pk="company-org-1")
        self.resolve = Mock(return_value=(SimpleNamespace(organization=self.org), None))
        self.manager = Mock()
        self.store = {}
        def save(**kwargs):
            self.assertIs(kwargs["organization"], self.org)
            key = (kwargs["organization"].pk, kwargs["slug"])
            created = key not in self.store
            if created:
                self.store[key] = SimpleNamespace(slug=kwargs["slug"], **kwargs["defaults"])
            return self.store[key], created
        self.manager.get_or_create.side_effect = save
        org_manager = Mock()
        org_manager.select_for_update.return_value.get.return_value = self.org
        self.modules = {}
        for name, attrs in {
            "organizations.models": {"Organization": SimpleNamespace(objects=org_manager)},
            "content_factory.models": {"ContentIsland": SimpleNamespace(objects=self.manager),
                "ContentIslandOrigin": SimpleNamespace(MANUAL="manual"), "ContentIslandStatus": SimpleNamespace(VISIBLE="visible")},
            "content_factory.vibe_marketing_views": {"_resolve_context_or_response": self.resolve, "_content_islands_enabled": lambda: False},
        }.items():
            module = ModuleType(name)
            module.__dict__.update(attrs)
            self.modules[name] = module

    def post(self, data=None, authenticated=True):
        request = APIRequestFactory().post("/islands/custom", BRIEF if data is None else data, format="json")
        if authenticated:
            force_authenticate(request, user=SimpleNamespace(pk="owner", is_authenticated=True))
        with patch.dict(sys.modules, self.modules), patch("content_factory.custom_island_views.transaction.atomic", return_value=nullcontext()):
            return CustomContentIslandView.as_view()(request)

    def test_save_then_retry_creates_one_visible_manual_island(self):
        first = self.post()
        second = self.post()
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(self.store), 1)
        self.assertEqual(first.data["island"], second.data["island"])
        island = next(iter(self.store.values()))
        self.assertEqual(island.origin, "manual")
        self.assertEqual(island.status, "visible")
        self.assertEqual(first.data["island"]["pillarKeyword"], BRIEF["keyword"])
        self.assertEqual(first.data["island"]["topicCandidates"], [])

    def test_auth_and_company_access_fail_before_persistence(self):
        self.assertIn(self.post(authenticated=False).status_code, (401, 403))
        self.resolve.return_value = (None, Response({"detail": "Company not owned"}, status=403))
        self.assertEqual(self.post().status_code, 403)
        self.manager.get_or_create.assert_not_called()

    def test_invalid_input_has_no_persistence_side_effect(self):
        response = self.post({**BRIEF, "description": "short"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Content description", response.data["detail"])
        self.manager.get_or_create.assert_not_called()

    def test_first_custom_island_preserves_existing_fallback_themes(self):
        self.modules["content_factory.vibe_marketing_views"]._content_islands_enabled = lambda: True
        self.manager.filter.return_value.exists.return_value = False
        with patch("content_factory.content_islands.seed_islands_from_bootstrap_pillars") as seed:
            self.assertEqual(self.post().status_code, 201)
        seed.assert_called_once_with(self.org)

    def test_arbitrary_organization_and_visual_fields_are_not_trusted(self):
        response = self.post({**BRIEF, "organization": "victim", "origin": "cluster_birth", "status": "archived", "color_key": "invalid"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(next(iter(self.store.values())).origin, "manual")
        self.assertEqual(response.data["island"]["colorKey"], "purple")

    def test_discovery_uses_saved_brief_and_limits_lookup_to_current_company(self):
        self.post()
        island = next(iter(self.store.values()))
        self.manager.filter.return_value.first.return_value = island
        with patch.dict(sys.modules, self.modules):
            scope = resolve_island_discovery_scope(self.org, None, island.slug)
        self.manager.filter.assert_called_once_with(organization=self.org, slug=island.slug, status="visible")
        self.assertEqual(scope["context"], custom_island_description(BRIEF))
        self.assertEqual(scope["keyword"], BRIEF["keyword"])

    def test_missing_or_foreign_island_is_rejected_but_legacy_pillars_work(self):
        self.manager.filter.return_value.first.return_value = None
        fallback = Mock(return_value=[])
        self.modules["content_factory.vibe_marketing_views"]._topic_pillars_for_bootstrap = fallback
        with patch.dict(sys.modules, self.modules):
            with self.assertRaisesRegex(ValueError, "belonging to this company"):
                resolve_island_discovery_scope(self.org, None, "foreign-island")
            fallback.return_value = [{"slug": "legacy", "name": "Legacy theme", "pillarKeyword": "legacy search"}]
            scope = resolve_island_discovery_scope(self.org, None, "legacy")
        self.assertEqual(scope["keyword"], "legacy search")
        self.assertEqual(scope["context"], "")


if __name__ == "__main__":
    unittest.main()
