"""B12: "Reconnect" means lost GitHub access, nothing else.

``website_connection_required`` is split into ``github_access_required`` (access
lost: the only Reconnect), ``website_not_connected`` and the protocol fault
``callback_contract_missing``. The table test pins the code of every raise site
that used to say ``website_connection_required``; the behaviour tests cover the
saved-run decision, the legacy alias and the authority need.
"""

import ast
from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.parsers import JSONParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from workflow_runs.models import ContentFactoryRun
from . import registry_policy
from .tests_website_connections import WebsiteDatabaseFixture
from .website_connection_codes import (
    ACCESS_LOST, CONTRACT_MISSING, LEGACY_CODE, NOT_CONNECTED, PAUSED, for_connection_state, legacy_code,
    missing_contract_error,
)
from .website_connections import guarded_service_write, needs_repository_authority
from .website_contract import WebsiteAuthorityError

ROOT = Path(__file__).resolve().parent

# Every raise site that said website_connection_required before B12, by
# (file, enclosing function) -> the code (or helper) it raises now.
RAISE_SITES = {
    ("incident_guards.py", "publish_child_binding"): {"not_connected"},
    ("website_support.py", "owner_support_operation"): {"not_connected"},
    ("vibe_marketing_views.py", "_reserve_article_start_connection"): {"_not_connected"},
    ("vibe_marketing_views.py", "_github_app_token_payload_for_domain"): {"_not_connected"},
    ("vibe_marketing_views.py", "_queue_content_factory_run"): {"_not_connected"},
    # An old web app that sends no reviewed identity: the legacy alias stays.
    ("vibe_marketing_views.py", "_start_website_preview_article"): {LEGACY_CODE},
    # A run without saved consent on a connected site: not a disconnect.
    ("website_connections.py", "setup_merge_observation_guard"): {"invalid_connection_contract"},
    ("website_connections.py", "authority_guard"): {"missing_contract_error"},
    ("website_connections.py", "bind_website"): {LEGACY_CODE},
    ("website_connections.py", "transition_connection"): {"not_connected", LEGACY_CODE},
    ("website_connections.py", "dispatch_contract"): {"not_connected"},
    ("website_cleanup_verification.py", "verify_cleanup_deployment"): {"not_connected"},
    # Content Factory asked for admission without the identity of a connected site.
    ("service_views.py", "_worker_article_admission_response"): {CONTRACT_MISSING},
    ("website_views.py", "wrapped"): {"not_connected", LEGACY_CODE},
    ("website_views.py", "post"): {"not_connected"},
    ("website_contract.py", "validate_authority"): {LEGACY_CODE},
    ("website_verification.py", "verify_live_url_deployment"): {"not_connected"},
    ("website_prepare.py", "start_prepare"): {"not_connected"},
    ("website_disconnect.py", "start_disconnect"): {"not_connected", LEGACY_CODE},
    # Plan E8: a grant without contents or pull request access at repository
    # selection is a real missing grant: the founder reconnects on GitHub.
    ("website_repository_selection.py", "permission_check"): {ACCESS_LOST},
}
SPLIT = {LEGACY_CODE, NOT_CONNECTED, CONTRACT_MISSING, ACCESS_LOST, "invalid_connection_contract"}
HELPERS = {"not_connected", "_not_connected", "missing_contract_error"}


class _Sites(ast.NodeVisitor):
    """Raises of the split codes or helpers, keyed by their innermost function."""

    def __init__(self, name):
        self.name, self.stack, self.found = name, [], {}

    def visit_FunctionDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Raise(self, node):
        call = node.exc
        if self.stack and isinstance(call, ast.Call):
            callee = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", None)
            name = None
            if callee in HELPERS:
                name = callee
            elif callee == "WebsiteAuthorityError" and call.args and isinstance(call.args[0], ast.Constant):
                name = call.args[0].value if call.args[0].value in SPLIT else None
            if name:
                self.found.setdefault((self.name, self.stack[-1]), set()).add(name)
        self.generic_visit(node)


def _raise_sites():
    """(file, innermost function) -> codes raised there, for the split codes and helpers."""
    found = {}
    for path in sorted(ROOT.glob("*.py")):
        if path.name.startswith("tests") or path.name == "website_connection_codes.py":
            continue
        visitor = _Sites(path.name)
        visitor.visit(ast.parse(path.read_text()))
        found.update(visitor.found)
    return found


class RaiseSiteTableTests(SimpleTestCase):
    def test_every_former_reconnect_site_has_its_classified_code(self):
        found = _raise_sites()
        for key, codes in RAISE_SITES.items():
            self.assertIn(key, found, f"{key} no longer raises a classified connection code")
            self.assertEqual(found[key], codes, key)

    def test_no_new_site_says_website_connection_required_unclassified(self):
        unexpected = {key: codes for key, codes in _raise_sites().items()
                      if key not in RAISE_SITES and codes & {LEGACY_CODE, ACCESS_LOST, NOT_CONNECTED, CONTRACT_MISSING}
                      and not codes <= HELPERS}
        self.assertEqual(unexpected, {}, "classify the new site in RAISE_SITES (B12)")

    def test_registry_keeps_the_legacy_alias_and_only_access_loss_reconnects(self):
        registry = registry_policy.load_registry()["by_code_v2"]
        self.assertIn(LEGACY_CODE, registry)
        self.assertEqual(legacy_code(NOT_CONNECTED), LEGACY_CODE)
        self.assertEqual(legacy_code(CONTRACT_MISSING), LEGACY_CODE)
        self.assertIsNone(legacy_code(LEGACY_CODE))
        self.assertEqual(registry[CONTRACT_MISSING]["failure_class"], "transient")
        self.assertEqual(registry[CONTRACT_MISSING]["founder_copy_id"], "working")
        self.assertEqual(registry[ACCESS_LOST]["action_id"], "authorize_github")

    def test_refusal_carries_the_legacy_code_for_old_clients(self):
        body = WebsiteAuthorityError(NOT_CONNECTED, "Connect your website to continue.").as_dict()
        if body["code"] == NOT_CONNECTED:
            self.assertEqual(body["legacy_code"], LEGACY_CODE)
        else:  # taxonomy observe: the legacy view already is the old code
            self.assertEqual(body["code"], LEGACY_CODE)


