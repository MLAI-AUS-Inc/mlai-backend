"""Test-only helpers for P2 (self-healing). Never import this from production code.

- :data:`ALL_P2_FLAGS_ON`: an ``override_settings`` dict with every P2 boolean
  switch (contract section 5.1) on. Pair it with P1's ``ALL_P1_FLAGS_ON``, or use
  :data:`ALL_FLAGS_ON`, so new-journey organisations are in the P2 cohort.
- :data:`P2_SETTING_DEFAULTS`: the documented code default of every P2 setting.
- :func:`make_p2_org`: a new-journey organisation (in the P2 cohort while the P1
  V2 switch is on); :func:`make_named_legacy_org` is P1's, re-exported.
- :func:`make_run`: stamped and unstamped runs.
- :func:`fake_provider`: installs a fake module for an IF-P2-24 seam and clears
  every ``_optional`` cache, so the seam sees it; and
  :func:`fake_member_context`, a fake ``founder_tools.startup_team``.
- One builder per backend receipt in contract section 10.

Every backend P2 package proves its legacy guarantee with these builders.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone as dt_timezone
import sys
import types
from uuid import uuid4

from django.utils import timezone

from .founder_journey_testing import (  # noqa: F401 - re-exported for P2 packages
    ALL_P1_FLAGS_ON,
    FounderJourneyOrg,
    NAMED_LEGACY_ORGS,
    make_history_free_org,
    make_named_legacy_org,
    make_new_journey_org,
)
from .p2_foundation import BACKEND_WORKER, MERGE_POLICY_KEY, STAMP_KEY, STAMP_VERSION, TEAM_MODULE

P2_FLAG_NAMES = (
    "ARTICLES_FAILURE_POLICY_V2_ENABLED",
    "MY_STARTUP_CANONICAL_ACTION_IDS_ENABLED",
    "ARTICLES_SELF_HEAL_POLICY_ENABLED",
    "ARTICLES_SELF_HEAL_REPORT_ENABLED",
    "ARTICLES_CHANNEL_SWITCH_ENABLED",
    "ARTICLES_SOURCE_REPIN_ENABLED",
    "ARTICLES_CONNECTION_REFUSAL_CODES_ENABLED",
    "ARTICLES_DISPATCH_RELIABILITY_ENABLED",
    "ARTICLES_NO_CODELESS_TERMINAL_ENABLED",
    "ARTICLES_REGISTRY_REFUNDS_ENABLED",
    "MY_STARTUP_IDEMPOTENT_PROFILE_SAVE_ENABLED",
    "MY_STARTUP_STAFF_CONSOLE_ENABLED",
    "ARTICLES_STAFF_ALERT_RULES_ENABLED",
    "ARTICLES_MERGE_WORKER_ENABLED",
    "ARTICLES_DEPLOYMENT_WATCH_SWEEP_ENABLED",
    "ARTICLES_GITHUB_INSTALLATION_WAKES_ENABLED",
    "ARTICLES_SETUP_EXAMPLE_AUTO_REMOVE_ENABLED",
    "MY_STARTUP_TRANSITION_NOTIFICATIONS_ENABLED",
    "MY_STARTUP_TURN_REMINDERS_ENABLED",
    "ARTICLES_HOSTED_AUTO_REGISTER_ENABLED",
    "ARTICLES_PUBLISH_REVIEWED_DRAFT_ENABLED",
    "MY_STARTUP_TEAM_ENABLED",
    "MY_STARTUP_TEAM_DOMAIN_AUTO_JOIN_ENABLED",
)
# Documented code defaults of every P2 backend setting (contract section 5.1).
P2_SETTING_DEFAULTS = {
    **{name: False for name in P2_FLAG_NAMES},
    "ARTICLES_SELF_HEAL_LEGACY_ORGANIZATIONS": "",
    "ARTICLES_SELF_HEAL_SWEEP_LIMIT": 50,
    "ARTICLES_MERGE_WORKER_LIMIT": 20,
    "ARTICLES_HOSTED_RUNG_ORGANIZATIONS": "",
    "MY_STARTUP_TEAM_DOMAIN_DENYLIST": "",
}
ALL_P2_FLAGS_ON = {name: True for name in P2_FLAG_NAMES}
ALL_FLAGS_ON = {**ALL_P1_FLAGS_ON, **ALL_P2_FLAGS_ON}

P2_SEAM_PROVIDER_MODULES = (
    "content_factory.failure_policy_engine",
    "content_factory.no_delivery_refunds",
    "content_factory.publish_merge_worker",
    "content_factory.hosted_auto_register",
    "content_factory.founder_transitions",
    "content_factory.staff_alert_rules",
    "content_factory.channel_switch",
    "content_factory.publish_reviewed_draft",
    TEAM_MODULE,
)


def make_p2_org(domain=None, **kwargs) -> FounderJourneyOrg:
    """A new-journey organisation: in the P2 cohort while ``ALL_P1_FLAGS_ON`` (or ``ALL_FLAGS_ON``) is active."""
    return make_new_journey_org(domain, **kwargs)


def make_run(organization, *, stamped=False, cohort="p2", failure_policy="v2", merge_policy=None,
             workflow="article_generation", status="failed", result=None, run_request=None, run_id=None):
    """A ``ContentFactoryRun`` for ``organization``, with or without the IF-P2-26 stamp."""
    from workflow_runs.models import ContentFactoryRun
    request = dict(run_request or {})
    if stamped:
        request[STAMP_KEY] = {"version": STAMP_VERSION, "cohort": cohort, "failure_policy": failure_policy}
    if merge_policy is not None:
        request[MERGE_POLICY_KEY] = merge_policy
    return ContentFactoryRun.objects.create(
        run_id=run_id or f"run-{uuid4().hex[:12]}", organization=organization, workflow=workflow, status=status,
        run_request=request, result=dict(result or {}))


def make_stamped_run(organization, **kwargs):
    """A run stamped ``cohort: "p2"``, ``failure_policy: "v2"``."""
    return make_run(organization, stamped=True, **kwargs)


def make_unstamped_run(organization, **kwargs):
    """A run with no stamp, which CF and every P2 path treat as legacy."""
    return make_run(organization, stamped=False, **kwargs)


def clear_optional_caches() -> None:
    """Clear the ``_optional`` lookup cache of every loaded module that has one."""
    for module in list(sys.modules.values()):
        helper = getattr(module, "_optional", None) if module is not None else None
        cache_clear = getattr(helper, "cache_clear", None)
        if callable(cache_clear):
            cache_clear()


@contextmanager
def fake_provider(module: str, **functions):
    """Install a fake ``module`` exposing ``functions`` for the duration of the block.

    The real module (if any) is restored afterwards, and every ``_optional``
    cache is cleared on the way in and out.
    """
    sentinel = object()
    previous = sys.modules.get(module, sentinel)
    if module == TEAM_MODULE:
        functions.setdefault("pinned_owner", lambda organization: (False, None))
    fake = types.ModuleType(module)
    for name, function in functions.items():
        setattr(fake, name, function)
    sys.modules[module] = fake
    clear_optional_caches()
    try:
        yield fake
    finally:
        if previous is sentinel:
            sys.modules.pop(module, None)
        else:
            sys.modules[module] = previous
        clear_optional_caches()


@contextmanager
def missing_provider(module: str):
    """Make ``module`` unimportable for the duration of the block (its package not merged yet)."""
    sentinel = object()
    previous = sys.modules.get(module, sentinel)
    sys.modules[module] = None  # an import of a None entry raises ImportError
    clear_optional_caches()
    try:
        yield
    finally:
        if previous is sentinel:
            sys.modules.pop(module, None)
        else:
            sys.modules[module] = previous
        clear_optional_caches()


@contextmanager
def fake_member_context(*, members=(), publishers=(), owner_ids=()):
    """A fake ``founder_tools.startup_team`` for the member seams (IF-P2-22, IF-P2-24).

    ``members`` are user ids with role ``member``; ``publishers`` are the
    members who may publish; ``owner_ids`` have role ``owner``. Anyone else
    has no role.
    """
    member_ids, publisher_ids, owners = set(members), set(publishers), set(owner_ids)

    def viewer_role(user, organization):
        user_id = getattr(user, "pk", user)
        if user_id in owners:
            return "owner"
        return "member" if user_id in member_ids else None

    def member_may_publish(user, organization):
        user_id = getattr(user, "pk", user)
        return user_id in owners or user_id in publisher_ids

    def member_may_use_run(user, run):
        role = viewer_role(user, run.organization)
        return role == "owner" or (role == "member" and run.workflow in {
            "article_generation", "article_revision", "confirmed_topic", "content_factory_article", "direct_generate",
        })

    def notification_recipients(organization, *, item, ask=None):
        from core.models import User
        from founder_tools.services import organization_owner_user_id
        recipients = set(owners)
        owner_id = organization_owner_user_id(organization)
        if owner_id is not None:
            recipients.add(owner_id)
        article = item.startswith("article")
        if article and ask == "approve_on_github":
            recipients.update(member_ids & publisher_ids)
        elif article and ask not in {"approve_setup", "authorize_github", "approve_join_request", "agree_terms"}:
            recipients.update(member_ids)
        return list(User.objects.filter(pk__in=recipients).order_by("pk"))

    def member_context(user, company_id, *, need):
        from types import SimpleNamespace
        from founder_tools.models import VibeRaisingCompany
        from founder_tools.services import get_or_create_founder_profile
        company = VibeRaisingCompany.objects.select_related("profile", "organization").filter(pk=company_id).first()
        if company is None or viewer_role(user, company.organization) != "member":
            return None
        rights = {"read", "write_articles"}
        if getattr(user, "pk", user) in publisher_ids:
            rights.add("publish")
        if need not in rights:
            return None
        return SimpleNamespace(profile=get_or_create_founder_profile(user), company=company,
                               organization=company.organization, role="member", rights=frozenset(rights))

    references = {}

    def issue_join_reference(*, user, organization):
        if user is None or organization is None:
            return None
        key = (user.pk, organization.pk)
        if key not in references:
            references[key] = uuid4().hex
        return references[key]

    def pending_request_count(organization):
        return 0

    with fake_provider(TEAM_MODULE, viewer_role=viewer_role, member_may_publish=member_may_publish,
                       member_may_use_run=member_may_use_run, notification_recipients=notification_recipients,
                       member_context=member_context, issue_join_reference=issue_join_reference,
                       pending_request_count=pending_request_count) as fake:
        yield fake


# --- Receipt builders (contract section 10) -----------------------------------

def _iso(moment=None) -> str:
    moment = moment or timezone.now()
    if isinstance(moment, str):
        return moment
    if timezone.is_naive(moment):
        moment = moment.replace(tzinfo=dt_timezone.utc)
    return moment.isoformat()


def self_heal_receipt(*, mode="policy", code="GITHUB_TEMPORARILY_UNAVAILABLE", policy="retry_same_step", attempts=1,
                      next_at=None, capped=False, rung=None, source_sha=None, history=None, at=None) -> dict:
    """``run.result["self_heal_v1"]`` (IF-P2-07)."""
    at = _iso(at)
    return {"version": 1, "mode": mode, "code": code, "policy": policy, "attempts": attempts, "first_failed_at": at,
            "last_action": "retry", "last_action_at": at, "next_at": _iso(next_at) if next_at else None, "rung": rung,
            "source_sha": source_sha, "capped": capped,
            "history": list(history if history is not None else [{"at": at, "action": "retry", "code": code, "attempt": attempts}])}


def points_receipt(*, state="captured", points=12, original_key=None, at=None, refund_ledger_id=None) -> dict:
    """``run.result["points_v1"]`` (IF-P2-12)."""
    return {"version": 1, "state": state, "points": points, "original_key": original_key, "at": _iso(at),
            "refund_ledger_id": refund_ledger_id}


def channel_receipt(*, channel="copy_ready", reason="ladder_exhausted", at=None) -> dict:
    """``run.result["channel_v1"]`` (IF-P2-10)."""
    return {"version": 1, "channel": channel, "reason": reason, "waiting_to_publish": True, "at": _iso(at)}


def re_pin_receipt(*, from_sha="a" * 40, to_sha="b" * 40, reason="source_changed", at=None) -> dict:
    """``run.result["re_pin_v1"]`` (IF-P2-09)."""
    at = _iso(at)
    return {"version": 1, "repins": 1, "first_from_sha": from_sha, "last_to_sha": to_sha,
            "history": [{"from_sha": from_sha, "to_sha": to_sha, "reason": reason, "repin_key": f"{from_sha[:12]}:{to_sha[:12]}", "at": at}]}


def article_approval_receipt(*, source="receipt", approved_by_user_id=1, run_id="run-1", approved_body_sha256=None,
                             claims_open=0, at=None) -> dict:
    """``run.result["article_approval_v1"]`` (IF-P2-15)."""
    return {"version": 1, "source": source, "approved_by_user_id": approved_by_user_id, "approved_at": _iso(at),
            "review_identity": {"run_id": run_id, "run_generation": 1, "preview_url": None, "commit_sha": "c" * 40,
                                "resume_generation": 0, "quality_inputs_sha256": "d" * 64},
            "approved_body_sha256": approved_body_sha256, "claims_open": claims_open}


def owner_merge_approval_receipt(*, kind="setup", pr_url="https://github.com/example/site/pull/1", head_sha="e" * 40,
                                 approved_by_user_id=1, at=None) -> dict:
    """``run.result["owner_merge_approval_v1"]`` (IF-P2-15)."""
    return {"version": 1, "kind": kind, "pr_url": pr_url, "head_sha": head_sha,
            "approved_by_user_id": approved_by_user_id, "approved_at": _iso(at)}


def merge_receipt(*, merge_sha="f" * 40, head_sha="e" * 40, method="squash", consent="article_approval_v1",
                  approved_by_user_id=1, at=None) -> dict:
    """``run.result["merge_v1"]`` (IF-P2-15)."""
    return {"merged_at": _iso(at), "merge_sha": merge_sha, "head_sha": head_sha, "method": method, "consent": consent,
            "approved_by_user_id": approved_by_user_id}


def deploy_receipt(*, url="https://example.test/blog/post", provider="vercel", at=None) -> dict:
    """``run.result["deploy_v1"]`` (IF-P2-15)."""
    return {"live_at": _iso(at), "url": url, "provider": provider}


def staff_actions_receipt(*, actions=(("retry_now", "done"),), by_user_id=1, note="", at=None) -> list:
    """``run.result["staff_actions_v1"]`` (IF-P2-19): a list, last 20."""
    at = _iso(at)
    return [{"action": action, "at": at, "by_user_id": by_user_id, "note": note[:500], "outcome": outcome}
            for action, outcome in actions][-20:]


def publish_offer_receipt(*, dismissed_at=None) -> dict:
    """``run.result["publish_offer_v1"]`` (IF-P2-21)."""
    return {"dismissed_at": _iso(dismissed_at) if dismissed_at else None}


def ops_alert_receipt(*, alert_id=None, alert_reason="our_team_item", state="pending", run=None, company_id=None,
                      details=None, queued_at=None, acknowledged_at=None, acknowledged_by_user_id=None) -> dict:
    """One ``pillar_strategy["ops_alerts"][alert_id]`` receipt with the P2 acknowledgement fields (IF-P2-19)."""
    receipt = {"alert_id": alert_id or str(uuid4()), "alert_reason": alert_reason, "state": state, "attempts": 0,
               "sent_at": None, "queued_at": _iso(queued_at), "company_id": str(company_id) if company_id else None,
               "run_id": str(run.run_id) if run is not None else None, "run_pk": run.pk if run is not None else None,
               "details": dict(details or {})}
    if acknowledged_at is not None:
        receipt.update(acknowledged_at=_iso(acknowledged_at), acknowledged_by_user_id=acknowledged_by_user_id)
    return receipt


def publish_reviewed_receipt(*, source_run_id="source-run", approved_body_sha256="a"*64):
    """Backend-owned saved-body publish identity, protected alongside section10 receipts."""
    return {"source_run_id": source_run_id, "approved_body_sha256": approved_body_sha256}


def publish_dispatch_receipt(*, at=None, hosted=False):
    """Backend-owned accepted publish dispatch, including the hosted destination kind."""
    return {"accepted_at": _iso(at), "hosted": bool(hosted)}


RECEIPT_BUILDERS = {
    "self_heal_v1": self_heal_receipt,
    "points_v1": points_receipt,
    "channel_v1": channel_receipt,
    "re_pin_v1": re_pin_receipt,
    "article_approval_v1": article_approval_receipt,
    "owner_merge_approval_v1": owner_merge_approval_receipt,
    "merge_v1": merge_receipt,
    "deploy_v1": deploy_receipt,
    "staff_actions_v1": staff_actions_receipt,
    "publish_offer_v1": publish_offer_receipt,
    "publish_reviewed_v1": publish_reviewed_receipt,
    "publish_dispatch_v1": publish_dispatch_receipt,
}
FRIDAY_4PM_MELBOURNE = datetime(2026, 10, 30, 5, 0, tzinfo=dt_timezone.utc)  # Fri 30 Oct 2026, 16:00 AEDT
__all__ = [
    "ALL_FLAGS_ON", "ALL_P1_FLAGS_ON", "ALL_P2_FLAGS_ON", "BACKEND_WORKER", "P2_FLAG_NAMES", "P2_SETTING_DEFAULTS",
    "RECEIPT_BUILDERS", "fake_member_context", "fake_provider", "make_named_legacy_org", "make_p2_org", "make_run",
    "make_stamped_run", "make_unstamped_run", "missing_provider", "ops_alert_receipt",
]
