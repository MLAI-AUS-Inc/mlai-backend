"""Daylight saving, half-hour zones, lead time, overlap and away dates. No database.

Australia/Melbourne 2026: DST starts Sun 4 Oct at 02:00 (clocks jump to 03:00,
UTC+10 to UTC+11) and ends Sun 5 Apr at 03:00 (clocks go back to 02:00,
UTC+11 to UTC+10). Slots advance in real time, windows are wall-clock.
"""
from datetime import date, datetime, time, timedelta, timezone as utc_tz
from zoneinfo import ZoneInfo
import unittest

from mentoring import scheduling

MEL = ZoneInfo('Australia/Melbourne')
EARLY = datetime(2026, 1, 1, tzinfo=utc_tz.utc)


def local(slots, tz=MEL):
    return [s.astimezone(tz).strftime('%d %H:%M') for s in slots]


def gen(rules, day, minutes=30, tz='Australia/Melbourne', busy=(), now=EARLY, last=None, away=()):
    return scheduling.generate_slots(
        rules=rules, tz_name=tz, session_minutes=minutes, first_day=day,
        last_day=last or day, busy=list(busy), now=now, away=away,
    )


# Sunday 4 Oct 2026 is weekday 6; Sunday 5 Apr 2026 is weekday 6.
SUN = 6
START_DAY = date(2026, 10, 4)
END_DAY = date(2026, 4, 5)
# "now" far enough back that lead time and the 30 day horizon never bite
NOW_START = datetime(2026, 9, 25, tzinfo=utc_tz.utc)
NOW_END = datetime(2026, 3, 25, tzinfo=utc_tz.utc)
NOW_NOV = datetime(2026, 10, 30, tzinfo=utc_tz.utc)


class SpringForwardTests(unittest.TestCase):
    def test_window_spanning_the_gap_offers_only_hours_that_exist(self):
        got = gen([(SUN, time(1, 0), time(4, 0))], START_DAY, now=NOW_START)
        self.assertEqual(local(got), ['04 01:00', '04 01:30', '04 03:00', '04 03:30'])

    def test_no_slot_starts_inside_the_skipped_hour(self):
        got = gen([(SUN, time(1, 0), time(4, 0))], START_DAY, now=NOW_START)
        self.assertFalse([s for s in local(got) if s.startswith('04 02:')])

    def test_window_wholly_inside_the_gap_has_no_slots(self):
        self.assertEqual(gen([(SUN, time(2, 0), time(3, 0))], START_DAY, now=NOW_START), [])
        self.assertEqual(gen([(SUN, time(2, 30), time(2, 59))], START_DAY, now=NOW_START), [])

    def test_window_starting_in_the_gap_starts_at_the_jump(self):
        got = gen([(SUN, time(2, 0), time(4, 0))], START_DAY, now=NOW_START)
        self.assertEqual(local(got), ['04 03:00', '04 03:30'])

    def test_longer_session_uses_real_elapsed_time(self):
        got = gen([(SUN, time(1, 0), time(4, 0))], START_DAY, minutes=45, now=NOW_START)
        self.assertEqual(len(got), 2)  # 2 real hours / 45 minutes

    def test_utc_instants_are_consistent_across_the_change(self):
        got = gen([(SUN, time(1, 0), time(4, 0))], START_DAY, now=NOW_START)
        self.assertEqual(got[0], datetime(2026, 10, 3, 15, 0, tzinfo=utc_tz.utc))  # 01:00 at +10
        self.assertEqual(got[2], datetime(2026, 10, 3, 16, 0, tzinfo=utc_tz.utc))  # 03:00 at +11


class FallBackTests(unittest.TestCase):
    def test_repeated_hour_is_offered_twice(self):
        got = gen([(SUN, time(1, 0), time(4, 0))], END_DAY, now=NOW_END)
        self.assertEqual(len(got), 8)  # 4 real hours
        self.assertEqual(local(got).count('05 02:00'), 2)
        self.assertEqual(local(got).count('05 02:30'), 2)

    def test_repeated_hour_slots_are_distinct_instants(self):
        got = gen([(SUN, time(2, 0), time(3, 0))], END_DAY, now=NOW_END)
        self.assertEqual(len(got), 4)
        self.assertEqual(len(set(got)), 4)
        self.assertEqual(got[1] - got[0], timedelta(minutes=30))
        self.assertEqual(got[2] - got[0], timedelta(hours=1))

    def test_busy_in_first_pass_does_not_block_the_repeat(self):
        first = datetime(2026, 4, 4, 15, 0, tzinfo=utc_tz.utc)   # 02:00 at +11
        busy = [(first, first + timedelta(minutes=30))]
        got = gen([(SUN, time(2, 0), time(3, 0))], END_DAY, busy=busy, now=NOW_END)
        self.assertEqual(len(got), 3)
        self.assertEqual(local(got).count('05 02:00'), 1)


