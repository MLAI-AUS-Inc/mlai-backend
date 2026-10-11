"""Financial cancellation support for bookings created by the retired pilot.

This module cannot admit claims, reopen days, schedule work or contact Slack.
The owning coworking service supplies its ordered user/date/day/booking locks.
Historical records and their purchased/earned allocation remain authoritative.
"""

from django.db import transaction
from django.utils import timezone

from .models import OfficeManagerAssignment, OfficeManagerClaimAttempt
from .permissions import InsufficientBalanceError


@transaction.atomic
def cancel_legacy_office_manager_booking(
    booking,
    *,
    requester_slack_id: str,
    locked_day,
) -> None:
    """Close a historical assignment and reverse its exact converted refund.

    Any missing allocation, inconsistent ownership or unavailable balance aborts
    the surrounding cancellation transaction. The day always stays closed.
    """
    # Imported here because CoworkingService invokes this compatibility helper.
    from .services import CoworkingService, PointsService

    assignment = (
        OfficeManagerAssignment.objects.select_for_update(of=("self",))
        .filter(booking=booking, status="active")
        .first()
    )
    if assignment is None or locked_day is None:
        raise ValueError("The historical Office Manager assignment is unavailable")
    if (
        assignment.day_id != locked_day.pk
        or assignment.user_id != booking.user_id
        or locked_day.date != booking.date
        or locked_day.status != "claimed"
    ):
        raise ValueError("The historical Office Manager assignment does not match its booking")

    if assignment.points_refunded and not assignment.refund_reversal_ledger_entry_id:
        refund_ledger = assignment.refund_ledger_entry
        expected_refund_microroo = PointsService.roo_to_microroo(
            assignment.points_refunded
        )
        purchased_refund_microroo = assignment.purchased_points_refunded_microroo
        original_debit = booking.ledger_entry
        if (
            booking.original_points_cost != assignment.points_refunded
            or original_debit is None
            or original_debit.user_id != booking.user_id
            or original_debit.kind != "SPEND"
            or original_debit.source != "COWORKING"
            or original_debit.delta_microroo != -expected_refund_microroo
            or original_debit.reference_type != "COWORKING_BOOKING"
            or not CoworkingService.booking_debit_reference_matches(
                booking, original_debit.reference_id
            )
            or booking.purchased_points_cost_microroo is None
            or booking.purchased_points_cost_microroo != purchased_refund_microroo
            or refund_ledger is None
            or refund_ledger.user_id != booking.user_id
            or refund_ledger.kind != "REFUND"
            or refund_ledger.source != "COWORKING"
            or refund_ledger.delta_microroo != expected_refund_microroo
            or refund_ledger.reference_type != "OFFICE_MANAGER_ASSIGNMENT"
            or refund_ledger.reference_id != str(locked_day.pk)
            or purchased_refund_microroo is None
            or not 0 <= purchased_refund_microroo <= expected_refund_microroo
        ):
            raise ValueError(
                "The Office Manager refund cannot be safely reversed "
                "because its authoritative ledger entry is unavailable"
            )
        try:
            reversal_ledger, _ = PointsService.spend(
                user=booking.user,
                delta=assignment.points_refunded,
                source="COWORKING",
                description=f"Reversal of Office Manager refund for {booking.date}",
                created_by_slack_id=requester_slack_id,
                idempotency_key=f"office_manager_refund_reversal:{assignment.pk}",
                reference_type="OFFICE_MANAGER_REFUND_REVERSAL",
                reference_id=str(assignment.pk),
                purchased_delta_microroo=purchased_refund_microroo,
            )
        except InsufficientBalanceError as exc:
            raise ValueError(
                "The Office Manager booking cannot be cancelled because "
                "the previously refunded Roo points are no longer available"
            ) from exc
        assignment.refund_reversal_ledger_entry = reversal_ledger
        booking.points_cost = booking.original_points_cost or assignment.points_refunded
        booking.booking_source = "points"
        booking.save(update_fields=["points_cost", "booking_source"])

    cancelled_at = timezone.now()
    assignment.status = "relinquished"
    assignment.relinquished_at = cancelled_at
    assignment.save(
        update_fields=[
            "status", "relinquished_at", "refund_reversal_ledger_entry", "updated_at",
        ]
    )
    OfficeManagerClaimAttempt.objects.filter(
        booking_date=locked_day.date,
        generation=locked_day.generation,
    ).exclude(outcome="attempt_superseded").update(
        outcome="attempt_superseded",
        message="The Office Manager pilot has retired and this booking was cancelled",
        superseded_at=cancelled_at,
    )
    locked_day.status = "closed"
    locked_day.closed_at = cancelled_at
    locked_day.save(update_fields=["status", "closed_at", "updated_at"])
