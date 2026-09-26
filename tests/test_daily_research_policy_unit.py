"""Pure calendar policy checks; no Django setup or database creation required."""
import unittest
from datetime import datetime, timezone
from integrations.services.daily_research_policy import unanswered_days


class DailyCalendarPolicyTests(unittest.TestCase):
    def test_duplicate_channels_twice_daily_and_current_day_do_not_inflate_misses(self):
        def at(day, hour=22):
            return datetime(2026, 9, day, hour, tzinfo=timezone.utc)
        # Melbourne is UTC+10: these deliveries are on the following local morning.
        dates = unanswered_days([at(12), at(12), at(12, 23), at(13), at(14), at(15)],
                                 since=at(11), now=at(15, 23), timezone_name="Australia/Melbourne")
        self.assertEqual([d.isoformat() for d in dates], ["2026-09-13", "2026-09-14", "2026-09-15"])

    def test_response_starts_a_new_window_and_dst_uses_calendar_days(self):
        deliveries = [datetime(2026, 10, d, 0, tzinfo=timezone.utc) for d in [3, 4, 5]]
        dates = unanswered_days(deliveries, since=datetime(2026, 10, 4, 1, tzinfo=timezone.utc),
                                 now=datetime(2026, 10, 6, tzinfo=timezone.utc), timezone_name="Australia/Melbourne")
        self.assertEqual([d.isoformat() for d in dates], ["2026-10-05"])
