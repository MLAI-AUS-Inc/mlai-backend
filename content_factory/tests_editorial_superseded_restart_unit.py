"""Owner Restart carries an article to its profile's newer approved version (plan A8).

Run with scripts/test_without_database.py. The Restart helper is executed from
the unmodified function AST in vibe_marketing_views.py, so the view module (and
its database imports) is never loaded. Synthetic catalogs only.
"""
import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

from django.conf import settings

if not settings.configured:
    settings.configure(SECRET_KEY="unit-fixture-only", USE_TZ=True, USE_I18N=False,
                       DATABASES={"default": {"ENGINE": "django.db.backends.dummy"}})

from .editorial_catalog import (
    approve_catalog, article_brief_for_catalog, catalog_payload, update_catalog, upgrade_superseded_brief,
)
from .tests_editorial_catalog_unit import approval_payload, edit_payload

NOW = datetime(2026, 11, 18, 0, 49, tzinfo=timezone.utc)
NO_OFFER_BRIEF = {"audience_id": "SHIFT", "audience_version": 1, "conversion_intent": "none", "offer_id": None,
                  "offer_version": None, "no_offer_reason": "First article builds trust before any offer.",
                  "country": "AU", "reader_task": "Plan a week of shifts", "distinct_contribution": "A worked roster",
                  "acceptance_criteria": ["Show the roster"]}


def _approve(strategy):
    return approve_catalog(strategy, approval_payload(strategy), actor_id="user:fixture-owner", approved_at=NOW)


def no_offer_catalog():
    draft = update_catalog({}, {"expected_editorial_catalog_version": 0, "audience_options": [
        {"id": "SHIFT", "reader_task": "Plan a week of shifts", "allow_no_offer": True}]})
    return _approve(draft)


def resaved(strategy, **changes):
    """An owner Save of the same profile: a new draft version, then the owner's approval."""
    payload = edit_payload(strategy)
    payload["audience_options"][0].update(version=payload["audience_options"][0]["version"] + 1, status="draft",
                                          approved_by=None, approved_at=None,
                                          reader_task="Plan a fortnight of shifts", **changes)
    return _approve(update_catalog(strategy, payload))


def offer_catalog():
    draft = update_catalog({}, {"expected_editorial_catalog_version": 0,
        "audience_options": [{"id": "SHIFT", "reader_task": "Plan a week of shifts", "allow_no_offer": True}],
        "cta_options": [{"id": "trial", "title": "Try it", "body": "Start a trial.", "button_text": "Start",
                         "button_href": "/trial", "audience_ids": ["SHIFT"], "countries": ["AU"]}]})
    return _approve(draft)


def load_restart_helper(enabled=True):
    tree = ast.parse(Path(__file__).with_name("vibe_marketing_views.py").read_text())
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_restart_editorial_brief"]
    namespace = {"article_brief_for_catalog": article_brief_for_catalog,
                 "upgrade_superseded_brief": upgrade_superseded_brief,
                 "settings": SimpleNamespace(CONTENT_FACTORY_EDITORIAL_SUPERSEDED_OK=enabled)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "vibe_marketing_views.py", "exec"),
         namespace)
    return namespace["_restart_editorial_brief"]


class UpgradeSupersededBriefTests(unittest.TestCase):
    def test_restart_after_a_resave_yields_a_valid_brief_at_the_current_version(self):
        strategy = resaved(no_offer_catalog())
        self.assertEqual(catalog_payload(strategy)["audience_options"][0]["version"], 2)
        request = {"editorial_brief": NO_OFFER_BRIEF}
        with self.assertRaises(ValueError):
            article_brief_for_catalog(strategy, request)
        upgraded = load_restart_helper()(strategy, request)
        self.assertEqual(upgraded, {**NO_OFFER_BRIEF, "audience_version": 2})
        # The upgraded brief passes the same check every later boundary applies.
        self.assertEqual(article_brief_for_catalog(strategy, {"editorial_brief": upgraded}), upgraded)
        self.assertEqual(request["editorial_brief"]["audience_version"], 1)

    def test_current_brief_is_returned_unchanged(self):
        strategy = no_offer_catalog()
        self.assertEqual(load_restart_helper()(strategy, {"editorialBrief": NO_OFFER_BRIEF}), NO_OFFER_BRIEF)
        self.assertIsNone(upgrade_superseded_brief(strategy, {"editorial_brief": NO_OFFER_BRIEF}))

    def test_restart_refuses_across_a_retired_draft_or_unreceipted_profile(self):
        approved = resaved(no_offer_catalog())
        retired_payload = edit_payload(approved)
        retired_payload["audience_options"][0].update(version=3, status="retired", approved_by=None, approved_at=None)
        retired = update_catalog(approved, retired_payload)
        draft_payload = edit_payload(approved)
        draft_payload["audience_options"][0].update(version=3, status="draft", approved_by=None, approved_at=None,
                                                    reader_task="Unreviewed wording")
        draft = update_catalog(approved, draft_payload)
        forged = resaved(no_offer_catalog())
        forged["editorial_catalog"]["approval_receipts"]["audience"]["SHIFT"]["content_sha256"] = "0" * 64
        withdrawn = resaved(no_offer_catalog(), allow_no_offer=False)
        for name, strategy in (("retired", retired), ("draft", draft), ("receipt", forged), ("no-offer", withdrawn)):
            with self.subTest(name), self.assertRaises(ValueError):
                load_restart_helper()(strategy, {"editorial_brief": NO_OFFER_BRIEF})

    def test_restart_never_crosses_a_changed_offer(self):
        original = offer_catalog()
        brief = {**NO_OFFER_BRIEF, "conversion_intent": "offer", "offer_id": "trial", "offer_version": 1,
                 "no_offer_reason": None}
        self.assertEqual(article_brief_for_catalog(original, {"editorial_brief": brief}), brief)
        # An audience edit re-versions its dependent offer; the owner re-approves both.
        payload = edit_payload(original)
        payload["audience_options"][0].update(version=2, status="draft", approved_by=None, approved_at=None,
                                              reader_task="Plan a fortnight of shifts")
        payload["cta_options"][0].update(version=2, status="draft", approved_by=None, approved_at=None)
        both = _approve(update_catalog(original, payload))
        with self.assertRaises(ValueError):
            load_restart_helper()(both, {"editorial_brief": brief})

    def test_switch_off_keeps_the_original_refusal(self):
        strategy = resaved(no_offer_catalog())
        with self.assertRaisesRegex(ValueError, "stale"):
            load_restart_helper(enabled=False)(strategy, {"editorial_brief": NO_OFFER_BRIEF})

    def test_the_switch_defaults_on_and_ships_with_every_release_list(self):
        root = Path(__file__).resolve().parent.parent
        name = "CONTENT_FACTORY_EDITORIAL_SUPERSEDED_OK"
        self.assertIn(f'{name} = _env_is_true("{name}", True)', (root / "mlai/settings.py").read_text())
        self.assertIn(f"{name}: ${{{{ vars.{name} || '' }}}}", (root / ".github/workflows/deploy.yml").read_text())
        self.assertIn(f"{name}=true", (root / ".env.example").read_text())
        self.assertEqual((root / "scripts/upsert_env_value_from_stdin.sh").read_text().count(f"|{name}|"), 2)


if __name__ == "__main__":
    unittest.main()
