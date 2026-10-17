"""Private financial audit that survives erasing a native website's local data."""

from copy import deepcopy
import re
from uuid import UUID

from .channel_artifact_contract import canonical_bytes, digest, validate_envelope
from .cms_goal_contract import _time, CmsGoalError

REFERENCE = "cms_financial_settlement"
FIELDS = {"version", "kind", "goal_operation_id", "billing_request_id", "organization_id", "company_id",
    "payer_user_id", "spend_ledger_id", "cost_points", "decision", "retention_subject", "recorded_at", "digest"}
BASES = {"goal_first_draft", "goal_draft_history", "private_native_handoff"}


def retention_subject(value):
    """Validate a minimized private reference, never accept a paid/approved flag."""
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"basis", "evidence_digest"} or value["basis"] not in BASES or
            not isinstance(value["evidence_digest"], str) or re.fullmatch(r"[a-f0-9]{64}", value["evidence_digest"]) is None):
        raise CmsGoalError("The original generated-work reference changed.")
    return deepcopy(value)


def _source(outbox):
    request = outbox.payload["request"]
    envelope = validate_envelope(request["publishing_channel_artifact"])
    identifier = request["cms_goal_operation_id"]
    key = request["client_request_id"]
    cost = request["roo_points_cost"]
    payer = request["roo_points_billing_user_id"]
    ledger = request.get("roo_points_ledger_id")
    if (not isinstance(identifier, str) or str(UUID(identifier)) != identifier or request["cms_goal_id"] != identifier or
            not isinstance(key, str) or re.fullmatch(r"cms-articles-first:[a-f0-9]{64}", key) is None or outbox.client_request_id != key or
            envelope["channel"] != "cms" or envelope["billing_request_id"] != key or envelope["organization_id"] != str(outbox.organization_id) or
            type(cost) is not int or not 0 <= cost < 10**9 or request.get("roo_points_authorized") is not True or
            request.get("roo_points_action") != "article_generation" or request.get("roo_points_billing_status") not in {"free", "charged", "reused"} or
            request.get("domain", "").lower().removeprefix("www.") != envelope["public_origin"].split("/")[2].removeprefix("www.") or
            isinstance(payer, bool) or not str(payer).isdecimal() or not 0 < int(payer) < 2**63 or
            str(request["roo_points_requested_by_user_id"]) != envelope["owner_user_id"] or
            cost > 0 and (request["roo_points_billing_status"] == "free" or not isinstance(ledger, str) or not ledger.isdecimal() or not 0 < int(ledger) < 2**63) or
            cost == 0 and (ledger is not None or request["roo_points_billing_status"] != "free")):
        raise CmsGoalError("The original native financial dispatch changed.")
    return {"goal_operation_id": identifier, "billing_request_id": key, "organization_id": str(outbox.organization_id),
        "company_id": envelope["company_id"], "payer_user_id": str(payer), "spend_ledger_id": ledger, "cost_points": cost}


def seal(outbox, *, decision, subject, at):
    """Only the financial worker records a validated original settlement."""
    try:
        original = _source(outbox)
        if not isinstance(decision, str) or decision not in {"retained", "refunded"}:
            raise CmsGoalError("The native settlement decision changed.")
        subject = retention_subject(subject)
        if (decision == "retained") != (subject is not None):
            raise CmsGoalError("The native settlement evidence changed.")
        value = {"version": 1, "kind": "cms_first_article_settlement", **original, "decision": decision,
            "retention_subject": subject, "recorded_at": _time(at.isoformat()).isoformat()}
        return {**value, "digest": digest(value)}
    except CmsGoalError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise CmsGoalError("The original native settlement changed.") from None


def validate(outbox):
    """Read only a private server stamp tied to its original source and ledger."""
    try:
        value = outbox.payload[REFERENCE]
        if not isinstance(value, dict) or set(value) != FIELDS:
            raise CmsGoalError("The native financial audit changed.")
        expected = seal(outbox, decision=value["decision"], subject=value["retention_subject"], at=_time(value["recorded_at"]))
        if canonical_bytes(value) != canonical_bytes(expected):
            raise CmsGoalError("The native financial audit changed.")
        return deepcopy(expected)
    except CmsGoalError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise CmsGoalError("The original native settlement changed.") from None
