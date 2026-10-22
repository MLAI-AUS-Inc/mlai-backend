"""Capability and current audience acknowledgement checks without a database."""
from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import uuid
from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from integrations.services import slack_chat_catalog as catalog

KEY, OTHER = '11' * 32, '22' * 32
GENERATION, ROOM = str(uuid.UUID(int=1)), str(uuid.UUID(int=2))
HASH = 'ab' * 32


def bridge_row(**changes):
    identity = dict(workspace_id='TTESTMEMBERS', channel_id='CTESTMEMBERS', destination_channel_id=ROOM)
    return SimpleNamespace(**(dict(audience='members', destination_platform='buzz', enabled=True,
        slack_workspace_id=identity['workspace_id'], slack_channel_id=identity['channel_id'],
        destination_channel_id=ROOM, audience_generation=GENERATION, audience_hash=HASH,
        audience_sync_pending=False, room_sync_pending=False, slack_visibility='public',
        slack_seen_at=datetime.now(timezone.utc),
        slack_archived=False, slack_deleted=False, mirror_decision='mirror', hold_reason='',
        room_state={'applied': {'audience': 'members'},
                    'audience_applied': {'generation': GENERATION, 'hash': HASH, **identity}}) | changes))


class MemberCatalogProofTests(SimpleTestCase):
    def test_capability_requires_explicit_bounded_well_formed_value(self):
        self.assertIn('public_channel', catalog.supported_catalog_kinds('im, mpim,private_channel,public_channel'))
        for value in (None, '', True, {}, 'public_channel,', 'unknown,public_channel', 'x' * 257, ['public_channel', {}]):
            self.assertNotIn('public_channel', catalog.supported_catalog_kinds(value))

    def test_exact_acknowledged_identity_and_current_hash_are_required(self):
        self.assertEqual(catalog._member_room_acknowledged(bridge_row(), HASH), ROOM)
        for field, value in [('generation', str(uuid.UUID(int=3))), ('hash', 'cd' * 32),
                             ('workspace_id', 'TOTHER'), ('channel_id', 'COTHER'),
                             ('destination_channel_id', str(uuid.UUID(int=4)))]:
            row = bridge_row()
            row.room_state['audience_applied'][field] = value
            with self.subTest(field=field):
                self.assertIsNone(catalog._member_room_acknowledged(row, HASH))
        self.assertIsNone(catalog._member_room_acknowledged(bridge_row(), 'cd' * 32))

    def test_pending_malformed_or_unapplied_audiences_never_look_ready(self):
        for changes in [dict(audience_sync_pending=True), dict(room_sync_pending=True),
                        dict(audience_generation=None), dict(audience_generation=str(uuid.UUID(int=0))),
                        dict(audience_hash=''), dict(destination_channel_id='untrusted'),
                        dict(room_state=[]), dict(room_state={'applied': {'audience': 'community'}}),
                        dict(room_state={'applied': {'audience': 'members'}, 'audience_applied': []})]:
            with self.subTest(changes=changes):
                self.assertIsNone(catalog._member_room_acknowledged(bridge_row(**changes), HASH))

    def test_capability_absence_does_not_even_query_new_catalog_rows(self):
        with patch('integrations.models.CommunityBridgeChannel.objects.filter') as query:
            self.assertEqual(catalog.member_channel_catalog_payload(SimpleNamespace(), KEY), [])
        query.assert_not_called()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.grant = SimpleNamespace(pk=4, user_id=8, slack_workspace_id='TTESTMEMBERS')
        self.enabled = self.stack.enter_context(patch('integrations.services.slack_member_access.enabled', return_value=True))
        self.scope = self.stack.enter_context(patch('integrations.services.community_bridge.member_channels.member_channel_in_scope', return_value=True))
        self.eligible = self.stack.enter_context(patch.object(catalog, 'public_channels_enabled', return_value=True))
        self.devices = self.stack.enter_context(patch('integrations.services.slack_dm_mirror._owner_device_pubkeys', return_value=[KEY]))
        self.members = self.stack.enter_context(patch('integrations.services.slack_channel_membership.consenting_members', return_value=[self.grant]))
        self.keys = self.stack.enter_context(patch('integrations.services.community_bridge.member_audiences.current_audience_keys', return_value={KEY}))
        self.hash = self.stack.enter_context(patch('integrations.services.community_bridge.member_audiences.audience_hash', return_value=HASH))

    def room(self, row=None, **kwargs):
        return catalog.member_channel_room_id(self.grant, KEY, row or bridge_row(),
                                             catalog_kinds=kwargs.get('catalog_kinds', 'public_channel'))

    def test_current_exact_grant_owner_device_and_complete_ack_can_open(self):
        self.assertEqual(self.room(), ROOM)
        self.devices.assert_called_once_with(self.grant.user_id, limit=8)
        self.hash.assert_called_once_with({KEY})

    def test_ninth_verified_device_cannot_use_older_audience_receipt(self):
        self.devices.return_value = [OTHER]
        self.assertIsNone(self.room())
        self.keys.assert_not_called()

    def test_current_membership_and_full_current_hash_are_both_required(self):
        self.members.return_value = [SimpleNamespace(pk=44, user_id=8)]
        self.assertIsNone(self.room())
        self.members.return_value = [self.grant]
        self.keys.return_value = {OTHER}
        self.assertIsNone(self.room())
        self.keys.return_value = None
        self.assertIsNone(self.room())
        self.keys.return_value = {KEY}
        self.hash.return_value = 'cd' * 32
        self.assertIsNone(self.room())

    def test_flags_scope_existing_consent_and_capability_all_gate_catalog(self):
        for boundary in (self.enabled, self.scope, self.eligible):
            boundary.return_value = False
            self.assertIsNone(self.room())
            boundary.return_value = True
        self.assertIsNone(self.room(catalog_kinds='im,mpim,private_channel'))

    def test_private_connect_archived_or_community_rows_cannot_be_members_catalog(self):
        for changes in [dict(audience='community'), dict(slack_visibility='private'), dict(enabled=False),
                        dict(slack_archived=True), dict(slack_deleted=True), dict(hold_reason='slack_connect'),
                        dict(mirror_decision='hold'), dict(slack_workspace_id='TOTHER')]:
            with self.subTest(changes=changes):
                self.assertIsNone(self.room(bridge_row(**changes)))

    def test_stale_missing_or_malformed_source_observation_cannot_advertise_ready(self):
        for value in (None, 'yesterday', datetime.now(timezone.utc) - timedelta(seconds=301),
                      datetime.now(timezone.utc) + timedelta(seconds=1), datetime.now()):
            with self.subTest(value=value):
                self.assertIsNone(self.room(bridge_row(slack_seen_at=value)))

    def test_catalog_uses_new_kind_only_for_ready_members_rooms(self):
        query = self.stack.enter_context(patch('integrations.models.CommunityBridgeChannel.objects.filter'))
        query.return_value.order_by.return_value = [bridge_row(), bridge_row(audience_sync_pending=True)]
        items = catalog.member_channel_catalog_payload(self.grant, KEY, catalog_kinds='public_channel')
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]['kind'], items[0]['channel_id'], items[0]['slack_conversation_id']),
                         ('public_channel', ROOM, 'CTESTMEMBERS'))
        self.assertIs(items[0]['ready_for_display'], True)
