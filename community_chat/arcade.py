"""MLAI Arcade entitlements, one server-decided result for each earned round."""

import secrets

from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from roo.services import PointsService

from .volunteer.access import community_id
from .volunteer.ladder import level_for
from .volunteer.models import VolunteerSourceReceipt

ORIGIN = "mlai_arcade"
POINTS = 5


def _earn_round(user, key, source):
    # The scoped source key is unique even when a publication or award is replayed.
    round_, _ = VolunteerSourceReceipt.objects.get_or_create(
        community=community_id(),
        source_key=f"arcade:{key}",
        defaults=dict(actor=user, origin=ORIGIN, kind="round", source=source,
                      occurred_at=timezone.now(), status="ready"),
    )
    return round_


def earn_pulse_round(user, draft_id):
    """Grant a pull to the publisher of a newly completed Pulse update."""
    return _earn_round(user, f"pulse:{draft_id}", {"trigger": "pulse"})


def earn_level_rounds(user, after, gained):
    """Grant each newly crossed member-facing ladder level once, including jumps."""
    if after is None or gained <= 0:
        return
    previous = level_for(max(0, after - gained))
    for level in range(previous + 1, level_for(after) + 1):
        _earn_round(user, f"level:{user.pk}:{level}", {"trigger": "level", "level": level})


def earn_ledger_levels(user, ledger):
    """Observe contributions that can move the profile ladder before recognition."""
    from .volunteer.credits import COUNTS, classify, ledger_amount, one_record_enabled
    from .volunteer.services import contribution_total, state_for

    if one_record_enabled():
        # Count at original issuance. Mirroring an old wallet row into the
        # contribution history does not move the ladder a second time.
        if classify(ledger)[0] != COUNTS:
            return
    else:
        # Under the legacy calculation verified startup-support ledgers count
        # even before a VolunteerRecognition is available.
        if ledger.description != "link-love":
            return
        from roo.system_rewards import legacy_boost_ledgers
        if not legacy_boost_ledgers(user).filter(pk=ledger.pk).exists():
            return
    state = state_for(user, before_ledger=ledger)
    earn_level_rounds(user, contribution_total(user, state), ledger_amount(ledger))


def rounds_for(user):
    """Only the signed-in member can retrieve or advance their scoped rounds."""
    return VolunteerSourceReceipt.objects.filter(
        actor_id=user.pk, community=community_id(), origin=ORIGIN, kind="round",
    )


def round_payload(round_):
    """Return member-visible result fields; inventory records stay private."""
    prize = round_.metadata.get("prize")
    result = {"id": str(round_.pk), "trigger": round_.source["trigger"], "prize": prize}
    if result["trigger"] == "level":
        result["level"] = round_.source["level"]
    if prize == "roo_points":
        result["points"] = round_.metadata["points"]
    if prize == "event_ticket":
        from .arcade_inventory import ticket_payload
        result.update(ticket_payload(round_))
    return result


def draw_prize():
    """A one-percent Codex draw precedes the stock and cadence checks."""
    draw = secrets.randbelow(100)
    if draw < 1:
        return "codex_credits"
    if draw < 9:
        return "event_ticket"
    if draw < 65:
        return "roo_points"
    return "none"


@transaction.atomic
def spin(user, round_id):
    """Persist the draw, stock reservation or wallet credit in the same transaction."""
    from .arcade_inventory import reserve_prize

    # Match the member-before-receipt order in the reward and moderation services.
    member = get_object_or_404(get_user_model().objects.select_for_update(), pk=user.pk, is_active=True)
    row = get_object_or_404(rounds_for(member).select_for_update(), pk=round_id)
    if row.status == "ready":
        prize = draw_prize()
        result = {}
        if prize in ("codex_credits", "event_ticket"):
            stock = reserve_prize(row, prize, timezone.now())
            if stock is None:
                prize = "roo_points"
            else:
                result.update(stock)
        if prize == "roo_points":
            from .volunteer.services import state_for
            # Establish a fresh member's opening before this wallet-only credit.
            state_for(member)
            ledger, _ = PointsService.award(
                user=member, delta=POINTS, source="EVENT", description="MLAI Arcade prize",
                created_by_slack_id="mlai-arcade", idempotency_key=f"arcade:points:{row.pk}",
                reference_type="MLAI_ARCADE", reference_id=str(row.pk),
            )
            result.update(points=POINTS, ledgerId=ledger.pk)
        row.metadata = {**result, "prize": prize}
        row.status = "revealed"
        row.save(update_fields=("metadata", "status", "updated_at"))
    return round_payload(row)
