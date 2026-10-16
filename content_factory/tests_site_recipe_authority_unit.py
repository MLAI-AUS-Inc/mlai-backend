"""Current recipe authorization uses saved server inputs and performs no writes."""
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from . import website_site_recipe as recipe
from .website_contract import WebsiteAuthorityError


@override_settings(ARTICLES_SITE_CONTRACT_V2_ENABLED=True)
class SiteRecipeAuthorityTests(SimpleTestCase):
    def setUp(self):
        self.connection = SimpleNamespace(pk="25a99b3e-53a1-4dbd-b9cd-c26585f6d7dc", generation=2, repository_id=12,
            github_repo="fixture/site", branch="main", app_root="", state="connected", capabilities={"publishingReady": False},
            organization=SimpleNamespace(domain="fixture.test"), targets=Mock())
        self.target = {"target_id": "articles", "kind": "static_markdown", "delivery_adapter": "static_markdown",
            "content_path_pattern": "content/{slug}.md", "route_template": "/articles/{slug}.html",
            "site_contract_digest": "e" * 64, "contract": {"live_marker": {"kind": "artifact_digest", "value": "d" * 64}}}
        self.target["contract_digest"] = recipe.native_target_digest(self.target)
        self.binding = {"website_connection_id": str(self.connection.pk), "connection_generation": 2, "repository_id": 12,
            "github_repo": "fixture/site", "branch": "main", "app_root": "", "operation_id": "b5789f29-8f06-44a2-bb97-335ec0242ac7",
            "operation_attempt": 1, "deletion_epoch": 0, "expected_source_sha": "a" * 40, "target_id": "articles",
            "site_contract_digest": "e" * 64, "contract_digest": self.target["contract_digest"],
            "run_id": "site-job", "site_recipe_version": 2}
        self.operation = SimpleNamespace(pk=self.binding["operation_id"], action="workflow", generation=2, state="running",
            payload={"workflow": "hosted_native_verification", "run_id": "site-job", "binding": deepcopy(self.binding),
                "site_recipe_probe": {"slug": "fixture", "title": "Fixture article", "port": 3000}})
        self.connection.targets.filter.return_value.first.return_value = SimpleNamespace(contract=self.target)
        self.query = {key: str(value) for key, value in self.binding.items()}
        self.query["action"] = "custom_contract"

    def call(self, query=None):
        with patch("content_factory.website_hosted_verification.enabled", return_value=True), \
                patch("content_factory.website_operations.validate_operation", return_value=self.operation), \
                patch("content_factory.website_operations.deletion_epoch", return_value=0):
            return recipe.authorization_inputs(self.connection, self.query if query is None else query)

    def test_exact_current_reservation_projects_only_read_build_inputs(self):
        before = deepcopy(self.operation.payload), deepcopy(self.target)
        result = self.call()
        self.assertEqual(result["binding"]["connection_generation"], 2)
        self.assertEqual(result["binding"]["operation_id"], self.binding["operation_id"])
        self.assertEqual(result["binding"]["adapter_id"], "static_markdown")
        self.assertEqual(result["probe"], self.operation.payload["site_recipe_probe"])
        self.assertEqual(result["target"]["site_contract_digest"], "e" * 64)
        self.assertNotIn("capabilities", result)
        self.assertNotIn("verification", result["target"])
        self.connection.targets.filter.assert_called_once_with(generation=2, target_key="articles", source_sha="a" * 40)
        self.assertEqual(before, (self.operation.payload, self.target))
        result["probe"]["title"] = "Changed returned data"
        self.assertEqual(self.operation.payload["site_recipe_probe"]["title"], "Fixture article")
        self.assertEqual(self.connection.capabilities, {"publishingReady": False})

    def test_disabled_extension_performs_no_target_query(self):
        with override_settings(ARTICLES_SITE_CONTRACT_V2_ENABLED=False), self.assertRaises(WebsiteAuthorityError):
            self.call()
        self.connection.targets.filter.assert_not_called()

    def test_query_cannot_replace_saved_target_source_recipe_run_or_operation(self):
        for field, value in (("run_id", "other"), ("target_id", "other"), ("expected_source_sha", "b" * 40),
                ("site_contract_digest", "f" * 64), ("site_recipe_version", "3"), ("operation_id", "other"),
                ("operation_attempt", "2"), ("deletion_epoch", "1"), ("branch", "other"), ("app_root", "apps/other"),
                ("connection_generation", "1"), ("repository_id", "13"), ("github_repo", "other/site")):
            with self.subTest(field=field), self.assertRaises(WebsiteAuthorityError):
                self.call({**self.query, field: value})
        self.connection.targets.filter.assert_not_called()

    def test_foreign_or_coerced_original_scope_is_rejected_before_target_read(self):
        for field, value in (("connection_generation", 1), ("connection_generation", True), ("repository_id", "12"),
                ("branch", "other"), ("app_root", "apps/other"), ("github_repo", "other/site"),
                ("operation_id", "other"), ("deletion_epoch", 1), ("website_connection_id", "other")):
            original = deepcopy(self.operation.payload["binding"])
            self.operation.payload["binding"][field] = value
            with self.subTest(field=field), self.assertRaises(WebsiteAuthorityError):
                self.call()
            self.operation.payload["binding"] = original
        self.connection.targets.filter.assert_not_called()

    def test_old_generation_cancelled_or_wrong_workflow_never_receives_target_inputs(self):
        for field, value in (("generation", 1), ("state", "cancelled"), ("action", "prepare")):
            original = getattr(self.operation, field)
            setattr(self.operation, field, value)
            with self.subTest(field=field), self.assertRaises(WebsiteAuthorityError):
                self.call()
            setattr(self.operation, field, original)
        self.operation.payload["workflow"] = "article_system_setup"
        with self.assertRaises(WebsiteAuthorityError):
            self.call()
        self.connection.targets.filter.assert_not_called()

    def test_target_must_already_own_the_recipe_and_unchanged_native_digest(self):
        original = deepcopy(self.target)
        for field, value in (("site_contract_digest", "f" * 64), ("contract_digest", "f" * 64),
                ("route_template", "/other/{slug}"), ("target_id", "other"), ("adapter_version", True)):
            self.target[field] = value
            with self.subTest(field=field), self.assertRaises(WebsiteAuthorityError):
                self.call()
            self.target.clear()
            self.target.update(deepcopy(original))
        self.connection.targets.filter.return_value.first.return_value = None
        with self.assertRaises(WebsiteAuthorityError):
            self.call()

    def test_probe_is_saved_server_data_and_never_selected_from_query(self):
        result = self.call({**self.query, "probe": "untrusted", "command": "untrusted"})
        self.assertEqual(result["probe"]["slug"], "fixture")
        for change in ({"slug": "../other"}, {"port": True}, {"title": "token=opaque"},
                {"title": "x" * 257}, {"is_setup_example": "true"}, {"command": ["untrusted"]}):
            self.operation.payload["site_recipe_probe"] = {"slug": "fixture", "title": "Fixture article", **change}
            with self.subTest(change=change), self.assertRaises(WebsiteAuthorityError):
                self.call()

    def test_actual_authorization_view_adds_receipt_only_for_explicit_v2_request(self):
        from .website_views import WebsiteConnectionAuthorizeView
        with patch("content_factory.website_views.authority_guard", return_value=nullcontext(self.connection)), \
                patch("content_factory.website_operations.deletion_epoch", return_value=0), \
                patch.object(recipe, "authorization_inputs", return_value={"version": 1}) as inputs:
            response = WebsiteConnectionAuthorizeView().get(SimpleNamespace(query_params=self.query))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["site_recipe_verification"], {"version": 1})
            self.assertEqual(response.data["permission_mode"], "read")
            inputs.assert_called_once_with(self.connection, self.query)
            inputs.reset_mock()
            legacy = {key: value for key, value in self.query.items() if key not in {"site_recipe_version", "site_contract_digest"}}
            response = WebsiteConnectionAuthorizeView().get(SimpleNamespace(query_params=legacy))
            self.assertNotIn("site_recipe_verification", response.data)
            inputs.assert_not_called()

    def test_actual_view_returns_denial_without_a_partial_receipt(self):
        from .website_views import WebsiteConnectionAuthorizeView
        with patch("content_factory.website_views.authority_guard", return_value=nullcontext(self.connection)), \
                patch.object(recipe, "authorization_inputs", side_effect=WebsiteAuthorityError("verified_target_required", "Unavailable")):
            response = WebsiteConnectionAuthorizeView().get(SimpleNamespace(query_params=self.query))
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.data["allowed"])
        self.assertNotIn("site_recipe_verification", response.data)
