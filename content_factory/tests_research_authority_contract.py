"""B16: research authority is stamped in the admission, not inferred.

Builds the admission every research producer sends to Content Factory, under
every cohort stamp and admission flag combination, with the backend's own
``_identity`` / ``wire_identity`` / ``evidence_digest``. The cases are written
to ``contracts/research-admission-cases.json``; Content Factory keeps a byte
copy at ``backend/tests/fixtures/research-admission-cases.json`` and asserts
the outcome it reaches for each case (``tests/test_research_authority_contract.py``),
so a change on either side fails one of the two suites.

Regenerate after an intended change with
``RESEARCH_ADMISSION_CASES_WRITE=1 python manage.py test content_factory.tests_research_authority_contract``.
"""
from __future__ import annotations

from copy import deepcopy
import itertools
import json
import os
from pathlib import Path
from types import SimpleNamespace

from django.test import SimpleTestCase

from .billing import CONTENT_FACTORY_ACTION_CONTENT_ISLAND_TOPIC_GENERATION
from .run_ownership import (
    AUTHORITY_SCOPE_KEY, BUDGET_KEY, ORGANIZATION_RESEARCH_SCOPE, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY,
    _identity, _matching_legacy_identity, scheduler_principal, user_principal, wire_identity,
)
from .website_contract import evidence_digest

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "contracts/research-admission-cases.json"
ORGANIZATION = SimpleNamespace(pk=209, domain="lilia-planner.workers.dev")
USER = ("7", "mlai_user:7")
COHORTS = {
    "legacy": {"version": 1, "cohort": "legacy", "failure_policy": "pre_p2"},
    "p2_pre_p2": {"version": 1, "cohort": "p2", "failure_policy": "pre_p2"},
    "p2_v2": {"version": 1, "cohort": "p2", "failure_policy": "v2"},
}
WEBSITE_BINDING = {
    "website_connection_id": "2090c0de-0000-4000-8000-000000000209", "connection_generation": 3, "repository_id": 4242,
    "connection_target_id": "target-209", "github_repo": "lilia/planner", "app_root": "", "branch": "main",
    "expected_source_sha": "a" * 40,
}


def _points(action="topic_discovery"):
    return {"roo_points_authorized": True, "roo_points_action": action, "roo_points_cost": 6,
            "roo_points_required": 6, "roo_points_billing_status": "charged", "free_domain": False,
            "roo_points_balance": 20, "roo_points_ledger_id": "ledger-1"}


def _producers():
    """(name, workflow, principal, payload) as each research producer builds it."""
    domain = ORGANIZATION.domain
    yield ("nightly_island_refresh", "island_refresh", scheduler_principal("island_refresh"), {
        # services.island_refresh_scheduler.dispatch_island_refresh + dispatch_admission
        "domain": domain, "client_request_id": f"island-refresh:{domain}:2026-11-19",
        "request_source": "mlai_backend_scheduler", "include_expansion": True,
        "island_refresh_recovery_version": 1})
    yield ("founder_island_research", "island_refresh", user_principal(*USER), {
        # island_research_views.ContentIslandResearchView
        "domain": domain, "client_request_id": "island-research:" + "b" * 64, "slack_user_id": USER[1],
        "request_source": "roo_slackbot", "requested_topic_count": 1,
        "island_research_brief": {"subject": "Daily planning for shift workers", "description": "", "audience": "",
                                  "focus": "", "intent": "any"},
        **_points(CONTENT_FACTORY_ACTION_CONTENT_ISLAND_TOPIC_GENERATION)})
    yield ("founder_topic_discovery", "auto_discovery", user_principal(*USER), {
        "domain": domain, "client_request_id": "discovery:" + "c" * 32, "slack_user_id": USER[1],
        "request_source": "roo_slackbot", **_points()})
    automation = {
        # integrations.services.research_automations._discovery_payload_for_run
        "domain": domain, "client_request_id": "automation-research:" + "d" * 64, "request_source": "roo_slackbot",
        "requested_topic_count": 3, "requested_by_slack_user_id": USER[1], "user_email": "founder@example.test",
        "recipient_user_id": USER[0],
        "daily_topic_policy": {"version": 1, "cooldown_days": 7, "recent_topics": [], "preferences": []},
        "notification_context": {"automation_id": "auto-1", "automation_run_id": "slot-1", "channel_type": "email",
                                 "channel_route_id": "chan-1", "recipient_user_id": USER[0],
                                 "automation_trigger": "scheduled"},
        **_points()}
    yield ("automation_research", "auto_discovery", user_principal(*USER), automation)
    yield ("automation_research_website_bound", "auto_discovery", user_principal(*USER),
           {**automation, **WEBSITE_BINDING})


def _case(name, workflow, principal, payload, *, cohort, merge_policy, event_protocol):
    request = deepcopy(payload)
    if cohort is not None:
        request["self_heal_policy_v1"] = deepcopy(COHORTS[cohort])
    if merge_policy:
        request["merge_policy"] = "backend_worker"
    stamped_expected = False
    if event_protocol != "no_admission":
        identity, saved = _identity(organization=ORGANIZATION, principal=principal, workflow=workflow, payload=request)
        stamped_expected = identity.get(AUTHORITY_SCOPE_KEY) == ORGANIZATION_RESEARCH_SCOPE
        wire = wire_identity({"identity": identity, "authority": {"event_protocol": event_protocol}})
        ticket = {"schema_version": 1, "execution_id": "admit:" + evidence_digest(wire), "generation": 0,
                  "admission_digest": evidence_digest(wire), "request_digest": identity["request_digest"],
                  "scope": deepcopy(identity["scope"])}
        request = {**saved, WIRE_REQUEST_KEY: wire, WIRE_EXECUTION_KEY: ticket}
    request["workflow"] = workflow
    bound = name.endswith("website_bound")
    # What Content Factory must conclude: a stamp is read; without one the
    # old key inference decides (kept one release), where a merge policy key
    # on island refresh still reads as website authority.
    if bound:
        expected = "website"
    elif stamped_expected:
        expected = "organization"
    else:
        expected = "website" if merge_policy and workflow == "island_refresh" else "organization"
    return {"name": f"{name}/{cohort or 'unstamped'}/merge_{'on' if merge_policy else 'off'}/{event_protocol}",
            "producer": name, "workflow": workflow, "request": request,
            "stamped": stamped_expected, "expected_research_authority": expected}


