"""Reader context and scalar overview contracts for the existing MCP route."""
from datetime import date
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import ValidationError

from community_chat.tests import test_startup_mcp as fixtures
from startup_updates.mcp import tools


@override_settings(**fixtures.SETTINGS)
class StartupMcpReadabilityTests(SimpleTestCase):
    def test_brief_exposes_profile_context_and_instructions_without_private_notes(self):
        company = Obj(pk=fixtures.COMPANY_ID, name="Acme", organization=Obj(domain="acme.example"))
        profile = Obj(reporting_timezone="Australia/Melbourne", short_description="We help clinics plan care.",
            problem_solved="Too much paperwork.", target_audience="Clinic staff", notes="Private founder notes")
        with patch.object(tools.StartupProfile, "objects") as profiles, patch.object(tools.timezone, "now", return_value=parse_datetime("2026-10-25T00:00:00Z")):
            profiles.filter.return_value.first.return_value = profile
            brief = tools._brief(company, date(2026, 9, 1))
        self.assertEqual(brief["startup"]["description"], profile.short_description)
        self.assertEqual(brief["startup"]["problemSolved"], profile.problem_solved)
        self.assertEqual(brief["startup"]["targetAudience"], profile.target_audience)
        self.assertNotIn("notes", brief["startup"])
        self.assertIn("startupOverview", brief["sections"])
        instructions = " ".join(brief["instructions"])
        for concept in ("never heard", "conversational", "self-contained", "do not invent", "Leave it blank", "at most four", "do not repeat", "material uncertainty", "only in coverageNotes"):
            self.assertIn(concept, instructions)

    def test_overview_schema_and_validation_are_optional_bounded_strings(self):
        definition = next(item for item in tools.TOOLS if item["name"] == "save_narrative_draft")
        schema = definition["inputSchema"]["properties"]["narrative"]
        self.assertEqual(schema["properties"]["startupOverview"], {"type": "string", "maxLength": 2000})
        self.assertNotIn("startupOverview", schema["required"])
        helper = fixtures.NarrativeTests()
        for overview in ("We help clinics plan care.", ""):
            tools.validate_save(helper.arguments(narrative={"summary": "We welcomed a clinic.", "startupOverview": overview}))
        for overview in (None, [], {"private": "notes"}, 123, "x" * 2001):
            with self.subTest(overview=overview), self.assertRaises(ValidationError):
                tools.validate_save(helper.arguments(narrative={"summary": "We welcomed a clinic.", "startupOverview": overview}))

    def test_overview_is_saved_as_text_and_omission_preserves_existing_review_state(self):
        helper = fixtures.NarrativeTests()
        old = {"startupOverview": "We help clinics.", "financial_snapshot": {"income": 123}}
        snapshot = Obj(payload={"metrics": [{"key": "revenue", "value": "123"}]})
        validation = {"groundedness_status": "failed", "failed_claims": ["unresolved"]}
        draft = Obj(pk=10, month=date(2026, 9, 1), status="draft", current_revision_id=5,
            current_revision=Obj(pk=5, content_hash="revision", validation=validation,
                snapshot=snapshot, structured_memo=old),
            refresh_from_db=MagicMock(), title="title", save=MagicMock(), revisions=MagicMock())
        draft.revisions.filter.return_value.order_by.return_value.first.return_value = None
        for narrative, expected in (({"summary": "A busy month."}, "We help clinics."),
                ({"summary": "A busy month.", "startupOverview": "We help hospitals."}, "We help hospitals."),
                ({"summary": "A busy month.", "startupOverview": ""}, "")):
            with self.subTest(narrative=narrative):
                _, saved, captured = helper.save_with_mocks(draft, helper.arguments(expectedRevision=5, narrative=narrative))
                memo = saved.call_args.args[1]
                self.assertEqual(memo["startupOverview"], expected)
                self.assertIsInstance(memo["startupOverview"], str)
                self.assertEqual(memo["financial_snapshot"], old["financial_snapshot"])
                self.assertEqual(saved.call_args.kwargs["validation"], validation)
                self.assertEqual(saved.call_args.kwargs["audience"], "private")
                self.assertIs(saved.call_args.kwargs["snapshot"], snapshot)
                captured.assert_not_called()
        self.assertEqual(old, {"startupOverview": "We help clinics.", "financial_snapshot": {"income": 123}})
