"""Regression coverage for retained historical financial and migration evidence."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import io
import json
import uuid
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from core.models import User
from .models import (
    CoworkingBooking, Ledger, OfficeManagerAssignment, OfficeManagerClaimAttempt,
    OfficeManagerDay, OfficeManagerProvenanceBucketRepair,
    OfficeManagerProvenanceReconciliation, OfficeManagerRefundReversalProvenance,
    PointsAccount,
)
from .services import CoworkingService, PointsService


MELBOURNE = ZoneInfo("Australia/Melbourne")


def melbourne_at(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=MELBOURNE)


def office_manager_day(day, *, status_value="open"):
    return OfficeManagerDay.objects.create(
        date=day,
        status=status_value,
        slack_channel_id="C0BRM181EDV",
        claim_cutoff_at=melbourne_at(day.year, day.month, day.day, 10),
        announcement_status="sent",
        slack_message_ts="123.456",
    )


class OfficeManagerMigrationAuditInvariantTests(TestCase):
    def test_recovery_migration_schema_has_required_constraints(self):
        from core.management.commands.audit_office_manager_migrations import (
            _office_manager_recovery_schema_issues,
        )

        self.assertEqual(_office_manager_recovery_schema_issues(), [])

    def test_hardening_migration_schema_has_required_constraints(self):
        from core.management.commands.audit_office_manager_migrations import (
            _office_manager_hardening_schema_issues,
        )

        self.assertEqual(_office_manager_hardening_schema_issues(), [])

    def test_audit_rejects_unsuperseded_attempt_from_reopened_generation(self):
        from core.management.commands.audit_office_manager_migrations import (
            _applied_migration_names,
        )

        user = User.objects.create_user(
            email="stale-attempt-audit@example.com",
            slack_id="USTALEATTEMPTAUDIT",
        )
        booking_date = melbourne_at(2026, 9, 7, 9).date()
        day = OfficeManagerDay.objects.create(
            date=booking_date,
            status="open",
            generation=2,
            slack_channel_id="C0BRM181EDV",
            claim_cutoff_at=melbourne_at(2026, 9, 7, 10),
            announcement_status="sent",
            slack_message_ts="audit-reopened.123",
        )
        booking = CoworkingBooking.objects.create(
            user=user,
            date=booking_date,
            status="cancelled",
            points_cost=0,
            booking_source="office_manager",
        )
        OfficeManagerAssignment.objects.create(
            day=day,
            user=user,
            booking=booking,
            status="relinquished",
        )
        attempt = OfficeManagerClaimAttempt.objects.create(
            attempt_id=uuid.uuid4(),
            slack_user_id="UOLDLOSER",
            booking_date=booking_date,
            generation=1,
            outcome="already_claimed",
        )
        stdout = io.StringIO()

        pre_0039_stdout = io.StringIO()
        pre_0039_applied = [
            name
            for name in _applied_migration_names()
            if not name.startswith("0039_")
        ]
        with patch(
            "core.management.commands.audit_office_manager_migrations."
            "_applied_migration_names",
            return_value=pre_0039_applied,
        ):
            call_command(
                "audit_office_manager_migrations",
                configured_office_manager_channel="C0BRM181EDV",
                stdout=pre_0039_stdout,
            )
        pre_0039_report = json.loads(pre_0039_stdout.getvalue().splitlines()[0])
        self.assertEqual(pre_0039_report["status"], "safe")
        self.assertEqual(
            pre_0039_report["data_invariants"]
            ["stale_reopened_office_manager_attempts"][0]["attempt_id"],
            str(attempt.attempt_id),
        )

        with self.assertRaises(CommandError):
            call_command(
                "audit_office_manager_migrations",
                configured_office_manager_channel="C0BRM181EDV",
                stdout=stdout,
            )

        report = json.loads(stdout.getvalue().splitlines()[0])
        self.assertEqual(
            report["data_invariants"]
            ["stale_reopened_office_manager_attempts"],
            [
                {
                    "attempt_id": str(attempt.attempt_id),
                    "date": booking_date.isoformat(),
                    "attempt_generation": 1,
                    "day_generation": 2,
                }
            ],
        )

    def test_audit_finds_stale_attempts_after_day_closed_or_reclaimed(self):
        from core.management.commands.audit_office_manager_migrations import (
            _office_manager_data_invariants,
        )

        stale_attempt_ids = []
        for offset, day_status in enumerate(("closed", "claimed")):
            booking_date = melbourne_at(2026, 9, 8 + offset, 9).date()
            former_user = User.objects.create_user(
                email=f"former-{offset}@example.com",
                slack_id=f"UFORMER{offset}",
            )
            day = OfficeManagerDay.objects.create(
                date=booking_date,
                status=day_status,
                generation=2,
                slack_channel_id="C0BRM181EDV",
                claim_cutoff_at=melbourne_at(2026, 9, 8 + offset, 10),
                announcement_status="sent",
                slack_message_ts=f"audit-reopened.{offset}",
            )
            former_booking = CoworkingBooking.objects.create(
                user=former_user,
                date=booking_date,
                status="cancelled",
                points_cost=0,
                booking_source="office_manager",
            )
            OfficeManagerAssignment.objects.create(
                day=day,
                user=former_user,
                booking=former_booking,
                status="relinquished",
            )
            if day_status == "claimed":
                current_user = User.objects.create_user(
                    email=f"current-{offset}@example.com",
                    slack_id=f"UCURRENT{offset}",
                )
                current_booking = CoworkingBooking.objects.create(
                    user=current_user,
                    date=booking_date,
                    status="booked",
                    points_cost=0,
                    booking_source="office_manager",
                )
                OfficeManagerAssignment.objects.create(
                    day=day,
                    user=current_user,
                    booking=current_booking,
                    status="active",
                )
            attempt = OfficeManagerClaimAttempt.objects.create(
                attempt_id=uuid.uuid4(),
                slack_user_id=f"USTALELIFECYCLE{offset}",
                booking_date=booking_date,
                generation=1,
                outcome="already_claimed",
            )
            stale_attempt_ids.append(str(attempt.attempt_id))

        report = _office_manager_data_invariants()

        self.assertEqual(
            {
                row["attempt_id"]
                for row in report["stale_reopened_office_manager_attempts"]
            },
            set(stale_attempt_ids),
        )

    def test_reconciliation_command_persists_immutable_operator_evidence(self):
        user = User.objects.create_user(
            email="reconcile-provenance@example.com",
            slack_id="URECONCILEPROVENANCE",
        )
        booking_date = timezone.localdate() + timedelta(days=5)
        PointsService.award(
            user=user,
            delta=8,
            source="MANUAL",
            description="reconciliation setup",
            created_by_slack_id="UADMIN",
            idempotency_key="reconciliation-award",
        )
        booking, _ = CoworkingService.book(
            user=user,
            booking_date=booking_date,
            created_by_slack_id=user.slack_id,
        )
        day = OfficeManagerDay.objects.create(
            date=booking_date,
            status="claimed",
            slack_channel_id="C0BRM181EDV",
            claim_cutoff_at=timezone.now() + timedelta(days=5),
        )
        refund, _ = PointsService.refund(
            user=user,
            delta=8,
            source="COWORKING",
            description="Office Manager booking refund",
            created_by_slack_id=user.slack_id,
            idempotency_key="reconciliation-refund",
            reference_type="OFFICE_MANAGER_ASSIGNMENT",
            reference_id=str(day.pk),
            purchased_delta_microroo=0,
            reverse_lifetime_spent=True,
        )
        booking.original_points_cost = 8
        booking.points_cost = 0
        booking.booking_source = "office_manager"
        booking.purchased_points_cost_microroo = None
        booking.refund_ledger_entry = refund
        booking.save()
        assignment = OfficeManagerAssignment.objects.create(
            day=day,
            user=user,
            booking=booking,
            points_refunded=8,
            purchased_points_refunded_microroo=None,
            refund_ledger_entry=refund,
        )

        dry_run = io.StringIO()
        call_command(
            "reconcile_office_manager_provenance",
            booking_id=str(booking.pk),
            purchased_microroo=0,
            reviewed_by="ops@example.com",
            stdout=dry_run,
        )
        booking.refresh_from_db()
        self.assertIsNone(booking.purchased_points_cost_microroo)
        self.assertFalse(
            OfficeManagerProvenanceReconciliation.objects.filter(
                booking=booking
            ).exists()
        )

        call_command(
            "reconcile_office_manager_provenance",
            booking_id=str(booking.pk),
            purchased_microroo=0,
            reviewed_by="ops@example.com",
            commit=True,
            stdout=io.StringIO(),
        )

        booking.refresh_from_db()
        assignment.refresh_from_db()
        evidence = OfficeManagerProvenanceReconciliation.objects.get(
            booking=booking
        )
        self.assertEqual(booking.purchased_points_cost_microroo, 0)
        self.assertEqual(assignment.purchased_points_refunded_microroo, 0)
        self.assertEqual(evidence.reviewed_by, "ops@example.com")
        self.assertEqual(evidence.debit_ledger_id, booking.ledger_entry_id)
        self.assertEqual(
            evidence.assignment_refund_snapshot[0]["assignment_id"],
            assignment.pk,
        )

    def test_reconciliation_reclassifies_legacy_purchased_refund_once(self):
        user = User.objects.create_user(
            email="legacy-purchased-refund@example.com",
            slack_id="ULEGACYPURCHASEDREFUND",
        )
        booking_date = timezone.localdate() + timedelta(days=5)
        PointsService.credit_purchased_topup(
            user=user,
            delta=8,
            description="Purchased setup",
            idempotency_key="legacy-purchased-refund-setup",
        )
        booking, _ = CoworkingService.book(
            user=user,
            booking_date=booking_date,
            created_by_slack_id=user.slack_id,
        )
        day = OfficeManagerDay.objects.create(
            date=booking_date,
            status="claimed",
            slack_channel_id="C0BRM181EDV",
            claim_cutoff_at=timezone.now() + timedelta(days=5),
        )
        # This reproduces the pre-provenance behavior: a purchased debit was
        # refunded wholly into the earned bucket.
        refund, _ = PointsService.refund(
            user=user,
            delta=8,
            source="COWORKING",
            description="Legacy Office Manager refund",
            created_by_slack_id=user.slack_id,
            idempotency_key="legacy-purchased-refund",
            reference_type="OFFICE_MANAGER_ASSIGNMENT",
            reference_id=str(day.pk),
            reverse_lifetime_spent=True,
        )
        booking.original_points_cost = 8
        booking.points_cost = 0
        booking.booking_source = "office_manager"
        booking.purchased_points_cost_microroo = None
        booking.refund_ledger_entry = refund
        booking.save()
        assignment = OfficeManagerAssignment.objects.create(
            day=day,
            user=user,
            booking=booking,
            points_refunded=8,
            purchased_points_refunded_microroo=None,
            refund_ledger_entry=refund,
        )
        account = PointsAccount.objects.get(user=user)
        self.assertEqual(account.earned_balance_microroo, 8_000_000)
        self.assertEqual(account.purchased_topup_balance_microroo, 0)

        for _ in range(2):
            call_command(
                "reconcile_office_manager_provenance",
                booking_id=str(booking.pk),
                purchased_microroo=8_000_000,
                reviewed_by="ops@example.com",
                commit=True,
                stdout=io.StringIO(),
            )

        account.refresh_from_db()
        booking.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(account.earned_balance_microroo, 0)
        self.assertEqual(
            account.purchased_topup_balance_microroo,
            8_000_000,
        )
        self.assertEqual(booking.purchased_points_cost_microroo, 8_000_000)
        self.assertEqual(
            assignment.purchased_points_refunded_microroo,
            8_000_000,
        )
        repair = OfficeManagerProvenanceBucketRepair.objects.get(
            reconciliation__booking=booking
        )
        self.assertEqual(repair.purchased_microroo, 8_000_000)
        self.assertEqual(repair.ledger.delta_microroo, 0)
        self.assertEqual(
            Ledger.objects.filter(
                idempotency_key=(
                    f"office_manager_bucket_reclassification:{booking.pk}"
                )
            ).count(),
            1,
        )

    def test_reversed_legacy_refund_requires_exact_reversal_bucket_evidence(self):
        user = User.objects.create_user(
            email="legacy-reversed-refund@example.com",
            slack_id="ULEGACYREVERSEDREFUND",
        )
        booking_date = timezone.localdate() + timedelta(days=5)
        PointsService.credit_purchased_topup(
            user=user,
            delta=8,
            description="Purchased setup",
            idempotency_key="legacy-reversed-refund-setup",
        )
        booking, _ = CoworkingService.book(
            user=user,
            booking_date=booking_date,
            created_by_slack_id=user.slack_id,
        )
        day = OfficeManagerDay.objects.create(
            date=booking_date,
            status="closed",
            slack_channel_id="C0BRM181EDV",
            claim_cutoff_at=timezone.now() + timedelta(days=5),
        )
        refund, _ = PointsService.refund(
            user=user,
            delta=8,
            source="COWORKING",
            description="Legacy Office Manager refund",
            created_by_slack_id=user.slack_id,
            idempotency_key="legacy-reversed-refund",
            reference_type="OFFICE_MANAGER_ASSIGNMENT",
            reference_id=str(day.pk),
            reverse_lifetime_spent=True,
        )
        booking.original_points_cost = 8
        booking.points_cost = 0
        booking.booking_source = "office_manager"
        booking.purchased_points_cost_microroo = None
        booking.refund_ledger_entry = refund
        booking.status = "cancelled"
        booking.save()
        assignment = OfficeManagerAssignment.objects.create(
            day=day,
            user=user,
            booking=booking,
            status="relinquished",
            points_refunded=8,
            purchased_points_refunded_microroo=None,
            refund_ledger_entry=refund,
        )
        reversal, _ = PointsService.spend(
            user=user,
            delta=8,
            source="COWORKING",
            description="Legacy refund reversal",
            created_by_slack_id=user.slack_id,
            idempotency_key="legacy-reversed-refund-spend",
            reference_type="OFFICE_MANAGER_REFUND_REVERSAL",
            reference_id=str(assignment.pk),
            purchased_delta_microroo=0,
        )
        assignment.refund_reversal_ledger_entry = reversal
        assignment.save(update_fields=["refund_reversal_ledger_entry"])

        with self.assertRaises(CommandError):
            call_command(
                "reconcile_office_manager_provenance",
                booking_id=str(booking.pk),
                purchased_microroo=8_000_000,
                reviewed_by="ops@example.com",
                commit=True,
                stdout=io.StringIO(),
            )

        call_command(
            "reconcile_office_manager_provenance",
            booking_id=str(booking.pk),
            purchased_microroo=8_000_000,
            reversal_purchased_microroo=[f"{assignment.pk}:0"],
            reviewed_by="ops@example.com",
            commit=True,
            stdout=io.StringIO(),
        )

        account = PointsAccount.objects.get(user=user)
        self.assertEqual(account.earned_balance_microroo, 0)
        self.assertEqual(account.purchased_topup_balance_microroo, 0)
        evidence = OfficeManagerRefundReversalProvenance.objects.get(
            assignment=assignment
        )
        self.assertEqual(evidence.reversal_ledger_id, reversal.pk)
        self.assertEqual(evidence.purchased_microroo, 0)
        self.assertFalse(
            OfficeManagerProvenanceBucketRepair.objects.filter(
                reconciliation__booking=booking
            ).exists()
        )
        call_command(
            "audit_office_manager_migrations",
            configured_office_manager_channel="C0BRM181EDV",
            stdout=io.StringIO(),
        )

    def test_audit_rejects_purchased_reconciliation_without_bucket_repair(self):
        user = User.objects.create_user(
            email="unrepaired-bucket@example.com",
            slack_id="UUNREPAIREDBUCKET",
        )
        booking_date = melbourne_at(2026, 9, 4, 9).date()
        day = OfficeManagerDay.objects.create(
            date=booking_date,
            status="claimed",
            slack_channel_id="C0BRM181EDV",
            claim_cutoff_at=melbourne_at(2026, 9, 4, 10),
        )
        booking = CoworkingBooking.objects.create(
            user=user,
            date=booking_date,
            status="booked",
            points_cost=0,
            original_points_cost=8,
            booking_source="office_manager",
            purchased_points_cost_microroo=8_000_000,
        )
        debit = Ledger.objects.create(
            user=user,
            delta=-8,
            delta_microroo=-8_000_000,
            kind="SPEND",
            source="COWORKING",
            reference_type="COWORKING_BOOKING",
            reference_id=str(booking.pk),
            idempotency_key="unrepaired-bucket-debit",
        )
        refund = Ledger.objects.create(
            user=user,
            delta=8,
            delta_microroo=8_000_000,
            kind="REFUND",
            source="COWORKING",
            reference_type="OFFICE_MANAGER_ASSIGNMENT",
            reference_id=str(day.pk),
            idempotency_key="unrepaired-bucket-refund",
        )
        booking.ledger_entry = debit
        booking.refund_ledger_entry = refund
        booking.save(update_fields=["ledger_entry", "refund_ledger_entry"])
        assignment = OfficeManagerAssignment.objects.create(
            day=day,
            user=user,
            booking=booking,
            points_refunded=8,
            purchased_points_refunded_microroo=8_000_000,
            refund_ledger_entry=refund,
        )
        OfficeManagerProvenanceReconciliation.objects.create(
            booking=booking,
            debit_ledger=debit,
            purchased_microroo=8_000_000,
            reviewed_by="ops@example.com",
            assignment_refund_snapshot=[
                {
                    "assignment_id": assignment.pk,
                    "refund_ledger_id": refund.pk,
                    "refund_microroo": 8_000_000,
                }
            ],
        )
        stdout = io.StringIO()

        with self.assertRaises(CommandError):
            call_command("audit_office_manager_migrations", stdout=stdout)

        report = json.loads(stdout.getvalue().splitlines()[0])
        self.assertEqual(
            report["data_invariants"]
            ["unrepaired_office_manager_refund_buckets"][0]["booking_id"],
            str(booking.pk),
        )

    def test_office_manager_booking_without_assignment_is_unsafe(self):
        booking = CoworkingBooking.objects.create(
            user=User.objects.create_user(
                email="orphan-office-manager@example.com",
                slack_id="UORPHANOFFICEMANAGER",
            ),
            date=melbourne_at(2026, 9, 2, 9).date(),
            points_cost=0,
            booking_source="office_manager",
        )
        stdout = io.StringIO()

        with self.assertRaises(CommandError):
            call_command("audit_office_manager_migrations", stdout=stdout)

        report = json.loads(stdout.getvalue().splitlines()[0])
        self.assertEqual(
            report["data_invariants"][
                "office_manager_bookings_without_assignment"
            ][0]["booking_id"],
            str(booking.id),
        )

    def test_refunded_assignment_without_authoritative_debit_is_unsafe(self):
        user = User.objects.create_user(
            email="missing-office-manager-debit@example.com",
            slack_id="UMISSINGOFFICEMANAGERDEBIT",
        )
        booking_date = melbourne_at(2026, 9, 5, 9).date()
        day = OfficeManagerDay.objects.create(
            date=booking_date,
            status="claimed",
            slack_channel_id="C0BRM181EDV",
            claim_cutoff_at=melbourne_at(2026, 9, 5, 10),
        )
        booking = CoworkingBooking.objects.create(
            user=user,
            date=booking_date,
            points_cost=0,
            original_points_cost=8,
            booking_source="office_manager",
            purchased_points_cost_microroo=0,
        )
        refund = Ledger.objects.create(
            user=user,
            delta=8,
            delta_microroo=8_000_000,
            kind="REFUND",
            source="COWORKING",
            reference_type="OFFICE_MANAGER_ASSIGNMENT",
            reference_id=str(day.pk),
            idempotency_key="missing-office-manager-debit-refund",
        )
        assignment = OfficeManagerAssignment.objects.create(
            day=day,
            user=user,
            booking=booking,
            points_refunded=8,
            purchased_points_refunded_microroo=0,
            refund_ledger_entry=refund,
        )
        stdout = io.StringIO()

        with self.assertRaises(CommandError):
            call_command("audit_office_manager_migrations", stdout=stdout)

        report = json.loads(stdout.getvalue().splitlines()[0])
        self.assertEqual(
            report["data_invariants"][
                "invalid_office_manager_debit_ledgers"
            ][0]["assignment_id"],
            assignment.pk,
        )

    def test_future_paid_booking_without_bucket_provenance_is_unsafe(self):
        booking = CoworkingBooking.objects.create(
            user=User.objects.create_user(
                email="unknown-booking-provenance@example.com",
                slack_id="UUNKNOWNBOOKINGPROVENANCE",
            ),
            date=melbourne_at(2030, 9, 2, 9).date(),
            points_cost=8,
            booking_source="points",
            purchased_points_cost_microroo=None,
        )
        stdout = io.StringIO()

        with self.assertRaises(CommandError):
            call_command("audit_office_manager_migrations", stdout=stdout)

        report = json.loads(stdout.getvalue().splitlines()[0])
        self.assertEqual(
            report["data_invariants"]["unreconciled_paid_bookings"][0][
                "booking_id"
            ],
            str(booking.id),
        )

    def test_claimed_day_requires_exactly_one_active_assignment(self):
        day = office_manager_day(
            melbourne_at(2026, 8, 3, 9).date(),
            status_value="claimed",
        )
        stdout = io.StringIO()

        with self.assertRaises(CommandError):
            call_command("audit_office_manager_migrations", stdout=stdout)

        report = json.loads(stdout.getvalue().splitlines()[0])
        self.assertEqual(report["status"], "unsafe")
        self.assertEqual(
            report["data_invariants"][
                "claimed_days_without_exactly_one_active_assignment"
            ],
            [
                {
                    "active_assignment_count": 0,
                    "date": day.date.isoformat(),
                    "day_id": day.id,
                }
            ],
        )

    def test_active_assignment_requires_claimed_day(self):
        day = office_manager_day(melbourne_at(2026, 8, 3, 9).date())
        user = User.objects.create_user(
            email="audit-invariant@example.com",
            slack_id="UAUDITINVARIANT",
        )
        booking = CoworkingBooking.objects.create(
            user=user,
            date=day.date,
            points_cost=0,
            booking_source="office_manager",
        )
        assignment = OfficeManagerAssignment.objects.create(
            day=day,
            user=user,
            booking=booking,
        )
        stdout = io.StringIO()

        with self.assertRaises(CommandError):
            call_command("audit_office_manager_migrations", stdout=stdout)

        report = json.loads(stdout.getvalue().splitlines()[0])
        self.assertEqual(report["status"], "unsafe")
        self.assertEqual(
            report["data_invariants"][
                "active_assignments_on_non_claimed_days"
            ],
            [
                {
                    "assignment_id": assignment.id,
                    "date": day.date.isoformat(),
                    "day_id": day.id,
                    "day_status": "open",
                }
            ],
        )
