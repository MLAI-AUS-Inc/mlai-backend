"""Build scoped, read-only Member progress reports from existing records."""

from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone
from django.utils.crypto import salted_hmac

from .admin_member_progress_data import (
    CATEGORY_DEFINITIONS, REGIONS, WALLET_RESIDUALS, categories, load_people, local_day, midnight,
)
from .volunteer.access import community_id
from .volunteer.ladder import MAX_LEVEL, level_for
from .volunteer.policy import MICROROO
from .volunteer.profile_progression import public_position

HARD_BADGES = frozenset({
    "build.durable", "data.reuse", "event.handoff", "event.series",
    "mentor.repeat", "mentor.successor", "ops.continuity", "partner.repeat", "partner.train",
})
LEADERSHIP_PAIRS = {
    "event.handoff": ("event.series", "event.lead"),
    "mentor.successor": ("community.induct", "mentor.session", "mentor.repeat"),
    "ops.continuity": ("ops.process",),
    "partner.train": ("partner.ship", "partner.repeat"),
}
PAGE_SIZE = 25


def _roo(amount):
    return Decimal(amount) / MICROROO


def _level(amount):
    return level_for(amount) if amount is not None else None


def _gain(ranks):
    current, opening = map(_level, ranks)
    return current - opening if current is not None and opening is not None else None


def _window(person, start, end):
    return [item for item in person.activity if start <= item.day <= end]


def _member(person, start, end, category=None):
    activity = _window(person, start, end)
    current, opening = map(_level, person.ranks)
    badges = person.badge_collection
    return dict(
        id=person.id, name=person.name, level=current, starting_level=opening,
        level_gain=_gain(person.ranks),
        roo=_roo(sum(item.amount for item in activity if item.kind == "roo")),
        badges=len(badges) if badges is not None else None,
        badges_earned=sum(item.kind == "badge" for item in activity),
        check_ins=sum(item.kind == "check_in" for item in activity),
        bookings=sum(item.kind == "booking" for item in activity),
        last_activity=max((item.day for item in activity if item.kind != "booking"), default=None),
        category_badges=(
            sum(badge["category"] == category for badge in badges)
            if category and badges is not None else None
        ),
    )


def _month_start(day, earlier):
    index = day.year * 12 + day.month - 1 - earlier
    year, month = divmod(index, 12)
    return date(year, month + 1, 1)


def _months(end, count):
    starts = [_month_start(end, offset) for offset in range(count - 1, -1, -1)]
    ends = [start - timedelta(days=1) for start in starts[1:]] + [end]
    return list(zip(starts, ends))


def _days(start, end, width):
    one_day = timedelta(days=1)
    partial = ((end - start).days + 1) % width
    stop = start + timedelta(days=(partial or width) - 1)
    while start <= end:
        yield start, stop
        start = stop + one_day
        stop = min(end, stop + timedelta(days=width))


def _undated_badges(people):
    return sum(badge["awarded_on"] is None for p in people for badge in p.badge_collection or [])


def _bucket(people, start, end, *, tracks, award_dates_available):
    amounts = Counter()
    badge_counts = Counter()
    booking_members, active = set(), set()
    check_ins = bookings = 0
    for person in people:
        for item in _window(person, start, end):
            if item.kind == "booking":
                booking_members.add(person.id)
                bookings += 1
                continue
            active.add(person.id)
            if item.kind == "roo":
                amounts[item.track] += item.amount
            elif item.kind == "badge":
                badge_counts[item.track] += 1
            elif item.kind == "check_in":
                check_ins += 1
    dates_known = award_dates_available and (bool(badge_counts) or not _undated_badges(people))
    return dict(
        start=start, end=end, roo=_roo(sum(amounts.values())),
        badges=sum(badge_counts.values()) if dates_known else None,
        bookings=bookings, booking_members=len(booking_members), check_ins=check_ins, active=len(active),
        tracks={
            key: dict(
                roo=_roo(amounts[key]),
                badges=badge_counts[key] if dates_known and key in CATEGORY_DEFINITIONS else None,
            )
            for key in (*CATEGORY_DEFINITIONS, *WALLET_RESIDUALS)
        } if tracks else {},
    )


def _booking_summary(people, start, end):
    members, days = set(), set()
    count = 0
    for person in people:
        for item in _window(person, start, end):
            if item.kind == "booking":
                count += 1
                members.add(person.id)
                days.add(item.day)
    return dict(bookings=count, members=len(members), active_days=len(days))


def _award_dates_available(people):
    return any(badge["awarded_on"] is not None for p in people for badge in p.badge_collection or [])


def _boards(people):
    boards = defaultdict(list)
    for person in people:
        for category, count in Counter(b["category"] for b in person.badge_collection or []).items():
            boards[category].append(dict(id=person.id, name=person.name, count=count))
    result = {}
    for category in CATEGORY_DEFINITIONS:
        entries = sorted(boards[category], key=lambda row: (-row["count"], row["name"].casefold(), row["id"]))
        previous_count = None
        for position, entry in enumerate(entries, 1):
            if entry["count"] != previous_count:
                rank = position
                previous_count = entry["count"]
            entry["rank"] = rank
        result[category] = entries[:5]
    return result


