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
