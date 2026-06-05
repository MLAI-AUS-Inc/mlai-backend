"""Unit tests for the Watt smart-home block compiler + catalog (pure, no DB / no Firebase)."""
from django.test import SimpleTestCase

from generic_hackathons import smart_home_blocks as blocks
from generic_hackathons import smart_home_firebase as shf
from generic_hackathons import smart_home_policy as policy
from generic_hackathons import smart_home_progression as progression


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

    def test_liveness_classifies_reason(self):
        now = 1_000_000
        # absent node vs node-without-timestamp are distinguished
        self.assertEqual(shf.observation_liveness(None, now)["reason"], "no_observation")
        self.assertEqual(shf.observation_liveness({}, now)["reason"], "missing_timestamp")
        fresh = shf.observation_liveness({"published_at_ms": now - 3_000}, now)
        self.assertTrue(fresh["live"])
        self.assertEqual(fresh["reason"], "live")
        self.assertEqual(fresh["age_ms"], 3_000)
        stale = shf.observation_liveness({"published_at_ms": now - 60_000}, now)
        self.assertFalse(stale["live"])
        self.assertEqual(stale["reason"], "stale")
        self.assertEqual(stale["age_ms"], 60_000)

    def test_liveness_matches_is_observation_live(self):
        # The boolean wrapper must agree with the classifier for every case.
        now = 1_000_000
        for obs in (None, {}, {"published_at_ms": now - 3_000}, {"published_at_ms": now - 60_000}):
            self.assertEqual(
                shf.is_observation_live(obs, now),
                shf.observation_liveness(obs, now)["live"],
            )


class ShopContractTests(SimpleTestCase):
    """Lock the 2C shop/buy contract against Unity's HackathonFirebasePaths / authority."""

    def test_shop_path_matches_unity_node(self):
        # Unity: HackathonFirebasePaths.ShopCurrent = root + "/shop/current".
        self.assertEqual(
            shf.shop_current_path("WATT", "TEAM1"),
            "classes/WATT/hackathon/households/TEAM1/shop/current",
        )

    def test_shop_path_cleans_segments(self):
        self.assertEqual(
            shf.shop_current_path("WA.TT", "TEAM/9"),
            "classes/WA_TT/hackathon/households/TEAM_9/shop/current",
        )

    def test_purchase_command_shape(self):
        # Unity routes on action == "purchase_upgrade" and reads target_id as the catalog id.
        cmd = shf.build_command(
            action="purchase_upgrade", target_type="upgrade",
            target_id="solar_panel_3kw", params={}, tick_seen=12,
        )
        self.assertEqual(cmd["action"], "purchase_upgrade")
        self.assertEqual(cmd["target_type"], "upgrade")
        self.assertEqual(cmd["target_id"], "solar_panel_3kw")
        self.assertEqual(cmd["tick_seen"], 12)
        self.assertEqual(cmd["status"], "pending")
        self.assertTrue(cmd["command_id"])  # non-empty Firebase key

    def test_purchase_command_target_id_never_empty(self):
        # target_id MUST be non-empty or Unity rejects the command outright.
        cmd = shf.build_command(
            action="purchase_upgrade", target_type="upgrade",
            target_id="", params={}, tick_seen=1,
        )
        self.assertTrue(cmd["target_id"])


