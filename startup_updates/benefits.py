"""Shared, read-only benefit anchors and durable startup/month reward keys."""
from django.db.models import Min, Q


def has_monthly_update_discount(user):
    """Reuse the founder's existing 30-day monthly-update benefit today."""
    from zoneinfo import ZoneInfo
    from django.utils import timezone
    from roo.services import CoworkingService

    return CoworkingService._has_ready_monthly_update(
        user, timezone.localdate(timezone.now(), ZoneInfo("Australia/Melbourne")),
    )


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


def approval_is_shared(approval):
    """Only an exact approved community/public revision proves sharing.

    Working-copy audience changes are not approval and cannot unlock benefits.
    """
    revision = approval.revision
    return (
        revision.audience in {"community", "public"}
        and approval.audience_visibility == [revision.audience]
        and approval.content_hash == revision.content_hash
    )


def published_update_is_shared(draft):
    """Check the saved publication receipt, never a client or working memo."""
    from startup_updates.models import MonthlyUpdateApproval

    approval = MonthlyUpdateApproval.objects.filter(
        revision_id=draft.published_revision_id,
    ).select_related("revision").first()
    return approval is not None and approval_is_shared(approval)


def discount_approved_update_at(draft):
    """Retain earned windows, requiring sharing for post-policy approvals.

    Sharing a previously private update can unlock the remainder of its original
    30 days. Later edits, privacy changes and reapproval never restart the clock.
    Legacy publications without canonical receipts keep their original window.
    """
    from startup_updates.models import MonthlyUpdateApproval

    anchor = approved_update_at(draft)
    if anchor is None:
        return None
    approvals = list(MonthlyUpdateApproval.objects.filter(
        revision__draft_id=draft.pk,
    ).select_related("revision"))
    grandfathered = not approvals or any(not approval.requires_shared_update for approval in approvals)
    if grandfathered:
        # A legacy publication's first canonical receipt can be newer than the
        # benefit it records. Converting it must not restart its old window.
        legacy_anchor = (getattr(draft, "first_published_at", None)
            or draft.ready_at or draft.published_at)
        return min(anchor, legacy_anchor) if legacy_anchor is not None else anchor
    if any(approval_is_shared(approval) for approval in approvals):
        return anchor
    return None
