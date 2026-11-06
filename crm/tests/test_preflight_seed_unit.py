"""Receiver header proofs and read-only IMAP fixtures, without real inbox access."""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from crm.deliverability.preflight import seed_inbox

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)
DOMAIN = 'news.mlai.au'


def raw_message(token, *, authority='mx.google.com', auth='pass', signatures=None, from_address='hello@news.mlai.au'):
    signatures = signatures or ['v=1; d=news.mlai.au; s=resend; h=from:subject:list-unsubscribe:list-unsubscribe-post:feedback-id; b=synthetic']
    lines = [f'From: MLAI <{from_address}>', f'X-MLAI-Seed: {token}',
        f'Authentication-Results: {authority}; spf={auth}; dkim={auth} header.d=news.mlai.au header.s=resend; dmarc={auth}',
        'List-Unsubscribe: <https://lists.mlai.au/subscription/synthetic>', 'List-Unsubscribe-Post: List-Unsubscribe=One-Click',
        *['DKIM-Signature: ' + value for value in signatures], '', 'Synthetic probe']
    return '\r\n'.join(lines).encode()


class SeedEvidenceTests(SimpleTestCase):
    def result(self, provider='gmail', **fields):
        authority = {'gmail': 'mx.google.com', 'outlook': 'mx.outlook.com', 'yahoo': 'mta.mail.yahoo.com'}[provider]
        return seed_inbox.header_result(raw_message('token', authority=authority, **fields),
            provider=provider, folder='inbox', stream_domain=DOMAIN, now=NOW)

    def test_all_major_inboxes_need_authentication_and_one_valid_aligned_signature(self):
        rows = [self.result(provider) for provider in ('gmail', 'outlook', 'yahoo')]
        self.assertEqual(seed_inbox.grade(rows), ('pass', 'inboxes_reached'))
        self.assertTrue(all(row['one_click_signed'] and row['feedback_id_signed'] for row in rows))

    def test_observed_failed_or_missing_authentication_blocks(self):
        self.assertEqual(seed_inbox.grade([self.result(auth='fail')])[0], 'block')
        raw = raw_message('token').replace(b'Authentication-Results:', b'X-Untrusted-Results:')
        row = seed_inbox.header_result(raw, provider='gmail', folder='inbox', stream_domain=DOMAIN, now=NOW)
        self.assertEqual(row['dkim'], 'unknown')
        self.assertEqual(seed_inbox.grade([row])[0], 'block')

    def test_untrusted_authentication_authority_is_not_accepted(self):
        for authority in ('evil.example', 'mx.google.com.evil.example', 'evil.google.com'):
            row = seed_inbox.header_result(raw_message('token', authority=authority), provider='gmail',
                folder='inbox', stream_domain=DOMAIN, now=NOW)
            self.assertEqual(row['spf'], 'unknown')
            self.assertEqual(seed_inbox.grade([row])[0], 'block')

    def test_unsigned_one_click_headers_block(self):
        row = self.result(signatures=['v=1; d=news.mlai.au; s=resend; h=from:subject; b=synthetic'])
        self.assertFalse(row['one_click_signed'])
        self.assertEqual(seed_inbox.grade([row])[0], 'block')

    def test_two_different_signatures_cannot_be_combined_into_false_one_click_proof(self):
        row = self.result(signatures=['v=1; d=news.mlai.au; s=resend; h=from:list-unsubscribe; b=synthetic',
            'v=1; d=news.mlai.au; s=unverified; h=from:list-unsubscribe-post; b=synthetic'])
        self.assertNotIn('list-unsubscribe-post', row['dkim_signed_headers'])
        self.assertFalse(row['one_click_signed'])
        self.assertEqual(seed_inbox.grade([row])[0], 'block')

    def test_a_signature_that_did_not_pass_is_not_counted(self):
        row = self.result(signatures=['v=1; d=news.mlai.au; s=wrong; h=from:list-unsubscribe:list-unsubscribe-post; b=synthetic'])
        self.assertEqual(row['dkim'], 'unknown')
        self.assertFalse(row['one_click_signed'])

    def test_mailto_alone_does_not_satisfy_one_click_unsubscribe(self):
        raw = raw_message('token').replace(b'<https://lists.mlai.au/subscription/synthetic>', b'<mailto:hi@mlai.au>')
        row = seed_inbox.header_result(raw, provider='gmail', folder='inbox', stream_domain=DOMAIN, now=NOW)
        self.assertEqual(seed_inbox.grade([row])[0], 'block')

    def test_one_spam_provider_warns_two_block_and_duplicate_gmail_inboxes_are_one_provider(self):
        google = {**self.result(), 'folder': 'spam'}
        microsoft = {**self.result('outlook'), 'folder': 'spam'}
        self.assertEqual(seed_inbox.grade([google, {**google}])[0], 'warn')
        self.assertEqual(seed_inbox.grade([google, microsoft]), ('block', 'multiple_spam_folders'))

    def test_missing_coverage_and_timeout_warn_instead_of_inventing_inbox_success(self):
        self.assertEqual(seed_inbox.grade([self.result()]), ('warn', 'coverage_incomplete'))
        self.assertEqual(seed_inbox.grade([{'provider': 'gmail', 'folder': 'missing'}], timed_out=True), ('warn', 'incomplete_check'))

    def test_feedback_header_signature_gaps_warn_owners(self):
        rows = [self.result(provider) for provider in ('gmail', 'outlook', 'yahoo')]
        rows[0]['feedback_id_signed'] = False
        self.assertEqual(seed_inbox.grade(rows), ('warn', 'feedback_header_unsigned'))


