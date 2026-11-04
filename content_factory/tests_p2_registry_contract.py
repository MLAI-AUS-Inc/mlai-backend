"""P2.1 backend registry (be-registry): the vendored bytes, both views, and the policy reads.

Covers the contract's be-registry acceptance lines for the registry-hash test,
L4 (the four IF-P2-03 equivalence tests), refund equivalence, L5 (wire ids),
the registry half of "an unknown code is Working on it", and the checker's
IF-P2-01 and IF-P2-27 checks.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from . import registry_policy
from . import website_failure_contract as contract
from .self_heal_testing import ALL_FLAGS_ON, make_named_legacy_org, make_p2_org

ROOT = Path(__file__).resolve().parents[1]
# cf-registry's bytes, checked with cmp when vendored. Merge provenance is checked remotely.
VENDORED_SHA256 = {
    "contracts/failure-codes.json": "801630e9199bfb659d8efc3a1d7a1148c3573d78ce6a95db303d0858457a175f",
    "contracts/failure-envelope-v1.schema.json": "de30edc7b4f5996c639d13a84225dd64a106861abb8e5cfd60ee42bc2f5e2c74",
    "contracts/setup-delivery-v1.schema.json": "6a604e610716ba45c6c1e8491daae94cc51ece99e8e3d6c693ac378674581069",
    "content_factory/failure_registry_pre_p2.json": "f8ca80a13c17497fc2dab0f2bfc2cb3f8c08840c09737e6082d52022ebbd5cc2",
    "content_factory/tests/fixtures/p2/failure-codes.pre-p2.json": "5dc161ae1f02212f3898ec0a2bc4bee347ccbc0cf8c26cf640ced545dd4d8b3e",
}
FIXTURE_PATH = ROOT / "content_factory/tests/fixtures/p2/failure-codes.pre-p2.json"
OVERLAY_PATH = ROOT / "content_factory/failure_registry_pre_p2.json"
ALIASES = ("merge_pr", "review_github_app_permissions", "review_github_permissions", "review_setup")
RETIRED = ("choose_articles_page", "configure_website", "deploy_site", "provide_credentials", "remove_setup_example")


def _sha(path) -> str:
    return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_bytes())


def _fixture_by_code() -> dict:
    """The pre-P2 registry's ``by_code``, built the way main's loader built it."""
    by_code = {}
    for entry in _fixture()["codes"]:
        by_code[entry["code"]] = entry
        for alias in entry.get("aliases", []):
            by_code[alias] = entry
    return by_code


def _all_names() -> set:
    return set(contract.load_registry()["by_code_v2"]) | set(_fixture_by_code())


class VendoredRegistryTests(SimpleTestCase):
    """Acceptance: the backend registry-hash test passes."""

    def test_lock_digests_verify_and_vendor_check_passes(self):
        from scripts.check_failure_contract import ARTIFACTS, verify_vendor
        lock = json.loads((ROOT / "contracts/failure-codes.lock.json").read_text())
        self.assertRegex(lock["source_revision"], r"^[0-9a-f]{40}$")
        self.assertEqual(set(lock["files"]), set(ARTIFACTS))
        for name, digest in lock["files"].items():
            with self.subTest(name=name):
                self.assertEqual(_sha(name), digest)
        verify_vendor(ROOT)

    def test_vendored_files_equal_cf_registry_bytes(self):
        lock = json.loads((ROOT / "contracts/failure-codes.lock.json").read_text())
        for name, digest in VENDORED_SHA256.items():
            with self.subTest(name=name):
                self.assertEqual(_sha(name), digest)
                if name in lock["files"]:
                    self.assertEqual(lock["files"][name], digest)

    def test_the_checker_passes_on_this_tree(self):
        from scripts.check_failure_contract import check_emitters
        errors, rows = check_emitters(ROOT)
        self.assertEqual(errors, [])
        self.assertTrue(rows)


class PreP2ViewEquivalenceTests(SimpleTestCase):
    """L4, IF-P2-03 test (1): by_code is the pre-P2 registry, code for code."""

    def test_by_code_matches_the_pre_p2_fixture_for_every_code(self):
        by_code = contract.load_registry()["by_code"]
        for name, old in _fixture_by_code().items():
            with self.subTest(code=name):
                entry = by_code[name]
                self.assertEqual((entry["failure_class"], entry["retry_policy"], entry.get("action_id"),
                                  entry["legacy"]["retryable"]),
                                 (old["failure_class"], old["retry_policy"], old.get("action_id"),
                                  old["legacy"]["retryable"]))

    def test_codes_cf_already_emits_stay_unregistered_in_the_pre_p2_view(self):
        registry = contract.load_registry()
        overlay = json.loads(OVERLAY_PATH.read_bytes())
        fixture_names = set(_fixture_by_code())
        new_in_overlay = {name for name in overlay if name not in {"actions", "source_revision"} and name not in fixture_names}
        self.assertEqual(new_in_overlay, set(contract.EMITTED_BEFORE_P2))
        for name in contract.EMITTED_BEFORE_P2:
            with self.subTest(code=name):
                self.assertNotIn(name, registry["by_code"])
                self.assertIn(name, registry["by_code_v2"])

    def test_pre_p2_action_labels_are_kept_for_emission(self):
        registry = contract.load_registry()
        self.assertEqual(registry["actions_pre_p2"], _fixture()["actions"])
        failure = contract.classify_failure("ARTICLE_SYSTEM_SETUP_REQUIRED", "plan")
        self.assertEqual(failure["action"]["id"], "review_setup")
        self.assertEqual(failure["action"]["label"], "Review setup")


class EffectiveEntryEquivalenceTests(TestCase):
    """L4, IF-P2-03 test (2): with every P2 flag on, a legacy org reads exactly by_code."""

    @override_settings(**ALL_FLAGS_ON)
    def test_every_code_reads_the_pre_p2_view_for_a_legacy_org(self):
        org = make_named_legacy_org("talathrive").organization
        by_code = contract.load_registry()["by_code"]
        for name in sorted(_all_names()):
            with self.subTest(code=name):
                self.assertEqual(registry_policy.effective_entry(name, org), by_code.get(name))

    @override_settings(**ALL_FLAGS_ON)
    def test_a_p2_org_reads_the_p2_view_only_with_the_policy_flag(self):
        org = make_p2_org().organization
        registry = contract.load_registry()
        self.assertEqual(registry_policy.effective_entry("SCAFFOLD_PREP_FAILED", org)["retry_policy"], "switch_channel")
        with override_settings(ARTICLES_FAILURE_POLICY_V2_ENABLED=False):
            self.assertEqual(registry_policy.effective_entry("SCAFFOLD_PREP_FAILED", org),
                             registry["by_code"]["SCAFFOLD_PREP_FAILED"])
        self.assertEqual(registry_policy.effective_entry("SCAFFOLD_PREP_FAILED"), registry["by_code"]["SCAFFOLD_PREP_FAILED"])

    def test_flags_off_a_p2_shaped_org_reads_the_pre_p2_view(self):
        org = make_p2_org().organization
        by_code = contract.load_registry()["by_code"]
        for name in sorted(_all_names()):
            with self.subTest(code=name):
                self.assertEqual(registry_policy.effective_entry(name, org), by_code.get(name))


class DirectReaderEquivalenceTests(SimpleTestCase):
    """L4, IF-P2-03 test (3): every direct by_code reader gives the pre-P2 answer, flags off and all on.

    The readers are ``article_publication_evidence.unrecoverable_refund_code``,
    ``run_events._registered_failure`` and ``website_failure_classes.legacy_failure_class``
    (the incident guards read no registry). Every pre-P2 code and every code a
    pre-P2 path already emits is compared with the answer on the pre-P2 fixture.
    """

    def _fixture_registry(self):
        """``load_registry`` as main had it: the pre-P2 ``by_code`` and nothing else changed."""
        fixture = {**contract.load_registry(), "by_code": _fixture_by_code()}
        return lambda: fixture

    def _refund_run(self, code):
        from types import SimpleNamespace
        return SimpleNamespace(
            workflow="article_generation", status="failed", resume_available=False,
            run_request={"roo_points_billing_status": "charged"},
            result={"failure": {"code": code, "requires_user_action": True, "retryable": False}})

    def _readers(self, code):
        from . import article_publication_evidence as evidence
        from .run_events import _registered_failure
        from .website_failure_classes import legacy_failure_class
        with patch("content_factory.billing_lineage.settlement_owed_request", return_value=True), \
                patch.object(evidence, "restart_reused_charge", return_value=False), \
                patch.object(evidence, "publication_state", return_value="none"), \
                patch.object(evidence, "useful_delivery", return_value=False), \
                patch("content_factory.incident_guards.delivered_content", return_value=False):
            refund = evidence.unrecoverable_refund_code(self._refund_run(code))
        return (refund, _registered_failure({"code": code, "failure_class": "repairable"}),
                legacy_failure_class(code), legacy_failure_class(code, retryable=True))

    def _compare(self):
        names = sorted(set(_fixture_by_code()) | set(contract.EMITTED_BEFORE_P2))
        today = {}
        with patch.object(contract, "load_registry", self._fixture_registry()):
            for name in names:
                today[name] = self._readers(name)
        for name in names:
            with self.subTest(code=name):
                self.assertEqual(self._readers(name), today[name])

    def test_flags_off(self):
        self._compare()

    @override_settings(**ALL_FLAGS_ON)
    def test_all_flags_on(self):
        self._compare()


class EnvelopeValidationTests(SimpleTestCase):
    """L4, IF-P2-03 test (4) and the R5 rule."""

    def _envelope(self, code, *, failure_class=None, action_id=None):
        failure = contract.classify_failure(code, "plan")
        if failure_class is not None:
            failure = {**failure, "failure_class": failure_class, "retryable": failure_class == "transient"}
        if action_id is not None:
            label = contract._action_label(action_id) or "Continue"
            failure = {**failure, "action": {"id": action_id, "label": label, "url": None}}
        return failure

    def test_pre_release_alias_envelopes_validate(self):
        for code, action_id in (("ARTICLE_SYSTEM_SETUP_REQUIRED", "review_setup"), ("manual_merge_required", "merge_pr"),
                                ("github_workflow_permission_required", "review_github_permissions"),
                                ("github_workflow_permission_required", "review_github_app_permissions")):
            with self.subTest(action=action_id):
                failure = self._envelope(code, failure_class="needs_user", action_id=action_id)
                self.assertEqual(contract.validate_failure(failure)["action"]["id"], action_id)

    def test_pre_release_retired_id_envelopes_validate(self):
        for code, action_id in (("articles_page_choice_required", "choose_articles_page"),
                                ("SETUP_EXAMPLE_CLEANUP_REQUIRED", "remove_setup_example")):
            with self.subTest(action=action_id):
                failure = self._envelope(code)
                self.assertEqual(failure["action"]["id"], action_id)
                self.assertEqual(contract.validate_failure(failure), failure)
        # Ids no code names any more arrive only on envelopes this registry no longer
        # knows; they keep their class and action through the drift path, as today.
        for action_id in ("configure_website", "deploy_site", "provide_credentials"):
            with self.subTest(action=action_id):
                base = self._envelope("ARTICLE_SYSTEM_SETUP_REQUIRED")
                failure = {**base, "code": "FUTURE_SETUP_CODE",
                           "action": {"id": action_id, "label": contract._action_label(action_id), "url": None}}
                with self.assertLogs("content_factory.website_failure_contract", "WARNING"):
                    validated = contract.validate_failure(failure)
                self.assertEqual(validated["action"]["id"], action_id)

    def test_canonical_id_envelopes_validate(self):
        for code, action_id in (("ARTICLE_SYSTEM_SETUP_REQUIRED", "approve_setup"),
                                ("manual_merge_required", "approve_on_github"),
                                ("github_workflow_permission_required", "authorize_github"),
                                ("COMPANY_CONTEXT_INCOMPLETE", "describe_customer")):
            with self.subTest(code=code):
                failure = self._envelope(code, failure_class="needs_user", action_id=action_id)
                self.assertEqual(contract.validate_failure(failure)["action"]["id"], action_id)

    def test_an_action_neither_view_names_still_raises(self):
        for code, action_id in (("ARTICLE_SYSTEM_SETUP_REQUIRED", "buy_points"),
                                ("manual_merge_required", "review_setup"),
                                ("COMPANY_CONTEXT_INCOMPLETE", "approve_on_github")):
            with self.subTest(code=code):
                failure = self._envelope(code, failure_class="needs_user", action_id=action_id)
                with self.assertRaisesMessage(contract.FailureContractError, "invalid_failure_action"):
                    contract.validate_failure(failure)


class RefundEquivalenceTests(SimpleTestCase):
    """Every code today's backend refunds has ``refund: true``."""

    def test_no_delivery_refund_codes(self):
        from .incident_guards import NO_DELIVERY_REFUND_CODES
        by_code = contract.load_registry()["by_code_v2"]
        for code in sorted(NO_DELIVERY_REFUND_CODES):
            with self.subTest(code=code):
                self.assertTrue(registry_policy.refund_on_no_delivery(code))
                for name in (code, code.lower()):
                    if name in by_code:
                        self.assertIs(by_code[name]["refund"], True)

    def test_stop_contract_codes_without_an_action(self):
        registry = contract.load_registry()
        checked = 0
        for name, entry in registry["by_code"].items():
            if entry["retry_policy"] == "stop_contract" and entry.get("action_id") is None:
                checked += 1
                with self.subTest(code=name):
                    self.assertIs(registry["by_code_v2"][name]["refund"], True)
                    self.assertTrue(registry_policy.refund_on_no_delivery(name))
        self.assertGreater(checked, 0)

    def test_unknown_and_missing_codes_refund(self):
        self.assertTrue(registry_policy.refund_on_no_delivery(None))
        self.assertTrue(registry_policy.refund_on_no_delivery("NOT_A_CODE"))
        no_charge = [entry["code"] for entry in contract.load_registry()["codes"]
                     if entry["failure_class"] == "needs_user" and entry.get("action_id") in {"buy_points", "agree_terms"}]
        self.assertTrue(no_charge)
        for code in no_charge:
            self.assertFalse(registry_policy.refund_on_no_delivery(code))


