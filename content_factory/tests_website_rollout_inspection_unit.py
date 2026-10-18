"""Real rollout inspection and command chain with only DB/file boundaries faked."""

import json
import os
from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, mock_open, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import website_rollout_inspection as inspection


class RolloutInspectionTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.flags = {name: True for name in inspection.BACKEND_FLAGS}
        self.flags.update(ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"],
            ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
            CONTENT_FACTORY_SETUP_DELIVERY_ENROLLED_ORGANIZATIONS=["7"])
        self.snapshot = {"schema_version": 1, "organization_id": "7", "run_id": "fixture-run",
            "observed_at": self.now.isoformat(), "revision": "a" * 40,
            "flags": {name: True for name in inspection.CF_FLAGS},
            "run_predicates": {name: True for name in inspection.SCOPED_PREDICATES}}

    def report(self, **kwargs):
        return inspection.rollout_report("7", now=self.now, **kwargs)

    def test_defaults_do_not_infer_a_goal_or_worker_enablement(self):
        with override_settings(**{name: False for name in inspection.BACKEND_FLAGS},
                ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=[], ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=[]):
            report = self.report()
        self.assertFalse(report["goal_enrolled"])
        self.assertEqual(report["steps"]["B2"]["state"], "disabled")
        self.assertEqual(report["steps"]["E2"]["state"], "unknown")
        self.assertIsNone(report["content_factory"]["flags"]["CONTENT_FACTORY_ARTICLES_REPRODUCIBLE_BUILDS_ENABLED"])

    def test_runner_flag_and_one_enrollment_list_cannot_report_an_enabled_goal(self):
        with override_settings(**{**self.flags, "ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS": ["70"]}):
            report = self.report()
        self.assertFalse(report["goal_enrolled"])
        self.assertTrue(report["enrollment"]["taxonomy"])
        self.assertFalse(report["enrollment"]["goals"])
        for name in ("B2", "B3", "B4", "C1", "C2", "D1", "article_delivery"):
            self.assertEqual(report["steps"][name]["state"], "disabled")

    def test_taxonomy_policy_is_an_independent_goal_prerequisite(self):
        flags = {**self.flags, "ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED": False}
        with override_settings(**flags):
            report = self.report(cf_snapshot=self.snapshot, run_id="fixture-run")
        self.assertEqual(report["steps"]["B1_observe"]["state"], "configured")
        self.assertEqual(report["steps"]["B1_policy"]["state"], "disabled")
        self.assertEqual(report["steps"]["D1"]["state"], "disabled")

    def test_missing_cf_observation_is_unknown_even_when_backend_is_eligible(self):
        with override_settings(**self.flags):
            report = self.report()
        self.assertTrue(report["steps"]["B5_setup"]["backend_eligible"])
        self.assertEqual(report["steps"]["B5_setup"]["state"], "unknown")
        self.assertEqual(report["content_factory"]["source"], "unobserved")

    def test_exact_recent_observation_reports_configuration_without_claiming_permission(self):
        before = deepcopy(self.snapshot)
        with override_settings(**self.flags):
            report = self.report(cf_snapshot=self.snapshot, run_id="fixture-run")
        self.assertEqual(report["steps"]["B5_setup"]["state"], "configured")
        self.assertEqual(report["steps"]["B4"]["state"], "configured")
        self.assertEqual(report["content_factory"]["source"], "operator_snapshot")
        self.assertIn("not_permission_or_outcome", report["semantics"])
        self.assertEqual(self.snapshot, before)

    def test_wrong_or_absent_run_scope_does_not_borrow_another_runs_predicates(self):
        with override_settings(**self.flags):
            for run_id in (None, "different-run"):
                report = self.report(cf_snapshot=self.snapshot, run_id=run_id)
                self.assertEqual(report["steps"]["B1_observe"]["state"], "unknown")
                self.assertEqual(report["steps"]["B5_setup"]["state"], "unknown")
                self.assertEqual(report["steps"]["E1"]["state"], "configured")

    def test_false_run_predicate_and_disabled_cf_flag_are_not_unknown(self):
        self.snapshot["run_predicates"]["goal_usage_enabled"] = False
        self.snapshot["flags"]["CONTENT_FACTORY_ARTICLE_DELIVERY_V1_ENABLED"] = False
        with override_settings(**self.flags):
            report = self.report(cf_snapshot=self.snapshot, run_id="fixture-run")
        self.assertEqual(report["steps"]["B4"]["state"], "disabled")
        self.assertEqual(report["steps"]["article_delivery"]["state"], "disabled")

    def test_stale_future_foreign_and_malformed_observations_never_report_cf_configuration(self):
        variants = [None, [], "private text", {**self.snapshot, "schema_version": True},
            {**self.snapshot, "observed_at": (self.now - timedelta(seconds=301)).isoformat()},
            {**self.snapshot, "observed_at": (self.now + timedelta(seconds=31)).isoformat()},
            {**self.snapshot, "organization_id": "other"},
            {**self.snapshot, "observed_at": "2026-10-18T03:00:00"}]
        with override_settings(**self.flags):
            for value in variants:
                with self.subTest(value=value):
                    report = self.report(cf_snapshot=value, run_id="fixture-run")
                    self.assertEqual(report["steps"]["C3"]["state"], "unknown")

    def test_snapshot_redaction_and_strict_boolean_types(self):
        secret = "github_pat_rolloutCanary"
        self.snapshot.update(credentials=secret, revision=secret)
        self.snapshot["flags"]["CONTENT_FACTORY_ARTICLES_HOSTED_VERIFICATION_ENABLED"] = secret
        self.snapshot["flags"][secret] = secret
        self.snapshot["run_predicates"]["goal_usage_enabled"] = 1
        with override_settings(**self.flags):
            report = self.report(cf_snapshot=self.snapshot, run_id="fixture-run")
        self.assertNotIn(secret, json.dumps(report))
        self.assertIsNone(report["content_factory"]["revision"])
        self.assertEqual(report["steps"]["C3"]["state"], "unknown")
        self.assertEqual(report["steps"]["B4"]["state"], "unknown")

    def test_setup_delivery_enrollment_is_separate_from_goal_enrollment(self):
        flags = {**self.flags, "CONTENT_FACTORY_SETUP_DELIVERY_ENROLLED_ORGANIZATIONS": "70, 17"}
        with override_settings(**flags):
            report = self.report(cf_snapshot=self.snapshot, run_id="fixture-run")
        self.assertTrue(report["goal_enrolled"])
        self.assertEqual(report["steps"]["B5_setup"]["state"], "disabled")

    def test_f3_reports_the_actual_alert_goal_enrollment_requirement(self):
        from .website_operational_alerts import enabled as alerts_enabled
        flags = {**self.flags, "ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS": []}
        with override_settings(**flags):
            self.assertFalse(alerts_enabled(SimpleNamespace(organization_id=7)))
            report = self.report()
        self.assertEqual(report["steps"]["F3"]["state"], "disabled")
        self.assertFalse(report["steps"]["F3"]["backend_eligible"])

    def test_actual_deploy_app_release_is_full_revision_and_conflicting_fallback_cannot_replace_it(self):
        with patch.dict("os.environ", {"APP_RELEASE": "b" * 40, "APP_COMMIT_SHA": "a" * 40}):
            self.assertEqual(self.report()["backend_revision"], "b" * 40)
        with patch.dict("os.environ", {"APP_RELEASE": "unknown-release", "APP_COMMIT_SHA": "a" * 40}):
            self.assertIsNone(self.report()["backend_revision"])
        with patch.dict("os.environ", {"APP_RELEASE": "", "APP_COMMIT_SHA": "a" * 40}):
            self.assertIsNone(self.report()["backend_revision"])
        with patch.dict("os.environ", {"APP_COMMIT_SHA": "a" * 40}):
            os.environ.pop("APP_RELEASE", None)
            self.assertEqual(self.report()["backend_revision"], "a" * 40)

    def test_command_enforces_read_only_before_company_lookup_and_runs_actual_report(self):
        from founder_tools.models import VibeRaisingCompany
        from .management.commands import inspect_articles_rollout as command
        output, order = StringIO(), []
        database = MagicMock(vendor="postgresql")
        database.cursor.return_value.__enter__.return_value.execute.side_effect = lambda sql: order.append(sql)
        with override_settings(**self.flags), patch.object(command, "database", database), \
            patch.object(command.transaction, "atomic", side_effect=lambda: nullcontext()), \
            patch.object(VibeRaisingCompany.objects, "select_related") as companies:
            companies.side_effect = lambda *args: order.append("company_lookup") or companies.return_value
            companies.return_value.filter.return_value.first.return_value = SimpleNamespace(pk="company", organization_id=7)
            call_command("inspect_articles_rollout", company_id="company", stdout=output)
        self.assertEqual(order, ["SET TRANSACTION READ ONLY", "company_lookup"])
        report = json.loads(output.getvalue())
        self.assertEqual(report["company_id"], "company")
        self.assertTrue(report["goal_enrolled"])
        self.assertEqual(report["content_factory"]["reason"], "not_supplied")

    def test_command_refuses_non_postgres_without_any_company_read(self):
        from founder_tools.models import VibeRaisingCompany
        from .management.commands import inspect_articles_rollout as command
        with patch.object(command, "database", SimpleNamespace(vendor="sqlite")), \
            patch.object(VibeRaisingCompany.objects, "select_related") as companies, self.assertRaises(CommandError):
            call_command("inspect_articles_rollout", company_id="company", stdout=StringIO())
        companies.assert_not_called()

    def test_command_rejects_private_or_oversized_snapshot_without_echoing_it(self):
        from founder_tools.models import VibeRaisingCompany
        from .management.commands import inspect_articles_rollout as command
        for data in ('{"secret":"github_pat_privateCanary"', ' ' * 65537):
            with patch.object(command, "database", SimpleNamespace(vendor="postgresql")), \
                patch("builtins.open", mock_open(read_data=data)), \
                patch.object(VibeRaisingCompany.objects, "select_related") as companies, \
                self.assertRaises(CommandError) as error:
                call_command("inspect_articles_rollout", company_id="company", cf_snapshot="/private/snapshot", stdout=StringIO())
            companies.assert_not_called()
            self.assertEqual(str(error.exception), "CF snapshot must be valid JSON of at most 64 KiB.")