def build_cases():
    cases = []
    for producer, cohort, merge_policy, event_protocol in itertools.product(
            list(_producers()), [None, *COHORTS], [False, True], ["no_admission", None, 1]):
        if merge_policy and cohort != "p2_v2":
            continue  # The backend sets a merge policy only for P2 requesters.
        cases.append(_case(*producer, cohort=cohort, merge_policy=merge_policy, event_protocol=event_protocol))
    return {"schema_version": 1, "source": "mlai-backend content_factory.tests_research_authority_contract",
            "cases": cases}


def render(value):
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


class ResearchAuthorityStampTests(SimpleTestCase):
    def test_committed_cases_match_the_backend_producers(self):
        rendered = render(build_cases())
        if os.environ.get("RESEARCH_ADMISSION_CASES_WRITE") == "1":
            CASES_PATH.write_text(rendered)
        self.assertEqual(CASES_PATH.read_text(), rendered,
                         "Regenerate contracts/research-admission-cases.json and copy it to Content Factory.")

    def test_every_organization_research_admission_is_stamped_inside_its_digest(self):
        cases = build_cases()["cases"]
        self.assertEqual(len(cases), 5 * (4 * 3 + 3))
        for case in cases:
            with self.subTest(case=case["name"]):
                request = case["request"]
                admission = request.get(WIRE_REQUEST_KEY)
                if case["name"].endswith("/no_admission"):
                    self.assertIsNone(admission)
                    continue
                expected = not case["producer"].endswith("website_bound")
                self.assertIs(admission.get(AUTHORITY_SCOPE_KEY) == ORGANIZATION_RESEARCH_SCOPE, expected)
                self.assertIs(case["stamped"], expected)
                # The stamp sits inside the admission the execution ticket binds.
                self.assertEqual(request[WIRE_EXECUTION_KEY]["admission_digest"], evidence_digest(admission))
                if expected:
                    self.assertEqual(admission["github_repo"], "")
                    self.assertTrue(all(value is None for value in admission["scope"].values()))
                else:
                    self.assertNotIn(AUTHORITY_SCOPE_KEY, admission)
                    self.assertEqual(admission["scope"]["website_connection_id"], WEBSITE_BINDING["website_connection_id"])

    def test_a_request_cannot_carry_its_own_stamp(self):
        request = {"domain": ORGANIZATION.domain, "client_request_id": "discovery:x", "request_source": "roo_slackbot",
                   AUTHORITY_SCOPE_KEY: ORGANIZATION_RESEARCH_SCOPE, "github_repo": "lilia/planner"}
        identity, _saved = _identity(organization=ORGANIZATION, principal=user_principal(*USER),
                                     workflow="auto_discovery", payload=request)
        self.assertNotIn(AUTHORITY_SCOPE_KEY, identity)

    def test_non_research_workflows_are_never_stamped(self):
        for workflow in ("article_generation", "scan", "article_system_setup", "startup_autofill"):
            with self.subTest(workflow=workflow):
                identity, _saved = _identity(organization=ORGANIZATION, principal=user_principal(*USER),
                    workflow=workflow, payload={"domain": ORGANIZATION.domain, "client_request_id": "k"})
                self.assertNotIn(AUTHORITY_SCOPE_KEY, identity)

    def test_an_admission_saved_before_the_stamp_keeps_its_identity_on_retry(self):
        identity, _saved = _identity(organization=ORGANIZATION, principal=user_principal(*USER),
            workflow="auto_discovery", payload={"domain": ORGANIZATION.domain, "client_request_id": "k"})
        legacy = {key: value for key, value in identity.items() if key != AUTHORITY_SCOPE_KEY}
        self.assertEqual(_matching_legacy_identity(legacy, identity), legacy)
        self.assertEqual(_matching_legacy_identity(identity, identity), identity)
        # A different saved identity still differs; only the stamp is forgiven.
        other = {**legacy, "workflow": "island_refresh"}
        self.assertNotEqual(_matching_legacy_identity(other, identity), other)

    def test_budget_key_is_not_part_of_the_fixture_requests(self):
        for case in build_cases()["cases"]:
            self.assertNotIn(BUDGET_KEY, case["request"])


class StampedAdmissionEventAuthorityTests(SimpleTestCase):
    def test_backend_event_authority_accepts_only_the_organization_research_stamp(self):
        from .run_event_authority import _admission
        for case in build_cases()["cases"]:
            admission = case["request"].get(WIRE_REQUEST_KEY)
            if not admission or admission["event_protocol"] != 1:
                continue
            with self.subTest(case=case["name"]):
                self.assertTrue(_admission(admission))
                if AUTHORITY_SCOPE_KEY in admission:
                    self.assertFalse(_admission({**admission, AUTHORITY_SCOPE_KEY: "website"}))
                    self.assertFalse(_admission({**admission, AUTHORITY_SCOPE_KEY: None}))
                    unstamped = {key: value for key, value in admission.items() if key != AUTHORITY_SCOPE_KEY}
                    self.assertTrue(_admission(unstamped), "an admission saved before the stamp stays valid")