class WireActionIdTests(SimpleTestCase):
    """L5: ``wire_action_id`` maps every id as IF-P2-05 says."""

    LEGACY = {"approve_setup": "review_setup", "describe_customer": "review_setup", "agree_terms": "review_setup",
              "approve_on_github": "merge_pr", "authorize_github": "authorize_github", "review_draft": "review_draft",
              "buy_points": "buy_points", "approve_join_request": None}

    def test_flag_off_sends_legacy_ids(self):
        self.assertEqual(registry_policy.LEGACY_WIRE_ACTION_IDS, self.LEGACY)
        for canonical, legacy in self.LEGACY.items():
            with self.subTest(action=canonical):
                self.assertEqual(registry_policy.wire_action_id(canonical), legacy)
        for alias, target in contract.load_registry()["action_aliases"].items():
            with self.subTest(alias=alias):
                self.assertEqual(registry_policy.wire_action_id(alias), self.LEGACY[target])
        for retired in RETIRED:
            with self.subTest(retired=retired):
                self.assertEqual(registry_policy.wire_action_id(retired), retired)
        self.assertIsNone(registry_policy.wire_action_id(None))
        self.assertIsNone(registry_policy.wire_action_id(""))

    @override_settings(MY_STARTUP_CANONICAL_ACTION_IDS_ENABLED=True)
    def test_flag_on_sends_canonical_ids(self):
        for canonical in self.LEGACY:
            with self.subTest(action=canonical):
                self.assertEqual(registry_policy.wire_action_id(canonical), canonical)
        for alias, target in contract.load_registry()["action_aliases"].items():
            with self.subTest(alias=alias):
                self.assertEqual(registry_policy.wire_action_id(alias), target)
        for retired in RETIRED:
            self.assertEqual(registry_policy.wire_action_id(retired), retired)

    def test_canonical_action_id(self):
        self.assertEqual(registry_policy.canonical_action_id("review_setup"), "approve_setup")
        self.assertEqual(registry_policy.canonical_action_id("approve_setup"), "approve_setup")
        self.assertIsNone(registry_policy.canonical_action_id("deploy_site"))
        self.assertIsNone(registry_policy.canonical_action_id("nope"))


