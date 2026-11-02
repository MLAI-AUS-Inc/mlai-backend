"""Pure pool rules: no database, credentials, provider calls or migrations."""
from collections import Counter
from types import SimpleNamespace
import uuid

from django.test import SimpleTestCase, override_settings

from crm import sender_pool
from crm.blast_email import campaign_payload
from crm.resend_feedback import feedback_senders, monitored_senders, parse_event

SENDERS = tuple(f'{name}@mlai.au' for name in (
    'sam', 'shan', 'partnerships', 'tech', 'sonia', 'ryan', 'treasurer', 'hi'))
WARMUP = ('hello@mlai-aus.com', 'hello@mlai.org.au')


@override_settings(CRM_CAMPAIGN_SENDERS=SENDERS, CRM_RESEND_MONITORED_SENDERS=WARMUP)
class SenderPoolUnitTests(SimpleTestCase):
    def test_five_thousand_recipients_are_balanced_once_and_deterministically(self):
        people = [str(uuid.UUID(int=i)) for i in range(5000)]
        result = sender_pool.assignments(people, SENDERS)
        self.assertEqual(result, sender_pool.assignments(list(reversed(people)) + people[:3], SENDERS))
        self.assertEqual(len(result), 5000)
        self.assertEqual(Counter(sender for _, sender in result), {sender: 625 for sender in SENDERS})

    def test_empty_pool_refuses_instead_of_guessing_sender(self):
        with self.assertRaises(ValueError):
            sender_pool.assignments(['person'], [])

    def test_feedback_union_does_not_change_the_warmup_roster(self):
        self.assertEqual(monitored_senders(), WARMUP)
        self.assertEqual(set(feedback_senders()), set(WARMUP + SENDERS))
        for sender in SENDERS:
            event = parse_event({'type': 'email.delivered', 'data': {
                'from': f'MLAI <{sender}>', 'to': ['delivered@resend.dev'],
                'email_id': str(uuid.uuid4()), 'subject': 'Provider check'}})
            self.assertEqual(event['sender'], sender)
            self.assertTrue(event['test'])

    def test_every_sender_has_its_own_from_and_reply_to(self):
        for sender in SENDERS:
            blast = SimpleNamespace(pk=uuid.uuid4(), event=None, subject='MLAI news',
                body_markdown='Hello', stats={}, mailbox=SimpleNamespace(
                    email=sender, display_name='MLAI', reply_to=sender))
            payload = campaign_payload(blast, 1)
            self.assertIn(sender, payload['from_email'])
            self.assertEqual(payload['headers'], [{'Reply-To': sender}])

    @override_settings(CRM_SENDER_POOL_ENABLED=True)
    def test_warmup_drafts_are_never_converted_to_pool_campaigns(self):
        blast = SimpleNamespace(stats={'warmup': {'run_id': 'mlai-20261027'}}, channels=['email'])
        sender_pool.prepare_draft(blast)
        self.assertFalse(sender_pool.is_parent(blast))

    def test_pool_is_disabled_by_default(self):
        self.assertFalse(sender_pool.enabled())
