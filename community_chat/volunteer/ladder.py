"""The 200-level volunteer ladder (policy v6). Pure; no Django or database.

Nothing reads this module in a request path yet. Today's members still see
the seven levels in ``policy.py``. This holds the ladder's numbers so the
preview command and the later switch-over share one definition.

The curve is the owner's proposal of 15 November 2026: moving from Level L
to Level L + 1 costs 12 + L points, so reaching Level L takes
L * (L + 23) / 2 points in total, and Level 200 takes 22,300.

Three rank names differ from the proposal until the committee settles the
names (decision 3 in the plan): two that swear, and one that uses an office
MLAI really has. ``RANKS`` marks them.
"""

from math import isqrt

from .policy import MICROROO, roo

VERSION = "mlai-volunteer-v6"
MAX_LEVEL = 200
FIRST_STEP = 12
MILESTONE_PREFIX = "ladder_level_"

# (first level, name, flavour). A rank runs until the next rank's first level.
RANKS = (
    (0, "MLAI Curious", "Just having a look. Please don't make this person pitch."),
    (1, "Pre-Seed Human", "Huge potential. Currently raising a hand to help."),
    (2, "CAPTCHA Graduate", "Passed the test. It was mostly buses."),
    (3, "Prompt Heckler", "Has typed \"no, the other thing\" so often it's now a skill."),
    (4, "Minimum Viable Human", "Still in beta. So is the organisation. You'll fit right in."),
    (5, "Artificial General Intern", "Human-level intelligence. Currently being used to print name tags."),
    (7, "Copy-Paste Cadet", "Trusted with Ctrl+C. Ctrl+V still requires adult supervision."),
    (9, "Junior Button Pusher", "Has been shown the buttons. Has also been shown the incident report."),
    (11, "Vibe Apprentice", "The confidence is senior. The supervision is not."),
    (13, "Recovering Idea Person", "Said \"someone should do that\", then made the mistake of making eye contact."),
    (15, "Token Tradie", "On the tools. Can quote a job without calling it a transformation journey."),
    (18, "Prompt Wrangler", "Gets useful answers from the AI. Still working on the committee."),
    (21, "Full-Stack Feral", "Works across the entire stack, including the stack of chairs."),
    (24, "Rubber Duck Consultant", "People explain their problem to you and accidentally solve it. You accept the credit."),
    # Renamed pending decision 3: the proposal's title here swears.
    (27, "Hype Debugger", "Spots hallucinations in model outputs and pitch decks. Neither party enjoys this."),
    # Renamed pending decision 3: the proposal's title here swears.
    (30, "Human in the Thick of It", "The AI saved everyone six hours. You spent seven fixing it."),
    (34, "Ship-It Specialist", "Has a suspicious habit of finishing things before announcing them on LinkedIn."),
    (38, "Demo Day Menace", "The demo works even when someone else clicks the buttons."),
    (42, "Vibe Repair Technician", "Fixes what happened after somebody said, \"I built it in ten minutes.\""),
    (46, "Senior Vibe Engineer", "Still builds on vibes. Now knows which ones need tests."),
    (50, "Agent Supervisor", "Your direct reports are all AI. Somehow you're still dealing with performance issues."),
    (55, "Artificial Middle Management", "Five agents are discussing whose job it is. You've rebuilt an office."),
    (60, "Senior Cat Herder", "Gets a room full of founders to do one thing. The same thing. At roughly the same time."),
    (65, "Department of Getting It Done", "Other departments have strategies. Yours has a finished version."),
    (70, "Principal Chaos Engineer", "Can distinguish a bold experiment from a preventable disaster. Usually before catering arrives."),
    (75, "Chief Ctrl+Z Officer", "People message you \"tiny issue\" and attach six screenshots."),
    (80, "Chief Unblocking Officer", "Knows who to ask, what to cut, and which meeting to quietly cancel."),
    (85, "Chief Reality Officer", "Occasionally ruins a perfectly good strategy by asking who's doing the work."),
    (90, "Director of Fewer Meetings", "Your biggest organisational contribution is the calendar invitation you declined on everyone's behalf."),
    # Renamed pending decision 3: MLAI has real Vice Presidents.
    (95, "Department of Actually Shipping", "Reports directly to reality. Reality would like an update by Friday."),
    (100, "Pitch Deck Exorcist", "Removes the buzzwords until there's a business left. Sometimes there isn't."),
    (105, "Executive Function", "The organisation can now remember, prioritise and follow through. Apparently that was a job."),
    (110, "Load-Bearing Human", "Officially, we have systems. Unofficially, several of them are you."),
    (115, "Distinguished Human", "A proven source of good judgement. No subscription required."),
    (120, "Organic Superintelligence", "Remembers what we agreed at the last meeting. Frontier capability."),
    (125, "Principal Problem Remover", "Your best work is the disaster nobody knows was about to happen."),
    (130, "Big Roo", "A big deal around here. Still asks where the chairs need to go."),
    (135, "Chief Roo Officer", "An impressive title. Somehow still the person buying more milk."),
    (140, "Open-Source Legend", "Keeps giving away advice someone else is charging $999 for."),
    (145, "Local Legend", "Half the room has a story about how you helped. The other half hasn't met you yet."),
    (150, "Human Infrastructure", "Built the systems, taught the people, wrote the docs. Can finally go on holiday."),
    (155, "Accidental Establishment", "Joined to disrupt the system. Now people ask you to approve a budget."),
    (160, "Institutional Memory", "Knows why that rule exists. Was probably there when we needed it."),
    (165, "Distinguished Troublemaker", "Has earned enough trust to ask why we're still doing the stupid thing."),
    (170, "Industry Cheat Code", "Your \"I know someone\" is more useful than most accelerators."),
    (176, "National Treasure", "We considered naming a room after you. We're renting, so there's a mug."),
    (182, "Final Boss", "The person you call when the person you normally call says \"absolutely not\"."),
    (188, "Humanity's Reference Implementation", "When we say \"we need more people like that\", you are the that."),
    (194, "Frontier Human", "The benchmark is now whether the rest of us can keep up. We've requested more compute."),
    (200, "AGI with a Lanyard", "Got twelve volunteers to agree on a date. The labs would like a word."),
)

