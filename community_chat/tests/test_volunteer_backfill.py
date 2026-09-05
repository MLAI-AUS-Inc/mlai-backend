"""Synthetic historical approval tests; queued until migration approval."""

from io import StringIO
import json

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from community_chat.models import CommunityChatDevice
from community_chat.volunteer.access import VolunteerError, community_id
from community_chat.volunteer.backfill import (
    award_historical_bonuses,
    historical_bonus_preview,
)
from community_chat.volunteer.models import (
    VolunteerMemberState,
    VolunteerMilestone,
    VolunteerSourceReceipt,
    VolunteerRecognition,
)
from community_chat.volunteer.policy import microroo
from community_chat.volunteer.services import contribution_total
from roo.models import Ledger
from roo.services import PointsService


@override_settings(
    COMMUNITY_CHAT_VOLUNTEER_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_AWARDS_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_BONUSES_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_COMMUNITY="backfill-tests",
)
class HistoricalBonusBackfillTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="historical@example.test"
        )
        self.reviewer = get_user_model().objects.create_user(
            email="reviewer@example.test", is_superuser=True
        )
        CommunityChatDevice.objects.create(
            user=self.user, public_key="a" * 64, status="verified"
        )
        self.state = VolunteerMemberState.objects.create(
            community=community_id(),
            user=self.user,
            historical_microroo=microroo("100"),
            historical_ledger_cutoff=0,
            reconciled_by=self.reviewer,
            reconciled_at=timezone.now(),
            reconciliation_note="Reviewed synthetic opening",
        )

    def approval(self, keys=None):
        proposal = historical_bonus_preview(self.user, self.reviewer)
        return dict(
            expected_opening_roo="100",
            expected_ledger_cutoff=0,
            expected_state_token=proposal["reviewed_state_token"],
            approved_level_keys=keys or [row["key"] for row in proposal["levels"]],
            reason="Committee explicitly approved these historical wallet bonuses",
        )

    def test_dry_run_is_read_only_and_reports_twenty_nine_historical(self):
        proposal = historical_bonus_preview(self.user, self.reviewer)
        self.assertEqual(
            proposal["liability"],
            {"historical_potential_roo": "29", "prospective_pending_roo": "0"},
        )
        self.assertFalse(Ledger.objects.exists())
        self.assertFalse(VolunteerMilestone.objects.exists())
        self.assertFalse(VolunteerSourceReceipt.objects.exists())
        self.state.historical_microroo = microroo("250")
        self.state.save()
        self.assertEqual(
            historical_bonus_preview(self.user, self.reviewer)["liability"][
                "historical_potential_roo"
            ],
            "49",
        )

    def test_explicit_approval_pays_once_without_advancing_contribution(self):
        approval = self.approval()
        first = award_historical_bonuses(self.user, self.reviewer, **approval)
        retry = award_historical_bonuses(self.user, self.reviewer, **approval)
        self.assertEqual(first["credited_roo"], "29")
        self.assertEqual(retry["outcome"], "already_applied")
        self.assertEqual(first["audit_receipt_id"], retry["audit_receipt_id"])
        self.assertEqual(Ledger.objects.filter(user=self.user).count(), 5)
        self.assertEqual(
            VolunteerSourceReceipt.objects.filter(
                kind="historical_bonus_backfill"
            ).count(),
            1,
        )
        self.assertEqual(
            PointsService.get_available_microroo(self.user), microroo("29")
        )
        self.assertEqual(PointsService.get_balance(self.user)["lifetime_earned"], 0)
        self.assertEqual(contribution_total(self.user), microroo("100"))

    def test_changed_reviewed_state_and_reason_are_rejected(self):
        approval = self.approval(["level_1"])
        self.state.reconciliation_note = "Changed review"
        self.state.save()
        with self.assertRaisesMessage(VolunteerError, "reviewed_state_changed"):
            award_historical_bonuses(self.user, self.reviewer, **approval)
        approval = self.approval(["level_1"])
        award_historical_bonuses(self.user, self.reviewer, **approval)
        with self.assertRaisesMessage(VolunteerError, "conflict"):
            award_historical_bonuses(
                self.user, self.reviewer, **{**approval, "reason": "Changed approval"}
            )

    def test_flags_current_permission_and_explicit_eligible_levels_are_required(self):
        approval = self.approval(["level_1"])
        with override_settings(COMMUNITY_CHAT_VOLUNTEER_AWARDS_ENABLED=False):
            with self.assertRaisesMessage(VolunteerError, "awards_disabled"):
                award_historical_bonuses(self.user, self.reviewer, **approval)
        with self.assertRaisesMessage(VolunteerError, "level_not_eligible"):
            award_historical_bonuses(
                self.user,
                self.reviewer,
                **{**approval, "approved_level_keys": ["level_6"]},
            )
        get_user_model().objects.filter(pk=self.reviewer.pk).update(is_active=False)
        with self.assertRaisesMessage(VolunteerError, "not_authorised"):
            award_historical_bonuses(self.user, self.reviewer, **approval)
        self.assertFalse(Ledger.objects.exists())

    def test_aggregate_report_separates_historical_potential_and_unknown_members(self):
        other = get_user_model().objects.create_user(email="unknown@example.test")
        CommunityChatDevice.objects.create(
            user=other, public_key="b" * 64, status="verified"
        )
        PointsService.award(
            other, 1, "MANUAL", "Unknown historical source", "system", "unknown-opening"
        )
        output = StringIO()
        call_command("audit_volunteer_journey", stdout=output)
        report = json.loads(output.getvalue())
        self.assertEqual(report["historical_potential_bonus_liability_roo"], "29")
        self.assertEqual(report["prospective_pending_bonus_liability_roo"], "0")
        self.assertEqual(report["unreconciled"], 1)
        self.assertFalse(VolunteerMemberState.objects.filter(user=other).exists())

    def test_reconciled_opening_and_independent_beneficiary_review_are_required(self):
        approval = self.approval(["level_1"])
        self.user.is_superuser = True
        self.user.save()
        with self.assertRaisesMessage(VolunteerError, "self_approval_forbidden"):
            award_historical_bonuses(self.user, self.user, **approval)
        self.state.reconciled_by = None
        self.state.save()
        with self.assertRaisesMessage(VolunteerError, "history_not_reviewed"):
            historical_bonus_preview(self.user, self.reviewer)
        self.assertFalse(Ledger.objects.exists())

    def test_paid_historical_bonus_has_private_read_only_history_and_reviewer(self):
        result = award_historical_bonuses(
            self.user, self.reviewer, **self.approval(["level_1"])
        )
        milestone_id = result["results"][0]["milestone_id"]
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.get(
            "/api/v1/community-chat/volunteer/contributions/", {"filter": "recognised"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 1)
        receipt = response.data["results"][0]
        self.assertEqual(receipt["id"], milestone_id)
        self.assertEqual(receipt["record_type"], "level_bonus")
        self.assertEqual(receipt["reward_roo"], "0")
        self.assertEqual(receipt["bonus_roo"], "2")
        self.assertEqual(receipt["reviewer"]["id"], str(self.reviewer.pk))
        self.assertFalse(
            any(receipt[key] for key in ("can_review", "can_withdraw", "can_resubmit"))
        )
        detail_url = f"/api/v1/community-chat/volunteer/contributions/{milestone_id}/"
        self.assertEqual(client.get(detail_url).status_code, 200)
        client.force_authenticate(self.reviewer)
        self.assertEqual(client.get(detail_url).status_code, 404)
        self.assertEqual(
            client.get(
                f"/api/v1/community-chat/volunteer/manage/contributions/{milestone_id}/"
            ).status_code,
            200,
        )

    def test_bonus_linked_to_recognition_is_not_duplicated_in_history(self):
        result = award_historical_bonuses(
            self.user, self.reviewer, **self.approval(["level_1"])
        )
        record = VolunteerRecognition.objects.create(
            community=community_id(),
            user=self.user,
            action_key="first_channel_contribution",
            outcome_key="linked-test",
            source={},
            policy_snapshot={
                "title": "Source contribution",
                "reward_roo": "1",
                "reward_max_roo": "1",
            },
            reward_microroo=microroo("1"),
            status="approved",
            occurred_at=timezone.now(),
        )
        VolunteerMilestone.objects.filter(
            pk=result["results"][0]["milestone_id"]
        ).update(recognition=record)
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.get(
            "/api/v1/community-chat/volunteer/contributions/", {"filter": "recognised"}
        )
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["record_type"], "contribution")
        self.assertEqual(response.data["results"][0]["bonus_roo"], "2")

    def test_merged_bonus_history_pagination_reaches_every_record_once(self):
        award_historical_bonuses(
            self.user, self.reviewer, **self.approval(["level_1", "level_2"])
        )
        for index in range(9):
            VolunteerRecognition.objects.create(
                community=community_id(),
                user=self.user,
                action_key="first_channel_contribution",
                outcome_key=f"page-{index}",
                source={},
                policy_snapshot={
                    "title": f"Receipt {index}",
                    "reward_roo": "0",
                    "reward_max_roo": "0",
                },
                status="withdrawn",
                occurred_at=timezone.now(),
            )
        client = APIClient()
        client.force_authenticate(self.user)
        endpoint = "/api/v1/community-chat/volunteer/contributions/"
        url = endpoint + "?filter=recognised&limit=3"
        seen = []
        while url:
            response = client.get(url)
            self.assertEqual(response.status_code, 200)
            seen.extend(row["id"] for row in response.data["results"])
            continuation = response.data["next"]
            if continuation:
                self.assertIn("filter=recognised", continuation)
            url = endpoint + continuation if continuation else None
        self.assertEqual(len(seen), 11)
        self.assertEqual(len(set(seen)), 11)
