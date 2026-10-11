"""Database-free checks for retained historical cancellation boundaries."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid

from django.contrib.admin.sites import AdminSite
from django.test import SimpleTestCase, override_settings
from django.urls import Resolver404, resolve

from core.permissions import HasIsolatedRooApiKey
from .admin import OfficeManagerAssignmentAdmin, OfficeManagerDayAdmin
from .legacy_office_manager import cancel_legacy_office_manager_booking
from .models import OfficeManagerAssignment, OfficeManagerClaimAttempt, OfficeManagerDay
from .permissions import InsufficientBalanceError
from .services import PointsService


class LegacyOfficeManagerCancellationTests(SimpleTestCase):
    def setUp(self):
        self.day = SimpleNamespace(
            pk=11, date=date(2026, 9, 7), status="claimed", generation=1,
            save=Mock(),
        )
        booking_id = uuid.uuid4()
        self.booking = SimpleNamespace(
            pk=booking_id, user_id=7, user=SimpleNamespace(pk=7), date=self.day.date,
            points_cost=0, original_points_cost=8, booking_source="office_manager",
            purchased_points_cost_microroo=2_000_000, save=Mock(),
            ledger_entry=SimpleNamespace(
                user_id=7, kind="SPEND", source="COWORKING", delta_microroo=-8_000_000,
                reference_type="COWORKING_BOOKING", reference_id=str(booking_id),
            ),
        )
        self.assignment = SimpleNamespace(
            pk=23, day_id=11, user_id=7, status="active", points_refunded=8,
            purchased_points_refunded_microroo=2_000_000,
            refund_reversal_ledger_entry_id=None, save=Mock(),
            refund_ledger_entry=SimpleNamespace(
                user_id=7, kind="REFUND", source="COWORKING", delta_microroo=8_000_000,
                reference_type="OFFICE_MANAGER_ASSIGNMENT", reference_id="11",
            ),
        )

    def cancel(self, *, spend_error=None, assignment_present=True, day_present=True):
        with (
            patch.object(OfficeManagerAssignment, "objects") as assignments,
            patch.object(OfficeManagerClaimAttempt, "objects") as attempts,
            patch.object(PointsService, "spend") as spend,
        ):
            assignments.select_for_update.return_value.filter.return_value.first.return_value = (
                self.assignment if assignment_present else None
            )
            self.last_spend = spend
            spend.return_value = (SimpleNamespace(pk=99), True)
            spend.side_effect = spend_error
            # The caller owns the transaction. Bypass only the decorator so the
            # DB-free harness can exercise the financial logic with fake records.
            cancel_legacy_office_manager_booking.__wrapped__(
                self.booking,
                requester_slack_id="ULEGACY",
                locked_day=self.day if day_present else None,
            )
            return spend, attempts

    def test_conversion_reverses_exact_original_buckets_and_closes_day(self):
        spend, attempts = self.cancel()

        payload = spend.call_args.kwargs
        self.assertEqual(payload["delta"], 8)
        self.assertEqual(payload["purchased_delta_microroo"], 2_000_000)
        self.assertEqual(payload["idempotency_key"], "office_manager_refund_reversal:23")
        self.assertEqual(payload["reference_type"], "OFFICE_MANAGER_REFUND_REVERSAL")
        self.assertEqual(self.booking.booking_source, "points")
        self.assertEqual(self.booking.points_cost, 8)
        self.assertEqual(self.assignment.status, "relinquished")
        self.assertEqual(self.assignment.refund_reversal_ledger_entry.pk, 99)
        self.assertEqual(self.day.status, "closed")
        self.assertEqual(self.day.generation, 1)
        self.day.save.assert_called_once_with(
            update_fields=["status", "closed_at", "updated_at"]
        )
        self.assertEqual(
            attempts.filter.return_value.exclude.return_value.update.call_args.kwargs["outcome"],
            "attempt_superseded",
        )

    def test_free_booking_cancellation_never_spends_points(self):
        self.assignment.points_refunded = 0
        spend, _ = self.cancel()
        spend.assert_not_called()
        self.booking.save.assert_not_called()
        self.assertEqual(self.day.status, "closed")

    def test_legacy_date_debit_reference_remains_accepted(self):
        self.booking.ledger_entry.reference_id = str(self.booking.date)
        spend, _ = self.cancel()
        spend.assert_called_once()

    def test_unknown_or_mismatched_provenance_aborts_before_financial_mutation(self):
        mutations = (
            ("booking", "purchased_points_cost_microroo", None),
            ("assignment", "purchased_points_refunded_microroo", None),
            ("assignment", "purchased_points_refunded_microroo", 9_000_000),
            ("assignment", "purchased_points_refunded_microroo", 1_000_000),
            ("booking", "original_points_cost", 4),
            ("debit", "user_id", 8),
            ("debit", "reference_id", "another-booking"),
            ("debit", "delta_microroo", -4_000_000),
            ("refund", "user_id", 8),
            ("refund", "reference_id", "another-day"),
            ("refund", "delta_microroo", 4_000_000),
        )
        records = {
            "booking": self.booking, "assignment": self.assignment,
            "debit": self.booking.ledger_entry, "refund": self.assignment.refund_ledger_entry,
        }
        for owner, name, invalid_value in mutations:
            with self.subTest(owner=owner, field=name):
                with patch.object(records[owner], name, invalid_value):
                    with self.assertRaisesRegex(ValueError, "cannot be safely reversed"):
                        self.cancel()
                    self.last_spend.assert_not_called()
                self.assignment.save.assert_not_called()
                self.booking.save.assert_not_called()
                self.day.save.assert_not_called()

    def test_missing_assignment_or_day_is_not_silently_cancelled(self):
        for options in ({"assignment_present": False}, {"day_present": False}):
            with self.subTest(options=options):
                with self.assertRaisesRegex(ValueError, "assignment is unavailable"):
                    self.cancel(**options)
        self.assignment.save.assert_not_called()
        self.booking.save.assert_not_called()

    def test_identity_and_closed_day_mismatches_fail_before_spending(self):
        for owner, field, value in (
            (self.assignment, "day_id", 99),
            (self.assignment, "user_id", 99),
            (self.day, "date", date(2026, 9, 8)),
            (self.day, "status", "closed"),
        ):
            with self.subTest(field=field):
                with patch.object(owner, field, value):
                    with self.assertRaisesRegex(ValueError, "does not match"):
                        self.cancel()
        self.assignment.save.assert_not_called()
        self.booking.save.assert_not_called()

    def test_spent_refund_rejects_cancellation_without_closing_assignment(self):
        with self.assertRaisesRegex(ValueError, "previously refunded Roo points"):
            self.cancel(spend_error=InsufficientBalanceError("insufficient points"))
        self.assignment.save.assert_not_called()
        self.booking.save.assert_not_called()
        self.day.save.assert_not_called()
        self.assertEqual(self.assignment.status, "active")


class OfficeManagerRetirementBoundaryTests(SimpleTestCase):
    def test_claim_and_preflight_routes_are_removed(self):
        for operation in ("claim", "preflight"):
            with self.subTest(operation=operation):
                with self.assertRaises(Resolver404):
                    resolve(f"/api/v1/points/coworking/office-manager/{operation}/")
        self.assertEqual(
            resolve("/api/v1/points/coworking/cancel/").url_name, "coworking-cancel"
        )

    def test_retained_admin_records_are_view_only(self):
        for admin_type, model in (
            (OfficeManagerDayAdmin, OfficeManagerDay),
            (OfficeManagerAssignmentAdmin, OfficeManagerAssignment),
        ):
            with self.subTest(model=model.__name__):
                admin = admin_type(model, AdminSite())
                self.assertFalse(admin.has_add_permission(None))
                self.assertFalse(admin.has_change_permission(None))
                self.assertFalse(admin.has_delete_permission(None))
                self.assertIsNone(admin.actions)

    @override_settings(ROO_API_KEY="roo-synthetic", INTERNAL_API_KEY="internal-synthetic", MLAI_API_KEY="mlai-synthetic")
    def test_legacy_mutation_rejects_broader_service_credentials(self):
        permission = HasIsolatedRooApiKey()
        for credential in (None, "internal-synthetic", "mlai-synthetic"):
            with self.subTest(credential=credential):
                request = SimpleNamespace(META={"HTTP_X_API_KEY": credential})
                self.assertFalse(permission.has_permission(request, None))
        request = SimpleNamespace(META={"HTTP_X_API_KEY": "roo-synthetic"})
        self.assertTrue(permission.has_permission(request, None))

    @override_settings(ROO_API_KEY="shared-synthetic", INTERNAL_API_KEY="shared-synthetic", MLAI_API_KEY="")
    def test_legacy_mutation_rejects_aliased_roo_credential(self):
        request = SimpleNamespace(META={"HTTP_X_API_KEY": "shared-synthetic"})
        self.assertFalse(HasIsolatedRooApiKey().has_permission(request, None))