class Endpoint:
    calls = []

    @guarded_service_write("config_write", only_repository=True)
    def post(self, request):
        Endpoint.calls.append(dict(request.data))
        return Response({"saved": True})


@override_settings(ARTICLES_CALLBACK_CONTRACT_FROM_RUN_ENABLED=False)
class SavedRunDecisionTests(WebsiteDatabaseFixture, TestCase):
    """The code of a callback without identity comes from the saved run and the connection."""

    def setUp(self):
        super().setUp()
        Endpoint.calls = []
        self.run = ContentFactoryRun.objects.create(run_id="run-split", organization=self.org, domain=self.org.domain,
            github_repo=self.website.github_repo, workflow="confirmed_topic", status="approval_required",
            run_request={**self.binding, "topic": "Home modifications"})

    def bare(self, **extra):
        return {"event": "article_review_preview_quality", "job_id": self.run.run_id, "domain": self.org.domain, **extra}

    def code(self, payload=None):
        return missing_contract_error(self.bare() if payload is None else payload)

    def test_bound_run_on_a_live_connection_is_the_retryable_protocol_fault(self):
        refusal = self.code()
        self.assertEqual(refusal.code, CONTRACT_MISSING)
        self.assertTrue(refusal.retryable)

    def test_the_guard_answers_with_the_protocol_fault_not_reconnect(self):
        request = Request(APIRequestFactory().post("/api/content-factory/callback", self.bare(), format="json"),
                          parsers=[JSONParser()])
        response = Endpoint().post(request)
        self.assertEqual(response.data.get("code"), CONTRACT_MISSING)
        self.assertEqual(Endpoint.calls, [])

    def test_revoked_connection_is_access_loss(self):
        self.website.state = "revoked"
        self.website.save(update_fields=["state"])
        self.assertEqual(self.code().code, ACCESS_LOST)

    def test_disconnected_connection_is_not_connected(self):
        self.website.state = "disconnected"
        self.website.save(update_fields=["state"])
        self.assertEqual(self.code().code, NOT_CONNECTED)

    def test_paused_connection_is_paused_not_reconnect(self):
        self.website.state = "paused"
        self.website.save(update_fields=["state"])
        self.assertEqual(self.code().code, PAUSED)

    def test_older_generation_is_a_changed_connection_and_never_retried(self):
        self.website.generation += 1
        self.website.save(update_fields=["generation"])
        refusal = self.code()
        self.assertEqual(refusal.code, "website_connection_changed")
        self.assertFalse(refusal.retryable)

    def test_a_callback_naming_another_domain_is_a_scope_mismatch(self):
        self.assertEqual(self.code(self.bare(domain="other.example.test")).code, "website_scope_mismatch")

    def test_a_run_without_saved_consent_on_a_connected_site(self):
        self.run.run_request = {"topic": "Unbound"}
        self.run.save(update_fields=["run_request"])
        refusal = self.code()
        self.assertEqual(refusal.code, "invalid_connection_contract")
        self.assertFalse(refusal.retryable)

    def test_no_run_and_no_connection_is_not_connected(self):
        self.config.website_connection = None
        self.config.save(update_fields=["website_connection"])
        self.assertEqual(missing_contract_error({"domain": self.org.domain}).code, NOT_CONNECTED)

    def test_old_client_without_identity_keeps_the_legacy_code(self):
        self.assertEqual(missing_contract_error({"domain": self.org.domain}).code, LEGACY_CODE)

    def test_connection_state_helper(self):
        self.assertIsNone(for_connection_state(self.website))
        self.assertEqual(for_connection_state(None).code, NOT_CONNECTED)


class AuthorityNeedTests(WebsiteDatabaseFixture, TestCase):
    """Authority need comes from the saved run, not the event name."""

    def run_for(self, workflow, request=None):
        return ContentFactoryRun.objects.create(run_id=f"need-{workflow}", organization=self.org, domain=self.org.domain,
            workflow=workflow, status="running", run_request=request or {})

    def test_unbound_research_run_needs_no_repository_authority_whatever_the_event(self):
        run = self.run_for("island_research")
        self.assertFalse(needs_repository_authority({"run_id": run.run_id, "event": "article_research_done"}))

    def test_bound_research_run_keeps_the_guard(self):
        run = self.run_for("island_research", self.binding)
        self.assertTrue(needs_repository_authority({"run_id": run.run_id, "event": "research_done"}))

    def test_repository_run_needs_authority_without_an_event(self):
        run = self.run_for("confirmed_topic")
        self.assertTrue(needs_repository_authority({"run_id": run.run_id}))

    def test_payload_connection_and_config_fields_still_force_the_guard(self):
        run = self.run_for("island_research")
        self.assertTrue(needs_repository_authority({"run_id": run.run_id, **self.binding}))
        self.assertTrue(needs_repository_authority({"run_id": run.run_id, "github_repo": "example/site"}))
        self.assertTrue(needs_repository_authority({"run_id": run.run_id, "workflow": "article_system_setup"}))

    def test_unknown_run_falls_back_to_the_event_name(self):
        self.assertTrue(needs_repository_authority({"run_id": "not-saved", "event": "publish_started"}))
        self.assertFalse(needs_repository_authority({"run_id": "not-saved", "event": "research_done"}))
