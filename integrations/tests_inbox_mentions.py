"""Database-free verification of trusted mention identity and delivery stability."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
from django.test import SimpleTestCase, override_settings
from integrations.services import slack_mentions as mentions
from integrations.services.community_bridge.buzz import BuzzBridgeClient, BuzzBridgePermanentError


@override_settings(MESSAGE_SYNC_INBOX_MENTIONS=True)
class InboxMentionTests(SimpleTestCase):
    @override_settings(MESSAGE_SYNC_INBOX_MENTIONS=False)
    def test_disabled_resolution_never_accesses_database(self):
        self.assertEqual(mentions.delivery_mention_pubkeys('T123', '<@U123>'), [])
        self.assertEqual(mentions.queue_delivery_mentions(object(), '<@U123>', {'legacy': 1}), {'legacy': 1})

    def test_only_explicit_linked_active_account_mentions_are_emitted(self):
        links = [SimpleNamespace(slack_user_id=value, user_id=index) for index, value in enumerate(('U123', 'W123', 'U456', 'U789'),1)]
        identities = {'U123': {'identity_source': 'mlai_account', 'buzz_pubkey': 'a'*64},
                      'W123': {'identity_source': 'mlai_account', 'buzz_pubkey': 'a'*64},
                      'U456': {'identity_source': 'legacy_key', 'buzz_pubkey': 'b'*64},
                      'U789': {'identity_source': 'mlai_account', 'buzz_pubkey': ''}}
        with patch.object(mentions.CommunityBridgeIdentityLink.objects, 'filter', return_value=links) as query, patch(
            'integrations.services.community_bridge.identity.verified_identity_for_slack', side_effect=lambda **kwargs: identities.get(kwargs['slack_user_id'])):
            result=mentions.delivery_mention_pubkeys('T123', '<@U123> <@W123|label> <@U456> <@U789> <@U000> @unlinked <!here> &lt;@U111&gt;')
        self.assertEqual(result,['a'*64])
        self.assertEqual(query.call_args.kwargs['slack_workspace_id'],'T123')
        self.assertTrue(query.call_args.kwargs['user__is_active'])
        self.assertTrue(query.call_args.kwargs['revoked_at__isnull'])

    def test_private_mentions_choose_an_active_device_inside_the_audience(self):
        from community_chat.models import CommunityChatDevice
        link=SimpleNamespace(slack_user_id='U123',user_id=1)
        devices=Mock();devices.order_by.return_value.values_list.return_value.first.return_value='b'*64
        with patch.object(mentions.CommunityBridgeIdentityLink.objects,'filter',return_value=[link]), patch(
            'integrations.services.community_bridge.identity.verified_identity_for_slack',return_value={'identity_source':'mlai_account','buzz_pubkey':'a'*64}), patch.object(
            CommunityChatDevice.objects,'filter',return_value=devices) as query:
            self.assertEqual(mentions.delivery_mention_pubkeys('T123','<@U123>',participant_pubkeys=['b'*64]),['b'*64])
        self.assertEqual(query.call_args.kwargs['public_key__in'], {'b'*64})
        self.assertEqual(query.call_args.kwargs['status'],'verified')
        self.assertTrue(query.call_args.kwargs['revoked_at__isnull'])

    def test_frozen_mentions_and_legacy_empty_envelopes_survive_identity_changes(self):
        field=mentions.DELIVERY_MENTIONS_KEY
        updated={'participant_hash':'audience',field:['b'*64]}
        self.assertEqual(mentions.preserve_delivery_mentions({'participant_hash':'audience',field:['a'*64]},updated)[field],['a'*64])
        self.assertEqual(mentions.preserve_delivery_mentions({'participant_hash':'audience'},updated)[field],[])
        self.assertEqual(mentions.preserve_delivery_mentions({'participant_hash':'old-audience'},updated),updated)
        conversation=SimpleNamespace(slack_workspace_id='T123',participant_buzz_pubkeys=['a'*64])
        with patch.object(mentions,'delivery_mention_pubkeys',return_value=['a'*64]):
            self.assertEqual(mentions.queue_delivery_mentions(conversation,'<@U123>',{'participant_hash':'audience'})[field],['a'*64])
            queued = mentions.queue_delivery_mentions(conversation,'<@U123>',{'participant_hash':'audience', 'broadcast':True})
            self.assertTrue(queued[mentions.DELIVERY_BROADCAST_KEY])
            self.assertFalse(mentions.preserve_delivery_mentions({'participant_hash':'audience'},queued)[mentions.DELIVERY_BROADCAST_KEY])

    def test_private_contract_omits_empty_mentions_for_older_adapters(self):
        kwargs={'delivery_id':'synthetic','created_at':100,'operation':'create','channel_id':'00000000-0000-0000-0000-000000000001',
            'participant_pubkeys':['a'*64,'b'*64], 'text':'body','source_workspace_id':'T123','source_channel_id':'D123',
            'source_message_id':'100.000001','source_author_id':'U123','source_author_display_name':'Member','source_author_avatar_url':'', 'linked_pubkey':'a'*64}
        response={'channel_id':kwargs['channel_id'],'message_id':'c'*64,'parent_message_id':''}
        with patch.object(BuzzBridgeClient,'_post_adapter',return_value=response) as submit:
            BuzzBridgeClient.deliver_private(**kwargs)
            self.assertNotIn('mention_pubkeys', submit.call_args.args[1])
            BuzzBridgeClient.deliver_private(**kwargs,mention_pubkeys=['b'*64,'a'*64,'b'*64])
            self.assertEqual(submit.call_args.args[1]['mention_pubkeys'],['a'*64,'b'*64])
            with self.assertRaises(BuzzBridgePermanentError):
                BuzzBridgeClient.deliver_private(**kwargs,mention_pubkeys=['invalid'])
