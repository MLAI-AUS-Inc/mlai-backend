"""Unit tests for the Watt smart-home block compiler + catalog (pure, no DB / no Firebase)."""
from django.test import SimpleTestCase

from generic_hackathons import smart_home_blocks as blocks
from generic_hackathons import smart_home_firebase as shf
from generic_hackathons import smart_home_policy as policy


class CompileBlocksTests(SimpleTestCase):
    def test_unknown_ids_are_skipped(self):
        self.assertEqual(blocks.compile_blocks(["nope", "also_nope"]), [])

    def test_empty_input(self):
        self.assertEqual(blocks.compile_blocks([]), [])
        self.assertEqual(blocks.compile_blocks(None), [])

    def test_single_block_maps_to_expected_command_spec(self):
        specs = blocks.compile_blocks(["thermostat_eco"])
        self.assertEqual(len(specs), 1)
        spec = specs[0]
        self.assertEqual(spec["action"], "set_thermostat_setpoint")
        self.assertEqual(spec["target_type"], "thermostat")
        self.assertEqual(spec["target_id"], "thermostat")
        self.assertEqual(spec["params"], {"setpoint_c": 20})

    def test_same_concern_last_placed_wins(self):
        specs = blocks.compile_blocks(["battery_store_solar", "battery_discharge_peak"])
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["params"], {"mode": "discharge"})

    def test_distinct_concerns_all_emit(self):
        ids = ["dishwasher_offpeak", "battery_smart", "ev_overnight_80", "thermostat_eco"]
        specs = blocks.compile_blocks(ids)
        self.assertEqual(len(specs), 4)
        actions = {spec["action"] for spec in specs}
        self.assertEqual(
            actions,
            {"defer_appliance", "set_battery", "set_ev_charging", "set_thermostat_setpoint"},
        )

    def test_each_appliance_is_its_own_concern(self):
        specs = blocks.compile_blocks(["dishwasher_offpeak", "washer_offpeak", "dryer_hang_dry"])
        self.assertEqual(len(specs), 3)

    def test_compile_returns_copied_params(self):
        # Mutating a compiled spec must not corrupt the shared catalog entry.
        specs = blocks.compile_blocks(["thermostat_eco"])
        specs[0]["params"]["setpoint_c"] = 99
        self.assertEqual(blocks.CATALOG_BY_ID["thermostat_eco"]["params"]["setpoint_c"], 20)


class CatalogIntegrityTests(SimpleTestCase):
    def test_every_entry_has_required_fields_and_nonempty_target(self):
        required = {
            "block_id", "group", "label", "blurb",
            "concern", "action", "target_type", "target_id", "params",
        }
        for entry in blocks.CATALOG:
            with self.subTest(block=entry.get("block_id")):
                self.assertTrue(required.issubset(entry.keys()))
                self.assertTrue(entry["target_id"], "target_id must be non-empty (Unity requires it)")
                self.assertIn(entry["group"], blocks.GROUPS)

    def test_block_ids_are_unique(self):
        ids = [entry["block_id"] for entry in blocks.CATALOG]
        self.assertEqual(len(ids), len(set(ids)))

    def test_actions_are_within_the_allowed_set(self):
        allowed = {
            "set_battery", "set_ev_charging", "set_hot_water",
            "run_appliance", "defer_appliance", "set_lights", "set_thermostat_setpoint",
        }
        for entry in blocks.CATALOG:
            self.assertIn(entry["action"], allowed)
        # Scope guard: no house-rule / family-behaviour actions sneak in.
        actions = {entry["action"] for entry in blocks.CATALOG}
        self.assertNotIn("set_house_rule", actions)
        self.assertNotIn("set_thermostat_mode", actions)

    def test_public_catalog_hides_server_fields(self):
        for row in blocks.public_catalog():
            self.assertEqual(set(row.keys()), {"block_id", "group", "label", "blurb"})


class ObservationLivenessTests(SimpleTestCase):
    def test_missing_published_at_is_not_live(self):
        self.assertIsNone(shf.observation_published_at_ms({}))
        self.assertFalse(shf.is_observation_live({}, 1_000_000))
        self.assertFalse(shf.is_observation_live(None, 1_000_000))

    def test_fresh_observation_is_live(self):
        now = 1_000_000
        self.assertTrue(shf.is_observation_live({"published_at_ms": now - 3_000}, now))

    def test_stale_observation_is_not_live(self):
        now = 1_000_000
        self.assertFalse(shf.is_observation_live({"published_at_ms": now - 60_000}, now))


