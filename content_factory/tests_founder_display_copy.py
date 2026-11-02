"""Founder display translation never changes authority, protocol or Advanced evidence."""
from django.test import SimpleTestCase
from .founder_display_copy import plain_founder_journey, TECHNICAL
from .website_journey import project_journey


class FounderDisplayCopyTests(SimpleTestCase):
    def test_plain_display_slots_keep_exact_protocol_and_technical_support(self):
        legacy = project_journey(company_id='company', domain='synthetic.test',
            website={'status':'connected', 'repositoryId':1, 'writePolicy':{'allowed':True}},
            capabilities={'repositoryAccessVerified':True, 'accountAccessVerified':True, 'repositoryWriteVerified':True})
        original_label = legacy['nextAction']['label']
        plain = plain_founder_journey(legacy)
        self.assertEqual(legacy['nextAction']['label'], original_label)
        for key in ('allowedActions', 'capabilities', 'support', 'proofContract', 'repository', 'sourceRuns'):
            self.assertEqual(plain[key], legacy[key])
        self.assertEqual(plain['reasonCode'], legacy['reasonCode'])
        self.assertEqual(plain['nextAction']['id'], legacy['nextAction']['id'])
        for before, after in zip(legacy['actions'], plain['actions']):
            self.assertEqual(before['path'], after['path'])
            self.assertEqual(before['id'], after['id'])
        self.assertEqual(plain['nextAction']['label'], 'Check website')
        for item in [*plain['steps'], *plain['actions'], plain['nextAction'], plain['policy']]:
            for key in ('reason','label'):
                self.assertFalse(TECHNICAL.search(item.get(key,'')), item)
