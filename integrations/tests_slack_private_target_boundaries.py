"""Private destination references must belong to the current owner room."""
from datetime import timedelta
from unittest.mock import patch

from django.db import transaction
from django.test import TransactionTestCase
from django.utils import timezone

from community_chat.tests.test_slack_dm_io_authority import SlackDmIoAuthorityFixture
from integrations.models import SlackDmMirrorDelivery
from integrations.services import slack_dm_mirror as dm


class PrivateTargetBoundaryTests(SlackDmIoAuthorityFixture, TransactionTestCase):
    def row(self, *, operation='create', source_platform='slack', boundary='old-boundary', **metadata):
        return SlackDmMirrorDelivery.objects.create(
            conversation=self.conversation, source_platform=source_platform,
            source_message_id=f'{int(timezone.now().timestamp())-120}.000001',
            source_author_id='UOTHER', operation=operation, status='completed',
            completed_at=timezone.now(), available_at=timezone.now(), encrypted_text='',
            metadata={'participant_hash': boundary, 'destination_message_id': 'a'*64, **metadata},
        )

    def test_old_completed_parent_is_not_a_current_destination(self):
        parent = self.row()
        self.assertEqual(dm._private_destination_message_id(self.conversation, parent.source_message_id), '')
        parent.metadata['participant_hash'] = self.conversation.participant_hash
        parent.save()
        self.assertEqual(dm._private_destination_message_id(self.conversation, parent.source_message_id), 'a'*64)

    def test_old_completed_reaction_and_outbound_fallback_are_not_targets(self):
        reaction = self.row(operation='reaction_add', reaction_object_id='reaction-object')
        self.assertEqual(dm._private_destination_operation_message_id(self.conversation,
            source_message_id='reaction-object', operation='reaction_add', metadata_key='reaction_object_id'), '')
        outbound = self.row(source_platform='buzz', slack_ts='123.000001', source_event_id='b'*64)
        self.assertEqual(dm._private_destination_message_id(self.conversation, '123.000001'), '')
        self.assertEqual(dm._slack_destination_message_id(self.conversation, outbound.source_message_id), '')
        reaction.delete()
        outbound.operation = 'reaction_add'
        outbound.metadata['slack_reaction_object_id'] = 'reaction-object'
        outbound.save()
        self.assertEqual(dm._private_destination_operation_message_id(self.conversation,
            source_message_id='reaction-object', operation='reaction_add', metadata_key='reaction_object_id'), '')

    @transaction.atomic
    def test_fresh_observation_rebuilds_old_completed_parent_without_relabeling_destination(self):
        parent = self.row(history_reconcile_candidate=True)
        fresh = {'participant_hash': self.conversation.participant_hash, 'backfill': True, 'history_scan_epoch': 'fresh'}
        dm._upsert_history_delivery(self.conversation, source_message_id=parent.source_message_id,
            author_id='UOTHER', operation='create', text='freshly fetched source', metadata=fresh, held_until=timezone.now())
        parent.refresh_from_db()
        self.assertEqual(parent.status, 'pending')
        self.assertEqual(parent.encrypted_text, 'freshly fetched source')
        self.assertNotIn('destination_message_id', parent.metadata)
        self.assertNotIn('history_reconcile_candidate', parent.metadata)
        self.assertIsNone(parent.completed_at)
        self.assertEqual(dm._private_destination_message_id(self.conversation, parent.source_message_id), '')

    def test_current_completed_parent_remains_deduplicated(self):
        parent = self.row(boundary=self.conversation.participant_hash, history_reconcile_candidate=True)
        dm._upsert_history_delivery(self.conversation, source_message_id=parent.source_message_id,
            author_id='UOTHER', operation='create', text='fresh source body',
            metadata={'participant_hash': self.conversation.participant_hash, 'backfill': True}, held_until=timezone.now())
        parent.refresh_from_db()
        self.assertEqual(parent.status, 'completed')
        self.assertEqual(parent.encrypted_text, '')
        self.assertEqual(parent.metadata['destination_message_id'], 'a'*64)
        self.assertNotIn('history_reconcile_candidate', parent.metadata)

    def test_stale_pending_dependency_cannot_hold_a_current_reply_forever(self):
        parent = self.row()
        parent.status = 'pending'
        parent.save()
        self.assertFalse(dm._mlai_target_dependency_can_progress(self.conversation, parent.source_message_id))
        parent.metadata['participant_hash'] = self.conversation.participant_hash
        parent.save()
        self.assertTrue(dm._mlai_target_dependency_can_progress(self.conversation, parent.source_message_id))


