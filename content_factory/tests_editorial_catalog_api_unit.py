"""Real DRF view/control-flow tests with in-memory persistence seams.

Run with unittest, NOT manage.py test. No application settings, .env, Django
model setup, database connection or migration is used. These tests do not prove
real SQL locking or JWT validation; those need approved integration testing.
"""
import ast
from contextlib import contextmanager
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from django.conf import settings

if not settings.configured:
    settings.configure(SECRET_KEY="unit-fixture-only", USE_TZ=True, USE_I18N=False,
                       DATABASES={"default": {"ENGINE": "django.db.backends.dummy"}},
                       REST_FRAMEWORK={"UNAUTHENTICATED_USER": None, "DEFAULT_AUTHENTICATION_CLASSES": []})

from rest_framework import status
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from .editorial_catalog import EDIT_FIELDS
from .tests_editorial_catalog_unit import approved_catalog, draft_catalog, edit_payload, approval_payload


class EditorialCatalogAPIUnitTests(unittest.TestCase):
    def setUp(self):
        self.state = draft_catalog()
        self.calls = []
        self.user = SimpleNamespace(pk="owner-1", is_authenticated=True)
        self.org = SimpleNamespace(pk="org-1", domain="example.com")
        self.profile = SimpleNamespace(pk="profile-1", user_id="owner-1", role="founder")
        self.company = SimpleNamespace(pk="company-1", profile_id="profile-1", organization_id="org-1")
        self.before_transaction = None
        self.in_transaction = False
        class OrgMissing(Exception):
            pass
        class CompanyMissing(Exception):
            pass
        class ProfileMissing(Exception):
            pass
        self.missing = CompanyMissing
        self.org_model = SimpleNamespace(DoesNotExist=OrgMissing, objects=Mock())
        self.config_model = SimpleNamespace(objects=Mock())
        self.company_model = SimpleNamespace(DoesNotExist=CompanyMissing, objects=Mock())
        self.profile_model = SimpleNamespace(DoesNotExist=ProfileMissing, ROLE_FOUNDER="founder", objects=Mock())
        self.resolve = Mock(side_effect=self.resolve_company)
        modules = {}
        for name, attrs in {
            "founder_tools.models": {"VibeRaisingCompany": self.company_model, "VibeRaisingProfile": self.profile_model},
            "founder_tools.services": {"get_founder_company_context": self.resolve},
            "organizations.models": {"Organization": self.org_model},
            "content_factory.models": {"OrganizationContentConfig": self.config_model},
        }.items():
            module = ModuleType(name)
            module.__dict__.update(attrs)
            modules[name] = module
        self.modules = patch.dict(sys.modules, modules)
        self.modules.start()
        self.addCleanup(self.modules.stop)
        spec = importlib.util.spec_from_file_location("content_factory._catalog_views_under_test", Path(__file__).with_name("editorial_views.py"))
        self.views = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.views)
        self.org_model.objects.select_for_update.return_value.get.side_effect = self.lock_org
        self.profile_model.objects.select_for_update.return_value.get.side_effect = self.lock_profile
        self.company_model.objects.select_for_update.return_value.get.side_effect = self.lock_company
        self.config_model.objects.filter.return_value.first.side_effect = self.read_config
        self.config_model.objects.update_or_create.side_effect = self.save_config
        atomic = patch.object(self.views.transaction, "atomic", self.atomic)
        atomic.start()
        self.addCleanup(atomic.stop)
        self.factory = APIRequestFactory()

    @contextmanager
    def atomic(self):
        self.calls.append("transaction")
        # A deterministic state change between the initial scoped lookup and
        # transaction entry. This is not a concurrent SQL/rollback simulation.
        if self.before_transaction is not None:
            change, self.before_transaction = self.before_transaction, None
            change()
        self.assertFalse(self.in_transaction)
        self.in_transaction = True
        try:
            yield
        finally:
            self.in_transaction = False

    def resolve_company(self, user, *, company_id):
        if company_id != "company-1" or user.pk != "owner-1":
            raise self.missing()
        return deepcopy(SimpleNamespace(organization=self.org, profile=self.profile, company=self.company))

    def lock_org(self, **lookup):
        self.assertTrue(self.in_transaction)
        self.calls.append(("lock", lookup))
        if self.org is None or any(getattr(self.org, key) != value for key, value in lookup.items()):
            raise self.org_model.DoesNotExist()
        return self.org

    def lock_profile(self, **lookup):
        self.assertTrue(self.in_transaction)
        self.calls.append(("lock_profile", lookup))
        if self.profile is None or any(getattr(self.profile, key) != value for key, value in lookup.items()):
            raise self.profile_model.DoesNotExist()
        return self.profile

    def lock_company(self, **lookup):
        self.assertTrue(self.in_transaction)
        self.calls.append(("lock_company", lookup))
        if self.company is None or any(getattr(self.company, key) != value for key, value in lookup.items()):
            raise self.company_model.DoesNotExist()
        return self.company

    def read_config(self):
        self.calls.append("read")
        return SimpleNamespace(pillar_strategy=deepcopy(self.state))

    def save_config(self, *, organization, defaults):
        self.assertTrue(self.in_transaction)
        self.calls.append("write")
        self.assertIs(organization, self.org)
        self.assertEqual(set(defaults), {"pillar_strategy"})
        self.state = deepcopy(defaults["pillar_strategy"])
        return SimpleNamespace(pillar_strategy=self.state), False

    def request(self, method, payload=None, *, company="company-1", authenticated=True, approving=False):
        url = "/editorial-catalog/" + ("approve/" if approving else "")
        if company is not None:
            url += "?company_id=" + company
        request = getattr(self.factory, method)(url, data=payload, format="json")
        if authenticated:
            force_authenticate(request, user=self.user)
        view = self.views.EditorialCatalogApprovalView if approving else self.views.EditorialCatalogView
        return view.as_view()(request)

    def test_authenticated_review_and_approval_derive_server_identity(self):
        review = self.request("get")
        self.assertEqual(review.status_code, 200)
        self.assertEqual(self.calls, ["read"])
        self.calls.clear()
        result = self.request("post", {"expected_editorial_catalog_version": review.data["editorial_catalog_version"], "entries": review.data["review_entries"]}, approving=True)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.calls, [
            "transaction", ("lock", {"pk": "org-1"}),
            ("lock_profile", {"pk": "profile-1", "user_id": "owner-1"}),
            ("lock_company", {"pk": "company-1", "profile_id": "profile-1"}),
            "read", "write",
        ])
        self.assertEqual(result.data["cta_options"][0]["approved_by"], "user:owner-1")
        self.resolve.assert_called_with(self.user, company_id="company-1")

    def test_anonymous_missing_foreign_and_ambiguous_company_requests_cannot_write(self):
        for kwargs, expected in (({"authenticated": False}, 403), ({"company": None}, 400), ({"company": "company-2"}, 404)):
            with self.subTest(kwargs=kwargs):
                response = self.request("post", approval_payload(self.state), approving=True, **kwargs)
                self.assertEqual(response.status_code, expected)
        response = self.request("put", {**edit_payload(self.state), "company_id": "company-2"})
        self.assertEqual(response.status_code, 400)
        self.config_model.objects.update_or_create.assert_not_called()

    def test_non_founder_cannot_approve(self):
        self.resolve.side_effect = PermissionError
        self.assertEqual(self.request("post", approval_payload(self.state), approving=True).status_code, 403)
        self.org_model.objects.select_for_update.assert_not_called()

    def test_two_editors_stale_save_and_stale_approval_fail_after_locked_reread(self):
        stale_approval = approval_payload(self.state)
        edit = edit_payload(self.state)
        edit["audience_options"][0].update(version=2, reader_task="Different task")
        self.assertEqual(self.request("put", edit).status_code, 200)
        current = deepcopy(self.state)
        self.config_model.objects.update_or_create.reset_mock()
        self.assertEqual(self.request("put", edit).status_code, 409)
        self.assertEqual(self.request("post", stale_approval, approving=True).status_code, 409)
        self.assertEqual(self.state, current)
        self.config_model.objects.update_or_create.assert_not_called()

    def test_forged_approval_and_incorrect_hash_never_persist(self):
        payload = approval_payload(self.state)
        self.assertEqual(self.request("post", {**payload, "approved_by": "spoof"}, approving=True).status_code, 400)
        payload["entries"][0]["content_sha256"] = "0" * 64
        self.assertEqual(self.request("post", payload, approving=True).status_code, 409)
        self.config_model.objects.update_or_create.assert_not_called()

    def test_service_can_edit_drafts_but_not_approve_or_create_unknown_organisations(self):
        edit = edit_payload(self.state)
        edit["cta_options"][0].update(version=2, body="Changed promise")
        response = self.views.service_catalog_update("example.com", {"domain": "example.com", **edit})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["cta_options"][0]["status"], "draft")
        self.config_model.objects.update_or_create.reset_mock()
        self.assertEqual(self.views.service_catalog_update("unknown.example", {"domain": "unknown.example", **edit_payload(self.state)}).status_code, 404)
        bad = edit_payload(self.state)
        bad["cta_options"][0].update(status="approved", version=3, approved_by="service", approved_at="2026-09-10")
        self.assertEqual(self.views.service_catalog_update("example.com", bad).status_code, 400)
        self.config_model.objects.update_or_create.assert_not_called()
        self.org_model.objects.get_or_create.assert_not_called()
        self.profile_model.objects.select_for_update.assert_not_called()
        self.company_model.objects.select_for_update.assert_not_called()

    def assert_changed_authority_denied(self, change, expected_status):
        original = deepcopy((self.profile, self.company, self.org))
        for method, approving in (("put", False), ("post", True)):
            with self.subTest(method=method):
                self.profile, self.company, self.org = deepcopy(original)
                self.calls.clear()
                self.config_model.objects.update_or_create.reset_mock()
                current = deepcopy(self.state)
                payload = approval_payload(self.state) if approving else edit_payload(self.state)
                if not approving:
                    payload["cta_options"][0].update(version=2, body="Changed promise")
                self.before_transaction = change
                result = self.request(method, payload, approving=approving)
                self.assertEqual(result.status_code, expected_status)
                self.assertFalse(self.in_transaction)
                self.assertNotIn("read", self.calls)
                self.config_model.objects.update_or_create.assert_not_called()
                self.assertEqual(self.state, current)

    def test_company_transfer_after_initial_scope_cannot_save_or_approve(self):
        self.assert_changed_authority_denied(lambda: setattr(self.company, "profile_id", "other-profile"), 404)

    def test_founder_role_revoked_after_initial_scope_cannot_save_or_approve(self):
        self.assert_changed_authority_denied(lambda: setattr(self.profile, "role", "investor"), 403)

    def test_profile_account_changed_after_initial_scope_cannot_save_or_approve(self):
        self.assert_changed_authority_denied(lambda: setattr(self.profile, "user_id", "other-owner"), 403)

    def test_company_relinked_or_unlinked_after_scope_cannot_change_old_catalog(self):
        for organization_id in ("org-2", None):
            with self.subTest(organization_id=organization_id):
                self.company.organization_id = "org-1"
                self.assert_changed_authority_denied(lambda: setattr(self.company, "organization_id", organization_id), 409)

    def test_deleted_ownership_records_after_scope_cannot_save_or_approve(self):
        for field, expected_status in (("profile", 403), ("company", 404), ("org", 404)):
            with self.subTest(field=field):
                original = deepcopy(getattr(self, field))
                self.assert_changed_authority_denied(lambda: setattr(self, field, None), expected_status)
                setattr(self, field, original)

    def test_company_domain_changed_after_scope_requires_reload(self):
        self.assert_changed_authority_denied(lambda: setattr(self.org, "domain", "changed.example"), 409)

    def test_approval_helper_requires_an_owner_even_with_exact_reviewed_content(self):
        response = self.views.mutate_catalog_response({"domain": "example.com"}, approval_payload(self.state), approving=True)
        self.assertEqual(response.status_code, 400)
        self.config_model.objects.update_or_create.assert_not_called()

    def test_incomplete_internal_owner_context_is_rejected_before_policy_io(self):
        for identity in ({"owner_context": self.resolve_company(self.user, company_id="company-1")},
                         {"owner_user_id": "owner-1"}):
            with self.subTest(identity=list(identity)):
                response = self.views.mutate_catalog_response({"domain": "example.com"}, edit_payload(self.state), **identity)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.calls, [])
        self.config_model.objects.update_or_create.assert_not_called()

    def test_an_approved_noop_still_requires_current_owner_authority(self):
        self.state = approved_catalog()
        for method, approving in (("put", False), ("post", True)):
            with self.subTest(method=method):
                self.profile.role = "founder"
                self.calls.clear()
                current = deepcopy(self.state)
                payload = approval_payload(current) if approving else edit_payload(current)
                self.before_transaction = lambda: setattr(self.profile, "role", "investor")
                self.assertEqual(self.request(method, payload, approving=approving).status_code, 403)
                self.assertNotIn("read", self.calls)
                self.assertEqual(self.state, current)
        self.config_model.objects.update_or_create.assert_not_called()

    def test_body_company_selection_uses_same_locked_owner_checks(self):
        review = self.request("get")
        self.calls.clear()
        response = self.request("post", {
            "company_id": "company-1", "expected_editorial_catalog_version": review.data["editorial_catalog_version"],
            "entries": review.data["review_entries"],
        }, company=None, approving=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(("lock_company", {"pk": "company-1", "profile_id": "profile-1"}), self.calls)
        self.assertEqual(response.data["cta_options"][0]["approved_by"], "user:owner-1")

    def test_service_put_routes_catalog_before_any_general_metadata_write(self):
        # Execute the actual PUT function without importing its unrelated model,
        # billing and network dependencies. This is not a substitute for an
        # approved full Django integration test of permissions or transactions.
        source = Path(__file__).with_name("service_views.py").read_text()
        tree = ast.parse(source)
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ContentFactoryOrgConfigView")
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "put")
        namespace = {"sanitize_json_for_postgres": lambda value: value, "EDIT_FIELDS": EDIT_FIELDS,
                     "service_catalog_update": self.views.service_catalog_update, "Response": Response,
                     "status": status, "Organization": self.org_model}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "service_views.py", "exec"), namespace)
        owner = SimpleNamespace(_normalize_domain=lambda value: value)
        for extra in ({"name": "Must not be saved"}, {"pillar_strategy": {"editorial_catalog": {}}}, {}):
            payload = {"domain": "example.com", "audience_options": [], **extra}
            self.assertEqual(namespace["put"](owner, SimpleNamespace(data=payload)).status_code, 400)
        self.config_model.objects.update_or_create.assert_not_called()
        self.org_model.objects.get_or_create.assert_not_called()

    def test_exact_noop_does_not_write_and_approval_route_cannot_edit(self):
        self.state = approved_catalog()
        self.assertEqual(self.request("put", edit_payload(self.state)).status_code, 200)
        self.assertEqual(self.request("post", approval_payload(self.state), approving=True).status_code, 200)
        self.config_model.objects.update_or_create.assert_not_called()
        self.assertEqual(self.request("put", edit_payload(self.state), approving=True).status_code, 405)

    def test_research_suggestions_reload_without_creating_a_live_catalog(self):
        self.state={}
        envelope={'schemaVersion':1,'domain':'example.com','researchRunId':'scan-1','profiles':[{'id':'p1','name':'Owners'}]}
        run=SimpleNamespace(run_id='scan-1',run_request={'editorial_catalog_version':0},result={'autofill':{'editorialSuggestions':envelope}})
        model=SimpleNamespace(objects=Mock())
        model.objects.filter.return_value.order_by.return_value=[run]
        module=ModuleType('workflow_runs.models');module.ContentFactoryRun=model
        with patch.dict(sys.modules,{'workflow_runs.models':module}):
            request=self.factory.get('/editorial-catalog/?company_id=company-1&include_suggestions=1')
            force_authenticate(request,user=self.user)
            result=self.views.EditorialCatalogView.as_view()(request)
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.data['research_suggestions']['researchRunId'],'scan-1')
        self.assertEqual(result.data['research_suggestions']['sourceCatalogVersion'],0)
        self.assertEqual(result.data['audience_options'],[])
        self.assertEqual(self.state,{})
        model.objects.filter.assert_called_once_with(organization=self.org,domain='example.com',workflow='startup_autofill')
        self.config_model.objects.update_or_create.assert_not_called()

    def test_suggestion_acceptance_retains_evidence_outside_approval_hashes(self):
        envelope={'domain':'example.com','researchRunId':'scan-1','profiles':[{'id':'p1','name':'Owners','rationale':'Website evidence','source_urls':['https://example.com/']} ]}
        run=SimpleNamespace(run_id='scan-1',run_request={'editorial_catalog_version':0},result={'autofill':{'editorialSuggestions':envelope}})
        model=SimpleNamespace(objects=Mock());model.objects.filter.return_value.first.return_value=run
        module=ModuleType('workflow_runs.models');module.ContentFactoryRun=model
        edit=edit_payload(self.state);entry=edit['audience_options'][0]
        entry.update(version=entry['version']+1,reader_task='A reviewed task')
        edit['suggestion_reference']={'research_run_id':'scan-1','suggestion_id':'p1','kind':'audience','entry_id':entry['id'],'entry_version':entry['version']}
        with patch.dict(sys.modules,{'workflow_runs.models':module}):result=self.request('put',edit)
        self.assertEqual(result.status_code,200,result.data)
        self.assertEqual(result.data['audience_options'][0]['status'],'draft')
        self.assertNotIn('suggestion',result.data['audience_options'][0])
        record=self.state['editorial_suggestion_reviews'][0]
        self.assertEqual(record['source_catalog_version'],0)
        self.assertEqual(record['suggestion']['rationale'],'Website evidence')
        self.assertEqual(record['reviewed_by'],'user:owner-1')
