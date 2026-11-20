"""F4 (plan 7.5) registry rules in scripts/check_failure_contract.py; never create a database.

One failing fixture per rule this repository checks (3, 5, 6 and 7): a small
synthetic registry or source tree fed to the checker's rule function. Content
Factory's checker owns rules 1, 2 and 4.
"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile

from django.test import SimpleTestCase

from scripts import check_failure_contract as checker

ROOT = Path(__file__).resolve().parents[1]


def _registry():
    return {
        "actions": {"authorize_github": "Give access on GitHub", "approve_setup": "Add to my website",
                    "buy_points": "Get more points"},
        "action_aliases": {"review_setup": "approve_setup"},
        "policies": {"retry_same_step": {"class": "transient", "mode": "same_step"},
                     "await_owner_action": {"class": "needs_user", "mode": "await_owner_event"},
                     "stop_contract": {"class": "unsupported", "mode": "stop"}},
        "codes": [
            {"code": "ASK_CODE", "failure_class": "needs_user", "retry_policy": "await_owner_action",
             "action_id": "authorize_github", "founder_copy_id": "ask_authorize_github"},
            {"code": "WORKING_CODE", "failure_class": "transient", "retry_policy": "retry_same_step", "action_id": None,
             "founder_copy_id": "working_slow"},
            {"code": "STOP_CODE", "failure_class": "unsupported", "retry_policy": "stop_contract", "action_id": None,
             "founder_copy_id": "stopped"},
        ],
    }


def _facts(**changes):
    facts = {"founder_asks": {"authorize_github", "approve_setup", "buy_points", "choose_articles_page"},
             "retired": {"choose_articles_page", "remove_setup_example"},
             "ask_copy": {"ask_authorize_github", "ask_approve_setup"},
             "copy_strings": {"ask_authorize_github", "ask_approve_setup", "ask_buy_points", "working_slow", "stopped"},
             "legacy_wire": {"approve_setup": "review_setup", "buy_points": "buy_points"},
             "legacy_rendered": {"authorize_github", "review_setup", "buy_points", "choose_articles_page"}}
    facts.update(changes)
    return facts


class RealTreeTests(SimpleTestCase):
    def test_the_real_tree_passes_every_registry_rule(self):
        self.assertEqual(checker.registry_rule_errors(ROOT), [])

    def test_committed_ratchets_are_exact(self):
        rules = json.loads((ROOT / checker.RULES_PATH).read_text())
        registry = json.loads((ROOT / "contracts/failure-codes.json").read_text())
        self.assertEqual(rules["ratchets"]["stop_codes"], checker.stop_code_count(registry))
        self.assertEqual(rules["ratchets"]["render_gaps"], len(rules["render_gaps"]))
        self.assertEqual(rules["ratchets"]["grandfathered_sweeps"],
                         sum(1 for entry in rules["sweeps"].values() if entry.get("grandfathered")))

    def test_the_page_choice_ask_is_rendered_in_both_journeys(self):
        """Plan 4.7: ask the founder for the page; the pre-P2 view asks, and both journeys render it."""
        registry = json.loads((ROOT / "contracts/failure-codes.json").read_text())
        overlay = json.loads((ROOT / checker.OVERLAY_PATH).read_text())
        self.assertIn("articles_page_choice_required", checker.ask_actions(registry, overlay)["choose_articles_page"])
        gaps = checker.render_gaps(registry, overlay, checker.journey_facts(ROOT))
        self.assertFalse({gap for gap in gaps if gap[1] == "choose_articles_page"})

    def test_the_rules_file_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(checker.registry_rule_errors(Path(directory)),
                             ["contracts/backend-registry-rules.json: missing; the F4 registry rules need it"])


class FounderStateRuleTests(SimpleTestCase):
    """Rule 3: every code resolves to one founder state, and every ask is rendered."""

    def test_every_state_resolves(self):
        registry = _registry()
        self.assertEqual([checker.founder_state(registry, entry)[0] for entry in registry["codes"]],
                         ["ask", "working", "fixing"])
        self.assertEqual(checker.founder_state_errors(registry, _facts()), [])

    def test_working_copy_paired_with_a_non_retryable_stop_fails(self):
        registry = _registry()
        registry["codes"][2]["founder_copy_id"] = "working_website_update"
        errors = checker.founder_state_errors(registry, _facts(copy_strings=_facts()["copy_strings"] | {"working_website_update"}))
        self.assertEqual(errors, ["STOP_CODE: no single founder state (working copy paired with a non-retryable stop)"])

    def test_copy_no_journey_renders_fails(self):
        registry = _registry()
        registry["codes"][1]["founder_copy_id"] = "working_queue"
        self.assertEqual(checker.founder_state_errors(registry, _facts()), [
            "WORKING_CODE: founder copy working_queue is not rendered by content_factory/founder_journey_p2.py"])

    def test_an_ask_the_legacy_journey_does_not_render_fails(self):
        registry = _registry()
        registry["codes"][0].update(action_id="buy_points", founder_copy_id="ask_buy_points")
        facts = _facts(legacy_rendered={"authorize_github", "review_setup"})
        self.assertEqual(checker.render_gaps(registry, {}, facts), {("legacy_journey", "buy_points")})
        errors = checker.render_gap_errors(registry, {}, facts, [], 0)
        self.assertEqual(len(errors), 1)
        self.assertIn("ask buy_points: not rendered in the legacy journey", errors[0])
        reviewed = [{"journey": "legacy_journey", "action": "buy_points", "reason": "A reviewed reason long enough."}]
        self.assertEqual(checker.render_gap_errors(registry, {}, facts, reviewed, 1), [])

    def test_an_ask_the_new_journey_does_not_send_fails(self):
        registry = _registry()
        facts = _facts(founder_asks={"approve_setup"})
        self.assertEqual(checker.render_gaps(registry, {}, facts), {("new_journey", "authorize_github")})

    def test_a_pre_p2_ask_counts_and_aliases_resolve_first(self):
        registry = _registry()
        overlay = {"WORKING_CODE": {"failure_class": "needs_user", "action_id": "review_setup"}}
        self.assertEqual(checker.ask_actions(registry, overlay)["approve_setup"], {"WORKING_CODE"})
        self.assertEqual(checker.render_gaps(registry, overlay, _facts()), set())
        missing = _facts(legacy_rendered={"authorize_github"})
        self.assertEqual(checker.render_gaps(registry, overlay, missing), {("legacy_journey", "approve_setup")})

    def test_an_id_less_legacy_ask_uses_the_fallback_card(self):
        registry = _registry()
        registry["codes"][0].update(action_id="approve_setup")
        facts = _facts(legacy_wire={"approve_setup": None}, legacy_rendered=set())
        self.assertEqual(checker.render_gaps(registry, {}, facts), set())

    def test_a_render_gap_that_is_fixed_is_stale(self):
        reviewed = [{"journey": "legacy_journey", "action": "authorize_github", "reason": "A reviewed reason long enough."}]
        errors = checker.render_gap_errors(_registry(), {}, _facts(), reviewed, 0)
        self.assertIn("contracts/backend-registry-rules.json: stale render gap legacy_journey authorize_github "
                      "(it is rendered now)", errors)


class OperatorOnlyRuleTests(SimpleTestCase):
    """Rule 5: an operator-only remedy never reaches a founder as an ask."""

    def test_an_operator_only_code_that_asks_the_founder_fails(self):
        registry = _registry()
        self.assertEqual(checker.operator_only_errors(registry, {}, ["STOP_CODE"]), [])
        overlay = {"STOP_CODE": {"failure_class": "needs_user", "action_id": "review_setup"}}
        self.assertEqual(checker.operator_only_errors(registry, overlay, ["STOP_CODE"]),
                         ["STOP_CODE: an operator-only remedy is a founder ask in the pre_p2 view"])
        self.assertEqual(checker.operator_only_errors(registry, {}, ["ASK_CODE"]),
                         ["ASK_CODE: an operator-only remedy is a founder ask in the v2 view"])


class StopRatchetRuleTests(SimpleTestCase):
    """Rule 6: the stop-code count only falls."""

    def test_a_new_stop_code_breaks_the_ratchet(self):
        registry = _registry()
        registry["codes"].append(dict(deepcopy(registry["codes"][2]), code="SECOND_STOP"))
        self.assertEqual(checker.ratchet_count_errors("stop_codes", checker.stop_code_count(registry), 1),
                         ["stop_codes: 2 exceeds the ratchet of 1; it may only fall"])
        self.assertEqual(checker.ratchet_regressions({"stop_codes": 313}, {"stop_codes": 312}),
                         ["ratchet stop_codes rose from 312 to 313; ratchets may only fall"])
        self.assertEqual(checker.ratchet_regressions({"stop_codes": 311}, {"stop_codes": 312}), [])

    def test_ratchet_base_reads_the_committed_rules_from_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                subprocess.run(["git", "-C", directory, *args], check=True, capture_output=True)
            git("init", "-q")
            git("config", "user.email", "fixture@example.invalid")
            git("config", "user.name", "fixture")
            (root / "contracts").mkdir()
            (root / checker.RULES_PATH).write_text(json.dumps({"ratchets": {"stop_codes": 4}}))
            git("add", ".")
            git("commit", "-q", "-m", "base")
            self.assertEqual(checker.base_ratchets(root, "HEAD"), {"stop_codes": 4})
            with self.assertRaises(checker.ContractCheckError):
                checker.base_ratchets(root, "no-such-ref")


SWEEPS_SOURCE = '''
SWEEPS = (
    ("old_sweep", "content_factory.old_sweep", "run"),
    ("new_sweep", "content_factory.new_sweep", "run"),
)
'''


class SweepRuleTests(SimpleTestCase):
    """Rule 7: a new sweep or alert rule ships with a shadow flag and a history horizon."""

    def tree(self, *, new_sweep_source):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for path in (checker.DEPLOY_VALIDATOR_PATH,):
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text('FLAG_KEYS = ("ARTICLES_NEW_SWEEP_SHADOW_ENABLED",)\n'
                                     'ALERTS_SINCE = "ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE"\n')
        (root / "mlai").mkdir()
        (root / checker.SETTINGS_PATH).write_text(
            "ARTICLES_NEW_SWEEP_SHADOW_ENABLED = False\nARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE = '2026-09-29'\n")
        (root / "content_factory").mkdir()
        (root / checker.SWEEP_SOURCES["self_heal"]).write_text(SWEEPS_SOURCE)
        (root / checker.SWEEP_SOURCES["founder_journey"]).write_text("SWEEPS = ()\n")
        (root / checker.SWEEP_SOURCES["alert_rule"]).write_text("GLOBAL_RULES = {}\n")
        (root / "content_factory/old_sweep.py").write_text("def run(now=None):\n    return {}\n")
        (root / "content_factory/new_sweep.py").write_text(new_sweep_source)
        return root

    OLD = {"self_heal:old_sweep": {"grandfathered": True, "reason": "Shipped before rule 7 with a plain switch."}}

    def test_an_undeclared_new_sweep_fails(self):
        root = self.tree(new_sweep_source="def run(now=None):\n    return {}\n")
        self.assertEqual(checker.sweep_errors(root, dict(self.OLD), 1), [
            "self_heal:new_sweep: a new sweep or alert rule must ship with a shadow flag and a history horizon "
            "(contracts/backend-registry-rules.json sweeps)"])

    def test_a_new_sweep_must_read_a_release_installable_shadow_flag_and_horizon(self):
        source = ("from django.conf import settings\nfrom .founder_journey_alerts import alerts_since\n\n"
                  "def run(now=None):\n    shadow = settings.ARTICLES_NEW_SWEEP_SHADOW_ENABLED\n"
                  "    return {'since': alerts_since(), 'shadow': shadow}\n")
        root = self.tree(new_sweep_source=source)
        declared = dict(self.OLD, **{"self_heal:new_sweep": {
            "shadow_flag": "ARTICLES_NEW_SWEEP_SHADOW_ENABLED", "history_horizon": "ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE",
            "reason": "Reports what it would do while the shadow flag is on."}})
        self.assertEqual(checker.sweep_errors(root, declared, 1), [])
        declared["self_heal:new_sweep"]["shadow_flag"] = "ARTICLES_NOT_INSTALLABLE_ENABLED"
        self.assertIn("self_heal:new_sweep: shadow_flag must be a release-installable flag "
                      "(scripts/validate_articles_deploy_config.py FLAG_KEYS) defined in mlai/settings.py",
                      checker.sweep_errors(root, declared, 1))
        unread = self.tree(new_sweep_source="def run(now=None):\n    return {}\n")
        declared["self_heal:new_sweep"]["shadow_flag"] = "ARTICLES_NEW_SWEEP_SHADOW_ENABLED"
        self.assertEqual(checker.sweep_errors(unread, declared, 1), [
            "self_heal:new_sweep: the sweep's module does not read its shadow flag ARTICLES_NEW_SWEEP_SHADOW_ENABLED",
            "self_heal:new_sweep: the sweep's module does not read its history horizon ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE"])

    def test_grandfathering_a_new_sweep_breaks_the_ratchet(self):
        root = self.tree(new_sweep_source="def run(now=None):\n    return {}\n")
        declared = dict(self.OLD, **{"self_heal:new_sweep": {"grandfathered": True, "reason": "Trying to skip rule 7."}})
        self.assertEqual(checker.sweep_errors(root, declared, 1),
                         ["grandfathered_sweeps: 2 exceeds the ratchet of 1; it may only fall"])

    def test_a_removed_sweep_is_stale(self):
        root = self.tree(new_sweep_source="def run(now=None):\n    return {}\n")
        declared = dict(self.OLD, **{"self_heal:gone": {"grandfathered": True, "reason": "No longer scheduled anywhere."},
                                     "self_heal:new_sweep": {"grandfathered": True, "reason": "Placeholder reason."}})
        self.assertIn("contracts/backend-registry-rules.json: stale sweep self_heal:gone",
                      checker.sweep_errors(root, declared, 2))
