"""Custom Slack images preserve identity, aliases and existing code formatting."""
import unittest
from integrations.services.slack_custom_emoji import resolve_catalog, present, safe_url, needs_catalog
from integrations.services.slack_emoji import custom_shortcode, emoji_to_slack_reaction, slack_reaction_to_emoji

URL = 'https://emoji.slack-edge.com/TTEST/parrot/abc.gif'

class SlackCustomEmojiTests(unittest.TestCase):
    def test_reconnect_requests_emoji_read_even_with_legacy_environment_scopes(self):
        from django.test import override_settings
        from integrations.services.external_connectors import _slack_oauth_user_scope_list
        with override_settings(SLACK_OAUTH_USER_SCOPES=['channels:read']):
            scopes = _slack_oauth_user_scope_list()
        self.assertIn('emoji:read', scopes)
        self.assertIn('channels:read', scopes)
        self.assertIn('reactions:write', scopes)
        self.assertEqual(len(scopes),len(set(scopes)))

    def test_plain_text_and_standard_emoji_do_not_depend_on_catalog(self):
        for text in ('hello', '👍', ':thumbsup::skin-tone-4:', '`x :custom:`', 'https://example.com/:custom:'):
            self.assertFalse(needs_catalog(text))
        self.assertTrue(needs_catalog(':party_parrot:'))

    def test_images_aliases_unicode_and_skin_tones(self):
        catalog = resolve_catalog({'party_parrot': URL, 'celebrate': 'alias:party_parrot',
                                   'success': 'alias:white_check_mark'})
        text, images = present(':party_parrot: :celebrate: :success: :thumbsup::skin-tone-4:', catalog)
        self.assertEqual(text, ':party_parrot: :celebrate: ✅ 👍🏽')
        self.assertEqual(images, [{'shortcode':'party_parrot','url':URL}, {'shortcode':'celebrate','url':URL}])
        self.assertTrue(images[0]['url'].endswith('.gif'))

    def test_cycles_broken_aliases_and_unsafe_images_do_not_escape(self):
        catalog = resolve_catalog({'one':'alias:two','two':'alias:one','missing':'alias:nope',
                                   'evil':'https://example.com/image.png','good':URL})
        self.assertEqual(set(catalog), {'good'})
        for value in ('http://emoji.slack-edge.com/x', 'https://emoji.slack-edge.com@evil.test/x',
                      'https://emoji.slack-edge.com/x?token=secret', 'https://emoji.slack-edge.com:443/x',
                      'https://emoji.slack-edge.com.evil.test/x', None):
            self.assertEqual(safe_url(value), '')
        self.assertEqual(resolve_catalog(['unexpected']), {})

    def test_inline_and_fenced_code_stay_literal_and_unused_images_are_absent(self):
        catalog = resolve_catalog({'party_parrot':URL, 'unused':URL})
        source = '`x :party_parrot:`\n```\n:thumbsup: :party_parrot:\n```\n:party_parrot: :party_parrot: :unknown:'
        rendered, images = present(source, catalog)
        self.assertEqual(rendered, source)
        self.assertEqual(images,[{'shortcode':'party_parrot','url':URL}])

    def test_shortcodes_inside_links_remain_literal(self):
        source = 'https://example.com/:smile: <https://example.com/:party_parrot:|link> :smile:'
        rendered, images = present(source, resolve_catalog({'party_parrot': URL}))
        self.assertEqual(rendered, source[:-7] + '😄')
        self.assertEqual(images, [])

    def test_long_and_plus_names_round_trip_with_the_workspace_catalog(self):
        for name in ('x'*100, 'a+b', '_party'):
            catalog = resolve_catalog({name:URL})
            rendered, images = present(':'+name+':',catalog)
            self.assertEqual(rendered,slack_reaction_to_emoji(name))
            self.assertLessEqual(len(rendered),64)
            self.assertEqual(images[0]['shortcode'],custom_shortcode(name))
            self.assertEqual(emoji_to_slack_reaction(rendered,catalog=catalog),name)

    def test_hashed_reaction_can_attach_image_after_ingestion(self):
        name='x'*100
        catalog=resolve_catalog({name:URL})
        text, images=present(slack_reaction_to_emoji(name),catalog)
        self.assertEqual(images[0]['shortcode'],text[1:-1])

    def test_only_distinct_used_images_are_bounded(self):
        catalog=resolve_catalog({f'e{i}':URL for i in range(102)})
        text,images=present(' '.join(f':e{i}:' for i in range(102)),catalog)
        self.assertEqual(len(images),100)
        self.assertIn(':e101:',text)

from django.test import TransactionTestCase, override_settings
from unittest.mock import patch
from django.core.cache import cache
from integrations.tests_message_sync_member_history_phase4b import Fixture, SETTINGS
from integrations.services import slack_custom_emoji as custom
from integrations.services import slack_directory_authority as directory
from integrations.services import slack_dm_mirror as dm

