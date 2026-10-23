"""Synthetic member-token public pages and real PostgreSQL privacy interleavings."""
from datetime import timedelta
from io import StringIO
import threading
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection, transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings, skipUnlessDBFeature, tag
from django.utils import timezone
from slack_sdk.errors import SlackApiError

from community_chat.models import CommunityChatDevice
from integrations.models import (
    BridgeSyncJob, CommunityBridgeChannel, CommunityBridgeDelivery, ExternalServiceConnection,
    SlackChannelMembership, SlackDmMirrorConversation, SlackDmMirrorGrant,
)
from integrations.services import slack_dm_mirror as dm, slack_channel_membership as membership
from integrations.services.community_bridge import member_audiences as audiences, member_channels as channels
from integrations.services.community_bridge.slack import SlackBridgeClient
from integrations.services.message_sync import member_history as member
from integrations.services.message_sync.history import ensure_state, public_page
from integrations.services.message_sync.scheduler import BudgetDeferred, claim_job, schedule_job, LeaseLost
from integrations.services.slack_chat_catalog import PUBLIC_CHANNEL_SCOPES
from integrations.services.slack_member_membership_epoch import invalidate_memberships_locked


SETTINGS = dict(MESSAGE_SYNC_ENABLED=True, SLACK_OWNER_INVENTORY_ENABLED=True, SLACK_MEMBER_CHANNELS_ENABLED=True,
                MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID='TTEST', SLACK_MEMBER_CHANNEL_ALLOWED_IDS='',
                MESSAGE_SYNC_SLACK_USER_APP_ID='AUSER', SLACK_COMMUNITY_SOURCE='bot',
                CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})


class Fixture:
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.now = int(timezone.now().timestamp())
        self.a, self.b = self.grant(1), self.grant(2)
        self.channel = CommunityBridgeChannel.objects.create(
            slack_workspace_id='TTEST', slack_channel_id='CROOM', destination_platform='buzz',
            destination_workspace_id='test.invalid', destination_channel_id=str(uuid.uuid4()), audience='members',
            enabled=True, slack_visibility='public', slack_seen_at=timezone.now(), mirror_decision='mirror',
            reader_grant=self.a, room_state={'applied': {'audience': 'members'}})
        self.state = ensure_state(self.channel)
        self.ack()
        self.calls = []
        self.real_transport = dm._invoke_slack_with_locked_connection
        transport = patch.object(dm, '_invoke_slack_with_locked_connection', side_effect=self.transport)
        transport.start()
        self.addCleanup(transport.stop)
        bot = patch.object(SlackBridgeClient, 'get_client', side_effect=AssertionError('member bot fallback'))
        self.bot = bot.start()
        self.addCleanup(bot.stop)
        reductions = patch.object(channels, '_schedule_audience_reductions')
        reductions.start()
        self.addCleanup(reductions.stop)

    def grant(self, n):
        user = get_user_model().objects.create_user(email=f'history-{n}@example.invalid')
        conn = ExternalServiceConnection.objects.create(user=user, provider='slack', status='connected',
            external_account_id='TTEST', access_token=f'synthetic-token-{n}', scopes=sorted(PUBLIC_CHANNEL_SCOPES),
            provider_metadata={'team': {'id': 'TTEST'}, 'authed_user': {'id': f'UHISTORY{n}'}})
        grant = SlackDmMirrorGrant.objects.create(user=user, connection=conn,
            slack_workspace_id='TTEST', slack_user_id=f'UHISTORY{n}', consented_at=timezone.now()-timedelta(minutes=2))
        SlackChannelMembership.objects.create(grant=grant, slack_workspace_id='TTEST', slack_channel_id='CROOM',
            state='joined', source='event', observed_at=timezone.now()-timedelta(seconds=10))
        CommunityChatDevice.objects.create(user=user, public_key=str(n)*64, status='verified', verified_at=timezone.now())
        return grant

    def ack(self):
        self.channel.refresh_from_db()
        keys = audiences.current_audience_keys('TTEST', 'CROOM')
        generation, digest = str(uuid.uuid4()), audiences.audience_hash(keys)
        self.channel.audience_generation, self.channel.audience_hash = uuid.UUID(generation), digest
        self.channel.audience_sync_pending, self.channel.room_sync_pending, self.channel.enabled = False, False, True
        self.channel.room_state = {**self.channel.room_state, 'audience_applied': {
            'generation': generation, 'hash': digest, 'workspace_id': 'TTEST', 'channel_id': 'CROOM',
            'destination_channel_id': self.channel.destination_channel_id}}
        self.channel.save()
        self.state.public_channel = self.channel

    def transport(self, authority, conn, method, **kwargs):
        fence=kwargs.pop("before_request",None)
        if fence is not None: fence()
        self.calls.append((authority.grant_id, method, kwargs))
        if method == 'conversations_info':
            return {'ok': True, 'channel': {'id': 'CROOM', 'is_channel': True, 'is_private': False, 'is_member': True}}
        return {'ok': True, 'messages': [{'ts': f'{self.now-30}.000001', 'user': 'UOTHER', 'text': 'synthetic history'}]}

    def run_page(self, kind='archive', *, source=''):
        schedule_job(self.state, kind, source_object_key=source)
        lease = claim_job(kinds=[kind])
        self.assertIsNotNone(lease)
        self.state.refresh_from_db()
        return public_page(lease, self.state)