class AlignmentTests(unittest.TestCase):
    def test_local_start_times_stay_on_the_hour_either_side_of_the_change(self):
        rules = [(5, time(9, 0), time(10, 0)), (0, time(9, 0), time(10, 0))]  # Sat, Mon
        got = gen(rules, date(2026, 10, 3), last=date(2026, 10, 5), now=NOW_START)
        self.assertEqual(local(got), ['03 09:00', '03 09:30', '05 09:00', '05 09:30'])
        # but the UTC gap between them shrinks by an hour
        self.assertEqual(got[2] - got[0], timedelta(hours=47))

    def test_slots_are_aligned_to_the_session_length_from_window_start(self):
        got = gen([(0, time(9, 15), time(11, 15))], date(2026, 11, 2), minutes=45, now=NOW_NOV)
        self.assertEqual(local(got), ['02 09:15', '02 10:00'])

    def test_half_hour_timezone_adelaide(self):
        # Adelaide is UTC+9:30 in winter, +10:30 in summer; 9am local must hold.
        for day, offset in ((date(2026, 7, 6), 9.5), (date(2026, 12, 7), 10.5)):
            got = gen([(0, time(9, 0), time(10, 0))], day, tz='Australia/Adelaide', now=datetime(2026, 6, 20, tzinfo=utc_tz.utc) if day.month == 7 else datetime(2026, 11, 20, tzinfo=utc_tz.utc))
            self.assertEqual(len(got), 2)
            self.assertEqual(got[0], datetime.combine(day, time(9, 0)).replace(tzinfo=utc_tz.utc) - timedelta(hours=offset))

    def test_quarter_hour_timezone_kathmandu(self):
        got = gen([(0, time(9, 0), time(10, 0))], date(2026, 11, 2), tz='Asia/Kathmandu', now=NOW_NOV)
        self.assertEqual(got[0], datetime(2026, 11, 2, 3, 15, tzinfo=utc_tz.utc))


class LeadTimeAndOverlapTests(unittest.TestCase):
    def test_minimum_lead_is_real_elapsed_time_across_the_gap(self):
        now = datetime(2026, 10, 3, 14, 30, tzinfo=utc_tz.utc)  # 00:30 at +10
        got = gen([(SUN, time(1, 0), time(4, 0))], START_DAY, now=now)
        # earliest = 15:30Z = 01:30 local, so 01:00 is too soon
        self.assertEqual(local(got), ['04 01:30', '04 03:00', '04 03:30'])

    def test_lead_time_boundary_is_inclusive(self):
        now = datetime(2026, 10, 3, 14, 0, tzinfo=utc_tz.utc)
        got = gen([(SUN, time(1, 0), time(4, 0))], START_DAY, now=now)
        self.assertEqual(local(got)[0], '04 01:00')  # exactly 60 minutes ahead
        got = gen([(SUN, time(1, 0), time(4, 0))], START_DAY, now=now + timedelta(seconds=1))
        self.assertEqual(local(got)[0], '04 01:30')

    def test_existing_session_after_the_jump_blocks_only_its_slot(self):
        busy_start = datetime(2026, 10, 3, 16, 0, tzinfo=utc_tz.utc)  # 03:00 at +11
        got = gen([(SUN, time(1, 0), time(4, 0))], START_DAY, now=NOW_START,
                  busy=[(busy_start, busy_start + timedelta(minutes=30))])
        self.assertEqual(local(got), ['04 01:00', '04 01:30', '04 03:30'])

    def test_session_straddling_the_jump_blocks_both_neighbours(self):
        busy_start = datetime(2026, 10, 3, 15, 45, tzinfo=utc_tz.utc)  # 01:45 +10 to 03:15 +11 (30 real minutes)
        got = gen([(SUN, time(1, 0), time(4, 0))], START_DAY, now=NOW_START,
                  busy=[(busy_start, busy_start + timedelta(minutes=30))])
        self.assertEqual(local(got), ['04 01:00', '04 03:30'])

    def test_longer_existing_session_blocks_every_overlapping_slot(self):
        busy_start = datetime(2026, 10, 3, 15, 0, tzinfo=utc_tz.utc)
        got = gen([(SUN, time(1, 0), time(4, 0))], START_DAY, now=NOW_START,
                  busy=[(busy_start, busy_start + timedelta(minutes=75))])
        self.assertEqual(local(got), ['04 03:30'])

    def test_back_to_back_sessions_do_not_conflict(self):
        busy_start = datetime(2026, 10, 3, 15, 0, tzinfo=utc_tz.utc)
        got = gen([(SUN, time(1, 0), time(4, 0))], START_DAY, now=NOW_START,
                  busy=[(busy_start, busy_start + timedelta(minutes=30))])
        self.assertEqual(local(got)[0], '04 01:30')


class ExpiryAcrossDstTests(unittest.TestCase):
    def test_48_hours_is_real_time_when_the_clock_changes(self):
        now = datetime(2026, 10, 3, 12, 0, tzinfo=utc_tz.utc)
        far = now + timedelta(days=30)
        expiry = scheduling.request_expiry(now=now, starts_at=far, ttl_hours=48)
        self.assertEqual(expiry - now, timedelta(hours=48))


class AwayDatesTests(unittest.TestCase):
    RULES = [(0, time(9, 0), time(10, 0))]  # Mondays

    def test_away_range_removes_bookings_on_those_days_only(self):
        away = [(date(2026, 11, 9), date(2026, 11, 15))]
        got = gen(self.RULES, date(2026, 11, 2), last=date(2026, 11, 23), now=NOW_NOV, away=away)
        days = sorted({s.astimezone(MEL).date().isoformat() for s in got})
        self.assertEqual(days, ['2026-11-02', '2026-11-16', '2026-11-23'])

    def test_away_range_is_inclusive_on_both_ends(self):
        away = [(date(2026, 11, 2), date(2026, 11, 2))]
        self.assertEqual(gen(self.RULES, date(2026, 11, 2), now=NOW_NOV, away=away), [])
        away = [(date(2026, 10, 30), date(2026, 11, 2))]
        self.assertEqual(gen(self.RULES, date(2026, 11, 2), now=NOW_NOV, away=away), [])

    def test_multiple_ranges(self):
        away = [(date(2026, 11, 2), date(2026, 11, 2)), (date(2026, 11, 16), date(2026, 11, 16))]
        got = gen(self.RULES, date(2026, 11, 2), last=date(2026, 11, 23), now=NOW_NOV, away=away)
        self.assertEqual({s.astimezone(MEL).day for s in got}, {9, 23})


if __name__ == '__main__':
    unittest.main()