class PrivateReplyRecoveryTests(SlackDmIoAuthorityFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        from integrations.services.message_sync.history import ensure_state
        self.grant.consented_at = timezone.now() - timedelta(days=1)
        self.grant.save()
        self.conversation.participant_buzz_pubkeys = [self.owner_key, '2'*64]
        self.conversation.participant_identity_map = {'UOWNER': self.owner_key, 'UOTHER': '2'*64}
        self.conversation.history_backfilled_at = timezone.now()-timedelta(hours=1)
        self.conversation.save()
        self.state = ensure_state(self.conversation)
        now = int(timezone.now().timestamp())
        self.parent = SlackDmMirrorDelivery.objects.create(
            conversation=self.conversation, source_platform='slack', source_message_id=f'{now-180}.000001',
            source_author_id='UOTHER', operation='create', status='completed', completed_at=timezone.now(),
            available_at=timezone.now(), metadata={'participant_hash':'retired-boundary', 'destination_message_id':'a'*64},
        )
        self.child = SlackDmMirrorDelivery.objects.create(
            conversation=self.conversation, source_platform='slack', source_message_id=f'{now-120}.000001',
            source_author_id='UOTHER', operation='create', status='dead', available_at=timezone.now(),
            encrypted_text='old rejected body must never be replayed',
            last_error='BuzzBridgePermanentError: MLAI Chat adapter rejected request with HTTP 400',
            metadata={'participant_hash':self.conversation.participant_hash, 'backfill':False,
                      'thread_ts':self.parent.source_message_id, 'permanent_failure':True, 'history_recovery_scheduled':True},
        )

    @transaction.atomic
    def observe(self, epoch='fresh-archive', *, parent=True, child=True):
        dm._ensure_history_state(self.conversation, source_message_id=dm.HISTORY_MAIN_STATE_ID,
                                metadata={'scan_epoch':epoch, 'complete':True, 'history_scan_state':'main'})
        for row, text in ([(self.parent,'fresh parent')] if parent else []) + ([(self.child,'fresh child')] if child else []):
            dm._upsert_history_delivery(self.conversation, source_message_id=row.source_message_id,
                author_id='UOTHER', operation='create', text=text, held_until=timezone.now(),
                metadata={'participant_hash':self.conversation.participant_hash, 'backfill':True,
                          'history_scan_epoch':epoch, 'thread_ts':self.parent.source_message_id if row.pk == self.child.pk else ''})

    def qualify(self, epoch='fresh-archive', limited=False):
        from django.db import transaction
        from integrations.services.message_sync.parent_recovery import qualify_reply_recovery
        with transaction.atomic():
            qualify_reply_recovery(self.conversation, scan_epoch=epoch, source_limited=limited)

    def schedule(self):
        from integrations.services.message_sync.recovery import schedule_private_recoveries
        return schedule_private_recoveries()

    def test_known_live_failure_is_erased_then_fresh_reply_waits_for_current_parent(self):
        from integrations.services.message_sync.parent_recovery import CONTRACT_KEY
        self.assertEqual(self.schedule(), 1)
        self.child.refresh_from_db()
        self.assertEqual(self.child.status,'dead')
        self.assertEqual(self.child.encrypted_text,'')
        self.assertEqual(self.child.metadata[CONTRACT_KEY]['old_parent_delivery_id'],self.parent.pk)
        self.observe()
        self.qualify()
        self.child.refresh_from_db()
        self.assertEqual(self.child.status,'pending')
        self.assertEqual(self.child.encrypted_text,'fresh child')
        self.assertNotIn('permanent_failure',self.child.metadata)
        with patch.object(dm.BuzzBridgeClient,'deliver_private') as deliver:
            with self.assertRaises(dm.SlackDmMirrorDependencyPending):
                dm._deliver_to_mlai(self.child)
            deliver.assert_not_called()
            self.parent.refresh_from_db()
            self.parent.status='completed'
            self.parent.metadata['destination_message_id']='b'*64
            self.parent.save()
            deliver.return_value={'message_id':'c'*64}
            self.child.status='processing'
            self.child.save()
            dm._deliver_to_mlai(self.child)
        self.assertEqual(deliver.call_args.kwargs['parent_message_id'],'b'*64)
        self.assertEqual(deliver.call_args.kwargs['text'],'fresh child')

    def test_limited_source_erases_staging_and_later_fresh_scan_can_recover(self):
        self.assertEqual(self.schedule(),1)
        self.observe()
        self.qualify(limited=True)
        self.child.refresh_from_db()
        self.assertEqual(self.child.status,'dead')
        self.assertTrue(self.child.metadata['permanent_failure'])
        self.assertEqual(self.child.encrypted_text,'')
        self.observe('later-archive')
        self.qualify('later-archive')
        self.child.refresh_from_db()
        self.assertEqual(self.child.status,'pending')
        self.assertEqual(self.child.encrypted_text,'fresh child')

    def test_parent_observation_preserves_failure_diagnosis_before_replacing_old_mapping(self):
        from integrations.services.message_sync.parent_recovery import CONTRACT_KEY
        self.observe()
        self.child.refresh_from_db()
        self.assertEqual(self.child.metadata[CONTRACT_KEY]['old_parent_destination_id'],'a'*64)
        self.assertEqual(self.child.status,'dead')
        self.qualify()
        self.child.refresh_from_db()
        self.assertEqual(self.child.status,'pending')

    def test_retention_erased_fresh_body_requires_another_source_observation(self):
        self.schedule()
        self.observe()
        SlackDmMirrorDelivery.objects.filter(pk=self.child.pk).update(encrypted_text='')
        self.qualify()
        self.child.refresh_from_db()
        self.assertEqual(self.child.status,'dead')
        self.assertTrue(self.child.metadata['permanent_failure'])
        self.observe('later-archive')
        self.qualify('later-archive')
        self.child.refresh_from_db()
        self.assertEqual(self.child.status,'pending')
        self.assertEqual(self.child.encrypted_text,'fresh child')

    def test_other_permanent_failure_and_current_parent_remain_fenced(self):
        self.parent.metadata['participant_hash']=self.conversation.participant_hash
        self.parent.save()
        self.assertEqual(self.schedule(),0)
        self.parent.metadata['participant_hash']='retired-boundary'
        self.parent.save()
        self.child.last_error='BuzzBridgePermanentError: MLAI Chat adapter rejected request with HTTP 422'
        self.child.save()
        self.assertEqual(self.schedule(),0)
        self.child.refresh_from_db()
        self.assertTrue(self.child.metadata['permanent_failure'])

    def test_old_window_and_revoked_device_cannot_authorize_repair(self):
        self.child.source_message_id=f'{int(timezone.now().timestamp())-31*86400}.000001'
        self.child.save()
        self.assertEqual(self.schedule(),0)
        self.child.source_message_id=f'{int(timezone.now().timestamp())-120}.000001'
        self.child.save()
        from community_chat.models import CommunityChatDevice
        CommunityChatDevice.objects.filter(user=self.user).update(revoked_at=timezone.now())
        self.assertEqual(self.schedule(),0)

    def test_qualified_absence_supersedes_without_replaying_failed_body(self):
        self.schedule()
        self.observe(child=False)
        self.qualify()
        self.child.refresh_from_db()
        self.assertEqual(self.child.status,'dead')
        self.assertEqual(self.child.encrypted_text,'')
        self.assertTrue(self.child.metadata['history_recovery_superseded'])
        self.assertNotIn('permanent_failure',self.child.metadata)

    def test_empty_body_prior_qualification_is_invalidated_when_staging_is_erased(self):
        from integrations.services.message_sync import parent_recovery
        self.schedule()
        self.observe()
        # An attachment-only Slack message can have a legitimately empty body.
        with transaction.atomic():
            dm._upsert_history_delivery(self.conversation, source_message_id=self.child.source_message_id,
                author_id='UOTHER', operation='create', text='', held_until=timezone.now(),
                metadata={'participant_hash':self.conversation.participant_hash, 'backfill':True,
                          'history_scan_epoch':'fresh-archive', 'thread_ts':self.parent.source_message_id})
        # Hold a previously qualified row before the release step, then lose
        # source coverage. The empty digest must not count as retained evidence.
        with patch.object(parent_recovery, 'release_qualified_replies'):
            self.qualify()
        self.child.refresh_from_db()
        self.assertEqual(self.child.metadata[parent_recovery.CONTRACT_KEY]['outcome'], 'waiting_for_current_parent')
        self.qualify('later-limited', limited=True)
        with transaction.atomic():
            parent_recovery.release_qualified_replies(self.conversation)
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'dead')
        self.assertEqual(self.child.encrypted_text, '')
        self.assertTrue(self.child.metadata['permanent_failure'])
        self.assertNotIn('qualified_epoch', self.child.metadata[parent_recovery.CONTRACT_KEY])
        self.assertNotIn('fresh_source_text_sha256', self.child.metadata[parent_recovery.CONTRACT_KEY])

    def test_qualified_parent_absence_flattens_without_using_retired_destination(self):
        self.schedule()
        self.observe(parent=False)
        self.qualify()
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'pending')
        self.assertEqual(self.child.metadata['thread_ts'], '')
        self.assertEqual(self.child.metadata['original_thread_ts'], self.parent.source_message_id)
        self.assertTrue(self.child.metadata['thread_parent_unavailable'])

    def test_staged_reply_aging_out_is_erased_before_release(self):
        self.schedule()
        self.observe()
        self.child.refresh_from_db()
        self.child.source_message_id=f'{int(timezone.now().timestamp())-31*86400}.000001'
        self.child.save()
        self.qualify()
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'completed')
        self.assertEqual(self.child.encrypted_text, '')
        self.assertTrue(self.child.metadata['history_outside_window'])

    def test_finishing_pre_version_archive_cannot_release_a_staged_reply(self):
        self.schedule()
        self.observe('started-before-fix')
        with transaction.atomic():
            dm._finish_history_scan(self.conversation)
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'dead')
        self.assertEqual(self.child.encrypted_text, '')
        self.assertTrue(self.child.metadata['permanent_failure'])
