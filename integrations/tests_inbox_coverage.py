"""Freshness describes possible unread work, independently of quiet sweep age."""
from django.test import SimpleTestCase, override_settings
from integrations.services.message_sync.read_coverage import read_coverage

@override_settings(MESSAGE_SYNC_TARGETED_READ_POLLING=True)
class InboxCoverageTests(SimpleTestCase):
    def coverage(self, values, **kwargs):
        return read_coverage(values, discovery_complete=True, now=1000, **kwargs)

    def test_quiet_baseline_can_be_complete_and_fresh_between_safety_sweeps(self):
        value = self.coverage({'quiet': {'available': True, 'is_unread': False,
                                        'fetched_at': 100, 'last_read': '200'}}, source_activity={'quiet': '150'})
        self.assertTrue(value['complete'])
        self.assertTrue(value['fresh'])
        self.assertEqual(value['possibly_unread_channels'], 0)

    def test_new_activity_requires_a_fresh_observation_without_guessing_unread(self):
        states = {'hot': {'available': True, 'is_unread': False, 'fetched_at': 800, 'last_read': '200'}}
        self.assertFalse(self.coverage(states, source_activity={'hot': '300'})['fresh'])
        states['hot']['fetched_at'] = 900
        self.assertTrue(self.coverage(states, source_activity={'hot': '300'})['fresh'])
        self.assertFalse(states['hot']['is_unread'])

    def test_unknown_baseline_and_discovery_still_prevent_completeness(self):
        self.assertFalse(self.coverage({'unknown': None})['complete'])
        self.assertFalse(read_coverage({}, discovery_complete=False, now=1000)['fresh'])
        self.assertFalse(self.coverage({}, pending_channels=1)['complete'])

    def test_known_unread_without_activity_remains_freshness_work(self):
        self.assertFalse(self.coverage({'hot': {'available': True, 'is_unread': True,
                                               'fetched_at': 800}})['fresh'])

    @override_settings(MESSAGE_SYNC_TARGETED_READ_POLLING=False)
    def test_disabled_coverage_retains_all_snapshot_freshness(self):
        value = self.coverage({'quiet': {'available': True, 'is_unread': False, 'fetched_at': 100}})
        self.assertFalse(value['fresh'])
        self.assertNotIn('freshness_basis', value)
