"""Block catalog + compiler for the Watt *Smart Home (Beginner)* challenge.

A "block" is a fixed, non-editable mapping from a draggable UI tile to ONE Unity device
command. The browser only ever sends a list of ``block_id`` strings; this module owns the
translation to ``action`` / ``target_type`` / ``target_id`` / ``params`` so command params
cannot be tampered with client-side.

Scope (product decision): appliances, battery/solar, EV, climate & lights. NO house rules
and NO family-behaviour/schedule changes. Thermostat uses ``set_thermostat_setpoint`` (pure
device state), never ``set_thermostat_mode`` (which would write household decisions =
family behaviour).

Compile model (v1): **additive, de-duped by concern (last-placed wins).** Each placed block
emits one command; if two placed blocks target the same concern (e.g. two battery modes), the
later one in the list wins so the outcome is deterministic. Concerns not covered by any placed
block are left untouched (no "reset to default" command in v1) — removing a block stops
re-asserting it rather than actively reverting it.

Every param key/value below is verified against Unity's HouseDeviceCommands.cs handlers.
"""

GROUPS = ("Appliances", "Battery & solar", "EV charging", "Climate & lights")

# Each entry: block_id, group, label, blurb, concern, action, target_type, target_id, params.
# `concern` is the de-dupe axis. action/target_*/params are server-owned (never sent by client).
CATALOG = [
    # --- Appliances (defer to off-peak; hang-dry is a run-now action) ---
    {
        "block_id": "dishwasher_offpeak",
        "group": "Appliances",
        "label": "Run dishwasher off-peak",
        "blurb": "Holds the dishwasher until 10pm so it runs on cheaper, greener power.",
        "concern": "appliance:dishwasher",
        "action": "defer_appliance",
        "target_type": "appliance",
        "target_id": "dishwasher",
        "params": {"until": "22:00"},
    },
    {
        "block_id": "washer_offpeak",
        "group": "Appliances",
        "label": "Run washing machine off-peak",
        "blurb": "Defers the wash to 10pm to dodge the evening peak.",
        "concern": "appliance:washer",
        "action": "defer_appliance",
        "target_type": "appliance",
        "target_id": "washer",
        "params": {"until": "22:00"},
    },
    {
        "block_id": "dryer_hang_dry",
        "group": "Appliances",
        "label": "Hang-dry the laundry",
        "blurb": "Skips the tumble dryer entirely - zero dryer energy.",
        "concern": "appliance:dryer",
        "action": "run_appliance",
        "target_type": "appliance",
        "target_id": "dryer",
        "params": {"hang_dry": True},
    },
    # --- Battery & solar ---
    {
        "block_id": "battery_store_solar",
        "group": "Battery & solar",
        "label": "Store solar in the battery",
        "blurb": "Charges the home battery so daytime solar isn't exported cheaply.",
        "concern": "battery",
        "action": "set_battery",
        "target_type": "battery",
        "target_id": "battery",
        "params": {"mode": "charge"},
    },
    {
        "block_id": "battery_smart",
        "group": "Battery & solar",
        "label": "Smart battery (auto)",
        "blurb": "Lets the battery charge and discharge automatically to shave peaks.",
        "concern": "battery",
        "action": "set_battery",
        "target_type": "battery",
        "target_id": "battery",
        "params": {"mode": "auto"},
    },
    {
        "block_id": "battery_discharge_peak",
        "group": "Battery & solar",
        "label": "Use battery at peak",
        "blurb": "Discharges the battery to power the home during expensive peak hours.",
        "concern": "battery",
        "action": "set_battery",
        "target_type": "battery",
        "target_id": "battery",
        "params": {"mode": "discharge"},
    },
    # --- EV charging ---
    {
        "block_id": "ev_overnight_80",
        "group": "EV charging",
        "label": "Charge EV overnight to 80%",
        "blurb": "Charges the car to 80% by 6:15am, mostly on cheap overnight power.",
        "concern": "ev",
        "action": "set_ev_charging",
        "target_type": "ev",
        "target_id": "ev",
        "params": {"enabled": True, "target_soc": 0.8, "finish_by": "06:15"},
    },
    {
        "block_id": "ev_pause",
        "group": "EV charging",
        "label": "Pause EV charging",
        "blurb": "Stops the car charging - useful to avoid adding to the peak.",
        "concern": "ev",
        "action": "set_ev_charging",
        "target_type": "ev",
        "target_id": "ev",
        "params": {"enabled": False},
    },
    # --- Climate & lights ---
    {
        "block_id": "hot_water_offpeak",
        "group": "Climate & lights",
        "label": "Heat water off-peak only",
        "blurb": "Heats hot water in a 10pm-6am window instead of during the day.",
        "concern": "hot_water",
        "action": "set_hot_water",
        "target_type": "hot_water",
        "target_id": "hot_water",
        "params": {"mode": "window", "target_c": 60, "window_start": "22:00", "window_end": "06:00"},
    },
    {
        "block_id": "thermostat_eco",
        "group": "Climate & lights",
        "label": "Eco thermostat (20 C)",
        "blurb": "Sets a leaner 20C setpoint to cut heating/cooling energy.",
        "concern": "thermostat",
        "action": "set_thermostat_setpoint",
        "target_type": "thermostat",
        "target_id": "thermostat",
        "params": {"setpoint_c": 20},
    },
    {
        "block_id": "lights_auto_off",
        "group": "Climate & lights",
        "label": "Lights off when nobody's home",
        "blurb": "Automatically switches lights off in empty rooms.",
        "concern": "lights",
        "action": "set_lights",
        "target_type": "lights",
        "target_id": "all",
        "params": {"auto_off_when_empty": True},
    },
]

CATALOG_BY_ID = {entry["block_id"]: entry for entry in CATALOG}

# Fields safe to expose to the browser (it never needs action/params - it only sends block_ids).
_PUBLIC_FIELDS = ("block_id", "group", "label", "blurb")


def public_catalog():
    """Catalog shaped for the palette UI (no server-side action/param details)."""
    return [{key: entry[key] for key in _PUBLIC_FIELDS} for entry in CATALOG]


def known_block_ids():
    return set(CATALOG_BY_ID.keys())


def compile_blocks(block_ids):
    """Resolve a list of placed block ids into device-command specs.

    Pure function (no Firebase, no clock). Returns a list of dicts with keys
    ``block_id, concern, action, target_type, target_id, params`` - one per concern,
    de-duplicated so the LAST placed block for a concern wins. Unknown ids are skipped.
    The caller stamps each spec with tick/timestamp via ``smart_home_firebase.build_command``.
    """
    by_concern = {}
    order = []
    for block_id in block_ids or []:
        entry = CATALOG_BY_ID.get(block_id)
        if entry is None:
            continue
        concern = entry["concern"]
        if concern not in by_concern:
            order.append(concern)
        by_concern[concern] = {
            "block_id": entry["block_id"],
            "concern": concern,
            "action": entry["action"],
            "target_type": entry["target_type"],
            "target_id": entry["target_id"],
            "params": dict(entry["params"]),
        }
    return [by_concern[concern] for concern in order]
