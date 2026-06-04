"""Path B-lite policy compiler for the Watt Smart Home controller.

The web "controller" is a sense->think->act pipeline (Inputs -> Schedule -> Brain -> Actions
-> Outputs + Safety). Instead of a fixed block->command map, ``compile_policy`` reads the LIVE
game observation (tariff period, solar forecast, current load, temperatures) and -- based on
which Inputs / Brain / Schedule the player placed -- chooses smart device modes and
forecast/price-aware parameters. Unity's per-tick auto-modes (battery "auto" peak-shaving,
hot_water "window"/"auto", appliance defer, EV target) then execute the plan continuously, so
no backend daemon is required.

This makes Inputs and the Brain genuinely change behaviour (they were cosmetic in "Path A").
Boundary: this is a DEPLOY-TIME decision on a live snapshot, NOT a per-tick custom evaluator;
truly reactive policies (e.g. "shed the instant live load spikes") would need a policy
interpreter inside the Unity authority ("full Path B").

Returns command SPECS (action/target_type/target_id/params) + human-readable ``decisions``
(the "why", shown back to the player). The view stamps each spec with the current game tick.

Block ids mirror app/lib/smart-home-pipeline.ts on the frontend.
"""

INPUTS = {"in_smart_meter", "in_temp", "in_weather"}
SCHEDULES = {"sc_time", "sc_day", "sc_price"}
BRAINS = {"br_chatgpt", "br_claude", "br_gemini"}
ACTION_INTENT = {"ac_shift": "shift_load", "ac_reduce": "reduce_usage", "ac_charge": "charge_battery"}
OUTPUT_DEVICE = {"ou_plugs": "smart_plugs", "ou_battery": "battery", "ou_ev": "ev"}
SAFETY = {"sa_manual", "sa_budget"}

KNOWN_PIPELINE_IDS = INPUTS | SCHEDULES | BRAINS | set(ACTION_INTENT) | set(OUTPUT_DEVICE) | SAFETY

# Brain personality -> parameter bias (cosmetic brands become a real, if small, mechanical choice).
BRAIN_BIAS = {
    "br_chatgpt": {"eco_setpoint_c": 19, "hot_water_target_c": 58, "battery_reserve_kwh": 0.5, "label": "ChatGPT (saver)"},
    "br_claude": {"eco_setpoint_c": 21, "hot_water_target_c": 62, "battery_reserve_kwh": 1.2, "label": "Claude (comfort-first)"},
    "br_gemini": {"eco_setpoint_c": 20, "hot_water_target_c": 60, "battery_reserve_kwh": 0.75, "label": "Gemini (balanced)"},
}
DEFAULT_BIAS = BRAIN_BIAS["br_gemini"]

# Concise "what your brain did" line for the nightly recap, so the ChatGPT/Claude/Gemini
# choice is *felt* each day (not only at deploy time). Surfaced via the deploy response.
BRAIN_EFFECT = {
    "br_chatgpt": "ChatGPT ran the house lean - lower bills, tighter comfort.",
    "br_claude": "Claude kept rooms & showers warmer - more comfort, a little more cost.",
    "br_gemini": "Gemini balanced comfort and cost.",
}
DEFAULT_BRAIN_EFFECT = BRAIN_EFFECT["br_gemini"]

