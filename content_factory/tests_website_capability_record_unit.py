"""Plan C10: the site capability record and its contract; never create a database.

The contract test validates every projection this module can produce against
``contracts/site-capability-record-v1.schema.json``, the same file MLAI Chat
generates its staff types from (``pnpm contracts:generate``).
"""
from datetime import datetime, timezone as dt_timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from . import website_capability_record as capability
from .founder_journey_contract import FounderContractMismatch, _validate
from .website_discovery import discovery_snapshot

NOW = datetime(2026, 11, 19, 1, 0, tzinfo=dt_timezone.utc)
SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "contracts/site-capability-record-v1.schema.json").read_text())
SCANS = Path(__file__).parent / "testdata" / "cms-content"
SHA = "a" * 40


def validate(record):
    _validate(record, SCHEMA, SCHEMA["$defs"])
    return record


def connection(**overrides):
    values = {"pk": "conn-1", "generation": 2, "github_repo": "owner/site", "branch": "main", "app_root": "",
              "verified_sha": SHA, "last_verified_at": NOW, "capabilities": {}, "state": "connected",
              "organization_id": 7}
    values.update(overrides)
    return SimpleNamespace(**values)


def operation(state="completed", **receipt):
    return SimpleNamespace(state=state, receipt=receipt)


def verified_target(adapter="next_app_router", **contract):
    return SimpleNamespace(adapter=adapter, contract={"verification": {"status": "verified"}, **contract})


class CapabilityRecordContractTests(SimpleTestCase):
    def test_schema_uses_only_the_vocabulary_both_generators_compile(self):
        self.assertEqual(SCHEMA["version"], 1)
        self.assertEqual(SCHEMA["$ref"], "#/$defs/SiteCapabilityRecord")
        self.assertEqual(set(SCHEMA["$defs"]["SiteTierId"]["enum"]), set(capability.TIERS))
        self.assertNotIn("unsupported", json.dumps(SCHEMA["$defs"]))
        record = SCHEMA["$defs"]["SiteCapabilityRecord"]
        self.assertEqual(set(record["required"]), set(record["properties"]))

    def test_every_state_projects_a_valid_record(self):
        ladder = {"setup_ladder": {"schema_version": 1, "tier": "static_dropin", "tier_number": 3, "outcome": "ready",
                                   "rungs": [{"tier": "native", "outcome": "left", "reason": "no_native_adapter"},
                                             {"tier": "agentic", "outcome": "left", "reason": "check_setup_failed"},
                                             {"tier": "static_dropin", "outcome": "passed"}]}}
        cases = {
            "native_ready": dict(op=operation(), target=verified_target(), setup={"adapter_mode": "deterministic"}),
            "agentic_ready": dict(op=operation(), target=verified_target("stack_native"),
                                  setup={"article_system_setup": {"adapter_mode": "model_synthesized"}}),
            "ladder_ready": dict(op=operation(), target=verified_target("static_html"), setup=ladder),
            "in_setup": dict(op=operation("running"), discovery=discovery_snapshot({"repository_discovery": {"support_level": "native"}})),
            "your_turn": dict(op=operation("needs_user", user_action={"id": "deploy_site"})),
            "fixing": dict(op=operation("needs_attention", status="staff_fixing")),
            "cannot_yet": dict(op=operation("stopped", status="cant_be_done_yet",
                                            cant_be_done_yet={"version": 1, "reason": "cms_content"})),
            "not_started": dict(),
        }
        for name, facts in cases.items():
            with self.subTest(name=name):
                record = self.project(**facts)
                validate(record)
                self.assertNotIn("unsupported", json.dumps(record))

    def project(self, **facts):
        with patch.object(capability, "_latest_inventory", return_value=facts.get("discovery") or {}), \
                patch.object(capability, "_latest_prepare", return_value=facts.get("op")), \
                patch.object(capability, "_latest_setup_result", return_value=facts.get("setup") or {}):
            website = connection()
            targets, operations = Mock(), Mock()
            targets.filter.return_value.order_by.return_value.first.return_value = facts.get("target")
            operations.filter.return_value.order_by.return_value.first.return_value = facts.get("deployed")
            website.targets, website.operations = targets, operations
            for key, value in facts.get("connection", {}).items():
                setattr(website, key, value)
            return capability.build(website, now=NOW)

    def test_the_validator_refuses_drift(self):
        record = self.project(op=operation(), target=verified_target(), setup={"adapter_mode": "deterministic"})
        for broken in ({**record, "extra": 1}, {**record, "tier": {**record["tier"], "id": "unsupported"}},
                       {**record, "state": "stopped"}):
            with self.subTest(broken=sorted(set(broken) ^ set(record)) or broken.get("state")):
                with self.assertRaises(FounderContractMismatch):
                    validate(broken)

    def test_tiers_reached_and_why_higher_tiers_were_left(self):
        native = self.project(op=operation(), target=verified_target(), setup={"adapter_mode": "deterministic"})
        self.assertEqual((native["tier"]["id"], native["tier"]["rank"], native["tier"]["source"]), ("native", 1, "proven"))
        self.assertEqual(native["tiersLeft"], [])
        agentic = self.project(op=operation(), target=verified_target("stack_native"), setup={"adapter_mode": "model_synthesized"})
        self.assertEqual(agentic["tier"]["id"], "agentic")
        self.assertEqual(agentic["tiersLeft"], [{"tier": "native", "reason": "no_native_adapter"}])
        ladder = self.project(op=operation(), target=verified_target("static_html"), setup={"setup_ladder": {
            "schema_version": 1, "tier": "static_dropin", "rungs": [
                {"tier": "native", "outcome": "left", "reason": "no_native_adapter"},
                {"tier": "agentic", "outcome": "left", "reason": "check_setup_failed"}]}})
        self.assertEqual((ladder["tier"]["id"], ladder["tier"]["rank"]), ("static_dropin", 3))
        self.assertEqual([item["tier"] for item in ladder["tiersLeft"]], ["native", "agentic"])

    def test_an_unproven_site_shows_the_predicted_tier(self):
        record = self.project(op=operation("running"), discovery=discovery_snapshot({"repository_discovery": {"support_level": "native"}}))
        self.assertEqual((record["state"], record["tier"]["id"], record["tier"]["source"]), ("in_setup", "native", "predicted"))
        self.assertEqual(self.project()["tier"]["id"], "not_checked")

    def test_cms_content_is_cannot_yet_with_its_reason(self):
        scan = json.loads((SCANS / "wordpress-theme.scan.json").read_text())
        record = self.project(op=operation("stopped", status="cant_be_done_yet",
                                           cant_be_done_yet={"version": 1, "reason": "cms_content"}),
                              discovery=discovery_snapshot(scan))
        validate(record)
        self.assertEqual((record["state"], record["tier"]["id"], record["tier"]["reason"]), ("cannot_yet", "cannot_yet", "cms_content"))
        self.assertEqual(record["contentSource"], {"kind": "cms", "cms": "wordpress"})

    def test_deploy_link_host_and_host_config(self):
        record = self.project(op=operation("needs_user", user_action={"id": "deploy_site"}),
                              connection={"capabilities": {"hosting_provider": "netlify",
                                  "hostConfig": {"buildCommand": "hugo", "publishDirectory": "public", "secret": "x"}}})
        self.assertEqual(record["deployLink"]["state"], "ask_raised")
        self.assertEqual(record["host"], "netlify")
        self.assertEqual(record["hostConfig"], {"buildCommand": "hugo", "baseDirectory": None, "publishDirectory": "public",
                                                "productionBranch": None})
        proven = self.project(op=operation(), target=verified_target(),
                              deployed=SimpleNamespace(receipt={"checked_at": NOW.isoformat()}, updated_at=NOW))
        self.assertEqual(proven["deployLink"], {"state": "proven", "checkedAt": NOW.isoformat()})


