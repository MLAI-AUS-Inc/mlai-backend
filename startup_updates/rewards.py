"""Startup Pulse credits, keyed to an approved update and a Melbourne month."""
from datetime import datetime
import re
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import Q

from founder_tools.models import VibeRaisingCompany
from organizations.models import Organization
from roo.models import Ledger
from startup_updates.models import MonthlyUpdateDraft

MELBOURNE = ZoneInfo("Australia/Melbourne")
MONTHLY_POINTS = 20
STANDARD_POINTS = 5


def reward_key(draft, month=None):
    """An edit, retry, different founder or reporting date cannot repay an update."""
    key = f"monthly_update_reward:organization:{draft.organization_id}:update:{draft.pk}"
    return f"{key}:month:{month:%Y-%m}" if month is not None else key


def reward_month_bounds(at):
    """Use a fixed Australian calendar regardless of editable reporting settings."""
    local = at.astimezone(MELBOURNE)
    start = datetime(local.year, local.month, 1, tzinfo=MELBOURNE)
    end = datetime(local.year + (local.month == 12), local.month % 12 + 1, 1, tzinfo=MELBOURNE)
    return start, end


def update_ledger(draft):
    """Include historical awards made before credits were keyed per update."""
    return Ledger.objects.filter(
        Q(idempotency_key=reward_key(draft))
        | Q(reference_type="MONTHLY_UPDATE_DRAFT", reference_id=str(draft.pk)),
        source="STARTUP_UPDATE", kind="EARN",
    ).order_by("created_at", "pk").first()


def reward_receipt(ledger, *, user=None, awarded=False):
    """Expose credited points without disclosing another founder's identity."""
    from roo.services import PointsService

    exact = ledger.delta_microroo
    if exact is None:
        # Historical ledger rows can predate the microroo columns. Report their
        # recorded whole-point amount without mutating a wallet during a read.
        whole = ledger.delta if ledger.delta is not None else ledger.points_delta
        exact = PointsService.roo_to_microroo(whole or 0)
    points = PointsService.microroo_to_legacy_whole(exact)
    month = str(ledger.idempotency_key or "").rsplit(":month:", 1)[-1]
    if not re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", month):
        month = ledger.created_at.astimezone(MELBOURNE).strftime("%Y-%m")
    return {
        "points": points,
        "awarded": awarded,
        "status": "awarded" if awarded else "already_awarded",
        "month": month,
        "tier": "verified_monthly" if points >= MONTHLY_POINTS else "standard",
        "creditedToCurrentUser": bool(user is not None and ledger.user_id == user.pk),
    }


def update_reward_receipt(draft, *, user=None):
    """Read the durable credit receipt; reads never mint points or reverify ABR."""
    ledger = update_ledger(draft) if draft.published_at else None
    return reward_receipt(ledger, user=user) if ledger is not None else None


def has_monthly_completion(draft, start, end):
    """Count other approvals and retained ledgers, including deleted/legacy updates."""
    company_ids = VibeRaisingCompany.objects.filter(
        organization_id=draft.organization_id,
    ).values_list("pk", flat=True)
    keys = Q(idempotency_key__startswith=f"monthly_update_reward:organization:{draft.organization_id}:")
    for company_id in company_ids:
        keys |= Q(idempotency_key__startswith=f"monthly_update_reward:{company_id}:")
    calendar_month = Q(idempotency_key__endswith=f":month:{start:%Y-%m}") | (
        ~Q(idempotency_key__contains=":month:")
        & Q(created_at__gte=start, created_at__lt=end)
    )
    if Ledger.objects.filter(keys, calendar_month, source="STARTUP_UPDATE", kind="EARN").exists():
        return True
    # Some older unverified approvals earned no ledger. They still occupied the
    # startup's first completed update for that month.
    return MonthlyUpdateDraft.objects.filter(
        organization_id=draft.organization_id, published_at__isnull=False,
    ).exclude(pk=draft.pk).filter(
        Q(first_published_at__gte=start, first_published_at__lt=end)
        | Q(first_published_at__isnull=True, ready_at__gte=start, ready_at__lt=end)
    ).exists()


@transaction.atomic
def award_completion(user, company, draft, *, newly_approved=False):
    """Credit the approver once: 20 for an eligible first update, otherwise 5.

    The organisation lock serializes different drafts and founder accounts. The
    existing unique ledger key survives edits and retries without a migration.
    Call inside the approval transaction so wallet failures roll back approval.
    """
    from roo.services import PointsService
    from startup_updates.reward_eligibility import startup_reward_eligibility

    if user is None or company is None or draft is None or not company.organization_id:
        raise ValueError("An owned, approved startup update is required for rewards.")
    Organization.objects.select_for_update().get(pk=company.organization_id)
    draft = MonthlyUpdateDraft.objects.select_for_update().get(pk=draft.pk)
    if draft.organization_id != company.organization_id or not draft.published_at:
        raise ValueError("Only an approved update for this startup can receive points.")
    existing = update_ledger(draft)
    if existing is not None:
        return reward_receipt(existing, user=user)

    if not newly_approved:
        # An old approval may predate rewards. Editing it must not manufacture
        # a completion or imply that an absent historical credit is pending.
        return {"points": 0, "awarded": False, "status": "unavailable",
                "month": draft.published_at.astimezone(MELBOURNE).strftime("%Y-%m"),
                "tier": "standard", "creditedToCurrentUser": False}

    # Freeze the month before ABR/website checks. A slow lookup that crosses
    # midnight must not move this approval or its ledger into a new allowance.
    first_approval = draft.first_published_at or draft.ready_at or draft.published_at
    start, end = reward_month_bounds(first_approval)
    first_this_month = not has_monthly_completion(draft, start, end)
    eligible = first_this_month and startup_reward_eligibility(company).get("eligible") is True
    amount = MONTHLY_POINTS if eligible else STANDARD_POINTS
    key = reward_key(draft, start)
    if (getattr(settings, "COMMUNITY_CHAT_VOLUNTEER_ENABLED", False)
            and getattr(settings, "COMMUNITY_CHAT_VOLUNTEER_AWARDS_ENABLED", False)):
        from community_chat.volunteer.receipts import award_startup_update
        created = award_startup_update(user, company, start.date(), draft,
            idempotency_key=key, reward_amount=amount, occurred_at=first_approval)
        ledger = update_ledger(draft)
        if ledger is None:
            raise RuntimeError("The update credit did not produce a ledger receipt.")
    else:
        ledger, created = PointsService.award(
            user=user, delta=amount, source="STARTUP_UPDATE",
            description=f"Startup Pulse update completed — {start:%B %Y}",
            created_by_slack_id=getattr(user, "slack_id", "") or "system",
            idempotency_key=key, reference_type="MONTHLY_UPDATE_DRAFT", reference_id=str(draft.pk),
        )
    return reward_receipt(ledger, user=user, awarded=created)