class PolicyContractTests(SimpleTestCase):
    """Lock the AI-brain policy node path against Unity's HackathonFirebasePaths.PolicyCurrent."""

    def test_policy_path_matches_unity_node(self):
        # Unity: HackathonFirebasePaths.PolicyCurrent = root + "/policy/current".
        self.assertEqual(
            shf.policy_current_path("WATT", "TEAM1"),
            "classes/WATT/hackathon/households/TEAM1/policy/current",
        )

    def test_policy_path_cleans_segments(self):
        self.assertEqual(
            shf.policy_current_path("WA.TT", "TEAM/9"),
            "classes/WA_TT/hackathon/households/TEAM_9/policy/current",
        )


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

    def _by_target(self, result, target_type):
        return [c for c in result["commands"] if c["target_type"] == target_type]

    def test_empty_pipeline_emits_factory_defaults(self):
        # Full-state: even an empty pipeline asserts every device's factory default, so the
        # house is always in a known state instead of "stuck in the last command".
        result = policy.compile_policy({"inputs": [], "schedule": [], "brain": [], "actions": [], "outputs": [], "safety": []}, self.SUNNY)
        concerns = {c["target_type"] for c in result["commands"]}
        self.assertEqual(concerns, {"battery", "ev", "thermostat", "lights", "hot_water"})
        self.assertEqual(self._by_target(result, "battery")[0]["params"]["mode"], "auto")
        self.assertEqual(self._by_target(result, "ev")[0]["params"]["enabled"], False)
        self.assertEqual(self._by_target(result, "thermostat")[0]["params"]["setpoint_c"], 22)
        self.assertEqual(self._by_target(result, "hot_water")[0]["params"]["mode"], "off")
        self.assertTrue(any("factory default" in d.lower() for d in result["decisions"]))

    def test_full_state_always_covers_every_persistent_concern(self):
        # A real deploy that only manages the battery still emits a command for every other
        # persistent device (ev/thermostat/lights/hot_water), so nothing is left stuck.
        p = {"inputs": ["in_weather"], "schedule": ["sc_price"], "brain": ["br_claude"], "actions": ["ac_charge"], "outputs": ["ou_battery"], "safety": []}
        concerns = {c["target_type"] for c in policy.compile_policy(p, self.SUNNY)["commands"]}
        self.assertTrue({"battery", "ev", "thermostat", "lights", "hot_water"} <= concerns)

    def test_removing_battery_block_reverts_to_auto(self):
        # Battery managed in "charge"; with the battery block removed the next deploy puts it
        # back to the default auto mode (self-healing -- removing a block changes the house).
        managed = {"inputs": ["in_weather"], "schedule": ["sc_time"], "brain": ["br_gemini"], "actions": ["ac_charge"], "outputs": ["ou_battery"], "safety": []}
        self.assertEqual(self._by_target(policy.compile_policy(managed, self.CLOUDY_OFFPEAK), "battery")[0]["params"]["mode"], "charge")
        removed = {**managed, "outputs": ["ou_plugs"]}
        self.assertEqual(self._by_target(policy.compile_policy(removed, self.CLOUDY_OFFPEAK), "battery")[0]["params"]["mode"], "auto")

    def test_unmanaged_ev_charging_is_off(self):
        # No EV block -> EV charging reverts to off (it won't top itself up).
        p = {"inputs": [], "schedule": ["sc_time"], "brain": ["br_gemini"], "actions": ["ac_reduce"], "outputs": ["ou_plugs"], "safety": []}
        self.assertEqual(self._by_target(policy.compile_policy(p, self.SUNNY), "ev")[0]["params"]["enabled"], False)

    def test_brain_effect_present_and_varies(self):
        base = {"inputs": [], "schedule": ["sc_time"], "actions": ["ac_reduce"], "outputs": ["ou_plugs"], "safety": []}
        claude = policy.compile_policy({**base, "brain": ["br_claude"]}, self.SUNNY)
        chatgpt = policy.compile_policy({**base, "brain": ["br_chatgpt"]}, self.SUNNY)
        self.assertIn("Claude", claude["brain_effect"])
        self.assertIn("ChatGPT", chatgpt["brain_effect"])
        self.assertNotEqual(claude["brain_effect"], chatgpt["brain_effect"])
        # No brain placed -> falls back to the balanced (Gemini) effect line.
        self.assertEqual(policy.compile_policy({**base, "brain": []}, self.SUNNY)["brain_effect"], policy.DEFAULT_BRAIN_EFFECT)


class ProgressionTests(SimpleTestCase):
    """Day-gated capability unlocks (mirror app/lib/smart-home-progression.ts)."""

    def test_stage_boundaries(self):
        self.assertEqual(progression.stage_for_day(1), 1)
        self.assertEqual(progression.stage_for_day(progression.STAGE2_DAY - 1), 1)
        self.assertEqual(progression.stage_for_day(progression.STAGE2_DAY), 2)
        self.assertEqual(progression.stage_for_day(progression.STAGE3_DAY), 3)
        self.assertEqual(progression.stage_for_day(progression.STAGE4_DAY), 4)
        self.assertEqual(progression.stage_for_day(999), 4)

    def test_bad_day_defaults_to_stage_1(self):
        self.assertEqual(progression.stage_for_day(None), 1)
        self.assertEqual(progression.stage_for_day("nope"), 1)
        self.assertEqual(progression.stage_for_day(0), 1)

    def test_unlocked_blocks_grow_with_stage(self):
        self.assertEqual(progression.unlocked_block_ids(1), set())  # switchboard only
        stage2 = progression.unlocked_block_ids(progression.STAGE2_DAY)
        self.assertIn("ou_plugs", stage2)
        self.assertIn("ac_reduce", stage2)
        self.assertNotIn("sc_time", stage2)    # schedule still locked
        self.assertNotIn("br_claude", stage2)  # brain still locked
        stage3 = progression.unlocked_block_ids(progression.STAGE3_DAY)
        self.assertIn("sc_time", stage3)
        self.assertNotIn("br_claude", stage3)
        stage4 = progression.unlocked_block_ids(progression.STAGE4_DAY)
        self.assertIn("br_claude", stage4)
        self.assertIn("in_weather", stage4)

    def test_locked_blocks_in_pipeline(self):
        p = {"brain": ["br_claude"], "actions": ["ac_reduce"], "outputs": ["ou_plugs"]}
        self.assertIn("br_claude", progression.locked_block_ids_in(p, 1))
        self.assertEqual(progression.locked_block_ids_in(p, progression.STAGE4_DAY), [])

    def test_locked_blocks_fail_open_on_unknown_day(self):
        p = {"brain": ["br_claude"]}
        self.assertEqual(progression.locked_block_ids_in(p, None), [])
        self.assertEqual(progression.locked_block_ids_in(p, "nope"), [])

    def test_switch_devices_map_to_rooms(self):
        self.assertEqual(progression.SWITCH_DEVICE_ROOM["bathroom"], "bathroom")
        self.assertIn("living", progression.SWITCH_DEVICE_ROOM)