# Factory-default device state, mirroring HouseholdEnergySimState.CreateDefault() in Unity.
# compile_policy emits one of these for every concern the player ISN'T managing, so a deploy
# is a FULL-STATE snapshot: removing a block reverts that device to its game-start mode
# instead of leaving it stuck in the last command. (Appliances are event-based, not a
# persistent mode, so they're not part of the baseline.)
BASELINE_COMMANDS = {
    "battery": {"action": "set_battery", "target_type": "battery", "target_id": "battery",
                "params": {"mode": "auto", "reserve_kwh": 0.75}},
    "ev": {"action": "set_ev_charging", "target_type": "ev", "target_id": "ev",
           "params": {"enabled": False}},
    "thermostat": {"action": "set_thermostat_setpoint", "target_type": "thermostat",
                   "target_id": "thermostat", "params": {"setpoint_c": 22}},
    "lights": {"action": "set_lights", "target_type": "lights", "target_id": "all",
               "params": {"auto_off_when_empty": False}},
    "hot_water": {"action": "set_hot_water", "target_type": "hot_water", "target_id": "hot_water",
                  "params": {"mode": "off"}},
}
REVERT_LABEL = {
    "battery": "battery on default auto",
    "ev": "EV charging off",
    "thermostat": "thermostat back to a comfy 22C",
    "lights": "lights manual (some may be left on)",
    "hot_water": "hot water off (cold-shower risk)",
}


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _observation_facts(observation):
    """Extract the few live facts the policy reasons about (safe defaults if absent)."""
    obs = observation if isinstance(observation, dict) else {}
    tariff = obs.get("tariff") or {}
    weather = obs.get("weather") or {}
    loads = obs.get("loads") or {}
    period = str(tariff.get("period") or "").lower()
    condition = str(weather.get("condition") or "").lower()
    forecast = weather.get("solar_forecast_kw") or []
    try:
        forecast_avg = sum(float(x) for x in forecast) / len(forecast) if forecast else None
    except (TypeError, ValueError):
        forecast_avg = None
    low_solar = (forecast_avg is not None and forecast_avg < 1.0) or condition in {"cloudy", "overcast", "rain", "cold"}
    return {
        "period": period,
        "is_offpeak": "off" in period,
        "condition": condition,
        "forecast_avg": forecast_avg,
        "low_solar": low_solar,
        "grid_import_kw": _f(loads.get("grid_import_kw")),
        "outdoor_c": _f(weather.get("outdoor_c")),
    }


