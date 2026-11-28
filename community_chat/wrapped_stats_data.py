"""Read one calendar year of member records for the MLAI Wrapped stat sheet.

The roster, dated activity, ranks and badges come from the Admin Member
progress loader (``load_people``) with a calendar-year window, so Wrapped and
the progress report agree on who a member is. This module adds the year's
ledger split (earned versus paid, see ``wrapped_stats``), Luma event check-ins,
mentoring sessions and preferred names, then reduces each person to counts.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.db.models import Q

from crm.models import EventRegistration
from mentoring.models import MentorSession
from roo.models import Ledger, PointsAdmin, PointsRequest

from .admin_member_progress_data import load_people, local_day, midnight
from .onboarding_models import CommunityMemberProfile
from .volunteer.access import community_id
from .volunteer.ladder import level_for, rank_for
from .volunteer.models import VolunteerRecognition
from .wrapped_stats import FALLBACK_NAME, WrappedPerson, is_monthly_update, points_bucket

SCOPES = ("committee", "members", "all")
CHUNK = 500


def year_bounds(year, now):
    """Calendar ``year`` in Melbourne time, cut off at ``now`` while it is running."""
    start = date(year, 1, 1)
    return start, min(now, midnight(date(year + 1, 1, 1)) - timedelta(microseconds=1))


def _chunks(values):
    values = list(values)
    for index in range(0, len(values), CHUNK):
        yield values[index:index + CHUNK]


def committee_account_ids():
    """Accounts on the active committee (``PointsAdmin`` role ``committee``)."""
    rows = list(PointsAdmin.objects.filter(is_active=True, role="committee").values_list("user_id", "slack_user_id"))
    slack_ids = {slack_id for _, slack_id in rows if slack_id}
    linked = get_user_model().objects.filter(slack_id__in=slack_ids).values_list("pk", flat=True)
    return {user_id for user_id, _ in rows if user_id} | set(linked)


def member_account_ids(profile_ids):
    """Accounts for explicit MLAI Chat public profile IDs; unknown IDs are returned separately."""
    found = dict(get_user_model().objects.filter(
        community_chat_profile_id__in=list(profile_ids),
    ).values_list("community_chat_profile_id", "pk"))
    found = {str(key): value for key, value in found.items()}
    return set(found.values()), sorted(set(map(str, profile_ids)) - set(found))


def _committee_roster_names():
    # The President-supplied 2026 committee names, keyed by account email.
    from roo.management.commands.sync_committee_points_admins import COMMITTEE

    return {email.lower(): name for name, email in COMMITTEE}


def _clean(*parts):
    return " ".join(" ".join(part or "" for part in parts).split())


def preferred_names(people, overrides):
    """Return {account_id: (name, source)}: an admin override, the member's own
    MLAI Chat profile name, the committee roster name, then the account name."""
    owners = {person.account_id: person for person in people}
    accounts = {uid: person for person in people for uid in person.source_ids}
    chat_names = {}
    for uid, first, last in CommunityMemberProfile.objects.filter(
        user_id__in=list(accounts),
    ).values_list("user_id", "first_name", "last_name"):
        if _clean(first, last):
            chat_names[uid] = _clean(first, last)
    roster = _committee_roster_names()
    roster_names = {}
    for uid, email in get_user_model().objects.filter(pk__in=list(accounts)).values_list("pk", "email"):
        if (email or "").lower() in roster:
            roster_names[uid] = roster[email.lower()]

    names = {}
    for owner_id, person in owners.items():
        ordered = [owner_id, *sorted(person.source_ids - {owner_id})]
        override = _clean(overrides.get(person.id, ""))
        chat = next((chat_names[uid] for uid in ordered if uid in chat_names), "")
        listed = next((roster_names[uid] for uid in ordered if uid in roster_names), "")
        if override:
            names[owner_id] = (override, "override")
        elif chat:
            names[owner_id] = (chat, "chat_profile")
        elif listed:
            names[owner_id] = (listed, "committee_roster")
        elif person.name != FALLBACK_NAME:
            names[owner_id] = (person.name, "account")
        else:
            names[owner_id] = (FALLBACK_NAME, "missing")
    return names


def _ledger_facts(pks):
    rows, actions, minutes = {}, {}, {}
    scope = community_id()
    for chunk in _chunks(pks):
        rows.update({row["pk"]: row for row in Ledger.objects.filter(pk__in=chunk).values(
            "pk", "source", "reference_type", "description",
        )})
        actions.update(VolunteerRecognition.objects.filter(
            community=scope, status="approved", ledger_id__in=chunk,
        ).values_list("ledger_id", "action_key"))
        for ledger_id, details in PointsRequest.objects.filter(
            ledger_entry_id__in=chunk, status="approved", manual_time_key__isnull=False,
        ).values_list("ledger_entry_id", "manual_time"):
            value = details.get("volunteer_minutes") if isinstance(details, dict) else None
            if type(value) is int and value > 0:
                minutes[ledger_id] = value
    return rows, actions, minutes


def _event_check_ins(selected, start, end_at):
    """Distinct Luma events each person checked in to, joined by account or email."""
    accounts = {uid: person for person, _ in selected for uid in person.source_ids}
    account_emails = {
        uid: email.lower()
        for uid, email in get_user_model().objects.filter(pk__in=list(accounts)).values_list("pk", "email")
        if email
    }
    emails = {email: accounts[uid] for uid, email in account_emails.items()}
    seen = {}
    for chunk in _chunks(accounts):
        chunk_emails = [account_emails[uid] for uid in chunk if uid in account_emails]
        for user_id, email, event_id, checked_in in EventRegistration.objects.filter(
            Q(contact__user_id__in=chunk) | Q(contact__user__isnull=True, contact__email__in=chunk_emails),
            checked_in_at__gte=midnight(start), checked_in_at__lte=end_at, removed_at__isnull=True,
        ).values_list("contact__user_id", "contact__email", "event_id", "checked_in_at").iterator():
            person = accounts.get(user_id) if user_id else emails.get((email or "").lower())
            if person is not None:
                seen.setdefault((person.account_id, event_id), local_day(checked_in))
    return seen


def _mentoring(selected, start, end_at):
    accounts = {uid: person.account_id for person, _ in selected for uid in person.source_ids}
    given, received = set(), set()
    for chunk in _chunks(accounts):
        for session_id, mentor_id, founder_id in MentorSession.objects.filter(
            Q(mentor__user_id__in=chunk) | Q(founder_id__in=chunk),
            status=MentorSession.STATUS_COMPLETED,
            starts_at__gte=midnight(start), starts_at__lte=end_at,
        ).values_list("pk", "mentor__user_id", "founder_id"):
            if mentor_id in accounts:
                given.add((accounts[mentor_id], session_id))
            if founder_id in accounts:
                received.add((accounts[founder_id], session_id))
    return given, received


def _badges(person, year):
    if person.badge_collection is None:
        return None, ""
    dated = [
        (badge["awarded_on"] or badge["recorded_on"], badge["name"])
        for badge in person.badge_collection
        if (badge["awarded_on"] or badge["recorded_on"]).year == year
    ]
    return len(dated), max(dated)[1] if dated else ""


def _ranks(person):
    current, opening = (level_for(value) if value is not None else None for value in person.ranks)
    rank = rank_for(current)["name"] if current is not None else ""
    climbed = current - opening if current is not None and opening is not None else None
    return current, rank, climbed


def _select(people, scope, account_ids):
    if scope == "all":
        return list(people), set()
    chosen = [person for person in people if person.source_ids & account_ids]
    covered = {uid for person in chosen for uid in person.source_ids}
    return chosen, account_ids - covered


def wrapped_people(year, now, *, scope="committee", member_ids=(), overrides=None):
    """Return (WrappedPerson list, report of requested accounts with no qualifying record)."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    start, end_at = year_bounds(year, now)
    unknown_ids = []
    if scope == "committee":
        account_ids = committee_account_ids()
    elif scope == "members":
        account_ids, unknown_ids = member_account_ids(member_ids)
    else:
        account_ids = set()
    people = load_people(start, start, end_at, include_regions=False)
    chosen, uncovered = _select(people, scope, account_ids)
    names = preferred_names(chosen, overrides or {})
    selected = []
    for person in chosen:
        name, source = names[person.account_id]
        badges, latest = _badges(person, year)
        level, rank, climbed = _ranks(person)
        selected.append((person, WrappedPerson(
            id=person.id, name=name, name_source=source, badges=badges, latest_badge=latest,
            level=level, rank=rank, levels_climbed=climbed,
        )))

    credits = [item for person, _ in selected for item in person.activity if item.kind == "roo"]
    rows, actions, minutes = _ledger_facts({int(item.identity) for item in credits})
    for person, wrapped in selected:
        for item in person.activity:
            if not start <= item.day <= local_day(end_at):
                continue
            if item.kind in ("roo", "check_in", "booking", "badge"):
                wrapped.active_days.append(item.day)
            if item.kind == "check_in":
                wrapped.volunteer_check_ins += 1
            elif item.kind == "booking":
                wrapped.coworking_days += 1
            elif item.kind == "roo":
                pk = int(item.identity)
                row = rows.get(pk, {})
                bucket, volunteering = points_bucket(row, actions.get(pk))
                wrapped.add_credit(item.amount, bucket, volunteering, minutes.get(pk))
                wrapped.monthly_updates += is_monthly_update(row)

    by_account = {person.account_id: wrapped for person, wrapped in selected}
    for (account_id, _), day in _event_check_ins(selected, start, end_at).items():
        by_account[account_id].events_attended += 1
        by_account[account_id].active_days.append(day)
    given, received = _mentoring(selected, start, end_at)
    for account_id, _ in given:
        by_account[account_id].mentoring_given += 1
    for account_id, _ in received:
        by_account[account_id].mentoring_received += 1

    profile_ids = dict(get_user_model().objects.filter(pk__in=list(uncovered)).values_list(
        "pk", "community_chat_profile_id",
    )) if uncovered else {}
    report = dict(
        unknown_profile_ids=unknown_ids,
        without_qualifying_record=sorted(str(profile_ids[uid]) for uid in uncovered if uid in profile_ids),
    )
    return [wrapped for _, wrapped in selected], report
