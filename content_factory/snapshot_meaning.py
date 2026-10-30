"""Compare worker run snapshots by meaning; only rails refuse a real change.

Content Factory reports whole run snapshots and the backend compared them byte
for byte, so a spelling difference ("" for null, "2" for 2, an upper-case
repository owner) refused the snapshot and stopped the run. Behind
ARTICLES_SNAPSHOT_MERGE_BY_MEANING_ENABLED every snapshot comparison uses the
one normalisation below. A rail still refuses a real mismatch. Any other
compared field keeps its owner's saved value and records the drift on the run.
"""
from decimal import Decimal, InvalidOperation
import json
import logging
import re

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

DRIFT_RESULT_KEY = "snapshot_drift_v1"
DRIFT_FIELD_LIMIT = 50

# The report's rails ("What stays strict"). A real mismatch still refuses.
RAIL_FIELDS = frozenset({
    # Connection identity and generation at write time.
    "website_connection_id", "repository_id", "github_repo", "connection_generation", "expected_source_sha",
    "website_access_lost",
    # Operation fences before any repository write.
    "operation_id", "operation_attempt", "deletion_epoch",
    # Charge identity and billing amount (roo_points_* by prefix below).
    "client_request_id", "strategy_goal_id", "goal_budget", "first_article_goal_id",
    "first_article_goal_operation_id", "editorial_admission",
    # The approved revision.
    "article_publish_approval_receipt", "article_publish_approval_revoked",
    # The publish target and the founder's delivery consent.
    "connection_target_id", "branch", "app_root", "delivery_mode", "requested_delivery_mode",
    "delivery_mode_confirmed",
    # Organisation.
    "domain",
})
RAIL_PREFIXES = ("roo_points_",)
# Identifiers other code already compares without case: domains (editorial
# admission, cancellation receipts), GitHub owner/repo (casefold throughout
# website_*), UUIDs (canonicalised by connection_contract) and hex commit SHAs.
CASELESS_FIELDS = frozenset({"domain", "github_repo", "website_connection_id", "expected_source_sha"})
# Lists whose order carries no meaning.
SET_LIKE_FIELDS = frozenset({"acceptance_criteria"})
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")


def merge_by_meaning_enabled():
    return getattr(settings, "ARTICLES_SNAPSHOT_MERGE_BY_MEANING_ENABLED", False) is True


def is_rail(field):
    return field in RAIL_FIELDS or str(field).startswith(RAIL_PREFIXES)


def _number(value):
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return str(value)
    return number.normalize() if number.is_finite() else str(value)


def meaning(value, field=""):
    """Return a comparable form. Empty strings and null become None."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        # bool is an int in Python; a flag never equals the number 1.
        return ("bool", value)
    if isinstance(value, (int, float, Decimal)):
        return _number(value)
    if isinstance(value, str):
        if _NUMBER.fullmatch(value.strip()):
            return _number(value)
        if field in CASELESS_FIELDS:
            return value.strip().casefold() or None
        return value
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            item = meaning(item, str(key))
            if item is not None:  # A missing key, null and "" are the same.
                normalized[str(key)] = item
        return normalized
    if isinstance(value, (list, tuple)):
        items = [meaning(item, field) for item in value]
        if field in SET_LIKE_FIELDS:
            items.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str))
        return items
    return value


def same_meaning(saved, reported, field=""):
    return meaning(saved, field) == meaning(reported, field)


def refuses(field, saved, reported, drift, *, path=""):
    """True only for a real rail mismatch; other differences are noted as drift."""
    if same_meaning(saved, reported, field):
        return False
    if is_rail(field):
        return True
    drift.append(path + str(field))
    return False


def record_drift(result, drift, *, run_id=""):
    """Count drifted fields on the run's result JSON. Never records values."""
    if not drift:
        return result
    merged = dict(result if isinstance(result, dict) else {})
    saved = merged.get(DRIFT_RESULT_KEY)
    saved = saved if isinstance(saved, dict) else {}
    fields = dict(saved.get("fields") or {}) if isinstance(saved.get("fields"), dict) else {}
    names = sorted({str(field)[:120] for field in drift})
    for name in names:
        if name in fields or len(fields) < DRIFT_FIELD_LIMIT:
            previous = fields.get(name)
            fields[name] = (previous if type(previous) is int and previous > 0 else 0) + 1
    count = saved.get("count")
    merged[DRIFT_RESULT_KEY] = {
        "schema_version": 1,
        "count": (count if type(count) is int and count > 0 else 0) + 1,
        "fields": fields,
        "last_fields": names[:20],
        "last_at": timezone.now().isoformat(),
    }
    logger.warning("content_factory_snapshot_drift run_id=%s fields=%s",
                   str(run_id)[:100], ",".join(names)[:500])
    return merged
