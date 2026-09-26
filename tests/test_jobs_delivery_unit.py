"""Exercise jobs delivery seams without Django setup, credentials or migrations."""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]


def load_seams(path, names, **bindings):
    source = ast.parse((ROOT / path).read_text())
    nodes = [node for node in source.body if (
        isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names
    ) or (isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id in names for target in node.targets
    ))]
    module = ast.Module(body=[ast.ImportFrom(
        module='__future__', names=[ast.alias(name='annotations')], level=0
    ), *nodes], type_ignores=[])
    scope = dict(bindings)
    exec(compile(ast.fix_missing_locations(module), str(ROOT / path), 'exec'), scope)
    return scope


class JobsDeliveryUnitTests(unittest.TestCase):
    def test_jobs_diagnostics_are_enabled_in_production_logging(self):
        config = load_seams('mlai/settings.py', ['LOGGING'], DJANGO_LOG_LEVEL='INFO')['LOGGING']
        self.assertEqual(config['loggers']['jobs']['level'], 'INFO')
        self.assertIn('console', config['loggers']['jobs']['handlers'])

    def config(self, value):
        scope = load_seams('jobs/conf.py', ['JobsSettings', 'DEFAULT_JOBS_SLACK_CHANNEL'],
                           django_settings=SimpleNamespace(JOBS_SLACK_CHANNEL=value))
        return scope['JobsSettings']()

    def pipeline(self, **bindings):
        return load_seams('jobs/services/job_pipeline.py', [
            '_build_run_status', '_summarize_run_issues', 'run_daily_jobs',
            'TERMINAL_COMPLETED_STATUSES',
        ], **bindings)

    def test_legacy_channel_resolves_to_stable_jobs_id(self):
        self.assertEqual(self.config('#jobs-and-founder-matching').slack_jobs_channel, 'C05QE82M2KE')

    def test_blank_configuration_uses_stable_jobs_id(self):
        for value in ('', None, '  '):
            with self.subTest(value=value):
                self.assertEqual(self.config(value).slack_jobs_channel, 'C05QE82M2KE')

    def test_explicit_destinations_are_preserved(self):
        for value in ('C05QE82M2KE', 'C0C230H418U', '#roo-testing', '#jobs'):
            self.assertEqual(self.config(value).slack_jobs_channel, value)

    def test_missing_configuration_uses_stable_jobs_id(self):
        scope = load_seams('jobs/conf.py', ['JobsSettings', 'DEFAULT_JOBS_SLACK_CHANNEL'],
                           django_settings=SimpleNamespace())
        self.assertEqual(scope['JobsSettings']().slack_jobs_channel, 'C05QE82M2KE')

    def test_channel_id_bypasses_name_lookup(self):
        service = Mock()
        service.send_message.return_value = (True, '123.456')
        post = load_seams('jobs/services/slack.py', ['post_slack_message'],
                          settings=self.config('#jobs-and-founder-matching'),
                          _slack_service=lambda: service)['post_slack_message']
        self.assertEqual(post({'text': 'digest'}), (True, None))
        service.get_channel_id_by_name.assert_not_called()
        service.send_message.assert_called_once_with('C05QE82M2KE', 'digest', blocks=None)
        service.reset_mock()
        # The digest formatter supplies the resolved channel explicitly.
        self.assertEqual(post({'text': 'digest', 'channel': 'C05QE82M2KE'}), (True, None))
        service.get_channel_id_by_name.assert_not_called()
        service.send_message.assert_called_once_with('C05QE82M2KE', 'digest', blocks=None)

    def test_empty_matches_and_filtered_matches_have_distinct_outcomes(self):
        build = self.pipeline()['_build_run_status']
        for count, expected in [(0, 'completed_no_results'), (11, 'completed_no_new_picks')]:
            for errors, suffix in [([], ''), (['source unavailable'], '_with_source_errors')]:
                with self.subTest(count=count, errors=errors):
                    self.assertEqual(build(top_jobs_count=0, matched_count=count,
                                           source_errors=errors, slack_error=None), expected + suffix)

    def test_publish_errors_still_take_precedence(self):
        build = self.pipeline()['_build_run_status']
        self.assertEqual(build(top_jobs_count=3, matched_count=11, source_errors=['source'],
                               slack_error='not found'), 'completed_with_publish_errors')

    def test_selected_picks_keep_existing_success_statuses(self):
        build = self.pipeline()['_build_run_status']
        for errors, expected in [([], 'completed'), (['source'], 'completed_with_source_errors')]:
            self.assertEqual(build(top_jobs_count=3, matched_count=11,
                                   source_errors=errors, slack_error=None), expected)

    def test_new_outcomes_prevent_another_same_day_run(self):
        terminal = self.pipeline()['TERMINAL_COMPLETED_STATUSES']
        self.assertIn('completed_no_new_picks', terminal)
        self.assertIn('completed_no_new_picks_with_source_errors', terminal)

    def test_zero_picks_skips_publishers_and_records_counts(self):
        run = SimpleNamespace(run_id='test-run', run_date='2026-09-16', status='queued',
                              save=Mock(), slack_posted_at=None)
        listing_manager = Mock()
        listing_manager.filter.return_value.count.return_value = 11
        slack, notion, alert = Mock(), Mock(), Mock()
        logger = logging.getLogger('jobs-delivery-unit')
        scope = self.pipeline(
            JobRun=SimpleNamespace(objects=Mock(get=Mock(return_value=run))),
            JobListing=SimpleNamespace(objects=listing_manager),
            timezone=SimpleNamespace(now=lambda: 'now'),
            fetch_raw_jobs=Mock(return_value=([{}] * 88, [])),
            _all_attempted_live_sources_failed=Mock(return_value=False),
            insert_matched_jobs=Mock(return_value=[{}] * 11),
            select_top_jobs=Mock(return_value=[]),
            publish_daily_jobs_page=notion, post_slack_message=slack,
            post_failure_alert=alert, logger=logger,
        )
        with self.assertLogs(logger, level='INFO') as captured:
            scope['run_daily_jobs'](run.run_id, post_to_slack=True, post_to_notion=True)
        self.assertEqual(run.status, 'completed_no_new_picks')
        self.assertEqual((run.fetched_count, run.matched_count, run.ranked_count), (88, 11, 0))
        self.assertIsNone(run.slack_posted_at)
        self.assertIsNone(run.error_message)
        slack.assert_not_called()
        notion.assert_not_called()
        alert.assert_not_called()
        self.assertIn('completed_no_new_picks', '\n'.join(captured.output))
        self.assertIn('fetched=88', '\n'.join(captured.output))


if __name__ == '__main__':
    unittest.main()
