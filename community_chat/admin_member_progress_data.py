"""Read the existing member and rewards records for the Admin progress report."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db.models import Exists, OuterRef, Q

from core.models import SlackFounderAccountLink
from roo.manual_time_rules import CATALOGUE as MANUAL_TIME_CATALOGUE
from roo.models import CoworkingBooking, Ledger, PointsRequest

from .models import AccountDeletionRequest, CommunityChatDevice, CommunityMemberProfile, DeviceBindingStatus
from .volunteer.access import community_id
from .volunteer.models import VolunteerAttendance, VolunteerRecognition, VolunteerSourceReceipt
from .volunteer.point_history import earned_wallet_credits
from .volunteer.profile_progression import (
    BADGE_KIND, BADGE_ORIGIN, BADGE_STATUS, PROFILE_KIND, PROFILE_STATUS, badge_catalogue,
)

MELBOURNE = ZoneInfo("Australia/Melbourne")
CATEGORY_DEFINITIONS = {
    "data-engineering": ("Data and engineering", ("Engineering", "Data and evaluation")),
    "community": ("Community", ("Community and mentoring", "Community care")),
    "design-product": ("Design and product", ("Product and research",)),
    "events": ("Events", ("Events",)),
    "getting-started": ("Getting started", ("Getting started",)),
    "communications": ("Communications", ("Communications",)),
    "operations": ("Operations", ("Operations",)),
    "partnerships-delivery": ("Partnerships and delivery", ("Partnerships and delivery",)),
}
BADGE_TRACKS = {
    track: key
    for key, (_, tracks) in CATEGORY_DEFINITIONS.items()
    for track in tracks
}
WALLET_RESIDUALS = ("programme-rewards", "multiple-tracks", "unassigned")

REGIONS = {
    "VIC": "Victoria", "NSW": "New South Wales", "QLD": "Queensland",
    "WA": "Western Australia", "SA": "South Australia", "TAS": "Tasmania",
    "ACT": "Australian Capital Territory", "NT": "Northern Territory",
    "OUT": "Outside Australia", "UNKNOWN": "Not provided",
}
CITY_REGIONS = {
    "Melbourne": "VIC", "Sydney": "NSW", "Brisbane": "QLD", "Perth": "WA",
    "Adelaide": "SA", "Hobart": "TAS", "Canberra": "ACT", "Darwin": "NT",
    "Outside Australia": "OUT",
}

# Only source actions with an unambiguous match. General task, event, mentoring
# and manual credit rows need reviewed provenance before they are assigned.
ACTION_TRACKS = {
    "introduce_yourself": "getting-started",
    "helpful_answer": "community",
    "boost_startup": "programme-rewards",
    "monthly_startup_update": "programme-rewards",
}
MANUAL_GROUP_TRACKS = {
    "events": "events", "community": "community", "content": "communications",
    "design": "design-product", "tech": "data-engineering", "operations": "operations",
    "partnerships": "partnerships-delivery",
}
MANUAL_ACTION_TRACKS = {
    action["id"]: MANUAL_GROUP_TRACKS.get(group["id"], "unassigned")
    for group in MANUAL_TIME_CATALOGUE["groups"] for action in group["actions"]
}


@dataclass(frozen=True)
class Activity:
    day: date
    kind: str
    identity: str
    amount: int = 0
    track: str = ""
    label: str = ""


@dataclass
class Person:
    account_id: int
    source_ids: set[int]
    id: str
    name: str
    region: str | None = None
    ranks: tuple[int | None, int | None] = (None, None)
    badge_collection: list[dict] | None = None
    activity: list[Activity] = field(default_factory=list)


def midnight(day):
    return datetime.combine(day, time.min, MELBOURNE)


def local_day(timestamp):
    return timestamp.astimezone(MELBOURNE).date()


def categories():
    counts = defaultdict(int)
    for definition in badge_catalogue().values():
        counts[BADGE_TRACKS[definition["track"]]] += 1
    return [
        dict(id=key, label=label, badge_count=counts[key])
        for key, (label, _) in CATEGORY_DEFINITIONS.items()
    ]


def _report_wallet():
    reversed_recognition = VolunteerRecognition.objects.filter(
        community=community_id(), status="reversed", ledger_id=OuterRef("pk"),
    )
    return (
        earned_wallet_credits(Ledger.objects)
        .exclude(reference_type__in=("VOLUNTEER_CORRECTION", "OPENING_BALANCE"))
        .exclude(Exists(reversed_recognition))
    )


def _roster(now):
    User = get_user_model()
    deleting = AccountDeletionRequest.objects.filter(user_id=OuterRef("pk")).exclude(
        status=AccountDeletionRequest.Status.COMPLETED,
    )
    devices = CommunityChatDevice.objects.filter(
        user_id=OuterRef("pk"), status=DeviceBindingStatus.VERIFIED,
        revoked_at__isnull=True, verified_at__isnull=False,
    )
    profiles = CommunityMemberProfile.objects.filter(user_id=OuterRef("pk"))
    chat_account = Q(chat_verified=True) & (Q(chat_application=False) | Q(chat_approved=True))
    slack_account = Q(slack_id__regex=r"^[UW][A-Z0-9]+$")

    wallet = _report_wallet().filter(created_at__lte=now)
    earned = wallet.filter(user_id=OuterRef("pk"))
    # Older rows have no user foreign key or Slack-recipient index. Read their
    # distinct recipient IDs once, then match established account bindings.
    legacy_recipients = set(
        wallet.filter(user__isnull=True, slack_user_id__regex=r"^[UW][A-Z0-9]+$")
        .order_by().values_list("slack_user_id", flat=True).distinct()
    )
    active = User.objects.filter(is_active=True).exclude(Exists(deleting)).annotate(
        chat_verified=Exists(devices), chat_application=Exists(profiles),
        chat_approved=Exists(profiles.filter(status=CommunityMemberProfile.Status.APPROVED)),
        earned=Exists(earned),
    )
    # Explicitly verified two-account Slack links exist alongside directly
    # linked User.slack_id records. Use the linked account's canonical ID.
    links = SlackFounderAccountLink.objects.filter(
        slack_user_id__in=active.values("pk"), founder_user_id__in=active.values("pk"),
    ).values_list("slack_user_id", "founder_user_id")
    owner_ids = dict(links)
    source_ids = set(owner_ids) | set(owner_ids.values())
    rows = active.filter(chat_account | slack_account | Q(pk__in=source_ids)).annotate(
        eligible_link=(chat_account | slack_account),
    ).values("pk", "community_chat_profile_id", "first_name", "last_name",
             "slack_id", "eligible_link", "earned")
    accounts = {row["pk"]: row for row in rows}
    groups = defaultdict(list)
    for account_id in accounts:
        groups[owner_ids.get(account_id, account_id)].append(accounts[account_id])

    people = {}
    for owner_id, group in groups.items():
        if not (
            any(row["eligible_link"] for row in group)
            and any(row["earned"] or row["slack_id"] in legacy_recipients for row in group)
        ):
            continue
        owner = accounts[owner_id]
        name = " ".join(f'{owner["first_name"]} {owner["last_name"]}'.split())
        people[owner_id] = Person(
            owner_id, {row["pk"] for row in group}, str(owner["community_chat_profile_id"]),
            name or "MLAI member",
        )
    account_owners = {uid: person for person in people.values() for uid in person.source_ids}
    slack_owners = {
        accounts[uid]["slack_id"]: person for uid, person in account_owners.items()
        if accounts[uid]["slack_id"]
    }
    return list(people.values()), account_owners, slack_owners


def _assign_regions(people):
    profiles = {
        uid: city.strip()
        for uid, city in CommunityMemberProfile.objects.filter(
            user_id__in={uid for person in people for uid in person.source_ids},
        ).values_list("user_id", "city")
        if city.strip()
    }
    for person in people:
        city = profiles.get(person.account_id)
        if city:
            person.region = CITY_REGIONS.get(city)
            continue
        answers = {profiles[uid] for uid in person.source_ids if uid in profiles}
        if len(answers) == 1:
            person.region = CITY_REGIONS.get(next(iter(answers)))
        elif not answers:
            person.region = "UNKNOWN"


def _assign_ranks(people, account_owners, scope, start, now):
    from .volunteer.admin_rank_history import report_ranks

    ranks = report_ranks(account_owners.keys(), scope, midnight(start), now)
    for person in people:
        comparable = [
            ranks[uid] for uid in person.source_ids if uid in ranks and ranks[uid][0] is not None
        ]
        # An explicit link establishes identity, but does not choose between
        # conflicting reconciliations. Leave their rank pending until merged.
        if len(comparable) == 1:
            person.ranks = comparable[0]


def _manual_track(details):
    actions = details.get("action_ids") if isinstance(details, dict) else None
    if not isinstance(actions, list) or not actions:
        return "unassigned"
    tracks = {MANUAL_ACTION_TRACKS.get(action, "unassigned") for action in actions if isinstance(action, str)}
    if len(tracks) > 1:
        return "multiple-tracks"
    return next(iter(tracks), "unassigned")


def _wallet_track(row, actions, manual):
    if row["reference_type"] == "VOLUNTEER_LEVEL_BONUS":
        return "programme-rewards"
    if row["pk"] in manual:
        return _manual_track(manual[row["pk"]])
    if row["reference_type"] == "FIRST_CHANNEL_POST":
        return "getting-started"
    return ACTION_TRACKS.get(actions.get(row["pk"]), "unassigned")


def _assign_wallet(owners, slack_owners, scope, start, now):
    wallet = _report_wallet().filter(
        Q(user_id__in=owners.keys()) | Q(user__isnull=True, slack_user_id__in=slack_owners.keys()),
        created_at__gte=midnight(start), created_at__lte=now,
    )
    actions = dict(VolunteerRecognition.objects.filter(
        community=scope, status="approved", ledger__in=wallet,
    ).values_list("ledger_id", "action_key"))
    manual = dict(PointsRequest.objects.filter(
        ledger_entry__in=wallet, status="approved", manual_time_key__isnull=False,
    ).values_list("ledger_entry_id", "manual_time"))
    for row in wallet.order_by().values(
        "pk", "user_id", "slack_user_id", "reference_type", "created_at", "amount",
    ).iterator():
        owner = owners[row["user_id"]] if row["user_id"] else slack_owners[row["slack_user_id"]]
        owner.activity.append(Activity(
            local_day(row["created_at"]), "roo", str(row["pk"]), row["amount"],
            _wallet_track(row, actions, manual), "Roo earned",
        ))


def _award_day(metadata, recorded):
    value = metadata.get("awarded_on")
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return None
    return day if day <= recorded else None


def _assign_badges(owners, scope, now):
    receipts = VolunteerSourceReceipt.objects.filter(
        community=scope, origin=BADGE_ORIGIN, actor_id__in=owners.keys(), occurred_at__lte=now,
    )
    published = receipts.filter(kind=PROFILE_KIND, status=PROFILE_STATUS).values("actor_id")
    for uid in published.values_list("actor_id", flat=True).distinct():
        owner = owners[uid]
        if owner.ranks[0] is not None:
            owner.badge_collection = []
    definitions = badge_catalogue()
    seen = set()
    for row in receipts.filter(
        kind=BADGE_KIND, status=BADGE_STATUS, actor_id__in=published,
    ).order_by("occurred_at", "pk").values("actor_id", "metadata", "occurred_at").iterator():
        owner = owners[row["actor_id"]]
        metadata = row["metadata"]
        if not isinstance(metadata, dict):
            continue
        key = metadata.get("badge_id")
        reason = metadata.get("public_reason")
        if (
            owner.badge_collection is None or not isinstance(key, str) or key not in definitions
            or not isinstance(reason, str) or not reason.strip() or (owner.id, key) in seen
        ):
            continue
        seen.add((owner.id, key))
        definition = definitions[key]
        track = BADGE_TRACKS[definition["track"]]
        recorded = local_day(row["occurred_at"])
        awarded = _award_day(metadata, recorded)
        owner.badge_collection.append(dict(
            id=key, name=definition["name"], category=track, criterion=definition["criterion"],
            reason=reason, awarded_on=awarded, recorded_on=recorded,
        ))
        if awarded is not None:
            owner.activity.append(Activity(awarded, "badge", key, track=track, label=definition["name"]))


def _assign_attendance(owners, scope, start, now):
    seen = set()
    for uid, event_id, checked_in in VolunteerAttendance.objects.filter(
        community=scope, user_id__in=owners.keys(), checked_in_at__gte=midnight(start), checked_in_at__lte=now,
    ).order_by("checked_in_at", "pk").values_list("user_id", "event_id", "checked_in_at").iterator():
        person = owners[uid]
        pair = (person.id, event_id)
        if pair not in seen:
            seen.add(pair)
            person.activity.append(Activity(local_day(checked_in), "check_in", event_id, label="Verified event check-in"))


def coworking_activities(owners, rows):
    """Project active booking person-days, including Office Manager places."""
    seen = set()
    for row in rows:
        uid, day = row
        person = owners[uid]
        key = (person.id, day)
        if key not in seen:
            seen.add(key)
            person.activity.append(Activity(day, "booking", day.isoformat(), label="Co-working booking"))


def load_people(start, rank_start, now, *, include_regions):
    """Read canonical people and dated activity within the report's bounded horizon."""
    scope = community_id()
    people, owners, slack_owners = _roster(now)
    if not people:
        return people
    if include_regions:
        _assign_regions(people)
    _assign_ranks(people, owners, scope, rank_start, now)
    _assign_wallet(owners, slack_owners, scope, start, now)
    _assign_badges(owners, scope, now)
    _assign_attendance(owners, scope, start, now)
    coworking_activities(owners, CoworkingBooking.objects.filter(
        status="booked", date__range=(start, local_day(now)), user_id__in=owners.keys(),
    ).order_by().values_list("user_id", "date").iterator())
    return people
