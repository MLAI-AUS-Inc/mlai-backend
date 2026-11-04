"""Disposable-database coverage for fingerprinted public dependency cleanup."""
from datetime import timedelta
from io import StringIO
import json

from django.core.management import call_command, CommandError
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from integrations.models import CommunityBridgeChannel, CommunityBridgeDelivery
from integrations.management.commands.resolve_community_bridge_dependencies import dependency_id


@override_settings(COMMUNITY_BRIDGE_DEPENDENCY_VERDICT_MIN_AGE_SECONDS=3600)
class DependencyCleanupCommandTests(TransactionTestCase):
    def setUp(self):
        self.channel = CommunityBridgeChannel.objects.create(slack_channel_id='CTEST', destination_platform='buzz', destination_channel_id='room')
        self.dead = self.row(source_message_id='reaction:' + 'a'*64, status='dead', delivery_type='reaction_add')
        self.remove = self.row(source_message_id=self.dead.source_message_id, status='waiting_parent', delivery_type='reaction_remove',
            last_error='parent_mapping_pending:' + self.dead.source_message_id, dependency_first_seen_at=timezone.now() - timedelta(hours=2))

    def row(self, **changes):
        fields = dict(channel=self.channel, source_platform='slack', target_platform='buzz', source_channel_id='CTEST', source_message_id='123.4',
            delivery_type='create', source_event_key=str(timezone.now().timestamp()), payload={'text': 'private fixture text'}, available_at=timezone.now())
        fields.update(changes)
        return CommunityBridgeDelivery.objects.create(**fields)

    def test_dry_run_and_fingerprint_gate(self):
        before = self.remove.updated_at
        out = StringIO()
        call_command('resolve_community_bridge_dependencies', stdout=out)
        self.remove.refresh_from_db()
        self.assertEqual(self.remove.updated_at, before)
        self.assertNotIn('private fixture text', out.getvalue())
        self.assertEqual(dependency_id(self.remove), 'reaction:' + 'a'*64)
        summary = json.loads(out.getvalue().splitlines()[-1])
        self.assertEqual(summary['by_action'], {'noop': 1})
        for plan in (None, '0'*64):
            with self.assertRaises(CommandError):
                call_command('resolve_community_bridge_dependencies', apply=True, expected_plan=plan, stdout=StringIO())
        call_command('resolve_community_bridge_dependencies', apply=True, expected_plan=summary['plan'], stdout=StringIO())
        self.remove.refresh_from_db()
        self.assertEqual(self.remove.status, 'completed')

    def test_old_parent_can_be_flattened_only_with_explicit_confirmation(self):
        row = self.row(status='waiting_parent', source_parent_message_id='123.5', dependency_first_seen_at=timezone.now() - timedelta(days=40))
        with self.assertRaises(CommandError):
            call_command('resolve_community_bridge_dependencies', flatten_replies=True, stdout=StringIO())
        out = StringIO()
        call_command('resolve_community_bridge_dependencies', flatten_replies=True, confirm_flatten=True, stdout=out)
        plan = json.loads(out.getvalue().splitlines()[-1])['plan']
        call_command('resolve_community_bridge_dependencies', flatten_replies=True, confirm_flatten=True, apply=True, expected_plan=plan, stdout=StringIO())
        row.refresh_from_db()
        self.assertEqual((row.status, row.source_parent_message_id), ('pending', ''))
        self.assertTrue(row.payload['metadata']['thread_parent_unavailable'])

    def test_classifier_waits_for_evidence_age_and_opposite_direction_work(self):
        from integrations.services.community_bridge.dependencies import classify_missing_dependency
        from integrations.models import CommunityBridgeReceipt
        now = timezone.now()
        first = now - timedelta(hours=2)
        parent = f'{int(now.timestamp()) - 7200}.000001'
        reply = self.row(source_message_id='reply', source_parent_message_id=parent, status='waiting_parent')
        receipt = CommunityBridgeReceipt.objects.create(channel=self.channel, platform='slack', receipt_key='ignored',
            source_channel_id='CTEST', source_message_id=parent, status='ignored', event_type='channel_join',
            error_text='unsupported_or_ignored_event:subtype:channel_join')
        def verdict(seen=first):
            return classify_missing_dependency(reply, parent, first_seen=seen, now=now)
        self.assertEqual(verdict().code, 'dependency_ignored_channel_join')
        self.assertEqual(verdict(now - timedelta(minutes=5)).code, 'too_recent')
        receipt.error_text = 'unsupported_or_ignored_event:bridge_echo'
        receipt.save()
        opposite = self.row(source_platform='buzz', target_platform='slack', source_message_id='b'*64, status='pending')
        self.assertEqual(verdict().code, 'dependency_in_flight')
        opposite.delete()
        self.assertEqual(verdict().action, 'wait')
        receipt.source_message_id = ''
        receipt.payload = {'event': {'type': 'message', 'subtype': 'channel_leave', 'ts': parent}}
        receipt.save()
        self.assertEqual((verdict().code, verdict().evidence), ('dependency_ignored_channel_leave', 'payload'))
        reply.target_platform = 'discord'
        self.assertEqual(verdict().code, 'platform_not_covered')

    def test_existing_mapping_is_kept_even_after_a_historical_missed_wake(self):
        from integrations.models import CommunityBridgeMessageLink
        CommunityBridgeMessageLink.objects.create(channel=self.channel, source_platform='slack', source_channel_id='CTEST',
            source_message_id=self.dead.source_message_id, destination_platform='buzz', destination_channel_id='room', destination_message_id='a'*64)
        out = StringIO()
        call_command('resolve_community_bridge_dependencies', stdout=out)
        row = json.loads(out.getvalue().splitlines()[0])
        self.assertEqual((row['action'], row['code']), ('keep', 'dependency_already_mapped'))