def _movement_sort(row):
    return -(row["level_gain"] or 0), -row["roo"], row["name"].casefold(), row["id"]


def _overview(people, end, start, windows, month, category, now):
    from .volunteer.admin_rank_history import report_ranks

    snapshot_start = end - timedelta(days=29)
    if start == snapshot_start:
        gains = sum((_gain(person.ranks) or 0) > 0 for person in people)
        gain_coverage = sum(_gain(person.ranks) is not None for person in people)
    else:
        ids = {uid for person in people for uid in person.source_ids}
        snapshots = report_ranks(ids, community_id(), midnight(snapshot_start), now)
        comparisons = [
            next((snapshots[uid] for uid in person.source_ids if uid in snapshots and snapshots[uid][0] is not None), (None, None))
            for person in people if person.ranks[0] is not None
        ]
        gains = sum((_gain(ranks) or 0) > 0 for ranks in comparisons)
        gain_coverage = sum(_gain(ranks) is not None for ranks in comparisons)
    levels = [0] * (MAX_LEVEL + 1)
    for person in people:
        level = _level(person.ranks[0])
        if level is not None:
            levels[level] += 1
    monthly_start = windows[0][0]
    dates_available = _award_dates_available(people)
    gain_rows = sorted(
        (_member(p, start, end) for p in people if (_gain(p.ranks) or 0) > 0),
        key=_movement_sort,
    )
    result = dict(
        summary=dict(
            members=len(people), levels_known=sum(levels), level_gains=gains if gain_coverage else None,
            level_gain_coverage=gain_coverage,
            attended=sum(any(item.kind == "check_in" for item in _window(p, snapshot_start, end)) for p in people),
        ),
        levels=levels, gains=gain_rows[:5],
        months=[_bucket(people, lower, upper, tracks=True, award_dates_available=dates_available) for lower, upper in windows],
        booking_summary=_booking_summary(people, monthly_start, end),
        badge_leaders=_boards(people), undated_badges=_undated_badges(people),
        badges_published=sum(p.badge_collection is not None for p in people),
    )
    if month:
        month_end = min(_month_start(month, -1) - timedelta(days=1), end)
        records = []
        for person in people:
            rows = _window(person, month, month_end)
            if category:
                rows = [entry for entry in rows if entry.track == category]
            earned = sum(entry.amount for entry in rows if entry.kind == "roo")
            awarded = sum(entry.kind == "badge" for entry in rows)
            bookings = sum(entry.kind == "booking" for entry in rows)
            if earned or awarded or bookings:
                records.append(dict(
                    id=person.id, name=person.name, roo=_roo(earned),
                    badges=awarded if dates_available else None, bookings=bookings,
                ))
        result["month_members"] = sorted(
            records, key=lambda row: (-row["roo"], -(row["badges"] or 0), -row["bookings"], row["name"].casefold(), row["id"]),
        )[:50]
        result["month_members_total"] = len(records)
    return result


def _locations(people, region, start, end, days):
    group = [p for p in people if p.region == region]
    coverage = Counter(p.region for p in people)
    comparable = sum(_gain(p.ranks) is not None for p in group)
    dates_available = _award_dates_available(people)
    activity = _bucket(group, start, end, tracks=False, award_dates_available=dates_available)
    return dict(
        region=region, regions=[dict(id=key, label=label) for key, label in REGIONS.items()],
        coverage=dict(
            known_regions=sum(coverage[key] for key in REGIONS if key not in {"UNKNOWN", "OUT"}),
            not_provided=coverage["UNKNOWN"], other_answers=coverage[None], total=len(people),
        ),
        summary=dict(
            members=len(group), active=activity["active"],
            levels_known=comparable, check_ins=activity["check_ins"],
            level_gains=sum((_gain(p.ranks) or 0) > 0 for p in group) if comparable else None,
        ),
        buckets=[
            _bucket(group, lower, upper, tracks=False, award_dates_available=dates_available)
            for lower, upper in _days(start, end, 1 if days == 7 else 7)
        ],
        booking_summary=_booking_summary(group, start, end), undated_badges=_undated_badges(group),
    )


def _leadership(people, end):
    since = end - timedelta(days=89)
    hard_earned, emerging = [], []
    for person in people:
        badges = {badge["id"]: badge for badge in person.badge_collection or []}
        candidate = dict(id=person.id, name=person.name)
        difficult = [badge for badge in badges.values() if badge["id"] in HARD_BADGES]
        if difficult:
            hard_earned.append(dict(**candidate, badges=difficult))
        pairs = []
        for outcome_key, anchors in LEADERSHIP_PAIRS.items():
            outcome = badges.get(outcome_key)
            if not outcome or outcome["awarded_on"] is None:
                continue
            for key in anchors:
                anchor = badges.get(key)
                if anchor and anchor["awarded_on"] and since <= anchor["awarded_on"] < outcome["awarded_on"] <= end:
                    pairs.append((anchor, outcome))
        if pairs:
            anchor, outcome = max(pairs, key=lambda pair: (pair[1]["awarded_on"], pair[0]["awarded_on"]))
            emerging.append(dict(**candidate, anchor=anchor, outcome=outcome))
    return dict(
        hard_earned=sorted(hard_earned, key=lambda row: (-len(row["badges"]), row["name"].casefold(), row["id"]))[:50],
        emerging=sorted(emerging, key=lambda row: (-row["outcome"]["awarded_on"].toordinal(), row["name"].casefold(), row["id"]))[:50],
        undated_badges=_undated_badges(people),
    )


