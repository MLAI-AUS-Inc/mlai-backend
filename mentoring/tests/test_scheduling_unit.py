"""Pure rule tests for slots, expiry and settlement. No database."""
from datetime import date, datetime, time, timedelta, timezone as tz
import unittest

from mentoring import scheduling

NOW = datetime(2026, 11, 2, 0, 0, tzinfo=tz.utc)  # Monday 11:00 Melbourne


class SlotTests(unittest.TestCase):
    def slots(self, **kw):
        args = dict(
            rules=[(1, time(9, 0), time(11, 0))],  # Tuesday 09:00-11:00 Melbourne
            tz_name='Australia/Melbourne', session_minutes=30,
            first_day=date(2026, 11, 3), last_day=date(2026, 11, 3), busy=[], now=NOW,
        )
        args.update(kw)
        return scheduling.generate_slots(**args)

    def test_windows_are_chopped_into_session_length_in_mentor_timezone(self):
        got = self.slots()
        self.assertEqual(len(got), 4)
        # 09:00 AEDT (UTC+11) on 3 Nov is 22:00 UTC on 2 Nov
        self.assertEqual(got[0], datetime(2026, 11, 2, 22, 0, tzinfo=tz.utc))
        self.assertEqual(got[-1], datetime(2026, 11, 2, 23, 30, tzinfo=tz.utc))

    def test_session_must_fit_inside_window(self):
        self.assertEqual(len(self.slots(session_minutes=45)), 2)
        self.assertEqual(self.slots(session_minutes=180), [])

    def test_busy_intervals_remove_overlapping_slots(self):
        busy = [(datetime(2026, 11, 2, 22, 30, tzinfo=tz.utc), datetime(2026, 11, 2, 23, 0, tzinfo=tz.utc))]
        got = self.slots(busy=busy)
        self.assertNotIn(datetime(2026, 11, 2, 22, 30, tzinfo=tz.utc), got)
        self.assertEqual(len(got), 3)

    def test_adjacent_busy_interval_does_not_block(self):
        busy = [(datetime(2026, 11, 2, 21, 0, tzinfo=tz.utc), datetime(2026, 11, 2, 22, 0, tzinfo=tz.utc))]
        self.assertEqual(len(self.slots(busy=busy)), 4)

    def test_minimum_lead_time(self):
        now = datetime(2026, 11, 2, 22, 0, tzinfo=tz.utc)
        got = self.slots(now=now)
        self.assertEqual(got[0], datetime(2026, 11, 2, 23, 0, tzinfo=tz.utc))

    def test_other_weekdays_and_far_future_excluded(self):
        self.assertEqual(self.slots(first_day=date(2026, 11, 4), last_day=date(2026, 11, 4)), [])
        far = date(2026, 12, 29)  # a Tuesday more than 30 days away
        self.assertEqual(self.slots(first_day=far, last_day=far), [])

    def test_range_validation(self):
        self.assertIsNone(scheduling.validate_range(date(2026, 11, 1), date(2026, 11, 14)))
        self.assertIsNotNone(scheduling.validate_range(date(2026, 11, 14), date(2026, 11, 1)))
        self.assertIsNotNone(scheduling.validate_range(date(2026, 11, 1), date(2026, 12, 5)))


class SettlementTests(unittest.TestCase):
    def act(self, status, now, **kw):
        start = NOW + timedelta(days=3)
        args = dict(
            status=status, now=now, starts_at=start, ends_at=start + timedelta(minutes=30),
            expires_at=NOW + timedelta(hours=48), auto_complete_hours=24,
        )
        args.update(kw)
        return scheduling.settlement_action(**args)

    def test_request_expires_at_48h_not_before(self):
        self.assertIsNone(self.act('requested', NOW + timedelta(hours=47, minutes=59)))
        self.assertEqual(self.act('requested', NOW + timedelta(hours=48)), 'expire')

    def test_accepted_auto_completes_24h_after_end(self):
        end = NOW + timedelta(days=3, minutes=30)
        self.assertIsNone(self.act('accepted', end + timedelta(hours=23, minutes=59)))
        self.assertEqual(self.act('accepted', end + timedelta(hours=24)), 'complete')

    def test_final_states_never_settle(self):
        for status in ('declined', 'cancelled', 'expired', 'completed'):
            self.assertIsNone(self.act(status, NOW + timedelta(days=30)))

    def test_expiry_is_start_time_when_sooner_than_48h(self):
        start = NOW + timedelta(hours=5)
        self.assertEqual(scheduling.request_expiry(now=NOW, starts_at=start, ttl_hours=48), start)
        far = NOW + timedelta(days=9)
        self.assertEqual(scheduling.request_expiry(now=NOW, starts_at=far, ttl_hours=48), NOW + timedelta(hours=48))


if __name__ == '__main__':
    unittest.main()
