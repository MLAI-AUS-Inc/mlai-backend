"""Source-pinned producer component fixtures; no DB, providers or promotion."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from contextlib import redirect_stdout
from io import StringIO
import hashlib

from django.test import SimpleTestCase, override_settings

from . import publisher_contract_v2 as protocol, website_publisher_inputs_v2 as inputs
from . import website_publishing_inputs as legacy, website_verification as verification
from .website_contract import WebsiteAuthorityError
from .tests_publishing_inputs_unit import hosted_fixtures

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "testdata/publisher-inputs-v2/component.json"


def example():
    return json.loads(FIXTURE.read_text())


def received(value, original=None, **changes):
    original = original or example()
    return inputs.validate_component(value, **{"candidate": original["candidate"], "origin": "authenticated_hosted",
        "original_binding": original["original_binding"], "trusted_profile": original["trusted_profile"], **changes})


def paths(value, path=()):
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items(): yield from paths(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value): yield from paths(child, (*path, index))


class PublisherInputsV2Tests(SimpleTestCase):
    def test_actual_pinned_component_roundtrips_but_remains_unqualified(self):
        original = example()
        self.assertEqual(inputs.validate_candidate(original["candidate"]), original["candidate"])
        parsed = received(original["receipt"])
        self.assertEqual(parsed, original["receipt"])
        self.assertIs(parsed["pipeline_verified"], False)
        self.assertIs(parsed["publication_authority"], False)
        self.assertEqual(original["provenance"]["component_ci_run"], 39117612063)
        parsed["component_result"]["publisher_execution"]["source_sha"] = "f" * 40
        self.assertEqual(example(), original)

    def test_every_object_and_scalar_rejects_unknown_fields_and_coercion(self):
        original = example()
        for kind in ("candidate", "receipt"):
            for path, item in paths(original[kind]):
                if path == (("digest",) if kind == "candidate" else ("receipt_id",)): continue
                value = deepcopy(original[kind])
                if not path:
                    value["unknown_field"] = "unrepresented"
                else:
                    parent = value
                    for key in path[:-1]: parent = parent[key]
                    if isinstance(item, dict): parent[path[-1]]["unknown_field"] = "unrepresented"
                    else: parent[path[-1]] = True if type(item) is int else 1 if type(item) is bool else {} if isinstance(item, list) else []
                with self.subTest(kind=kind, path=path), self.assertRaises(protocol.PublisherContractError):
                    if kind == "candidate":
                        value["digest"] = protocol.digest(value["payload"])
                        inputs.validate_candidate(value)
                    else:
                        value["receipt_id"] = protocol.digest({key: item for key, item in value.items() if key != "receipt_id"})
                        received(value)

    def test_producer_schemas_and_vendored_reader_are_identical(self):
        for name, schema in zip(("publishing-inputs-v2.schema.json", "publisher-component-v1.schema.json"), inputs.schemas()):
            self.assertEqual(json.loads((ROOT / "contracts" / name).read_text()), schema)
        with self.assertRaises(legacy.PublishingInputsError): legacy.validate_candidate(example()["candidate"])

    def test_saved_original_identity_and_request_digest_cannot_be_hydrated_or_rebound(self):
        original = example()
        for key in original["original_binding"]:
            for missing in (False, True):
                binding = deepcopy(original["original_binding"])
                if missing: binding.pop(key)
                else: binding[key] = binding[key] + 1 if type(binding[key]) is int else "changed-original-value"
                with self.subTest(key=key, missing=missing), self.assertRaises(protocol.PublisherContractError):
                    received(original["receipt"], original_binding=binding)

    def test_receipt_origin_and_executable_profile_are_receiver_inputs(self):
        original = example()
        for origin in (None, "caller", "authenticated_ci", True):
            with self.subTest(origin=origin), self.assertRaises(protocol.PublisherContractError):
                received(original["receipt"], origin=origin)
        for key in original["trusted_profile"]["executor"]:
            profile = deepcopy(original["trusted_profile"])
            profile["executor"][key] = "changed-supported-profile"
            with self.subTest(key=key), self.assertRaises(protocol.PublisherContractError):
                received(original["receipt"], trusted_profile=profile)
        profile = deepcopy(original["trusted_profile"]); profile["wrapper_sha256"] = "f" * 64
        with self.assertRaises(protocol.PublisherContractError): received(original["receipt"], trusted_profile=profile)
        profile = deepcopy(original["trusted_profile"]); profile["native_verifier"]["runner_sha256"] = "f" * 64
        with self.assertRaises(protocol.PublisherContractError): received(original["receipt"], trusted_profile=profile)

    def test_component_cannot_be_sent_to_legacy_or_schema3_ready_parsers(self):
        from .website_sealed_verification import validate_hosted_sealed_proof
        original = example()
        receipt = received(original["receipt"])
        with self.assertRaises(WebsiteAuthorityError): verification.require_legacy_proof_inputs(proof=receipt)
        with self.assertRaises(WebsiteAuthorityError):
            validate_hosted_sealed_proof(receipt, origin="authenticated_hosted", binding={},
                candidate=original["candidate"]["payload"]["native_inputs_v1"], supported_verifier={})

    def test_resealed_receipt_does_not_make_changed_execution_or_authority_valid(self):
        original = example()
        for kind in ("source_sha", "source_tree_sha", "controller_sha256", "recipe_digest", "record_digests",
                "runtime_image", "network", "pipeline_verified", "publication_authority", "prior_article"):
            value = deepcopy(original["receipt"])
            result = value["component_result"]
            if kind in {"source_sha", "source_tree_sha", "controller_sha256", "recipe_digest"}:
                result["publisher_execution"][kind] = "f" * (40 if kind.startswith("source_") else 64)
            elif kind == "record_digests": result["publisher_execution"][kind][0] = "f" * 64
            elif kind == "runtime_image": result["isolation"][kind] = "sha256:" + "f" * 64
            elif kind == "network": result["isolation"][kind] = "registry_allowlist"
            elif kind in {"pipeline_verified", "publication_authority"}: value[kind] = True
            else:
                first, second = result["publisher_component"]["successive_publications"]
                second["new_detail_sha256"][next(iter(first["new_detail_sha256"]))] = "f" * 64
            value["receipt_id"] = protocol.digest({key: item for key, item in value.items() if key != "receipt_id"})
            with self.subTest(kind=kind), self.assertRaises(protocol.PublisherContractError): received(value)

    def test_private_argv_is_not_echoed_or_accepted_after_resealing(self):
        for argument in ("PROVIDER_TOKEN=private-canary", "SERVICE_API_KEY=private-canary", "--token",
                "Authorization: Bearer private-canary", "https://user:private-canary@fixture.invalid/"):
            value = example()["candidate"]
            value["payload"]["recipe"]["publisher_argv"].append(argument)
            value["digest"] = protocol.digest(value["payload"])
            with self.subTest(argument=argument), self.assertRaises(protocol.PublisherContractError) as error:
                inputs.validate_candidate(value)
            self.assertEqual(str(error.exception), "publisher_contract_invalid")


@override_settings(ARTICLES_HOSTED_ATTESTATION_ENABLED=True, ARTICLES_HOSTED_VERIFICATION_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True, ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class LegacyPublisherV2FenceTests(SimpleTestCase):
    def runtime(self):
        helper = hosted_fixtures.HostedPromotionTests()
        return helper, helper.fixture()

    def test_presence_even_malformed_refuses_legacy_promotion_without_provider_or_save(self):
        for version in ("publishing_inputs_v2", "publishing_inputs_v3", "publishing_inputs_v99"):
            for candidate in (None, {}, example()["candidate"]):
                helper, value = self.runtime()
                value.target.contract["contract"] = {version: candidate}
                with self.subTest(version=version, candidate=type(candidate)), self.assertRaises(WebsiteAuthorityError):
                    helper.promote(value)
                value.target.save.assert_not_called(); value.http.assert_not_called()
                self.assertTrue(legacy.has_candidate(value.target.contract))
                with self.assertRaises(legacy.PublishingInputsError): legacy.candidate_for_target(value.target.contract)

    def test_saved_legacy_receipt_cannot_make_newer_target_current_or_live(self):
        helper, value = self.runtime()
        helper.promote(value)
        with value.boundaries(): self.assertIsNotNone(verification.current_ci_attestation(value.website, value.target))
        value.target.contract["contract"] = {"publishing_inputs_v2": example()["candidate"]}
        value.http.reset_mock()
        with value.boundaries():
            self.assertIsNone(verification.current_ci_attestation(value.website, value.target))
            with self.assertRaises(WebsiteAuthorityError): verification.verify_live_deployment(value.config, data=value.sealed)
        value.http.assert_not_called()


class PublisherV2ProvenanceTests(SimpleTestCase):
    def tree(self, root):
        from scripts import check_publisher_v2_contract as check
        records, remote = {}, {}
        producer = b"synthetic producer helper for provenance boundary tests\n"
        for path, source in check.ARTIFACTS.items():
            data = (ROOT / path).read_bytes()
            target = root / path; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
            records[path] = {"source_path": source, "sha256": hashlib.sha256(data).hexdigest()}
            remote[source] = data
        lock = {"schema_version": 1, "source_repository": check.SOURCE_REPOSITORY, "source_revision": "a" * 40,
            "artifacts": records, "producer": {"source_path": check.PRODUCER, "sha256": hashlib.sha256(producer).hexdigest()}}
        remote[check.PRODUCER] = producer
        (root / check.LOCK).write_text(json.dumps(lock))
        def fetch(source, revision, token):
            self.assertEqual(revision, "a" * 40); self.assertEqual(token, "synthetic-read-only-key")
            return remote[source]
        return lock, remote, fetch

    def test_exact_artifacts_and_actual_producer_are_read_from_one_immutable_pin(self):
        from scripts import check_publisher_v2_contract as check
        with TemporaryDirectory() as directory:
            root = Path(directory); lock, _, fetch = self.tree(root)
            observed = Mock(side_effect=fetch)
            self.assertEqual(check.verify(root, remote=True, token="synthetic-read-only-key", fetch=observed), lock)
            self.assertEqual(observed.call_count, 5)

    def test_moving_refs_missing_tokens_and_remapped_paths_are_not_provenance(self):
        from scripts import check_publisher_v2_contract as check
        for fault in ("main", "short_sha", "token", "path", "producer"):
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                root = Path(directory); lock, _, _ = self.tree(root)
                if fault in {"main", "short_sha"}: lock["source_revision"] = "main" if fault == "main" else "a" * 8
                if fault == "path": lock["artifacts"][next(iter(check.ARTIFACTS))]["source_path"] = "other/source.json"
                if fault == "producer": lock["producer"]["source_path"] = "other/producer.py"
                (root / check.LOCK).write_text(json.dumps(lock))
                fetch = Mock(side_effect=AssertionError("Invalid provenance must not fetch"))
                with self.assertRaises(check.ContractCheckError):
                    check.verify(root, remote=True, token=None if fault == "token" else "synthetic-read-only-key", fetch=fetch)
                fetch.assert_not_called()

    def test_changed_local_or_remote_bytes_and_duplicate_metadata_are_rejected(self):
        from scripts import check_publisher_v2_contract as check
        for fault in ("local", "remote", "producer", "duplicate"):
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                root = Path(directory); _, remote, fetch = self.tree(root)
                if fault == "local": (root / next(iter(check.ARTIFACTS))).write_text("changed")
                if fault == "remote": remote[next(iter(check.ARTIFACTS.values()))] = b"changed"
                if fault == "producer": remote[check.PRODUCER] = b"changed"
                if fault == "duplicate":
                    path = root / check.LOCK; path.write_text(path.read_text().replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'))
                with self.assertRaises(check.ContractCheckError):
                    check.verify(root, remote=True, token="synthetic-read-only-key", fetch=fetch)

    def test_unexpected_cli_failure_does_not_print_provider_values_or_tokens(self):
        from scripts import check_publisher_v2_contract as check
        output = StringIO()
        with patch("sys.argv", ["check", "--remote"]), patch.object(check, "verify", side_effect=RuntimeError("private-canary-value")), redirect_stdout(output):
            self.assertEqual(check.main(), 1)
        self.assertNotIn("private-canary-value", output.getvalue())
