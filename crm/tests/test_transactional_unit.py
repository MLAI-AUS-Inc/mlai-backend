"""System sender and telemetry preserve provider results without storing payloads."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from integrations import system_mail


class TransactionalUnitTests(SimpleTestCase):
    @override_settings(CRM_TRANSACTIONAL_MONITOR_ENABLED=True, CRM_NOTIFY_API_KEY='notify-only',
        RESEND_API_KEY='', CUSTOMERIO_API_KEY='', MLAI_SYSTEM_FROM_EMAIL='MLAI <system@notify.example.test>')
    def test_notification_route_uses_the_notify_key_and_does_not_mark_malformed_responses_accepted(self):
        from integrations.services import notification_adapters as adapters
        channel = SimpleNamespace(route_id='synthetic@example.test')
        response = SimpleNamespace(status_code=200, json=lambda: {'id': 'synthetic-receipt'})
        with patch.object(adapters.http_client, 'post', return_value=response) as post, patch.object(system_mail, '_record') as record:
            result = adapters._send_email(channel, subject='Synthetic', text='Synthetic', html_body=None, idempotency_key='synthetic')
            self.assertTrue(result[0]); self.assertEqual(result[1], 'synthetic-receipt')
            self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Bearer notify-only')
            self.assertEqual(post.call_args.kwargs['json']['from'], system_mail.sender())
            self.assertTrue(record.call_args.args[1]['ok'])
        response.json = lambda: ['invalid-provider-shape']
        with patch.object(adapters.http_client, 'post', return_value=response), patch.object(system_mail, '_record') as record:
            with self.assertRaises(AttributeError):
                adapters._send_email(channel, subject='Synthetic', text='Synthetic', html_body=None, idempotency_key='synthetic')
            self.assertFalse(record.call_args.args[1]['ok'])

    @override_settings(MLAI_SYSTEM_FROM_EMAIL='MLAI <system@notify.example.test>', RESEND_FROM_EMAIL='legacy@example.test')
    def test_all_system_payload_builders_use_the_canonical_sender(self):
        from content_factory import founder_notification_outbox as founder, website_notification_transport as website
        self.assertEqual(founder._sender(), system_mail.sender())
        self.assertEqual(founder.email_payload('recipient@example.test', 'https://example.test')['from'], system_mail.sender())
        self.assertEqual(website.email_payload(SimpleNamespace(route_id='recipient@example.test'),
            {'subject': 'Synthetic', 'text': 'Synthetic', 'link': 'https://example.test'})['from'], system_mail.sender())

    @override_settings(CRM_NOTIFY_API_KEY='notify-restricted', RESEND_API_KEY='legacy')
    def test_notify_key_is_preferred_with_a_legacy_rollout_fallback(self):
        self.assertEqual(system_mail.resend_key(), 'notify-restricted')
        with override_settings(CRM_NOTIFY_API_KEY=''):
            self.assertEqual(system_mail.resend_key(), 'legacy')

    @override_settings(CRM_TRANSACTIONAL_MONITOR_ENABLED=False)
    def test_disabled_observer_never_writes_or_changes_results(self):
        with patch.object(system_mail, '_record') as record:
            with system_mail.observe('customerio', 'signin') as attempt:
                attempt.accepted({'delivery_id': 'synthetic-id'})
        record.assert_not_called()

    @override_settings(CRM_TRANSACTIONAL_MONITOR_ENABLED=True)
    def test_observation_is_bounded_and_does_not_capture_code_or_response_text(self):
        with patch.object(system_mail, '_record') as record:
            with system_mail.observe('customerio', 'signin') as attempt:
                attempt.accepted({'delivery_id': 'synthetic-id', 'email': 'secret@example.test', 'code': '123456'})
        details = [call.args[1] for call in record.call_args_list]
        self.assertEqual([row['phase'] for row in details], ['attempt', 'result'])
        self.assertTrue(details[-1]['ok'])
        self.assertEqual(details[-1]['provider_message_id'], 'synthetic-id')
        self.assertNotIn('secret', json.dumps(details)); self.assertNotIn('123456', json.dumps(details))
        self.assertEqual(system_mail.provider_id({'id': 'secret@example.test'}), '')

    @override_settings(CRM_TRANSACTIONAL_MONITOR_ENABLED=True)
    def test_provider_exception_survives_unchanged_and_records_only_a_failure(self):
        failure = RuntimeError('private upstream response')
        with patch.object(system_mail, '_record') as record:
            with self.assertRaises(RuntimeError) as caught:
                with system_mail.observe('resend'):
                    raise failure
        self.assertIs(caught.exception, failure)
        self.assertFalse(record.call_args.args[1]['ok'])
        self.assertNotIn('private', json.dumps(record.call_args.args[1]))

    @override_settings(CRM_TRANSACTIONAL_MONITOR_ENABLED=True, CUSTOMERIO_API_KEY='synthetic',
        CUSTOMERIO_COMMUNITY_CHAT_CODE_MESSAGE_ID='synthetic-template', CUSTOMERIO_FROM_EMAIL='codes@notify.example.test')
    def test_signin_request_disables_tracking_and_observes_only_the_provider_call(self):
        from community_chat.email_delivery import send_community_chat_email_code
        user = SimpleNamespace(pk=1, id=1, email='member@example.test', full_name='Synthetic Member', first_name='Synthetic')
        with patch('community_chat.email_delivery.APIClient') as client, patch.object(system_mail, '_record') as record:
            client.return_value.send_email.return_value = {'delivery_id': 'cio-synthetic'}
            self.assertEqual(send_community_chat_email_code(user, '123456'), {'delivery_id': 'cio-synthetic'})
        payload = client.return_value.send_email.call_args.args[0]
        self.assertIs(payload['tracked'], False)
        self.assertEqual(payload['from'], 'codes@notify.example.test')
        self.assertTrue(record.call_args.args[1]['ok'])
        with override_settings(CUSTOMERIO_API_KEY=''), patch.object(system_mail, '_record') as record:
            with self.assertRaises(RuntimeError):
                send_community_chat_email_code(user, '123456')
        record.assert_not_called()
