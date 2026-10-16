"""Original-run accounting preserves authority, measurement coverage and replay."""
from copy import deepcopy
from types import SimpleNamespace
from django.test import SimpleTestCase
from django.utils import timezone
from . import website_cost_usage as costs, website_goal as goals
from .tests_website_goal_unit import scope
from .tests_website_usage_settlement_unit import report


def accounting(value):
    """A producer-shaped report with actual tokens and separate invocation gaps."""
    calls = value["model_calls"]
    return {"version": 1, "model_calls": {"basis": "admitted_calls", "total": calls},
        "tokens": {"basis": "provider_reported", "total": value["tokens"], "known_subtotal": value["tokens"], "measured_calls": calls, "total_calls": calls},
        "provider_seconds": {"basis": "settled_elapsed", "total": value["provider_seconds"], "known_subtotal": value["provider_seconds"], "measured_calls": calls, "total_calls": calls},
        "active_seconds": {"basis": "admitted_activity_elapsed", "total": value["active_seconds"]},
        "model_usd": {"basis": "configured_token_rate_upper_bound", "upper_bound": value["model_usd"]},
        **{key: {"basis": "workflow_step_attempts", "attempts": value[key], "invocations": None} for key in ("builds", "previews")}}


class CostUsageTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.report = report(tokens=17, provider_seconds=0.2, active_seconds=3, model_usd=0.00017)
        self.report["accounting"] = accounting(self.report)

    def merge(self, previous, value):
        return costs.merge(previous, value, run_id="meter-run", now=self.now)

    def verifiers(self, *, builds=3, previews=0, version=1):
        value = deepcopy(self.report)
        value["state_version"] = version
        for key, count in (("builds", builds), ("previews", previews)):
            value["accounting"][key].update(basis=costs.VERIFIER_BASIS, invocations=count)
        return value

    def test_verifier_admissions_and_workflow_attempts_remain_distinct(self):
        recorded = self.merge(None, self.verifiers())
        summary = costs.aggregate([recorded], now=self.now)
        self.assertEqual(recorded["accounting"]["builds"]["attempts"], self.report["builds"])
        self.assertEqual(summary["metrics"]["builds"]["basis"], "isolated_verifier_admissions")
        self.assertEqual(summary["metrics"]["builds"]["total"], 3)
        self.assertEqual(summary["metrics"]["previews"]["total"], 0)
        partial = costs.aggregate([recorded, self.merge(None, self.report)], now=self.now)
        self.assertIsNone(partial["metrics"]["builds"]["total"])
        self.assertEqual(partial["metrics"]["builds"]["knownSubtotal"], 3)
        self.assertEqual(partial["metrics"]["builds"]["measuredRuns"], 1)

    def test_admission_replays_and_older_snapshots_cannot_duplicate_or_replace_counts(self):
        value = self.verifiers()
        first = self.merge(None, value)
        self.assertEqual(self.merge(first, value), first)
        second = self.merge(first, self.verifiers(builds=4, version=2))
        self.assertEqual(self.merge(second, value), second)
        conflict = self.merge(second, self.verifiers(builds=5, version=2))
        self.assertEqual(conflict["incomplete_reason"], "conflicting_snapshot")
        self.assertEqual(conflict["accounting"]["builds"]["invocations"], 4)

    def test_counter_peaks_survive_missing_coverage_and_reject_a_later_reset(self):
        first = self.merge(None, self.verifiers())
        gap = self.merge(first, self.verifiers(builds=None, version=2))
        self.assertEqual(gap["verifier_peaks"]["builds"], 3)
        reset = self.merge(gap, self.verifiers(builds=1, version=3))
        self.assertEqual(reset["incomplete_reason"], "verifier_counter_reset")
        self.assertIsNone(costs.aggregate([reset], now=self.now)["metrics"]["builds"]["total"])
        restored = self.merge(reset, self.verifiers(builds=5, version=4))
        self.assertEqual(restored["verifier_peaks"]["builds"], 5)
        self.assertEqual(restored["accounting"]["builds"]["invocations"], 5)
        self.assertIsNone(restored["incomplete_reason"])

    def test_invalid_admission_values_and_corrupt_peaks_cannot_become_measurements(self):
        for count in (True, -1, "3", float("nan"), float("inf"), 0.5):
            with self.subTest(count=count):
                result = self.merge(None, self.verifiers(builds=count))
                self.assertEqual(result["incomplete_reason"], "invalid_accounting")
                self.assertIsNone(result["accounting"])
        first = self.merge(None, self.verifiers())
        for peaks in ([], {"provider_text": 3}, {"builds": True}, {"builds": None}):
            with self.subTest(peaks=peaks), self.assertRaises(ValueError):
                self.merge({**first, "verifier_peaks": peaks}, self.verifiers(builds=4, version=2))

    def test_settlement_and_replay_keep_exact_measurements_separate_from_peaks(self):
        reserved = self.merge(None, report())
        settled = self.merge(reserved, {**self.report, "state_version": 2, "provider_message": "must-remain-private"})
        self.assertEqual(settled["budget_peaks"]["tokens"], 35_000)
        self.assertEqual(settled["accounting"]["tokens"]["total"], 17)
        self.assertEqual(self.merge(settled, {**self.report, "state_version": 2}), settled)
        self.assertEqual(self.merge(settled, report()), settled)
        self.assertNotIn("must-remain-private", str(settled))

    def test_same_identity_cannot_replace_valid_accounting_and_stays_incomplete(self):
        first = self.merge(None, self.report)
        changed = deepcopy(self.report)
        changed["accounting"]["active_seconds"]["total"] = 2
        conflict = self.merge(first, changed)
        self.assertEqual(conflict["incomplete_reason"], "conflicting_snapshot")
        self.assertEqual(conflict["accounting"], first["accounting"])
        self.assertEqual(self.merge(conflict, self.report), conflict)
        self.assertIsNone(self.merge(conflict, {**self.report, "state_version": 2})["incomplete_reason"])

    def test_invalid_bases_coverage_and_private_fields_never_become_exact_totals(self):
        bad_rows = []
        for metric, field, value in (("tokens", "basis", "reserved"), ("tokens", "total", True),
                ("tokens", "known_subtotal", 18), ("tokens", "measured_calls", 2),
                ("builds", "attempts", True), ("builds", "invocations", 1),
                ("tokens", "provider_text", "must-remain-private")):
            bad = deepcopy(self.report)
            bad["accounting"][metric][field] = value
            bad_rows.append(bad)
        for bad in bad_rows:
            with self.subTest(bad=bad):
                result = self.merge(None, bad)
                self.assertIsNone(result["accounting"])
                self.assertEqual(result["incomplete_reason"], "invalid_accounting")
                self.assertNotIn("must-remain-private", str(result))

    def test_partial_known_and_zero_totals_are_not_confused_with_missing_meters(self):
        measured = self.merge(None, self.report)
        partial = deepcopy(self.report)
        partial["model_calls"] = 2
        partial["accounting"]["model_calls"]["total"] = 2
        for key in ("tokens", "provider_seconds"):
            partial["accounting"][key].update(total=None, total_calls=2)
        incomplete = self.merge(None, partial)
        value = costs.aggregate([measured, incomplete], now=self.now)
        self.assertIsNone(value["metrics"]["tokens"]["total"])
        self.assertEqual(value["metrics"]["tokens"]["knownSubtotal"], 34)
        self.assertEqual(value["metrics"]["tokens"]["measuredRuns"], 1)
        self.assertIsNone(value["metrics"]["builds"]["total"])
        empty = costs.aggregate([], now=self.now)
        self.assertIsNone(empty["metrics"]["tokens"]["knownSubtotal"])
        zero = report(model_calls=0, tokens=0, provider_seconds=0, active_seconds=0, model_usd=0, builds=0)
        zero["accounting"] = accounting(zero)
        value = costs.aggregate([self.merge(None, zero)], now=self.now)
        self.assertEqual(value["metrics"]["tokens"]["total"], 0)
        self.assertEqual(value["metrics"]["model_usd"]["total"], 0)

    def test_legacy_corrupt_and_truncated_history_is_explicit(self):
        legacy = self.merge(None, report())
        summary = costs.aggregate([legacy], now=self.now)
        self.assertEqual(summary["metrics"]["model_calls"]["total"], 1)
        self.assertEqual(summary["metrics"]["model_usd"]["basis"], "configured_token_rate_upper_bound")
        self.assertIsNone(summary["metrics"]["tokens"]["total"])
        self.assertEqual(summary["incompleteRuns"], 1)
        corrupt = deepcopy(self.merge(None, self.report))
        corrupt["accounting"]["tokens"]["total"] = float("nan")
        self.assertIsNone(costs.aggregate([corrupt], now=self.now)["metrics"]["tokens"]["total"])
        truncated = costs.aggregate([self.merge(None, self.report)], now=self.now, truncated=True)
        self.assertIsNone(truncated["metrics"]["tokens"]["total"])
        self.assertEqual(truncated["metrics"]["tokens"]["knownSubtotal"], 17)
        corrupt_state = {**self.merge(None, self.report), "incomplete_reason": []}
        self.assertIsNone(costs.aggregate([corrupt_state], now=self.now)["metrics"]["tokens"]["total"])

    def test_missing_or_invalid_worker_reports_remain_in_coverage_and_replays_are_inert(self):
        known = self.merge(None, self.report)
        for raw in (None, [], {"provider_message": "must-remain-private"}):
            with self.subTest(raw=raw):
                missing = self.merge(None, raw)
                self.assertIsNone(missing["usage"])
                self.assertEqual(self.merge(missing, raw), missing)
                summary = costs.aggregate([known, missing], now=self.now)
                self.assertEqual(summary["recordedRuns"], 2)
                self.assertIsNone(summary["metrics"]["tokens"]["total"])
                self.assertEqual(summary["metrics"]["tokens"]["knownSubtotal"], 17)
                self.assertNotIn("must-remain-private", str(missing))
                recovered = self.merge(missing, self.report)
                self.assertIsNone(recovered["incomplete_reason"])

    def test_original_attribution_rejects_tenant_goal_source_and_scope_changes(self):
        original = scope()
        goal = goals.new_goal("original", original, now=self.now)
        operation = SimpleNamespace(pk="original", connection_id=original["connection_id"], generation=3,
            connection=SimpleNamespace(organization_id=7), payload={"company_id": 9}, receipt={"goal": goal})
        saved = {"goal_budget": {"version": 1, "goal_id": "original", "scope_digest": goals.digest(original)},
            "website_connection_id": "website", "connection_generation": 3, "repository_id": 12,
            "branch": "main", "app_root": "", "deletion_epoch": 0}
        run = SimpleNamespace(run_id="meter-run", organization_id=7, run_request=saved)
        self.assertEqual(costs.attribution(operation, run)["goal_id"], "original")
        for changes in ({"connection_generation": True}, {"deletion_epoch": True}, {"branch": "other"},
                {"app_root": "apps/foreign"}, {"goal_budget": {**saved["goal_budget"], "goal_id": "reusing-goal"}},
                {"goal_budget": {**saved["goal_budget"], "scope_digest": "f" * 64}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                costs.attribution(operation, SimpleNamespace(**{**vars(run), "run_request": {**saved, **changes}}))
        with self.assertRaises(ValueError):
            costs.attribution(operation, SimpleNamespace(**{**vars(run), "organization_id": 8}))
        for key in ("connection_generation", "configuration_revision", "deletion_epoch"):
            corrupted = deepcopy(operation)
            corrupted_run = deepcopy(run)
            corrupted.receipt["goal"]["scope"][key] = False if key == "deletion_epoch" else True
            if key == "connection_generation":
                corrupted.generation = corrupted_run.run_request["connection_generation"] = 1
            corrupted_run.run_request["goal_budget"]["scope_digest"] = goals.digest(corrupted.receipt["goal"]["scope"])
            with self.subTest(scope_key=key), self.assertRaises(ValueError):
                costs.attribution(corrupted, corrupted_run)
