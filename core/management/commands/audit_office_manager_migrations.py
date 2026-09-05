import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


REPORT_VERSION = 1
MIGRATION_PREFIXES = (
    "0029",
    "0030",
    "0031",
    "0032",
    "0034",
    "0035",
    "0036",
    "0037",
    "0038",
    "0039",
)
CANONICAL_IDENTITIES = {
    "boost": "0029_boostpostadmission_and_more",
    "microroo": "0030_microroo_and_coding_billing",
    "meeting_room": "0031_meeting_room_booking",
    "office_manager": (
        "0034_officemanagerday_coworkingbooking_booking_source_and_more"
    ),
    "office_manager_protect": "0035_protect_office_manager_assignment_day",
    "office_manager_successor": "0036_office_manager_attempts_and_provenance",
    "office_manager_recovery": (
        "0037_quarantine_legacy_office_manager_provenance"
    ),
    "office_manager_hardening": "0038_office_manager_claim_generation",
    "office_manager_attempt_repair": (
        "0039_supersede_reopened_office_manager_attempts"
    ),
}
REVIEWED_0036_IDENTITIES = frozenset(
    {
        CANONICAL_IDENTITIES["office_manager_successor"],
        "0036_sanitize_coworking_operation_receipts",
    }
)


def _uuid_text(value) -> str:
    """Return stable hyphenated UUID text across SQLite and PostgreSQL."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return str(value)


HISTORICAL_IDENTITIES = {
    "0029_officemanagerday_coworkingbooking_booking_source_and_more": (
        "0034_officemanagerday_coworkingbooking_booking_source_and_more"
    ),
    "0030_officemanagerday_coworkingbooking_booking_source_and_more": (
        "0034_officemanagerday_coworkingbooking_booking_source_and_more"
    ),
    "0031_protect_office_manager_assignment_day": (
        "0035_protect_office_manager_assignment_day"
    ),
    "0030_meeting_room_booking": "0031_meeting_room_booking",
    "0031_small_and_big_meeting_rooms": "0032_small_and_big_meeting_rooms",
}
REQUIRED_SCHEMA = {
    "boost": {
        "roo_boostpostadmission": {"id", "submission_key"},
    },
    "microroo": {
        "roo_pointsaccount": {
            "balance_microroo",
            "earned_balance_microroo",
            "purchased_topup_balance_microroo",
            "microroo_initialized",
        },
        "roo_ledger": {"delta_microroo", "points_delta_microroo"},
        "roo_codingpricingversion": {"id", "version"},
        "roo_codingturn": {"id", "idempotency_key"},
        "roo_codingmodelcall": {"id", "call_id"},
    },
    "meeting_room": {
        "roo_meetingroom": {"id", "slug"},
        "roo_meetingroomblock": {"id", "room_id", "starts_at", "ends_at"},
        "roo_meetingroombooking": {
            "id",
            "room_id",
            "user_id",
            "purchased_points_cost",
        },
    },
    "office_manager": {
        "roo_coworkingbooking": {"booking_source", "original_points_cost"},
        "roo_officemanagerday": {
            "id",
            "date",
            "announcement_status",
            "announcement_attempt_count",
        },
        "roo_officemanagerassignment": {
            "id",
            "day_id",
            "booking_id",
            "user_id",
            "winner_channel_message_ts",
            "winner_channel_retraction_pending",
            "refund_reversal_ledger_entry_id",
        },
    },
    "office_manager_successor": {
        "roo_coworkingbooking": {"purchased_points_cost_microroo"},
        "roo_officemanagerassignment": {
            "purchased_points_refunded_microroo",
            "winner_channel_retraction_attempt_count",
            "winner_channel_retraction_lease_token",
            "winner_channel_retraction_next_attempt_at",
            "winner_channel_retraction_status",
        },
        "roo_officemanagerclaimattempt": {
            "attempt_id",
            "slack_user_id",
            "booking_date",
            "outcome",
            "assignment_id",
        },
    },
    "office_manager_recovery": {
        "roo_officemanagerassignment": {
            "purchased_points_refunded_microroo",
        },
        "roo_officemanagerprovenancereconciliation": {
            "id",
            "booking_id",
            "debit_ledger_id",
            "purchased_microroo",
            "reviewed_by",
            "assignment_refund_snapshot",
            "created_at",
        },
    },
    "office_manager_hardening": {
        "roo_officemanagerday": {"generation"},
        "roo_officemanagerclaimattempt": {"generation"},
        "roo_officemanagerassignment": {
            "winner_dm_message_ts",
            "end_of_day_reminder_message_ts",
            "private_correction_pending",
            "private_correction_status",
            "private_correction_sent_at",
            "private_correction_last_error",
            "private_correction_attempt_count",
            "private_correction_next_attempt_at",
        },
        "roo_officemanagerprovenancebucketrepair": {
            "id",
            "reconciliation_id",
            "ledger_id",
            "purchased_microroo",
            "account_before",
            "created_at",
        },
        "roo_officemanagerrefundreversalprovenance": {
            "id",
            "assignment_id",
            "reversal_ledger_id",
            "purchased_microroo",
            "reviewed_by",
            "created_at",
        },
        "roo_scheduleddiscoveryheartbeat": {
            "name",
            "last_started_at",
            "last_succeeded_at",
            "last_failed_at",
            "last_error",
            "updated_at",
        },
    },
}


def _applied_migration_names() -> list[str]:
    table_names = set(connection.introspection.table_names())
    if "django_migrations" not in table_names:
        return []
    placeholders = " OR ".join(["name LIKE %s"] * len(MIGRATION_PREFIXES))
    params = [f"{prefix}%" for prefix in MIGRATION_PREFIXES]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT name FROM django_migrations "
            f"WHERE app = %s AND ({placeholders}) ORDER BY name",
            ["roo", *params],
        )
        return [str(row[0]) for row in cursor.fetchall()]


def _schema_snapshot() -> dict[str, list[str]]:
    table_names = set(connection.introspection.table_names())
    relevant_tables = {
        table_name
        for requirements in REQUIRED_SCHEMA.values()
        for table_name in requirements
    }
    snapshot = {}
    with connection.cursor() as cursor:
        for table_name in sorted(relevant_tables & table_names):
            snapshot[table_name] = sorted(
                str(column.name)
                for column in connection.introspection.get_table_description(
                    cursor,
                    table_name,
                )
            )
    return snapshot


def _schema_group_report(snapshot: dict[str, list[str]]) -> dict[str, dict]:
    report = {}
    for group, requirements in REQUIRED_SCHEMA.items():
        missing = {}
        present_artifact_count = 0
        present_required_column_count = 0
        for table_name, required_columns in requirements.items():
            actual_columns = set(snapshot.get(table_name, []))
            if actual_columns:
                present_artifact_count += 1
            present_required_column_count += len(required_columns & actual_columns)
            missing_columns = sorted(required_columns - actual_columns)
            if missing_columns:
                missing[table_name] = missing_columns
        report[group] = {
            "complete": not missing,
            "missing": missing,
            "present_artifact_count": present_artifact_count,
            "present_required_column_count": present_required_column_count,
            "required_artifact_count": len(requirements),
        }
    return report


def _office_manager_recovery_schema_issues() -> list[str]:
    """Verify 0037's nullability and relational constraints, not just names."""
    table_names = set(connection.introspection.table_names())
    assignment_table = "roo_officemanagerassignment"
    reconciliation_table = "roo_officemanagerprovenancereconciliation"
    if not {assignment_table, reconciliation_table}.issubset(table_names):
        return []

    issues: list[str] = []
    with connection.cursor() as cursor:
        assignment_columns = {
            str(column.name): column
            for column in connection.introspection.get_table_description(
                cursor,
                assignment_table,
            )
        }
        provenance = assignment_columns.get(
            "purchased_points_refunded_microroo"
        )
        if provenance is not None and not bool(provenance.null_ok):
            issues.append(
                "roo_officemanagerassignment."
                "purchased_points_refunded_microroo must remain nullable"
            )

        constraints = connection.introspection.get_constraints(
            cursor,
            reconciliation_table,
        )
    unique_booking = any(
        constraint.get("unique")
        and constraint.get("columns") == ["booking_id"]
        for constraint in constraints.values()
    )
    booking_fk = any(
        constraint.get("foreign_key")
        == ("roo_coworkingbooking", "id")
        and constraint.get("columns") == ["booking_id"]
        for constraint in constraints.values()
    )
    ledger_fk = any(
        constraint.get("foreign_key") == ("roo_ledger", "id")
        and constraint.get("columns") == ["debit_ledger_id"]
        for constraint in constraints.values()
    )
    if not unique_booking:
        issues.append(
            "roo_officemanagerprovenancereconciliation.booking_id must be unique"
        )
    if not booking_fk:
        issues.append(
            "roo_officemanagerprovenancereconciliation.booking_id foreign key is missing"
        )
    if not ledger_fk:
        issues.append(
            "roo_officemanagerprovenancereconciliation.debit_ledger_id foreign key is missing"
        )
    return issues


