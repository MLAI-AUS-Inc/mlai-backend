"""Actual startup draft/save control flow with no database or network imports.

Run with unittest, never manage.py test. Selected production AST bodies execute
against synthetic ownership, persistence and queue seams. These checks do not
prove authentication, SQL locking, actual worker results or model latency.
"""

import ast
from contextlib import nullcontext
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def load_bodies(path, names, namespace):
    tree = ast.parse(path.read_text())
    selected = [node for node in tree.body if getattr(node, "name", None) in names]
    for node in selected:
        node.decorator_list = []
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.Module(body=[future, *selected], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)


class StartupProfileDraftUnitTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(id="owner", is_staff=False, is_superuser=False)
        self.startup = SimpleNamespace(
            founder_names=["Saved founder"], target_audience="Saved audience",
            short_description="Saved description", problem_solved="Saved problem",
            stage="Seed", organization_kind="For-profit", notes="Saved notes",
            competitor_domains=["rival.example"], positive_keywords=["saved keyword"],
            company_aliases=["Acme"], domain_aliases=["acme.example"], save=Mock(),
        )
        self.org = SimpleNamespace(
            id="org", name="Acme", domain="acme.example", startup_profile=self.startup,
            competitors=["rival.example"], seed_keywords=["saved keyword"],
            company_linkedin_url="https://www.linkedin.com/company/acme", save=Mock(),
        )
        self.company = SimpleNamespace(
            id="company", name="Acme", domain="acme.example", location="Melbourne, Australia",
            abn=None, organization=self.org, organization_id="org", save=Mock(),
        )
        self.profile = SimpleNamespace(
            role="founder", ROLE_FOUNDER="founder", companies=Mock(), save=Mock(),
        )
        self.profile.companies.get.return_value = self.company
        self.config = SimpleNamespace(
            connected_slack_user_id="actor", brand_name="Acme", company_context="Saved context",
            pillar_strategy={}, github_repo="fixture/site", article_delivery_mode="content_only",
            default_timezone="Australia/Melbourne", daily_discovery_enabled=False, save=Mock(),
        )
        self.context = SimpleNamespace(company=self.company, organization=self.org, profile=self.profile)
        self.bind = Mock()
        modules = {}
        for name, attrs in {
            "content_factory.models": {
                "OrganizationContentConfig": SimpleNamespace(
                    objects=SimpleNamespace(get_or_create=Mock(return_value=(self.config, False))),
                ),
            },
            "startup_updates.services": {
                "resolve_or_create_profile": Mock(return_value=(self.org, self.startup)),
                "bind_user_to_startup": self.bind,
            },
        }.items():
            module = ModuleType(name)
            module.__dict__.update(attrs)
            modules[name] = module
        fake_modules = patch.dict(sys.modules, modules)
        fake_modules.start()
        self.addCleanup(fake_modules.stop)
        self.ns = {
            "ensure_company_organization": Mock(return_value=self.org),
            "founder_actor_id_for_user": lambda user: "actor",
            "actor_ids_for_user": lambda user: {"actor"},
            "_is_synthetic_actor_id": lambda actor: False,
            "normalize_company_linkedin_url": lambda value: str(value or "").strip(),
        }
        load_bodies(ROOT / "founder_tools/services.py", {
            "apply_shared_startup_details", "string_list_from_value", "_first_value",
            "_has_any_key", "_submitted_value", "_bool_from_value", "_normalize_organization_kind",
        }, self.ns)
        self.shared_details = Mock(wraps=self.ns["apply_shared_startup_details"])
        self.ns["apply_shared_startup_details"] = self.shared_details
        self.run = SimpleNamespace(
            run_id="run", status="queued", error="", result={}, domain="acme.example",
            workflow="startup_autofill", run_request={},
        )
        self.queue = Mock(return_value=self.run)
        self.gate = Mock(return_value=(None, 100))
        self.ns.update({
            "APIView": object,
            "Response": lambda data, status=200: SimpleNamespace(data=data, status_code=status),
            "status": SimpleNamespace(HTTP_400_BAD_REQUEST=400, HTTP_403_FORBIDDEN=403,
                                      HTTP_404_NOT_FOUND=404, HTTP_409_CONFLICT=409,
                                      HTTP_202_ACCEPTED=202),
            "transaction": SimpleNamespace(atomic=nullcontext),
            "get_or_create_founder_profile": Mock(return_value=self.profile),
            "normalize_company_domain": lambda value: str(value or "").strip(),
            "domain_is_available_to": Mock(return_value=True),
            "DomainOwnershipError": type("DomainOwnershipError", (Exception,), {}),
            "DuplicateCompanyDomainError": type("DuplicateCompanyDomainError", (Exception,), {}),
            "_require_roo_points_for_ai_agent": self.gate,
            "VibeRaisingCompany": SimpleNamespace(DoesNotExist=type("CompanyMissing", (Exception,), {})),
            "resolve_active_company": Mock(return_value=self.company),
            "assert_company_domain_available": Mock(),
            "apply_company_domain_change": Mock(),
            "set_unverified_company_abn": Mock(),
            "get_founder_company_context": Mock(return_value=self.context),
            "_get_config": Mock(return_value=self.config),
            "_active_startup_autofill_run_for_domain": Mock(return_value=None),
            "_camel_list": lambda value: list(value or []),
            "CONTENT_FACTORY_REQUEST_SOURCE": "founder_tools",
            "_mark_roo_points_gate_authorized": Mock(),
            "_queue_content_factory_run": self.queue,
            "logger": Mock(),
        })
        load_bodies(ROOT / "content_factory/vibe_marketing_views.py", {
            "VibeMarketingAutofillView", "_request_flag", "_company_id_from_request",
            "_autofill_startup_profile_payload", "_autofill_profile_fields_payload",
            "_autofill_draft_existing_fields",
            "_autofill_start_payload", "_autofill_start_response",
        }, self.ns)

    def request(self, **overrides):
        data = {
            "companyId": "company", "company_name": "Acme", "domain": "acme.example",
            "location": "Melbourne, Australia", "draftMode": True,
        }
        data.update(overrides)
        return self.ns["VibeMarketingAutofillView"]().post(
            SimpleNamespace(user=self.user, data=data, query_params={})
        )

    def test_basics_only_draft_preserves_saved_hidden_and_lower_fields(self):
        response = self.request()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["researchCompanyId"], "company")
        self.assertEqual(response.data["companyId"], "company")
        payload = self.queue.call_args.kwargs["payload"]
        self.assertTrue(payload["draft_mode"])
        self.assertFalse(payload["persist"])
        self.assertEqual(payload["startup_profile"]["short_description"], "Saved description")
        self.assertEqual(payload["startup_profile"]["founder_names"], ["Saved founder"])
        self.assertEqual(payload["startup_profile"]["target_audience"], "Saved audience")
        self.assertEqual(payload["existing_fields"]["companyContext"], "Saved context")
        self.assertEqual(payload["existing_fields"]["seedKeywords"], ["saved keyword"])
        self.startup.save.assert_not_called()
        self.config.save.assert_not_called()
        self.org.save.assert_not_called()
        self.shared_details.assert_not_called()
        self.bind.assert_not_called()

    def assert_saved_answers_unchanged(self):
        self.assertEqual(self.startup.short_description, "Saved description")
        self.assertEqual(self.startup.problem_solved, "Saved problem")
        self.assertEqual(self.startup.stage, "Seed")
        self.assertEqual(self.startup.organization_kind, "For-profit")
        self.assertEqual(self.startup.notes, "Saved notes")
        self.assertEqual(self.startup.founder_names, ["Saved founder"])
        self.assertEqual(self.startup.target_audience, "Saved audience")
        self.assertEqual(self.startup.competitor_domains, ["rival.example"])
        self.assertEqual(self.startup.positive_keywords, ["saved keyword"])
        self.assertEqual(self.org.competitors, ["rival.example"])
        self.assertEqual(self.org.seed_keywords, ["saved keyword"])
        self.assertEqual(self.org.company_linkedin_url, "https://www.linkedin.com/company/acme")
        self.assertEqual(self.config.brand_name, "Acme")
        self.assertEqual(self.config.company_context, "Saved context")
        self.assertEqual(self.config.github_repo, "fixture/site")
        self.assertFalse(self.config.daily_discovery_enabled)
        self.shared_details.assert_not_called()
        self.startup.save.assert_not_called()
        self.org.save.assert_not_called()
        self.config.save.assert_not_called()
        self.bind.assert_not_called()

    def test_nonempty_local_draft_answers_are_context_only(self):
        response = self.request(
            shortDescription=" Unsaved description ", problemSolved="Unsaved problem",
            stage="Bootstrapped", organizationKind="Not-for-profit", notes="Local notes",
            brandName="Draft brand", companyContext="Draft context",
            companyLinkedInUrl="https://www.linkedin.com/company/draft-acme",
            competitors=["new-rival.example"], seedKeywords=[" draft keyword "],
            hasRevenue=False, abn="94 807 394 137", acn="123456789",
            founderNames=[], targetAudience="Unsubmitted audience",
            githubRepo="draft/repository", dailyDiscoveryEnabled=True,
        )
        self.assertEqual(response.status_code, 202)
        self.assert_saved_answers_unchanged()
        self.ns["set_unverified_company_abn"].assert_not_called()
        self.assertIsNone(self.company.abn)
        payload = self.queue.call_args.kwargs["payload"]
        context = payload["existing_fields"]
        self.assertEqual(context["companyContext"], "Draft context")
        self.assertEqual(context["brandName"], "Draft brand")
        self.assertEqual(context["competitors"], ["new-rival.example"])
        self.assertEqual(context["seedKeywords"], ["draft keyword"])
        self.assertEqual(context["profileFields"]["shortDescription"], "Unsaved description")
        self.assertEqual(context["profileFields"]["problemSolved"], "Unsaved problem")
        self.assertEqual(context["profileFields"]["stage"], "Bootstrapped")
        self.assertEqual(context["profileFields"]["organizationKind"], "Not-for-profit")
        self.assertEqual(context["profileFields"]["hasRevenue"], "no")
        self.assertEqual(context["profileFields"]["notes"], "Local notes")
        self.assertEqual(context["profileFields"]["acn"], "123456789")
        self.assertEqual(context["profileFields"]["founderNames"], ["Saved founder"])
        self.assertEqual(context["profileFields"]["targetAudience"], "Saved audience")
        self.assertEqual(payload["startup_profile"]["short_description"], "Saved description")
        self.assertEqual(payload["brand_name"], "Draft brand")
        self.assertEqual(payload["company_linkedin_url"], context["companyLinkedInUrl"])
        self.assertEqual(payload["abn"], "94 807 394 137")
        self.assertNotIn("githubRepo", context)
        self.assertNotIn("dailyDiscoveryEnabled", context)

    def test_explicit_empty_local_drafts_never_clear_saved_answers(self):
        response = self.request(
            shortDescription="", stage="", organizationKind="", companyLinkedInUrl="",
            companyContext="", competitors=[], seedKeywords=[], abn="",
            existingFields={"profileFields": {"founderNames": [], "targetAudience": ""}},
        )
        self.assertEqual(response.status_code, 202)
        self.assert_saved_answers_unchanged()
        context = self.queue.call_args.kwargs["payload"]["existing_fields"]
        self.assertEqual(context["profileFields"]["shortDescription"], "")
        self.assertEqual(context["profileFields"]["stage"], "")
        self.assertEqual(context["profileFields"]["organizationKind"], "")
        self.assertEqual(context["companyContext"], "")
        self.assertEqual(context["companyLinkedInUrl"], "")
        self.assertEqual(context["competitors"], [])
        self.assertEqual(context["seedKeywords"], [])
        self.assertEqual(context["profileFields"]["founderNames"], ["Saved founder"])
        self.assertEqual(context["profileFields"]["targetAudience"], "Saved audience")

    def test_nested_camel_draft_fields_are_forwarded_without_writes(self):
        response = self.request(existingFields={
            "companyLinkedInUrl": "https://www.linkedin.com/company/draft",
            "competitors": ["nested-rival.example"], "seedKeywords": ["nested keyword"],
            "profileFields": {"shortDescription": "Nested draft", "stage": "Idea", "abn": "draft-abn"},
        })
        self.assertEqual(response.status_code, 202)
        self.assert_saved_answers_unchanged()
        payload = self.queue.call_args.kwargs["payload"]
        context = payload["existing_fields"]
        self.assertEqual(context["profileFields"]["shortDescription"], "Nested draft")
        self.assertEqual(context["profileFields"]["stage"], "Idea")
        self.assertEqual(context["competitors"], ["nested-rival.example"])
        self.assertEqual(context["seedKeywords"], ["nested keyword"])
        self.assertEqual(payload["company_linkedin_url"], "https://www.linkedin.com/company/draft")
        self.assertEqual(payload["abn"], "draft-abn")

    def test_snake_aliases_and_explicit_top_level_answers_take_precedence(self):
        response = self.request(
            short_description="Top-level draft", stage="", has_revenue=False,
            company_linkedin_url="https://www.linkedin.com/company/local",
            seed_keywords="local keyword, another keyword", competitor_domains=["local.example"],
            existing_fields={
                "company_context": "Snake context", "brand_name": "Snake brand",
                "profile_fields": {
                    "short_description": "Nested value", "organization_kind": "Not-for-profit",
                    "stage": "Seed", "founder_names": [], "target_audience": "Other audience",
                },
                "seed_keywords": ["nested keyword"],
            },
        )
        self.assertEqual(response.status_code, 202)
        self.assert_saved_answers_unchanged()
        context = self.queue.call_args.kwargs["payload"]["existing_fields"]
        self.assertEqual(context["companyContext"], "Snake context")
        self.assertEqual(context["brandName"], "Snake brand")
        self.assertEqual(context["profileFields"]["shortDescription"], "Top-level draft")
        self.assertEqual(context["profileFields"]["stage"], "")
        self.assertEqual(context["profileFields"]["hasRevenue"], "no")
        self.assertEqual(context["profileFields"]["organizationKind"], "Not-for-profit")
        self.assertEqual(context["competitors"], ["local.example"])
        self.assertEqual(context["seedKeywords"], ["local keyword", "another keyword"])

    def test_null_or_malformed_context_values_cannot_replace_saved_context(self):
        response = self.request(
            shortDescription=None, companyContext={"not": "text"}, seedKeywords={"not": "a list"},
            existingFields={"competitors": [None, {"domain": "not-a-string"}], "profileFields": "invalid"},
        )
        self.assertEqual(response.status_code, 202)
        self.assert_saved_answers_unchanged()
        context = self.queue.call_args.kwargs["payload"]["existing_fields"]
        self.assertEqual(context["companyContext"], "Saved context")
        self.assertEqual(context["profileFields"]["shortDescription"], "Saved description")
        self.assertEqual(context["competitors"], ["rival.example"])
        self.assertEqual(context["seedKeywords"], ["saved keyword"])

    def test_new_company_research_creates_only_the_basic_identity_record(self):
        created = SimpleNamespace(
            id="new-company", name="Acme", domain="acme.example",
            location="Melbourne, Australia", abn=None, organization=self.org,
        )
        create = Mock(return_value=created)
        self.ns["VibeRaisingCompany"].objects = SimpleNamespace(create=create)
        self.context.company = created
        response = self.request(
            companyId="", createNew=True, abn="94 807 394 137", shortDescription="Local draft",
            existingFields={"seedKeywords": ["local keyword"]},
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["researchCompanyId"], "new-company")
        self.assertEqual(create.call_args.kwargs["name"], "Acme")
        self.assertEqual(create.call_args.kwargs["domain"], "acme.example")
        self.assertEqual(create.call_args.kwargs["location"], "Melbourne, Australia")
        self.assertIsNone(create.call_args.kwargs["abn"])
        self.assertNotIn("short_description", create.call_args.kwargs)
        self.assert_saved_answers_unchanged()
        context = self.queue.call_args.kwargs["payload"]["existing_fields"]
        self.assertEqual(context["profileFields"]["shortDescription"], "Local draft")
        self.assertEqual(context["seedKeywords"], ["local keyword"])
        self.assertEqual(context["profileFields"]["abn"], "94 807 394 137")

    def test_invalid_draft_linkedin_stops_before_dispatch_without_profile_writes(self):
        self.ns["normalize_company_linkedin_url"] = Mock(side_effect=ValueError("Invalid company LinkedIn URL"))
        response = self.request(companyLinkedInUrl="invalid", shortDescription="Local draft")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["field"], "companyLinkedInUrl")
        self.queue.assert_not_called()
        self.assert_saved_answers_unchanged()

    def test_draft_context_merge_does_not_mutate_saved_or_submitted_dictionaries(self):
        saved = {"seedKeywords": ["saved"], "profileFields": {"shortDescription": "Saved", "founderNames": ["Founder"]}}
        submitted = {"existingFields": {"seedKeywords": [" local "], "profileFields": {"shortDescription": "Local"}}}
        context = self.ns["_autofill_draft_existing_fields"](saved, submitted)
        self.assertEqual(context["seedKeywords"], ["local"])
        self.assertEqual(saved["seedKeywords"], ["saved"])
        self.assertEqual(saved["profileFields"]["shortDescription"], "Saved")
        self.assertEqual(submitted["existingFields"]["seedKeywords"], [" local "])
        self.assertEqual(context["profileFields"]["founderNames"], ["Founder"])

    def test_draft_mode_alias_is_forwarded(self):
        response = self.request(draftMode=False, draft_mode=True)
        self.assertEqual(response.status_code, 202)
        self.assertTrue(self.queue.call_args.kwargs["payload"]["draft_mode"])

    def test_legacy_mode_keeps_deep_research_and_optional_location(self):
        response = self.request(draftMode=False, location="")
        self.assertEqual(response.status_code, 202)
        payload = self.queue.call_args.kwargs["payload"]
        self.assertNotIn("draft_mode", payload)
        self.assertEqual(payload["research_depth"], "deep")
        self.assertTrue(payload["strict_deep_research"])
        self.assertFalse(payload["persist"])

    def test_legacy_mode_still_persists_submitted_shared_details(self):
        response = self.request(
            draftMode=False, shortDescription="Saved through legacy research",
            stage="Idea", seedKeywords=["legacy keyword"], competitors=["legacy-rival.example"],
            companyContext="Legacy context", companyLinkedInUrl="https://www.linkedin.com/company/legacy",
        )
        self.assertEqual(response.status_code, 202)
        self.shared_details.assert_called_once()
        self.assertEqual(self.startup.short_description, "Saved through legacy research")
        self.assertEqual(self.startup.stage, "Idea")
        self.assertEqual(self.org.seed_keywords, ["legacy keyword"])
        self.assertEqual(self.org.competitors, ["legacy-rival.example"])
        self.assertEqual(self.config.company_context, "Legacy context")
        self.startup.save.assert_called_once()
        self.config.save.assert_called_once()
        self.org.save.assert_called_once()
        self.assertEqual(self.startup.founder_names, ["Saved founder"])
        self.assertEqual(self.startup.target_audience, "Saved audience")

    def test_legacy_mode_still_applies_submitted_abn(self):
        response = self.request(draftMode=False, abn="94 807 394 137")
        self.assertEqual(response.status_code, 202)
        self.ns["set_unverified_company_abn"].assert_called_once_with(self.company, "94 807 394 137")

    def test_missing_draft_location_stops_before_billing_or_persistence(self):
        response = self.request(location="   ")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["field"], "location")
        self.gate.assert_not_called()
        self.queue.assert_not_called()
        self.company.save.assert_not_called()

    def test_reused_run_retains_exact_researched_company_scope(self):
        self.ns["_active_startup_autofill_run_for_domain"].return_value = self.run
        response = self.request()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["researchCompanyId"], "company")
        self.queue.assert_not_called()

    def test_start_response_can_recover_company_scope_from_saved_request(self):
        self.run.run_request = {"company_id": "saved-company"}
        response = self.ns["_autofill_start_response"](self.run)
        self.assertEqual(response.data["companyId"], "saved-company")
        self.assertEqual(response.data["researchCompanyId"], "saved-company")

    def test_missing_company_name_or_domain_stops_before_dispatch(self):
        for field in ("company_name", "domain"):
            with self.subTest(field=field):
                response = self.request(**{field: ""})
                self.assertEqual(response.status_code, 400)
                self.queue.assert_not_called()

    def test_visible_profile_edits_do_not_clear_removed_fields(self):
        self.ns["apply_shared_startup_details"](user=self.user, company=self.company, data={
            "shortDescription": "New description", "problemSolved": "New problem",
            "seedKeywords": ["new keyword"], "competitors": ["new-rival.example"],
        })
        self.assertEqual(self.startup.short_description, "New description")
        self.assertEqual(self.startup.problem_solved, "New problem")
        self.assertEqual(self.startup.positive_keywords, ["new keyword"])
        self.assertEqual(self.org.seed_keywords, ["new keyword"])
        self.assertEqual(self.startup.founder_names, ["Saved founder"])
        self.assertEqual(self.startup.target_audience, "Saved audience")
        self.assertNotIn("founder_names", self.startup.save.call_args.kwargs["update_fields"])
        self.assertNotIn("target_audience", self.startup.save.call_args.kwargs["update_fields"])

    def test_explicit_empty_visible_field_still_clears(self):
        self.ns["apply_shared_startup_details"](user=self.user, company=self.company, data={
            "shortDescription": "", "seedKeywords": [],
        })
        self.assertEqual(self.startup.short_description, "")
        self.assertEqual(self.startup.positive_keywords, [])
        self.assertEqual(self.startup.founder_names, ["Saved founder"])
        self.assertEqual(self.startup.target_audience, "Saved audience")


if __name__ == "__main__":
    unittest.main()
