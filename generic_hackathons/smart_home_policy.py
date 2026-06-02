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
    """Compile a structured pipeline + live observation into device-command specs + decisions."""
    pipeline = pipeline if isinstance(pipeline, dict) else {}
    inputs = set(pipeline.get("inputs") or [])
    schedule = set(pipeline.get("schedule") or [])
    brain_ids = [b for b in (pipeline.get("brain") or []) if b in BRAINS]
    actions = {ACTION_INTENT[a] for a in (pipeline.get("actions") or []) if a in ACTION_INTENT}
    devices = {OUTPUT_DEVICE[o] for o in (pipeline.get("outputs") or []) if o in OUTPUT_DEVICE}
    safety = set(pipeline.get("safety") or [])

    bias = BRAIN_BIAS.get(brain_ids[0], DEFAULT_BIAS) if brain_ids else DEFAULT_BIAS
    facts = _observation_facts(observation)
    price_aware = "sc_price" in schedule
    forecast_aware = "in_weather" in inputs
    meter_aware = "in_smart_meter" in inputs
    temp_aware = "in_temp" in inputs

    specs = []
    decisions = []

    def add(action, target_type, target_id, params, why):
        specs.append({"action": action, "target_type": target_type, "target_id": target_id, "params": params})
        decisions.append(why)

    # --- BATTERY ---
    if "battery" in devices and (actions & {"charge_battery", "shift_load", "reduce_usage"}):
        if forecast_aware and facts["low_solar"] and facts["is_offpeak"]:
            add("set_battery", "battery", "battery", {"mode": "charge", "reserve_kwh": bias["battery_reserve_kwh"]},
                f"{bias['label']} saw low solar in the forecast and cheap power right now -> pre-charging the battery from the grid before peak.")
        elif price_aware or forecast_aware:
            add("set_battery", "battery", "battery", {"mode": "auto", "reserve_kwh": bias["battery_reserve_kwh"]},
                "Smart battery: auto-charges on solar surplus and discharges through the expensive peak.")
        else:
            add("set_battery", "battery", "battery", {"mode": "charge", "reserve_kwh": bias["battery_reserve_kwh"]},
                "Battery set to charge from solar.")

    # --- EV ---
    if "ev" in devices:
        if "reduce_usage" in actions and not (actions & {"charge_battery", "shift_load"}):
            add("set_ev_charging", "ev", "ev", {"enabled": False},
                "Pausing EV charging to cut load.")
        else:
            add("set_ev_charging", "ev", "ev", {"enabled": True, "target_soc": 0.8, "finish_by": "06:15"},
                "Charging the EV overnight to 80% on cheap power, ready by 6:15am.")

    # --- SMART PLUGS (appliances + lights + thermostat) ---
    if "smart_plugs" in devices:
        if "shift_load" in actions:
            add("defer_appliance", "appliance", "dishwasher", {"until": "22:00"}, "Shifting the dishwasher to 22:00 (off-peak).")
            add("defer_appliance", "appliance", "washer", {"until": "22:00"}, "Shifting the washing machine to 22:00 (off-peak).")
        if "reduce_usage" in actions:
            setpoint = bias["eco_setpoint_c"]
            if meter_aware and facts["grid_import_kw"] > 3.0:
                setpoint = max(16, setpoint - 1)
                add("set_thermostat_setpoint", "thermostat", "thermostat", {"setpoint_c": setpoint},
                    f"Smart Meter sees high draw ({facts['grid_import_kw']:.1f} kW) -> trimming the thermostat to {setpoint}C.")
            elif temp_aware and facts["outdoor_c"] >= 18:
                add("set_thermostat_setpoint", "thermostat", "thermostat", {"setpoint_c": setpoint},
                    f"Mild outside ({facts['outdoor_c']:.0f}C) -> easing the thermostat to {setpoint}C.")
            else:
                add("set_thermostat_setpoint", "thermostat", "thermostat", {"setpoint_c": setpoint},
                    f"Eco thermostat at {setpoint}C.")
            add("set_lights", "lights", "all", {"auto_off_when_empty": True}, "Lights switch off automatically in empty rooms.")

    # --- HOT WATER (forecast-aware pre-heat avoids a cold shower) ---
    if forecast_aware and facts["low_solar"]:
        add("set_hot_water", "hot_water", "hot_water",
            {"mode": "window", "target_c": bias["hot_water_target_c"], "window_start": "04:30", "window_end": "06:30"},
            f"Cloudy/cold forecast -> pre-heating water to {bias['hot_water_target_c']}C before the morning, so no cold shower.")
    elif "reduce_usage" in actions and "smart_plugs" in devices:
        add("set_hot_water", "hot_water", "hot_water",
            {"mode": "window", "target_c": bias["hot_water_target_c"], "window_start": "22:00", "window_end": "06:00"},
            f"Heating water overnight (off-peak) to {bias['hot_water_target_c']}C.")

    # --- SAFETY (lite: reflected via battery reserve/auto; a hard spend clamp would need Unity) ---
    if "sa_budget" in safety:
        decisions.append("Max Budget Guard armed: keeping the battery in smart mode to avoid grid charging during the 55c peak.")

    if not specs:
        decisions.append("No device actions yet -- add an Action and an Output (e.g. Shift Load + Smart Plugs).")

    return {"commands": specs, "decisions": decisions, "brain": bias["label"]}