class UnknownCodeAndCopyIdTests(TestCase):
    """Acceptance: an injected unknown code produces Working on it (registry half), and copy_id_for."""

    def test_unknown_code_is_working_and_unregistered(self):
        self.assertEqual(registry_policy.founder_copy_id("NOT_A_CODE"), "working")
        self.assertEqual(registry_policy.founder_copy_id(None), "working")
        self.assertFalse(registry_policy.is_registered("NOT_A_CODE"))
        self.assertTrue(registry_policy.is_registered("WEBSITE_SOURCE_CHANGED"))

    @override_settings(**ALL_FLAGS_ON)
    def test_unknown_code_for_a_p2_org_with_the_policy_flag(self):
        org = make_p2_org().organization
        self.assertEqual(registry_policy.copy_id_for("NOT_A_CODE", organization=org, terminal=False, self_heal=None),
                         "working")

    @override_settings(**ALL_FLAGS_ON)
    def test_terminal_items_stop_unless_the_engine_will_act(self):
        from .self_heal_testing import self_heal_receipt
        org = make_p2_org().organization
        self.assertEqual(registry_policy.copy_id_for("GITHUB_TEMPORARILY_UNAVAILABLE", organization=org,
                                                     terminal=True, self_heal=None), "stopped")
        acting = self_heal_receipt(next_at="2026-11-01T00:00:00+00:00")
        self.assertEqual(registry_policy.copy_id_for("NOT_A_CODE", organization=org, terminal=True, self_heal=acting),
                         "working")
        for receipt in (self_heal_receipt(next_at=None), self_heal_receipt(next_at="2026-11-01T00:00:00+00:00", capped=True),
                        self_heal_receipt(mode="observe", next_at="2026-11-01T00:00:00+00:00")):
            self.assertEqual(registry_policy.copy_id_for("NOT_A_CODE", organization=org, terminal=True,
                                                         self_heal=receipt), "stopped")

    @override_settings(**ALL_FLAGS_ON)
    def test_needs_user_codes_give_their_ask(self):
        org = make_p2_org().organization
        self.assertEqual(registry_policy.copy_id_for("COMPANY_CONTEXT_INCOMPLETE", organization=org, terminal=True,
                                                     self_heal=None), "ask_describe_customer")
        self.assertEqual(registry_policy.founder_ask_for_code("manual_merge_required", org), "approve_on_github")

    @override_settings(**ALL_FLAGS_ON)
    def test_legacy_org_and_flag_off_give_none(self):
        legacy = make_named_legacy_org("vyavos").organization
        self.assertIsNone(registry_policy.copy_id_for("NOT_A_CODE", organization=legacy, terminal=False, self_heal=None))
        org = make_p2_org().organization
        with override_settings(ARTICLES_FAILURE_POLICY_V2_ENABLED=False):
            self.assertIsNone(registry_policy.copy_id_for("NOT_A_CODE", organization=org, terminal=False, self_heal=None))

    def test_limits_come_from_the_registry(self):
        limits = registry_policy.limits()
        self.assertEqual(limits["pull_requests_per_org_per_day"], 3)
        self.assertEqual(limits["backoff_minutes"], [1, 2, 5, 10, 20, 40, 60])
        self.assertEqual(limits["max_elapsed_hours"], 24)


