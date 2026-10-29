from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit


CONTENT_FACTORY_ARTICLE_COST_POINTS = 12
CONTENT_FACTORY_CONTENT_ISLAND_TOPIC_COST_POINTS = 1
CONTENT_FACTORY_MINIMUM_AI_AGENT_POINTS = 6
FREE_CONTENT_FACTORY_DOMAINS = {"mlai.au"}
INSUFFICIENT_ROO_POINTS_ERROR_CODE = "INSUFFICIENT_ROO_POINTS"
CONTENT_FACTORY_ACTION_ARTICLE_GENERATION = "article_generation"
CONTENT_FACTORY_ACTION_CONTENT_ISLAND_TOPIC_GENERATION = "content_island_topic_generation"
FREE_SETUP_ACTIONS = frozenset({"prepare", "repo_scan", "content_factory_scan", "article_system_setup", "website_verify"})
# Startup research a new founder needs before any article. Free only behind
# ARTICLES_FREE_SETUP_RESEARCH_ENABLED, and capped per account per day.
FREE_SETUP_RESEARCH_ACTIONS = frozenset({"startup_autofill", "website_baseline"})
FREE_SETUP_RESEARCH_CAP_SETTINGS = {
    "startup_autofill": "ARTICLES_FREE_SETUP_AUTOFILL_DAILY_CAP",
    "website_baseline": "ARTICLES_FREE_SETUP_BASELINE_DAILY_CAP",
}
FREE_SETUP_RESEARCH_WINDOW_SECONDS = 24 * 60 * 60


def free_setup_actions() -> frozenset:
    """Return the AI actions that skip the 6-point balance floor.

    Setup work is always free. Startup autofill and the website baseline join
    it only while ARTICLES_FREE_SETUP_RESEARCH_ENABLED is on, because Content
    Factory refuses a 'free' authorization for them until it accepts one.
    """
    from django.conf import settings

    if getattr(settings, "ARTICLES_FREE_SETUP_RESEARCH_ENABLED", False):
        return FREE_SETUP_ACTIONS | FREE_SETUP_RESEARCH_ACTIONS
    return FREE_SETUP_ACTIONS


def normalized_account_key(user) -> str:
    """Key per-account limits on the account email, or the user id without one."""
    email = str(getattr(user, "email", "") or "").strip().casefold()
    return f"email:{email}" if email else f"user:{getattr(user, 'pk', '')}"


def enforce_free_setup_daily_cap(user, action: str, *, now: Optional[float] = None) -> None:
    """Allow a free setup research action at most N times per account in 24 hours.

    The window rolls: each use is remembered for 24 hours. A cap of 0 turns the
    limit off. Over the cap this raises DRF ``Throttled`` (429 + Retry-After),
    so no new failure code is introduced.
    """
    import hashlib
    import time

    from django.conf import settings
    from django.core.cache import cache
    from rest_framework.exceptions import Throttled

    setting = FREE_SETUP_RESEARCH_CAP_SETTINGS.get(action)
    cap = int(getattr(settings, setting, 0) or 0) if setting else 0
    if cap <= 0:
        return
    current = time.time() if now is None else float(now)
    digest = hashlib.sha256(normalized_account_key(user).encode("utf-8")).hexdigest()
    key = f"content_factory:free_setup:{action}:{digest}"
    window = FREE_SETUP_RESEARCH_WINDOW_SECONDS
    recent = sorted(stamp for stamp in (cache.get(key) or []) if isinstance(stamp, (int, float)) and current - stamp < window)
    if len(recent) >= cap:
        raise Throttled(wait=max(1, int(recent[len(recent) - cap] + window - current) + 1))
    cache.set(key, [*recent, current], timeout=window)


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


def get_content_factory_article_cost_points(domain: Optional[str], *, user=None) -> int:
    """Quote every article at 12 points, halved by the existing founder benefit."""
    if user is not None:
        from startup_updates.benefits import has_monthly_update_discount

        if has_monthly_update_discount(user):
            return CONTENT_FACTORY_ARTICLE_COST_POINTS // 2
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


def _points(count: int) -> str:
    return "point" if count == 1 else "points"


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
    if required_points is None and action == CONTENT_FACTORY_ACTION_ARTICLE_GENERATION:
        required = cost
    balance = int(current_balance or 0)
    account = mask_billing_email(account_email)
    payer = mask_billing_email(billing_email) if billing_email else account
    # Founder copy names the numbers only. The masked account and payer stay
    # in their own keys for older clients and are never part of the sentence.
    if cost > 0 and action == CONTENT_FACTORY_ACTION_CONTENT_ISLAND_TOPIC_GENERATION:
        message = f"Finding topics uses {cost} {_points(cost)}. You have {balance}."
    elif cost > 0:
        message = f"This article uses {cost} {_points(cost)}. You have {balance}."
    else:
        message = f"This needs {required} {_points(required)}. You have {balance}."
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
        "free_domain": action != CONTENT_FACTORY_ACTION_ARTICLE_GENERATION and is_free_content_factory_domain(normalized_domain),
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
    entitlement: Optional[str] = None,
) -> dict:
    """Build the run_request billing stamp Content Factory checks before AI work.

    ``entitlement`` names the free grant behind an 'entitled' stamp (for now
    only 'first_article'); its ledger id is the zero-point entitlement row.
    """
    payload = {
        "roo_points_authorized": True,
        "roo_points_action": action,
        "roo_points_cost": int(cost_points or 0),
        "roo_points_required": int(
            required_points
            if required_points is not None
            else cost_points if action == CONTENT_FACTORY_ACTION_ARTICLE_GENERATION
            else get_content_factory_ai_agent_required_points(domain)
        ),
        "roo_points_billing_status": str(billing_status or "").strip(),
        "free_domain": action != CONTENT_FACTORY_ACTION_ARTICLE_GENERATION and is_free_content_factory_domain(domain),
    }
    if current_balance is not None:
        payload["roo_points_balance"] = int(current_balance)
    if ledger_id not in (None, ""):
        payload["roo_points_ledger_id"] = str(ledger_id)
    if entitlement:
        payload["roo_points_entitlement"] = str(entitlement)
    return payload


def public_run_billing_receipt(run_request: dict, result: dict) -> dict:
    """Expose backend billing state for polling without payer or ledger identities."""
    billing_status = str(run_request.get("roo_points_billing_status") or "").strip()
    if billing_status not in {"charged", "reused", "free", "gated", "refunded", "entitled"}:
        billing_status = "unknown"
    pending = bool(
        run_request.get("pending_billing_refund") and not result.get("dispatch_refund_processed")
        or result.get("reconciliation_refund_pending")
    )
    return {
        "billingStatus": billing_status,
        "refundStatus": "refunded" if billing_status == "refunded" else "pending" if pending else "none",
    }
