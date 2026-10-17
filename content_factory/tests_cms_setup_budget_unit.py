"""Real setup budget carry is separate from original CF reports and never resets."""

from copy import deepcopy
from uuid import uuid4

from django.test import SimpleTestCase
from django.utils import timezone

from . import cms_goal_budget as budget
from . import website_goal_budget as shared
from .website_contract import WebsiteAuthorityError


class CmsSetupBudgetTests(SimpleTestCase):
    def setUp(self):
        self.now, self.identifier = timezone.now(), str(uuid4())
        self.original = budget.initial_at(self.now.isoformat())
        self.original["setup_usage"] = {"version": 1, "operation_id": self.identifier, "counter_semantics": shared.BOUND_SEMANTICS,
            "active_seconds": 180, "provider_seconds": 40}
        self.original["budget_used"] = {"active_seconds": 180, "provider_seconds": 40}
        self.report = {"version": 1, "run_id": "actual-original-cf-run", "generation": 0, "state_version": 1,
            **{key: 0 for key in shared.COUNTERS}, "active_seconds": 20, "provider_seconds": 5}

    def validate(self, value):
        return budget.validate_at(value, started_at=self.now.isoformat(), operation_id=self.identifier, now=self.now)

    def test_pre_policy_budget_has_real_accepted_start_without_fabricated_goal_or_cf_run(self):
        value = self.validate(self.original)
        self.assertEqual(value["started_at"], self.now.isoformat())
        self.assertEqual(value["usage_reports"], {})
        self.assertNotIn("goal", value)
        self.assertNotIn("milestones", value)

    def test_actual_cf_snapshot_adds_backend_setup_once_and_replays_do_not_double_count(self):
        after = budget.account_usage(self.original, self.report, run_id=self.report["run_id"])
        self.assertEqual(after["budget_used"]["active_seconds"], 200)
        self.assertEqual(after["budget_used"]["provider_seconds"], 45)
        self.assertEqual(set(after["usage_reports"]), {self.report["run_id"]})
        self.assertEqual(budget.account_usage(after, self.report, run_id=self.report["run_id"]), after)
        self.validate(after)

    def test_newer_and_stale_cf_reports_preserve_original_setup_base(self):
        after = budget.account_usage(self.original, self.report, run_id=self.report["run_id"])
        newer = {**self.report, "state_version": 2, "active_seconds": 30}
        latest = budget.account_usage(after, newer, run_id=self.report["run_id"])
        self.assertEqual(latest["budget_used"]["active_seconds"], 210)
        self.assertEqual(budget.account_usage(latest, self.report, run_id=self.report["run_id"]), latest)
        self.assertEqual(self.original["budget_used"]["active_seconds"], 180)

    def test_unknown_cf_time_stays_unknown_instead_of_becoming_backend_only_or_zero(self):
        report = {**self.report, "active_seconds": None, "provider_seconds": None}
        result = budget.account_usage(self.original, report, run_id=report["run_id"])
        self.assertIsNone(result["budget_used"]["active_seconds"])
        self.assertIsNone(result["budget_used"]["provider_seconds"])
        self.assertEqual(result["setup_usage"], self.original["setup_usage"])
        self.validate(result)

    def test_changed_operation_null_boolean_unknown_fields_or_erased_base_is_not_valid_carry(self):
        for changes in ({"operation_id": str(uuid4())}, {"active_seconds": True}, {"provider_seconds": -1}, {"extra": 1}):
            value = deepcopy(self.original)
            value["setup_usage"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): self.validate(value)
        for changes in ({"setup_usage": None}, {"budget_used": {"active_seconds": 0, "provider_seconds": 40}}):
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): self.validate({**self.original, **changes})

    def test_existing_native_budget_without_setup_carry_keeps_the_shared_meter_behavior(self):
        value = budget.initial_at(self.now.isoformat())
        self.assertEqual(budget.account_usage(value, self.report, run_id=self.report["run_id"]),
            shared.account_usage(value, self.report, run_id=self.report["run_id"]))

    def test_setup_allowances_cannot_reset_an_actual_cf_report_or_admit_a_coerced_counter(self):
        used = budget.account_usage(self.original, self.report, run_id=self.report["run_id"])
        for value, amount in ((used, 180), (self.original, True), (self.original, -1)):
            with self.subTest(amount=amount), self.assertRaises(WebsiteAuthorityError):
                budget.reserve_setup_usage(value, started_at=self.now.isoformat(), operation_id=self.identifier,
                    provider_seconds=amount, now=self.now)

    def access(self, value, *, identifier=None, active=180, provider=180):
        return budget.reserve_access_usage(value, started_at=self.now.isoformat(), operation_id=self.identifier,
            access_operation_id=identifier or str(uuid4()), active_seconds=active, provider_seconds=provider, now=self.now)

    def test_access_allowances_add_to_real_cf_and_setup_usage_without_changing_original_report(self):
        value = budget.account_usage(self.original, self.report, run_id=self.report["run_id"])
        value = self.access(value)
        self.assertEqual(value["budget_used"]["active_seconds"], 380)
        self.assertEqual(value["budget_used"]["provider_seconds"], 225)
        self.assertEqual(set(value["usage_reports"]), {self.report["run_id"]})
        self.assertEqual(value["usage_reports"][self.report["run_id"]]["active_seconds"], 20)
        self.assertEqual(budget.account_usage(value, self.report, run_id=self.report["run_id"]), value)
        self.validate(value)

    def test_distinct_actual_key_jobs_and_claims_are_cumulative_not_fake_cf_runs(self):
        first, second = str(uuid4()), str(uuid4())
        value = self.access(self.original, identifier=first)
        value = self.access(value, identifier=first)
        value = self.access(value, identifier=second)
        self.assertEqual(value["access_usage"][first]["active_seconds"], 360)
        self.assertEqual(value["access_usage"][second]["active_seconds"], 180)
        self.assertEqual(value["budget_used"]["active_seconds"], 720)
        self.assertEqual(value["usage_reports"], {})
        self.assertEqual(value["started_at"], self.original["started_at"])
        self.assertEqual(value["attempts_by_class"], self.original["attempts_by_class"])

    def test_access_bounds_survive_newer_and_stale_original_cf_snapshots(self):
        value = self.access(self.original)
        value = budget.account_usage(value, self.report, run_id=self.report["run_id"])
        newer = {**self.report, "state_version": 2, "active_seconds": 30}
        current = budget.account_usage(value, newer, run_id=self.report["run_id"])
        self.assertEqual(current["budget_used"]["active_seconds"], 390)
        self.assertEqual(budget.account_usage(current, self.report, run_id=self.report["run_id"]), current)

    def test_unknown_original_cf_seconds_stay_unknown_after_real_key_access_and_setup_bounds(self):
        report = {**self.report, "active_seconds": None, "provider_seconds": None}
        value = budget.account_usage(self.original, report, run_id=report["run_id"])
        value = self.access(value)
        self.assertIsNone(value["budget_used"]["active_seconds"])
        self.assertIsNone(value["budget_used"]["provider_seconds"])
        self.validate(value)

    def test_pre_policy_setup_retry_preserves_key_access_bounds_and_unknowns(self):
        value = self.access(self.original)
        value = budget.reserve_setup_usage(value, started_at=self.now.isoformat(), operation_id=self.identifier,
            active_seconds=180, now=self.now)
        self.assertEqual(value["budget_used"]["active_seconds"], 540)
        self.assertEqual(value["budget_used"]["provider_seconds"], 220)
        value["budget_used"]["active_seconds"] = None
        value = budget.reserve_setup_usage(value, started_at=self.now.isoformat(), operation_id=self.identifier,
            active_seconds=180, now=self.now)
        self.assertIsNone(value["budget_used"]["active_seconds"])

    def test_bad_access_sources_counters_or_erased_backend_base_cannot_be_valid_accounting(self):
        value = self.access(self.original)
        identifier = next(iter(value["access_usage"]))
        for changes in ({"version": True}, {"operation_id": str(uuid4())}, {"extra": 1},
                {"active_seconds": True}, {"provider_seconds": -1}, {"counter_semantics": "measured"}):
            changed = deepcopy(value); changed["access_usage"][identifier].update(changes)
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): self.validate(changed)
        for changes in ({"access_usage": None}, {"access_usage": {"not-a-job": value["access_usage"][identifier]}},
                {"budget_used": {"active_seconds": 180, "provider_seconds": 40}}):
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): self.validate({**value, **changes})
        for identifier, amount in (("untrusted", 180), (str(uuid4()), True), (str(uuid4()), -1)):
            with self.subTest(identifier=identifier, amount=amount), self.assertRaises(WebsiteAuthorityError):
                self.access(value, identifier=identifier, active=amount)

    def test_access_sources_are_bounded_and_actual_new_claims_cannot_reset_old_usage(self):
        value = self.original
        for _ in range(64): value = self.access(value)
        with self.assertRaises(WebsiteAuthorityError): self.access(value)
        identifier = next(iter(value["access_usage"]))
        continued = self.access(value, identifier=identifier)
        self.assertEqual(len(continued["access_usage"]), 64)
        self.assertEqual(continued["budget_used"]["active_seconds"], 180 * 66)
