"""Build MLAI Wrapped stat sheets in media-intel's stat-sheet CSV format.

media-intel stays generic: it receives a comma CSV whose first column is
``Name`` and whose other columns (at most 30) are stat cards, matched by exact
People label. Everything MLAI-specific is computed here. Emails and account or
profile IDs never enter the CSV; the review that travels beside it may name
profile IDs so an admin can resolve names one by one.

Roo points: earned versus paid
------------------------------
Roo wallet credits come in three kinds, and Wrapped keeps them apart:

- **Earned** points are credited for something the member did: volunteering,
  approved tasks and points requests, admin awards, first posts, link-love,
  monthly startup updates, arcade prizes and level bonuses.
- **Paid** points are remuneration for a role or a service, not a
  contribution: the committee's weekly points remuneration and mentors'
  session payouts (``PAID_POINTS_KINDS``).
- **Purchased** top-ups (Stripe or Apple) are bought with money. They are
  excluded before this module sees a credit, as in every contribution report.

Volunteer hours: a member's volunteering is the subset of earned points that
counts toward their volunteer level. Where the member logged real minutes
(manual-time points requests, recorded from 2026-11-21) those minutes are used;
otherwise each volunteering point is taken as 10 minutes, MLAI's standing rate
(1 Roo is usually 10 minutes of volunteering). The column is therefore labelled
an estimate.
"""

from __future__ import annotations

import csv
import io
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace

from .volunteer.credits import COUNTS, NON_LEVEL_ACTIONS, classify
from .volunteer.policy import MICROROO

EARNED = "earned"
PAID = "paid"
PURCHASED = "purchased"
# Remuneration, not contribution. ``classify`` names these kinds.
PAID_POINTS_KINDS = frozenset({"committee_pay", "mentor_payout"})
MINUTES_PER_VOLUNTEER_POINT = 10

MAX_STAT_COLUMNS = 30
MAX_HEADING = 80
MAX_CELL = 200
MAX_ROWS = 5000
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
FALLBACK_NAME = "MLAI member"
MONTHS = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)

NOTES = (
    "Roo points earned excludes paid points (committee remuneration and mentor "
    "session payouts) and purchased top-ups; Roo points paid is shown separately.",
    "Volunteer hours (est.) uses logged minutes where a member recorded them "
    "(from 2026-11-21) and otherwise 10 minutes per volunteering Roo point.",
    "A zero leaves the cell empty, so media-intel shows no card for it.",
    "Standing columns rank each member within this export only.",
    "Badges and mentoring were only recorded from November 2026; volunteer "
    "check-ins from September 2026.",
)


def points_bucket(row, recognition_action=None):
    """Classify one wallet credit as (EARNED, PAID or PURCHASED, counts toward volunteering).

    ``row`` carries the ledger's ``source``, ``reference_type`` and
    ``description``. ``recognition_action`` is the approved Volunteer record's
    action key when one holds this credit.
    """
    label, kind = classify(SimpleNamespace(
        source=row.get("source"), reference_type=row.get("reference_type"),
        description=row.get("description") or "",
    ))
    if kind in PAID_POINTS_KINDS:
        return PAID, False
    if kind == "purchase":
        return PURCHASED, False
    return EARNED, label == COUNTS and recognition_action not in NON_LEVEL_ACTIONS


def is_monthly_update(row):
    """A founder's monthly startup update reward: at most one per calendar month."""
    return classify(SimpleNamespace(
        source=row.get("source"), reference_type=row.get("reference_type"),
        description=row.get("description") or "",
    ))[1] == "startup_update"


@dataclass
class WrappedPerson:
    """One canonical member's year, already reduced to counts and words."""

    id: str
    name: str
    name_source: str = "account"
    roo_earned_microroo: int = 0
    roo_paid_microroo: int = 0
    volunteer_minutes: int = 0
    logged_minutes: int = 0
    events_attended: int = 0
    volunteer_check_ins: int = 0
    coworking_days: int = 0
    monthly_updates: int = 0
    mentoring_given: int = 0
    mentoring_received: int = 0
    badges: int | None = None
    latest_badge: str = ""
    level: int | None = None
    rank: str = ""
    levels_climbed: int | None = None
    active_days: list[date] = field(default_factory=list)

    def add_credit(self, amount_microroo, bucket, volunteering, logged_minutes=None):
        if bucket == PURCHASED:
            return
        if bucket == PAID:
            self.roo_paid_microroo += amount_microroo
            return
        self.roo_earned_microroo += amount_microroo
        if not volunteering:
            return
        if logged_minutes is not None:
            self.logged_minutes += logged_minutes
            self.volunteer_minutes += logged_minutes
        else:
            # 1 volunteering Roo point is about 10 minutes of volunteering.
            self.volunteer_minutes += amount_microroo * MINUTES_PER_VOLUNTEER_POINT // MICROROO


