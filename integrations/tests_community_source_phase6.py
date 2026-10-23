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
        self.assertEqual(source.heartbeat('synthetic-process-1', now=self.start), 15)
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
        with source.authenticated_callback(payload, app_id=inbox.app_id, received_at=inbox.received_at,
                inbox_id=inbox.pk, source_event_id=inbox.source_event_id):
            result = ingest_slack_event(payload, retired_bot_source=retired)
        return result, CommunityBridgeReceipt.objects.get(pk=result['receipt_id'])

    def epoch(self):
        self.row.refresh_from_db()
        return source.epoch(self.row.room_state[codec.EPOCH_KEY])

    def closed_window(self):
        """Synthetic persisted continuous chain; does not claim real elapsed time."""
        now = timezone.now()
        for row in self.rows:
            row.refresh_from_db()
            value = deepcopy(row.room_state[codec.EPOCH_KEY])
            value['start_ns'] = codec.stamp(now-timedelta(days=7, seconds=1))
            value['last_ns'] = codec.stamp(now)
            row.room_state[codec.EPOCH_KEY] = value
            row.save(update_fields=['room_state'])
        return now


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

    def test_authenticated_public_route_and_compare_logs_never_include_body(self):
        from integrations.services.community_bridge import observability
        payload = self.callback(app="AUSER")
        payload["event"]["text"] = "synthetic body must not enter logs"
        inbox = self.inbox(payload, completed=False)
        with patch.object(observability.logger, "info") as logger:
            result = dispatch_authenticated_slack_callback(payload, app_id=inbox.app_id,
                received_at=inbox.received_at, inbox_id=inbox.pk,
                source_event_id=inbox.source_event_id, private_ingest=lambda _: None)
        self.assertEqual(result["status"], "enqueued")
        receipt = CommunityBridgeReceipt.objects.get(pk=result["receipt_id"])
        evidence = codec.marker(receipt.payload.get(codec.KEY))
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["sources"], {"members": codec.stamp(inbox.received_at)})
        self.assertEqual(evidence["epoch"], self.epoch()["id"])
        self.assertEqual(evidence["room"], source.room_identity(self.row))
        families = [call.args[1] for call in logger.call_args_list]
        self.assertIn("slack_member_event", families)
        self.assertIn("slack_community_source compare", families)
        self.assertNotIn(payload["event"]["text"], str(logger.call_args_list))

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
            result, receipt = self.ingest(self.callback())
        self.assertEqual(result["reason"], "member_slack_echo")
        self.assertEqual(receipt.status, "ignored")
        echo = [call for call in logger.call_args_list if call.args[1] == "slack_public_echo"]
        self.assertEqual(len(echo), 1)
        self.assertIn(f"receipt_id={receipt.pk}", echo[0].args[2])
        self.assertIn("kind=create", echo[0].args[2])

    def test_report_logs_reuse_exact_derived_counts_and_fallback_only_on_change(self):
        from integrations.services.community_bridge import observability
        from integrations.services import slack_member_access as access
        from types import SimpleNamespace
        self.ingest(self.callback())
        with patch.object(observability.logger, "info") as logger:
            report = source.status()
        reports = [call for call in logger.call_args_list if call.args[1] == "slack_community_source compare"]
        self.assertEqual(len(reports), 15)
        first = report["rooms"][0]
        self.assertIn(f"N={first['N']}", reports[0].args[2])
        self.assertIn(f"M={first['M']}", reports[0].args[2])
        assessment = SimpleNamespace(eligible_count=0, unknown_count=0, complete=True,
            effective_source="bot", fallback_reason="no_reader", fingerprint="a"*64)
        with patch.object(observability.logger, "info") as logger:
            access.record_community_reader_state(self.row, assessment)
            access.record_community_reader_state(self.row, assessment)
        self.assertEqual(logger.call_count, 1)
        self.assertIn("outcome=no_reader", logger.call_args.args[2])

    def test_both_apps_one_outbox_and_minimum_original_ingress_survive_duplicates(self):
        bot, member = self.callback(), self.callback(app='AUSER')
        first, receipt = self.ingest(bot)
        _, same = self.ingest(member)
        self.assertEqual(same.pk, receipt.pk)
        self.assertEqual(CommunityBridgeDelivery.objects.filter(receipt=receipt).count(), 1)
        evidence = same.payload[codec.KEY]
        self.assertEqual(set(evidence['sources']), {'bot','members'})
        # Well before the member copy's own receipt, so slow CI cannot make it the later one.
        earlier = self.start+timedelta(milliseconds=500)
        self.ingest(self.callback(app='AUSER', suffix='retry'), received=earlier)
        same.refresh_from_db()
        self.assertEqual(same.payload[codec.KEY]['sources']['members'], codec.stamp(earlier))
        self.assertEqual(CommunityBridgeDelivery.objects.filter(receipt=receipt).count(), 1)

    def test_broadcast_and_original_reply_are_one_comparison_message(self):
        ordinary = self.callback()
        ordinary['event']['thread_ts'] = f'{int(self.start.timestamp())-1}.000001'
        _, first = self.ingest(ordinary)
        broadcast = self.callback(suffix='broadcast', event={**ordinary['event'], 'subtype':'thread_broadcast'})
        _, second = self.ingest(broadcast)
        self.assertNotEqual(first.receipt_key, second.receipt_key)
        self.assertEqual(first.payload[codec.KEY]['comparison_id'], second.payload[codec.KEY]['comparison_id'])

    def test_mutations_only_record_separate_closed_diagnostics(self):
        target = self.callback()['event']['ts']
        revision = f'{int(self.start.timestamp())+1}.900001'
        events = [
            dict(type='message',subtype='message_changed',channel='CROOM1',channel_type='channel',
                 message={'ts':target,'user':'UAUTHOR','text':'changed','edited':{'ts':revision}}),
            dict(type='message',subtype='message_deleted',channel='CROOM1',channel_type='channel',
                 deleted_ts=target,event_ts=revision,previous_message={'ts':target,'user':'UAUTHOR'}),
            *[dict(type=kind,item={'type':'message','channel':'CROOM1','ts':target},
                   reaction='thumbsup',user='UAUTHOR',event_ts=revision)
              for kind in ('reaction_added','reaction_removed')],
        ]
        for n,event in enumerate(events):
            _,receipt=self.ingest(self.callback(suffix=f'mutation{n}',event=event))
            self.assertEqual(receipt.payload[codec.KEY]['kind'],'diagnostic')
            self.assertNotEqual(receipt.payload[codec.KEY]['operation'],'message')
            self.assertEqual(set(receipt.payload[codec.KEY]),codec.MARKER_FIELDS)

    def test_report_before_seven_complete_days_is_explicitly_inconclusive(self):
        result=source.status()
        self.assertEqual((result['status'],result['reason']),('inconclusive','seven_full_days_required'))
        self.assertEqual(len(result['reader_status']),15)
        self.assertEqual(result['reader_status'][0]['eligible_readers'],2)
        self.assertIsNone(result['miss_ratio'])
        self.assertFalse(result['window_complete'])
        with self.assertRaisesRegex(ValueError, 'source_cutoff_invalid'):
            source.status(cutoff_ns=codec.stamp(timezone.now()+timedelta(days=1)))

    def test_direct_buzz_and_slack_ingest_cannot_forge_current_epoch_sightings(self):
        _, original = self.ingest(self.callback())
        forged = deepcopy(original.payload[codec.KEY])
        forged['sources'] = {'members': codec.stamp(timezone.now())}
        for platform, channel in [('slack', 'CROOM1'), ('buzz', self.row.destination_channel_id)]:
            result = ingest_inbound_event(source_platform=platform, receipt_key=f'forged-{platform}',
                source_channel_id=channel, event_type='message', normalized_event=None,
                raw_payload={codec.KEY: forged, 'metadata': {codec.KEY: forged}})
            receipt = CommunityBridgeReceipt.objects.get(pk=result['receipt_id'])
            self.assertNotIn(codec.KEY, receipt.payload)
        # Defense in depth: even preexisting forged non-Slack/misbound rows
        # cannot enter a report's exact Slack/source/channel evidence relation.
        cutoff = self.closed_window()
        original.refresh_from_db()
        value = deepcopy(original.payload[codec.KEY])
        value['message_ns'] = codec.stamp(cutoff-timedelta(days=1))
        value['sources'] = {'bot': value['message_ns']+1000, 'members': value['message_ns']+2000}
        CommunityBridgeReceipt.objects.filter(pk=original.pk).update(payload={codec.KEY:value})
        CommunityBridgeReceipt.objects.create(channel=self.row, platform='buzz', receipt_key='legacy-forged',
            source_channel_id='CROOM1', payload={codec.KEY:{**value,'comparison_id':'b'*64,'sources':{'bot':value['message_ns']+1000}}})
        CommunityBridgeReceipt.objects.create(channel=self.row, platform='slack', receipt_key='misbound-forged',
            source_channel_id='COTHER', payload={codec.KEY:{**value,'comparison_id':'c'*64,'sources':{'bot':value['message_ns']+1000}}})
        result = source.status(cutoff_ns=codec.stamp(cutoff))
        self.assertEqual((result['N'],result['M']),(1,0))

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

    def test_member_first_bot_second_also_merges_before_duplicate_return(self):
        _, first = self.ingest(self.callback(app='AUSER'))
        result, second = self.ingest(self.callback())
        self.assertEqual((result['status'], first.pk), ('duplicate', second.pk))
        self.assertEqual(set(second.payload[codec.KEY]['sources']), {'bot','members'})

    def test_forged_marker_inline_synthetic_and_foreign_workspace_cannot_create_proof(self):
        forged = {'text':'must not persist as trusted metadata'}
        payload = self.callback(**{codec.KEY:forged})
        result = ingest_slack_event(payload)
        receipt = CommunityBridgeReceipt.objects.get(pk=result['receipt_id'])
        self.assertNotIn(codec.KEY, receipt.payload)
        inline = self.callback(suffix='inline', ts=f'{int(self.start.timestamp())+1}.000002')
        with source.authenticated_callback(inline, app_id='ABOT', received_at=timezone.now()):
            result = ingest_slack_event(inline)
        self.assertNotIn(codec.KEY, CommunityBridgeReceipt.objects.get(pk=result['receipt_id']).payload)
        self.assertEqual(self.epoch()['invalid_reason'], 'non_durable_ingress')
        wrong = {**self.callback(), 'team_id':'TOTHER'}
        with source.authenticated_callback(wrong, app_id='ABOT', received_at=timezone.now(), inbox_id=1):
            self.assertIsNone(source.authenticated_role(wrong))

    def test_third_party_userless_bot_is_countable_but_own_bot_is_excluded(self):
        for n, user in ((1,None),(2,'UBOT')):
            event = self.callback()['event']
            event.update(subtype='bot_message', bot_id='BOTHER', ts=f'{int(self.start.timestamp())+1}.{n:06d}')
            event.pop('user')
            if user:
                event['user'] = user
            _, receipt = self.ingest(self.callback(suffix=f'bot{n}', event=event))
            self.assertEqual(receipt.payload[codec.KEY]['kind'], 'own_send' if user else 'message')
            self.assertEqual(CommunityBridgeDelivery.objects.filter(receipt=receipt).count(), 0)

    def test_echo_checked_before_duplicate_and_comparison_excludes_real_own_send(self):
        with patch('integrations.services.community_bridge.public_echo.is_public_slack_echo', return_value=True):
            _, receipt = self.ingest(self.callback())
        self.assertEqual(receipt.payload[codec.KEY]['kind'], 'own_send')
        self.assertEqual(receipt.error_text, 'member_slack_echo')
        self.assertFalse(CommunityBridgeDelivery.objects.filter(receipt=receipt).exists())

    def test_private_or_connect_and_members_mapping_never_create_public_evidence(self):
        for n, extra in enumerate(({'is_private':True}, {'is_ext_shared_channel':True})):
            event = {**self.callback()['event'], **extra, 'ts':f'{int(self.start.timestamp())+1}.{n+1:06d}'}
            _, receipt = self.ingest(self.callback(suffix=f'private{n}', event=event))
            self.assertNotIn(codec.KEY, receipt.payload)
        self.row.audience='members'
        self.row.save(update_fields=['audience'])
        with patch('integrations.services.community_bridge.member_channels.member_channel_in_scope', return_value=False):
            _, receipt = self.ingest(self.callback(suffix='members', ts=f'{int(self.start.timestamp())+1}.000003'))
        self.assertNotIn(codec.KEY, receipt.payload)

    def test_foreign_workspace_rate_notice_does_not_invalidate_this_epoch(self):
        payload = {'type':'app_rate_limited','api_app_id':'ABOT','team_id':'TOTHER'}
        with source.authenticated_callback(payload, app_id='ABOT', source_event_id='rate:foreign',
                received_at=timezone.now(), inbox_id=1):
            source.rate_limited(payload)
        self.assertEqual(self.epoch()['invalid_reason'], '')
        payload['team_id']='TTEST'
        with source.authenticated_callback(payload, app_id='ABOT', source_event_id='rate:configured',
                received_at=timezone.now(), inbox_id=1):
            source.rate_limited(payload)
        self.assertEqual(self.epoch()['invalid_reason'], 'rate_limited')

    def test_gap_restart_and_source_config_change_begin_new_window_without_backfill(self):
        initial = self.epoch()
        source.heartbeat('synthetic-process-1', now=self.start+timedelta(seconds=180))
        self.assertEqual(self.epoch()['id'], initial['id'])
        source.heartbeat('synthetic-process-1', now=self.start+timedelta(seconds=361))
        gap = self.epoch()
        self.assertNotEqual(gap['id'], initial['id'])
        self.assertEqual(gap['start_ns'], codec.stamp(self.start+timedelta(seconds=361)))
        source.heartbeat('new-process-same-host-pid', now=self.start+timedelta(seconds=542))
        self.assertNotEqual(self.epoch()['id'], gap['id'])
        with override_settings(APP_RELEASE='b'*40):
            source.heartbeat('new-process-same-host-pid', now=self.start+timedelta(seconds=543))
            self.assertNotEqual(self.epoch()['config'], gap['config'])

    def test_sixteenth_or_removed_or_remapped_room_refuses_frozen_cohort(self):
        prior = self.epoch()
        extra = CommunityBridgeChannel.objects.create(slack_workspace_id='TTEST', slack_channel_id='CEXTRA',
            destination_platform='buzz', destination_channel_id=str(uuid.uuid4()), audience='community')
        self.assertEqual(source.heartbeat('synthetic-process-1'), 0)
        self.assertEqual(source.status()['reason'], 'cohort_not_fifteen')
        extra.delete()
        self.row.destination_channel_id=str(uuid.uuid4())
        self.row.save(update_fields=['destination_channel_id'])
        source.heartbeat('synthetic-process-1')
        self.assertNotEqual(self.epoch()['id'], prior['id'])

    def test_off_mode_baseline_creates_no_epoch_or_evidence(self):
        CommunityBridgeChannel.objects.update(room_state={})
        with override_settings(SLACK_COMMUNITY_SOURCE='bot', SLACK_MEMBER_CHANNELS_ENABLED=False):
            self.assertEqual(source.heartbeat('disabled-process'), 0)
            result = ingest_slack_event(self.callback())
        receipt=CommunityBridgeReceipt.objects.get(pk=result['receipt_id'])
        self.assertNotIn(codec.KEY, receipt.payload)
        self.assertEqual(source.status()['status'], 'inactive')

    def test_purge_retains_only_closed_metadata_and_terminates_on_second_run(self):
        _, receipt = self.ingest(self.callback())
        value = deepcopy(receipt.payload[codec.KEY])
        CommunityBridgeReceipt.objects.filter(pk=receipt.pk).update(created_at=timezone.now()-timedelta(days=31))
        out=StringIO()
        call_command('purge_community_bridge_payloads', days=30, batch_size=1, stdout=out)
        receipt.refresh_from_db()
        self.assertEqual(receipt.payload, {codec.KEY:value})
        out=StringIO()
        call_command('purge_community_bridge_payloads', days=30, batch_size=1, stdout=out)
        self.assertIn('Cleared 0',out.getvalue())
        self.ingest(self.callback(app='AUSER'))
        receipt.refresh_from_db()
        self.assertEqual(set(receipt.payload), {codec.KEY})
        self.assertEqual(set(receipt.payload[codec.KEY]['sources']), {'bot','members'})
        receipt.payload={codec.KEY:{'bad':'private'}, 'event':{'text':'private'}}
        receipt.save(update_fields=['payload'])
        call_command('purge_community_bridge_payloads', days=30, batch_size=1, stdout=StringIO())
        receipt.refresh_from_db()
        self.assertEqual(receipt.payload,{})

    def test_report_requires_drained_cutoff_and_revisions_invalidate_on_late_observation(self):
        cutoff = self.closed_window()
        stamp=f'{int((cutoff-timedelta(days=1)).timestamp())}.000001'
        self.ingest(self.callback(ts=stamp), received=cutoff-timedelta(seconds=2))
        self.ingest(self.callback(app='AUSER', ts=stamp), received=cutoff-timedelta(seconds=1))
        first=source.status(cutoff_ns=codec.stamp(cutoff), finalize=True)
        self.assertEqual((first['status'],first['N'],first['M']),('pass',1,0))
        backlog=self.inbox(self.callback(suffix='unclassified'), received=cutoff-timedelta(seconds=1), completed=False)
        censored=source.status(cutoff_ns=codec.stamp(cutoff))
        self.assertEqual((censored['status'],censored['unclassified_pending_count']),('inconclusive',1))
        self.assertNotIn('private',json.dumps(censored))
        backlog.delete()
        stamp2=f'{int((cutoff-timedelta(hours=1)).timestamp())}.000001'
        self.ingest(self.callback(suffix='late',ts=stamp2),received=cutoff+timedelta(microseconds=1))
        self.assertTrue(self.epoch()['report']['invalidated'])
        checked_at = cutoff+timedelta(seconds=1)
        with patch.object(timezone, 'now', return_value=checked_at):
            source.heartbeat('synthetic-process-1', now=checked_at)
            next_report=source.status(cutoff_ns=codec.stamp(checked_at),finalize=True)
        self.assertEqual(next_report['report_receipt']['revision'],2)
        self.assertEqual(next_report['report_receipt']['supersedes'],first['report_receipt']['fingerprint'])
        self.assertEqual((next_report['N'],next_report['M']),(2,1))

    def test_pre_cutoff_sighting_processed_after_finalization_invalidates_saved_report(self):
        cutoff=self.closed_window()
        stamp=f'{int((cutoff-timedelta(days=1)).timestamp())}.000001'
        self.ingest(self.callback(ts=stamp),received=cutoff-timedelta(seconds=2))
        self.ingest(self.callback(app='AUSER',ts=stamp),received=cutoff-timedelta(seconds=1))
        first=source.status(cutoff_ns=codec.stamp(cutoff),finalize=True)
        self.assertEqual(first['status'],'pass')
        delayed=f'{int((cutoff-timedelta(hours=1)).timestamp())}.000001'
        self.ingest(self.callback(suffix='late-pre-cutoff',ts=delayed),received=cutoff-timedelta(microseconds=1))
        self.assertTrue(self.epoch()['report']['invalidated'])
        current=source.status(cutoff_ns=codec.stamp(cutoff))
        self.assertFalse(current['historical_report_current'])
        self.assertEqual((current['N'],current['M']),(2,1))

    def cutover_fixture(self):
        cutoff = self.closed_window()
        stamp = f'{int((cutoff-timedelta(days=1)).timestamp())}.000001'
        self.ingest(self.callback(ts=stamp),received=cutoff-timedelta(seconds=2))
        self.ingest(self.callback(app='AUSER',ts=stamp),received=cutoff-timedelta(seconds=1))
        report=source.status(cutoff_ns=codec.stamp(cutoff),finalize=True)
        self.assertEqual(report['status'],'pass')
        with override_settings(SLACK_COMMUNITY_SOURCE='members'):
            self.assertEqual(source.heartbeat('synthetic-process-1'),15)
        return self.epoch(), report

    def test_members_restart_and_unrelated_release_reset_only_prospective_monitor(self):
        first,report=self.cutover_fixture()
        resumed_at=timezone.now()+timedelta(seconds=181)
        with override_settings(SLACK_COMMUNITY_SOURCE='members',APP_RELEASE='b'*40), \
                patch.object(timezone,'now',return_value=resumed_at):
            self.assertEqual(source.heartbeat('new-process-after-restart'),15)
            resumed=self.epoch()
            self.assertEqual((resumed['id'],resumed['start_ns'],resumed['config'],resumed['report']),
                (first['id'],first['start_ns'],first['config'],first['report']))
            self.assertNotEqual(resumed['monitor_config'],first['monitor_config'])
            self.assertEqual(resumed['members_since_ns'],codec.stamp(resumed_at))
            result=source.status()
            self.assertTrue(result['historical_coverage_certified'])
            self.assertFalse(result['fourteen_days_without_fallback'])

    def test_brief_fallback_between_ticks_resets_prospective_epoch_then_recovers(self):
        first,_=self.cutover_fixture()
        with override_settings(SLACK_COMMUNITY_SOURCE='members'):
            self.row.refresh_from_db()
            from integrations.services.slack_member_access import ReaderAssessment
            source.note_reader_transition(self.row,ReaderAssessment(0,0,True,'bot','no_reader','a'*64))
            reset=self.epoch()
            self.assertEqual(reset['members_since_ns'],0)
            self.assertGreater(reset['last_fallback_ns'],0)
            self.assertEqual(source.heartbeat('synthetic-process-1'),15)
            recovered=self.epoch()
            self.assertEqual(recovered['report'],first['report'])
            self.assertGreaterEqual(recovered['members_since_ns'],reset['last_fallback_ns'])
            self.assertEqual(recovered['monitor_reason'],'')

    def test_late_comparison_after_restart_invalidates_history_and_crosses_threshold(self):
        first,report=self.cutover_fixture()
        resumed_at=timezone.now()+timedelta(seconds=181)
        with override_settings(SLACK_COMMUNITY_SOURCE='members',APP_RELEASE='b'*40), \
                patch.object(timezone,'now',return_value=resumed_at):
            source.heartbeat('new-process-after-restart')
            stamp=f'{first["start_ns"]//10**9+60}.000001'
            payload = self.callback(suffix='late-after-restart', ts=stamp)
            inbox = self.inbox(payload, received=resumed_at)
            outcome = dispatch_authenticated_slack_callback(
                payload, app_id=inbox.app_id, received_at=inbox.received_at,
                inbox_id=inbox.pk, source_event_id=inbox.source_event_id)
            self.assertEqual(outcome['reason'], 'bot_source_retired')
            self.assertIn(codec.KEY, CommunityBridgeReceipt.objects.get(pk=outcome['receipt_id']).payload)
            self.assertTrue(self.epoch()['report']['invalidated'])
        checked_at=resumed_at+timedelta(seconds=1)
        with override_settings(SLACK_COMMUNITY_SOURCE='members',APP_RELEASE='b'*40), \
                patch.object(timezone,'now',return_value=checked_at):
            source.heartbeat('new-process-after-restart')
            result=source.status(cutoff_ns=codec.stamp(checked_at),finalize=True)
            self.assertEqual((result['N'],result['M'],result['status']),(2,1,'fail'))
            self.assertEqual(result['report_receipt']['supersedes'],report['report_receipt']['fingerprint'])
            self.assertEqual(self.epoch()['comparison_covered_until_ns'],first['comparison_covered_until_ns'])
            self.assertFalse(result['fourteen_days_without_fallback'])

    def test_members_source_app_or_frozen_room_drift_cannot_rebase_monitor(self):
        first,_=self.cutover_fixture()
        with override_settings(SLACK_COMMUNITY_SOURCE='members',MESSAGE_SYNC_SLACK_USER_APP_ID='AOTHER'):
            self.assertEqual(source.heartbeat('synthetic-process-1'),0)
            self.assertEqual(self.epoch()['members_since_ns'],0)
            self.assertFalse(source.status()['historical_report_current'])
        self.row.refresh_from_db()
        self.row.destination_channel_id=str(uuid.uuid4())
        self.row.save(update_fields=['destination_channel_id'])
        with override_settings(SLACK_COMMUNITY_SOURCE='members'):
            self.assertEqual(source.heartbeat('synthetic-process-1'),0)
            self.assertEqual(self.epoch()['cohort'],first['cohort'])
            self.assertEqual(self.epoch()['report'],first['report'])

    def test_members_bot_copy_cannot_claim_shared_key_and_zero_reader_user_copy_is_quarantined(self):
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
            held=CommunityBridgeReceipt.objects.get(pk=result['receipt_id'])
            self.assertEqual(held.error_text,'member_source_no_reader')
            self.assertFalse(CommunityBridgeDelivery.objects.filter(receipt=held).exists())
            bot={**user,'api_app_id':'ABOT','event_id':'EBOTZERO'}
            inbox=self.inbox(bot)
            result=dispatch_authenticated_slack_callback(bot,app_id=inbox.app_id,received_at=inbox.received_at,
                inbox_id=inbox.pk,source_event_id=inbox.source_event_id)
            self.assertEqual(CommunityBridgeDelivery.objects.filter(receipt_id=result['receipt_id']).count(),1)

    def test_unknown_reader_policy_preserves_pending_encrypted_callback(self):
        row=self.inbox(self.callback(),completed=False)
        with override_settings(SLACK_COMMUNITY_SOURCE='members'), \
                patch('integrations.services.slack_member_access.community_reader_policy_current',return_value=None):
            self.assertEqual(process_inbox_once(),0)
        row.refresh_from_db()
        self.assertEqual((row.status,row.last_error_code),('pending','community_source_pending'))
        self.assertTrue(row.encrypted_payload)
        self.assertFalse(CommunityBridgeReceipt.objects.exists())


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


