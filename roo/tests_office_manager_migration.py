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
