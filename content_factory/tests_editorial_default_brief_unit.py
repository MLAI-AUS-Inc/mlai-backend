"""Every org state yields a brief or one founder question, never a bare code string (plan B19).

Run with scripts/test_without_database.py. Pure catalogue fixtures only; the
start path wiring and the call-site classification are checked against the
source so the view module (and its database imports) is never loaded.
"""
import ast
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest

from django.conf import settings

if not settings.configured:
    settings.configure(SECRET_KEY="unit-fixture-only", USE_TZ=True, USE_I18N=False,
                       DATABASES={"default": {"ENGINE": "django.db.backends.dummy"}})

from .editorial_catalog import CATALOG_KEY, approve_catalog, article_brief_for_catalog, catalog_payload, update_catalog
from .editorial_default_brief import (
    ASK_ACTION, ASK_CODE, default_start_brief, has_brief, start_brief,
)
from .tests_editorial_catalog_unit import approval_payload

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 19, 1, 0, tzinfo=timezone.utc)
REGISTRY = json.loads((ROOT / "contracts/failure-codes.json").read_text())
OFFER = {"title": "Try it", "body": "Start a trial.", "button_text": "Start", "button_href": "/trial", "countries": ["AU"]}


def _approve(strategy):
    return approve_catalog(strategy, approval_payload(strategy), actor_id="user:fixture-owner", approved_at=NOW)


def _draft(audiences, offers=()):
    return update_catalog({"pillars": ["Delivery"]}, {"expected_editorial_catalog_version": 0,
                                                      "audience_options": list(audiences), "cta_options": list(offers)})


def _retire_all(strategy):
    payload = catalog_payload(strategy)
    payload["expected_editorial_catalog_version"] = payload.pop("editorial_catalog_version")
    for item in payload["audience_options"]:
        item.update(version=item["version"] + 1, status="retired", approved_by=None, approved_at=None)
    payload["cta_options"] = []
    return update_catalog(strategy, payload)


NO_OFFER = {"id": "shift-managers", "reader_task": "Plan a week of shifts", "allow_no_offer": True}
NEEDS_OFFER = {"id": "smb", "reader_task": "Choose rostering software", "allow_no_offer": False}

ORG_STATES = {
    # name: (strategy factory, expected state)
    "legacy catalogue-free": (lambda: {"pillars": ["Delivery"]}, "legacy"),
    "no strategy at all": (lambda: None, "legacy"),
    "empty configured catalogue": (lambda: {CATALOG_KEY: {"version": 0}}, "ask"),
    "seeded draft audience only": (lambda: _draft([NO_OFFER]), "ask"),
    "approved no-offer audience": (lambda: _approve(_draft([NO_OFFER])), "brief"),
    "approved audience with one offer": (lambda: _approve(_draft([NEEDS_OFFER], [{**OFFER, "id": "trial", "audience_ids": ["smb"]}])), "brief"),
    "approved audience with two offers": (lambda: _approve(_draft([NEEDS_OFFER], [
        {**OFFER, "id": "trial", "audience_ids": ["smb"]}, {**OFFER, "id": "demo", "audience_ids": ["smb"]}])), "ask"),
    "approved audience needing an offer, none approved": (lambda: _approve(_draft([NEEDS_OFFER])), "ask"),
    "offer in another market only": (lambda: _approve(_draft([NEEDS_OFFER], [
        {**OFFER, "id": "trial", "audience_ids": ["smb"], "countries": ["NZ"]}])), "ask"),
    "every audience retired": (lambda: _retire_all(_approve(_draft([NO_OFFER]))), "ask"),
    "receipt no longer matches (unreceipted)": (lambda: _forged(_approve(_draft([NO_OFFER]))), "ask"),
    "unreadable catalogue": (lambda: {CATALOG_KEY: "not a catalogue"}, "ask"),
    "unreadable strategy": (lambda: ["not", "a", "strategy"], "ask"),
    "approved and draft audiences": (lambda: _with_extra_draft(_approve(_draft([NO_OFFER]))), "brief"),
}


def _forged(strategy):
    strategy[CATALOG_KEY]["approval_receipts"]["audience"]["shift-managers"]["content_sha256"] = "0" * 64
    return strategy


def _with_extra_draft(strategy):
    payload = catalog_payload(strategy)
    payload["expected_editorial_catalog_version"] = payload.pop("editorial_catalog_version")
    payload["audience_options"].append({"id": "draft-only", "reader_task": "Unreviewed", "allow_no_offer": True,
                                        "version": 1, "status": "draft", "approved_by": None, "approved_at": None})
    return update_catalog(strategy, payload)


