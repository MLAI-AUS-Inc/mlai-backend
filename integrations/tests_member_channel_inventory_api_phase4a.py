"""Public room open/mapping boundaries; synthetic rows and no provider I/O."""
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from datetime import datetime, timezone

from django.test import SimpleTestCase

from integrations.services import slack_owner_inventory_api as api

KEY, SOURCE, ROOM = '11' * 32, 'CTESTMEMBERS', '00000000-0000-0000-0000-000000000002'


class MemberPublicOpenTests(SimpleTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.source = SimpleNamespace(kind='public_channel', eligibility='eligible')
        self.grant = SimpleNamespace(pk=4, user_id=8, slack_workspace_id='TTESTMEMBERS', owner_conversation_inventory=Mock())
        self.grant.owner_conversation_inventory.filter.return_value.first.return_value = self.source
        self.device = SimpleNamespace(pk=9, public_key=KEY, verified_at=None)
        self.authority = SimpleNamespace(workspace_id='TTESTMEMBERS')
        self.stack.enter_context(patch.object(api, '_authorized', return_value=(self.grant, self.authority, self.device, {})))
        self.stack.enter_context(patch.object(api.transaction, 'atomic', side_effect=nullcontext))
        self.stack.enter_context(patch.object(api, '_lock_slack_grant_api_authority', return_value=(self.grant, SimpleNamespace())))
        self.stack.enter_context(patch.object(api.CommunityChatDevice.objects, 'filter')).return_value.exists.return_value = True
        self.stack.enter_context(patch.object(api, 'has_metadata_consent', return_value=True))
        self.bridge = self.stack.enter_context(patch.object(api.CommunityBridgeChannel.objects, 'filter'))
        self.bridge.return_value.first.return_value = SimpleNamespace(audience='members', destination_channel_id=ROOM)
        self.allowed = self.stack.enter_context(patch.object(api, '_member_public_open_allowed', return_value=True))
        self.ready = self.stack.enter_context(patch.object(api, 'member_channel_room_id', return_value=ROOM))
        self.queue = self.stack.enter_context(patch('integrations.services.community_bridge.member_channels.queue_refresh'))

    def open(self, **changes):
        return api.request_open(SimpleNamespace(pk=8), public_key=KEY, slack_conversation_id=SOURCE, **changes)

    def test_ready_member_room_requires_current_capability_and_authority(self):
        self.assertEqual(self.open(catalog_kinds='public_channel'), (200, {'state': 'ready', 'mlai_channel_id': ROOM}))
        self.allowed.assert_called_once_with(self.grant, KEY, SOURCE, catalog_kinds='public_channel')
        self.queue.assert_not_called()

    def test_current_member_can_queue_provisioning_without_room_id(self):
        for row in (None, SimpleNamespace(audience='members', destination_channel_id=ROOM)):
            self.bridge.return_value.first.return_value = row
            self.ready.return_value = None
            status, result = self.open(catalog_kinds='public_channel')
            self.assertEqual(status, 202)
            self.assertEqual(result, {'state': 'provisioning', 'stage': 'provisioning'})
            self.assertNotIn('mlai_channel_id', result)
            self.queue.assert_called_with('TTESTMEMBERS', SOURCE, reason='owner_open_priority')

    def test_ineligible_member_or_missing_capability_cannot_queue_or_get_id(self):
        self.allowed.return_value = False
        with self.assertRaises(api.InventoryError) as result:
            self.open()
        self.assertEqual(result.exception.code, 'public_mapping_required')
        self.ready.assert_not_called()
        self.queue.assert_not_called()

    def test_existing_community_mapping_keeps_legacy_no_header_open(self):
        self.bridge.return_value.first.return_value = SimpleNamespace(audience='community', destination_channel_id=ROOM)
        self.allowed.return_value = False
        self.assertEqual(self.open(), (200, {'state': 'ready', 'mlai_channel_id': ROOM}))
        self.allowed.assert_not_called()
        self.ready.assert_not_called()
        self.queue.assert_not_called()

    def test_ineligible_inventory_row_cannot_discover_members_room(self):
        self.source.eligibility = 'external_shared'
        with self.assertRaises(api.InventoryError) as result:
            self.open(catalog_kinds='public_channel')
        self.assertEqual(result.exception.code, 'external_shared')
        self.bridge.assert_not_called()
        self.queue.assert_not_called()


class MemberPublicOpenAuthorityTests(SimpleTestCase):
    def test_capability_flags_scope_consent_exact_grant_device_and_capacity_are_required(self):
        grant = SimpleNamespace(pk=4, user_id=8, slack_workspace_id='TTESTMEMBERS')
        with ExitStack() as stack:
            flag = stack.enter_context(patch('integrations.services.slack_member_access.enabled', return_value=True))
            scope = stack.enter_context(patch('integrations.services.community_bridge.member_channels.member_channel_in_scope', return_value=True))
            consent = stack.enter_context(patch.object(api, 'public_channels_enabled', return_value=True))
            devices = stack.enter_context(patch('integrations.services.slack_member_devices.member_device_key_allowed', return_value=True))
            members = stack.enter_context(patch('integrations.services.slack_channel_membership.consenting_members', return_value=[grant]))
            keys = stack.enter_context(patch('integrations.services.community_bridge.member_audiences.current_audience_keys', return_value={KEY}))
            def allowed(kinds='public_channel'):
                return api._member_public_open_allowed(grant, KEY, SOURCE, catalog_kinds=kinds)
            self.assertTrue(allowed())
            self.assertFalse(allowed('im,mpim,private_channel'))
            for gate in (flag, scope, consent):
                gate.return_value = False
                self.assertFalse(allowed())
                gate.return_value = True
            devices.return_value = False
            self.assertFalse(allowed())
            devices.return_value = True
            members.return_value = [SimpleNamespace(pk=44, user_id=8)]
            self.assertFalse(allowed())
            devices.assert_called_with(grant.user_id, KEY)
            members.return_value = [grant]
            keys.return_value = None
            self.assertFalse(allowed())


class MemberCatalogHeaderTests(SimpleTestCase):
    def test_status_inventory_and_open_forward_exact_capability_header(self):
        from community_chat import slack_views

        request = SimpleNamespace(user=SimpleNamespace(pk=8), community_chat_public_key=KEY,
            query_params={}, data={'slack_conversation_id': SOURCE},
            headers={'X-MLAI-Slack-Catalog-Kinds': 'im,mpim,private_channel,public_channel'})
        with patch.object(slack_views, 'status_payload', return_value={}) as status:
            self.assertEqual(slack_views.SlackDmMirrorView().get(request).status_code, 200)
        self.assertEqual(status.call_args.kwargs['catalog_kinds'], request.headers['X-MLAI-Slack-Catalog-Kinds'])
        with patch.object(api, 'conversation_page', return_value={}) as page:
            self.assertEqual(slack_views.SlackOwnerConversationView().get(request).status_code, 200)
        self.assertEqual(page.call_args.kwargs['catalog_kinds'], request.headers['X-MLAI-Slack-Catalog-Kinds'])
        with patch.object(api, 'request_open', return_value=(202, {'stage': 'provisioning'})) as opened:
            self.assertEqual(slack_views.SlackOwnerConversationOpenView().post(request).status_code, 202)
        self.assertEqual(opened.call_args.kwargs['catalog_kinds'], request.headers['X-MLAI-Slack-Catalog-Kinds'])


class MemberPublicInventoryPageTests(SimpleTestCase):
    def test_page_does_not_leak_members_mapping_to_legacy_or_unready_clients(self):
        rows = [SimpleNamespace(pk=index, slack_conversation_id=source, kind='public_channel',
            eligibility='eligible', source_activity_ts='1900000000.000001', source_name=source,
            display_name='', counterpart_slack_user_id='', source_archived=False, source_is_open=True,
            last_seen_at=datetime.now(timezone.utc)) for index, source in enumerate((SOURCE, 'CCOMMUNITY'), 1)]
        connection = SimpleNamespace(sync_cursor={})
        grant = SimpleNamespace(pk=4, user_id=8, slack_workspace_id='TTESTMEMBERS', slack_user_id='UOWNER',
            connection=connection, owner_conversation_inventory=MagicMock(), conversations=MagicMock())
        grant.owner_conversation_inventory.order_by.return_value = rows
        state = {'revision': 1}
        bridges = [SimpleNamespace(audience=audience, slack_channel_id=source, destination_channel_id=ROOM)
                   for source, audience in [(SOURCE, 'members'), ('CCOMMUNITY', 'community')]]
        with ExitStack() as stack:
            for name, options in [
                ('_authorized', {'return_value': (grant, SimpleNamespace(workspace_id='TTESTMEMBERS'), SimpleNamespace(pk=9, public_key=KEY), state)}),
                ('device_epoch', {'return_value': 'epoch'}), ('cache.get_many', {'return_value': {}}),
                ('_cache_key', {'side_effect': lambda authority, target: target.slack_id}),
                ('time.time', {'return_value': 1900000060}), ('catalog_conversations', {'return_value': []}),
                ('CommunityBridgeChannel.objects.filter', {'return_value': bridges}),
                ('cached_workspace_users', {'return_value': []}), ('_grant_history_days', {'return_value': 30}),
                ('source_read_targets', {'return_value': []}), ('transaction.atomic', {'side_effect': nullcontext}),
                ('_lock_slack_grant_api_authority', {'return_value': (grant, connection)}),
                ('CommunityChatDevice.objects', {}), ('has_metadata_consent', {'return_value': True}),
                ('state_for', {'return_value': state}), ('_member_public_open_allowed', {'return_value': False}),
            ]:
                stack.enter_context(patch(f'{api.__name__}.{name}', **options))
            readiness = stack.enter_context(patch.object(api, 'member_channel_room_id', side_effect=lambda grant, key, bridge, catalog_kinds: ROOM if catalog_kinds == 'public_channel' else None))
            legacy = api.conversation_page(object(), public_key=KEY)
            self.assertEqual([(row['state'], row['mlai_channel_id']) for row in legacy['items']],
                             [('unmapped', None), ('mapped', ROOM)])
            capable = api.conversation_page(object(), public_key=KEY, catalog_kinds='public_channel')
            self.assertEqual(capable['items'][0]['mlai_channel_id'], ROOM)
            self.assertEqual(capable['items'][0]['device_access'], 'member')
            readiness.return_value, readiness.side_effect = None, None
            pending = api.conversation_page(object(), public_key=KEY, catalog_kinds='public_channel')
            self.assertEqual((pending['items'][0]['state'], pending['items'][0]['mlai_channel_id']), ('unmapped', None))
