import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
}
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


def _disk_0036_identities() -> list[str]:
    migration_dir = Path(settings.BASE_DIR) / "roo" / "migrations"
    return sorted(path.stem for path in migration_dir.glob("0036_*.py"))


def _office_manager_data_invariants() -> dict:
    """Read cross-table invariants that database constraints cannot express."""
    table_names = set(connection.introspection.table_names())
    required = {"roo_officemanagerday", "roo_officemanagerassignment"}
    if not required.issubset(table_names):
        return {
            "checked": False,
            "claimed_days_without_exactly_one_active_assignment": [],
            "active_assignments_on_non_claimed_days": [],
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
    return {
        "checked": True,
        "claimed_days_without_exactly_one_active_assignment": claimed_mismatches,
        "active_assignments_on_non_claimed_days": inverse_mismatches,
    }


def _build_report() -> dict:
    applied = _applied_migration_names()
    applied_set = set(applied)
    schema = _schema_snapshot()
    schema_groups = _schema_group_report(schema)
    data_invariants = _office_manager_data_invariants()
    disk_0036 = _disk_0036_identities()
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
        if group == "office_manager_protect":
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
    if len(disk_0036) != 1:
        issues.append(
            "the reviewed source tree must contain exactly one append-only roo.0036 migration"
        )
    if len(applied_0036) > 1:
        issues.append("multiple roo.0036 migration identities are recorded")
    if applied_0036 and applied_0036 != disk_0036:
        issues.append(
            "recorded roo.0036 identity does not match the reviewed source tree"
        )
    if applied_0036 and protect_identity not in applied_set:
        issues.append(f"{applied_0036[0]} is recorded without {protect_identity}")

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
    return {
        "report_version": REPORT_VERSION,
        "database_vendor": connection.vendor,
        "inspected_prefixes": list(MIGRATION_PREFIXES),
        "applied_identities": applied,
        "canonical_identities": CANONICAL_IDENTITIES,
        "disk_0036_identities": disk_0036,
        "historical_identities": histories,
        "historical_applied": historical_applied,
        "schema": schema,
        "schema_groups": schema_groups,
        "data_invariants": data_invariants,
        "issues": sorted(set(issues)),
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

    def handle(self, *args, **options):
        report = _build_report()
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
                "schema/recorder mismatch before migrations."
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
