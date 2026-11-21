"""Read-only, member-scoped history for Profile and Roo Points."""

from django.db.models import BigIntegerField, Q, Sum, Value
from django.db.models.functions import Cast, Coalesce

from roo.models import Ledger

from .access import community_id
from .credits import one_record_enabled, opening_entries, rank_activity
from .ladder import level_for
from .models import VolunteerMemberState
from .policy import MICROROO, roo


def history_row(entry_id, title, amount, occurred_at, kind, detail=""):
    return dict(
        id=entry_id,
        title=title,
        detail=detail,
        kind=kind,
        occurred_at=occurred_at.isoformat() if occurred_at else None,
        amount_roo=roo(amount),
    )


def historical_activity(user, state):
    entries = list(opening_entries(user))
    if not entries and state.historical_microroo:
        amount = state.historical_microroo
        yield amount, history_row(
            "opening", "Historical contribution credit", amount, None, "historical",
            "Previously recognised contribution points carried into this profile.",
        )
    for entry in entries:
        evidence = entry.evidence
        area = evidence.get("area")
        title = area.replace("_", " ").replace("-", " ").strip().capitalize() if isinstance(area, str) else ""
        basis = evidence.get("basis")
        amount = entry.amount_microroo
        yield amount, history_row(
            f"opening:{entry.pk}", title or "Historical contribution credit", amount,
            None, "historical", basis[:500] if isinstance(basis, str) else "",
        )
        if entry.reversed_at:
            yield -amount, history_row(
                f"opening-reversal:{entry.pk}", f"Correction · {title or 'Historical contribution credit'}",
                -amount, entry.reversed_at, "correction",
            )


def contribution_rows(user):
    """Project the same credits used to calculate the live contribution level."""
    if not one_record_enabled():
        return None, []
    state = VolunteerMemberState.objects.filter(community=community_id(), user=user).first()
    if state is None or (state.historical_microroo is None and not opening_entries(user).exists()):
        return None, []

    rows = list(historical_activity(user, state))
    for kind, record, amount in rank_activity(user, state, with_details=True):
        if not amount:
            continue
        if kind == "ledger":
            title = record.get_source_display()
            detail = (record.description or record.reason or "").strip()[:500]
            rows.append((amount, history_row(
                f"ledger:{record.pk}", title, amount, record.created_at, "contribution",
                detail if detail != title else "",
            )))
        else:
            title = record.policy_snapshot.get("title") or (
                record.opportunity.title if record.opportunity else "Recognised contribution"
            )
            when = record.ledger.created_at if record.ledger_id and kind == "contribution" else record.updated_at
            if kind == "correction":
                title = f"Correction · {title}"
            rows.append((amount, history_row(
                f"recognition:{record.pk}", title, amount, when, kind,
            )))

    total = 0
    results = []
    for amount, row in sorted(rows, key=lambda item: (item[1]["occurred_at"] or "", item[1]["id"])):
        previous_level = level_for(max(0, total))
        total += amount
        row["progress_roo"] = roo(max(0, total))
        level = level_for(max(0, total))
        if row["occurred_at"] and level != previous_level:
            row["level"] = level
        results.append(row)
    return roo(max(0, total)), list(reversed(results))


def wallet_page(user, offset, limit):
    """List earned wallet credits without counting purchases or balance transfers."""
    rows = (
        Ledger.objects.filter(user=user)
        .filter(Q(kind="EARN") | Q(kind__isnull=True))
        .exclude(source="purchased_topup")
        .exclude(reference_type__in=("POINTS_PURCHASE", "APPLE_IAP", "ACCOUNT_MERGE"))
        .annotate(amount=Coalesce(
            "delta_microroo", "points_delta_microroo",
            Cast(Coalesce("delta", "points_delta", Value(0)), BigIntegerField()) * MICROROO,
            output_field=BigIntegerField(),
        ))
        .filter(amount__gt=0)
    )
    total = rows.aggregate(total=Sum("amount"))["total"] or 0
    page = rows.order_by("-created_at", "-pk")[offset:offset + limit + 1]
    results = []
    for ledger in page:
        title = ledger.get_source_display()
        if ledger.reference_type == "VOLUNTEER_LEVEL_BONUS":
            title = "Level bonus"
        detail = (ledger.description or ledger.reason or "").strip()[:500]
        results.append(history_row(
            f"ledger:{ledger.pk}", title, ledger.amount, ledger.created_at, "wallet",
            detail if detail != title else "",
        ))
    return roo(total), results