@override_settings(**SETTINGS)
class SlackEmojiAuthorityTests(Fixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        for grant in (self.a, self.b):
            grant.connection.scopes += ['users:read','emoji:read']
            grant.connection.save()

    def transport(self, selected, connection, method, **kwargs):
        self.calls.append((selected.grant_id,method))
        self.assertEqual(method,'emoji_list')
        return {'ok':True,'emoji':{'party_parrot':URL}}

    def test_member_catalog_is_cached_and_does_not_use_bot(self):
        self.assertEqual(custom.load_catalog('TTEST')['party_parrot']['url'],URL)
        self.assertEqual(custom.load_catalog('TTEST')['party_parrot']['url'],URL)
        self.assertEqual(self.calls,[(self.a.pk,'emoji_list')])
        self.bot.assert_not_called()

    def test_missing_scope_or_revoked_grant_cannot_read_cached_catalog(self):
        custom.load_catalog('TTEST')
        self.calls.clear()
        for grant in (self.a,self.b):
            grant.connection.scopes.remove('emoji:read')
            grant.connection.save()
        self.assertEqual(custom.load_catalog('TTEST'),{})
        self.assertEqual(self.calls,[])

    def test_revocation_between_selection_and_fence_blocks_provider(self):
        selected=directory.capture_reader('TTEST',required_scopes={'emoji:read'})
        type(self.a).objects.filter(pk=self.a.pk).update(status='paused')
        with patch.object(directory,'capture_reader',return_value=selected):
            with self.assertRaises(dm.SlackDmMirrorAuthorizationError):
                custom.load_catalog('TTEST')
        self.assertEqual(self.calls,[])

    def test_failed_fetch_is_not_cached_as_an_empty_success(self):
        with patch.object(dm,'_invoke_slack_with_locked_connection',return_value={'ok':False}):
            with self.assertRaises(dm.SlackDmMirrorError): custom.load_catalog('TTEST')
        self.assertIn('party_parrot',custom.load_catalog('TTEST'))

    def test_new_name_refreshes_cached_catalog_and_bounds_unknown_refreshes(self):
        custom.load_catalog('TTEST')
        with patch.object(dm, '_invoke_slack_with_locked_connection', return_value={'ok': True, 'emoji': {'party_parrot': URL, 'new_emoji': URL}}) as fetch:
            result = custom.load_catalog('TTEST', requested_names=['new_emoji'])
            self.assertEqual(result['new_emoji']['url'], URL)
            self.assertEqual(fetch.call_count, 1)
            custom.load_catalog('TTEST', requested_names=['new_emoji'])
            self.assertEqual(fetch.call_count, 1)
            custom.load_catalog('TTEST', requested_names=['absent'])
            custom.load_catalog('TTEST', requested_names=['absent'])
            self.assertEqual(fetch.call_count, 2)

    def emoji_request(self, *, names='party_parrot', workspace='TTEST', authenticated=True):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from community_chat.slack_emoji_views import SlackEmojiDirectoryView
        request = APIRequestFactory().get('/community-chat/slack/emoji/', {'workspace_id': workspace, 'names': names})
        request.community_chat_public_key = '1' * 64
        if authenticated:
            force_authenticate(request, user=self.a.user)
        with patch('community_chat.slack_emoji_views.require_community_access'):
            return SlackEmojiDirectoryView.as_view(throttle_classes=())(request)

    def test_directory_returns_only_requested_images_and_unicode_and_no_usage(self):
        response = self.emoji_request(names='party_parrot,head_shaking_vertically')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'workspace_id':'TTEST', 'emoji': {
            'party_parrot': {'shortcode':'party_parrot','url':URL},
            'head_shaking_vertically': {'unicode':'🙂\u200d↕️'},
        }})
        self.assertEqual(response['Cache-Control'], 'private, no-store')

    def test_directory_rejects_anonymous_wrong_workspace_and_revoked_device(self):
        from community_chat.models import CommunityChatDevice
        self.assertIn(self.emoji_request(authenticated=False).status_code, [401, 403])
        self.assertEqual(self.emoji_request(workspace='TOTHER').status_code, 400)
        self.assertEqual(self.emoji_request(names=','.join(['name']*101)).status_code, 400)
        CommunityChatDevice.objects.filter(user=self.a.user).update(status='revoked')
        self.assertEqual(self.emoji_request().status_code, 403)
        self.assertEqual(self.calls, [])

    def test_directory_rechecks_requesting_device_after_catalog_io(self):
        from community_chat.models import CommunityChatDevice
        def revoke(*args, **kwargs):
            CommunityChatDevice.objects.filter(user=self.a.user).update(status='revoked')
            return {'party_parrot': {'shortcode': 'party_parrot', 'url': URL}}
        with patch.object(custom, 'load_catalog', side_effect=revoke):
            self.assertEqual(self.emoji_request().status_code, 403)