def _office_manager_hardening_schema_issues() -> list[str]:
    """Verify 0038's immutable repair evidence relationships and uniqueness."""
    table_names = set(connection.introspection.table_names())
    table_name = "roo_officemanagerprovenancebucketrepair"
    reversal_table = "roo_officemanagerrefundreversalprovenance"
    if table_name not in table_names:
        return []

    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(
            cursor,
            table_name,
        )

    relationships = (
        (
            "reconciliation_id",
            "roo_officemanagerprovenancereconciliation",
            "reconciliation",
        ),
        ("ledger_id", "roo_ledger", "ledger"),
    )
    issues: list[str] = []
    for column, foreign_table, label in relationships:
        is_unique = any(
            constraint.get("unique")
            and constraint.get("columns") == [column]
            for constraint in constraints.values()
        )
        has_foreign_key = any(
            constraint.get("foreign_key") == (foreign_table, "id")
            and constraint.get("columns") == [column]
            for constraint in constraints.values()
        )
        if not is_unique:
            issues.append(
                "roo_officemanagerprovenancebucketrepair."
                f"{column} must be unique"
            )
        if not has_foreign_key:
            issues.append(
                "roo_officemanagerprovenancebucketrepair."
                f"{label} foreign key is missing"
            )
    if reversal_table in table_names:
        with connection.cursor() as cursor:
            reversal_constraints = connection.introspection.get_constraints(
                cursor,
                reversal_table,
            )
        for column, foreign_table in (
            ("assignment_id", "roo_officemanagerassignment"),
            ("reversal_ledger_id", "roo_ledger"),
        ):
            if not any(
                constraint.get("unique")
                and constraint.get("columns") == [column]
                for constraint in reversal_constraints.values()
            ):
                issues.append(
                    f"{reversal_table}.{column} must be unique"
                )
            if not any(
                constraint.get("foreign_key") == (foreign_table, "id")
                and constraint.get("columns") == [column]
                for constraint in reversal_constraints.values()
            ):
                issues.append(
                    f"{reversal_table}.{column} foreign key is missing"
                )
    return issues


def _disk_0036_identities() -> list[str]:
    migration_dir = Path(settings.BASE_DIR) / "roo" / "migrations"
    return sorted(path.stem for path in migration_dir.glob("0036_*.py"))


