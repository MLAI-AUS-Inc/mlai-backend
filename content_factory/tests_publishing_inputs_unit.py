"""Actual CF candidate fixtures and provider promotion fences; no DB or network."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from . import website_publishing_inputs as inputs, website_verification as verification
from .website_contract import WebsiteAuthorityError, evidence_digest
from .website_failure_contract import canonical_bytes
from . import tests_website_hosted_verification_unit as hosted_fixtures

FIXTURES = Path(__file__).parent / "testdata/publishing-inputs-v1"


def fixture(name="react-generated"):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def checksum(value):
    value["digest"] = hashlib.sha256(canonical_bytes(value["payload"])).hexdigest()
    return value


class PublishingInputsTests(SimpleTestCase):
    def test_actual_producer_fixtures_roundtrip_without_defaulting_or_mutation(self):
        for name in ("react-generated", "static-generated"):
            with self.subTest(name=name):
                original = fixture(name)
                parsed = inputs.validate_candidate(original)
                self.assertEqual(parsed, original)
                self.assertEqual(hashlib.sha256(canonical_bytes(parsed["payload"])).hexdigest(), original["digest"])
                parsed["payload"]["normalizer_version"] = 9
                self.assertEqual(original["payload"]["normalizer_version"], 1)

    def test_shape_types_missing_fields_and_unknown_fields_cannot_be_coerced(self):
        mutations = [
            lambda x: x.update(schema_version=True),
            lambda x: x.update(schema_version=2),
            lambda x: x.update(extra="ignored"),
            lambda x: x["payload"].update(normalizer_version=True),
            lambda x: x["payload"]["publishing"]["target"].update(supported=1),
            lambda x: x["payload"]["verification"]["oracle"].update(port="3000"),
            lambda x: x["payload"]["verification"]["oracle"].update(port=3000.0),
            lambda x: x["payload"]["verification"]["execution"].pop("app_root"),
            lambda x: x["payload"]["verification"]["execution"].update(preview_command=[]),
            lambda x: x["payload"]["publishing"]["target"]["metadata_strategy"].update(other_behavior="new"),
            lambda x: x["payload"]["publishing"]["source_selection"].update(unknown="main"),
            lambda x: x["payload"]["verification"]["execution"].update(environment_names=["SECRET"]),
        ]
        for mutate in mutations:
            value = fixture(); mutate(value); checksum(value)
            with self.subTest(mutation=mutations.index(mutate)), self.assertRaises(inputs.PublishingInputsError):
                inputs.validate_candidate(value)

    def test_checksum_and_publishing_execution_consistency_are_both_required(self):
        value = fixture()
        value["payload"]["publishing"]["target"]["route_path"] = "/changed"
        with self.assertRaises(inputs.PublishingInputsError):
            inputs.validate_candidate(value)
        checksum(value)
        with self.assertRaises(inputs.PublishingInputsError):
            inputs.validate_candidate(value)
        value = fixture("static-generated")
        value["payload"]["verification"]["execution"]["static_source_materialization"]["materializer_command"] = ["python", "different.py"]
        with self.assertRaises(inputs.PublishingInputsError):
            inputs.validate_candidate(checksum(value))

    def test_paths_commands_and_private_values_are_rejected_without_echo(self):
        for key, value in (("build_cwd", "../outside"), ("preview_cwd", "/tmp"),
                           ("app_root", ".git"), ("install_cwd", "src/{slug}"),
                           ("preview_command", ["https://user:password@host.test/build"]),
                           ("preview_command", ["tool", "--token", "synthetic-private-value"]),
                           ("preview_command", ["tool", "Authorization: Bearer synthetic-private-value"]),
                           ("preview_command", ["env", "GITHUB_TOKEN=synthetic-private-value", "build"]),
                           ("build_command", [""]), ("build_command", ["x" * 1001])):
            seal = fixture(); seal["payload"]["verification"]["execution"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(inputs.PublishingInputsError) as error:
                inputs.validate_candidate(checksum(seal))
            self.assertEqual(str(error.exception), "publishing_inputs_candidate_invalid")

    def test_unicode_is_stable_and_canonical_absence_is_preserved(self):
        seal = fixture(); seal["payload"]["verification"]["oracle"]["expected_h1"] = "Café 澳大利亚"
        checksum(seal)
        self.assertEqual(inputs.validate_candidate(seal), seal)
        self.assertEqual(seal["payload"]["publishing"]["source_selection"], {})
        with_root = deepcopy(seal); with_root["payload"]["publishing"]["source_selection"]["app_root"] = ""
        checksum(with_root)
        self.assertNotEqual(with_root["digest"], seal["digest"])
        self.assertEqual(inputs.validate_candidate(with_root), with_root)
        seal["payload"]["verification"]["oracle"]["expected_h1"] = "Token budgets and password managers"
        self.assertEqual(inputs.validate_candidate(checksum(seal)), seal)

    def test_file_paths_cannot_be_empty_or_name_a_directory(self):
        for key in ("registry_path", "seo_config_path"):
            for path in ("", "."):
                seal = fixture()
                seal["payload"]["publishing"]["target"]["registration_strategy"][key] = path
                with self.subTest(key=key, path=path), self.assertRaises(inputs.PublishingInputsError):
                    inputs.validate_candidate(checksum(seal))
        for key in ("example_path",):
            seal = fixture(); seal["payload"]["publishing"]["target"][key] = "."
            with self.assertRaises(inputs.PublishingInputsError):
                inputs.validate_candidate(checksum(seal))

    def test_outer_target_must_match_frozen_inputs_and_candidate_does_not_make_proof(self):
        seal = fixture(); target = deepcopy(seal["payload"]["publishing"]["target"])
        target["contract"] = {"publishing_inputs_v1": seal}
        self.assertEqual(inputs.candidate_for_target(target), seal)
        target["live_marker"].pop("schema_version"); target["live_marker"].pop("meta_name")
        self.assertEqual(inputs.candidate_for_target(target), seal)
        target["route_template"] = "/other/{slug}"
        with self.assertRaises(inputs.PublishingInputsError):
            inputs.candidate_for_target(target)
        for invalid in (None, {}, [], True):
            target["contract"]["publishing_inputs_v1"] = invalid
            self.assertTrue(inputs.has_candidate(target))
            with self.assertRaises(inputs.PublishingInputsError):
                inputs.candidate_for_target(target)
        self.assertIsNone(inputs.candidate_for_target({"contract": {}}))

    def test_unknown_outer_behavior_or_conflicting_nested_marker_is_rejected(self):
        for mutation in ("outer_behavior", "contract_behavior", "nested_marker"):
            seal = fixture(); target = deepcopy(seal["payload"]["publishing"]["target"])
            target["contract"] = {"publishing_inputs_v1": seal}
            if mutation == "outer_behavior":
                target["new_execution_behavior"] = {"command": ["other"]}
            elif mutation == "contract_behavior":
                target["contract"]["command"] = ["other"]
            else:
                target["contract"]["live_marker"] = {**target["live_marker"], "value": "0" * 64}
            with self.subTest(mutation=mutation), self.assertRaises(inputs.PublishingInputsError):
                inputs.candidate_for_target(target)

    def test_scan_stores_candidate_without_promoting_worker_claimed_verification(self):
        from . import website_connections as authority
        seal = fixture(); target = deepcopy(seal["payload"]["publishing"]["target"])
        target["contract"] = {"publishing_inputs_v1": seal}
        sha = "a" * 40
        target["verification"] = {"status": "verified", "source_sha": sha, "preview_capable": True}
        targets = Mock()
        targets.filter.side_effect = lambda **kw: SimpleNamespace(first=lambda: None) if "target_key" in kw else []
        connection = SimpleNamespace(pk="site", generation=3, verified_sha="", last_verified_at=None,
            targets=targets, capabilities={}, blockers=[], state="connected", save=Mock())
        config = SimpleNamespace(article_template="<article>Example</article>", design_guide="Original classes")
        with patch.object(authority, "_authority_depth") as depth, patch.object(authority, "_verified_native_targets") as native, \
                patch.object(authority.WebsiteScanSnapshot.objects, "get_or_create"), \
                patch.object(authority.WebsiteConnectionTarget.objects, "update_or_create") as write, \
                patch.object(authority.OrganizationContentConfig.objects, "get", return_value=config):
            depth.get.return_value = 1; native.get.return_value = {("site", 3)}
            authority.record_scan_evidence(connection, {"source_sha": sha, "publish_targets": [target]})
        saved = write.call_args.kwargs["defaults"]
        self.assertEqual(saved["contract"], target)
        self.assertIsNone(saved["verified_at"])
        self.assertFalse(saved["capabilities"]["publishingReady"])
        self.assertFalse(connection.capabilities["publishingReady"])
        self.assertFalse(connection.capabilities["generationReady"])
        self.assertFalse(connection.capabilities["previewSupported"])

    def test_scan_cannot_strip_a_pending_candidate_and_promote_legacy_flags(self):
        from . import website_connections as authority
        for scan_sha in ("a" * 40, "b" * 40):
            seal = fixture(); incoming = deepcopy(seal["payload"]["publishing"]["target"])
            incoming["verification"] = {"status": "verified", "source_sha": scan_sha, "preview_capable": True}
            saved_contract = {**deepcopy(incoming), "contract": {"publishing_inputs_v1": seal}}
            previous = SimpleNamespace(generation=3, source_sha="a" * 40, verified_at=None,
                capabilities={}, adapter="react_component", contract=saved_contract)
            targets = Mock()
            targets.filter.side_effect = lambda **kw: SimpleNamespace(first=lambda: previous) if "target_key" in kw else []
            connection = SimpleNamespace(pk="site", generation=3, verified_sha="", last_verified_at=None,
                targets=targets, capabilities={}, blockers=[], state="connected", save=Mock())
            config = SimpleNamespace(article_template="<article>Example</article>", design_guide="Original classes")
            with self.subTest(scan_sha=scan_sha), patch.object(authority, "_authority_depth") as depth, \
                    patch.object(authority, "_verified_native_targets") as native, \
                    patch.object(authority.WebsiteScanSnapshot.objects, "get_or_create"), \
                    patch.object(authority.WebsiteConnectionTarget.objects, "update_or_create") as write, \
                    patch.object(authority.OrganizationContentConfig.objects, "get", return_value=config):
                depth.get.return_value = 1; native.get.return_value = {("site", 3)}
                authority.record_scan_evidence(connection, {"source_sha": scan_sha, "publish_targets": [incoming]})
            write.assert_not_called()
            self.assertEqual(previous.contract, saved_contract)
            self.assertFalse(connection.capabilities["publishingReady"])
            self.assertFalse(connection.capabilities["generationReady"])

    def test_config_writer_preserves_pending_candidate_and_selected_identity(self):
        from .incident_guards import protect_certified_config
        seal = fixture(); original = deepcopy(seal["payload"]["publishing"]["target"])
        original["contract"] = {"publishing_inputs_v1": seal}
        incoming = deepcopy(original); incoming.pop("contract")
        pending = SimpleNamespace(generation=3, source_sha="a" * 40, verified_at=None,
            target_key=original["target_id"], contract=original)
        connection = SimpleNamespace(generation=3, verified_sha="a" * 40, targets=Mock())
        connection.targets.filter.side_effect = lambda *args, **kw: [] if kw.get("verified_at__isnull") is False else [pending]
        config = SimpleNamespace(website_connection=connection, default_publish_target_id=original["target_id"], publish_targets=[original])
        for targets in ([incoming], []):
            with self.subTest(targets=bool(targets)):
                saved = protect_certified_config(config, {"publish_targets": targets, "default_publish_target_id": "other"})
                self.assertIn(original, saved["publish_targets"])
                self.assertEqual(saved["default_publish_target_id"], original["target_id"])


@override_settings(ARTICLES_HOSTED_ATTESTATION_ENABLED=True, ARTICLES_HOSTED_VERIFICATION_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True, ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class CandidatePromotionTests(SimpleTestCase):
    def runtime(self):
        helper = hosted_fixtures.HostedPromotionTests()
        return helper, helper.fixture()

    def test_legacy_provider_cannot_promote_any_candidate_even_malformed(self):
        for candidate in (fixture(), None, {}):
            helper, value = self.runtime()
            value.target.contract["contract"] = {"publishing_inputs_v1": candidate}
            with self.subTest(candidate=type(candidate)), self.assertRaises(WebsiteAuthorityError) as error:
                helper.promote(value)
            self.assertEqual(error.exception.code, "ci_attestation_contract_mismatch")
            value.target.save.assert_not_called()
            value.http.assert_not_called()

    def test_query_schema_version_is_transport_only_and_stored_proof_stays_strict(self):
        helper, value = self.runtime()
        receipt = helper.promote(value).receipt
        with value.boundaries():
            query = {**value.sealed, "schema_version": "2"}
            self.assertEqual(verification.ci_attestation_for(value.website, query, query=True), receipt)
            with self.assertRaises(WebsiteAuthorityError):
                verification.ci_attestation_for(value.website, query)
            for invalid in ("2", 2.0, True, "3", 3):
                with self.subTest(provider_schema=invalid), self.assertRaises(WebsiteAuthorityError):
                    verification.validated_ci_identity(value.sealed, {**value.sealed, "schema_version": invalid})
            receipt["schema_version"] = "2"
            with self.assertRaises(WebsiteAuthorityError):
                verification.ci_attestation_for(value.website, query, query=True)

    def test_candidate_added_during_provider_read_is_rejected_before_promotion(self):
        from . import website_hosted_verification as hosted
        from .website_models import WebsiteConnection
        helper, value = self.runtime()
        def provider(*args):
            value.target.contract["contract"] = {"publishing_inputs_v1": fixture()}
            return value.sealed
        with value.boundaries(), patch.object(WebsiteConnection.objects, "get", return_value=value.website), patch.object(hosted, "read_completed_proof", side_effect=provider) as read:
            with self.assertRaises(WebsiteAuthorityError) as error:
                verification.record_ci_attestation({**value.sealed, "run_id": "native-run", "source_run_id": "setup-parent"}, origin="authenticated_hosted")
            read.assert_called_once()
            self.assertEqual(error.exception.code, "ci_attestation_contract_mismatch")
        value.target.save.assert_not_called()

    def test_self_reported_schema3_is_never_accepted_by_legacy_receiver(self):
        _, value = self.runtime()
        for change in ({"schema_version": 3}, {"publishing_inputs_version": 1, "publishing_inputs_digest": fixture()["digest"]}):
            proof = {**value.sealed, **change}
            proof["evidence_digest"] = evidence_digest({key: item for key, item in proof.items() if key != "evidence_digest"})
            with self.subTest(change=change), self.assertRaises(WebsiteAuthorityError) as error:
                verification.validated_ci_identity(proof, proof)
            self.assertEqual(error.exception.code, "ci_attestation_contract_mismatch")

    def test_saved_legacy_receipt_cannot_make_candidate_current_or_live(self):
        helper, value = self.runtime()
        helper.promote(value)
        value.target.contract["contract"] = {"publishing_inputs_v1": fixture()}
        with value.boundaries():
            self.assertIsNone(verification.current_ci_attestation(value.website, value.target))
            with self.assertRaises(WebsiteAuthorityError) as error:
                verification.ci_attestation_for(value.website, value.sealed)
            self.assertEqual(error.exception.code, "ci_attestation_contract_mismatch")
            with self.assertRaises(WebsiteAuthorityError) as error:
                verification.verify_live_deployment(value.config, data=value.sealed)
            self.assertEqual(error.exception.code, "ci_attestation_contract_mismatch")
        value.http.assert_not_called()


class PublishingInputsProvenanceTests(SimpleTestCase):
    def prepared(self, root):
        from scripts import check_publishing_inputs_contract as provenance
        files = {}
        lock = {"schema_version": 1, "source_repository": provenance.SOURCE_REPOSITORY,
            "source_revision": "a" * 40, "artifacts": {}}
        for path, source in provenance.ARTIFACTS.items():
            raw = (provenance.ROOT / path).read_bytes()
            local = root / path; local.parent.mkdir(parents=True, exist_ok=True); local.write_bytes(raw)
            files[source] = raw
            lock["artifacts"][path] = {"source_path": source, "sha256": hashlib.sha256(raw).hexdigest()}
        (root / "contracts/publishing-inputs-v1.lock.json").write_text(json.dumps(lock))
        return provenance, lock, files

    def test_remote_checks_every_artifact_at_exact_pin_with_no_mutation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); provenance, lock, files = self.prepared(root)
            fetch = Mock(side_effect=lambda path, revision, token: files[path])
            self.assertEqual(provenance.verify(root, remote=True, token="synthetic", fetch=fetch), lock)
            self.assertEqual(fetch.call_count, 3)
            self.assertTrue(all(call.args[1:] == ("a" * 40, "synthetic") for call in fetch.call_args_list))

    def test_missing_token_or_changed_private_bytes_cannot_pass(self):
        from scripts.check_failure_contract import ContractCheckError
        with TemporaryDirectory() as directory:
            root = Path(directory); provenance, _, _ = self.prepared(root)
            fetch = Mock(return_value=b"changed")
            with self.assertRaises(ContractCheckError):
                provenance.verify(root, remote=True, fetch=fetch)
            fetch.assert_not_called()
            with self.assertRaises(ContractCheckError):
                provenance.verify(root, remote=True, token="synthetic", fetch=fetch)

    def test_mutable_ref_and_changed_local_schema_are_rejected(self):
        from scripts.check_failure_contract import ContractCheckError
        with TemporaryDirectory() as directory:
            root = Path(directory); provenance, lock, _ = self.prepared(root)
            lock["source_revision"] = "main"
            (root / "contracts/publishing-inputs-v1.lock.json").write_text(json.dumps(lock))
            with self.assertRaises(ContractCheckError):
                provenance.verify(root)
            provenance, _, _ = self.prepared(root)
            (root / "contracts/publishing-inputs-v1.schema.json").write_text("{}")
            with self.assertRaises(ContractCheckError):
                provenance.verify(root)
