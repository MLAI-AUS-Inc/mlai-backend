"""Durable qualified public source evidence and real row-lock interleavings."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from io import StringIO
import json
import hashlib
import hmac
import time
import threading
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import close_old_connections, transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings, skipUnlessDBFeature, tag
from django.utils import timezone

from integrations.models import (BridgeSyncInbox, CommunityBridgeChannel, CommunityBridgeDelivery,
    CommunityBridgeReceipt, ExternalServiceConnection, SlackChannelMembership, SlackDmMirrorGrant)
from integrations.services.community_bridge import community_source as source
from integrations.services.community_bridge import community_source_codec as codec
from integrations.services.community_bridge.member_channel_events import dispatch_authenticated_slack_callback
from integrations.services.community_bridge.store import ingest_slack_event, ingest_inbound_event
from integrations.services.message_sync.inbox import enqueue_slack_callback, process_inbox_once
from integrations.services.slack_chat_catalog import PUBLIC_CHANNEL_SCOPES

CONFIG = dict(APP_RELEASE='a'*40, MESSAGE_SYNC_ENABLED=True, SLACK_MEMBER_CHANNELS_ENABLED=True,
    SLACK_OWNER_INVENTORY_ENABLED=True, SLACK_COMMUNITY_SOURCE='both', SLACK_COMMUNITY_SEND_AS_MEMBER=False,
    MESSAGE_SYNC_SLACK_APP_ID='ABOT', MESSAGE_SYNC_SLACK_USER_APP_ID='AUSER',
    MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID='TTEST', SLACK_MEMBER_CHANNEL_ALLOWED_IDS='',
    SLACK_BRIDGE_BOT_USER_ID='UBOT',
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})


class SourceFixture:
    def setUp(self):
        self.start = timezone.now()-timedelta(seconds=1)
        self.rows = [CommunityBridgeChannel.objects.create(
            slack_workspace_id='TTEST', slack_channel_id=f'CROOM{n}', destination_platform='buzz',
            destination_workspace_id='test.invalid', destination_channel_id=str(uuid.UUID(int=n)),
            audience='community', enabled=True, slack_visibility='public', slack_seen_at=timezone.now(),
            mirror_decision='mirror') for n in range(1,16)]
        self.a, self.b = self.grant(1), self.grant(2)
        self.row = self.rows[0]
        self.row.refresh_from_db()

    def grant(self, n):
        user = get_user_model().objects.create_user(email=f'source-{n}@example.invalid')
        conn = ExternalServiceConnection.objects.create(user=user, provider='slack', status='connected',
            external_account_id='TTEST', access_token=f'synthetic-{n}', scopes=sorted(PUBLIC_CHANNEL_SCOPES),
            provider_metadata={'team': {'id':'TTEST'}, 'authed_user': {'id':f'UREADER{n}'}})
        grant = SlackDmMirrorGrant.objects.create(user=user, connection=conn, slack_workspace_id='TTEST',
            slack_user_id=f'UREADER{n}', consented_at=self.start-timedelta(minutes=2))
        for row in self.rows:
            SlackChannelMembership.objects.create(grant=grant, slack_workspace_id='TTEST',
                slack_channel_id=row.slack_channel_id, state='joined', source='event', observed_at=self.start-timedelta(seconds=10))
        return grant

    def callback(self, *, app='ABOT', suffix='1', ts=None, event=None, **extra):
        stamp = ts or f'{int(self.start.timestamp())+1}.000001'
        return dict(type='event_callback', team_id='TTEST', api_app_id=app, event_id=f'E{app}{suffix}',
            event_time=int(self.start.timestamp())+1,
            event=event or dict(type='message', channel='CROOM1', channel_type='channel',
                               user='UAUTHOR', ts=stamp, text='synthetic public text'), **extra)

    def inbox(self, payload, *, received=None, completed=True):
        row = BridgeSyncInbox.objects.get(pk=enqueue_slack_callback(payload)['receipt_id'])
        BridgeSyncInbox.objects.filter(pk=row.pk).update(received_at=received or timezone.now(),
            status='completed' if completed else 'pending', encrypted_payload='' if completed else row.encrypted_payload)
        row.refresh_from_db()
        return row

    def ingest(self, payload, *, received=None, retired=False):
        inbox = self.inbox(payload, received=received)
        result = dispatch_authenticated_slack_callback(payload, app_id=inbox.app_id,
            received_at=inbox.received_at, inbox_id=inbox.pk, source_event_id=inbox.source_event_id,
            private_ingest=lambda _: None)
        return result, CommunityBridgeReceipt.objects.get(pk=result['receipt_id'])


@override_settings(**CONFIG)
class CommunitySourceTests(SourceFixture, TransactionTestCase):
    def test_observation_logs_commit_and_rollback_with_real_transaction(self):
        from integrations.services.community_bridge import observability
        with patch.object(observability.logger, "info") as logger:
            with transaction.atomic():
                observability.room(self.row, "room_updated")
                logger.assert_not_called()
            self.assertEqual(logger.call_count, 1)
            try:
                with transaction.atomic():
                    observability.room(self.row, "held")
                    raise ValueError("synthetic rollback")
            except ValueError:
                pass
            self.assertEqual(logger.call_count, 1)

    def test_real_callback_commit_survives_logging_handler_failure(self):
        from integrations.services.community_bridge import observability
        payload = self.callback(app="AUSER")
        with patch.object(observability.logger, "info", side_effect=RuntimeError("synthetic secret")):
            result = dispatch_authenticated_slack_callback(payload, app_id="AUSER",
                received_at=timezone.now(), private_ingest=lambda _: None)
        self.assertEqual(result["status"], "enqueued")
        self.assertEqual(CommunityBridgeDelivery.objects.filter(receipt_id=result["receipt_id"]).count(), 1)

    def test_room_update_archive_and_hold_logs_follow_persisted_outcomes(self):
        from integrations.services.community_bridge import observability, member_channels
        self.row.room_sync_pending = True
        self.row.save(update_fields=["room_sync_pending"])
        response = {"channel_id": self.row.destination_channel_id, "changed": ["name"]}
        with patch.object(observability.logger, "info") as logger, patch.object(
            member_channels.BuzzBridgeClient, "upsert_public_channel", return_value=response,
        ):
            self.assertEqual(member_channels.sync_room(self.row.pk), "synced")
            self.row.refresh_from_db()
            self.assertIsNotNone(self.row.room_synced_at)
            self.assertIn("outcome=room_updated", logger.call_args.args[2])
            self.row.slack_archived = True
            self.row.room_sync_pending = True
            self.row.save(update_fields=["slack_archived", "room_sync_pending"])
            self.assertEqual(member_channels.sync_room(self.row.pk), "synced")
            self.row.refresh_from_db()
            self.assertFalse(self.row.enabled)
            self.assertIn("outcome=archived", logger.call_args.args[2])
            self.row.room_sync_error = "rekey_requires_room_migration"
            self.row.save(update_fields=["room_sync_error"])
            self.assertEqual(member_channels.sync_room(self.row.pk), "held")
            self.assertIn("outcome=held", logger.call_args.args[2])

    def test_echo_log_occurs_at_ignored_receipt_settlement_not_predicate(self):
        from integrations.services.community_bridge import observability
        from integrations.services.community_bridge import public_echo
        with patch.object(observability.logger, "info") as logger, patch.object(
            public_echo, "is_public_slack_echo", return_value=True,
        ):
            result, receipt = self.ingest(self.callback(app="AUSER"))
        self.assertEqual(result["reason"], "member_slack_echo")
        self.assertEqual(receipt.status, "ignored")
        echo = [call for call in logger.call_args_list if call.args[1] == "slack_public_echo"]
        self.assertEqual(len(echo), 1)
        self.assertIn(f"receipt_id={receipt.pk}", echo[0].args[2])
        self.assertIn("kind=create", echo[0].args[2])

    def test_ambiguous_app_configuration_holds_callback_but_not_internal_history(self):
        for changes in ({'MESSAGE_SYNC_SLACK_USER_APP_ID':'ABOT'},
                        {'MESSAGE_SYNC_SLACK_USER_APP_ID':''}):
            with self.subTest(changes=changes), override_settings(SLACK_COMMUNITY_SOURCE='members', **changes):
                payload=self.callback(suffix='ambiguous')
                row=self.inbox(payload)
                result=dispatch_authenticated_slack_callback(payload,app_id=row.app_id,received_at=row.received_at,
                    inbox_id=row.pk,source_event_id=row.source_event_id)
                self.assertEqual(result['status'],'community_source_pending')
                self.assertFalse(CommunityBridgeReceipt.objects.exists())
        with override_settings(SLACK_COMMUNITY_SOURCE='members'):
            payload=self.callback(app='AWRONG')
            row=self.inbox(payload)
            result=dispatch_authenticated_slack_callback(payload,app_id=row.app_id,received_at=row.received_at,
                inbox_id=row.pk,source_event_id=row.source_event_id)
            self.assertEqual(result['status'],'community_source_pending')
            # Trusted history/direct-store work has its own authority fence,
            # and no callback boundary; it is not mistaken for an app copy.
            result=ingest_slack_event(self.callback(suffix='direct-history'))
            self.assertEqual(result['status'],'enqueued')

    def test_stale_user_mutations_keep_ciphertext_and_wake_exact_history_state(self):
        self.row.slack_seen_at=timezone.now()-timedelta(seconds=301)
        self.row.save(update_fields=['slack_seen_at'])
        target=self.callback()['event']['ts']
        for n,event in enumerate([
            {'type':'message','subtype':'message_changed','channel':'CROOM1',
             'message':{'ts':target,'user':'UAUTHOR','text':'edit'}},
            {'type':'reaction_added','item':{'type':'message','channel':'CROOM1','ts':target},
             'user':'UAUTHOR','reaction':'thumbsup','event_ts':target},
        ]):
            row=self.inbox(self.callback(app='AUSER',suffix=f'stale{n}',event=event),completed=False)
            with override_settings(SLACK_COMMUNITY_SOURCE='members'):
                self.assertEqual(process_inbox_once(),0)
            row.refresh_from_db()
            self.assertEqual((row.status,row.last_error_code),('pending','community_source_pending'))
            self.assertTrue(row.encrypted_payload)
        self.assertFalse(CommunityBridgeReceipt.objects.exists())
        from integrations.models import BridgeSyncState
        state=BridgeSyncState.objects.get(public_channel=self.row)
        self.assertIn('wake_requested_at',state.head_cursor)

    def test_bot_copy_is_retired_and_zero_reader_member_copy_waits_for_recovery(self):
        # Synthetic cutover receipt; actual deployment requires a passing live
        # seven-day report and owner approval, neither claimed by this fixture.
        with override_settings(SLACK_COMMUNITY_SOURCE='members'):
            payload=self.callback()
            inbox=self.inbox(payload)
            result=dispatch_authenticated_slack_callback(payload, app_id=inbox.app_id,
                received_at=inbox.received_at,inbox_id=inbox.pk,source_event_id=inbox.source_event_id)
            retired=CommunityBridgeReceipt.objects.get(pk=result['receipt_id'])
            self.assertEqual(retired.error_text,'bot_source_retired')
            self.assertTrue(retired.receipt_key.startswith('source-retired:'))
            self.assertFalse(CommunityBridgeDelivery.objects.filter(receipt=retired).exists())
            user=self.callback(app='AUSER')
            inbox=self.inbox(user)
            result=dispatch_authenticated_slack_callback(user,app_id=inbox.app_id,received_at=inbox.received_at,
                inbox_id=inbox.pk,source_event_id=inbox.source_event_id)
            active=CommunityBridgeReceipt.objects.get(pk=result['receipt_id'])
            self.assertNotEqual(active.pk,retired.pk)
            self.assertEqual(CommunityBridgeDelivery.objects.filter(receipt=active).count(),1)
            SlackChannelMembership.objects.update(state='left')
            user=self.callback(app='AUSER',suffix='zero',ts=f'{int(self.start.timestamp())+1}.000002')
            inbox=self.inbox(user)
            result=dispatch_authenticated_slack_callback(user,app_id=inbox.app_id,received_at=inbox.received_at,
                inbox_id=inbox.pk,source_event_id=inbox.source_event_id)
            self.assertEqual(result, {'status': 'community_source_pending'})
            self.assertFalse(CommunityBridgeReceipt.objects.filter(receipt_key=user['event_id']).exists())
            bot={**user,'api_app_id':'ABOT','event_id':'EBOTZERO'}
            inbox=self.inbox(bot)
            result=dispatch_authenticated_slack_callback(bot,app_id=inbox.app_id,received_at=inbox.received_at,
                inbox_id=inbox.pk,source_event_id=inbox.source_event_id)
            self.assertEqual(CommunityBridgeDelivery.objects.filter(receipt_id=result['receipt_id']).count(),0)
            self.assertEqual(CommunityBridgeReceipt.objects.get(pk=result['receipt_id']).error_text, 'bot_source_retired')
            SlackChannelMembership.objects.update(state='joined')
            inbox=self.inbox(user)
            result=dispatch_authenticated_slack_callback(user,app_id=inbox.app_id,received_at=inbox.received_at,
                inbox_id=inbox.pk,source_event_id=inbox.source_event_id)
            self.assertEqual(CommunityBridgeDelivery.objects.filter(receipt_id=result['receipt_id']).count(),1)

    def test_unknown_reader_policy_preserves_pending_encrypted_callback(self):
        row=self.inbox(self.callback(app='AUSER'),completed=False)
        with override_settings(SLACK_COMMUNITY_SOURCE='members'), \
                patch('integrations.services.slack_member_access.community_reader_policy_current',return_value=None):
            self.assertEqual(process_inbox_once(),0)
        row.refresh_from_db()
        self.assertEqual((row.status,row.last_error_code),('pending','community_source_pending'))
        self.assertTrue(row.encrypted_payload)
        self.assertFalse(CommunityBridgeReceipt.objects.exists())

    def test_known_joined_reader_allows_member_callback_despite_unrelated_unknown_grant(self):
        from integrations.services import slack_member_access as access

        SlackChannelMembership.objects.filter(grant=self.b, slack_channel_id='CROOM1').delete()
        with override_settings(SLACK_COMMUNITY_SOURCE='members'):
            proof = access.assess_readers('TTEST', 'CROOM1')
            self.assertEqual((proof.eligible_count, proof.unknown_count, proof.complete,
                              proof.effective_source), (1, 1, False, 'members'))
            for index, apps in enumerate((('ABOT', 'AUSER'), ('AUSER', 'ABOT'))):
                stamp = f'{int(self.start.timestamp())+1}.{index+1:06d}'
                for app in apps:
                    payload = self.callback(app=app, suffix=f'positive{index}', ts=stamp)
                    inbox = self.inbox(payload)
                    result = dispatch_authenticated_slack_callback(
                        payload, app_id=app, received_at=inbox.received_at,
                        inbox_id=inbox.pk, source_event_id=inbox.source_event_id,
                        private_ingest=lambda _: None)
                    receipt = CommunityBridgeReceipt.objects.get(pk=result['receipt_id'])
                    if app == 'ABOT':
                        self.assertEqual(receipt.error_text, 'bot_source_retired')
                        self.assertFalse(CommunityBridgeDelivery.objects.filter(receipt=receipt).exists())
                    else:
                        self.assertEqual(result['status'], 'enqueued')
                        self.assertEqual(CommunityBridgeDelivery.objects.filter(receipt=receipt).count(), 1)
            self.assertEqual(CommunityBridgeDelivery.objects.count(), 2)
            self.row.refresh_from_db()
            saved = self.row.room_state[access.COMMUNITY_READER_STATE]
            self.assertEqual((saved['eligible_count'], saved['unknown_count'], saved['complete'],
                              saved['effective_source']), (1, 1, False, 'members'))

    def test_unknown_reader_holds_member_callback_and_retires_bot_until_recovery(self):
        from integrations.services import slack_member_access as access

        SlackChannelMembership.objects.filter(grant=self.b, slack_channel_id='CROOM1').delete()
        SlackChannelMembership.objects.filter(grant=self.a, slack_channel_id='CROOM1').update(
            state='left', observed_at=timezone.now(), left_at=timezone.now())
        with override_settings(SLACK_COMMUNITY_SOURCE='members'):
            proof = access.assess_readers('TTEST', 'CROOM1')
            self.assertEqual((proof.eligible_count, proof.unknown_count, proof.complete,
                              proof.effective_source), (0, 1, False, 'hold'))
            for app in ('ABOT', 'AUSER'):
                row = self.inbox(self.callback(app=app, suffix='unknown'), completed=False)
                self.assertEqual(process_inbox_once(), 1 if app == 'ABOT' else 0)
                row.refresh_from_db()
                if app == 'AUSER':
                    self.assertEqual((row.status, row.last_error_code), ('pending', 'community_source_pending'))
                    self.assertTrue(row.encrypted_payload)
                else:
                    self.assertEqual(row.status, 'completed')
            self.assertEqual(CommunityBridgeReceipt.objects.get().error_text, 'bot_source_retired')
            self.assertFalse(CommunityBridgeDelivery.objects.exists())
            SlackChannelMembership.objects.filter(grant=self.a, slack_channel_id='CROOM1').update(
                state='joined', observed_at=timezone.now(), left_at=None)
            row.available_at = timezone.now()
            row.save(update_fields=['available_at'])
            self.assertEqual(process_inbox_once(), 1)
            self.assertEqual(CommunityBridgeDelivery.objects.count(), 1)


    def test_legacy_source_controls_cannot_select_bot_intake_or_write_comparison(self):
        before = list(CommunityBridgeChannel.objects.values_list('pk', 'room_state'))
        for mode in ('bot', 'both', 'members', 'invalid'):
            with override_settings(SLACK_COMMUNITY_SOURCE=mode):
                _, receipt = self.ingest(self.callback(app='AUSER', suffix=mode,
                    ts=f'{int(self.start.timestamp())+1}.{len(mode):06d}'))
                self.assertNotIn(codec.KEY, receipt.payload)
                result, bot = self.ingest(self.callback(suffix=mode))
                self.assertEqual(bot.error_text, 'bot_source_retired')
        self.assertTrue(CommunityBridgeDelivery.objects.exists())
        self.assertTrue(all(codec.EPOCH_KEY not in state for _, state in before))
        self.assertTrue(all(codec.EPOCH_KEY not in state for state in
                            CommunityBridgeChannel.objects.values_list('room_state', flat=True)))

    def test_status_is_read_only_and_does_not_require_a_fifteen_room_cohort(self):
        CommunityBridgeChannel.objects.filter(pk__in=[row.pk for row in self.rows[1:]]).delete()
        before = self.row.room_state
        report = source.status()
        self.assertEqual(report['mode'], 'members')
        self.assertEqual(report['policy'], 'permanent')
        self.assertEqual(len(report['rooms']), 1)
        self.assertEqual(report['rooms'][0]['eligible_reader_count'], 2)
        self.row.refresh_from_db()
        self.assertEqual(self.row.room_state, before)

    def test_purge_retains_historical_marker_without_new_comparison_writes(self):
        _, receipt = self.ingest(self.callback(app='AUSER'))
        value = dict(version=1, epoch=str(uuid.uuid4()), cohort='a'*64,
            room=source.room_identity(self.row), comparison_id='b'*64, operation='message',
            kind='message', message_ns=codec.stamp(self.start), own_send_ns=0,
            sources={'bot': codec.stamp(self.start), 'members': codec.stamp(self.start)})
        self.assertIsNotNone(codec.marker(value))
        receipt.payload = {**receipt.payload, codec.KEY: value}
        receipt.save(update_fields=['payload'])
        CommunityBridgeReceipt.objects.filter(pk=receipt.pk).update(created_at=timezone.now()-timedelta(days=31))
        call_command('purge_community_bridge_payloads', days=30, batch_size=1, stdout=StringIO())
        receipt.refresh_from_db()
        self.assertEqual(receipt.payload, {codec.KEY: value})
        out = StringIO()
        call_command('purge_community_bridge_payloads', days=30, batch_size=1, stdout=out)
        self.assertIn('Cleared 0', out.getvalue())
        self.ingest(self.callback(app='AUSER'))
        receipt.refresh_from_db()
        self.assertEqual(receipt.payload, {codec.KEY: value})
        receipt.payload = {codec.KEY: {'bad': 'private'}, 'event': {'text': 'private'}}
        receipt.save(update_fields=['payload'])
        call_command('purge_community_bridge_payloads', days=30, batch_size=1, stdout=StringIO())
        receipt.refresh_from_db()
        self.assertEqual(receipt.payload, {})

@override_settings(**CONFIG, SLACK_BRIDGE_SIGNING_SECRET='synthetic-bot-secret',
                   MESSAGE_SYNC_SLACK_USER_SIGNING_SECRET='synthetic-user-secret')
class CommunitySourceSignatureTests(SimpleTestCase):
    def test_event_app_id_is_bound_to_its_actual_signing_secret(self):
        from integrations.services.message_sync.configuration import valid_callback_signature
        timestamp=str(int(time.time()))
        def signed(app,secret):
            body=json.dumps({'type':'event_callback','api_app_id':app}).encode()
            signature='v0='+hmac.new(secret.encode(),b'v0:'+timestamp.encode()+b':'+body,hashlib.sha256).hexdigest()
            return valid_callback_signature(body,timestamp,signature)
        self.assertTrue(signed('ABOT','synthetic-bot-secret'))
        self.assertTrue(signed('AUSER','synthetic-user-secret'))
        self.assertFalse(signed('AUSER','synthetic-bot-secret'))
        self.assertFalse(signed('AWRONG','synthetic-bot-secret'))
