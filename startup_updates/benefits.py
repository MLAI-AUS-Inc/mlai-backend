"""Shared, read-only benefit anchors and durable startup/month reward keys."""
from django.db.models import Min, Q


def monthly_reward_key(organization_id, month):
    """Return a key retained when a draft or company wrapper is recreated."""
    return f"monthly_update_reward:organization:{organization_id}:{month:%Y-%m}"


def monthly_reward_history(organization_id, month):
    """Find canonical and pre-existing company/month payments for a startup."""
    from founder_tools.models import VibeRaisingCompany
    from roo.models import Ledger

    company_ids = VibeRaisingCompany.objects.filter(
        organization_id=organization_id,
    ).values_list("pk", flat=True)
    legacy_keys = [f"monthly_update_reward:{pk}:{month:%Y-%m}" for pk in company_ids]
    return Ledger.objects.filter(
        Q(idempotency_key=monthly_reward_key(organization_id, month))
        | Q(idempotency_key__in=legacy_keys),
        source="STARTUP_UPDATE", kind="EARN",
    ).order_by("created_at", "pk")


def approved_update_at(draft):
    """Resolve the first human approval, retaining old paid recreation windows.

    Older generators stamped ready_at before founder review. Immutable approval
    records are authoritative where available; legacy publications fall back
    to their stored timestamp. A recreated draft cannot use a newer approval
    to renew the window retained by its original monthly reward ledger.
    """
    if not draft.published_at:
        return None
    if hasattr(draft, "first_approved_at"):
        first = draft.first_approved_at
    else:
        first = draft.revisions.aggregate(first=Min("approval__approved_at"))["first"]
    anchor = first or getattr(draft, "first_published_at", None) or draft.ready_at or draft.published_at
    original = monthly_reward_history(draft.organization_id, draft.month).exclude(
        reference_type="MONTHLY_UPDATE_DRAFT", reference_id=str(draft.pk),
    ).first()
    return min(anchor, original.created_at) if original is not None else anchor
