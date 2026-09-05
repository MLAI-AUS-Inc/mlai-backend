"""Pure, exact Volunteer policy; no database or Django initialisation required."""

from datetime import datetime, timedelta
from decimal import Decimal, DecimalException
from zoneinfo import ZoneInfo

VERSION = "mlai-volunteer-v1"
MICROROO = 1_000_000
MELBOURNE = ZoneInfo("Australia/Melbourne")
LEVELS = (
    (
        "level_0",
        0,
        "MLAI Curious",
        0,
        0,
        "Meet people and try beginner community actions.",
    ),
    (
        "level_1",
        1,
        "MLAI Connected",
        4,
        2,
        "Event volunteering, induction and starter contributions.",
    ),
    (
        "level_2",
        2,
        "MLAI Contributor",
        10,
        3,
        "Guided fixes, recaps and small project deliverables.",
    ),
    (
        "level_3",
        3,
        "MLAI Regular",
        20,
        4,
        "Discuss an AI session or a defined project outcome.",
    ),
    (
        "level_4",
        4,
        "MLAI Collaborator",
        50,
        8,
        "Explore a small workstream or mentoring with a lead.",
    ),
    (
        "level_5",
        5,
        "MLAI Community Builder",
        100,
        12,
        "Explore committee involvement with a person.",
    ),
    (
        "level_6",
        6,
        "MLAI Steward",
        250,
        20,
        "Sustained stewardship and succession opportunities.",
    ),
)