class DefaultBriefMatrixTests(unittest.TestCase):
    def test_every_org_state_yields_a_brief_or_one_question(self):
        actions = set(REGISTRY["actions"])
        entry = next(item for item in REGISTRY["codes"] if item["code"] == ASK_CODE)
        for name, (factory, expected) in ORG_STATES.items():
            with self.subTest(name):
                strategy = factory()
                before = deepcopy(strategy)
                outcome = default_start_brief(strategy, country="AU", startup_name="Rosterly")
                self.assertEqual(outcome.state, expected)
                # Pure: never writes, approves or creates a catalogue.
                self.assertEqual(strategy, before)
                if expected == "legacy":
                    self.assertIsNone(outcome.brief)
                elif expected == "brief":
                    self.assertEqual(article_brief_for_catalog(strategy, {"editorial_brief": outcome.brief}),
                                     outcome.brief)
                    audience = next(item for item in catalog_payload(strategy)["audience_options"]
                                    if item["id"] == outcome.brief["audience_id"])
                    self.assertEqual(audience["status"], "approved")
                else:
                    payload = outcome.ask_payload()
                    # One question: a registered founder ask with a canonical action and plain copy.
                    self.assertEqual(payload["code"], ASK_CODE)
                    self.assertEqual(entry["failure_class"], "needs_user")
                    self.assertIn(payload["action"]["id"], actions)
                    self.assertEqual(payload["action"]["id"], ASK_ACTION)
                    self.assertTrue(payload["detail"].endswith(".") and "_" not in payload["detail"])
                    self.assertNotIn("Configured catalog", payload["detail"])

    def test_a_draft_audience_is_never_approved_by_the_default(self):
        strategy = _draft([NO_OFFER])
        outcome = default_start_brief(strategy, country="AU", startup_name="Rosterly")
        self.assertEqual(outcome.state, "ask")
        self.assertEqual(catalog_payload(strategy)["audience_options"][0]["status"], "draft")

    def test_a_legacy_org_never_gets_a_catalogue(self):
        strategy = {"pillars": ["Delivery"]}
        self.assertEqual(start_brief(strategy, {}, country="AU", startup_name="Rosterly").state, "legacy")
        self.assertNotIn(CATALOG_KEY, strategy)

    def test_a_no_offer_profile_is_preferred_and_a_named_profile_wins(self):
        strategy = _approve(_draft([NEEDS_OFFER, NO_OFFER], [{**OFFER, "id": "trial", "audience_ids": ["smb"]}]))
        self.assertEqual(default_start_brief(strategy, country="AU", startup_name="R").brief["audience_id"],
                         "shift-managers")
        named = default_start_brief(strategy, country="AU", startup_name="R", preferred_audience_id="smb").brief
        self.assertEqual((named["audience_id"], named["offer_id"], named["conversion_intent"]), ("smb", "trial", "offer"))
        self.assertEqual(start_brief(strategy, {"audienceId": "smb"}, country="AU", startup_name="R").brief["audience_id"],
                         "smb")

    def test_an_explicit_brief_is_validated_not_replaced(self):
        strategy = _approve(_draft([NO_OFFER]))
        brief = default_start_brief(strategy, country="AU", startup_name="R").brief
        chosen = {**brief, "reader_task": "The founder's own wording"}
        self.assertEqual(start_brief(strategy, {"editorial_brief": chosen}, country="AU", startup_name="R").brief, chosen)
        # An explicit null brief is refused as before, never silently defaulted.
        self.assertTrue(has_brief({"editorial_brief": None}))
        with self.assertRaises(ValueError):
            start_brief(strategy, {"editorial_brief": None}, country="AU", startup_name="R")
        with self.assertRaises(ValueError):
            start_brief(strategy, {"editorialBrief": {**brief, "audience_version": 9}}, country="AU", startup_name="R")


def _function(path, name):
    tree = ast.parse((ROOT / path).read_text())
    return next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)


def _callers(path, callee):
    tree = ast.parse((ROOT / path).read_text())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if isinstance(call, ast.Call):
                target = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", None)
                if target == callee:
                    keywords = {item.arg: ast.unparse(item.value) for item in call.keywords}
                    found.append((node.name, keywords.get("superseded")))
    return found


class StartPathWiringTests(unittest.TestCase):
    def test_article_start_uses_the_server_default_and_asks_one_question(self):
        source = ast.unparse(_function("content_factory/vibe_marketing_views.py", "start_article_generation"))
        self.assertIn("start_brief(config.pillar_strategy, command.data", source)
        self.assertIn("start.ask_payload()", source)
        self.assertNotIn("editorial_brief = article_brief_for_catalog(config.pillar_strategy, command.data)", source)

    def test_the_eight_call_sites_are_classified(self):
        sites = (_callers("content_factory/vibe_marketing_views.py", "_refresh_article_editorial_payload")
                 + _callers("content_factory/dispatch_outbox.py", "_refresh_article_editorial_payload")
                 + _callers("content_factory/staff_console.py", "_refresh_article_editorial_payload"))
        by_site = {}
        for name, rule in sites:
            by_site.setdefault(name, set()).add(rule)
        self.assertEqual(set(by_site), {
            "_revision_editorial_payload_from_run", "_charge_roo_points_for_article", "_restart_article_run",
            "_queue_content_factory_run_authorized", "_call_content_factory_component_revision", "post",
            "_deliver", "_start_copy_ready"})
        keep = "'keep'"
        self.assertEqual(by_site["_revision_editorial_payload_from_run"], {keep})
        self.assertEqual(by_site["_call_content_factory_component_revision"], {keep})
        self.assertEqual(by_site["post"], {keep})
        self.assertEqual(by_site["_deliver"], {"_superseded_rule(row, request)"})
        # Owner starts, Restart (A8 upgrades before this check) and staff keep the exact rule.
        for name in ("_charge_roo_points_for_article", "_restart_article_run",
                     "_queue_content_factory_run_authorized", "_start_copy_ready"):
            self.assertEqual(by_site[name], {None}, name)
        documented = (ROOT / "content_factory/editorial_default_brief.py").read_text()
        for name in by_site:
            self.assertIn(name if name != "post" else "VibeMarketingRunCommentsSubmitView.post", documented)


if __name__ == "__main__":
    unittest.main()