# (first level, key, name). Only the member sees their frame unless they share.
FRAMES = (
    (0, "none", None),
    (5, "creek", "Creek"),
    (15, "paddock", "Paddock"),
    (30, "ridge", "Ridge"),
    (50, "coast", "Coast"),
    (100, "reef", "Reef"),
    (150, "sky", "Sky"),
    (200, "lanyard", "The lanyard"),
)


def _check_level(level):
    if type(level) is not int or not 0 <= level <= MAX_LEVEL:
        raise ValueError("invalid_level")


def step_cost(level):
    """Points to move from ``level`` to the next level."""
    _check_level(level)
    if level == MAX_LEVEL:
        raise ValueError("invalid_level")
    return FIRST_STEP + level


def threshold(level):
    """Total whole points needed to reach ``level``."""
    _check_level(level)
    return level * (level + 2 * FIRST_STEP - 1) // 2


def level_for(total):
    """Level reached by a contribution total in integer microroo.

    Only whole points count, so 11.9 points is still Level 0.
    """
    if type(total) is not int or total < 0:
        raise ValueError("invalid_total")
    points = total // MICROROO
    base = 2 * FIRST_STEP - 1
    level = (isqrt(base * base + 8 * points) - base) // 2
    return min(level, MAX_LEVEL)


def milestone_key(level):
    """Level-up record key, kept apart from the seven-level ``level_N`` keys."""
    _check_level(level)
    return f"{MILESTONE_PREFIX}{level}"


def plain_label(level):
    """A title for use outside the app: certificates, references, reports."""
    _check_level(level)
    return f"Level {level} contributor"


def _rank_index(level):
    index = 0
    for i, row in enumerate(RANKS):
        if row[0] <= level:
            index = i
    return index


def _rank(index):
    first, name, flavour = RANKS[index]
    last = RANKS[index + 1][0] - 1 if index + 1 < len(RANKS) else MAX_LEVEL
    return dict(
        key=f"rank_{index + 1:02d}",
        number=index + 1,
        name=name,
        flavour=flavour,
        first_level=first,
        last_level=last,
        entry_roo=str(threshold(first)),
        frame=frame_for(first)["key"],
    )


def rank_for(level):
    """The rank that covers ``level``."""
    _check_level(level)
    return _rank(_rank_index(level))


def frame_for(level):
    """The frame around a member's Roo at ``level``."""
    _check_level(level)
    first, key, name = [row for row in FRAMES if row[0] <= level][-1]
    return dict(key=key, name=name, first_level=first)


def position(total):
    """Where a member stands, from a contribution total in microroo.

    ``None`` means the history is still being checked; it never becomes
    Level 0.
    """
    if total is None:
        return None
    level = level_for(total)
    index = _rank_index(level)
    following = _rank(index + 1) if index + 1 < len(RANKS) else None
    top = level == MAX_LEVEL
    start = threshold(level) * MICROROO
    required = 0 if top else step_cost(level) * MICROROO
    earned = min(total - start, required) if not top else 0
    return dict(
        policy=VERSION,
        level=level,
        plain_label=plain_label(level),
        rank=_rank(index),
        frame=frame_for(level),
        next_level=None if top else level + 1,
        points_to_next_level="0" if top else roo(required - earned),
        next_rank=following,
        points_to_next_rank=(
            roo(threshold(following["first_level"]) * MICROROO - total)
            if following
            else "0"
        ),
        progress=dict(
            earned_roo=roo(earned),
            required_roo=roo(required),
            fraction=earned / required if required else 1,
        ),
    )


def catalogue():
    """The whole ladder, for clients to fetch once per policy version."""
    return dict(
        policy=VERSION,
        max_level=MAX_LEVEL,
        first_step_roo=str(FIRST_STEP),
        ranks=[_rank(i) for i in range(len(RANKS))],
        frames=[
            dict(key=key, name=name, first_level=first) for first, key, name in FRAMES
        ],
    )


def preview(rows, today_level):
    """Summarise members' levels today and on the ladder, for a read-only report.

    ``rows`` yields ``(member_id, total)`` with ``total`` in microroo or
    ``None`` for history still being checked. ``today_level`` maps a total to
    today's level number. Returns aggregate counts and one row per member.
    """
    members, today, ladder, ranks, frames = [], {}, {}, {}, {}
    unknown = 0
    for member_id, total in rows:
        if total is None:
            unknown += 1
            members.append(dict(member=member_id, total_roo=None, today=None, ladder=None, rank=None))
            continue
        now = today_level(total)
        where = position(total)
        today[now] = today.get(now, 0) + 1
        ladder[where["level"]] = ladder.get(where["level"], 0) + 1
        rank_name = where["rank"]["name"]
        ranks[rank_name] = ranks.get(rank_name, 0) + 1
        frame = where["frame"]["key"]
        frames[frame] = frames.get(frame, 0) + 1
        members.append(
            dict(
                member=member_id,
                total_roo=roo(total),
                today=now,
                ladder=where["level"],
                rank=rank_name,
            )
        )
    return dict(
        policy=VERSION,
        members=len(members),
        unknown=unknown,
        today_levels={str(k): today[k] for k in sorted(today)},
        ladder_levels={str(k): ladder[k] for k in sorted(ladder)},
        ranks=ranks,
        frames=frames,
        rows=members,
    )
