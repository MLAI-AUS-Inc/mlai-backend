"""Re-pin event compatibility retains bounded source facts without changing authority."""
from django.test import SimpleTestCase
from content_factory.run_events import clean_event, fold_events


class SourceRepinnedEventsTests(SimpleTestCase):
    def event(self):
        return dict(seq=1, type='source_repinned', reason='resume_latest', repins=1,
                    repin_key='run-1:1', from_sha='a' * 40, to_sha='b' * 40)

    def test_bounded_fields_are_retained_and_extra_data_is_dropped(self):
        event = self.event()
        self.assertEqual(clean_event({**event, 'email': 'untrusted@example.test'}), event)

    def test_replayed_sequence_is_applied_once(self):
        event = self.event()
        log, accepted, duplicates = fold_events(None, [event, event])
        self.assertEqual((accepted, duplicates), (1, 1))
        self.assertEqual(log['accepted'], 1)
        self.assertEqual(log['duplicates'], 1)
        self.assertEqual(log['recent'], [event])
        self.assertEqual(log['steps'], {})

    def test_malformed_source_facts_are_rejected(self):
        for change in [dict(from_sha='bad'), dict(to_sha='a' * 40), dict(repins=True),
                       dict(repins=3), dict(reason='arbitrary'), dict(repin_key='bad key')]:
            with self.subTest(change=change):
                self.assertIsNone(clean_event({**self.event(), **change}))