def roo(value):
    """Serialize integer microroo as an exact decimal Roo string."""
    return (
        format(Decimal(value) / MICROROO, "f").rstrip("0").rstrip(".")
        if value % MICROROO
        else str(value // MICROROO)
    )


def microroo(value):
    """Parse nonnegative decimal Roo without rounding or binary floating point."""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError("invalid_reward")
    try:
        amount = Decimal(value)
        if (
            not amount.is_finite()
            or amount < 0
            or amount > Decimal("1000000000")
            or amount != amount.quantize(Decimal("0.000001"))
        ):
            raise ValueError("invalid_reward")
        return int(amount * MICROROO)
    except DecimalException as exc:
        raise ValueError("invalid_reward") from exc


def levels():
    """Return immutable-policy public rank descriptions."""
    return [
        dict(
            key=key,
            level=n,
            name=name,
            threshold_roo=str(threshold),
            bonus_roo=str(bonus),
            pathway=pathway,
        )
        for key, n, name, threshold, bonus, pathway in LEVELS
    ]


def progress(total):
    """Compute progress inside the current level; bonuses are not an input."""
    rank = max(row[1] for row in LEVELS if total >= row[3] * MICROROO)
    public = levels()
    current = public[rank]
    following = public[rank + 1] if rank + 1 < len(public) else None
    earned = total - LEVELS[rank][3] * MICROROO
    required = (LEVELS[rank + 1][3] - LEVELS[rank][3]) * MICROROO if following else 0
    return dict(
        current_level=current,
        next_level=following,
        points_to_next=roo(max(required - earned, 0)) if following else "0",
        progress=dict(
            earned_roo=roo(earned),
            required_roo=roo(required),
            fraction=min(earned / required, 1) if required else 1,
        ),
    )


def period_bounds(occurred_at, period):
    """Return timezone-aware Melbourne calendar boundaries using occurrence time."""
    if occurred_at.tzinfo is None:
        raise ValueError("timestamp_requires_timezone")
    local = occurred_at.astimezone(MELBOURNE)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        start -= timedelta(days=start.weekday())
        end = start + timedelta(days=7)
    elif period == "month":
        start = start.replace(day=1)
        end = (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else start.replace(month=start.month + 1)
        )
    else:
        raise ValueError("invalid_period")
    return start, end


def catalogue(monthly_reward=20):
    """Return all 17 templates; configured monthly reward remains authoritative."""
    rows = (
        (
            "introduce_yourself",
            "Introduce yourself",
            "Share who you are and what you are building or learning.",
            4,
            4,
            False,
            "once",
            1,
            "intro",
            "post",
            "start_here",
        ),
        (
            "boost_startup",
            "Like a startup post",
            "Like another member's post inside MLAI Chat.",
            1,
            1,
            False,
            "month",
            4,
            "boost",
            "reaction",
            "boost_startup",
        ),
        (
            "attend_first_event",
            "Attend your first MLAI event",
            "Meet the community at a verified event check-in.",
            2,
            2,
            False,
            "once",
            1,
            "attendance",
            "attendance",
            None,
        ),
        (
            "volunteer_event",
            "Volunteer at an MLAI event",
            "Help with setup, welcome, check-in, catering or pack-down; agree the scope with your guide.",
            6,
            18,
            True,
            "event",
            1,
            "event",
            "human",
            None,
        ),
        (
            "monthly_startup_update",
            "Submit a monthly startup update",
            "Publish a completed update using the existing startup workflow.",
            monthly_reward,
            monthly_reward,
            False,
            "month",
            1,
            "monthly_update",
            "monthly_update",
            "monthly_updates",
        ),
        (
            "monthly_learning_update",
            "Submit a monthly build/learning update",
            "Share an experiment, evidence, learning and next step.",
            monthly_reward,
            monthly_reward,
            False,
            "month",
            1,
            "monthly_update",
            "human",
            "monthly_updates",
        ),
        (
            "coworking_induction",
            "Complete coworking induction",
            "Meet the host and complete an induction.",
            6,
            6,
            False,
            "once",
            1,
            "induction",
            "human",
            None,
        ),
        (
            "share_first_meme",
            "Share a funny, appropriate meme",
            "Share something that makes the community smile.",
            1,
            1,
            False,
            "once",
            1,
            "meme",
            "human",
            "random",
        ),
        (
            "first_channel_contribution",
            "Make a useful channel post",
            "Share a useful resource, thoughtful question or relevant observation.",
            1,
            1,
            False,
            "once",
            1,
            "first_post",
            "human",
            "general",
        ),
        (
            "helpful_answer",
            "Answer somebody helpfully",
            "Help another member with a useful answer.",
            3,
            3,
            False,
            "week",
            2,
            "answer",
            "human",
            "help",
        ),
        (
            "report_bug",
            "Report a useful, reproducible bug",
            "Describe the expected result, actual result, steps and platform.",
            3,
            3,
            False,
            "week",
            3,
            "bug",
            "human",
            "bugs",
        ),
        (
            "fix_bug",
            "Fix a small, agreed bug",
            "Deliver a tested change accepted by the owner.",
            12,
            24,
            True,
            "deliverable",
            1,
            "fix",
            "human",
            None,
        ),
        (
            "test_product",
            "Test an MLAI app or website flow",
            "Complete the requested checklist and share observations.",
            3,
            3,
            True,
            "deliverable",
            1,
            "test",
            "human",
            None,
        ),
        (
            "proofread",
            "Proofread a short page or announcement",
            "Give the owner a useful review or corrections.",
            3,
            3,
            True,
            "deliverable",
            1,
            "proofread",
            "human",
            None,
        ),
        (
            "event_recap",
            "Write an event recap",
            "Create a short, useful recap accepted by the content lead.",
            6,
            6,
            True,
            "deliverable",
            1,
            "recap",
            "human",
            None,
        ),
        (
            "test_ai_tutorial",
            "Test a beginner AI tutorial",
            "Complete the tutorial and share an accepted test log.",
            6,
            6,
            True,
            "deliverable",
            1,
            "tutorial",
            "human",
            None,
        ),
        (
            "buy_merch",
            "Buy MLAI merch",
            "Optional Supporter milestone after fulfilment; no rank points.",
            0,
            0,
            False,
            "once",
            1,
            "merch",
            "merch",
            None,
        ),
    )
    return {
        key: dict(
            key=key,
            title=title,
            description=description,
            reward_roo=str(reward),
            reward_max_roo=str(maximum),
            requires_attendance=attendance,
            period=period,
            cap=cap,
            cap_group=group,
            verification=verification,
            channel_key=channel,
            repeat_label=(
                "Once per member" if period == "once" else f"Up to {cap} per {period}"
            ),
        )
        for key, title, description, reward, maximum, attendance, period, cap, group, verification, channel in rows
    }


def next_actions(actions, limit=3):
    """Select a small deterministic next-level checklist, never infer completion."""
    actionable = [
        item for item in actions if item.get("eligible") and not item.get("completed")
    ]
    priority = {
        "introduce_yourself": 0,
        "boost_startup": 1,
        "attend_first_event": 2,
        "coworking_induction": 3,
    }
    return sorted(
        actionable,
        key=lambda item: (
            item.get("priority", priority.get(item["key"], 10)),
            item["key"],
        ),
    )[:limit]