@override_settings(**SETTINGS)
class MemberHistoryTests(Fixture, TransactionTestCase):
    def test_member_archive_uses_exact_reader_info_then_history_and_atomic_outbox(self):
        self.run_page()
        self.assertEqual([c[1] for c in self.calls], ['conversations_info', 'conversations_history', 'conversations_info'])
        self.assertEqual({c[0] for c in self.calls}, {self.a.pk})
        self.assertEqual(CommunityBridgeDelivery.objects.get().payload['text'], 'synthetic history')
        self.assertIsNotNone(self.state.jobs.get(kind='archive').completed_at)
        self.bot.assert_not_called()

    def test_head_and_replies_use_member_transport(self):
        self.run_page('head')
        self.calls.clear()
        self.run_page('thread', source=f'{self.now-60}.000001')
        self.assertEqual(self.calls[-2][1], 'conversations_replies')
        self.assertEqual(self.calls[-2][2]['ts'], f'{self.now-60}.000001')
        # The second observation supplies the real reply parent omitted from
        # the first synthetic head row. It repairs that parent once without a
        # second CREATE for the same source message.
        self.assertEqual(CommunityBridgeDelivery.objects.filter(delivery_type='create').count(), 1)
        correction=CommunityBridgeDelivery.objects.get(delivery_type='edit')
        self.assertEqual(correction.source_parent_message_id,f'{self.now-60}.000001')

    @override_settings(SLACK_COMMUNITY_SOURCE='members')
    def test_community_member_source_needs_no_members_ack(self):
        self.channel.audience, self.channel.room_state = 'community', {}
        self.channel.save()
        self.state.public_channel = self.channel
        self.run_page()
        self.assertEqual(self.calls[-2][1], 'conversations_history')
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 1)

    @override_settings(MESSAGE_SYNC_ENABLED=False)
    def test_flags_off_never_fall_back_to_bot_for_members(self):
        with self.assertRaises(member.MemberHistoryDeferred):
            self.run_page()
        self.assertEqual(self.calls, [])
        self.bot.assert_not_called()

    def test_no_reader_pauses_without_provider_and_discards_old_member_cursor(self):
        SlackChannelMembership.objects.update(state='left')
        job = self.state.jobs.get(kind='archive')
        job.checkpoint={'cursor':'old', 'member_authority':'old'}
        job.save()
        self.run_page()
        self.channel.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(self.channel.sync_paused_reason, 'no_reader')
        self.assertIsNone(self.channel.reader_grant_id)
        self.assertEqual(job.checkpoint, {})
        self.assertEqual(self.calls, [])

    def test_pending_audience_or_revoked_device_denies_before_any_provider_io(self):
        self.channel.audience_sync_pending=True
        self.channel.save()
        self.run_page()
        self.assertEqual(self.calls, [])
        BridgeSyncJob.objects.update(due_at=timezone.now())
        self.channel.audience_sync_pending=False
        self.channel.save()
        CommunityChatDevice.objects.filter(user=self.b.user).update(status='revoked', revoked_at=timezone.now())
        self.run_page()
        self.assertEqual(self.calls, [])
        self.assertFalse(CommunityBridgeDelivery.objects.exists())

    def test_stale_and_private_source_never_read(self):
        self.channel.slack_seen_at=timezone.now()-timedelta(minutes=6)
        self.channel.save()
        self.run_page()
        self.assertEqual(self.calls, [])
        self.channel.slack_seen_at=timezone.now()
        self.channel.slack_visibility='private'
        self.channel.save()
        BridgeSyncJob.objects.update(due_at=timezone.now())
        self.run_page()
        self.assertEqual(self.calls, [])

    def test_malformed_info_is_uncertainty_not_a_membership_departure(self):
        for result in (None, {}, {'ok':True,'channel':{'id':'CROOM','is_channel':True,'is_private':False}},
                       {'ok':True,'channel':{'id':'CROOM','is_channel':True,'is_private':False,'is_member':1}}):
            BridgeSyncJob.objects.update(due_at=timezone.now())
            with patch.object(dm, '_invoke_slack_with_locked_connection', return_value=result):
                self.run_page()
            self.assertEqual(set(SlackChannelMembership.objects.values_list('state',flat=True)), {'joined'})
            self.channel.refresh_from_db()
            self.assertEqual(self.channel.slack_visibility,'public')
            self.assertFalse(self.channel.audience_sync_pending)
        self.assertFalse(CommunityBridgeDelivery.objects.exists())

    def test_info_private_or_connect_holds_source_without_history_body(self):
        def shared(authority,conn,method,**kwargs):
            kwargs['before_request']()
            return {'ok':True,'channel':{'id':'CROOM','is_channel':True,'is_private':False,
                'is_member':True,'is_ext_shared':True}}
        with patch.object(dm, '_invoke_slack_with_locked_connection', side_effect=shared):
            with self.assertRaises(member.MemberHistoryDeferred):
                self.run_page()
        self.channel.refresh_from_db()
        self.assertFalse(self.channel.enabled)
        self.assertEqual(self.channel.hold_reason, 'slack_connect')
        self.assertFalse(CommunityBridgeDelivery.objects.exists())

    def post_read_denied(self,kind,proof):
        info_calls=0
        original=self.transport
        def provider(authority,conn,method,**kwargs):
            nonlocal info_calls
            if method=='conversations_info':
                info_calls+=1
                if info_calls==2:
                    kwargs['before_request']()
                    return proof
            return original(authority,conn,method,**kwargs)
        with patch.object(dm,'_invoke_slack_with_locked_connection',side_effect=provider):
            try: self.run_page(kind,source=f'{self.now-60}.000001' if kind=='thread' else '')
            except member.MemberHistoryDeferred: pass
        self.assertEqual(info_calls,2)
        self.assertFalse(CommunityBridgeDelivery.objects.exists())
        self.assertEqual(self.state.jobs.get(kind=kind).checkpoint,{})

    def test_history_post_info_private_discards_ordinary_unannotated_body(self):
        self.post_read_denied('archive',{'ok':True,'channel':{'id':'CROOM','is_private':True,'is_channel':True,'is_member':True}})

    def test_replies_post_info_private_discards_ordinary_unannotated_body(self):
        self.post_read_denied('thread',{'ok':True,'channel':{'id':'CROOM','is_private':True,'is_channel':True,'is_member':True}})

    def test_history_post_info_connect_discards_ordinary_unannotated_body(self):
        self.post_read_denied('archive',{'ok':True,'channel':{'id':'CROOM','is_private':False,'is_channel':True,'is_member':True,'is_ext_shared':True}})

    def test_replies_post_info_connect_discards_ordinary_unannotated_body(self):
        self.post_read_denied('thread',{'ok':True,'channel':{'id':'CROOM','is_private':False,'is_channel':True,'is_member':True,'is_ext_shared':True}})

    def test_history_post_info_missing_proof_discards_body_without_departure(self):
        self.post_read_denied('archive',{})
        self.assertEqual(SlackChannelMembership.objects.get(grant=self.a).state,'joined')

    def test_replies_post_info_malformed_proof_discards_body_without_departure(self):
        self.post_read_denied('thread',{'ok':True,'channel':{'id':'CROOM','is_private':False,'is_channel':True,'is_member':1}})
        self.assertEqual(SlackChannelMembership.objects.get(grant=self.a).state,'joined')

    def test_post_info_explicit_departure_discards_body_and_records_left(self):
        self.post_read_denied('archive',{'ok':True,'channel':{'id':'CROOM','is_private':False,'is_channel':True,'is_member':False}})
        self.assertEqual(SlackChannelMembership.objects.get(grant=self.a).state,'left')

    def post_budget_provider(self,error,*,second_error=None):
        original=self.transport
        self.info_attempts=0
        def provider(authority,conn,method,**kwargs):
            if method=='conversations_info':
                self.info_attempts+=1
                if self.info_attempts==2: raise error
                if self.info_attempts==3 and second_error is not None: raise second_error
            return original(authority,conn,method,**kwargs)
        return provider

    def test_post_info_short_admission_refusal_waits_once_without_committing_body_early(self):
        provider=self.post_budget_provider(BudgetDeferred(2,before_request_method='conversations.info'))
        def wait(seconds):
            self.assertEqual(seconds,2)
            self.assertFalse(CommunityBridgeDelivery.objects.exists())
        with patch.object(dm,'_invoke_slack_with_locked_connection',side_effect=provider), \
             patch.object(member.time,'sleep',side_effect=wait) as sleep:
            self.run_page()
        sleep.assert_called_once_with(2)
        self.assertEqual(self.info_attempts,3)
        self.assertEqual(CommunityBridgeDelivery.objects.count(),1)

    def test_post_info_long_cooldown_or_429_never_retries(self):
        for error in (BudgetDeferred(4,before_request_method='conversations.info'),BudgetDeferred(1)):
            BridgeSyncJob.objects.update(lease_token=None,lease_expires_at=None,due_at=timezone.now())
            provider=self.post_budget_provider(error)
            with patch.object(dm,'_invoke_slack_with_locked_connection',side_effect=provider),patch.object(member.time,'sleep') as sleep:
                with self.assertRaises(BudgetDeferred):self.run_page()
            sleep.assert_not_called()
            self.assertEqual(self.info_attempts,2)
            self.assertFalse(CommunityBridgeDelivery.objects.exists())

    def test_post_info_second_deferral_discards_body_without_another_wait(self):
        error=BudgetDeferred(1,before_request_method='conversations.info')
        provider=self.post_budget_provider(error,second_error=error)
        with patch.object(dm,'_invoke_slack_with_locked_connection',side_effect=provider),patch.object(member.time,'sleep') as sleep:
            with self.assertRaises(BudgetDeferred):self.run_page()
        sleep.assert_called_once_with(1)
        self.assertEqual(self.info_attempts,3)
        self.assertFalse(CommunityBridgeDelivery.objects.exists())

    def test_lease_expiring_in_post_admission_wait_prevents_next_info_and_all_writes(self):
        provider=self.post_budget_provider(BudgetDeferred(1,before_request_method='conversations.info'))
        def wait(_):BridgeSyncJob.objects.filter(kind='archive').update(lease_expires_at=timezone.now()-timedelta(seconds=1))
        with patch.object(dm,'_invoke_slack_with_locked_connection',side_effect=provider),patch.object(member.time,'sleep',side_effect=wait):
            with self.assertRaises(LeaseLost):self.run_page()
        self.assertEqual(self.info_attempts,2)
        self.assertFalse(CommunityBridgeDelivery.objects.exists())

    def test_expired_lease_after_info_never_starts_history(self):
        original=self.transport
        def expire(authority,conn,method,**kwargs):
            result=original(authority,conn,method,**kwargs)
            if method=='conversations_info':
                BridgeSyncJob.objects.filter(kind='archive').update(lease_expires_at=timezone.now()-timedelta(seconds=1))
            return result
        with patch.object(dm, '_invoke_slack_with_locked_connection', side_effect=expire):
            with self.assertRaises(LeaseLost): self.run_page()
        self.assertEqual([c[1] for c in self.calls], ['conversations_info'])
        self.assertFalse(CommunityBridgeDelivery.objects.exists())

    def test_lease_expiring_during_quota_admission_prevents_actual_sdk_io(self):
        from integrations.services.message_sync import slack_client
        sdk_calls=[]
        class Sdk:
            def __init__(self,**kwargs): self.retry_handlers=[object()]
            def conversations_info(self,**kwargs): return self.api_call('conversations.info',params=kwargs)
            def api_call(self,method,**kwargs):
                sdk_calls.append(method)
                raise AssertionError('expired lease reached SDK')
        def admission(**kwargs):
            BridgeSyncJob.objects.filter(kind='archive').update(lease_expires_at=timezone.now()-timedelta(seconds=1))
        with patch.object(dm,'WebClient',Sdk), patch.object(dm,'_invoke_slack_with_locked_connection',wraps=self.real_transport), \
             patch.object(slack_client,'admit_request',side_effect=admission):
            with self.assertRaises(LeaseLost): self.run_page()
        self.assertEqual(sdk_calls,[])
        self.assertFalse(CommunityBridgeDelivery.objects.exists())

    def test_generation_ceiling_cannot_roll_back_a_privacy_departure(self):
        self.state.authority_generation=2**63-1
        self.state.save()
        self.assertTrue(membership.mark_left(self.a,'CROOM',observed_at=timezone.now()))
        self.state.refresh_from_db();self.channel.refresh_from_db()
        self.assertEqual(self.state.status,'revoked')
        self.assertFalse(self.channel.enabled)
        self.assertEqual(SlackChannelMembership.objects.get(grant=self.a).state,'left')

    def test_expired_lease_after_history_discards_body_and_checkpoint(self):
        original=self.transport
        def expire(authority,conn,method,**kwargs):
            result=original(authority,conn,method,**kwargs)
            if method=='conversations_history':
                BridgeSyncJob.objects.filter(kind='archive').update(lease_expires_at=timezone.now()-timedelta(seconds=1))
            return result
        with patch.object(dm, '_invoke_slack_with_locked_connection', side_effect=expire):
            with self.assertRaises(LeaseLost): self.run_page()
        self.assertFalse(CommunityBridgeDelivery.objects.exists())
        self.assertEqual(self.state.jobs.get(kind='archive').checkpoint,{})

    def test_new_reader_cannot_reuse_another_readers_cursor(self):
        original=self.transport
        def paged(*args,**kwargs):
            result=original(*args,**kwargs)
            if args[2]=='conversations_history':
                result.update(has_more=True,response_metadata={'next_cursor':'reader-A-only'})
            return result
        with patch.object(dm,'_invoke_slack_with_locked_connection',side_effect=paged): self.run_page()
        self.channel.reader_grant=self.b
        self.channel.save()
        self.calls.clear()
        self.run_page()
        self.assertEqual(self.calls[-2][0],self.b.pk)
        self.assertNotIn('cursor',self.calls[-2][2])

    def test_authority_epoch_and_membership_leave_rejoin_restart_thread_cursor(self):
        root=f'{self.now-60}.000001'
        original=self.transport
        def paged(*args,**kwargs):
            result=original(*args,**kwargs)
            if args[2]=='conversations_replies': result.update(has_more=True,response_metadata={'next_cursor':'old-join'})
            return result
        with patch.object(dm,'_invoke_slack_with_locked_connection',side_effect=paged): self.run_page('thread',source=root)
        before=self.state.authority_generation
        membership.mark_left(self.a,'CROOM',observed_at=timezone.now())
        membership.mark_joined(self.a,'CROOM',observed_at=timezone.now()+timedelta(microseconds=1),public_verified=True)
        self.state.refresh_from_db()
        self.assertNotEqual(self.state.authority_generation,before)
        self.ack()
        self.calls.clear()
        self.run_page('thread',source=root)
        self.assertNotIn('cursor',self.calls[-2][2])

    @override_settings(MESSAGE_SYNC_RECENT_HISTORY_ENABLED=True)
    def test_scroll_demand_uses_same_reader_scope_and_preserves_partial_cursor_without_io(self):
        from integrations.services.slack_history_pages import _public_progress
        original=self.transport
        def partial(*args,**kwargs):
            result=original(*args,**kwargs)
            if args[2]=='conversations_history': result.update(has_more=True,response_metadata={'next_cursor':'current-page'})
            return result
        with patch.object(dm,'_invoke_slack_with_locked_connection',side_effect=partial): self.run_page()
        before=self.state.jobs.get(kind='archive').checkpoint
        self.calls.clear()
        result=member.request_history(self.a.user,self.channel,'1'*64,'a'*64,_public_progress)
        self.assertTrue(result['pending'])
        after=self.state.jobs.get(kind='archive').checkpoint
        self.assertEqual(after['cursor'],'current-page')
        self.assertEqual(after['member_authority'],before['member_authority'])
        self.assertEqual(self.calls,[])
        self.run_page()
        self.assertEqual(self.calls[-2][2]['cursor'],'current-page')

    def test_scroll_current_membership_and_verified_key_are_required(self):
        from integrations.services.slack_owner_inventory_api import InventoryError
        from integrations.services.slack_history_pages import _public_progress
        outsider=self.grant(3)
        SlackChannelMembership.objects.filter(grant=outsider).update(state='left')
        with self.assertRaises(InventoryError):
            member.request_history(outsider.user,self.channel,'3'*64,'a'*64,_public_progress)
        self.assertEqual(self.calls,[])

    @override_settings(SLACK_COMMUNITY_SOURCE='members')
    def test_community_scroll_does_not_require_requester_slack_identity(self):
        from integrations.services.slack_history_pages import _public_progress
        requester=get_user_model().objects.create_user(email='unlinked-history@example.invalid')
        CommunityChatDevice.objects.create(user=requester,public_key='c'*64,status='verified',verified_at=timezone.now())
        self.channel.audience,self.channel.room_state='community',{}
        self.channel.save()
        result=member.request_history(requester,self.channel,'c'*64,'a'*64,_public_progress)
        self.assertTrue(result['pending'])
        self.assertFalse(SlackDmMirrorGrant.objects.filter(user=requester).exists())
        self.assertEqual(self.calls,[])

    def test_ninth_verified_device_requires_full_new_audience_before_history(self):
        from integrations.services.slack_owner_inventory_api import InventoryError
        from integrations.services.slack_history_pages import _public_progress
        key = f"{39:064x}"
        for number in range(8):
            CommunityChatDevice.objects.create(user=self.a.user, public_key=f"{number + 32:064x}",
                status="verified", verified_at=timezone.now())
        with self.assertRaises(InventoryError):
            member.request_history(self.a.user, self.channel, key, 'a' * 64, _public_progress)
        self.ack()
        result = member.request_history(self.a.user, self.channel, key, 'a' * 64, _public_progress)
        self.assertTrue(result['pending'])
        self.assertEqual(self.calls, [])

    def test_info_explicit_false_records_departure_without_history(self):
        def no_longer_member(authority,conn,method,**kwargs):
            kwargs.pop("before_request")()
            self.calls.append((authority.grant_id,method,kwargs))
            return {'ok':True,'channel':{'id':'CROOM','is_channel':True,'is_private':False,'is_member':False}}
        with patch.object(dm,'_invoke_slack_with_locked_connection',side_effect=no_longer_member):
            with self.assertRaises(member.MemberHistoryDeferred): self.run_page()
        self.assertEqual([call[1] for call in self.calls],['conversations_info'])
        self.assertEqual(SlackChannelMembership.objects.get(grant=self.a).state,'left')
        self.assertFalse(CommunityBridgeDelivery.objects.exists())

    def test_malformed_history_and_contradictory_source_metadata_discard_bodies(self):
        original=self.transport
        for hint in ({'ok':False}, {'ok':True,'messages':[],'channel':{'id':'COTHER','is_private':True}}):
            BridgeSyncJob.objects.update(due_at=timezone.now())
            def malformed(authority,conn,method,**kwargs):
                return original(authority,conn,method,**kwargs) if method=='conversations_info' else hint
            with patch.object(dm,'_invoke_slack_with_locked_connection',side_effect=malformed): self.run_page()
            self.channel.refresh_from_db()
            self.assertEqual(self.channel.slack_visibility,'public')
            self.assertTrue(self.channel.enabled)
        self.assertFalse(CommunityBridgeDelivery.objects.exists())

    @override_settings(SLACK_COMMUNITY_SOURCE='members')
    def test_refresh_authorization_failure_selects_second_reader_without_bot(self):
        original=dm._capture_slack_grant_api_authority
        def fail_a(grant,**kwargs):
            if grant.pk==self.a.pk and kwargs.get('refresh_token'):
                raise dm.SlackDmMirrorCredentialError('synthetic refresh rejected')
            return original(grant,**kwargs)
        self.channel.audience,self.channel.room_state='community',{}
        self.channel.save()
        with patch.object(dm,'_capture_slack_grant_api_authority',side_effect=fail_a): self.run_page()
        self.assertEqual({call[0] for call in self.calls},{self.b.pk})
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.reader_grant_id,self.b.pk)

    def test_direct_repairs_cannot_use_bot_or_replay_members(self):
        for action in (lambda:SlackBridgeClient.get_channel_history(channel_id='CROOM'),
                       lambda:SlackBridgeClient.get_thread_messages(channel_id='CROOM',root_message_id='1.000001')):
            with self.assertRaisesRegex(RuntimeError,'durable'): action()
        with self.assertRaises(CommandError): member.reject_legacy_repair(self.channel)
        with self.assertRaises(CommandError):
            call_command('backfill_community_bridge_thread_references',slack_channel_id='CROOM',stdout=StringIO())
        from integrations.services.message_sync.public_repair import repair_observed_message
        with self.assertRaises(member.MemberHistoryDeferred):
            repair_observed_message(self.channel,{'ts':'1.000001'},read_started_at=timezone.now())
        self.bot.assert_not_called()

    @skipUnlessDBFeature("has_select_for_update")
    def test_no_user_locks_are_reacquired_after_state(self):
        def inspect(execute,sql,params,many,context):
            lowered=sql.lower()
            first_table = lowered.split(' from ',1)[1].split()[0] if ' from ' in lowered else ''
            if 'for update' in lowered and first_table == '"bridge_sync_state"': self.state_locked=True
            if self.state_locked and 'for update' in lowered and first_table in {'"core_user"','"slack_dm_mirror_grant"'}:
                raise AssertionError('authority lock acquired below State')
            return execute(sql,params,many,context)
        self.state_locked=False
        # Claim is deliberately outside the page's new full authority order.
        lease=claim_job(kinds=['archive'])
        with connection.execute_wrapper(inspect): public_page(lease,self.state)
        self.assertTrue(self.state_locked)
        self.assertEqual(CommunityBridgeDelivery.objects.count(),1)

    def test_not_in_channel_reduces_then_real_full_audience_lane_reopens_for_B(self):
        original=self.transport
        def departed(authority,conn,method,**kwargs):
            if authority.grant_id==self.a.pk:
                kwargs['before_request']()
                raise SlackApiError('synthetic',{'ok':False,'error':'not_in_channel'})
            return original(authority,conn,method,**kwargs)
        with patch.object(dm,'_invoke_slack_with_locked_connection',side_effect=departed):
            with self.assertRaises(member.MemberHistoryDeferred): self.run_page()
        self.channel.refresh_from_db()
        self.assertFalse(self.channel.enabled)
        self.assertTrue(self.channel.audience_sync_pending)
        self.assertEqual(SlackChannelMembership.objects.get(grant=self.a).state,'left')
        self.assertFalse(CommunityBridgeDelivery.objects.exists())
        self.channel.reader_grant=self.b
        self.channel.save()
        # Real synchronizer validates current authority and emits the synthetic
        # adapter CAS; this is not a direct replacement of a pending ACK.
        oldkeys=['1'*64,'2'*64]
        generation=str(uuid.uuid4())
        with patch.object(audiences.BuzzBridgeClient,'inspect_member_audience',return_value={
                'participant_pubkeys':oldkeys,'generation':str(self.channel.audience_generation)}), \
             patch.object(audiences.BuzzBridgeClient,'update_member_audience',return_value={'generation':generation}) as cas:
            self.assertEqual(audiences.sync_audience(self.channel.pk),'applied')
        self.assertEqual(cas.call_args.kwargs['participant_pubkeys'],['2'*64])
        self.state.refresh_from_db()
        BridgeSyncJob.objects.update(due_at=timezone.now(),lease_token=None,lease_expires_at=None)
        self.calls.clear()
        self.run_page()
        self.assertEqual({c[0] for c in self.calls},{self.b.pk})
        self.assertEqual(CommunityBridgeDelivery.objects.count(),1)


