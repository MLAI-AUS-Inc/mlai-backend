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

# Stage-1 switchboard devices -> the on/off device-command spec the deploy view writes.
# Mirrors the frontend SWITCH_DEVICES ids; every (action, target_type) is one Unity's
# HouseDeviceCommands.TryApply already accepts. Lights target a room (mirror HouseEnergyIds
# in Unity); the thermostat has no on/off so "off" is an 18C eco setback; appliances are
# run-once cycles ("on" runs now, "off" holds to late off-peak).
_SWITCH_ROOMS = ("bathroom", "living", "kitchen", "bedroom", "child_bedroom", "office")


def _light_spec(room, on):
    return {"action": "set_lights", "target_type": "lights", "target_id": room, "params": {"on": on}}


SWITCH_DEVICE_COMMANDS = {
    room: {"on": _light_spec(room, True), "off": _light_spec(room, False)} for room in _SWITCH_ROOMS
}
SWITCH_DEVICE_COMMANDS.update(
    {
        "thermostat": {
            "on": {"action": "set_thermostat_setpoint", "target_type": "thermostat", "target_id": "thermostat", "params": {"setpoint_c": 22}},
            "off": {"action": "set_thermostat_setpoint", "target_type": "thermostat", "target_id": "thermostat", "params": {"setpoint_c": 18}},
        },
        "hot_water": {
            "on": {"action": "set_hot_water", "target_type": "hot_water", "target_id": "hot_water", "params": {"mode": "auto"}},
            "off": {"action": "set_hot_water", "target_type": "hot_water", "target_id": "hot_water", "params": {"mode": "off"}},
        },
        "ev": {
            "on": {"action": "set_ev_charging", "target_type": "ev", "target_id": "ev", "params": {"enabled": True}},
            "off": {"action": "set_ev_charging", "target_type": "ev", "target_id": "ev", "params": {"enabled": False}},
        },
        "battery": {
            "on": {"action": "set_battery", "target_type": "battery", "target_id": "battery", "params": {"mode": "auto"}},
            "off": {"action": "set_battery", "target_type": "battery", "target_id": "battery", "params": {"mode": "hold"}},
        },
        "dishwasher": {
            "on": {"action": "run_appliance", "target_type": "appliance", "target_id": "dishwasher", "params": {}},
            "off": {"action": "defer_appliance", "target_type": "appliance", "target_id": "dishwasher", "params": {"until": "23:59"}},
        },
        "washer": {
            "on": {"action": "run_appliance", "target_type": "appliance", "target_id": "washer", "params": {}},
            "off": {"action": "defer_appliance", "target_type": "appliance", "target_id": "washer", "params": {"until": "23:59"}},
        },
        "dryer": {
            "on": {"action": "run_appliance", "target_type": "appliance", "target_id": "dryer", "params": {}},
            "off": {"action": "defer_appliance", "target_type": "appliance", "target_id": "dryer", "params": {"until": "23:59"}},
        },
    }
)

# Back-compat: the lights-only {device: room} map (older callers/tests still read this).
SWITCH_DEVICE_ROOM = {
    dev: spec["on"]["target_id"]
    for dev, spec in SWITCH_DEVICE_COMMANDS.items()
    if spec["on"]["target_type"] == "lights"
}

SWITCH_DEVICE_LABELS = {
    "bathroom": "Bathroom light", "living": "Living-room light", "kitchen": "Kitchen light",
    "bedroom": "Bedroom light", "child_bedroom": "Child's-bedroom light", "office": "Office light",
    "thermostat": "Thermostat", "hot_water": "Hot water", "ev": "EV charger",
    "battery": "Home battery", "dishwasher": "Dishwasher", "washer": "Washing machine", "dryer": "Dryer",
}


def switch_decision(device_id, on):
    """Human-readable line for the deploy-result feedback."""
    label = SWITCH_DEVICE_LABELS.get(device_id, device_id.replace("_", " "))
    return f"{label} turned {'on' if on else 'off'}."


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