def compile_policy(pipeline, observation):
    """Compile a structured pipeline + live observation into a FULL-STATE set of device
    commands + human-readable decisions.

    Full-state / self-healing: every persistent device concern (battery, EV, thermostat,
    lights, hot water) gets a command on every deploy -- the player's smart choice when a
    block manages it, otherwise the sim's factory default. So *removing* a block reverts that
    device to its game-start behaviour instead of leaving it stuck in the last mode, and the
    player has to keep their controller in sync with the weather and the household's demands.
    Appliances stay event-based (a defer is only emitted under Shift Load; deferrals clear at
    the daily boundary, so they don't get "stuck").
    """
    pipeline = pipeline if isinstance(pipeline, dict) else {}
    inputs = set(pipeline.get("inputs") or [])
    schedule = set(pipeline.get("schedule") or [])
    brain_ids = [b for b in (pipeline.get("brain") or []) if b in BRAINS]
    actions = {ACTION_INTENT[a] for a in (pipeline.get("actions") or []) if a in ACTION_INTENT}
    devices = {OUTPUT_DEVICE[o] for o in (pipeline.get("outputs") or []) if o in OUTPUT_DEVICE}
    safety = set(pipeline.get("safety") or [])

    brain_id = brain_ids[0] if brain_ids else None
    bias = BRAIN_BIAS.get(brain_id, DEFAULT_BIAS)
    facts = _observation_facts(observation)
    price_aware = "sc_price" in schedule
    forecast_aware = "in_weather" in inputs
    meter_aware = "in_smart_meter" in inputs
    temp_aware = "in_temp" in inputs

    managed = {}  # concern -> command spec (a player block is actively driving this device)
    decisions = []

    def manage(concern, action, target_type, target_id, params, why):
        managed[concern] = {"action": action, "target_type": target_type, "target_id": target_id, "params": params}
        decisions.append(why)

    # --- BATTERY ---
    if "battery" in devices and (actions & {"charge_battery", "shift_load", "reduce_usage"}):
        if forecast_aware and facts["low_solar"] and facts["is_offpeak"]:
            manage("battery", "set_battery", "battery", "battery",
                   {"mode": "charge", "reserve_kwh": bias["battery_reserve_kwh"]},
                   f"{bias['label']} saw low solar in the forecast and cheap power right now -> pre-charging the battery from the grid before peak.")
        elif price_aware or forecast_aware:
            manage("battery", "set_battery", "battery", "battery",
                   {"mode": "auto", "reserve_kwh": bias["battery_reserve_kwh"]},
                   "Smart battery: auto-charges on solar surplus and discharges through the expensive peak.")
        else:
            manage("battery", "set_battery", "battery", "battery",
                   {"mode": "charge", "reserve_kwh": bias["battery_reserve_kwh"]},
                   "Battery set to charge from solar.")

    # --- EV ---
    if "ev" in devices:
        if "reduce_usage" in actions and not (actions & {"charge_battery", "shift_load"}):
            manage("ev", "set_ev_charging", "ev", "ev", {"enabled": False},
                   "Pausing EV charging to cut load.")
        else:
            manage("ev", "set_ev_charging", "ev", "ev",
                   {"enabled": True, "target_soc": 0.8, "finish_by": "06:15"},
                   "Charging the EV overnight to 80% on cheap power, ready by 6:15am.")

    # --- SMART PLUGS: appliances (event-based) + thermostat & lights (persistent) ---
    appliance_specs = []
    if "smart_plugs" in devices:
        if "shift_load" in actions:
            appliance_specs.append({"action": "defer_appliance", "target_type": "appliance",
                                    "target_id": "dishwasher", "params": {"until": "22:00"}})
            appliance_specs.append({"action": "defer_appliance", "target_type": "appliance",
                                    "target_id": "washer", "params": {"until": "22:00"}})
            decisions.append("Shifting the dishwasher & washing machine to 22:00 (off-peak).")
        if "reduce_usage" in actions:
            setpoint = bias["eco_setpoint_c"]
            if meter_aware and facts["grid_import_kw"] > 3.0:
                setpoint = max(16, setpoint - 1)
                why = f"Smart Meter sees high draw ({facts['grid_import_kw']:.1f} kW) -> trimming the thermostat to {setpoint}C."
            elif temp_aware and facts["outdoor_c"] >= 18:
                why = f"Mild outside ({facts['outdoor_c']:.0f}C) -> easing the thermostat to {setpoint}C."
            else:
                why = f"Eco thermostat at {setpoint}C."
            manage("thermostat", "set_thermostat_setpoint", "thermostat", "thermostat", {"setpoint_c": setpoint}, why)
            manage("lights", "set_lights", "lights", "all", {"auto_off_when_empty": True},
                   "Lights switch off automatically in empty rooms.")

    # --- HOT WATER (forecast-aware pre-heat avoids a cold shower) ---
    if forecast_aware and facts["low_solar"]:
        manage("hot_water", "set_hot_water", "hot_water", "hot_water",
               {"mode": "window", "target_c": bias["hot_water_target_c"], "window_start": "04:30", "window_end": "06:30"},
               f"Cloudy/cold forecast -> pre-heating water to {bias['hot_water_target_c']}C before the morning, so no cold shower.")
    elif "reduce_usage" in actions and "smart_plugs" in devices:
        manage("hot_water", "set_hot_water", "hot_water", "hot_water",
               {"mode": "window", "target_c": bias["hot_water_target_c"], "window_start": "22:00", "window_end": "06:00"},
               f"Heating water overnight (off-peak) to {bias['hot_water_target_c']}C.")

    # --- SAFETY (lite: reflected via battery reserve/auto; a hard spend clamp would need Unity) ---
    if "sa_budget" in safety:
        decisions.append("Max Budget Guard armed: keeping the battery in smart mode to avoid grid charging during the 55c peak.")

    # --- FULL STATE: fill every un-managed concern with its factory default, so removing a
    #     block reverts that device (idempotent, self-healing re-deploy). ---
    specs = list(managed.values()) + appliance_specs
    reverted = []
    for concern, baseline in BASELINE_COMMANDS.items():
        if concern not in managed:
            specs.append({
                "action": baseline["action"],
                "target_type": baseline["target_type"],
                "target_id": baseline["target_id"],
                "params": dict(baseline["params"]),
            })
            reverted.append(REVERT_LABEL[concern])

    if reverted:
        decisions.append("On factory defaults (add blocks to manage): " + "; ".join(reverted) + ".")

    return {
        "commands": specs,
        "decisions": decisions,
        "brain": bias["label"],
        "brain_effect": BRAIN_EFFECT.get(brain_id, DEFAULT_BRAIN_EFFECT),
    }