class LegacyFailureClassTests(SimpleTestCase):
    """website_failure_classes keeps today's answers for pre-P2 codes and the five early codes."""

    def test_classes_unchanged(self):
        from .website_failure_classes import legacy_failure_class
        self.assertEqual(legacy_failure_class("HOSTED_PREVIEW_UNAVAILABLE"), "transient")
        for name in sorted(contract.EMITTED_BEFORE_P2 - {"HOSTED_PREVIEW_UNAVAILABLE"}):
            with self.subTest(code=name):
                self.assertEqual(legacy_failure_class(name), "repairable")
                self.assertEqual(legacy_failure_class(name, retryable=True), "transient")
        self.assertEqual(legacy_failure_class("SCAN_QUEUE_NOT_STARTED"), "transient")
        self.assertEqual(legacy_failure_class("TERMINAL_CODE_MISSING"), "unsupported")


class CheckerDefectTests(SimpleTestCase):
    """IF-P2-01 and IF-P2-27: the checker names every registry and fragment defect."""

    def _registry(self):
        return deepcopy(json.loads((ROOT / "contracts/failure-codes.json").read_bytes()))

    def test_clean_registry_has_no_v2_errors(self):
        from scripts.check_failure_contract import registry_v2_errors
        self.assertEqual(registry_v2_errors(self._registry()), [])

    def test_each_v2_defect_is_named(self):
        from scripts.check_failure_contract import registry_v2_errors

        def first(registry, predicate):
            return next(entry for entry in registry["codes"] if predicate(entry))

        cases = []
        registry = self._registry()
        first(registry, lambda e: e["failure_class"] == "transient")["founder_copy_id"] = "not_a_copy_id"
        cases.append((registry, "missing_or_unknown_founder_copy_id"))
        registry = self._registry()
        first(registry, lambda e: e["failure_class"] == "transient")["founder_copy_id"] = "ask_review_draft"
        cases.append((registry, "founder_copy_id_class_mismatch"))
        registry = self._registry()
        first(registry, lambda e: True).pop("refund")
        cases.append((registry, "missing_refund_flag"))
        registry = self._registry()
        first(registry, lambda e: e["failure_class"] == "transient")["refund"] = False
        cases.append((registry, "invalid_refund_flag"))
        registry = self._registry()
        first(registry, lambda e: e["failure_class"] == "needs_user")["action_id"] = "review_setup"
        cases.append((registry, "noncanonical_owner_action"))
        registry = self._registry()
        registry["action_aliases"]["review_draft"] = "approve_setup"
        cases.append((registry, "invalid_action_aliases"))
        registry = self._registry()
        registry["retired_actions"] = ["review_draft"]
        cases.append((registry, "invalid_retired_actions"))
        registry = self._registry()
        registry["founder_copy_ids"] = list(reversed(registry["founder_copy_ids"]))
        cases.append((registry, "invalid_founder_copy_ids"))
        registry = self._registry()
        registry["limits"].pop("wall_hours")
        cases.append((registry, "invalid_registry_limits"))
        for registry, defect in cases:
            with self.subTest(defect=defect):
                self.assertTrue(any(defect in error for error in registry_v2_errors(registry)), defect)

    def _fragment_root(self, fragments: dict):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "contracts/failure-emitter-scopes.d").mkdir(parents=True)
        (root / "contracts/failure-emitter-scopes.json").write_bytes((ROOT / "contracts/failure-emitter-scopes.json").read_bytes())
        for name, body in fragments.items():
            (root / "contracts/failure-emitter-scopes.d" / name).write_text(body if isinstance(body, str) else json.dumps(body))
        return root

    def _entry(self, **overrides):
        from scripts.check_failure_contract import site_key
        entry = {"adapter": "nonfailure_category", "allowed_codes_ref": "contracts/failure-codes.json#/codes",
                 "expression": "code", "field": "code", "function": "example", "owner": "backend.example",
                 "path": "content_factory/example.py", "source": "example", "test": "example"}
        entry.update(overrides)
        entry["site_sha256"] = site_key(entry)
        return entry

    def test_fragments_merge_and_each_fragment_defect_is_named(self):
        from scripts.check_failure_contract import load_manifest
        good = self._entry()
        manifest, errors = load_manifest(self._fragment_root({"a.json": {"nonfailure_sites": [good]}}))
        self.assertEqual(errors, [])
        self.assertIn(good, manifest["nonfailure_sites"])
        stale = {**self._entry(function="other"), "function": "renamed"}
        cases = (
            ({"a.json": {"nonfailure_sites": [good]}, "b.json": {"dynamic_adapters": [good]}}, "duplicate site key"),
            ({"a.json": {"constructors": {}}}, "unknown fragment key constructors"),
            ({"a.json": {"coverage": "x"}}, "unknown fragment key coverage"),
            ({"a.json": {"nonfailure_sites": [stale]}}, "stale audit"),
            ({"a.json": "{"}, "invalid JSON"),
            ({"a.json": {"nonfailure_sites": {}}}, "must be a list"),
        )
        for fragments, defect in cases:
            with self.subTest(defect=defect):
                _, errors = load_manifest(self._fragment_root(fragments))
                self.assertTrue(any(defect in error for error in errors), errors)

    def test_a_fragment_audit_whose_site_is_gone_is_stale(self):
        from scripts.check_failure_contract import check_emitters
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for path in [*VENDORED_SHA256, "contracts/failure-codes.lock.json", "contracts/failure-emitter-scopes.json"]:
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_bytes((ROOT / path).read_bytes())
        (root / "contracts/failure-emitter-scopes.json").write_text(json.dumps({"dynamic_adapters": [], "nonfailure_sites": []}))
        (root / "contracts/failure-emitter-scopes.d").mkdir()
        (root / "contracts/failure-emitter-scopes.d/x.json").write_text(json.dumps({"nonfailure_sites": [self._entry()]}))
        errors, _ = check_emitters(root)
        self.assertTrue(any("stale emitter audit" in error for error in errors), errors)

    def test_the_overlay_must_match_the_fixture(self):
        from scripts.check_failure_contract import ContractCheckError, check_overlay
        registry = self._registry()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for path in ("content_factory/failure_registry_pre_p2.json", "content_factory/tests/fixtures/p2/failure-codes.pre-p2.json"):
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_bytes((ROOT / path).read_bytes())
        check_overlay(root, registry)
        overlay = json.loads((root / "content_factory/failure_registry_pre_p2.json").read_text())
        overlay.pop("SCAFFOLD_PREP_FAILED")
        (root / "content_factory/failure_registry_pre_p2.json").write_text(json.dumps(overlay))
        with self.assertRaisesMessage(ContractCheckError, "stale_pre_p2_overlay"):
            check_overlay(root, registry)
        (root / "content_factory/failure_registry_pre_p2.json").unlink()
        with self.assertRaisesMessage(ContractCheckError, "missing_or_unreadable_pre_p2_overlay"):
            check_overlay(root, registry)

    def test_the_loader_fails_closed_without_the_overlay(self):
        contract.load_registry.cache_clear()
        self.addCleanup(contract.load_registry.cache_clear)
        with patch.object(contract, "PRE_P2_OVERLAY_PATH", ROOT / "content_factory/does-not-exist.json"):
            with self.assertRaisesMessage(contract.FailureContractError, "invalid_registry_overlay"):
                contract.load_registry()
        contract.load_registry.cache_clear()
        self.assertIn("by_code_v2", contract.load_registry())