class ImapProbeTests(SimpleTestCase):
    def test_special_use_flags_find_localized_junk_without_selecting_other_folders(self):
        folders = seed_inbox.folder_list([b'(\\HasNoChildren \\Junk) "/" "Correo basura"',
            b'(\\Noselect \\All) "/" "Parent"', b'(\\All) "/" "Archive"'])
        self.assertIn((b'"Correo basura"', 'spam'), folders)
        self.assertIn((b'"Archive"', 'missing'), folders)
        self.assertNotIn((b'"Parent"', 'missing'), folders)

    def test_gmail_search_and_fetch_are_read_only_and_categories_use_documented_raw_search(self):
        token = uuid.uuid4()
        mailbox = SimpleNamespace(provider='gmail', secret_env='CRM_SEED_GMAIL_PASSWORD', auth='app_password',
            address='seed@example.test', imap_host='imap.example.test', imap_port=993)
        probe = SimpleNamespace(token=token, stream=SimpleNamespace(domain=DOMAIN, mailbox=SimpleNamespace(email='hello@news.mlai.au')))
        client = Mock(capabilities=(b'IMAP4rev1', b'X-GM-EXT-1'))
        client.list.return_value = ('OK', [])
        client.select.return_value = ('OK', [b'1'])
        def uid(command, *args):
            if command == 'FETCH':
                return 'OK', [(b'7 (X-GM-LABELS (\\Inbox))', raw_message(token))]
            return 'OK', [b'7']
        client.uid.side_effect = uid
        with patch.dict('os.environ', {'CRM_SEED_GMAIL_PASSWORD': 'synthetic-only'}), patch(
                'imaplib.IMAP4_SSL', return_value=client):
            result = seed_inbox.read_mailbox(mailbox, probe, now=NOW)
        self.assertEqual(result['folder'], 'promotions')
        client.select.assert_called_once_with(b'INBOX', readonly=True)
        self.assertTrue(any('BODY.PEEK[HEADER]' in str(call) for call in client.uid.call_args_list))
        self.assertTrue(any('category:promotions' in str(call) for call in client.uid.call_args_list))
        self.assertTrue(all(call.args[0] in ('FETCH', 'SEARCH') for call in client.uid.call_args_list))
        client.logout.assert_called_once()

    def test_arbitrary_environment_secret_names_are_rejected_without_reading_an_inbox(self):
        mailbox = SimpleNamespace(provider='gmail', secret_env='RESEND_API_KEY')
        with patch('imaplib.IMAP4_SSL') as connect:
            self.assertEqual(seed_inbox.read_mailbox(mailbox, None, now=NOW)['error'], 'credential_reference_invalid')
        connect.assert_not_called()
