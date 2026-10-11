"""Database-free authorization and input contracts for opt-in Articles tools."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase
from rest_framework.exceptions import PermissionDenied, ValidationError

from startup_updates.mcp import articles


class ArticleMCPBoundaryTests(SimpleTestCase):
    def setUp(self):
        self.company_id = str(uuid4())
        self.principal = SimpleNamespace(user=SimpleNamespace(pk=1), grant={"id": "grant-1", "company_id": self.company_id})
        self.company = SimpleNamespace(pk=self.company_id)
        self.generate = {"companyId": self.company_id, "topic": "Australian AI research", "expectedCostPoints": 6,
            "idempotencyKey": str(uuid4()), "websiteConnection": {"website_connection_id": str(uuid4()), "connection_generation": 2, "connection_target_id": "articles"}}

    def test_each_tool_declares_exact_scope_and_all_risk_annotations(self):
        for tool in articles.TOOLS:
            self.assertEqual(tool["securitySchemes"], [{"type": "oauth2", "scopes": [articles.TOOL_SCOPES[tool["name"]]]}])
            for key in ("readOnlyHint", "destructiveHint", "openWorldHint"):
                self.assertIsInstance(tool["annotations"][key], bool)
        publish = next(tool for tool in articles.TOOLS if tool["name"] == "publish_article")
        self.assertFalse(publish["annotations"]["readOnlyHint"])
        self.assertTrue(publish["annotations"]["destructiveHint"])
        self.assertTrue(publish["annotations"]["openWorldHint"])

    def test_legacy_or_revoked_grant_never_reaches_owned_company_or_operation(self):
        with patch.object(articles, "valid_grant", side_effect=PermissionDenied("scope missing")) as check, \
             patch.object(articles, "company_for") as company, patch.object(articles.operations, "start") as start:
            with self.assertRaises(PermissionDenied):
                articles.call_tool(self.principal, "generate_article", self.generate)
        check.assert_called_once_with("grant-1", scope="article:generate")
        company.assert_not_called()
        start.assert_not_called()

    def test_foreign_company_cannot_reach_any_service(self):
        with patch.object(articles, "valid_grant", return_value=self.principal), \
             patch.object(articles, "company_for", side_effect=PermissionDenied("foreign")), \
             patch.object(articles.operations, "draft") as draft:
            with self.assertRaises(PermissionDenied):
                articles.call_tool(self.principal, "get_article_draft", {"companyId": str(uuid4()), "runId": "run-1"})
        draft.assert_not_called()

    def test_generate_has_mandatory_price_identity_and_reviewed_website(self):
        for key in ("expectedCostPoints", "idempotencyKey", "websiteConnection"):
            value = deepcopy(self.generate)
            value.pop(key)
            with self.subTest(key=key), patch.object(articles, "valid_grant") as grant:
                with self.assertRaises(ValidationError):
                    articles.call_tool(self.principal, "generate_article", value)
                grant.assert_not_called()

    def test_invalid_scalar_and_nested_inputs_are_rejected_before_authority(self):
        cases = [{**self.generate, "expectedCostPoints": True}, {**self.generate, "expectedCostPoints": 6.5},
            {**self.generate, "expectedCostPoints": -1}, {**self.generate, "idempotencyKey": "retry-later"},
            {**self.generate, "topic": "x" * 501}, {**self.generate, "publish": True},
            {**self.generate, "websiteConnection": {"website_connection_id": str(uuid4()), "connection_generation": True}},
            {**self.generate, "websiteConnection": {**self.generate["websiteConnection"], "github_token": "synthetic-denied"}},
            {**self.generate, "editorialBrief": {"audience_id": "custom", "approved": True}}]
        for value in cases:
            with self.subTest(value=value), patch.object(articles, "valid_grant") as grant:
                with self.assertRaises(ValidationError):
                    articles.call_tool(self.principal, "generate_article", value)
                grant.assert_not_called()

    def test_research_selection_requires_matching_catalog_version(self):
        value = {"companyId": self.company_id, "requestedTopicCount": 4, "expectedCostPoints": 4,
                 "idempotencyKey": str(uuid4()), "preferredAudienceId": "audience-1"}
        with patch.object(articles, "valid_grant") as grant:
            with self.assertRaises(ValidationError):
                articles.call_tool(self.principal, "research_article_topics", value)
        grant.assert_not_called()

    def test_direct_edits_cannot_authorize_waivers_ai_images_or_publication(self):
        value = {"companyId": self.company_id, "runId": "run-1", "expectedRevision": "revision-1", "idempotencyKey": str(uuid4()),
                 "textEdits": [{"fieldId": "title", "originalValue": "Old", "value": "New"}]}
        for extra in ({"waivedComments": []}, {"action": "approve"}, {"commentIds": [str(uuid4())]}, {"regenerateImage": True}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                articles.call_tool(self.principal, "revise_article_draft", {**value, **extra})

    def test_start_passes_only_current_principal_and_owned_company_to_service(self):
        fresh = SimpleNamespace(user=SimpleNamespace(pk=2), grant=self.principal.grant)
        with patch.object(articles, "valid_grant", return_value=fresh), patch.object(articles, "company_for", return_value=self.company) as owner, \
             patch.object(articles.operations, "start", return_value={"runId": "run-2"}) as start:
            self.assertEqual(articles.call_tool(self.principal, "generate_article", self.generate), {"runId": "run-2"})
        owner.assert_called_once_with(fresh.user, self.company_id, fresh.grant)
        start.assert_called_once_with(fresh.user, self.company, self.generate, research=False)
