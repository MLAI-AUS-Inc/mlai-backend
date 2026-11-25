"""Delayed dispatch and revisions accept a superseded brief; saves name open runs (plan B9).

Run with scripts/test_without_database.py. The view helpers run from their
unmodified function AST in vibe_marketing_views.py with stub model managers, so
no database is touched. Synthetic catalogues only.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from django.conf import settings

if not settings.configured:
    settings.configure(SECRET_KEY="unit-fixture-only", USE_TZ=True, USE_I18N=False,
                       DATABASES={"default": {"ENGINE": "django.db.backends.dummy"}})

from django.db import DatabaseError
from rest_framework import status as drf_status
from rest_framework.response import Response

from django.test import override_settings

from .editorial_catalog import SUPERSEDED_KEEP, SUPERSEDED_UPGRADE, article_brief_for_catalog, update_catalog
from .tests_editorial_catalog_unit import edit_payload
from .tests_editorial_superseded_restart_unit import NO_OFFER_BRIEF, _approve, no_offer_catalog, offer_catalog, resaved

ROOT = Path(__file__).resolve().parents[1]


class _Configs:
    def __init__(self, strategy):
        self.strategy = strategy

    def filter(self, **_kwargs):
        return self

    def only(self, *_fields):
        return self

    def first(self):
        return None if self.strategy is ... else SimpleNamespace(pillar_strategy=self.strategy)


def load_refresh(strategy):
    tree = ast.parse((ROOT / "content_factory/vibe_marketing_views.py").read_text())
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name == "_refresh_article_editorial_payload"]
    namespace = {"article_brief_for_catalog": article_brief_for_catalog,
                 "OrganizationContentConfig": SimpleNamespace(objects=_Configs(strategy)),
                 "DatabaseError": DatabaseError, "Response": Response, "status": drf_status}
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "vibe_marketing_views.py", "exec"), namespace)
    return namespace["_refresh_article_editorial_payload"]


ORG = SimpleNamespace(pk=7, domain="rosterly.example")


class SupersededRefreshTests(unittest.TestCase):
    def setUp(self):
        self.strategy = resaved(no_offer_catalog())  # Audience SHIFT is now approved at v2.

    def refresh(self, mode, *, strategy=None, enabled=True, brief=NO_OFFER_BRIEF):
        refresh = load_refresh(self.strategy if strategy is None else strategy)
        payload = {"domain": ORG.domain, "editorial_brief": dict(brief)}
        rule = {"keep": SUPERSEDED_KEEP, "upgrade": SUPERSEDED_UPGRADE}.get(mode)
        with override_settings(CONTENT_FACTORY_EDITORIAL_SUPERSEDED_OK=enabled):
            return refresh(organization=ORG, payload=payload, superseded=rule), payload

    def test_exact_rule_still_refuses_a_stale_brief(self):
        error, _ = self.refresh(None)
        self.assertEqual((error.status_code, error.data["code"]), (400, "editorial_brief_invalid"))

    def test_revision_paths_keep_the_original_brief(self):
        error, payload = self.refresh("keep")
        self.assertIsNone(error)
        # The worker compares a revision with its source brief and admission; the brief is unchanged.
        self.assertEqual(payload["editorial_brief"], NO_OFFER_BRIEF)

    def test_delayed_dispatch_upgrades_to_the_current_approved_version(self):
        error, payload = self.refresh("upgrade")
        self.assertIsNone(error)
        self.assertEqual(payload["editorial_brief"], {**NO_OFFER_BRIEF, "audience_version": 2})
        self.assertEqual(article_brief_for_catalog(self.strategy, payload), payload["editorial_brief"])

    def test_a_current_brief_is_unchanged_in_every_mode(self):
        for mode in (None, "keep", "upgrade"):
            with self.subTest(mode):
                error, payload = self.refresh(mode, strategy=no_offer_catalog())
                self.assertIsNone(error)
                self.assertEqual(payload["editorial_brief"], NO_OFFER_BRIEF)

    def test_revocation_and_a_changed_offer_still_refuse_in_every_mode(self):
        retired_payload = edit_payload(self.strategy)
        retired_payload["audience_options"][0].update(version=3, status="retired", approved_by=None, approved_at=None)
        retired = update_catalog(self.strategy, retired_payload)
        withdrawn = resaved(no_offer_catalog(), allow_no_offer=False)
        original = offer_catalog()
        changed = edit_payload(original)
        changed["audience_options"][0].update(version=2, status="draft", approved_by=None, approved_at=None)
        changed["cta_options"][0].update(version=2, status="draft", approved_by=None, approved_at=None)
        offer_brief = {**NO_OFFER_BRIEF, "conversion_intent": "offer", "offer_id": "trial", "offer_version": 1,
                       "no_offer_reason": None}
        cases = (("retired", retired, NO_OFFER_BRIEF), ("no-offer withdrawn", withdrawn, NO_OFFER_BRIEF),
                 ("changed offer", _approve(update_catalog(original, changed)), offer_brief))
        for name, strategy, brief in cases:
            for mode in (None, "keep", "upgrade"):
                with self.subTest(name, mode=mode):
                    error, payload = self.refresh(mode, strategy=strategy, brief=brief)
                    self.assertEqual(error.data["code"], "editorial_brief_invalid")
                    self.assertEqual(payload["editorial_brief"], brief)

    def test_switch_off_keeps_the_exact_rule(self):
        for mode in ("keep", "upgrade"):
            with self.subTest(mode):
                error, _ = self.refresh(mode, enabled=False)
                self.assertEqual(error.data["code"], "editorial_brief_invalid")


class DelayedDispatchRuleTests(unittest.TestCase):
    def rule(self, request, *, provisional=False):
        from . import dispatch_outbox
        row = SimpleNamespace(client_request_id="vibe-article:7:abc")
        runs = SimpleNamespace(filter=lambda **_kwargs: SimpleNamespace(exists=lambda: provisional))
        with patch("workflow_runs.models.ContentFactoryRun.objects", runs):
            return dispatch_outbox._superseded_rule(row, request)

    def test_unbound_request_carries_the_brief_to_the_newer_approved_version(self):
        self.assertEqual(self.rule({"editorial_brief": NO_OFFER_BRIEF}), "upgrade")

    def test_admission_bound_or_provisional_requests_keep_the_exact_rule(self):
        from .run_ownership import WIRE_REQUEST_KEY
        self.assertEqual(self.rule({"editorial_brief": NO_OFFER_BRIEF, WIRE_REQUEST_KEY: {}}), "keep")
        self.assertEqual(self.rule({"editorial_brief": NO_OFFER_BRIEF}, provisional=True), "keep")

    def test_deliver_passes_the_rule_to_the_policy_recheck(self):
        tree = ast.parse((ROOT / "content_factory/dispatch_outbox.py").read_text())
        deliver = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_deliver")
        self.assertIn("superseded=_superseded_rule(row, request)", ast.unparse(deliver))


class OpenRunsNoticeTests(unittest.TestCase):
    def setUp(self):
        self.before = no_offer_catalog()
        self.after = resaved(self.before)

    def runs(self, *runs, fail=False):
        class Query:
            def filter(self, **kwargs):
                self.kwargs = kwargs
                if fail:
                    raise DatabaseError("unavailable")
                return self

            def only(self, *_fields):
                return self

            def order_by(self, *_fields):
                return self

            def __getitem__(self, _slice):
                return list(runs)
        return patch("workflow_runs.models.ContentFactoryRun.objects", Query())

    def article(self, run_id, brief, status="running"):
        return SimpleNamespace(run_id=run_id, status=status, workflow="article_generation",
                               run_request={"topic": f"Article {run_id}", "editorial_brief": brief})

    def test_changed_entries_name_only_what_this_save_changed(self):
        from .editorial_open_runs import changed_entries
        self.assertEqual(set(changed_entries(self.before, self.after)), {("audience", "SHIFT")})
        self.assertEqual(changed_entries(self.after, self.after), {})

    def test_an_open_no_offer_article_finishes_with_the_earlier_wording(self):
        from .editorial_open_runs import open_runs_for_changed_entries
        offer_brief = {**NO_OFFER_BRIEF, "conversion_intent": "offer", "offer_id": "trial", "offer_version": 1,
                       "no_offer_reason": None}
        current = {**NO_OFFER_BRIEF, "audience_version": 2}
        with self.runs(self.article("r1", NO_OFFER_BRIEF), self.article("r2", current), self.article("r3", offer_brief)):
            result = open_runs_for_changed_entries(ORG, self.before, self.after)
        self.assertEqual([(item["run_id"], item["outcome"]) for item in result],
                         [("r1", "finishes_with_earlier_wording"), ("r3", "asks_founder")])
        self.assertEqual((result[0]["written_for_version"], result[0]["current_version"], result[0]["title"]),
                         (1, 2, "Article r1"))

    def test_a_withdrawn_no_offer_permission_asks_the_founder(self):
        # Review fix: the worker treats a withdrawn no-offer permission as revoked.
        from .editorial_open_runs import open_runs_for_changed_entries
        after = resaved(self.before, allow_no_offer=False)
        with self.runs(self.article("r1", NO_OFFER_BRIEF)):
            result = open_runs_for_changed_entries(ORG, self.before, after)
        self.assertEqual([(item["run_id"], item["outcome"]) for item in result], [("r1", "asks_founder")])

    def test_no_change_or_an_unavailable_read_never_blocks_the_save(self):
        from .editorial_open_runs import open_runs_for_changed_entries
        self.assertEqual(open_runs_for_changed_entries(ORG, self.after, self.after), [])
        with self.runs(fail=True), self.assertLogs("content_factory.editorial_open_runs", "ERROR"):
            self.assertEqual(open_runs_for_changed_entries(ORG, self.before, self.after), [])

    def test_owner_save_response_carries_the_notice(self):
        tree = ast.parse((ROOT / "content_factory/editorial_views.py").read_text())
        mutate = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "mutate_catalog_response")
        source = ast.unparse(mutate)
        self.assertIn("open_runs = open_runs_for_changed_entries(org, strategy, updated)", source)
        self.assertIn("if open_runs:", source)
        self.assertIn("response['open_runs'] = open_runs", source)


if __name__ == "__main__":
    unittest.main()
