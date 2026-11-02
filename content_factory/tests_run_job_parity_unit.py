"""Read cutover requires non-empty coverage, not merely zero field mismatches.

Run with scripts/test_without_database.py. These tests never query a database;
the Job/run comparison itself is covered by tests.test_run_job_fold.
"""
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from .run_job_backfill import CompareReport
from .job_mirror import JOB_IDENTITY_COLUMNS, JOB_TO_RUN_FIELDS, missing_job_identity_fields
from .job_view import IncompleteRunJobIdentity, RunJobView


def matching_report(count=3):
    report = CompareReport()
    report.counts.update(jobs_compared=count, matched=count)
    return report


class ReadCutoverParityTests(SimpleTestCase):
    def test_nonempty_complete_matching_population_passes(self):
        self.assertEqual(matching_report().read_cutover_blockers(), {})

    def test_empty_population_is_not_a_parity_pass(self):
        self.assertEqual(CompareReport().read_cutover_blockers(), {"empty_population": 1})

    def test_every_unmatched_category_blocks_even_without_field_mismatches(self):
        for category in ("missing_run", "unfilled_run", "incomplete_identity", "orphan_token_jobs", "ambiguous_token_jobs"):
            with self.subTest(category=category):
                report = matching_report()
                report.counts["matched"] -= 1
                report.counts[category] = 1
                self.assertEqual(report.read_cutover_blockers(), {category: 1})

    def test_mismatched_jobs_and_fields_both_block(self):
        report = matching_report()
        report.counts["jobs_with_mismatch"] = 2
        report.mismatches.update(billing_status=2, slack_thread_ts=1)
        self.assertEqual(report.read_cutover_blockers(), {"jobs_with_mismatch": 2, "field_mismatches": 3})

    def test_field_mismatch_blocks_even_without_job_counter(self):
        report = matching_report()
        report.mismatches["billing_amount"] = 1
        self.assertEqual(report.read_cutover_blockers(), {"field_mismatches": 1})

    def test_unclassified_or_double_counted_jobs_cannot_pass(self):
        for matched in (2, 4):
            with self.subTest(matched=matched):
                report = matching_report()
                report.counts["matched"] = matched
                self.assertEqual(report.read_cutover_blockers(), {"unaccounted_jobs": 1})

    def test_uncopied_identity_is_explicit_and_never_uses_run_authority_as_a_fallback(self):
        run = SimpleNamespace(run_id="synthetic", legacy_job_id="", domain="authority", slack_user_id="actor")
        self.assertEqual(missing_job_identity_fields(run), JOB_IDENTITY_COLUMNS)
        with self.assertRaises(IncompleteRunJobIdentity):
            RunJobView(run)

    def test_copied_empty_identity_strings_are_complete_and_preserved(self):
        values = {column: None for column in JOB_TO_RUN_FIELDS.values()}
        values.update(job_domain="", job_slack_user_id="", job_created_at="created", job_updated_at="updated")
        run = SimpleNamespace(run_id="synthetic", legacy_job_id="", domain="authority", slack_user_id="actor", **values)
        self.assertEqual(missing_job_identity_fields(run), ())
        view = RunJobView(run)
        self.assertEqual((view.domain, view.slack_user_id, view.created_at, view.updated_at), ("", "", "created", "updated"))


class CompareCommandTests(SimpleTestCase):
    def compare(self, report, *args):
        out = StringIO()
        with patch("content_factory.management.commands.compare_run_job_fields.compare_run_job_fields",
                   return_value=report) as compare:
            call_command("compare_run_job_fields", *args, stdout=out)
        return out.getvalue(), compare

    def test_default_remains_report_only_with_same_lines(self):
        report = CompareReport()
        output, compare = self.compare(report)
        self.assertEqual(output, "\n".join(report.lines()) + "\n")
        compare.assert_called_once_with(batch_size=500, updated_since=None)

    def test_check_passes_for_complete_population(self):
        output, _ = self.compare(matching_report(), "--check")
        self.assertIn("Read cutover parity passed for selected population.", output)

    def test_check_fails_with_counts_only_and_still_prints_full_report(self):
        report = matching_report()
        report.counts.update(matched=2, orphan_token_jobs=1)
        out = StringIO()
        with patch("content_factory.management.commands.compare_run_job_fields.compare_run_job_fields",
                   return_value=report):
            with self.assertRaisesMessage(CommandError, "selected population: orphan_token_jobs=1"):
                call_command("compare_run_job_fields", "--check", stdout=out)
        self.assertEqual(out.getvalue(), "\n".join(report.lines()) + "\n")

    def test_check_fails_for_empty_population(self):
        with self.assertRaisesMessage(CommandError, "empty_population=1"):
            self.compare(CompareReport(), "--check")

    @override_settings(TIME_ZONE="Australia/Melbourne", USE_TZ=True)
    def test_date_filter_is_passed_to_comparison_and_does_not_waive_empty_check(self):
        report = matching_report()
        _, compare = self.compare(report, "--check", "--updated-since", "2026-11-02", "--batch-size", "7")
        when = compare.call_args.kwargs["updated_since"]
        self.assertEqual((when.year, when.month, when.day, when.hour), (2026, 11, 2, 0))
        self.assertEqual(when.utcoffset().total_seconds(), 11 * 3600)
        self.assertEqual(compare.call_args.kwargs["batch_size"], 7)
        with self.assertRaisesMessage(CommandError, "empty_population=1"):
            self.compare(CompareReport(), "--check", "--updated-since", "2026-11-02")

    def test_bad_options_fail_before_comparing(self):
        with patch("content_factory.management.commands.compare_run_job_fields.compare_run_job_fields") as compare:
            for args in (("--batch-size", "0"), ("--updated-since", "2026-11-02T12:00:00Z")):
                with self.subTest(args=args), self.assertRaises(CommandError):
                    call_command("compare_run_job_fields", *args, stdout=StringIO())
            compare.assert_not_called()
