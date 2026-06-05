"""Day-gated capability progression for the Watt Smart Home controller.

The controller starts as a simple light switch and reveals the SENSE->THINK->ACT pipeline
(Actions/Outputs -> Schedule -> Brain/Inputs) as the campaign progresses. The in-game day is
a deterministic function of wall-clock time (Unity CampaignClock), so the unlocked stage is
shared across a team + resume-safe.

Mirror of ``app/lib/smart-home-progression.ts`` on the frontend -- keep the two in sync.
"""

# Stage thresholds, in in-game days. Tunable -- the main pacing lever over the 46-day campaign.
STAGE2_DAY = 6    # pipeline introduced: Actions + Outputs
STAGE3_DAY = 16   # + Schedule
STAGE4_DAY = 26   # + Brain + Inputs (the full board)

# slot -> pipeline block ids (mirror the frontend palette / smart_home_policy KNOWN_PIPELINE_IDS).
SLOT_BLOCK_IDS = {
    "input": {"in_smart_meter", "in_temp", "in_weather"},
    "schedule": {"sc_time", "sc_day", "sc_price"},
    "brain": {"br_chatgpt", "br_claude", "br_gemini"},
    "action": {"ac_shift", "ac_reduce", "ac_charge"},
    "output": {"ou_plugs", "ou_battery", "ou_ev"},
    "safety": {"sa_manual", "sa_budget"},
}

# Stage-1 switchboard devices -> the room the direct set_lights command targets
# (mirror HouseEnergyIds room ids in Unity).
SWITCH_DEVICE_ROOM = {
    "bathroom": "bathroom",
    "living": "living",
    "kitchen": "kitchen",
    "bedroom": "bedroom",
}


def stage_for_day(day):
    """Return the unlocked stage (1..4) for an in-game day. Defaults to 1 on bad input."""
    try:
        d = int(day)
    except (TypeError, ValueError):
        d = 1
    d = max(1, d)
    if d >= STAGE4_DAY:
        return 4
    if d >= STAGE3_DAY:
        return 3
    if d >= STAGE2_DAY:
        return 2
    return 1


def unlocked_slots(day):
    """Pipeline slot types unlocked at this day (stage 1 = none; the switchboard handles it)."""
    stage = stage_for_day(day)
    slots = set()
    if stage >= 2:
        slots |= {"action", "output"}
    if stage >= 3:
        slots.add("schedule")
    if stage >= 4:
        slots |= {"input", "brain", "safety"}
    return slots


def unlocked_block_ids(day):
    ids = set()
    for slot in unlocked_slots(day):
        ids |= SLOT_BLOCK_IDS.get(slot, set())
    return ids


def locked_block_ids_in(pipeline, day):
    """Block ids used by ``pipeline`` that aren't unlocked yet at ``day``.

    Fail-open: returns [] when the day is unknown so a missing observation never blocks a deploy.
    """
    if day is None:
        return []
    try:
        int(day)
    except (TypeError, ValueError):
        return []
    allowed = unlocked_block_ids(day)
    used = {
        str(b).strip()
        for slot in (pipeline or {}).values() if isinstance(slot, list)
        for b in slot if str(b).strip()
    }
    return sorted(used - allowed)
