import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from core.models import User


MELBOURNE = ZoneInfo("Australia/Melbourne")


class OfficeManagerProvenanceMigrationTests(TransactionTestCase):
    """Prove 0037 preserves known 0036 writes and quarantines ambiguity."""

    migrate_from = ("roo", "0036_office_manager_attempts_and_provenance")
    migrate_to = (
        "roo",
        "0037_quarantine_legacy_office_manager_provenance",
    )

    def setUp(self):
        super().setUp()
        self.known_user_id = User.objects.create_user(
            email="known-provenance-migration@example.com"
        ).pk
        self.unknown_user_id = User.objects.create_user(
            email="unknown-provenance-migration@example.com"
        ).pk

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        Booking = old_apps.get_model("roo", "CoworkingBooking")
        Day = old_apps.get_model("roo", "OfficeManagerDay")
        Assignment = old_apps.get_model("roo", "OfficeManagerAssignment")
        Ledger = old_apps.get_model("roo", "Ledger")

        def create_assignment(*, user_id, day_number, purchased_microroo):
            day = Day.objects.create(
                date=f"2026-09-{day_number:02d}",
                status="claimed",
                slack_channel_id="CCOWORK",
                claim_cutoff_at=datetime(
                    2026, 9, day_number, 10, tzinfo=MELBOURNE
                ),
            )
            booking = Booking.objects.create(
                user_id=user_id,
                date=day.date,
                status="booked",
                points_cost=0,
                booking_source="office_manager",
                original_points_cost=8,
                purchased_points_cost_microroo=purchased_microroo,
            )
            refund = Ledger.objects.create(
                user_id=user_id,
                delta=8,
                delta_microroo=8_000_000,
                kind="REFUND",
                source="COWORKING",
                reference_type="OFFICE_MANAGER_ASSIGNMENT",
                reference_id=str(day.pk),
                idempotency_key=f"migration-refund-{day_number}",
            )
            return Assignment.objects.create(
                day=day,
                user_id=user_id,
                booking=booking,
                points_refunded=8,
                purchased_points_refunded_microroo=0,
                refund_ledger_entry=refund,
            ).pk

        self.known_assignment_id = create_assignment(
            user_id=self.known_user_id,
            day_number=2,
            purchased_microroo=2_000_000,
        )
        self.unknown_assignment_id = create_assignment(
            user_id=self.unknown_user_id,
            day_number=3,
            purchased_microroo=None,
        )

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        MigrationExecutor(connection).migrate([self.migrate_to])
        super().tearDown()

    def test_known_allocation_is_preserved_and_unknown_is_quarantined(self):
        Assignment = self.apps.get_model("roo", "OfficeManagerAssignment")
        Evidence = self.apps.get_model(
            "roo", "OfficeManagerProvenanceReconciliation"
        )

        known = Assignment.objects.get(pk=self.known_assignment_id)
        unknown = Assignment.objects.get(pk=self.unknown_assignment_id)
        self.assertEqual(known.purchased_points_refunded_microroo, 2_000_000)
        self.assertIsNone(unknown.purchased_points_refunded_microroo)
        self.assertEqual(Evidence.objects.count(), 0)


