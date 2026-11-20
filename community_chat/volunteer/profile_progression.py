"""Member-facing rank and reviewed badge projections."""

import json
from collections import defaultdict
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

from . import ladder
from .access import community_id, flag
from .credits import one_record_enabled
from .models import VolunteerMemberState, VolunteerSourceReceipt
from .policy import roo

BADGE_ORIGIN = "volunteer_badges"
BADGE_KIND = "badge_grant"
BADGE_STATUS = "awarded"
PROFILE_KIND = "profile_publication"
PROFILE_STATUS = "published"


def profile_contributions_enabled():
    return flag("enabled") and flag("profile_contributions_enabled") and one_record_enabled()


@cache
def badge_catalogue():
    with Path(__file__).with_name("badge_catalogue.json").open(encoding="utf-8") as file:
        return {badge["id"]: badge for badge in json.load(file)}


def public_evidence_url(value):
    """Accept only public event references."""
    if not isinstance(value, str) or len(value) > 600 or any(c.isspace() for c in value):
        return None
    try:
        link = urlsplit(value)
        if (link.scheme == "https" and link.netloc in {"luma.com", "lu.ma", "events.humanitix.com"}
                and link.path and not link.query and not link.fragment):
            return value
    except ValueError:
        pass
    return None


def public_position(total):
    position = ladder.position(total)
    if position is None:
        return None
    return {key: position[key] for key in (
        "policy", "level", "rank", "next_level", "points_to_next_level", "next_rank", "points_to_next_rank", "progress",
    )}


def _badge(key, reason, evidence=None):
    definition = badge_catalogue()[key]
    return dict(id=key, name=definition["name"], track=definition["track"],
                criterion=definition["criterion"], reason=reason, evidence_url=public_evidence_url(evidence))


def profile_progressions(users):
    """Project only member-safe fields from authoritative scoped records."""
    from .services import contribution_total

    if not users or not profile_contributions_enabled():
        return {}
    scope = community_id()
    published_ids = set(VolunteerSourceReceipt.objects.filter(
        community=scope, origin=BADGE_ORIGIN, kind=PROFILE_KIND,
        status=PROFILE_STATUS, actor_id__in=users,
    ).values_list("actor_id", flat=True))
    if not published_ids:
        return {}

    states = {
        state.user_id: state for state in VolunteerMemberState.objects.filter(community=scope, user_id__in=published_ids)
    }
    badges = defaultdict(dict)
    for grant in VolunteerSourceReceipt.objects.filter(
        community=scope, origin=BADGE_ORIGIN, kind=BADGE_KIND, status=BADGE_STATUS, actor_id__in=published_ids,
    ).order_by("source_key"):
        key = grant.metadata.get("badge_id")
        reason = grant.metadata.get("public_reason")
        if key in badge_catalogue() and isinstance(reason, str) and reason.strip():
            badges[grant.actor_id][key] = _badge(key, reason, grant.metadata.get("public_url"))
    result = {}
    for uid, state in states.items():
        total = contribution_total(users[uid], state)
        if total is None:
            continue
        result[uid] = dict(
            rank=public_position(total),
            contribution_roo=roo(total),
            badges=sorted(badges[uid].values(), key=lambda badge: (badge["track"], badge["name"])),
        )
    return result
