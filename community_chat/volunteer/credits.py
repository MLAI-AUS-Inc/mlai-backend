"""One record per contribution: which Roo wallet credits count toward level.

Volunteer build step 2. Everything here is inert until
COMMUNITY_CHAT_VOLUNTEER_ONE_RECORD_ENABLED is on. With it on:

- every earned wallet credit has a label saying whether it counts toward a
  member's level, taken from the back-fill plan's ledger table;
- rate-card task approvals, manual awards and points requests each write one
  approved Volunteer record linked to their ledger row;
- a member's opening balance is the sum of their opening entries (or the old
  single opening number until the back-fill gives them entries);
- a credit with no label raises an admin alert and is left out of the level,
  instead of hiding the member's level as "being checked".
"""

import hashlib
import logging

from django.conf import settings
from django.db.models import Q, Sum
from django.utils import timezone

from .access import community_id

logger = logging.getLogger(__name__)

COUNTS = "counts"
NOT_LEVEL = "not_level"
UNLABELLED = "unlabelled"

# Ledger reference types and what each one is. Checked before source or
# description, as in the back-fill plan (section 5.5).
REFERENCE_LABELS = {
    "TASK_ASSIGNMENT": (COUNTS, "rate_card_task"),
    "TASK_SUBMISSION": (COUNTS, "rate_card_task"),
    "MANUAL_ADJUSTMENT": (COUNTS, "manual_award"),
    "POINTS_REQUEST": (COUNTS, "points_request"),
    "VOLUNTEER_CONTRIBUTION": (COUNTS, "volunteer_recognition"),
    "FIRST_CHANNEL_POST": (COUNTS, "first_channel_post"),
    "MONTHLY_UPDATE_DRAFT": (NOT_LEVEL, "startup_update"),
    "MENTOR_SESSION": (NOT_LEVEL, "mentor_payout"),
    "VOLUNTEER_LEVEL_BONUS": (NOT_LEVEL, "level_bonus"),
    "VOLUNTEER_CORRECTION": (NOT_LEVEL, "correction"),
    "ACCOUNT_MERGE": (NOT_LEVEL, "account_merge"),
    "POINTS_PURCHASE": (NOT_LEVEL, "purchase"),
    "APPLE_IAP": (NOT_LEVEL, "purchase"),
}

# Ledger kinds that write their own Volunteer record when they are credited.
RECORDED_KINDS = {
    "rate_card_task": "Rate-card task",
    "manual_award": "Points awarded by an admin",
    "points_request": "Approved points request",
}

# Volunteer actions whose Roo still pays but no longer counts toward level:
# reaction rewards and a founder's own startup update (main plan 3.4).
NON_LEVEL_ACTIONS = frozenset(("boost_startup", "monthly_startup_update"))


def one_record_enabled():
    """Read the step 2 switch; off unless the deployment turns it on."""
    return bool(getattr(settings, "COMMUNITY_CHAT_VOLUNTEER_ONE_RECORD_ENABLED", False))


def earned(queryset):
    """Earned rows: kind EARN, or a legacy row with no kind and a positive delta."""
    return queryset.filter(Q(kind="EARN") | Q(kind__isnull=True, delta__gt=0))


def ledger_amount(ledger):
    """The row's exact amount in microroo, falling back to the legacy fields."""
    for value in (ledger.delta_microroo, ledger.points_delta_microroo):
        if value is not None:
            return max(0, value)
    whole = ledger.delta if ledger.delta is not None else ledger.points_delta
    return max(0, (whole or 0) * 1_000_000)


def classify(ledger):
    """Return (label, kind) for one earned wallet credit."""
    if ledger.source == "purchased_topup":
        return NOT_LEVEL, "purchase"
    if ledger.reference_type in REFERENCE_LABELS:
        return REFERENCE_LABELS[ledger.reference_type]
    if ledger.reference_type:
        return UNLABELLED, "unknown_reference"
    if ledger.source == "STARTUP_UPDATE":
        return NOT_LEVEL, "startup_update"
    description = (ledger.description or "").strip()
    if description == "link-love":
        return NOT_LEVEL, "reaction"
    if description.startswith("First post in channel"):
        return COUNTS, "first_post"
    if description == "Weekly committee points remuneration":
        return NOT_LEVEL, "committee_pay"
    return UNLABELLED, "unknown"


def recognition_level_microroo(record):
    """Points one Volunteer record adds toward level."""
    if record.action_key in NON_LEVEL_ACTIONS:
        return 0
    if record.level_microroo is not None:
        return record.level_microroo
    return record.reward_microroo


def opening_microroo(user, state):
    """The member's opening balance, or None while their history is being checked."""
    from .models import VolunteerOpeningEntry

    entries = VolunteerOpeningEntry.objects.filter(community=community_id(), user=user)
    if entries.exists():
        return (
            entries.filter(reversed_at__isnull=True).aggregate(
                value=Sum("amount_microroo")
            )["value"]
            or 0
        )
    return state.historical_microroo


def has_opening_entries(user):
    from .models import VolunteerOpeningEntry

    return VolunteerOpeningEntry.objects.filter(
        community=community_id(), user=user
    ).exists()


