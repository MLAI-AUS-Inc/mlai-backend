"""Content and public-link safety fixtures; no database or network is permitted."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from crm.deliverability.preflight import content, links, render
from crm.models import Blast, CrmEvent, SenderMailbox, SendingStream

TEMPLATE = 'MLAI hi@mlai.au {{ UnsubscribeURL }} {{ .Subscriber.Attribs.consent_reason }}'
NOW = datetime(2026, 11, 5, 0, tzinfo=timezone.utc)


def blast(**fields):
    mailbox = SenderMailbox(email='hello@news.mlai.au', display_name='MLAI', domain='news.mlai.au', reply_to='hi@mlai.au')
    return Blast(mailbox=mailbox, stream=SendingStream(key='news', domain='news.mlai.au', mailbox=mailbox),
        **{'subject': 'Your MLAI community news', 'body_markdown': ' '.join(['community'] * 50),
            'preview_text': 'People and ideas from our community.', 'sign_off_name': 'Sam', **fields})


class ContentRulesTests(SimpleTestCase):
    def checks(self, draft=None, **fields):
        args = {'html': '<p>MLAI community</p>', 'template': TEMPLATE, 'observations': [], 'spam': {'score': 0.5}}
        args.update(fields)
        return {row['key']: row for row in content.message_checks(draft or blast(), **args)}

    def test_complete_fixture_passes_the_content_checks(self):
        checks = self.checks()
        self.assertTrue(all(row['status'] == 'pass' for row in checks.values()))

    def test_empty_subject_blocks_and_short_or_long_subjects_warn(self):
        self.assertEqual(self.checks(blast(subject=''))['subject_present']['status'], 'block')
        for subject in ('Hi', 'A' * 71):
            self.assertEqual(self.checks(blast(subject=subject))['subject_length']['status'], 'warn')

    def test_acronyms_are_allowed_but_shouting_and_repeated_symbols_warn(self):
        self.assertEqual(self.checks(blast(subject='MLAI AI ML AU'))['subject_caps']['status'], 'pass')
        self.assertEqual(self.checks(blast(subject='YOUR community news'))['subject_caps']['status'], 'warn')
        for subject in ('Come along!!', 'Come along??', 'Save $$ today', 'Meet us 🙂 🎉'):
            self.assertEqual(self.checks(blast(subject=subject))['subject_punctuation']['status'], 'warn')
        self.assertEqual(self.checks(blast(subject='Community 🇦🇺'))['subject_punctuation']['status'], 'pass')

    def test_reply_tricks_block_and_missing_preview_has_a_safe_fix(self):
        for subject in ('Re: Community news', ' Fwd : Your invitation', 'FW: Hello'):
            self.assertEqual(self.checks(blast(subject=subject))['subject_tricks']['fix']['kind'], 'remove_subject_prefix')
        self.assertEqual(self.checks(blast(preview_text=''))['preview_text']['fix']['kind'], 'add_preview_text')

    def test_short_and_long_messages_warn(self):
        for body in ('Hi there', 'word ' * 1501):
            self.assertEqual(self.checks(blast(body_markdown=body))['body_length']['status'], 'warn')

    def test_html_size_thresholds_are_byte_based_and_missing_render_is_not_a_pass(self):
        self.assertEqual(self.checks(html='a' * (80 * 1024 + 1))['html_size']['status'], 'warn')
        self.assertEqual(self.checks(html='a' * (100 * 1024 + 1))['html_size']['status'], 'block')
        self.assertEqual(self.checks(html='🌏' * (26 * 1024))['html_size']['status'], 'block')
        self.assertEqual(self.checks(html=None)['html_size']['status'], 'block')

    def test_distinct_link_count_shorteners_and_insecure_links(self):
        for count, expected in ((11, 'warn'), (26, 'block')):
            body = '\n'.join(f'[Page](https://example.org/{index})' for index in range(count))
            self.assertEqual(self.checks(blast(body_markdown=body))['links_count']['status'], expected)
        checks = self.checks(blast(body_markdown='[Join](http://bit.ly/abc)'))
        self.assertEqual(checks['link_shorteners']['status'], 'block')
        self.assertEqual(checks['link_insecure']['status'], 'warn')

    def test_private_ip_and_failed_links_do_not_become_successes(self):
        checks = self.checks(blast(body_markdown='[Join](https://127.0.0.1/)'), observations=[
            links.Observation('https://example.org', 'https://example.org', None, 'nonpublic_link')])
        self.assertEqual(checks['link_ip']['status'], 'block')
        self.assertEqual(checks['link_reachable']['status'], 'warn')

    def test_visible_destination_mismatch_warns(self):
        self.assertEqual(self.checks(blast(body_markdown='[https://mlai.au](https://example.org/)'))['link_text_mismatch']['status'], 'warn')

    def test_images_need_alt_text_and_message_text(self):
        for body in ('![Welcome](https://example.org/image.png)', '<img src="https://example.org/a.png" alt=""> word ' * 50):
            self.assertEqual(self.checks(blast(body_markdown=body))['images']['status'], 'warn')

    def test_spam_phrases_warn_without_changing_the_draft(self):
        draft = blast(body_markdown='Act now to claim your prize. Click here!')
        self.assertEqual(self.checks(draft)['spam_phrases']['status'], 'warn')
        self.assertEqual(draft.body_markdown, 'Act now to claim your prize. Click here!')

    def test_spam_score_warns_at_three_blocks_at_five_and_blocks_unknown(self):
        for score, expected in ((2.99, 'pass'), (3, 'warn'), (5, 'block')):
            self.assertEqual(self.checks(spam={'score': score})['spam_score']['status'], expected)
        self.assertEqual(self.checks(spam={'status': 'unavailable'})['spam_score']['status'], 'block')

    def test_merge_tags_and_footer_are_mandatory(self):
        self.assertEqual(self.checks(blast(body_markdown='Hi {{ password }}'))['merge_tags']['status'], 'block')
        for template in ('MLAI hi@mlai.au', 'MLAI {{ UnsubscribeURL }}',
                'hi@mlai.au {{ UnsubscribeURL }}', 'MLAI hi@mlai.au {{ UnsubscribeURL }}'):
            self.assertEqual(self.checks(template=template)['footer']['status'], 'block')

    def test_template_and_event_changes_invalidate_content_identity(self):
        draft = blast(event=CrmEvent(name='Community meetup', url='https://example.org', start_at=NOW))
        original = render.content_hash(draft, TEMPLATE)
        draft.event.name = 'Updated meetup'
        self.assertNotEqual(original, render.content_hash(draft, TEMPLATE))
        draft.event.name = 'Community meetup'
        self.assertNotEqual(original, render.content_hash(draft, TEMPLATE + '\n'))
        draft.preview_text += ' Updated'
        self.assertNotEqual(original, render.content_hash(draft, TEMPLATE))


class PublicLinksTests(SimpleTestCase):
    def test_markdown_balanced_parentheses_and_html_links_are_extracted(self):
        targets = links.link_targets('[Guide](https://example.org/wiki/Guide_(book)) <a href="https://mlai.au">MLAI</a>')
        self.assertIn(('https://example.org/wiki/Guide_(book)', 'Guide'), targets)
        self.assertIn(('https://mlai.au', 'MLAI'), targets)
        self.assertIn(('https://example.org/plain', 'https://example.org/plain'), links.link_targets('<https://example.org/plain>'))

    def test_credentials_controls_and_ports_are_rejected_before_resolution(self):
        for url in ('http://localhost/', 'http://127.0.0.1/', 'http://[::1]/', 'https://user:password@example.org',
                'https://example.org:444/', 'file:///etc/passwd', 'https://example.org/\r\nSecret: value'):
            with self.subTest(url=url), patch('socket.getaddrinfo') as resolve:
                self.assertTrue(links.inspect(url).error)
                resolve.assert_not_called()

    def test_a_mixed_public_private_dns_answer_is_rejected(self):
        answers = [(socket_family, 1, 6, '', (address, 443)) for socket_family, address in ((2, '8.8.8.8'), (2, '10.0.0.1'))]
        with patch('socket.getaddrinfo', return_value=answers), patch.object(links, 'PinnedConnection') as connect:
            self.assertEqual(links.inspect('https://example.org').error, 'nonpublic_link')
            connect.assert_not_called()

    def test_redirect_to_private_host_is_revalidated_without_forwarding_a_request(self):
        response = Mock(status=302)
        response.getheader.return_value = 'http://127.0.0.1/private'
        client = Mock(); client.getresponse.return_value = response
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443))]), patch.object(links, 'PinnedConnection', return_value=client) as connect:
            self.assertEqual(links.inspect('https://example.org').error, 'ip_address_link')
            self.assertEqual(connect.call_count, 1)
            client.request.assert_called_once()
            response.close.assert_called_once()

    def test_pin_preserves_tls_hostname_but_connects_only_to_the_validated_address(self):
        with patch('socket.create_connection') as connect, patch('ssl.create_default_context') as context:
            client = links.PinnedConnection('example.org', '8.8.8.8', 443, encrypted=True)
            client.connect()
            connect.assert_called_once_with(('8.8.8.8', 443), timeout=5)
            context.return_value.wrap_socket.assert_called_once_with(connect.return_value, server_hostname='example.org')

    def test_response_errors_warn_and_every_connection_is_closed(self):
        response = Mock(status=503)
        client = Mock(); client.getresponse.return_value = response
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443))]), patch.object(links, 'PinnedConnection', return_value=client):
            self.assertEqual(links.inspect('https://example.org').error, 'unreachable')
            client.close.assert_called_once(); response.close.assert_called_once()


class SpamTransportTests(SimpleTestCase):
    def test_real_mime_has_plain_and_html_parts_and_unknown_scores_fail_closed(self):
        from email import message_from_bytes
        message = message_from_bytes(render.mime_message(blast(), '<p>MLAI</p>', NOW))
        self.assertEqual([part.get_content_type() for part in message.get_payload()], ['text/plain', 'text/html'])
        self.assertTrue(message['Message-ID'])
        with patch('integrations.http_client.request', return_value=SimpleNamespace(status_code=503)):
            self.assertEqual(render.spam_score(blast(), '<p>MLAI</p>', 'unavailable-test', NOW)['status'], 'unavailable')