class OfficeManagerHardeningMigrationTests(TransactionTestCase):
    """Prove 0038 upgrades existing Office Manager rows append-only."""

    migrate_from = (
        "roo",
        "0037_quarantine_legacy_office_manager_provenance",
    )
    migrate_to = ("roo", "0038_office_manager_claim_generation")

    def setUp(self):
        super().setUp()
        self.user_id = User.objects.create_user(
            email="office-manager-0038@example.com",
            slack_id="UOFFICEMANAGER0038",
        ).pk

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        Booking = old_apps.get_model("roo", "CoworkingBooking")
        Day = old_apps.get_model("roo", "OfficeManagerDay")
        Assignment = old_apps.get_model("roo", "OfficeManagerAssignment")
        Attempt = old_apps.get_model("roo", "OfficeManagerClaimAttempt")

        day = Day.objects.create(
            date="2026-09-04",
            status="claimed",
            slack_channel_id="CCOWORK",
            claim_cutoff_at=datetime(2026, 9, 4, 10, tzinfo=MELBOURNE),
        )
        booking = Booking.objects.create(
            user_id=self.user_id,
            date=day.date,
            status="booked",
            points_cost=0,
            booking_source="office_manager",
            original_points_cost=0,
        )
        self.assignment_id = Assignment.objects.create(
            day=day,
            user_id=self.user_id,
            booking=booking,
        ).pk
        self.attempt_id = uuid.uuid4()
        Attempt.objects.create(
            attempt_id=self.attempt_id,
            slack_user_id="UOFFICEMANAGER0038",
            booking_date=day.date,
            outcome="claimed",
            assignment_id=self.assignment_id,
        )
        reopened_day = Day.objects.create(
            date="2026-09-05",
            status="open",
            slack_channel_id="CCOWORK",
            slack_message_ts="legacy-reopened.123",
            announcement_status="sent",
            message_update_pending=False,
            claim_cutoff_at=datetime(2026, 9, 5, 10, tzinfo=MELBOURNE),
        )
        reopened_booking = Booking.objects.create(
            user_id=self.user_id,
            date=reopened_day.date,
            status="cancelled",
            points_cost=0,
            booking_source="office_manager",
            original_points_cost=0,
        )
        self.reopened_day_id = reopened_day.pk
        Assignment.objects.create(
            day=reopened_day,
            user_id=self.user_id,
            booking=reopened_booking,
            status="relinquished",
        )
        self.reopened_losing_attempt_id = uuid.uuid4()
        Attempt.objects.create(
            attempt_id=self.reopened_losing_attempt_id,
            slack_user_id="ULEGACYLOSER0038",
            booking_date=reopened_day.date,
            outcome="already_claimed",
            message="Another member already claimed this day",
        )

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        MigrationExecutor(connection).migrate([self.migrate_to])
        super().tearDown()

    def test_existing_rows_receive_safe_generation_and_correction_defaults(self):
        Day = self.apps.get_model("roo", "OfficeManagerDay")
        Assignment = self.apps.get_model("roo", "OfficeManagerAssignment")
        Attempt = self.apps.get_model("roo", "OfficeManagerClaimAttempt")
        BucketRepair = self.apps.get_model(
            "roo", "OfficeManagerProvenanceBucketRepair"
        )
        ReversalEvidence = self.apps.get_model(
            "roo", "OfficeManagerRefundReversalProvenance"
        )
        SchedulerHeartbeat = self.apps.get_model(
            "roo", "ScheduledDiscoveryHeartbeat"
        )

        assignment = Assignment.objects.get(pk=self.assignment_id)
        attempt = Attempt.objects.get(pk=self.attempt_id)
        self.assertEqual(Day.objects.get(pk=assignment.day_id).generation, 1)
        self.assertEqual(attempt.generation, 1)
        self.assertFalse(assignment.private_correction_pending)
        self.assertEqual(assignment.private_correction_status, "pending")
        self.assertEqual(assignment.winner_dm_message_ts, "")
        self.assertEqual(assignment.end_of_day_reminder_message_ts, "")
        self.assertEqual(BucketRepair.objects.count(), 0)
        self.assertEqual(ReversalEvidence.objects.count(), 0)
        self.assertEqual(SchedulerHeartbeat.objects.count(), 0)

        reopened_day = Day.objects.get(pk=self.reopened_day_id)
        self.assertEqual(reopened_day.generation, 2)
        self.assertTrue(reopened_day.message_update_pending)
        reopened_attempt = Attempt.objects.get(
            pk=self.reopened_losing_attempt_id
        )
        self.assertEqual(reopened_attempt.generation, 1)
        self.assertEqual(reopened_attempt.outcome, "already_claimed")
        self.assertIsNone(reopened_attempt.superseded_at)


class OfficeManagerAttemptRepairMigrationTests(TransactionTestCase):
    """Prove already-applied 0038 databases receive the 0039 repair."""

    migrate_from = ("roo", "0038_office_manager_claim_generation")
    migrate_to = (
        "roo",
        "0039_supersede_reopened_office_manager_attempts",
    )

    def setUp(self):
        super().setUp()
        self.user_id = User.objects.create_user(
            email="office-manager-0039@example.com",
            slack_id="UOFFICEMANAGER0039",
        ).pk

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        Booking = old_apps.get_model("roo", "CoworkingBooking")
        Day = old_apps.get_model("roo", "OfficeManagerDay")
        Assignment = old_apps.get_model("roo", "OfficeManagerAssignment")
        Attempt = old_apps.get_model("roo", "OfficeManagerClaimAttempt")

        day = Day.objects.create(
            date="2026-09-06",
            status="open",
            generation=2,
            slack_channel_id="CCOWORK",
            slack_message_ts="legacy-reopened.456",
            announcement_status="sent",
            message_update_pending=True,
            claim_cutoff_at=datetime(2026, 9, 6, 10, tzinfo=MELBOURNE),
        )
        booking = Booking.objects.create(
            user_id=self.user_id,
            date=day.date,
            status="cancelled",
            points_cost=0,
            booking_source="office_manager",
            original_points_cost=0,
        )
        Assignment.objects.create(
            day=day,
            user_id=self.user_id,
            booking=booking,
            status="relinquished",
        )
        self.stale_attempt_id = uuid.uuid4()
        Attempt.objects.create(
            attempt_id=self.stale_attempt_id,
            slack_user_id="USTALE0039",
            booking_date=day.date,
            generation=1,
            outcome="already_claimed",
            message="Another member already claimed this day",
        )
        self.current_attempt_id = uuid.uuid4()
        Attempt.objects.create(
            attempt_id=self.current_attempt_id,
            slack_user_id="UCURRENT0039",
            booking_date=day.date,
            generation=2,
            outcome="claim_closed",
            message="Claims are closed",
        )

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        MigrationExecutor(connection).migrate([self.migrate_to])
        super().tearDown()

    def test_stale_attempt_is_superseded_without_touching_current_generation(self):
        Attempt = self.apps.get_model("roo", "OfficeManagerClaimAttempt")

        stale = Attempt.objects.get(pk=self.stale_attempt_id)
        current = Attempt.objects.get(pk=self.current_attempt_id)
        self.assertEqual(stale.outcome, "attempt_superseded")
        self.assertIsNotNone(stale.superseded_at)
        self.assertEqual(current.outcome, "claim_closed")
        self.assertIsNone(current.superseded_at)