@tag('postgres-only')
@override_settings(**CONFIG)
@skipUnlessDBFeature('has_select_for_update')
class CommunitySourceConcurrencyTests(SourceFixture, TransactionTestCase):
    def test_pre_cutoff_uncommitted_inbox_cannot_leave_report_current_after_processing(self):
        cutoff=self.closed_window()
        stamp=f'{int((cutoff-timedelta(days=1)).timestamp())}.000001'
        self.ingest(self.callback(ts=stamp),received=cutoff-timedelta(seconds=2))
        self.ingest(self.callback(app='AUSER',ts=stamp),received=cutoff-timedelta(seconds=1))
        delayed=self.callback(suffix='transaction-delayed',ts=f'{int((cutoff-timedelta(hours=1)).timestamp())}.000001')
        inserted,commit=threading.Event(),threading.Event()
        errors=[]
        def intake():
            close_old_connections()
            try:
                with transaction.atomic():
                    inbox=self.inbox(delayed,received=cutoff-timedelta(microseconds=1),completed=False)
                    inserted.set()
                    if not commit.wait(5):
                        raise AssertionError('synthetic_commit_not_released')
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                close_old_connections()
        thread=threading.Thread(target=intake)
        thread.start()
        try:
            self.assertTrue(inserted.wait(3))
            first=source.status(cutoff_ns=codec.stamp(cutoff),finalize=True)
            self.assertEqual(first['status'],'pass')
            self.assertFalse(self.epoch()['report']['invalidated'])
        finally:
            commit.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[])
        self.assertEqual(process_inbox_once(),1)
        self.assertTrue(self.epoch()['report']['invalidated'])
        self.assertFalse(source.status(cutoff_ns=codec.stamp(cutoff))['historical_report_current'])

    def test_purge_locked_reread_preserves_concurrent_second_role_merge(self):
        _, receipt=self.ingest(self.callback())
        CommunityBridgeReceipt.objects.filter(pk=receipt.pk).update(created_at=timezone.now()-timedelta(days=31))
        started,finished=threading.Event(),threading.Event()
        errors=[]
        def purge():
            close_old_connections()
            try:
                started.set()
                call_command('purge_community_bridge_payloads',days=30,batch_size=1,stdout=StringIO())
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                finished.set()
                close_old_connections()
        with transaction.atomic():
            locked=CommunityBridgeReceipt.objects.select_for_update().get(pk=receipt.pk)
            thread=threading.Thread(target=purge)
            thread.start()
            self.assertTrue(started.wait(3))
            self.assertFalse(finished.wait(.2))
            evidence=deepcopy(locked.payload[codec.KEY])
            evidence['sources']['members']=codec.stamp(timezone.now())
            locked.payload={**locked.payload,codec.KEY:evidence}
            locked.save(update_fields=['payload'])
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[])
        receipt.refresh_from_db()
        self.assertEqual(set(receipt.payload),{codec.KEY})
        self.assertEqual(set(receipt.payload[codec.KEY]['sources']),{'bot','members'})

    def test_all_channel_monitor_waits_for_callback_without_reverse_channel_acquisition(self):
        acquired,finished=threading.Event(),threading.Event()
        errors=[]
        def monitor():
            close_old_connections()
            try:
                acquired.set()
                source.heartbeat('synthetic-process-1')
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                finished.set()
                close_old_connections()
        with transaction.atomic():
            CommunityBridgeChannel.objects.select_for_update().get(pk=self.rows[-1].pk)
            thread=threading.Thread(target=monitor)
            thread.start()
            self.assertTrue(acquired.wait(3))
            self.assertFalse(finished.wait(.2))
            # Callback only touches its one owned Channel/Receipt and cannot
            # request the monitor's earlier Channel while this lock is held.
            event = {**self.callback()['event'], 'channel': 'CROOM15'}
            payload = self.callback(event=event)
            self.ingest(payload)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[])