def _later_credits(user, state):
    """Earned rows after the member's cut-off that no Volunteer record holds yet."""
    from roo.models import Ledger

    from .models import VolunteerRecognition

    known = VolunteerRecognition.objects.filter(
        community=community_id(), user=user, ledger__isnull=False
    ).values("ledger_id")
    return earned(
        Ledger.objects.filter(user=user, pk__gt=state.historical_ledger_cutoff)
    ).exclude(pk__in=known)


def unlabelled_credits(user, state):
    """Rows after the cut-off that the label table doesn't cover."""
    return [
        ledger
        for ledger in _later_credits(user, state).order_by("pk")
        if classify(ledger)[0] == UNLABELLED
    ]


def one_record_total(user, state):
    """Level points: opening, plus approved records, plus counted credits.

    Read-only: safe inside a READ ONLY transaction. Unlabelled credits are left
    out here and reported by alert_unlabelled_credits on a write path.
    """
    from .models import VolunteerRecognition

    opening = opening_microroo(user, state)
    if opening is None:
        return None
    cutoff = state.historical_ledger_cutoff
    records = VolunteerRecognition.objects.filter(community=community_id(), user=user)
    total = sum(
        recognition_level_microroo(record)
        for record in records.filter(status="approved").filter(
            Q(ledger_id__gt=cutoff) | Q(ledger__isnull=True)
        )
    )
    # A reversal of work inside the opening takes off what counted toward level.
    reversed_opening = sum(
        recognition_level_microroo(record)
        for record in records.filter(status="reversed", ledger_id__lte=cutoff)
    )
    counted = sum(
        ledger_amount(ledger)
        for ledger in _later_credits(user, state)
        if classify(ledger)[0] == COUNTS
    )
    return max(0, opening + total + counted - reversed_opening)


def alert_unlabelled_credits(user, state):
    """Record each unlabelled credit once and tell admins, without blanking a level."""
    from .models import VolunteerSourceReceipt

    alerts = 0
    for ledger in unlabelled_credits(user, state):
        _, created = VolunteerSourceReceipt.objects.get_or_create(
            community=community_id(),
            source_key=f"roo_ledger:unlabelled:{ledger.pk}",
            defaults=dict(
                origin="roo_ledger",
                kind="unlabelled_credit",
                actor=user,
                source={"source_id": f"roo-ledger:{ledger.pk}"},
                metadata=dict(
                    ledger_id=ledger.pk,
                    ledger_source=ledger.source,
                    reference_type=ledger.reference_type or "",
                ),
                occurred_at=ledger.created_at,
                status="alert",
            ),
        )
        if created:
            alerts += 1
            logger.error(
                "volunteer_unlabelled_credit ledger_id=%s source=%s reference_type=%s",
                ledger.pk,
                ledger.source,
                ledger.reference_type or "",
            )
    return alerts


def record_wallet_credit(ledger):
    """Give a counted task, manual or request credit its one Volunteer record.

    Call inside the transaction that wrote the ledger row. Does nothing while
    the step 2 flag is off, for credits of other kinds, or when the record
    already exists, so a replayed award never writes twice.
    """
    if not one_record_enabled() or ledger is None or ledger.user_id is None:
        return None
    label, kind = classify(ledger)
    if label != COUNTS or kind not in RECORDED_KINDS:
        return None
    from .models import VolunteerRecognition
    from .policy import roo
    from .services import award_milestones, lock_member, state_for

    user = lock_member(ledger.user)
    existing = VolunteerRecognition.objects.filter(ledger=ledger).first()
    if existing is not None:
        return existing
    state_for(user, before_ledger=ledger)
    amount = ledger_amount(ledger)
    key = hashlib.sha256(f"roo-ledger:{ledger.pk}".encode()).hexdigest()
    record, created = VolunteerRecognition.objects.get_or_create(
        community=community_id(),
        user=user,
        outcome_key=key,
        defaults=dict(
            action_key=kind,
            source={"source_id": f"roo-ledger:{ledger.pk}"},
            policy_snapshot=dict(
                key=kind,
                title=RECORDED_KINDS[kind],
                cap_group=f"wallet:{kind}",
                period="none",
                cap=0,
                requires_attendance=False,
                verification="roo_ledger",
                reward_roo=roo(amount),
                reward_max_roo=roo(amount),
            ),
            occurred_at=ledger.created_at,
            note=ledger.description or "",
            status="approved",
            reward_microroo=amount,
            ledger=ledger,
            review_history=[
                dict(
                    decision="approve",
                    automatic=True,
                    actor_id=ledger.created_by_slack_id or "",
                    note="Recorded from the Roo wallet credit",
                    at=timezone.now().isoformat(),
                )
            ],
        ),
    )
    if created:
        award_milestones(user, record)
    return record


def store_level(state, total):
    """Keep the member's level and the highest level reached on their state row."""
    from .policy import VERSION, progress

    if not one_record_enabled() or total is None:
        return
    level = progress(total)["current_level"]["level"]
    highest = max(level, state.highest_level or 0)
    if (state.level, state.highest_level, state.level_policy) == (level, highest, VERSION):
        return
    state.level, state.highest_level, state.level_policy = level, highest, VERSION
    state.save(update_fields=("level", "highest_level", "level_policy", "updated_at"))
