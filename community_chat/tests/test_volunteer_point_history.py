from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from community_chat.volunteer import point_history
from community_chat.volunteer.policy import MICROROO
from community_chat.volunteer.views import PointHistoryView


class PointHistoryTests(SimpleTestCase):
    @patch.object(point_history, "rank_activity")
    @patch.object(point_history, "opening_entries")
    @patch.object(point_history.VolunteerMemberState, "objects")
    @patch.object(point_history, "one_record_enabled", return_value=True)
    def test_history_reconciles_category_credits_and_reversals(
        self, enabled, states, openings, activity
    ):
        member = object()
        state = SimpleNamespace(historical_microroo=0)
        states.filter.return_value.first.return_value = state
        reversal_date = datetime(2026, 11, 21, tzinfo=timezone.utc)
        openings.return_value = [
            SimpleNamespace(
                pk="events",
                amount_microroo=18_590 * MICROROO,
                evidence={"area": "event-delivery", "basis": "Lead or deliver events.", "support": ["private source"]},
                reversed_at=None,
            ),
            SimpleNamespace(
                pk="other",
                amount_microroo=4 * MICROROO,
                evidence={},
                reversed_at=reversal_date,
            ),
        ]
        activity.return_value = [
            (
                "contribution",
                SimpleNamespace(
                    pk="recent",
                    policy_snapshot={"title": "Member support"},
                    ledger_id=None,
                    updated_at=reversal_date,
                ),
                9 * MICROROO,
            ),
        ]

        total, rows = point_history.contribution_rows(member)

        self.assertEqual(total, "18599")
        events = next(row for row in rows if row["id"] == "opening:events")
        self.assertEqual(events["title"], "Event delivery")
        self.assertEqual(events["detail"], "Lead or deliver events.")
        self.assertIsNone(events["occurred_at"])
        self.assertNotIn("support", events)
        self.assertIn("-4", [row["amount_roo"] for row in rows])
        self.assertEqual(rows[0]["progress_roo"], total)
        activity.assert_called_once_with(member, state, with_details=True)

    @patch("community_chat.volunteer.views.wallet_page", return_value=("10", [{}, {}]))
    def test_wallet_uses_the_request_user_and_limits_the_page(self, wallet_page):
        member = object()
        request = SimpleNamespace(
            user=member, query_params={"scope": "wallet", "user_id": "other", "limit": "1"}
        )

        response = PointHistoryView().get(request)

        wallet_page.assert_called_once_with(member, 0, 1)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response.data, {"total_roo": "10", "results": [{}], "next_offset": 1})
