from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import User
from roo.models import (
    CoworkingBooking,
    Ledger,
    OfficeManagerAssignment,
    OfficeManagerDay,
    OfficeManagerProvenanceBucketRepair,
    OfficeManagerProvenanceReconciliation,
    OfficeManagerRefundReversalProvenance,
    PointsAccount,
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
        parser.add_argument(
            "--reversal-purchased-microroo",
            action="append",
            default=[],
            metavar="ASSIGNMENT_ID:MICROROO",
            help=(
                "Operator-audited purchased bucket consumed by each historical "
                "refund reversal; repeat once per reversed assignment."
            ),
        )
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **options):
        booking_id = str(options["booking_id"]).strip()
        purchased_microroo = int(options["purchased_microroo"])
        reviewed_by = str(options["reviewed_by"]).strip()
        if not reviewed_by:
            raise CommandError("--reviewed-by must name the accountable operator")
        reversal_allocations: dict[int, int] = {}
        for raw_value in options["reversal_purchased_microroo"]:
            assignment_text, separator, amount_text = str(raw_value).partition(":")
            try:
                assignment_id = int(assignment_text)
                amount = int(amount_text)
            except ValueError as exc:
                raise CommandError(
                    "--reversal-purchased-microroo must use "
                    "ASSIGNMENT_ID:MICROROO"
                ) from exc
            if not separator or assignment_id <= 0 or amount < 0:
                raise CommandError(
                    "--reversal-purchased-microroo must use a positive "
                    "assignment id and non-negative microroo amount"
                )
            if assignment_id in reversal_allocations:
                raise CommandError(
                    "Each reversed assignment allocation may be supplied once"
                )
            reversal_allocations[assignment_id] = amount

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
                or not CoworkingService.booking_debit_reference_matches(
                    booking,
                    ledger.reference_id,
                )
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
            reversal_by_id = {
                reversal.pk: reversal
                for reversal in Ledger.objects.filter(
                    pk__in={
                        assignment.refund_reversal_ledger_entry_id
                        for assignment in assignments
                        if assignment.refund_reversal_ledger_entry_id is not None
                    }
                )
            }
            seen_reversal_assignment_ids: set[int] = set()
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
                if assignment.refund_reversal_ledger_entry_id is not None:
                    reversal = reversal_by_id.get(
                        assignment.refund_reversal_ledger_entry_id
                    )
                    if (
                        reversal is None
                        or reversal.user_id != booking.user_id
                        or reversal.kind != "SPEND"
                        or reversal.source != "COWORKING"
                        or reversal.delta_microroo != -expected_refund
                        or reversal.reference_type
                        != "OFFICE_MANAGER_REFUND_REVERSAL"
                        or reversal.reference_id != str(assignment.pk)
                    ):
                        raise CommandError(
                            "The Office Manager assignment does not have a "
                            "matching authoritative refund reversal ledger"
                        )
                    if assignment.pk not in reversal_allocations:
                        raise CommandError(
                            "A reversed historical refund requires "
                            "--reversal-purchased-microroo "
                            f"{assignment.pk}:MICROROO"
                        )
                    reversal_purchased = reversal_allocations[assignment.pk]
                    if reversal_purchased > expected_refund:
                        raise CommandError(
                            "A reversal purchased allocation cannot exceed its "
                            "total reversal"
                        )
                    seen_reversal_assignment_ids.add(assignment.pk)

            extra_reversal_ids = (
                set(reversal_allocations) - seen_reversal_assignment_ids
            )
            if extra_reversal_ids:
                raise CommandError(
                    "Reversal allocations were supplied for unrelated or "
                    "non-reversed assignments: "
                    + ", ".join(str(value) for value in sorted(extra_reversal_ids))
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

            reversal_evidence_by_assignment = {
                evidence.assignment_id: evidence
                for evidence in OfficeManagerRefundReversalProvenance.objects
                .select_for_update()
                .filter(assignment_id__in=seen_reversal_assignment_ids)
                .select_related("reversal_ledger")
            }
            for assignment in assignments:
                if assignment.pk not in seen_reversal_assignment_ids:
                    continue
                allocation = reversal_allocations[assignment.pk]
                evidence = reversal_evidence_by_assignment.get(assignment.pk)
                if evidence is not None and (
                    evidence.reversal_ledger_id
                    != assignment.refund_reversal_ledger_entry_id
                    or evidence.purchased_microroo != allocation
                ):
                    raise CommandError(
                        "Assignment already has different immutable refund-"
                        "reversal provenance evidence"
                    )

            repair_purchased_microroo = sum(
                (
                    reversal_allocations[assignment.pk]
                    if assignment.pk in seen_reversal_assignment_ids
                    else purchased_microroo
                )
                for assignment in assignments
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
                existing_evidence = (
                    OfficeManagerProvenanceReconciliation.objects.create(
                        booking=booking,
                        debit_ledger=ledger,
                        purchased_microroo=purchased_microroo,
                        reviewed_by=reviewed_by,
                        assignment_refund_snapshot=refund_snapshot,
                    )
                )
            for assignment in assignments:
                if assignment.pk not in seen_reversal_assignment_ids:
                    continue
                if assignment.pk in reversal_evidence_by_assignment:
                    continue
                OfficeManagerRefundReversalProvenance.objects.create(
                    assignment=assignment,
                    reversal_ledger_id=(
                        assignment.refund_reversal_ledger_entry_id
                    ),
                    purchased_microroo=reversal_allocations[assignment.pk],
                    reviewed_by=reviewed_by,
                )
            if repair_purchased_microroo:
                existing_repair = (
                    OfficeManagerProvenanceBucketRepair.objects.select_for_update()
                    .filter(reconciliation=existing_evidence)
                    .select_related("ledger")
                    .first()
                )
                if existing_repair is not None:
                    if (
                        existing_repair.purchased_microroo
                        != repair_purchased_microroo
                        or existing_repair.ledger.delta_microroo != 0
                        or existing_repair.ledger.reference_type
                        != "OFFICE_MANAGER_BUCKET_REPAIR"
                        or existing_repair.ledger.reference_id
                        != str(booking.pk)
                    ):
                        raise CommandError(
                            "Booking already has different bucket-repair evidence"
                        )
                else:
                    account = PointsAccount.objects.select_for_update().filter(
                        user=booking.user
                    ).first()
                    if account is None:
                        raise CommandError("Booking owner has no points account")
                    PointsService._ensure_microroo_account(account)
                    if (
                        account.earned_balance_microroo
                        < repair_purchased_microroo
                    ):
                        raise CommandError(
                            "The legacy refund's earned balance is no longer "
                            "available for safe bucket reclassification"
                        )
                    account_before = {
                        "balance_microroo": account.balance_microroo,
                        "earned_balance_microroo": (
                            account.earned_balance_microroo
                        ),
                        "purchased_topup_balance_microroo": (
                            account.purchased_topup_balance_microroo
                        ),
                    }
                    repair_ledger = Ledger.objects.create(
                        user=booking.user,
                        delta=0,
                        delta_microroo=0,
                        kind="ADJUST",
                        source="COWORKING",
                        reference_type="OFFICE_MANAGER_BUCKET_REPAIR",
                        reference_id=str(booking.pk),
                        description=(
                            "Reclassify legacy Office Manager refund from "
                            "earned to purchased balance"
                        ),
                        created_by_slack_id=reviewed_by,
                        idempotency_key=(
                            "office_manager_bucket_reclassification:"
                            f"{booking.pk}"
                        ),
                    )
                    account.earned_balance_microroo -= (
                        repair_purchased_microroo
                    )
                    account.purchased_topup_balance_microroo += (
                        repair_purchased_microroo
                    )
                    PointsService._sync_legacy_account(account)
                    account.save()
                    OfficeManagerProvenanceBucketRepair.objects.create(
                        reconciliation=existing_evidence,
                        ledger=repair_ledger,
                        purchased_microroo=repair_purchased_microroo,
                        account_before=account_before,
                    )
            self.stdout.write(self.style.SUCCESS("Provenance reconciled"))