def whole_roo(microroo):
    return int((Decimal(microroo) / MICROROO).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def hours(minutes):
    value = (Decimal(minutes) / 60).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return value.quantize(Decimal(1)) if value == value.to_integral() else value


def busiest_month(days):
    if not days:
        return ""
    counts = Counter(day.month for day in days)
    top = max(counts.values())
    return MONTHS[min(month for month, count in counts.items() if count == top) - 1]


def standing(values, value):
    """'Top N%' within the exported group, or empty for a zero or a group of one."""
    if not value or len(values) < 2:
        return ""
    ahead = sum(other > value for other in values)
    return f"Top {math.ceil((ahead + 1) * 100 / len(values))}%"


def _count(value):
    return str(value) if value else ""


# (heading, value). Headings are the card's words in media-intel.
COLUMNS = (
    ("Roo points earned", lambda p: _count(whole_roo(p.roo_earned_microroo))),
    ("Roo points paid", lambda p: _count(whole_roo(p.roo_paid_microroo))),
    ("Volunteer hours (est.)", lambda p: str(hours(p.volunteer_minutes)) if p.volunteer_minutes >= 6 else ""),
    ("Events attended", lambda p: _count(p.events_attended)),
    ("Volunteer check-ins", lambda p: _count(p.volunteer_check_ins)),
    ("Days coworking", lambda p: _count(p.coworking_days)),
    ("Monthly updates shipped", lambda p: _count(p.monthly_updates)),
    ("Mentoring sessions given", lambda p: _count(p.mentoring_given)),
    ("Mentoring sessions received", lambda p: _count(p.mentoring_received)),
    ("Badges earned", lambda p: _count(p.badges)),
    ("Latest badge", lambda p: p.latest_badge),
    ("Level", lambda p: _count(p.level)),
    ("Rank", lambda p: p.rank),
    ("Levels climbed", lambda p: _count(p.levels_climbed if (p.levels_climbed or 0) > 0 else 0)),
    ("Busiest month", lambda p: busiest_month(p.active_days)),
)
# (heading, the measure ranked within the group).
STANDINGS = (
    ("Roo points earned standing", lambda p: p.roo_earned_microroo),
    ("Volunteer hours standing", lambda p: p.volunteer_minutes),
    ("Events attended standing", lambda p: p.events_attended),
    ("Days coworking standing", lambda p: p.coworking_days),
)
HEADINGS = ("Name", *(h for h, _ in COLUMNS), *(h for h, _ in STANDINGS))


def name_key(name):
    """media-intel matches names ignoring case and spacing."""
    return " ".join(name.split()).casefold()


def review(people):
    """What an admin must confirm one by one before uploading the sheet."""
    groups = defaultdict(list)
    for person in people:
        groups[name_key(person.name)].append(person)
    collisions = [
        dict(name=group[0].name, members=[dict(id=p.id, name_source=p.name_source) for p in group])
        for key, group in sorted(groups.items()) if len(group) > 1
    ]
    needs_name = [
        dict(id=p.id, name=p.name, reason=reason)
        for p in people
        for reason in [
            "no name on record" if p.name == FALLBACK_NAME
            else "too long for media-intel" if len(p.name) > MAX_CELL
            else "starts with a spreadsheet formula character" if p.name.startswith(FORMULA_PREFIXES)
            else ""
        ] if reason
    ]
    return dict(
        people=[dict(id=p.id, name=p.name, name_source=p.name_source) for p in people],
        name_collisions=collisions, needs_name=needs_name,
    )


def sheet_rows(people):
    """The CSV's rows (headings first). Same-name members stay separate rows."""
    ranked = [[measure(p) for p in people] for _, measure in STANDINGS]
    rows = [list(HEADINGS)]
    for person in sorted(people, key=lambda p: (name_key(p.name), p.id)):
        rows.append([
            person.name,
            *(value(person) for _, value in COLUMNS),
            *(standing(values, measure(person)) for values, (_, measure) in zip(ranked, STANDINGS)),
        ])
    return rows


def to_csv(rows):
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows(rows)
    return out.getvalue()


def sheet(people):
    """Return the media-intel CSV text plus the review and notes that go beside it."""
    if len(people) > MAX_ROWS:
        raise ValueError(f"media-intel accepts at most {MAX_ROWS} rows; narrow the scope.")
    return dict(
        csv=to_csv(sheet_rows(people)), columns=list(HEADINGS), rows=len(people),
        review=review(people), notes=list(NOTES),
    )
