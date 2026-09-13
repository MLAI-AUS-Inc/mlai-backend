"""Execute delivery seams with mocks; no Django setup, database or migrations."""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import importlib.util
_policy_spec = importlib.util.spec_from_file_location('article_notification_policy', Path(__file__).parents[1] / 'integrations/services/article_notification_policy.py')
_policy = importlib.util.module_from_spec(_policy_spec)
_policy_spec.loader.exec_module(_policy)
should_deliver_automation_event = _policy.should_deliver_automation_event


def adapter_function(name, **bindings):
    path = Path(__file__).parents[1] / 'integrations/services/notification_adapters.py'
    source = ast.parse(path.read_text())
    function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == name)
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), function], type_ignores=[])
    scope = {'should_deliver_automation_event': should_deliver_automation_event, **bindings}
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), scope)
    return scope[name]


class ArticleNotificationPolicyTests(unittest.TestCase):
    def test_only_completed_drafts_email_and_research_whatsapp_remains_enabled(self):
        for event in ('topic_selection', 'delivery_mode_required', 'error', 'article_progress', 'recovering'):
            self.assertFalse(should_deliver_automation_event(event, 'email'))
        for event in ('content_ready', 'review_ready'):
            self.assertTrue(should_deliver_automation_event(event, 'email'))
        self.assertTrue(should_deliver_automation_event('topic_selection', 'whatsapp'))
        self.assertTrue(should_deliver_automation_event('topic_selection', 'slack'))
        for channel in ('email', 'whatsapp', 'slack'):
            self.assertFalse(should_deliver_automation_event('error', channel))

    def test_filter_runs_before_delivery_creation_or_provider_calls(self):
        channels = [SimpleNamespace(channel_type=kind) for kind in ('email', 'whatsapp', 'slack')]
        create = Mock(return_value=(SimpleNamespace(status='pending'), True))
        send = Mock()
        fan_out = adapter_function('_fan_out_event', _active_channels_for_run=lambda run: channels,
            _delivery_for_event=create, _send_channel_delivery=send,
            NotificationDeliveryStatus=SimpleNamespace(SENT='sent'))
        self.assertEqual(fan_out(run=object(), event_type='error', request_payload={}, build_kwargs=lambda c: {}), [])
        create.assert_not_called(); send.assert_not_called()
        self.assertEqual(len(fan_out(run=object(), event_type='topic_selection', request_payload={}, build_kwargs=lambda c: {})), 2)
        self.assertEqual([call.kwargs['channel'].channel_type for call in send.call_args_list], ['whatsapp', 'slack'])

    def test_review_email_uses_existing_idempotency(self):
        delivery = SimpleNamespace(status='sent')
        send = Mock()
        fan_out = adapter_function('_fan_out_event', _active_channels_for_run=lambda run: [SimpleNamespace(channel_type='email')],
            _delivery_for_event=lambda **kw: (delivery, False), _send_channel_delivery=send,
            NotificationDeliveryStatus=SimpleNamespace(SENT='sent'))
        self.assertEqual(fan_out(run=object(), event_type='review_ready', request_payload={}, build_kwargs=lambda c: {}), [delivery])
        send.assert_not_called()

    def test_failure_records_diagnostics_without_notifying_and_late_error_preserves_completion(self):
        run = SimpleNamespace(id='automation-1', status='generating', save=Mock(), callback_payload={}, last_error='')
        notify = Mock(side_effect=AssertionError('Failure must not deliver'))
        error = adapter_function('send_error', resolve_automation_run_for_callback=lambda data: run,
            _callback_job_id=lambda data: data['job_id'], _fan_out_event=notify, logger=logging.getLogger(__name__),
            AutomationRunStatus=SimpleNamespace(COMPLETED='completed', FAILED='failed'))
        self.assertEqual(error({'job_id': 'article-1', 'error': 'quote mismatch'}), [])
        self.assertEqual(run.status, 'failed'); self.assertEqual(run.last_error, 'quote mismatch')
        run.status='completed'; run.callback_payload={'review_url': 'https://app.test/review'}; run.last_error=''; run.save.reset_mock()
        self.assertEqual(error({'job_id': 'article-1', 'error': 'delayed failure'}), [])
        self.assertEqual(run.callback_payload, {'review_url': 'https://app.test/review'})
        run.save.assert_not_called(); notify.assert_not_called()
