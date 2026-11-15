"""Volunteer build step 2: one record per contribution and opening entries."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from community_chat.models import CommunityChatDevice
from community_chat.volunteer.access import VolunteerError, community_id
from community_chat.volunteer.backfill import historical_bonus_preview
from community_chat.volunteer.credits import (
    COUNTS,
    NOT_LEVEL,
    UNLABELLED,
    classify,
    record_wallet_credit,
)
from community_chat.volunteer.models import (
    VolunteerMemberState,
    VolunteerMilestone,
    VolunteerOpeningEntry,
    VolunteerRecognition,
    VolunteerSourceReceipt,
)
from community_chat.volunteer.policy import VERSION, microroo
from community_chat.volunteer.services import (
    award_milestones,
    contribution_total,
    journey,
    state_for,
)
from roo.models import Ledger, PointsAdmin, PointsRequest
from roo.services import PointsService

User = get_user_model()
ONE_RECORD = dict(
    COMMUNITY_CHAT_VOLUNTEER_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_COMMUNITY="one-record-tests",
    COMMUNITY_CHAT_VOLUNTEER_ONE_RECORD_ENABLED=True,
)


def row(**fields):
    """An unsaved earned ledger row for label checks."""
    return Ledger(kind="EARN", delta=1, delta_microroo=1_000_000, **fields)


class CreditLabelTests(SimpleTestCase):
    def test_reference_type_decides_first(self):
        cases = {
            ("TASK", "TASK_ASSIGNMENT", ""): (COUNTS, "rate_card_task"),
            ("MANUAL", "MANUAL_ADJUSTMENT", ""): (COUNTS, "manual_award"),
            ("MANUAL", "POINTS_REQUEST", ""): (COUNTS, "points_request"),
            ("EVENT", "FIRST_CHANNEL_POST", "Completed quest: First Contact"): (
                COUNTS,
                "first_channel_post",
            ),
            ("MANUAL", "MENTOR_SESSION", ""): (NOT_LEVEL, "mentor_payout"),
            ("STARTUP_UPDATE", "MONTHLY_UPDATE_DRAFT", ""): (NOT_LEVEL, "startup_update"),
            ("MANUAL", "VOLUNTEER_LEVEL_BONUS", ""): (NOT_LEVEL, "level_bonus"),
            ("purchased_topup", "POINTS_PURCHASE", ""): (NOT_LEVEL, "purchase"),
        }
        for (source, reference, description), expected in cases.items():
            with self.subTest(reference=reference):
                self.assertEqual(
                    classify(
                        row(
                            source=source,
                            reference_type=reference,
                            description=description,
                        )
                    ),
                    expected,
                )

    def test_rows_without_a_reference_type_use_source_then_description(self):
        self.assertEqual(
            classify(row(source="EVENT", description="link-love")),
            (NOT_LEVEL, "reaction"),
        )
        self.assertEqual(
            classify(row(source="COMMUNITY", description="First post in channel C1")),
            (COUNTS, "first_post"),
        )
        self.assertEqual(
            classify(
                row(source="MANUAL", description="Weekly committee points remuneration")
            ),
            (NOT_LEVEL, "committee_pay"),
        )
        self.assertEqual(
            classify(row(source="purchased_topup", reference_type="ANYTHING")),
            (NOT_LEVEL, "purchase"),
        )
        for unknown in (
            row(source="EVENT", description="Completed quest: Something new"),
            row(source="MANUAL", reference_type="SOMETHING_NEW"),
        ):
            with self.subTest(description=unknown.description):
                self.assertEqual(classify(unknown)[0], UNLABELLED)


@override_settings(**ONE_RECORD)
class OneRecordTotalTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="member@example.test")
        self.admin = User.objects.create_user(email="admin@example.test")
        self.state = state_for(self.user)  # provably empty history: opening 0

    def award(self, points, source, key, **fields):
        ledger, _ = PointsService.award(
            user=self.user,
            delta=points,
            source=source,
            description=fields.pop("description", "Synthetic credit"),
            created_by_slack_id="UADMIN",
            idempotency_key=key,
            **fields,
        )
        return ledger

    def entry(self, amount, key, **fields):
        return VolunteerOpeningEntry.objects.create(
            community=community_id(),
            user=self.user,
            amount_microroo=microroo(str(amount)),
            evidence_class=fields.pop("evidence_class", "A"),
            source=fields.pop("source", "roo_ledger"),
            source_key=key,
            added_by=self.admin,
            **fields,
        )

    def test_flag_off_keeps_todays_being_checked_state(self):
        self.award(6, "TASK", "task:1", reference_type="TASK_ASSIGNMENT")
        with override_settings(COMMUNITY_CHAT_VOLUNTEER_ONE_RECORD_ENABLED=False):
            self.assertIsNone(contribution_total(self.user, self.state))
            self.assertIsNone(record_wallet_credit(Ledger.objects.get()))
        self.assertFalse(VolunteerRecognition.objects.exists())

    def test_counted_credits_count_once_and_write_one_record(self):
        ledger = self.award(6, "TASK", "task:1", reference_type="TASK_ASSIGNMENT")
        self.assertEqual(contribution_total(self.user, self.state), microroo("6"))
        record = record_wallet_credit(ledger)
        self.assertEqual(record_wallet_credit(ledger), record)
        self.assertEqual(VolunteerRecognition.objects.count(), 1)
        self.assertEqual(
            (record.status, record.action_key, record.reward_microroo, record.ledger_id),
            ("approved", "rate_card_task", microroo("6"), ledger.pk),
        )
        self.assertEqual(contribution_total(self.user, self.state), microroo("6"))

    def test_reactions_startup_updates_and_mentor_payouts_add_nothing(self):
        self.award(2, "EVENT", "link_love:1", description="link-love")
        self.award(20, "STARTUP_UPDATE", "monthly_update_reward:1")
        self.award(30, "MANUAL", "mentor_payout:1", reference_type="MENTOR_SESSION")
        self.assertEqual(contribution_total(self.user, self.state), 0)
        self.assertFalse(VolunteerSourceReceipt.objects.exists())

    def test_unlabelled_credit_alerts_once_instead_of_hiding_the_level(self):
        self.award(4, "MANUAL", "points_request:1", reference_type="POINTS_REQUEST")
        self.award(3, "EVENT", "quest:1", description="Completed quest: Something new")
        self.assertEqual(contribution_total(self.user, self.state), microroo("4"))
        with self.assertLogs("community_chat.volunteer.credits", "ERROR") as logs:
            result = journey(self.user)
            journey(self.user)
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(result["contribution_roo"], "4")
        alert = VolunteerSourceReceipt.objects.get()
        self.assertEqual((alert.kind, alert.status), ("unlabelled_credit", "alert"))

    def test_opening_entries_replace_the_single_opening_number(self):
        self.state.historical_microroo = None
        self.state.save()
        self.assertIsNone(contribution_total(self.user, self.state))
        self.entry(40, "linear:issue-1")
        self.entry(10, "baseline:1", evidence_class="D", source="baseline")
        self.entry(
            25,
            "linear:issue-2",
            reversed_at=timezone.now(),
            reversed_by=self.admin,
            reversal_reason="Counted twice",
        )
        self.assertEqual(contribution_total(self.user, self.state), microroo("50"))

    def test_a_source_key_can_only_be_counted_once(self):
        self.entry(5, "linear:issue-1")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.entry(5, "linear:issue-1")

    def test_level_points_can_differ_from_the_roo_paid(self):
        def record(key, action, reward, level=None, status="approved"):
            return VolunteerRecognition.objects.create(
                community=community_id(),
                user=self.user,
                outcome_key=key,
                action_key=action,
                occurred_at=timezone.now(),
                status=status,
                reward_microroo=microroo(reward),
                level_microroo=None if level is None else microroo(level),
            )

        record("rank-only", "volunteer_event", "0", level="12")
        record("ordinary", "helpful_answer", "2")
        record("boost", "boost_startup", "2")
        record("update", "monthly_startup_update", "20")
        self.assertEqual(contribution_total(self.user, self.state), microroo("14"))

    def test_bonuses_are_never_paid_for_levels_inside_the_opening(self):
        self.entry(60, "linear:issue-1")
        with override_settings(
            COMMUNITY_CHAT_VOLUNTEER_AWARDS_ENABLED=True,
            COMMUNITY_CHAT_VOLUNTEER_BONUSES_ENABLED=True,
        ):
            award_milestones(self.user)
            self.assertFalse(VolunteerMilestone.objects.exists())
            ledger = self.award(
                45, "MANUAL", "manual:1", reference_type="MANUAL_ADJUSTMENT"
            )
            record_wallet_credit(ledger)
        # 60 opening + 45 crosses 100 only, so only that bonus is paid.
        self.assertEqual(
            list(VolunteerMilestone.objects.values_list("level_key", flat=True)),
            ["level_5"],
        )

    def test_journey_stores_the_level_and_the_highest_level(self):
        self.entry(55, "linear:issue-1")
        journey(self.user)
        state = VolunteerMemberState.objects.get(pk=self.state.pk)
        self.assertEqual(
            (state.level, state.highest_level, state.level_policy), (4, 4, VERSION)
        )
        VolunteerOpeningEntry.objects.update(
            reversed_at=timezone.now(), reversed_by=self.admin
        )
        journey(self.user)
        state.refresh_from_db()
        self.assertEqual((state.level, state.highest_level), (0, 4))

    def test_one_shot_historical_bonus_refuses_members_with_entries(self):
        superuser = User.objects.create_user(email="super@example.test", is_superuser=True)
        self.entry(60, "linear:issue-1")
        with self.assertRaises(VolunteerError) as error:
            historical_bonus_preview(self.user, superuser)
        self.assertEqual(error.exception.code, "opening_entries_present")


@override_settings(
    **ONE_RECORD,
    ROO_API_KEY="synthetic-roo-points-key",
    INTERNAL_API_KEY="synthetic-other-key",
)
class EarningPathTests(APITestCase):
    def setUp(self):
        self.actor = User.objects.create_user(email="admin@example.test", slack_id="UADMIN")
        self.member = User.objects.create_user(email="member@example.test", slack_id="UMEMBER")
        CommunityChatDevice.objects.create(
            user=self.member, public_key="a" * 64, status="verified"
        )
        PointsAdmin.objects.create(
            user=self.actor, slack_user_id="UADMIN", role="admin", weekly_allowance=50
        )

    def test_manual_award_writes_one_record_even_when_replayed(self):
        self.client.credentials(HTTP_X_API_KEY="synthetic-roo-points-key")
        payload = {
            "admin_slack_id": "UADMIN",
            "target_profile_id": str(self.member.community_chat_profile_id),
            "points": 4,
            "reason": "Ran the welcome desk",
            "idempotency_key": "synthetic:1",
        }
        for _ in range(2):
            response = self.client.post(reverse("manual-award"), payload, format="json")
            self.assertEqual(response.status_code, 200, response.data)
        record = VolunteerRecognition.objects.get()
        self.assertEqual((record.action_key, record.user_id), ("manual_award", self.member.pk))
        self.assertEqual(record.ledger_id, response.data["ledger_id"])

    @patch("core.permissions.HasAPIKey.has_permission", return_value=True)
    def test_points_request_approval_writes_one_record(self, _permission):
        request = PointsRequest.objects.create(
            requester_slack_id="UMEMBER",
            target_slack_id="UMEMBER",
            points=12,
            reason="Ran the event check-in",
        )
        response = self.client.post(
            reverse("points-request-approve", args=[request.id]),
            {"admin_slack_id": "UADMIN"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        record = VolunteerRecognition.objects.get()
        self.assertEqual(
            (record.action_key, record.reward_microroo), ("points_request", microroo("12"))
        )
        self.assertEqual(contribution_total(self.member), microroo("12"))

    def test_a_first_credit_counts_but_earlier_history_stays_being_checked(self):
        newcomer = User.objects.create_user(email="new@example.test")
        first, _ = PointsService.award(
            user=newcomer, delta=6, source="TASK", description="Synthetic task",
            created_by_slack_id="UADMIN", idempotency_key="task:new",
            reference_type="TASK_ASSIGNMENT",
        )
        record_wallet_credit(first)
        self.assertEqual(contribution_total(newcomer), microroo("6"))
        PointsService.award(
            user=self.member, delta=2, source="EVENT", description="link-love",
            created_by_slack_id="UADMIN", idempotency_key="link_love:old",
        )
        later, _ = PointsService.award(
            user=self.member, delta=6, source="TASK", description="Synthetic task",
            created_by_slack_id="UADMIN", idempotency_key="task:member",
            reference_type="TASK_ASSIGNMENT",
        )
        record_wallet_credit(later)
        self.assertIsNone(contribution_total(self.member))