class TierDropTests(SimpleTestCase):
    def test_a_proven_tier_drop_is_detected_once_per_change(self):
        stored = {"version": 1, "generation": 2, "tier": "native", "rank": 1, "source": "proven", "state": "ready", "proven": "native"}
        agentic = {"id": "agentic", "rank": 2, "source": "proven"}
        self.assertTrue(capability._dropped(stored, agentic, generation=2))
        # A new generation, a predicted tier or a better tier is not a drop.
        self.assertFalse(capability._dropped(stored, agentic, generation=3))
        self.assertFalse(capability._dropped(stored, {"id": "agentic", "rank": 2, "source": "predicted"}, generation=2))
        self.assertFalse(capability._dropped({**stored, "proven": "agentic"}, {"id": "native", "rank": 1, "source": "proven"}, generation=2))
        # A site found to be CMS-held after it was proven is a drop.
        self.assertTrue(capability._dropped(stored, {"id": "cannot_yet", "rank": None, "source": "predicted"}, generation=2))

    def test_the_last_proven_tier_survives_unproven_observations(self):
        record = {"connectionGeneration": 2, "tier": {"id": "agentic", "rank": 2, "source": "predicted"}, "state": "in_setup",
                  "lastProvenTier": "native"}
        self.assertEqual(capability.snapshot(record)["proven"], "native")

    def test_refresh_stores_the_snapshot_and_alerts_on_a_drop(self):
        website = connection(capabilities={"capabilityRecord": {"version": 1, "generation": 2, "tier": "native", "rank": 1,
                                                                "source": "proven", "state": "ready", "proven": "native"}},
                             organization=SimpleNamespace(pk=7))
        record = {"connectionGeneration": 2, "tier": {"id": "agentic", "rank": 2, "source": "proven"}, "state": "ready",
                  "lastProvenTier": "native", "previousTier": "native", "tierDropped": True}
        rows = Mock()
        rows.select_for_update.return_value.select_related.return_value.filter.return_value.first.return_value = website
        with patch("content_factory.website_models.WebsiteConnection.objects", rows), \
                patch.object(capability, "build", return_value=record), \
                patch.object(capability.transaction, "atomic"), \
                patch("content_factory.founder_journey_alerts.queue_founder_journey_alert") as alert:
            capability.refresh("conn-1", now=NOW)
        update = rows.filter.return_value.update.call_args.kwargs["capabilities"]["capabilityRecord"]
        self.assertEqual((update["tier"], update["proven"], update["since"]), ("agentic", "agentic", NOW.isoformat()))
        self.assertEqual(alert.call_args.kwargs["alert_reason"], "our_team_item")
        self.assertEqual(alert.call_args.kwargs["details"]["kind"], "site_tier_dropped")
        self.assertEqual((alert.call_args.kwargs["details"]["from_tier"], alert.call_args.kwargs["details"]["to_tier"]),
                         ("native", "agentic"))

    def test_staff_tier_reads_only_the_current_generation(self):
        stored = {"version": 1, "generation": 2, "tier": "agentic"}
        self.assertEqual(capability.staff_tier(connection(capabilities={"capabilityRecord": stored})), "agentic")
        self.assertIsNone(capability.staff_tier(connection(generation=3, capabilities={"capabilityRecord": stored})))
        self.assertIsNone(capability.staff_tier(None))
        counts = capability.staff_tier_counts([connection(capabilities={"capabilityRecord": stored}), connection()])
        self.assertEqual((counts["agentic"], counts["unknown"]), (1, 1))
