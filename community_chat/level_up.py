"""Private presentation and pending-play projection for the contribution Profile."""

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Window
from django.http import Http404
from django.utils import timezone

from . import arcade
from .roo_avatar.policy import CATALOG
from .roo_avatar.services import owned_items_for
from .volunteer.access import flag
from .volunteer.ladder import level_for, rank_for
from .volunteer.services import contribution_total

ACTION_COPY = {
    "helpful_answer": "A member marked your answer as useful.",
    "introduce_yourself": "You introduced yourself to the community.",
    "boost_startup": "You helped an MLAI startup get noticed.",
}


def _queue(rows):
    first = rows.annotate(queue_count=Window(expression=Count("pk"))).first()
    return (first.queue_count, arcade.round_payload(first)) if first else (0, None)


def _celebration(user, rounds, level):
    crossings = [
        row for row in rounds
        if row.source["level"] <= level
        and (receipt := row.metadata.get("level_celebration")) is not None
        and not receipt.get("seen_at")
    ]
    if not crossings:
        return None
    crossed = {row.source["level"] for row in crossings}
    start, end = min(crossed) - 1, max(crossed)
    rank = rank_for(end)
    owned = owned_items_for(user)
    linked = next((row for row in reversed(crossings) if row.status == "ready"), None)
    return {
        "roundIds": [str(row.pk) for row in crossings],
        "fromLevel": start,
        "level": end,
        "rank": rank["name"],
        "newRank": rank["key"] != rank_for(start)["key"],
        "reason": ACTION_COPY.get(crossings[-1].metadata["level_celebration"].get("action")),
        "wardrobe": [
            {"name": item.name, "price": item.price}
            for items in CATALOG.values()
            for item in items
            if item.required_level in crossed and item.id not in owned
        ],
        "nextPlay": arcade.round_payload(linked) if linked else None,
    }


def summary(user):
    """Keep exact play and unread-result counts independent of the legacy list cap."""
    rows = arcade.rounds_for(user).order_by("created_at", "id")
    available, next_play = _queue(rows.filter(status="ready"))
    unread_results, next_result = _queue(rows.filter(status="revealed"))
    total = contribution_total(user) if flag("enabled") else None
    level = level_for(total) if total is not None else None
    return {
        "relayUrl": settings.COMMUNITY_CHAT_RELAY_URL,
        "level": level,
        "celebration": _celebration(user, rows.filter(source__trigger="level"), level)
        if level is not None else None,
        "available": available,
        "unreadResults": unread_results,
        "nextPlay": next_play,
        "nextResult": next_result,
    }


@transaction.atomic
def acknowledge(user, ids):
    """Record exactly the owned crossings shown, safely alongside a concurrent spin."""
    rows = list(arcade.rounds_for(user).select_for_update().filter(
        pk__in=ids, source__trigger="level",
    ).order_by("pk"))
    if len(rows) != len(set(ids)) or any("level_celebration" not in row.metadata for row in rows):
        raise Http404
    now = timezone.now().isoformat()
    for row in rows:
        receipt = row.metadata["level_celebration"]
        if not receipt.get("seen_at"):
            row.metadata["level_celebration"] = {**receipt, "seen_at": now}
            row.save(update_fields=("metadata", "updated_at"))
