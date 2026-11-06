"""Pure buffered scheduling checks; no database construction."""
from datetime import datetime, time, timezone
import unittest

from mentoring.scheduling import generate_slots, protected_interval


class InstantSchedulingTests(unittest.TestCase):
    def slots(self, busy=(), **changes):
        day = datetime(2026, 11, 9, tzinfo=timezone.utc).date()
        args = dict(rules=[(0, time(9), time(12))], tz_name='UTC', session_minutes=15,
                    first_day=day, last_day=day, busy=busy,
                    now=datetime(2026, 11, 8, tzinfo=timezone.utc),
                    buffer_before_minutes=15, buffer_after_minutes=15,
                    start_increment_minutes=15, minimum_notice_minutes=0)
        args.update(changes)
        return generate_slots(**args)

    def test_entire_buffer_fits_working_hours(self):
        slots = self.slots()
        self.assertEqual(slots[0].hour, 9)
        self.assertEqual(slots[0].minute, 15)
        self.assertEqual((slots[-1].hour, slots[-1].minute), (11, 30))

    def test_stacked_buffers_need_thirty_minute_gap(self):
        start = datetime(2026, 11, 9, 10, tzinfo=timezone.utc)
        busy = [protected_interval(start, 15, 15, 15)]
        slots = self.slots(busy)
        self.assertNotIn(datetime(2026, 11, 9, 10, 30, tzinfo=timezone.utc), slots)
        self.assertIn(datetime(2026, 11, 9, 10, 45, tzinfo=timezone.utc), slots)

    def test_external_touching_endpoint_is_allowed(self):
        busy = [(datetime(2026, 11, 9, 11, tzinfo=timezone.utc),
                 datetime(2026, 11, 9, 12, tzinfo=timezone.utc))]
        self.assertIn(datetime(2026, 11, 9, 10, 30, tzinfo=timezone.utc), self.slots(busy))
        self.assertNotIn(datetime(2026, 11, 9, 10, 45, tzinfo=timezone.utc), self.slots(busy))

    def test_increment_is_independent_of_duration(self):
        slots = self.slots(session_minutes=30, buffer_before_minutes=0, buffer_after_minutes=0)
        self.assertIn(datetime(2026, 11, 9, 9, 15, tzinfo=timezone.utc), slots)

    def test_away_day_blocks_all_slots(self):
        day = datetime(2026, 11, 9, tzinfo=timezone.utc).date()
        self.assertEqual(self.slots(away=[(day, day)]), [])

    def test_fall_back_offers_both_real_occurrences(self):
        from zoneinfo import ZoneInfo
        day = datetime(2026, 11, 1).date()
        slots = self.slots(rules=[(6, time(1), time(3))], tz_name='America/New_York',
            first_day=day, last_day=day, now=datetime(2026, 10, 31, tzinfo=timezone.utc))
        occurrences = [s for s in slots if s.astimezone(ZoneInfo('America/New_York')).strftime('%H:%M') == '01:15']
        self.assertEqual(len(occurrences), 2)
        self.assertEqual((occurrences[1] - occurrences[0]).total_seconds(), 3600)

    def test_spring_forward_never_offers_nonexistent_local_hour(self):
        from zoneinfo import ZoneInfo
        day = datetime(2027, 3, 14).date()
        slots = self.slots(rules=[(6, time(1), time(4))], tz_name='America/New_York',
            first_day=day, last_day=day, now=datetime(2027, 3, 13, tzinfo=timezone.utc))
        self.assertTrue(slots)
        self.assertTrue(all(s.astimezone(ZoneInfo('America/New_York')).hour != 2 for s in slots))
