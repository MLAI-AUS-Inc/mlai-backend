"""Pure state-boundary checks; actual transactions run in disposable CI only."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

from django.test import SimpleTestCase, override_settings

from . import native_publisher_contract_v4 as protocol
from . import website_publisher_reservations_v4 as reservations
from .website_contract import WebsiteAuthorityError

ROOT = Path(__file__).resolve().parents[1]


def example():
    """Use the explicitly mocked producer fixture, never qualify a release."""
    value = json.loads((ROOT / "content_factory/testdata/native-publisher-v4/synthetic.json").read_text())
    candidate = value["candidate"]
    profile = {"profile": reservations.PROFILE, "mode": "setup_only", "source_revision": json.loads(
        (ROOT / "contracts/native-publisher-v4.lock.json").read_text())["source_revision"],
        "qualification_digest": "e" * 64, "native": value["profile"], "executor": candidate["payload"]["executor"]}
    target = {**deepcopy(candidate["payload"]["native_inputs_v1"]["payload"]["publishing"]["target"]),
        "contract_digest": "f" * 64, "contract": {"contract_digest": "f" * 64}}
    return value, profile, target


def pure_reservation():
    value, profile, target = example()
    context = {"scope": deepcopy(value["candidate"]["payload"]["original_scope"]), "target": target,
        "snapshot": {"target_id": target["target_id"], "source_run_id": "source-fixture"},
        "connection": {"website_connection_id": value["identity"]["website_connection_id"], "connection_generation": 2,
            "repository_id": 17, "github_repo": "fixture/site", "app_root": "", "branch": "main", "domain": "fixture.invalid"},
        "current_source_sha": value["current_binding"]["source_sha"]}
    original = SimpleNamespace(pk=UUID("00000000-0000-4000-8000-000000000101"), action="publisher-original", state="running",
        connection_id=UUID(context["scope"]["website_connection_id"]), generation=2,
        payload={"version": 1, "context": context, "facts": value["candidate"]["payload"]["baseline"], "profile": profile})
    proposal = reservations._proposal(original, value["candidate"], value["records"])
    original.payload["proposal"] = proposal
    child = SimpleNamespace(pk=UUID(value["identity"]["operation_id"]), action="workflow", state="pending",
        connection_id=original.connection_id, generation=2, payload={"workflow": "hosted_native_verification", "attempt": 1,
            "publisher_original_operation_id": str(original.pk), "run_id": value["identity"]["operation_id"], "source_run_id": "source-fixture"})
    child.idempotency_key = "publisher-v4:" + protocol.digest({"original": str(original.pk), "source": context["current_source_sha"], "proposal": proposal})
    child.payload["binding"] = reservations._execution_binding(child, context, proposal)
    child.payload[reservations.PENDING] = {"version": 1, "original_id": str(original.pk), "context": context, "profile": profile,
        **proposal, "current_binding": value["current_binding"], "identity": value["identity"]}
    return value, profile, context, original, child


class PublisherV4ProfileTests(SimpleTestCase):
    def write(self, directory, profile):
        root = Path(directory)
        path = root / "native-verifier-v4.json"
        path.write_text(json.dumps({"schema_version": 1, "profiles": [profile]}))
        (root / "native-publisher-v4.lock.json").write_bytes((ROOT / "contracts/native-publisher-v4.lock.json").read_bytes())
        return path

    def test_default_rollout_and_release_list_are_disabled(self):
        from django.conf import settings
        self.assertFalse(settings.ARTICLES_PUBLISHING_INPUTS_V4_ENABLED)
        self.assertEqual(json.loads(reservations.MANIFEST.read_text()), {"schema_version": 1, "profiles": []})
        with self.assertRaises(WebsiteAuthorityError):
            reservations.supported_profile()

    def test_only_the_shipped_exact_profile_can_supply_expected_executable_identity(self):
        _, profile, _ = example()
        with TemporaryDirectory() as directory:
            path = self.write(directory, profile)
            with patch.object(reservations, "MANIFEST", path):
                parsed = reservations.supported_profile()
            self.assertEqual(parsed, profile)
            parsed["native"]["runtime_image"] = "changed"
            self.assertNotEqual(parsed, profile)

    def test_bad_profile_metadata_types_source_and_runtime_are_not_qualifications(self):
        for fault in ("unknown", "mode", "source", "moving", "runtime", "version", "duplicate", "two_profiles"):
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                _, profile, _ = example()
                path = self.write(directory, profile)
                manifest = json.loads(path.read_text())
                if fault == "unknown": manifest["profiles"][0]["unknown"] = True
                elif fault == "mode": manifest["profiles"][0]["mode"] = "publishing"
                elif fault == "source": manifest["profiles"][0]["source_revision"] = "a" * 40
                elif fault == "moving": manifest["profiles"][0]["source_revision"] = "main"
                elif fault == "runtime": manifest["profiles"][0]["native"]["runtime_image"] = "sha256:" + "a" * 64
                elif fault == "version": manifest["schema_version"] = True
                elif fault == "two_profiles": manifest["profiles"].append(deepcopy(profile))
                path.write_text(json.dumps(manifest))
                if fault == "duplicate": path.write_text(path.read_text().replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'))
                with patch.object(reservations, "MANIFEST", path), self.assertRaises(WebsiteAuthorityError):
                    reservations.supported_profile()

    @override_settings(ARTICLES_PUBLISHING_INPUTS_V4_ENABLED=False)
    def test_v4_switch_cannot_be_borrowed_from_v3_or_a_payload(self):
        with patch("content_factory.website_hosted_verification.enabled", return_value=True):
            self.assertFalse(reservations.enabled(SimpleNamespace()))


class PublisherV4FrozenInputsTests(SimpleTestCase):
    def test_source_observation_is_exact_closed_and_independent(self):
        value = example()[0]["current_binding"]
        self.assertEqual(reservations.source_facts(value, value["source_sha"]), value)
        for key in value:
            for change in (None, True, "not-a-sha"):
                changed = {**value, key: change}
                with self.subTest(key=key, change=type(change)), self.assertRaises(WebsiteAuthorityError):
                    reservations.source_facts(changed, value["source_sha"])
        with self.assertRaises(WebsiteAuthorityError):
            reservations.source_facts({**value, "extra": "not observed"}, value["source_sha"])

    def test_original_scope_target_and_facts_cannot_come_from_a_rehashed_proposal(self):
        for fault in ("scope", "source", "profile", "target", "record", "replan"):
            value, _, _, original, _ = pure_reservation()
            candidate, records = deepcopy(value["candidate"]), deepcopy(value["records"])
            if fault == "scope": original.payload["context"]["scope"]["setup_operation_attempt"] += 1
            elif fault == "source": original.payload["facts"]["source_tree_sha"] = "e" * 40
            elif fault == "profile": original.payload["profile"]["executor"]["controller_sha256"] = "e" * 64
            elif fault == "target": original.payload["context"]["target"]["route_path"] = "/another"
            elif fault == "record": records[0]["title"] = "Another hypothetical article"
            else:
                records[0]["title"] = "A different frozen plan"
                candidate["payload"]["probe_record_digests"] = [protocol.digest(row) for row in records]
                candidate["digest"] = protocol.digest(candidate["payload"])
            with self.subTest(fault=fault), self.assertRaises(WebsiteAuthorityError):
                reservations._proposal(original, candidate, records)

    def test_reservation_fence_rejects_cross_connection_cancelled_and_coerced_state(self):
        for fault in ("connection", "generation", "state", "version", "context", "profile"):
            _, profile, context, original, _ = pure_reservation()
            profile, context = deepcopy(profile), deepcopy(context)
            if fault == "connection": original.connection_id = UUID("00000000-0000-4000-8000-000000000199")
            elif fault == "generation": original.generation += 1
            elif fault == "state": original.state = "cancelled"
            elif fault == "version": original.payload["version"] = True
            elif fault == "context": context["current_source_sha"] = "d" * 40
            else: profile["qualification_digest"] = "d" * 64
            with self.subTest(fault=fault), self.assertRaises(WebsiteAuthorityError):
                reservations._fence(original, context, profile)

    def test_every_current_identity_binding_and_saved_proposal_remains_fenced(self):
        for fault in ("connection", "generation", "state", "attempt", "workflow", "run", "source_run", "key", "binding", "identity", "facts", "proposal", "version"):
            _, profile, context, original, child = pure_reservation()
            child.payload = deepcopy(child.payload)
            if fault == "connection": child.connection_id = UUID("00000000-0000-4000-8000-000000000199")
            elif fault == "generation": child.generation += 1
            elif fault == "state": child.state = "cancelled"
            elif fault == "attempt": child.payload["attempt"] = True
            elif fault == "workflow": child.payload["workflow"] = "article_generation"
            elif fault == "run": child.payload["run_id"] = "other-run"
            elif fault == "source_run": child.payload["source_run_id"] = "other-setup"
            elif fault == "key": child.idempotency_key += "changed"
            elif fault == "binding": child.payload["binding"]["operation_attempt"] = True
            elif fault == "identity": child.payload[reservations.PENDING]["identity"]["operation_attempt"] = True
            elif fault == "facts": child.payload[reservations.PENDING]["current_binding"]["source_sha"] = "d" * 40
            elif fault == "proposal": child.payload[reservations.PENDING]["records"][0]["title"] = "Changed"
            else: child.payload[reservations.PENDING]["version"] = True
            with self.subTest(fault=fault), self.assertRaises(WebsiteAuthorityError):
                reservations._pending(child, original, context, profile)

    def test_frozen_execution_uses_the_actual_reader_and_no_publication_authority(self):
        value, profile, context, original, child = pure_reservation()
        pending = reservations._pending(child, original, context, profile)
        self.assertEqual(reservations._validate(value["receipt"], pending), value["receipt"])
        self.assertFalse(value["receipt"]["publication_authority"])

    def test_observer_must_run_after_the_lock_guard_and_never_echo_provider_contents(self):
        value = example()[0]["current_binding"]
        with patch("content_factory.website_connections.require_unlocked_remote_call") as unlocked:
            def observe(request):
                unlocked.assert_called_once()
                request["private_mutation"] = True
                return value
            request = {"expected_source_sha": value["source_sha"]}
            self.assertEqual(reservations._observe(observe, request), value)
            self.assertNotIn("private_mutation", request)
        with patch("content_factory.website_connections.require_unlocked_remote_call"), self.assertRaises(WebsiteAuthorityError) as failed:
            reservations._observe(Mock(side_effect=RuntimeError("private-provider-canary")), {"expected_source_sha": value["source_sha"]})
        self.assertEqual(failed.exception.code, "worker_unavailable")
        self.assertNotIn("private-provider", str(failed.exception))

    def test_real_attestation_entry_routes_schema4_only_to_the_new_state_boundary(self):
        from .website_verification import record_ci_attestation
        value = example()[0]["receipt"]
        with patch.object(reservations, "record_native_setup", return_value="setup-only") as record:
            self.assertEqual(record_ci_attestation(value, origin="authenticated_hosted"), "setup-only")
        record.assert_called_once_with(value, origin="authenticated_hosted")