class CompilePolicyTests(SimpleTestCase):
    SUNNY = {"tariff": {"period": "off-peak"}, "weather": {"condition": "sunny", "solar_forecast_kw": [3, 3, 3], "outdoor_c": 24}, "loads": {"grid_import_kw": 0.5}}
    CLOUDY_OFFPEAK = {"tariff": {"period": "off-peak"}, "weather": {"condition": "cloudy", "solar_forecast_kw": [0.2, 0.3], "outdoor_c": 8}, "loads": {"grid_import_kw": 1.0}}
    PEAK_HIGHLOAD = {"tariff": {"period": "peak"}, "weather": {"condition": "normal", "solar_forecast_kw": [1, 1], "outdoor_c": 30}, "loads": {"grid_import_kw": 5.0}}

    def _battery_modes(self, result):
        return [c["params"].get("mode") for c in result["commands"] if c["target_type"] == "battery"]

    def test_price_aware_battery_is_auto(self):
        p = {"inputs": [], "schedule": ["sc_price"], "brain": ["br_gemini"], "actions": ["ac_charge"], "outputs": ["ou_battery"], "safety": []}
        self.assertIn("auto", self._battery_modes(policy.compile_policy(p, self.CLOUDY_OFFPEAK)))

    def test_weather_input_forecast_precharge(self):
        p = {"inputs": ["in_weather"], "schedule": ["sc_price"], "brain": ["br_gemini"], "actions": ["ac_charge"], "outputs": ["ou_battery"], "safety": []}
        result = policy.compile_policy(p, self.CLOUDY_OFFPEAK)
        self.assertIn("charge", self._battery_modes(result))  # pre-charge before peak on a cloudy off-peak morning
        self.assertTrue(any("forecast" in d.lower() or "cloudy" in d.lower() for d in result["decisions"]))

    def test_smart_meter_trims_thermostat_under_load(self):
        p = {"inputs": ["in_smart_meter"], "schedule": ["sc_time"], "brain": ["br_gemini"], "actions": ["ac_reduce"], "outputs": ["ou_plugs"], "safety": []}
        result = policy.compile_policy(p, self.PEAK_HIGHLOAD)
        setpoints = [c["params"]["setpoint_c"] for c in result["commands"] if c["action"] == "set_thermostat_setpoint"]
        self.assertTrue(setpoints and setpoints[0] <= 19)

    def test_brain_bias_changes_setpoint(self):
        base = {"inputs": [], "schedule": ["sc_time"], "actions": ["ac_reduce"], "outputs": ["ou_plugs"], "safety": []}
        claude = policy.compile_policy({**base, "brain": ["br_claude"]}, self.SUNNY)
        chatgpt = policy.compile_policy({**base, "brain": ["br_chatgpt"]}, self.SUNNY)
        sp = lambda r: [c["params"]["setpoint_c"] for c in r["commands"] if c["action"] == "set_thermostat_setpoint"][0]
        self.assertEqual(sp(claude), 21)
        self.assertEqual(sp(chatgpt), 19)

    def test_ev_pause_vs_charge(self):
        charge = policy.compile_policy({"inputs": [], "schedule": ["sc_time"], "brain": [], "actions": ["ac_shift"], "outputs": ["ou_ev"], "safety": []}, self.SUNNY)
        self.assertTrue(any(c["params"].get("enabled") is True for c in charge["commands"] if c["target_type"] == "ev"))
        pause = policy.compile_policy({"inputs": [], "schedule": ["sc_time"], "brain": [], "actions": ["ac_reduce"], "outputs": ["ou_ev"], "safety": []}, self.SUNNY)
        self.assertTrue(any(c["params"].get("enabled") is False for c in pause["commands"] if c["target_type"] == "ev"))

    def test_empty_pipeline_no_commands(self):
        result = policy.compile_policy({"inputs": [], "schedule": [], "brain": [], "actions": [], "outputs": [], "safety": []}, self.SUNNY)
        self.assertEqual(result["commands"], [])
        self.assertTrue(result["decisions"])
