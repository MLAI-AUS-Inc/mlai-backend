"""No-database checks for the persisted agent-history projection."""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat.token_agent_history import daily_agent_history, history_points
from community_chat.usage_views import TokenUsageLeaderboardView


class AgentHistoryTests(SimpleTestCase):
    def test_daily_shares_use_source_normalization_and_fill_empty_days(self):
        start = date(2026, 9, 25)
        groups = [
            {
                "usage_date": start,
                "source": "codex",
                "input_tokens": 100,
                "cache_read_tokens": 80,
                "output_tokens": 20,
            },
            {
                "usage_date": start,
                "source": "claude_code",
                "input_tokens": 20,
                "cache_read_tokens": 80,
            },
            {"usage_date": start, "source": "codex", "output_tokens": 80},
            {
                "usage_date": date(2026, 9, 27),
                "source": "new_agent",
                "input_tokens": 50,
            },
            {"usage_date": date(2026, 9, 28), "source": "codex", "input_tokens": 999},
        ]
        result = history_points(groups, start, date(2026, 9, 27))
        self.assertEqual(
            [point["date"] for point in result],
            ["2026-09-25", "2026-09-26", "2026-09-27"],
        )
        self.assertEqual(result[0]["grand_total"], 300)
        self.assertEqual(
            result[0]["agents"],
            [
                {"source": "codex", "display_name": "Codex", "grand_total": 200},
                {
                    "source": "claude_code",
                    "display_name": "Claude Code",
                    "grand_total": 100,
                },
            ],
        )
        self.assertEqual(
            result[1], {"date": "2026-09-26", "grand_total": 0, "agents": []}
        )
        self.assertEqual(result[2]["agents"][0]["display_name"], "New Agent")
        for point in result:
            self.assertEqual(
                point["grand_total"], sum(row["grand_total"] for row in point["agents"])
            )

    @override_settings(TOKEN_USAGE_LEADERBOARD_TIME_ZONE="Australia/Melbourne")
    def test_query_uses_public_durable_sessions_and_local_day_boundaries(self):
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        queryset = Mock()
        queryset.annotate.return_value.values.return_value.annotate.return_value.order_by.return_value = (
            []
        )
        with patch(
            "community_chat.token_agent_history.TokenUsageSession.objects.filter",
            return_value=queryset,
        ) as filtered:
            result = daily_agent_history("7d", date(2026, 10, 5), now)
        zone = ZoneInfo("Australia/Melbourne")
        filtered.assert_called_once_with(
            account__is_public=True,
            started_at__lte=now,
            started_at__gte=datetime(2026, 9, 29, tzinfo=zone),
            started_at__lt=datetime(2026, 10, 6, tzinfo=zone),
        )
        expression = queryset.annotate.call_args.kwargs["usage_date"]
        self.assertEqual(expression.tzinfo, zone)
        self.assertEqual(result["scope"], "mlai")
        self.assertEqual(result["basis"], "session_started_at")
        self.assertEqual(result["date_from"], "2026-09-29")
        self.assertEqual(result["date_to"], "2026-10-05")
        self.assertEqual(len(result["points"]), 7)

    def test_history_range_is_bounded_before_any_query(self):
        user = SimpleNamespace(is_authenticated=True)
        request = APIRequestFactory().get("/usage/leaderboard/", {"history": "50000d"})
        force_authenticate(request, user=user)
        response = TokenUsageLeaderboardView.as_view(throttle_classes=[])(request)
        self.assertEqual(response.status_code, 400)

    def test_empty_history_is_not_a_fabricated_hundred_percent(self):
        day = date(2026, 9, 25)
        self.assertEqual(
            history_points([], day, day),
            [
                {"date": "2026-09-25", "grand_total": 0, "agents": []},
            ],
        )
