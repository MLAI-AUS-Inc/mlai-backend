from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import User
from roo.models import (
    CoworkingBooking,
    Ledger,
    OfficeManagerAssignment,
    OfficeManagerDay,
    OfficeManagerProvenanceReconciliation,
)
from roo.services import CoworkingService, PointsService


class Command(BaseCommand):
    help = (
        "Verify and record an operator-audited purchased/earned allocation for "
        "one historical coworking booking. No value is inferred."
    )

    def add_arguments(self, parser):
        parser.add_argument("--booking-id", required=True)
        parser.add_argument(
            "--purchased-microroo",
            required=True,
            type=int,
            help="Exact purchased-point portion of the original debit/refund.",
        )
        parser.add_argument("--reviewed-by", required=True)
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **options):
        booking_id = str(options["booking_id"]).strip()
        purchased_microroo = int(options["purchased_microroo"])
        reviewed_by = str(options["reviewed_by"]).strip()
        if not reviewed_by:
            raise CommandError("--reviewed-by must name the accountable operator")

        try:
            booking_snapshot = CoworkingBooking.objects.values(
                "user_id", "date"
            ).get(pk=booking_id)
        except (ValueError, CoworkingBooking.DoesNotExist) as exc:
            raise CommandError("Booking not found") from exc

        with transaction.atomic():
            # Match live mutations: principal -> date namespace -> day ->
            # assignment -> booking -> account. Reading identifiers before
            # locking is safe because the date lock fences service writers;
            # identity drift is rejected after the booking row is acquired.
            try:
                User.objects.select_for_update().get(
                    pk=booking_snapshot["user_id"]
                )
            except User.DoesNotExist as exc:
                raise CommandError("Booking owner not found") from exc
            CoworkingService._lock_booking_date(booking_snapshot["date"])

            assignment_refs = list(
                OfficeManagerAssignment.objects.filter(
                    booking_id=booking_id,
                    points_refunded__gt=0,
                )
                .order_by("day_id", "pk")
                .values("id", "day_id")
            )
            day_by_id = {
                day.pk: day
                for day in OfficeManagerDay.objects.select_for_update()
                .filter(pk__in={row["day_id"] for row in assignment_refs})
                .order_by("pk")
            }
            assignments = list(
                OfficeManagerAssignment.objects.select_for_update()
                .filter(pk__in=[row["id"] for row in assignment_refs])
                .order_by("day_id", "pk")
            )
            try:
                booking = CoworkingBooking.objects.select_for_update().get(
                    pk=booking_id
                )
            except CoworkingBooking.DoesNotExist as exc:
                raise CommandError("Booking not found") from exc
            if (
                booking.user_id != booking_snapshot["user_id"]
                or booking.date != booking_snapshot["date"]
            ):
                raise CommandError(
                    "Booking identity changed while acquiring locks; retry"
                )

            original_cost = (
                booking.original_points_cost
                if booking.booking_source == "office_manager"
                else booking.points_cost
            )
            total_microroo = PointsService.roo_to_microroo(
                max(0, int(original_cost or 0))
            )
            if total_microroo <= 0:
                raise CommandError("Booking has no historical paid debit to reconcile")
            if not 0 <= purchased_microroo <= total_microroo:
                raise CommandError(
                    "Purchased allocation must be between zero and the original debit"
                )
            ledger = (
                Ledger.objects.filter(pk=booking.ledger_entry_id).first()
                if booking.ledger_entry_id
                else None
            )
            if (
                ledger is None
                or ledger.user_id != booking.user_id
                or ledger.kind != "SPEND"
                or ledger.source != "COWORKING"
                or ledger.delta_microroo != -total_microroo
                or ledger.reference_type != "COWORKING_BOOKING"
                or ledger.reference_id
                not in {str(booking.pk), str(booking.date)}
            ):
                raise CommandError(
                    "The booking does not have a matching authoritative debit ledger"
                )

            refund_by_id = {
                refund.pk: refund
                for refund in Ledger.objects.filter(
                    pk__in={
                        assignment.refund_ledger_entry_id
                        for assignment in assignments
                        if assignment.refund_ledger_entry_id is not None
                    }
                )
            }
            for assignment in assignments:
                day = day_by_id.get(assignment.day_id)
                refund = refund_by_id.get(assignment.refund_ledger_entry_id)
                expected_refund = PointsService.roo_to_microroo(
                    assignment.points_refunded
                )
                if (
                    refund is None
                    or refund.user_id != booking.user_id
                    or refund.kind != "REFUND"
                    or refund.source != "COWORKING"
                    or refund.delta_microroo != expected_refund
                    or refund.reference_type != "OFFICE_MANAGER_ASSIGNMENT"
                    or refund.reference_id != str(assignment.day_id)
                    or expected_refund != total_microroo
                    or assignment.user_id != booking.user_id
                    or day is None
                    or day.date != booking.date
                ):
                    raise CommandError(
                        "The Office Manager assignment does not have a matching "
                        "authoritative refund ledger"
                    )

            refund_snapshot = [
                {
                    "assignment_id": assignment.pk,
                    "refund_ledger_id": assignment.refund_ledger_entry_id,
                    "refund_microroo": PointsService.roo_to_microroo(
                        assignment.points_refunded
                    ),
                }
                for assignment in assignments
            ]

            existing_evidence = (
                OfficeManagerProvenanceReconciliation.objects.select_for_update()
                .filter(booking=booking)
                .first()
            )
            if existing_evidence is not None and (
                existing_evidence.purchased_microroo != purchased_microroo
                or existing_evidence.debit_ledger_id != ledger.pk
                or existing_evidence.assignment_refund_snapshot != refund_snapshot
            ):
                raise CommandError(
                    "Booking already has different immutable reconciliation evidence"
                )

            self.stdout.write(
                "Verified booking "
                f"{booking.pk}: total={total_microroo}, "
                f"purchased={purchased_microroo}, reviewed_by={reviewed_by}"
            )
            if not options["commit"]:
                transaction.set_rollback(True)
                self.stdout.write("Dry run only; re-run with --commit to persist")
                return

            booking.purchased_points_cost_microroo = purchased_microroo
            booking.save(update_fields=["purchased_points_cost_microroo"])
            if assignments:
                OfficeManagerAssignment.objects.filter(
                    pk__in=[assignment.pk for assignment in assignments]
                ).update(
                    purchased_points_refunded_microroo=purchased_microroo
                )
            if existing_evidence is None:
                OfficeManagerProvenanceReconciliation.objects.create(
                    booking=booking,
                    debit_ledger=ledger,
                    purchased_microroo=purchased_microroo,
                    reviewed_by=reviewed_by,
                    assignment_refund_snapshot=refund_snapshot,
                )
            self.stdout.write(self.style.SUCCESS("Provenance reconciled"))
