from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from .committee_remuneration import (
    CommitteeRemunerationService,
    format_slack_summary,
    post_slack_summary,
    week_key,
)
from .models import Ledger, PointsAdmin


User = get_user_model()


@override_settings(
    COMMITTEE_REMUNERATION_WEEKLY_POINTS=40,
    COMMITTEE_REMUNERATION_SLACK_CHANNEL="#roo-testing",
)
class CommitteeRemunerationTests(TestCase):
    def make_member(self, suffix, *, role="committee", is_active=True, linked=True):
        slack_id = f"U{suffix}"
        user = None
        if linked:
            user = User.objects.create_user(
                email=f"{suffix.lower()}@example.com",
                slack_id=slack_id,
                first_name=suffix.capitalize(),
                last_name="Member",
            )
        return PointsAdmin.objects.create(
            slack_user_id=slack_id,
            user=user,
            role=role,
            is_active=is_active,
        )

    def test_pays_weekly_points_to_active_committee_members(self):
        self.make_member("ALPHA")
        self.make_member("BRAVO")

        summary = CommitteeRemunerationService.pay(on_date=date(2026, 8, 24))

        self.assertEqual(summary["week"], "2026-W35")
        self.assertEqual(summary["points"], 40)
        self.assertEqual({m["slack_user_id"] for m in summary["paid"]}, {"UALPHA", "UBRAVO"})
        self.assertEqual(Ledger.objects.filter(kind="EARN", delta=40).count(), 2)

    def test_second_run_in_same_week_pays_nobody_twice(self):
        self.make_member("ALPHA")

        first = CommitteeRemunerationService.pay(on_date=date(2026, 8, 24))
        second = CommitteeRemunerationService.pay(on_date=date(2026, 8, 28))

        self.assertEqual(len(first["paid"]), 1)
        self.assertEqual(second["paid"], [])
        self.assertEqual(len(second["already_paid"]), 1)
        self.assertEqual(Ledger.objects.filter(kind="EARN").count(), 1)

    def test_next_week_pays_again(self):
        self.make_member("ALPHA")

        CommitteeRemunerationService.pay(on_date=date(2026, 8, 24))
        next_week = CommitteeRemunerationService.pay(on_date=date(2026, 8, 31))

        self.assertEqual(len(next_week["paid"]), 1)
        self.assertEqual(Ledger.objects.filter(kind="EARN").count(), 2)

    def test_skips_non_committee_and_inactive_members(self):
        self.make_member("ADMIN", role="admin")
        self.make_member("PARTNER", role="partner")
        self.make_member("FORMER", is_active=False)

        summary = CommitteeRemunerationService.pay(on_date=date(2026, 8, 24))

        self.assertEqual(summary["paid"], [])
        self.assertEqual(Ledger.objects.count(), 0)

    def test_unlinked_member_is_reported_and_not_paid(self):
        self.make_member("GHOST", linked=False)

        summary = CommitteeRemunerationService.pay(on_date=date(2026, 8, 24))

        self.assertEqual(summary["paid"], [])
        self.assertEqual([m["slack_user_id"] for m in summary["unlinked"]], ["UGHOST"])
        self.assertEqual(Ledger.objects.count(), 0)

    def test_dry_run_writes_no_ledger_entries(self):
        self.make_member("ALPHA")

        summary = CommitteeRemunerationService.pay(on_date=date(2026, 8, 24), dry_run=True)

        self.assertTrue(summary["dry_run"])
        self.assertEqual(len(summary["paid"]), 1)
        self.assertEqual(Ledger.objects.count(), 0)

    def test_week_key_uses_iso_week(self):
        self.assertEqual(week_key(date(2026, 1, 1)), "2026-W01")

    def test_slack_summary_names_members_and_flags_unlinked(self):
        summary = {
            "week": "2026-W35",
            "points": 40,
            "paid": [{"slack_user_id": "UALPHA", "name": "Alpha Member"}],
            "already_paid": [],
            "unlinked": [{"slack_user_id": "UGHOST", "name": "UGHOST"}],
            "dry_run": False,
        }

        text = format_slack_summary(summary)

        self.assertIn("your weekly *40 Roo points*", text)
        self.assertIn("2026-W35 · 1 member", text)
        self.assertIn("• Alpha Member", text)
        self.assertIn("No linked account", text)

    @patch("roo.committee_remuneration.SlackService.send_message", return_value=(True, "1724.5"))
    def test_posts_to_configured_channel(self, mock_send):
        summary = CommitteeRemunerationService.pay(on_date=date(2026, 8, 24), dry_run=True)

        posted, message_ts = post_slack_summary(summary)

        self.assertTrue(posted)
        self.assertEqual(message_ts, "1724.5")
        self.assertEqual(mock_send.call_args.kwargs["channel_id"], "#roo-testing")

    @patch("roo.committee_remuneration.SlackService.send_message", return_value=(True, "1724.5"))
    def test_channel_override_wins(self, mock_send):
        summary = CommitteeRemunerationService.pay(on_date=date(2026, 8, 24), dry_run=True)

        post_slack_summary(summary, channel="#mlai-committee-2026")

        self.assertEqual(mock_send.call_args.kwargs["channel_id"], "#mlai-committee-2026")