@tag('postgres-only')
@skipUnlessDBFeature('has_select_for_update')
@override_settings(**SETTINGS)
class MemberHistoryConcurrencyTests(Fixture, TransactionTestCase):
    def thread(self,action,errors):
        def run():
            close_old_connections()
            try: action()
            except BaseException as error: errors.append(type(error).__name__+':'+str(error))
            finally: connection.close()
        worker=threading.Thread(target=run,daemon=True)
        worker.start()
        return worker

    def test_current_page_holds_reader_and_other_members_device_authority_through_commit(self):
        started,release,writer_started,writer_done=(threading.Event() for _ in range(4))
        errors=[]
        original=self.transport
        def slow(authority,conn,method,**kwargs):
            if method=='conversations_history':
                started.set()
                if not release.wait(8): raise AssertionError('release timeout')
            return original(authority,conn,method,**kwargs)
        def revoke_device():
            writer_started.set()
            with transaction.atomic():
                get_user_model().objects.select_for_update().get(pk=self.b.user_id)
                CommunityChatDevice.objects.filter(user_id=self.b.user_id).update(status='revoked',revoked_at=timezone.now())
                channels.mark_member_channels_pending(self.b.user_id,reason='device_revoked',reduction=True)
            writer_done.set()
        with patch.object(dm,'_invoke_slack_with_locked_connection',side_effect=slow):
            page=self.thread(lambda:self.run_page(),errors)
            self.assertTrue(started.wait(5))
            writer=self.thread(revoke_device,errors)
            self.assertTrue(writer_started.wait(5))
            self.assertFalse(writer_done.wait(.2))
            release.set()
            page.join(10);writer.join(10)
        self.assertFalse(page.is_alive());self.assertFalse(writer.is_alive())
        self.assertEqual(errors,[])
        self.assertTrue(writer_done.is_set())
        self.assertEqual(CommunityBridgeDelivery.objects.count(),1)
        self.channel.refresh_from_db()
        self.assertFalse(self.channel.enabled)

    def test_real_shared_info_budget_allows_post_proof_after_one_bounded_wait(self):
        from integrations.models import BridgeApiBudget
        sdk_calls=[]
        fixture=self
        class Sdk:
            def __init__(self,**kwargs):self.retry_handlers=[object()]
            def conversations_info(self,**kwargs):return self.api_call('conversations.info',params=kwargs)
            def conversations_history(self,**kwargs):return self.api_call('conversations.history',params=kwargs)
            def api_call(self,method,**kwargs):
                sdk_calls.append(method)
                if method=='conversations.info':
                    return {'ok':True,'channel':{'id':'CROOM','is_private':False,'is_channel':True,'is_member':True}}
                return {'ok':True,'messages':[{'ts':f'{fixture.now-30}.000001','user':'UOTHER','text':'synthetic'}]}
        with patch.object(dm,'WebClient',Sdk),patch.object(dm,'_invoke_slack_with_locked_connection',wraps=self.real_transport), \
             patch.object(member.time,'sleep',wraps=member.time.sleep) as wait:
            self.run_page()
        self.assertEqual(sdk_calls,['conversations.info','conversations.history','conversations.info'])
        wait.assert_called_once()
        self.assertLessEqual(wait.call_args.args[0],member.POST_READ_ADMISSION_WAIT_SECONDS)
        self.assertEqual(BridgeApiBudget.objects.filter(app_id='AUSER',workspace_id='TTEST',method='conversations.info').count(),1)
        self.assertEqual(CommunityBridgeDelivery.objects.count(),1)

    def test_pause_winning_before_page_fence_prevents_provider_read(self):
        captured,resume=(threading.Event() for _ in range(2))
        errors=[]
        original=dm._capture_slack_grant_api_authority
        def capture(*args,**kwargs):
            value=original(*args,**kwargs)
            captured.set()
            if not resume.wait(8): raise AssertionError('capture timeout')
            return value
        with patch.object(dm,'_capture_slack_grant_api_authority',side_effect=capture):
            page=self.thread(lambda:self.run_page(),errors)
            self.assertTrue(captured.wait(5))
            with transaction.atomic():
                get_user_model().objects.select_for_update().get(pk=self.a.user_id)
                SlackDmMirrorGrant.objects.filter(pk=self.a.pk).update(status='paused')
                invalidate_memberships_locked(self.a.user_id)
            resume.set();page.join(10)
        self.assertFalse(page.is_alive())
        self.assertEqual(errors,[])
        self.assertEqual(self.calls,[])
        self.assertFalse(CommunityBridgeDelivery.objects.exists())


