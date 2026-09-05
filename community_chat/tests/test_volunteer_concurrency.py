"""Real PostgreSQL race tests on isolated test connections and synthetic data."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from community_chat.models import CommunityChatDevice
from community_chat.volunteer.models import (
    VolunteerAttendance,
    VolunteerMilestone,
    VolunteerOpportunity,
    VolunteerRecognition,
)
from community_chat.volunteer.policy import MELBOURNE, microroo
from community_chat.volunteer.services import (
    contribution_total,
    decision,
    request_recognition,
)
from roo.models import Ledger
from roo.services import PointsService, StartupUpdateRewardService


@skipUnless(connection.vendor == "postgresql", "PostgreSQL row-lock concurrency test")
@override_settings(
    COMMUNITY_CHAT_VOLUNTEER_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_RECOGNITION_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_AWARDS_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_BONUSES_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_COMMUNITY="volunteer-concurrency-tests",
    COMMUNITY_CHAT_VOLUNTEER_CHANNELS={"general": "general"},
)
class VolunteerConcurrencyTests(TransactionTestCase):
    """Exercise actual locking, uniqueness and ledger state under competing calls."""

    def setUp(self):
        User = get_user_model()
        self.member = User.objects.create_user(
            email="race-member@example.test", first_name="Race member"
        )
        self.reviewer = User.objects.create_user(
            email="race-reviewer@example.test",
            first_name="Race reviewer",
            is_superuser=True,
        )
        for user, key in ((self.member, "d"), (self.reviewer, "e")):
            CommunityChatDevice.objects.create(
                user=user, public_key=key * 64, status="verified"
            )
        VolunteerAttendance.objects.create(
            community="volunteer-concurrency-tests",
            user=self.member,
            event_id="prior-event",
            checked_in_at=timezone.now() - timedelta(days=1),
            source_id="synthetic-attendance",
        )
        self.event = VolunteerOpportunity.objects.create(
            community="volunteer-concurrency-tests",
            event_id="race-event",
            kind="event",
            action_key="volunteer_event",
            title="Synthetic concurrency event",
            purpose="Test concurrent recognition",
            description="Welcoming attendees",
            guide=self.reviewer,
            reviewer=self.reviewer,
            source={
                "channel_id": "general",
                "thread_root_id": "race-root",
                "source_id": "race-event",
            },
            starts_at=timezone.now() - timedelta(hours=3),
            ends_at=timezone.now() - timedelta(hours=2),
            reward_microroo=microroo("6"),
            reward_max_microroo=microroo("18"),
        )

    def race(self, first, second):
        barrier = Barrier(2)
        member_id, reviewer_id = self.member.pk, self.reviewer.pk

        def run(callback):
            close_old_connections()
            try:
                # Each thread loads independent model instances on its own
                # connection, before rendezvousing at the domain operation.
                user = get_user_model().objects.get(pk=member_id)
                reviewer = get_user_model().objects.get(pk=reviewer_id)
                barrier.wait(timeout=10)
                return callback(user, reviewer)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, callback) for callback in (first, second)]
            return [future.result(timeout=30) for future in futures]

    def payload(self, request_key):
        return dict(
            action_key="volunteer_event",
            opportunity_id=str(self.event.pk),
            source={},
            note="Welcomed attendees",
            idempotency_key=request_key,
        )

    def test_member_request_and_direct_recognition_share_one_outcome(self):
        event_id = str(self.event.pk)

        def member_request(user, reviewer):
            record, _ = request_recognition(
                user,
                dict(
                    action_key="volunteer_event",
                    opportunity_id=event_id,
                    source={},
                    note="Welcomed attendees",
                    idempotency_key="member-request",
                ),
            )
            return record.pk

        def direct_recognition(user, reviewer):
            with transaction.atomic():
                record, _ = request_recognition(
                    user,
                    dict(
                        action_key="volunteer_event",
                        opportunity_id=event_id,
                        source={},
                        note="Welcomed attendees",
                        idempotency_key="direct-request",
                    ),
                    actor=reviewer,
                )
                if record.status != "approved":
                    record, _ = decision(
                        record,
                        reviewer,
                        dict(
                            decision="approve",
                            reward_roo="6",
                            note="Thanks for welcoming new members.",
                            version=record.version,
                            idempotency_key="direct-decision",
                        ),
                    )
                return record.pk

        receipts = self.race(member_request, direct_recognition)
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(
            VolunteerRecognition.objects.filter(user=self.member).count(), 1
        )
        self.assertEqual(
            Ledger.objects.filter(
                user=self.member, reference_type="VOLUNTEER_CONTRIBUTION"
            ).count(),
            1,
        )
        self.assertEqual(VolunteerMilestone.objects.filter(user=self.member).count(), 1)
        self.assertEqual(
            PointsService.get_available_microroo(self.member), microroo("8")
        )

    def test_simultaneous_approvals_cannot_credit_or_bonus_twice(self):
        record, _ = request_recognition(self.member, self.payload("initial-request"))
        record_id, initial_version = record.pk, record.version

        def approve(user, reviewer):
            local_record = VolunteerRecognition.objects.get(pk=record_id)
            record, _ = decision(
                local_record,
                reviewer,
                dict(
                    decision="approve",
                    reward_roo="18",
                    note="Thanks for the combined event contribution.",
                    version=initial_version,
                    idempotency_key="same-decision-key",
                ),
            )
            return record.pk

        self.race(approve, approve)
        self.assertEqual(
            Ledger.objects.filter(
                user=self.member, reference_type="VOLUNTEER_CONTRIBUTION"
            ).count(),
            1,
        )
        self.assertEqual(VolunteerMilestone.objects.filter(user=self.member).count(), 2)
        self.assertEqual(
            PointsService.get_available_microroo(self.member), microroo("23")
        )

    def test_two_companies_cannot_bypass_shared_monthly_cap(self):
        from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
        from organizations.models import Organization

        profile = VibeRaisingProfile.objects.create(
            user=self.member, role=VibeRaisingProfile.ROLE_FOUNDER
        )
        company_ids = []
        for name in ("first", "second"):
            organization = Organization.objects.create(
                name=f"Synthetic {name}", domain=f"{name}.example.test"
            )
            company_ids.append(
                VibeRaisingCompany.objects.create(
                    profile=profile,
                    organization=organization,
                    name=f"Synthetic {name} Pty Ltd",
                    registered=True,
                    abn="89000000019",
                    acn="000000019",
                    abr_verified_at=timezone.now(),
                ).pk
            )
        month = timezone.now().astimezone(MELBOURNE).date().replace(day=1)

        def award(company_id):
            def perform(user, reviewer):
                company = VibeRaisingCompany.objects.get(pk=company_id)
                return StartupUpdateRewardService.award_monthly_update_completion(
                    user, company, month
                )

            return perform

        outcomes = self.race(award(company_ids[0]), award(company_ids[1]))
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(
            Ledger.objects.filter(user=self.member, source="STARTUP_UPDATE").count(), 1
        )
        self.assertEqual(
            VolunteerRecognition.objects.filter(
                user=self.member, status="approved"
            ).count(),
            1,
        )
        self.assertEqual(contribution_total(self.member), microroo("20"))
        self.assertEqual(
            PointsService.get_available_microroo(self.member), microroo("29")
        )

    def test_historical_approval_race_credits_each_level_once(self):
        from community_chat.volunteer.backfill import (
            award_historical_bonuses,
            historical_bonus_preview,
        )
        from community_chat.volunteer.models import VolunteerSourceReceipt
        from community_chat.volunteer.services import state_for

        state = state_for(self.member)
        state.historical_microroo = microroo("10")
        state.reconciled_by = self.reviewer
        state.save()
        preview = historical_bonus_preview(self.member, self.reviewer)

        def award(user, reviewer):
            return award_historical_bonuses(
                user,
                reviewer,
                expected_opening_roo="10",
                expected_ledger_cutoff=0,
                expected_state_token=preview["reviewed_state_token"],
                approved_level_keys=["level_1", "level_2"],
                reason="Explicit synthetic approval",
            )

        results = self.race(award, award)
        self.assertEqual(
            sorted(row["outcome"] for row in results), ["already_applied", "applied"]
        )
        self.assertEqual(Ledger.objects.filter(user=self.member).count(), 2)
        self.assertEqual(VolunteerMilestone.objects.filter(user=self.member).count(), 2)
        self.assertEqual(
            VolunteerSourceReceipt.objects.filter(
                kind="historical_bonus_backfill"
            ).count(),
            1,
        )
        self.assertEqual(
            PointsService.get_available_microroo(self.member), microroo("5")
        )
        self.assertEqual(contribution_total(self.member), microroo("10"))

    @override_settings(
        COMMUNITY_CHAT_VOLUNTEER_ACTIVE_FROM="2020-01-01T00:00:00Z",
        COMMUNITY_CHAT_VOLUNTEER_CHANNELS={"general": "general", "start_here": "start"},
    )
    def test_first_journey_and_intro_ingestion_initialize_consistent_history(self):
        from community_chat.volunteer.receipts import ingest_receipt
        from community_chat.volunteer.services import journey

        when = timezone.now().isoformat()

        def read(user, reviewer):
            return journey(user)

        def intro(user, reviewer):
            return ingest_receipt(
                dict(
                    origin="relay",
                    kind="post",
                    source_key="first-journey-race",
                    actor_public_key="d" * 64,
                    source={
                        "channel_id": "start",
                        "source_id": "first-journey-race",
                        "message_id": "first-journey-race",
                    },
                    metadata={"original": True, "top_level": True, "has_text": True},
                    occurred_at=when,
                )
            ).status

        self.race(read, intro)
        result = journey(self.member)
        self.assertTrue(result["history_reconciled"])
        self.assertEqual(result["contribution_roo"], "4")
        self.assertEqual(result["wallet_balance"], "6")
        self.assertEqual(VolunteerMilestone.objects.filter(user=self.member).count(), 1)
        self.assertEqual(Ledger.objects.filter(user=self.member).count(), 2)
