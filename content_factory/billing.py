from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit


CONTENT_FACTORY_ARTICLE_COST_POINTS = 6
CONTENT_FACTORY_CONTENT_ISLAND_TOPIC_COST_POINTS = 1
CONTENT_FACTORY_MINIMUM_AI_AGENT_POINTS = 6
FREE_CONTENT_FACTORY_DOMAINS = {"mlai.au"}
INSUFFICIENT_ROO_POINTS_ERROR_CODE = "INSUFFICIENT_ROO_POINTS"
CONTENT_FACTORY_ACTION_ARTICLE_GENERATION = "article_generation"
CONTENT_FACTORY_ACTION_CONTENT_ISLAND_TOPIC_GENERATION = "content_island_topic_generation"
FREE_SETUP_ACTIONS = frozenset({"prepare", "repo_scan", "content_factory_scan", "article_system_setup", "website_verify"})


def get_content_factory_setup_cost_points(domain: Optional[str]) -> int:
    """Website preparation and its verification steps are free for every company."""
    return 0


def mask_billing_email(email: str) -> str:
    """Identify an account without exposing its complete email address."""
    local, separator, host = str(email or "").partition("@")
    return f"{local[:1]}***@{host}" if separator else "your signed-in account"


def normalize_content_factory_domain(domain: Optional[str]) -> str:
    raw = str(domain or "").strip().lower()
    if not raw:
        return ""

    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlsplit(raw)
    host = (parsed.hostname or raw).strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def is_free_content_factory_domain(domain: Optional[str]) -> bool:
    return normalize_content_factory_domain(domain) in FREE_CONTENT_FACTORY_DOMAINS


def get_content_factory_article_cost_points(domain: Optional[str]) -> int:
    if is_free_content_factory_domain(domain):
        return 0
    return CONTENT_FACTORY_ARTICLE_COST_POINTS


def get_content_factory_content_island_topic_cost_points(domain: Optional[str]) -> int:
    if is_free_content_factory_domain(domain):
        return 0
    return CONTENT_FACTORY_CONTENT_ISLAND_TOPIC_COST_POINTS


def get_content_factory_research_cost_points(domain: Optional[str], requested_topic_count=4) -> int:
    """Quote one Roo point per requested research topic, with a bounded batch."""
    try:
        count = int(requested_topic_count)
    except (TypeError, ValueError):
        count = 4
    return get_content_factory_content_island_topic_cost_points(domain) * max(1, min(count, 8))


def get_content_factory_ai_agent_required_points(domain: Optional[str]) -> int:
    if is_free_content_factory_domain(domain):
        return 0
    return CONTENT_FACTORY_MINIMUM_AI_AGENT_POINTS


def build_roo_points_payload(
    *,
    domain: Optional[str],
    action: str,
    current_balance: Optional[int],
    required_points: Optional[int] = None,
    cost_points: Optional[int] = None,
    account_email: str = "",
    other_founder_has_points: bool = False,
    billing_email: str = "",
) -> dict:
    normalized_domain = normalize_content_factory_domain(domain)
    required = (
        int(required_points)
        if required_points is not None
        else get_content_factory_ai_agent_required_points(normalized_domain)
    )
    cost = (
        int(cost_points)
        if cost_points is not None
        else get_content_factory_article_cost_points(normalized_domain)
    )
    balance = int(current_balance or 0)
    account = mask_billing_email(account_email)
    payer = mask_billing_email(billing_email) if billing_email else account
    if cost > 0 and action == CONTENT_FACTORY_ACTION_CONTENT_ISLAND_TOPIC_GENERATION:
        plural = "point" if cost == 1 else "points"
        message = f"Researching topics costs {cost} Roo {plural}. {payer} has {balance} Roo points."
    elif cost > 0:
        message = f"Creating an article costs {cost} Roo points. {payer} has {balance} Roo points."
    else:
        message = f"This AI action requires at least {required} Roo points. {payer} has {balance} Roo points."
    if other_founder_has_points:
        message += " Another founder has enough points and can opt in as the company billing founder."
    return {
        "error": message,
        "detail": message,
        "message": message,
        "error_code": INSUFFICIENT_ROO_POINTS_ERROR_CODE,
        "required_points": required,
        "current_balance": balance,
        "cost_points": cost,
        "free_domain": is_free_content_factory_domain(normalized_domain),
        "domain": normalized_domain,
        "action": action,
        "retryable": False,
        "signed_in_account": account,
        "billing_account": payer,
        "other_founder_has_points": bool(other_founder_has_points),
    }


def build_roo_points_authorization_payload(
    *,
    domain: Optional[str],
    action: str,
    cost_points: int,
    required_points: Optional[int] = None,
    current_balance: Optional[int] = None,
    billing_status: str,
    ledger_id: Optional[object] = None,
) -> dict:
    payload = {
        "roo_points_authorized": True,
        "roo_points_action": action,
        "roo_points_cost": int(cost_points or 0),
        "roo_points_required": int(
            required_points
            if required_points is not None
            else get_content_factory_ai_agent_required_points(domain)
        ),
        "roo_points_billing_status": str(billing_status or "").strip(),
        "free_domain": is_free_content_factory_domain(domain),
    }
    if current_balance is not None:
        payload["roo_points_balance"] = int(current_balance)
    if ledger_id not in (None, ""):
        payload["roo_points_ledger_id"] = str(ledger_id)
    return payload


def public_run_billing_receipt(run_request: dict, result: dict) -> dict:
    """Expose backend billing state for polling without payer or ledger identities."""
    billing_status = str(run_request.get("roo_points_billing_status") or "").strip()
    if billing_status not in {"charged", "reused", "free", "gated", "refunded"}:
        billing_status = "unknown"
    pending = bool(
        run_request.get("pending_billing_refund") and not result.get("dispatch_refund_processed")
        or result.get("reconciliation_refund_pending")
    )
    return {
        "billingStatus": billing_status,
        "refundStatus": "refunded" if billing_status == "refunded" else "pending" if pending else "none",
    }