@override_settings(**SETTINGS)
class MemberProviderBudgetFenceTests(SimpleTestCase):
    def sdk_client(self,order):
        from types import SimpleNamespace
        return SimpleNamespace(api_call=lambda method,**kwargs: order.append('sdk') or {'ok':True},
                               retry_handlers=[object()])

    def test_fence_runs_after_admission_and_sdk_retries_are_disabled(self):
        from integrations.services.message_sync.slack_client import budgeted_client
        order=[]
        sdk=self.sdk_client(order)
        with patch('integrations.services.message_sync.slack_client.admit_request',side_effect=lambda **kw:order.append('admitted')):
            client=budgeted_client(sdk,workspace_id='TTEST',app_id='AUSER',before_request=lambda:order.append('fence'))
            self.assertEqual(client.retry_handlers,[])
            client.api_call('conversations.info')
        self.assertEqual(order,['admitted','fence','sdk'])

    def test_lost_lease_does_not_call_sdk_or_leak_fence_to_next_default_client(self):
        from integrations.services.message_sync.slack_client import budgeted_client
        order=[]
        def expired():
            order.append('fence')
            raise LeaseLost('synthetic_expired')
        with patch('integrations.services.message_sync.slack_client.admit_request'):
            stale=budgeted_client(self.sdk_client(order),workspace_id='TTEST',app_id='AUSER',before_request=expired)
            with self.assertRaises(LeaseLost): stale.api_call('conversations.info')
            fresh=budgeted_client(self.sdk_client(order),workspace_id='TTEST',app_id='AUSER')
            self.assertEqual(fresh.api_call('conversations.info'),{'ok':True})
        self.assertEqual(order,['fence','sdk'])
