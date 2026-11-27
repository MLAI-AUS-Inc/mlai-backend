"""Private report notification boundaries, using synthetic evidence and transport."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from . import cybertest_notifications as notices
from .cybertest_policy import CheckoutError
from .test_cybertest_delivery import basic


AUDIT = UUID('00000000-0000-4000-8000-000000000002')
CHECKOUT = UUID('00000000-0000-4000-8000-000000000001')
COMPANY = '00000000-0000-4000-8000-000000000003'
WORKSPACE = '00000000-0000-4000-8000-000000000004'
CHANNEL = '00000000-0000-4000-8000-000000000005'
KEY = '1' * 64


def receipt():
    return SimpleNamespace(audit_id=AUDIT, dispatched_at=timezone.now(), status='started',
                           phase_contract=basic(), settlement={'basis': 'sponsored_not_delivered'},
                           test_mode=False, user=SimpleNamespace(pk=42), user_id=42,
                           binding={'mlai_user_id': '42', 'mlai_company_id': COMPANY, 'workspace_id': WORKSPACE})


def proof(row):
    return {'auditId': str(AUDIT), 'planId': row.phase_contract['planId'],
            'report': {'persisted': True, 'sha256': '6'*64},
            'checks': [{'id': c['id'], 'status': 'completed'} for c in row.phase_contract['checks']]}


@override_settings(CYBERTEST_COMPLETION_NOTICES_ENABLED=True, COMMUNITY_CHAT_FRONTEND_URL='https://chat.mlai.au',
                   CYBERTEST_BILLING_API_KEY='synthetic-phase-service-key-not-secret')
class CompletionTests(SimpleTestCase):
    def test_service_route_rejects_browser_and_freeform_recipient(self):
        client = APIClient()
        url = f'/api/v1/points/cybertest/quotes/{CHECKOUT}/notify-complete/'
        body = {'audit_id': str(AUDIT), 'evidence': proof(receipt())}
        key = {'HTTP_X_CYBERTEST_KEY': 'synthetic-phase-service-key-not-secret'}
        with patch.object(notices, 'completed', return_value={'status': 'queued'}) as completed:
            self.assertEqual(client.post(url, body, format='json').status_code, 403)
            self.assertEqual(client.post(url, {**body, 'recipient': 'stranger'}, format='json', **key).status_code, 400)
            completed.assert_not_called()
            result = client.post(url, body, format='json', **key)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result['Cache-Control'], 'private, no-store')
            completed.assert_called_once_with(CHECKOUT, str(AUDIT), body['evidence'])

    def test_root_payer_receipt_is_unchanged_and_replay_uses_one_key(self):
        row = receipt()
        with patch.object(notices.checkout, '_get', return_value=row), patch.object(notices.ContentFactoryCallbackEvent.objects, 'get_or_create') as insert:
            for _ in range(2):
                self.assertEqual(notices.completed(CHECKOUT, str(AUDIT), proof(row)), {'status': 'queued'})
            self.assertEqual(insert.call_args_list[0], insert.call_args_list[1])
            self.assertEqual(insert.call_args.kwargs['event_id'], notices.PREFIX + str(AUDIT))
            for field in ('auditId', 'planId', 'checks', 'report'):
                evidence = proof(row)
                del evidence[field]
                with self.subTest(field=field), self.assertRaises(CheckoutError):
                    notices.completed(CHECKOUT, str(AUDIT), evidence)
            partial = proof(row)
            partial['checks'][0]['status'] = 'skipped'
            for attempt, data in ((str(CHECKOUT), proof(row)), (str(AUDIT), partial)):
                with self.assertRaises(CheckoutError):
                    notices.completed(CHECKOUT, attempt, data)
            self.assertEqual(insert.call_count, 2)
            self.assertEqual(row.settlement, {'basis': 'sponsored_not_delivered'})
            row.test_mode = True
            self.assertEqual(notices.completed(CHECKOUT, str(AUDIT), proof(row)), {'status': 'disabled'})
            self.assertEqual(insert.call_count, 2)

    @override_settings(APP_ENV='chat-staging', CYBERTEST_ENTRY_STAGING_ENABLED=True,
                       CYBERTEST_BILLING_MODE='test', CYBERTEST_STAGING_COMPLETION_NOTICES_ENABLED=True,
                       CYBERTEST_STAGING_COMPANY_IDS=[COMPANY], COMMUNITY_CHAT_FRONTEND_URL='https://chat-staging.mlai.au')
    def test_staging_notices_require_the_designated_test_receipt_and_sender(self):
        item = receipt()
        with patch.object(notices.ContentFactoryCallbackEvent.objects, 'get_or_create') as insert:
            self.assertEqual(notices.enqueue(item), {'status': 'disabled'})
            item.test_mode = True
            self.assertEqual(notices.enqueue(item), {'status': 'queued'})
            item.binding['mlai_company_id'] = str(WORKSPACE)
            self.assertEqual(notices.enqueue(item), {'status': 'disabled'})
            insert.assert_called_once()
        from agent_runtime.native_crypto import signing_key
        row = SimpleNamespace(event_id=notices.PREFIX+str(AUDIT), job_id='', created_at=timezone.now())
        with (patch.object(notices, 'recipient', return_value=(COMPANY, (KEY,))),
              patch.object(notices.transport, 'notifier_key', return_value=signing_key('2'*64)) as key,
              patch.object(notices.transport, 'open_dm', return_value=CHANNEL) as open_dm,
              patch.object(notices.transport, 'post_event') as post):
            self.assertEqual(notices.send_claimed(row, MagicMock()), 'sent')
            key.assert_called_once_with(staging=True)
            self.assertTrue(open_dm.call_args.kwargs['staging'])
            self.assertTrue(post.call_args.kwargs['staging'])
            self.assertIn('https://chat-staging.mlai.au/', post.call_args.args[0]['content'])
        with override_settings(COMMUNITY_CHAT_RELAY_URL='wss://chat.mlai.au'):
            with self.assertRaises(notices.transport.NotificationTransportError):
                notices.transport.relay_origin(staging=True)
        with override_settings(COMMUNITY_CHAT_RELAY_URL='wss://chat-staging.mlai.au'):
            self.assertEqual(notices.transport.relay_origin(staging=True), 'https://chat-staging.mlai.au')
            with self.assertRaises(notices.transport.NotificationTransportError):
                notices.transport.relay_origin()

    def test_membership_and_workspace_are_revalidated_before_device_selection(self):
        row = receipt()
        company = SimpleNamespace(pk=COMPANY, organization_id='org', organization='active-org')
        with (patch.object(notices.CybertestCheckout.objects, 'select_related') as checkouts,
              patch.object(notices.VibeRaisingCompany.objects, 'select_related') as companies,
              patch.object(notices.CybertestCompanyBinding.objects, 'filter') as bindings,
              patch.object(notices, 'has_community_access', return_value=True) as member,
              patch.object(notices, 'user_may_use_organization', return_value=True) as org,
              patch.object(notices.CommunityChatDevice.objects, 'filter') as devices):
            checkouts.return_value.filter.return_value.first.return_value = row
            companies.return_value.filter.return_value.first.return_value = company
            bindings.return_value.exists.return_value = True
            devices.return_value.order_by.return_value.values_list.return_value.distinct.return_value = [KEY]
            self.assertEqual(notices.recipient(AUDIT), (COMPANY, (KEY,)))
            bindings.assert_called_once_with(company=company, workspace_id=WORKSPACE, is_active=True, verified_at__isnull=False)
            devices.assert_called_once_with(user=row.user, status=notices.DeviceBindingStatus.VERIFIED,
                                           revoked_at__isnull=True, verified_at__isnull=False)
            bindings.return_value.exists.return_value = False
            self.assertIsNone(notices.recipient(AUDIT))
            bindings.return_value.exists.return_value = True
            org.return_value = False
            self.assertIsNone(notices.recipient(AUDIT))
            org.return_value = True
            member.return_value = False
            self.assertIsNone(notices.recipient(AUDIT))
            member.return_value = True
            row.binding['mlai_user_id'] = 'other'
            self.assertIsNone(notices.recipient(AUDIT))
            self.assertEqual(devices.call_count, 1)

    def test_private_message_replay_after_lost_ack_and_revoked_device(self):
        from agent_runtime.native_crypto import signing_key
        row = SimpleNamespace(event_id=notices.PREFIX+str(AUDIT), job_id='', created_at=timezone.now())
        claim = MagicMock()
        target = (COMPANY, (KEY,))
        with (patch.object(notices, 'recipient', return_value=target) as resolve,
              patch.object(notices.transport, 'notifier_key', return_value=signing_key('2'*64)),
              patch.object(notices.transport, 'open_dm', return_value=CHANNEL) as open_dm,
              patch.object(notices.transport, 'post_event') as post):
            self.assertEqual(notices.send_claimed(row, claim), 'sent')
            event = post.call_args.args[0]
            self.assertIn(f'/my-startup/security/checks/{AUDIT}?company_id={COMPANY}', event['content'])
            self.assertEqual([tag[1] for tag in event['tags'] if tag[0] == 'p'], [KEY])
            row.job_id = event['id']
            self.assertEqual(notices.send_claimed(row, claim), 'sent')
            self.assertEqual(post.call_args.args[0]['id'], row.job_id)
            resolve.side_effect = [target, None]
            self.assertEqual(notices.send_claimed(row, claim), 'skipped')
            self.assertEqual(post.call_count, 2)
            resolve.side_effect = None
            resolve.return_value = (COMPANY, ('3'*64,))
            self.assertEqual(notices.send_claimed(row, claim), 'skipped')
            self.assertEqual(post.call_count, 2)
            resolve.return_value = None
            self.assertEqual(notices.send_claimed(row, claim), 'skipped')
            self.assertEqual(open_dm.call_count, 4)

    def test_a_transient_transport_failure_keeps_the_leased_row_for_retry(self):
        now = timezone.now()
        row = SimpleNamespace(pk=1, event_id=notices.PREFIX + str(AUDIT), created_at=now)
        with (patch.object(notices.ContentFactoryCallbackEvent.objects, 'filter') as initial,
              patch.object(notices, 'send_claimed', side_effect=notices.transport.NotificationTransportError('temporary'))):
            pending = initial.return_value
            due = pending.filter.return_value
            due.order_by.return_value = [row]
            self.assertEqual(notices.drain()['retry'], 1)
            due.update.assert_not_called()
            row.created_at = now - timedelta(days=3)
            self.assertEqual(notices.drain()['skipped'], 1)
            due.update.assert_called_once()

    @override_settings(CYBERTEST_COMPLETION_NOTICES_ENABLED=True)
    def test_enabled_drain_satisfies_the_scheduler_runner_contract(self):
        from core.scheduling import run_runners
        with patch.object(notices.ContentFactoryCallbackEvent.objects, 'filter') as initial:
            initial.return_value.filter.return_value.order_by.return_value = []
            results, failures = run_runners([('security_review_notifications', notices.drain)])
        self.assertEqual(failures, [])
        self.assertEqual(results['security_review_notifications'],
                         {'status': 'completed', 'sent': 0, 'retry': 0, 'skipped': 0, 'superseded': 0})

    @override_settings(CYBERTEST_COMPLETION_NOTICES_ENABLED=False)
    def test_disabled_worker_and_test_receipts_send_nothing(self):
        self.assertEqual(notices.drain(), {'status': 'disabled'})
        self.assertEqual(notices.enqueue(receipt()), {'status': 'disabled'})
