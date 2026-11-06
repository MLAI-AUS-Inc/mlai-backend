"""Every staff status has plain copy with no provider implementation diagnostics."""

from datetime import datetime, timedelta
import re

from django.test import SimpleTestCase

from crm.deliverability.copy import REASONS, status_message
from crm.deliverability.volume import MELBOURNE
from crm.models import Blast, SendingStream

NOW = datetime(2026, 11, 9, 10, tzinfo=MELBOURNE)


class CampaignStatusCopyTests(SimpleTestCase):
    def blast(self, **fields):
        stream = SendingStream(pk=1, key='news', state='warming')
        return Blast(status='sending', stream=stream, channels=['email'], **fields)

    def text(self, blast, **kwargs):
        result = status_message(blast, now=NOW, **kwargs)
        self.assertNotRegex(result['text'], re.compile(r'\b(dns|spf|dkim|smtp|dmarc|listmonk|resend|ramp|webhook|canary)\b', re.I))
        return result

    def test_every_system_pause_reason_has_plain_copy(self):
        for reason in (*REASONS, 'unknown_future_reason'):
            row = self.text(self.blast(pause_state='system', pause_reason=reason))
            self.assertEqual(row['tone'], 'stop')
            self.assertIn('email team', row['text'])

    def test_inbox_pending_and_failed_have_different_messages(self):
        blast = self.blast(preflight={'seed_test': {'status': 'waiting'}})
        self.assertIn('quick inbox test', self.text(blast)['text'])
        blast.preflight = {'seed_test': {'status': 'complete'}, 'blocking': ['seed_test']}
        self.assertEqual(self.text(blast)['tone'], 'stop')

    def test_staff_pause_and_terminal_states_override_an_unrelated_stream_hold(self):
        blast = self.blast(pause_state='staff')
        self.assertIn('Paused by staff', self.text(blast)['text'])
        blast.stream.state = 'stopped'; blast.status = 'sent'; blast.stats = {'email': {'delivered': 12}}
        self.assertEqual(self.text(blast), {'tone': 'ok', 'text': 'Finished. Delivered to 12 people.'})
        blast.status = 'cancelled'; blast.stats = {'stopped_by': 'Sam', 'email': {'sent': 12}}; blast.recipient_count = 20
        self.assertIn("8 won't be sent", self.text(blast)['text'])

    def test_holds_slowing_provider_delay_and_frequency_are_explained(self):
        blast = self.blast()
        blast.stream.state = 'held'; self.assertIn('restart by itself', self.text(blast)['text'])
        blast.stream.state = 'slowed'; self.assertIn('more slowly', self.text(blast)['text'])
        blast.stream.state = 'warming'; blast.stream.state_detail = {'providers': {'microsoft': {'state': 'throttled'}}}
        self.assertIn('Outlook asked', self.text(blast)['text'])
        blast.stream.state_detail = {}; blast.stats = {'email': {'frequency_deferred': 8}}
        self.assertIn('8 people were emailed recently', self.text(blast)['text'])

    def test_next_slot_and_outside_hours_and_default_progress(self):
        blast = self.blast(plan={'estimate': {'slots': [{'at': (NOW + timedelta(hours=1)).isoformat(), 'count': 50}]}})
        self.assertIn('Next batch: about 50', self.text(blast)['text'])
        self.assertIn('next planned sending time', status_message(blast, now=NOW - timedelta(days=1))['text'])
        blast.plan = {}
        self.assertIn('most active members first', self.text(blast)['text'])