def _members(people, filters, start, end):
    region, category, level = filters["region"], filters["category"], filters["level"]
    group = [p for p in people if region is None or p.region == region]
    rows = [_member(p, start, end, category=category) for p in group]
    summary = dict(
        members=len(group), active=sum(row["last_activity"] is not None for row in rows),
        roo=_roo(sum(a.amount for p in group for a in _window(p, start, end) if a.kind == "roo")),
        check_ins=sum(row["check_ins"] for row in rows), bookings=sum(row["bookings"] for row in rows),
    )
    if level is not None:
        expected = None if level == "pending" else level
        rows = [row for row in rows if row["level"] == expected]
    if category:
        rows = [row for row in rows if row["category_badges"]]
    if filters["q"]:
        rows = [row for row in rows if filters["q"].casefold() in row["name"].casefold()]
    metric_field = {
        "movement": "level_gain", "points": "roo", "attendance": "check_ins",
        "newBadge": "badges_earned", "recent": "last_activity", "bookings": "bookings",
    }.get(filters["metric"])
    if metric_field == "last_activity":
        rows = [row for row in rows if row["last_activity"] is not None]
    elif metric_field:
        rows = [row for row in rows if (row[metric_field] or 0) > 0]

    def sort_key(row):
        if category:
            value = -row["category_badges"]
        elif filters["sort"] == "movement":
            return _movement_sort(row)
        elif filters["sort"] == "recent":
            value = -row["last_activity"].toordinal() if row["last_activity"] else 0
        else:
            field = {"points": "roo", "attendance": "check_ins", "badges": "badges", "bookings": "bookings"}.get(filters["sort"])
            value = -(row[field] or 0) if field else 0
        return value, row["name"].casefold(), row["id"]

    rows.sort(key=sort_key)
    page = filters["page"]
    offset = (page - 1) * PAGE_SIZE
    return dict(members=rows[offset:offset + PAGE_SIZE], total=len(rows), page=page, page_size=PAGE_SIZE, summary=summary)


def _detail(people, member_id, start, end, month):
    person = next((p for p in people if p.id == member_id), None)
    if person is None:
        return None
    amounts = person.ranks[0]
    position = public_position(amounts) if amounts is not None else {}
    rank = position.get("rank")
    activity_start = month or start
    activity_end = min(_month_start(month, -1) - timedelta(days=1), end) if month else end
    summary = _member(person, activity_start, activity_end)
    if month:
        summary.update(starting_level=None, level_gain=None)
    return dict(member=dict(
        **summary, badge_collection=person.badge_collection,
        activity_start=activity_start, activity_end=activity_end,
        activity=[
            dict(
                id=salted_hmac("member-progress-activity", f"{person.id}/{a.kind}/{a.identity}").hexdigest(),
                date=a.day, kind=a.kind, label=a.label, roo=_roo(a.amount) if a.kind == "roo" else None,
            )
            for a in sorted(_window(person, activity_start, activity_end), key=lambda item: (item.day, item.kind, item.identity), reverse=True)
        ][:200],
        contribution_roo=_roo(amounts) if amounts is not None else None,
        rank=rank.get("name") if isinstance(rank, dict) else rank,
        next_level=position.get("next_level"), roo_to_next_level=Decimal(position["points_to_next_level"]) if position.get("next_level") else None,
    ))


def member_progress(filters, now=None):
    """Build the requested server-gated payload with a common Melbourne cutoff."""
    now = now or timezone.now()
    end = local_day(now)
    days = filters["days"]
    start = end - timedelta(days=days - 1)
    view = filters["view"]
    month = filters["month"]
    windows = _months(end, filters["months"])
    lower = start
    if view == "overview":
        lower = min(lower, windows[0][0], end - timedelta(days=29))
    if month is not None:
        if month > end or month < _month_start(end, 5):
            return None
        lower = min(lower, month)
    people = load_people(lower, start, now, include_regions=view == "locations" or bool(filters["region"]))
    if view == "overview":
        result = _overview(people, end, start, windows, month, filters["category"], now)
    elif view == "locations":
        result = _locations(people, filters["region"] or "VIC", start, end, days)
    elif view == "leadership":
        result = _leadership(people, end)
    elif view == "member":
        result = _detail(people, filters["id"], start, end, month)
    else:
        result = _members(people, filters, start, end)
    if result is None:
        return None
    return dict(as_of=end, refreshed_at=now, start=start, days=days, categories=categories(), **result)
