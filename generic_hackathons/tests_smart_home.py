"""Unit tests for the Watt smart-home block compiler + catalog (pure, no DB / no Firebase)."""
from django.test import SimpleTestCase

from generic_hackathons import smart_home_blocks as blocks
from generic_hackathons import smart_home_firebase as shf


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
