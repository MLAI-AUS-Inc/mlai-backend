from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit


CONTENT_FACTORY_ARTICLE_COST_POINTS = 6
CONTENT_FACTORY_MINIMUM_AI_AGENT_POINTS = 6
FREE_CONTENT_FACTORY_DOMAINS = {"mlai.au"}
INSUFFICIENT_ROO_POINTS_ERROR_CODE = "INSUFFICIENT_ROO_POINTS"


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
    if cost > 0:
        message = f"Creating an article costs {cost} Roo points, and this user does not have enough."
    else:
        message = f"This AI action requires at least {required} Roo points before it can start."
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