def _disk_0037_identities() -> list[str]:
    migration_dir = Path(settings.BASE_DIR) / "roo" / "migrations"
    return sorted(path.stem for path in migration_dir.glob("0037_*.py"))


def _disk_0038_identities() -> list[str]:
    migration_dir = Path(settings.BASE_DIR) / "roo" / "migrations"
    return sorted(path.stem for path in migration_dir.glob("0038_*.py"))


def _disk_0039_identities() -> list[str]:
    migration_dir = Path(settings.BASE_DIR) / "roo" / "migrations"
    return sorted(path.stem for path in migration_dir.glob("0039_*.py"))


def _office_manager_data_invariants() -> dict:
    """Read cross-table invariants that database constraints cannot express."""
    table_names = set(connection.introspection.table_names())
    required = {
        "roo_coworkingbooking",
        "roo_ledger",
        "roo_officemanagerday",
        "roo_officemanagerassignment",
    }
    if not required.issubset(table_names):
        return {
            "checked": False,
            "claimed_days_without_exactly_one_active_assignment": [],
            "active_assignments_on_non_claimed_days": [],
            "office_manager_bookings_without_assignment": [],
            "assignment_booking_identity_mismatches": [],
            "unreconciled_paid_bookings": [],
            "unreconciled_office_manager_refunds": [],
            "invalid_office_manager_debit_ledgers": [],
            "invalid_office_manager_refund_ledgers": [],
            "invalid_office_manager_reversal_ledgers": [],
            "unattested_reversed_office_manager_refunds": [],
            "unrepaired_office_manager_refund_buckets": [],
            "pending_office_manager_delivery_channels": [],
            "stale_reopened_office_manager_attempts": [],
        }

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT d.id, d.date, COUNT(a.id)
            FROM roo_officemanagerday d
            LEFT JOIN roo_officemanagerassignment a
              ON a.day_id = d.id AND a.status = %s
            WHERE d.status = %s
            GROUP BY d.id, d.date
            HAVING COUNT(a.id) <> 1
            ORDER BY d.date, d.id
            """,
            ["active", "claimed"],
        )
        claimed_mismatches = [
            {
                "day_id": int(day_id),
                "date": (
                    booking_date.isoformat()
                    if hasattr(booking_date, "isoformat")
                    else str(booking_date)
                ),
                "active_assignment_count": int(active_count),
            }
            for day_id, booking_date, active_count in cursor.fetchall()
        ]
        cursor.execute(
            """
            SELECT a.id, a.day_id, d.date, d.status
            FROM roo_officemanagerassignment a
            INNER JOIN roo_officemanagerday d ON d.id = a.day_id
            WHERE a.status = %s AND d.status <> %s
            ORDER BY d.date, a.id
            """,
            ["active", "claimed"],
        )
        inverse_mismatches = [
            {
                "assignment_id": int(assignment_id),
                "day_id": int(day_id),
                "date": (
                    booking_date.isoformat()
                    if hasattr(booking_date, "isoformat")
                    else str(booking_date)
                ),
                "day_status": str(day_status),
            }
            for assignment_id, day_id, booking_date, day_status in cursor.fetchall()
        ]
        cursor.execute(
            """
            SELECT b.id, b.user_id, b.date, b.status
            FROM roo_coworkingbooking b
            LEFT JOIN roo_officemanagerassignment a ON a.booking_id = b.id
            WHERE b.booking_source = %s AND a.id IS NULL
            ORDER BY b.date, b.id
            """,
            ["office_manager"],
        )
        orphan_bookings = [
            {
                "booking_id": _uuid_text(booking_id),
                "user_id": str(user_id),
                "date": booking_date.isoformat(),
                "status": str(booking_status),
            }
            for booking_id, user_id, booking_date, booking_status
            in cursor.fetchall()
        ]
        cursor.execute(
            """
            SELECT a.id, a.user_id, b.user_id, d.date, b.date
            FROM roo_officemanagerassignment a
            INNER JOIN roo_officemanagerday d ON d.id = a.day_id
            INNER JOIN roo_coworkingbooking b ON b.id = a.booking_id
            WHERE a.user_id <> b.user_id OR d.date <> b.date
            ORDER BY d.date, a.id
            """
        )
        identity_mismatches = [
            {
                "assignment_id": int(assignment_id),
                "assignment_user_id": str(assignment_user_id),
                "booking_user_id": str(booking_user_id),
                "day_date": day_date.isoformat(),
                "booking_date": booking_date.isoformat(),
            }
            for (
                assignment_id,
                assignment_user_id,
                booking_user_id,
                day_date,
                booking_date,
            ) in cursor.fetchall()
        ]
        booking_columns = {
            str(column.name)
            for column in connection.introspection.get_table_description(
                cursor, "roo_coworkingbooking"
            )
        }
        assignment_columns = {
            str(column.name)
            for column in connection.introspection.get_table_description(
                cursor, "roo_officemanagerassignment"
            )
        }
        unreconciled_paid_bookings = []
        unreconciled_refunds = []
        if "purchased_points_cost_microroo" in booking_columns:
            local_date = datetime.now(
                ZoneInfo("Australia/Melbourne")
            ).date()
            cursor.execute(
                """
                SELECT b.id, b.user_id, b.date, b.points_cost
                FROM roo_coworkingbooking b
                WHERE b.status = %s
                  AND b.booking_source = %s
                  AND b.points_cost > 0
                  AND b.date >= %s
                  AND b.purchased_points_cost_microroo IS NULL
                ORDER BY b.date, b.id
                """,
                ["booked", "points", local_date],
            )
            unreconciled_paid_bookings = [
                {
                    "booking_id": _uuid_text(booking_id),
                    "user_id": str(user_id),
                    "date": booking_date.isoformat(),
                    "points_cost": int(points_cost),
                }
                for booking_id, user_id, booking_date, points_cost
                in cursor.fetchall()
            ]
        if (
            "purchased_points_refunded_microroo" in assignment_columns
            and "purchased_points_cost_microroo" in booking_columns
        ):
            cursor.execute(
                """
                SELECT a.id, a.booking_id, a.points_refunded,
                       a.purchased_points_refunded_microroo,
                       b.purchased_points_cost_microroo
                FROM roo_officemanagerassignment a
                INNER JOIN roo_coworkingbooking b ON b.id = a.booking_id
                WHERE a.points_refunded > 0 AND (
                    a.purchased_points_refunded_microroo IS NULL
                    OR b.purchased_points_cost_microroo IS NULL
                    OR a.purchased_points_refunded_microroo
                       <> b.purchased_points_cost_microroo
                    OR b.original_points_cost <> a.points_refunded
                )
                ORDER BY a.id
                """
            )
            unreconciled_refunds = [
                {
                    "assignment_id": int(assignment_id),
                    "booking_id": _uuid_text(booking_id),
                    "points_refunded": int(points_refunded),
                    "assignment_purchased_microroo": assignment_purchased,
                    "booking_purchased_microroo": booking_purchased,
                }
                for (
                    assignment_id,
                    booking_id,
                    points_refunded,
                    assignment_purchased,
                    booking_purchased,
                )
                in cursor.fetchall()
            ]
        ledger_columns = {
            str(column.name)
            for column in connection.introspection.get_table_description(
                cursor, "roo_ledger"
            )
        }
        required_ledger_columns = {
            "id",
            "user_id",
            "kind",
            "source",
            "delta_microroo",
            "reference_type",
            "reference_id",
        }
        if required_ledger_columns.issubset(ledger_columns):
            cursor.execute(
                """
                SELECT a.id, a.booking_id, a.points_refunded,
                       b.original_points_cost, b.date, b.ledger_entry_id,
                       l.id, l.user_id, b.user_id, l.kind, l.source,
                       l.delta_microroo, l.reference_type, l.reference_id
                FROM roo_officemanagerassignment a
                INNER JOIN roo_coworkingbooking b ON b.id = a.booking_id
                LEFT JOIN roo_ledger l ON l.id = b.ledger_entry_id
                WHERE a.points_refunded > 0
                ORDER BY a.id
                """
            )
            invalid_debit_ledgers = []
            for row in cursor.fetchall():
                (
                    assignment_id,
                    booking_id,
                    points_refunded,
                    original_points_cost,
                    booking_date,
                    booking_ledger_entry_id,
                    ledger_id,
                    ledger_user_id,
                    booking_user_id,
                    kind,
                    source,
                    delta_microroo,
                    reference_type,
                    reference_id,
                ) = row
                reference_matches = (
                    str(reference_id or "") == booking_date.isoformat()
                    or _uuid_text(reference_id) == _uuid_text(booking_id)
                )
                if (
                    int(original_points_cost or 0) != int(points_refunded)
                    or booking_ledger_entry_id is None
                    or ledger_id != booking_ledger_entry_id
                    or ledger_user_id != booking_user_id
                    or kind != "SPEND"
                    or source != "COWORKING"
                    or delta_microroo != -(int(points_refunded) * 1_000_000)
                    or reference_type != "COWORKING_BOOKING"
                    or not reference_matches
                ):
                    invalid_debit_ledgers.append(
                        {
                            "assignment_id": int(assignment_id),
                            "booking_id": _uuid_text(booking_id),
                            "debit_ledger_entry_id": (
                                int(booking_ledger_entry_id)
                                if booking_ledger_entry_id is not None
                                else None
                            ),
                        }
                    )
            cursor.execute(
                """
                SELECT a.id, a.booking_id, a.refund_ledger_entry_id
                FROM roo_officemanagerassignment a
                LEFT JOIN roo_ledger l ON l.id = a.refund_ledger_entry_id
                WHERE a.points_refunded > 0 AND (
                    l.id IS NULL
                    OR l.user_id <> a.user_id
                    OR l.kind <> %s
                    OR l.source <> %s
                    OR l.delta_microroo <> (a.points_refunded * 1000000)
                    OR l.reference_type <> %s
                    OR l.reference_id <> CAST(a.day_id AS TEXT)
                )
                ORDER BY a.id
                """,
                ["REFUND", "COWORKING", "OFFICE_MANAGER_ASSIGNMENT"],
            )
            invalid_refund_ledgers = [
                {
                    "assignment_id": int(assignment_id),
                    "booking_id": _uuid_text(booking_id),
                    "refund_ledger_entry_id": (
                        int(ledger_id) if ledger_id is not None else None
                    ),
                }
                for assignment_id, booking_id, ledger_id in cursor.fetchall()
            ]
            cursor.execute(
                """
                SELECT a.id, a.booking_id, a.refund_reversal_ledger_entry_id
                FROM roo_officemanagerassignment a
                LEFT JOIN roo_ledger l
                  ON l.id = a.refund_reversal_ledger_entry_id
                WHERE a.refund_reversal_ledger_entry_id IS NOT NULL AND (
                    l.id IS NULL
                    OR l.user_id <> a.user_id
                    OR l.kind <> %s
                    OR l.source <> %s
                    OR l.delta_microroo <> -(a.points_refunded * 1000000)
                    OR l.reference_type <> %s
                    OR l.reference_id <> CAST(a.id AS TEXT)
                )
                ORDER BY a.id
                """,
                ["SPEND", "COWORKING", "OFFICE_MANAGER_REFUND_REVERSAL"],
            )
            invalid_reversal_ledgers = [
                {
                    "assignment_id": int(assignment_id),
                    "booking_id": _uuid_text(booking_id),
                    "refund_reversal_ledger_entry_id": (
                        int(ledger_id) if ledger_id is not None else None
                    ),
                }
                for assignment_id, booking_id, ledger_id in cursor.fetchall()
            ]
        else:
            invalid_debit_ledgers = [{
                "reason": "required ledger columns are unavailable",
                "missing_columns": sorted(
                    required_ledger_columns - ledger_columns
                ),
            }]
            invalid_refund_ledgers = [{
                "reason": "required ledger columns are unavailable",
                "missing_columns": sorted(
                    required_ledger_columns - ledger_columns
                ),
            }]
            invalid_reversal_ledgers = [{
                "reason": "required ledger columns are unavailable",
                "missing_columns": sorted(
                    required_ledger_columns - ledger_columns
                ),
            }]
        repair_table = "roo_officemanagerprovenancebucketrepair"
        reconciliation_table = "roo_officemanagerprovenancereconciliation"
        reversal_evidence_table = (
            "roo_officemanagerrefundreversalprovenance"
        )
        if {
            repair_table,
            reconciliation_table,
            reversal_evidence_table,
        }.issubset(table_names):
            cursor.execute(
                """
                SELECT a.id, a.booking_id, a.refund_reversal_ledger_entry_id
                FROM roo_officemanagerassignment a
                LEFT JOIN roo_officemanagerprovenancereconciliation r
                  ON r.booking_id = a.booking_id
                LEFT JOIN roo_officemanagerrefundreversalprovenance p
                  ON p.assignment_id = a.id
                WHERE a.refund_reversal_ledger_entry_id IS NOT NULL
                  AND (
                    r.id IS NULL
                    OR p.id IS NULL
                    OR p.reversal_ledger_id
                       <> a.refund_reversal_ledger_entry_id
                    OR p.purchased_microroo
                       > (a.points_refunded * 1000000)
                  )
                ORDER BY a.id
                """
            )
            unattested_reversed_refunds = [
                {
                    "assignment_id": int(assignment_id),
                    "booking_id": _uuid_text(booking_id),
                    "refund_reversal_ledger_entry_id": int(reversal_id),
                }
                for assignment_id, booking_id, reversal_id in cursor.fetchall()
            ]
            cursor.execute(
                """
                SELECT r.id, r.booking_id, r.purchased_microroo,
                       r.assignment_refund_snapshot,
                       p.id, p.purchased_microroo, l.id, l.user_id, b.user_id,
                       l.kind, l.source, l.delta_microroo,
                       l.reference_type, l.reference_id
                FROM roo_officemanagerprovenancereconciliation r
                INNER JOIN roo_coworkingbooking b ON b.id = r.booking_id
                LEFT JOIN roo_officemanagerprovenancebucketrepair p
                  ON p.reconciliation_id = r.id
                LEFT JOIN roo_ledger l ON l.id = p.ledger_id
                ORDER BY r.id
                """
            )
            unrepaired_refund_buckets = []
            reconciliation_rows = cursor.fetchall()
            for row in reconciliation_rows:
                (
                    reconciliation_id,
                    booking_id,
                    purchased_microroo,
                    refund_snapshot,
                    repair_id,
                    repair_purchased_microroo,
                    ledger_id,
                    ledger_user_id,
                    booking_user_id,
                    kind,
                    source,
                    delta_microroo,
                    reference_type,
                    reference_id,
                ) = row
                if isinstance(refund_snapshot, str):
                    try:
                        refund_snapshot = json.loads(refund_snapshot)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        refund_snapshot = None
                cursor.execute(
                    """
                    SELECT a.id, a.refund_ledger_entry_id,
                           a.points_refunded,
                           a.refund_reversal_ledger_entry_id,
                           p.purchased_microroo
                    FROM roo_officemanagerassignment a
                    LEFT JOIN roo_officemanagerrefundreversalprovenance p
                      ON p.assignment_id = a.id
                    WHERE a.booking_id = %s AND a.points_refunded > 0
                    ORDER BY a.day_id, a.id
                    """,
                    [booking_id],
                )
                assignment_rows = cursor.fetchall()
                expected_snapshot = [
                    {
                        "assignment_id": int(assignment_id),
                        "refund_ledger_id": (
                            int(refund_ledger_id)
                            if refund_ledger_id is not None
                            else None
                        ),
                        "refund_microroo": int(points_refunded) * 1_000_000,
                    }
                    for (
                        assignment_id,
                        refund_ledger_id,
                        points_refunded,
                        _reversal_ledger_id,
                        _reversal_purchased,
                    ) in assignment_rows
                ]
                snapshot_valid = refund_snapshot == expected_snapshot
                expected_repair = 0
                for (
                    _assignment_id,
                    _refund_ledger_id,
                    _points_refunded,
                    reversal_ledger_id,
                    reversal_purchased,
                ) in assignment_rows:
                    if reversal_ledger_id is None:
                        expected_repair += int(purchased_microroo)
                    elif reversal_purchased is None:
                        snapshot_valid = False
                    else:
                        expected_repair += int(reversal_purchased)
                repair_is_valid = False
                if expected_repair == 0:
                    repair_is_valid = repair_id is None
                elif repair_id is not None:
                    repair_is_valid = (
                        repair_purchased_microroo == expected_repair
                        and ledger_id is not None
                        and ledger_user_id == booking_user_id
                        and kind == "ADJUST"
                        and source == "COWORKING"
                        and delta_microroo == 0
                        and reference_type == "OFFICE_MANAGER_BUCKET_REPAIR"
                        and _uuid_text(reference_id) == _uuid_text(booking_id)
                    )
                if not snapshot_valid or not repair_is_valid:
                    unrepaired_refund_buckets.append(
                        {
                            "reconciliation_id": int(reconciliation_id),
                            "booking_id": _uuid_text(booking_id),
                            "purchased_microroo": int(purchased_microroo),
                            "expected_repair_microroo": (
                                expected_repair if snapshot_valid else None
                            ),
                        }
                    )
        else:
            unrepaired_refund_buckets = []
            unattested_reversed_refunds = []

        day_pending_clauses = [
            "d.announcement_status IN ('pending', 'sending', 'unknown', 'failed')",
            "d.message_update_pending = %s",
        ]
        assignment_pending_clauses = [
            "a.winner_channel_announcement_status IN "
            "('pending', 'sending', 'unknown', 'failed')",
            "a.winner_channel_retraction_pending = %s",
        ]
        cursor.execute(
            f"""
            SELECT DISTINCT d.slack_channel_id
            FROM roo_officemanagerday d
            LEFT JOIN roo_officemanagerassignment a ON a.day_id = d.id
            WHERE ({' OR '.join(day_pending_clauses)})
               OR ({' OR '.join(assignment_pending_clauses)})
            ORDER BY d.slack_channel_id
            """,
            [True, True],
        )
        pending_delivery_channels = [
            str(channel_id)
            for (channel_id,) in cursor.fetchall()
            if str(channel_id or "").strip()
        ]
        if "roo_officemanagerclaimattempt" in table_names:
            attempt_columns = {
                str(column.name)
                for column in connection.introspection.get_table_description(
                    cursor,
                    "roo_officemanagerclaimattempt",
                )
            }
        else:
            attempt_columns = set()
        day_columns = {
            str(column.name)
            for column in connection.introspection.get_table_description(
                cursor,
                "roo_officemanagerday",
            )
        }
        if {"attempt_id", "booking_date", "generation", "outcome"}.issubset(
            attempt_columns
        ) and {"date", "generation", "status"}.issubset(day_columns):
            cursor.execute(
                """
                SELECT DISTINCT ca.attempt_id, ca.booking_date,
                                ca.generation, d.generation
                FROM roo_officemanagerclaimattempt ca
                INNER JOIN roo_officemanagerday d
                  ON d.date = ca.booking_date
                WHERE ca.generation < d.generation
                  AND ca.outcome <> %s
                  AND EXISTS (
                    SELECT 1
                    FROM roo_officemanagerassignment a
                    WHERE a.day_id = d.id AND a.status = %s
                  )
                ORDER BY ca.booking_date, ca.attempt_id
                """,
                ["attempt_superseded", "relinquished"],
            )
            stale_reopened_attempts = [
                {
                    "attempt_id": _uuid_text(attempt_id),
                    "date": (
                        booking_date.isoformat()
                        if hasattr(booking_date, "isoformat")
                        else str(booking_date)
                    ),
                    "attempt_generation": int(attempt_generation),
                    "day_generation": int(day_generation),
                }
                for (
                    attempt_id,
                    booking_date,
                    attempt_generation,
                    day_generation,
                ) in cursor.fetchall()
            ]
        else:
            stale_reopened_attempts = []
    return {
        "checked": True,
        "claimed_days_without_exactly_one_active_assignment": claimed_mismatches,
        "active_assignments_on_non_claimed_days": inverse_mismatches,
        "office_manager_bookings_without_assignment": orphan_bookings,
        "assignment_booking_identity_mismatches": identity_mismatches,
        "unreconciled_paid_bookings": unreconciled_paid_bookings,
        "unreconciled_office_manager_refunds": unreconciled_refunds,
        "invalid_office_manager_debit_ledgers": invalid_debit_ledgers,
        "invalid_office_manager_refund_ledgers": invalid_refund_ledgers,
        "invalid_office_manager_reversal_ledgers": invalid_reversal_ledgers,
        "unattested_reversed_office_manager_refunds": (
            unattested_reversed_refunds
        ),
        "unrepaired_office_manager_refund_buckets": (
            unrepaired_refund_buckets
        ),
        "pending_office_manager_delivery_channels": pending_delivery_channels,
        "stale_reopened_office_manager_attempts": stale_reopened_attempts,
    }


def _build_report(*, configured_office_manager_channel: str = "") -> dict:
    applied = _applied_migration_names()
    applied_set = set(applied)
    schema = _schema_snapshot()
    schema_groups = _schema_group_report(schema)
    data_invariants = _office_manager_data_invariants()
    disk_0036 = _disk_0036_identities()
    disk_0037 = _disk_0037_identities()
    disk_0038 = _disk_0038_identities()
    disk_0039 = _disk_0039_identities()
    issues = []
    histories = []

    for historical, replacement in sorted(HISTORICAL_IDENTITIES.items()):
        historical_applied = historical in applied_set
        replacement_applied = replacement in applied_set
        compatibility = "not_applied"
        if historical_applied:
            if not replacement_applied:
                compatibility = "unsafe_replacement_not_recorded"
                issues.append(
                    f"historical migration {historical} is recorded but "
                    f"replacement {replacement} is not"
                )
            else:
                compatibility = "requires_attestation"
        histories.append(
            {
                "identity": historical,
                "recorded": historical_applied,
                "replacement": replacement,
                "replacement_recorded": replacement_applied,
                "compatibility": compatibility,
            }
        )

    for group, identity in CANONICAL_IDENTITIES.items():
        if group in {
            "office_manager_protect",
            "office_manager_attempt_repair",
        }:
            continue
        marker = schema_groups[group]
        if identity in applied_set and not marker["complete"]:
            issues.append(
                f"canonical migration {identity} is recorded but its required "
                "schema markers are incomplete"
            )
        if (
            identity not in applied_set
            and marker["present_required_column_count"]
            and group != "office_manager"
        ):
            issues.append(
                f"schema artifacts for {group} exist without canonical migration "
                f"{identity}"
            )

    office_manager_identity = CANONICAL_IDENTITIES["office_manager"]
    office_manager_markers = schema_groups["office_manager"]
    if (
        office_manager_identity not in applied_set
        and office_manager_markers["present_required_column_count"]
    ):
        issues.append(
            "Office Manager schema artifacts exist before canonical migration "
            f"{office_manager_identity}; applying it would not be append-only safe"
        )

    protect_identity = CANONICAL_IDENTITIES["office_manager_protect"]
    if protect_identity in applied_set and office_manager_identity not in applied_set:
        issues.append(
            f"{protect_identity} is recorded without {office_manager_identity}"
        )

    applied_0036 = [name for name in applied if name.startswith("0036_")]
    disk_0036_set = set(disk_0036)
    applied_0036_set = set(applied_0036)
    if disk_0036_set != REVIEWED_0036_IDENTITIES:
        issues.append(
            "the reviewed source tree must contain exactly the approved "
            "append-only roo.0036 migration identities"
        )
    if not applied_0036_set.issubset(REVIEWED_0036_IDENTITIES):
        issues.append(
            "an unapproved roo.0036 migration identity is recorded"
        )
    if not applied_0036_set.issubset(disk_0036_set):
        issues.append(
            "recorded roo.0036 identity does not exist in the reviewed source tree"
        )
    office_manager_successor = CANONICAL_IDENTITIES["office_manager_successor"]
    if (
        office_manager_successor in applied_0036_set
        and protect_identity not in applied_set
    ):
        issues.append(
            f"{office_manager_successor} is recorded without {protect_identity}"
        )

    recovery_identity = CANONICAL_IDENTITIES["office_manager_recovery"]
    applied_0037 = [name for name in applied if name.startswith("0037_")]
    if disk_0037 != [recovery_identity]:
        issues.append(
            "the reviewed source tree must contain exactly the canonical "
            "append-only roo.0037 recovery migration"
        )
    if len(applied_0037) > 1:
        issues.append("multiple roo.0037 migration identities are recorded")
    if applied_0037 and applied_0037 != disk_0037:
        issues.append(
            "recorded roo.0037 identity does not match the reviewed source tree"
        )
    if recovery_identity in applied_set and CANONICAL_IDENTITIES[
        "office_manager_successor"
    ] not in applied_set:
        issues.append(
            f"{recovery_identity} is recorded without "
            f"{CANONICAL_IDENTITIES['office_manager_successor']}"
        )
    if recovery_identity in applied_set:
        issues.extend(_office_manager_recovery_schema_issues())

    hardening_identity = CANONICAL_IDENTITIES["office_manager_hardening"]
    applied_0038 = [name for name in applied if name.startswith("0038_")]
    if disk_0038 != [hardening_identity]:
        issues.append(
            "the reviewed source tree must contain exactly the canonical "
            "append-only roo.0038 Office Manager hardening migration"
        )
    if len(applied_0038) > 1:
        issues.append("multiple roo.0038 migration identities are recorded")
    if applied_0038 and applied_0038 != disk_0038:
        issues.append(
            "recorded roo.0038 identity does not match the reviewed source tree"
        )
    if hardening_identity in applied_set and recovery_identity not in applied_set:
        issues.append(
            f"{hardening_identity} is recorded without {recovery_identity}"
        )
    if hardening_identity in applied_set:
        issues.extend(_office_manager_hardening_schema_issues())

    attempt_repair_identity = CANONICAL_IDENTITIES[
        "office_manager_attempt_repair"
    ]
    applied_0039 = [name for name in applied if name.startswith("0039_")]
    if disk_0039 != [attempt_repair_identity]:
        issues.append(
            "the reviewed source tree must contain exactly the canonical "
            "append-only roo.0039 Office Manager attempt repair migration"
        )
    if len(applied_0039) > 1:
        issues.append("multiple roo.0039 migration identities are recorded")
    if applied_0039 and applied_0039 != disk_0039:
        issues.append(
            "recorded roo.0039 identity does not match the reviewed source tree"
        )
    if attempt_repair_identity in applied_set and hardening_identity not in applied_set:
        issues.append(
            f"{attempt_repair_identity} is recorded without {hardening_identity}"
        )

    historical_applied = sorted(
        name for name in HISTORICAL_IDENTITIES if name in applied_set
    )
    if data_invariants[
        "claimed_days_without_exactly_one_active_assignment"
    ]:
        issues.append(
            "claimed Office Manager days must have exactly one active assignment"
        )
    if data_invariants["active_assignments_on_non_claimed_days"]:
        issues.append(
            "active Office Manager assignments must belong to claimed days"
        )
    if data_invariants["office_manager_bookings_without_assignment"]:
        issues.append(
            "Office Manager bookings must retain their assignment and day graph"
        )
    if data_invariants["assignment_booking_identity_mismatches"]:
        issues.append(
            "Office Manager assignments, bookings, users, and dates must agree"
        )
    if data_invariants["unreconciled_paid_bookings"]:
        issues.append(
            "active paid coworking bookings require reconciled point-bucket provenance"
        )
    if data_invariants["unreconciled_office_manager_refunds"]:
        issues.append(
            "historical Office Manager refunds require reconciled point-bucket provenance"
        )
    if data_invariants["invalid_office_manager_debit_ledgers"]:
        issues.append(
            "Office Manager refunds require an exact authoritative debit ledger entry"
        )
    if data_invariants["invalid_office_manager_refund_ledgers"]:
        issues.append(
            "Office Manager refunds require an exact authoritative ledger entry"
        )
    if data_invariants["invalid_office_manager_reversal_ledgers"]:
        issues.append(
            "Office Manager refund reversals require an exact authoritative ledger entry"
        )
    if data_invariants["unattested_reversed_office_manager_refunds"]:
        issues.append(
            "historical reversed Office Manager refunds require immutable operator evidence"
        )
    if data_invariants["unrepaired_office_manager_refund_buckets"]:
        issues.append(
            "reconciled historical purchased refunds require immutable "
            "bucket-repair evidence"
        )
    pending_channels = set(
        data_invariants["pending_office_manager_delivery_channels"]
    )
    configured_channel = configured_office_manager_channel.strip()
    if pending_channels and (
        not configured_channel or pending_channels != {configured_channel}
    ):
        issues.append(
            "pending Office Manager public delivery work targets a channel "
            "other than the configured recovery channel"
        )
    # Before 0039, these rows are the repair migration's input and must remain
    # visible without deadlocking deployment before `migrate` can run. Once
    # 0039 is recorded, any remaining row is a real post-repair invariant
    # violation and must stop rollout.
    if (
        attempt_repair_identity in applied_set
        and data_invariants["stale_reopened_office_manager_attempts"]
    ):
        issues.append(
            "reopened Office Manager days contain stale claim attempts that "
            "were not superseded"
        )
    return {
        "report_version": REPORT_VERSION,
        "database_vendor": connection.vendor,
        "inspected_prefixes": list(MIGRATION_PREFIXES),
        "applied_identities": applied,
        "canonical_identities": CANONICAL_IDENTITIES,
        "disk_0036_identities": disk_0036,
        "disk_0037_identities": disk_0037,
        "disk_0038_identities": disk_0038,
        "disk_0039_identities": disk_0039,
        "historical_identities": histories,
        "historical_applied": historical_applied,
        "schema": schema,
        "schema_groups": schema_groups,
        "data_invariants": data_invariants,
        "issues": sorted(set(issues)),
        "configured_office_manager_channel": configured_channel,
    }


def _fingerprint(report: dict) -> str:
    encoded = json.dumps(
        report,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_attestation(path: str, *, expected_fingerprint: str) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommandError(f"Cannot read Office Manager attestation: {exc}") from exc
    if not isinstance(payload, dict):
        raise CommandError("Office Manager attestation must be a JSON object.")
    if payload.get("version") != REPORT_VERSION:
        raise CommandError("Office Manager attestation version is not supported.")
    if payload.get("decision") != "reviewed-compatible":
        raise CommandError(
            "Office Manager attestation decision must be reviewed-compatible."
        )
    if payload.get("report_sha256") != expected_fingerprint:
        raise CommandError(
            "Office Manager attestation does not match the current database report."
        )
    reviewed_by = str(payload.get("reviewed_by") or "").strip()
    if not reviewed_by:
        raise CommandError("Office Manager attestation must name reviewed_by.")
    try:
        reviewed_at = datetime.fromisoformat(
            str(payload.get("reviewed_at") or "").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise CommandError(
            "Office Manager attestation reviewed_at must be ISO-8601."
        ) from exc
    if reviewed_at.tzinfo is None:
        raise CommandError("Office Manager attestation reviewed_at must include a timezone.")
    if reviewed_at.astimezone(timezone.utc) > datetime.now(timezone.utc) + timedelta(
        minutes=5
    ):
        raise CommandError("Office Manager attestation reviewed_at is in the future.")
    return {
        "decision": payload["decision"],
        "reviewed_at": reviewed_at.isoformat(),
        "reviewed_by": reviewed_by,
    }


class Command(BaseCommand):
    help = (
        "Read-only audit of colliding Roo migration identities and Office Manager "
        "schema compatibility."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--attestation-file",
            help=(
                "Read an operator-reviewed JSON attestation tied to this database "
                "report fingerprint. The command never creates or changes it."
            ),
        )
        parser.add_argument(
            "--configured-office-manager-channel",
            default="",
            help=(
                "Fail closed when pending public delivery work is not bound "
                "to exactly this configured Slack channel."
            ),
        )

    def handle(self, *args, **options):
        report = _build_report(
            configured_office_manager_channel=str(
                options.get("configured_office_manager_channel") or ""
            )
        )
        report_sha256 = _fingerprint(report)
        output = {
            **report,
            "report_sha256": report_sha256,
            "status": "safe",
        }

        if report["issues"]:
            output["status"] = "unsafe"
            self.stdout.write(json.dumps(output, sort_keys=True))
            raise CommandError(
                "Office Manager migration history is unsafe; resolve the reported "
                "schema/recorder mismatch before migrations: "
                + "; ".join(report["issues"])
            )

        if report["historical_applied"]:
            attestation_path = str(options.get("attestation_file") or "").strip()
            if not attestation_path:
                output["status"] = "attestation_required"
                self.stdout.write(json.dumps(output, sort_keys=True))
                raise CommandError(
                    "Historical Roo migration identities require an audited "
                    f"attestation for report {report_sha256}."
                )
            output["attestation"] = _load_attestation(
                attestation_path,
                expected_fingerprint=report_sha256,
            )
            output["status"] = "attested"

        self.stdout.write(json.dumps(output, sort_keys=True))
