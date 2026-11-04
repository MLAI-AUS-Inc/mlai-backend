"""Actual CF verifier-entry fixture plus hostile mutations; no provider or DB.

Fixture generation substitutes process/browser boundaries. Its observed flags
are parser test data, not evidence of a deployed integration or a passing zoo.
"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from django.test import SimpleTestCase

from . import website_sealed_verification as sealed
from .website_contract import WebsiteAuthorityError, evidence_digest as legacy_digest
from .website_failure_contract import canonical_bytes

FIXTURE = Path(__file__).parent / "testdata/sealed-verification-v3/static.json"


def checksum(candidate):
    candidate["digest"] = hashlib.sha256(canonical_bytes(candidate["payload"])).hexdigest()
    return candidate


class SealedVerificationTests(SimpleTestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE.read_text())
        self.candidate = deepcopy(self.fixture["candidate"])
        self.proof = deepcopy(self.fixture["proof"])
        self.supported = deepcopy(self.fixture["supported_verifier"])
        self.binding = {key: self.proof[key] for key in sealed.SCOPE_FIELDS | sealed.SEALED_FIELDS}
        self.binding["expected_source_sha"] = self.proof["source_sha"]
        self.binding.update(self.candidate["payload"]["publishing"]["source_selection"])

    def check(self, proof=None, **kwargs):
        values = {"origin": "authenticated_hosted", "binding": self.binding,
            "candidate": self.candidate, "supported_verifier": self.supported}
        values.update(kwargs)
        return sealed.validate_hosted_sealed_proof(self.proof if proof is None else proof, **values)

    def resign(self, proof):
        proof["evidence_digest"] = sealed.sealed_evidence_digest(proof)
        return proof

    def reject(self, proof=None, **kwargs):
        with self.assertRaises(WebsiteAuthorityError) as error:
            self.check(proof, **kwargs)
        self.assertEqual(error.exception.code, "ci_attestation_contract_mismatch")
        self.assertEqual(str(error.exception), "Hosted evidence does not match the original sealed execution inputs.")

    def test_actual_verifier_receipt_roundtrip_and_copy_is_independent(self):
        before = deepcopy((self.proof, self.candidate, self.binding, self.supported))
        result = self.check()
        self.assertEqual(result, self.proof)
        result["verified_routes"]["listing"] = "/changed"
        self.assertEqual(before, (self.proof, self.candidate, self.binding, self.supported))
        self.assertEqual(self.proof["schema_version"], 3)
        self.assertEqual(self.proof["verifier_sha256"], self.supported["runner_sha256"])

    def test_only_authenticated_hosted_origin_is_accepted(self):
        for origin in (None, "authenticated_ci", "hosted", "owner", True, {}):
            with self.subTest(origin=origin): self.reject(origin=origin)
        reflected = {**self.proof, "attestation_origin": "authenticated_hosted", "allowed": True}
        self.reject(self.resign(reflected))

    def test_every_proof_field_is_required_and_unknown_fields_are_refused(self):
        for field in self.proof:
            proof = deepcopy(self.proof); proof.pop(field)
            with self.subTest(missing=field): self.reject(proof)
        for field in ("extra", "publishing_inputs_version", "source_run_id", "failure", "configuration"):
            proof = {**self.proof, field: True}
            with self.subTest(extra=field): self.reject(self.resign(proof))

    def test_strict_scalar_bounds_reject_coercion_and_nonfinite_values(self):
        for field in ("schema_version", "repository_id", "connection_generation", "operation_attempt", "deletion_epoch", "adapter_version"):
            for value in (str(self.proof[field]), float(self.proof[field]), True, -1, 2**63):
                proof = {**self.proof, field: value}
                with self.subTest(field=field, value=value): self.reject(self.resign(proof))
        for field in sealed.BOOLEANS:
            for value in (False, 1, "true", None):
                with self.subTest(field=field, value=value): self.reject(self.resign({**self.proof, field: value}))
        for value in (float("nan"), float("inf"), {"nested": []}, "x" * 65537, "bad\ud800"):
            with self.subTest(value=type(value).__name__): self.reject({**self.proof, "source_tree_sha": value})

    def test_identifiers_sources_and_digests_are_closed_bounded_and_exact(self):
        for field in ("source_sha", "source_tree_sha", "baseline_source_sha"):
            for value in ("short", "A" * 40, "g" * 40, "a" * 41, None):
                with self.subTest(field=field, value=value): self.reject(self.resign({**self.proof, field: value}))
        for field in ("website_connection_id", "operation_id"):
            for value in ("bad", self.proof[field].replace("-", ""), None, {}):
                with self.subTest(field=field, value=value): self.reject(self.resign({**self.proof, field: value}))
        for field in ("environment_fingerprint", "artifact_digest", "evidence_digest", "verifier_sha256", "publishing_inputs_digest", "contract_digest"):
            proof = {**self.proof, field: "x" * 64}
            if field != "evidence_digest": self.resign(proof)
            with self.subTest(field=field): self.reject(proof)
        for field, value in (("github_repo", "https://host/repo"), ("target_id", "x" * 257), ("adapter_id", "react_component\n")):
            with self.subTest(field=field): self.reject(self.resign({**self.proof, field: value}))

    def test_every_original_scope_field_and_input_identity_must_match(self):
        for field in sealed.SCOPE_FIELDS | sealed.SEALED_FIELDS:
            binding = deepcopy(self.binding); binding.pop(field)
            with self.subTest(missing=field): self.reject(binding=binding)
            binding[field] = str(self.proof[field]) if type(self.proof[field]) is int else "different"
            with self.subTest(changed=field): self.reject(binding=binding)
        self.reject(binding={**self.binding, "expected_source_sha": "f" * 40})
        self.reject(binding={**self.binding, "source_sha": "f" * 40})
        self.assertEqual(self.check(binding={**self.binding, "source_sha": self.proof["source_sha"]}), self.proof)
        for field, value in self.candidate["payload"]["publishing"]["source_selection"].items():
            with self.subTest(selection=field): self.reject(binding={**self.binding, field: "different"})

    def test_candidate_requires_complete_valid_frozen_inputs(self):
        for candidate in (None, {}, [], True):
            with self.subTest(candidate=candidate): self.reject(candidate=candidate)
        candidate = deepcopy(self.candidate)
        candidate["payload"]["verification"].pop("baseline_policy")
        checksum(candidate)
        self.reject(candidate=candidate)

    def test_unicode_candidate_identity_roundtrips_with_v3_utf8_digest(self):
        # Adversarial unit mutation of a real producer fixture, not execution proof.
        candidate = deepcopy(self.candidate)
        candidate["payload"]["publishing"]["target"]["target_id"] = "articles_café"
        checksum(candidate)
        proof = self.resign({**self.proof, "target_id": "articles_café", "publishing_inputs_digest": candidate["digest"]})
        binding = {**self.binding, "target_id": "articles_café", "publishing_inputs_digest": candidate["digest"]}
        self.assertEqual(self.check(proof, candidate=candidate, binding=binding), proof)
        legacy = legacy_digest({key: value for key, value in proof.items() if key != "evidence_digest"})
        self.assertNotEqual(proof["evidence_digest"], legacy)
        self.reject({**proof, "evidence_digest": legacy}, candidate=candidate, binding=binding)

    def test_candidate_checksum_and_frozen_baseline_are_not_receipt_selected(self):
        changed = deepcopy(self.candidate)
        changed["payload"]["verification"]["baseline_policy"]["source_sha"] = "f" * 40
        self.reject(candidate=changed)
        checksum(changed)
        proof = self.resign({**self.proof, "publishing_inputs_digest": changed["digest"]})
        binding = {**self.binding, "publishing_inputs_digest": changed["digest"]}
        self.reject(proof, candidate=changed, binding=binding)
        self.reject(self.resign({**self.proof, "baseline_source_sha": "f" * 40}),
            binding={**self.binding, "baseline_source_sha": "f" * 40})

    def test_candidate_hash_alone_cannot_choose_an_unsupported_verifier(self):
        candidate = deepcopy(self.candidate)
        candidate["payload"]["verification"]["verifier"]["runner_sha256"] = "f" * 64
        checksum(candidate)
        proof = self.resign({**self.proof, "publishing_inputs_digest": candidate["digest"], "verifier_sha256": "f" * 64})
        binding = {**self.binding, "publishing_inputs_digest": candidate["digest"], "verifier_sha256": "f" * 64}
        self.reject(proof, candidate=candidate, binding=binding)
        for change in ({"profile": "custom"}, {"runner_sha256": "f" * 64}, {"execution_schema_version": True},
                       {"python_version": "0.0"}, {"playwright_version": "0.0"}, {"extra": "ignored"}):
            with self.subTest(change=change): self.reject(supported_verifier={**self.supported, **change})
        self.reject(supported_verifier=None)

    def test_actual_adapter_marker_builds_and_routes_must_match_candidate(self):
        for field, value in (("adapter_id", "custom_contract_v1"), ("adapter_version", 2),
                             ("artifact_digest", "f" * 64), ("baseline_build", "skipped"), ("patched_build", "failed")):
            with self.subTest(field=field): self.reject(self.resign({**self.proof, field: value}))
        for field in ("listing", "detail", "unknown_slug"):
            for value in ("/wrong", "https://outside.test/articles", "//outside.test/path", "/articles/../other"):
                proof = deepcopy(self.proof); proof["verified_routes"][field] = value
                with self.subTest(field=field, value=value): self.reject(self.resign(proof))
        proof = deepcopy(self.proof); proof["verified_routes"]["extra"] = "/other"
        self.reject(self.resign(proof))
        proof = deepcopy(self.proof); proof["verified_routes"]["unknown_slug"] = proof["verified_routes"]["detail"]
        self.reject(self.resign(proof))

    def test_lockfile_witnesses_exactly_cover_frozen_inputs(self):
        for locks in ({"extra.lock": "f" * 64}, {"extra.lock": True}, {"extra.lock": {"nested": "f" * 64}},
                      {"x" * 4097: "f" * 64}, {str(i): "f" * 64 for i in range(21)}):
            with self.subTest(locks=len(locks)): self.reject(self.resign({**self.proof, "lockfile_digests": locks}))
        oversized = {f"{index}-" + "x" * 4000: "f" * 64 for index in range(20)}
        proof = self.resign({**self.proof, "lockfile_digests": oversized})
        self.assertTrue(sealed._proof_shape(proof))
        self.assertGreater(len(canonical_bytes(proof)), sealed.MAX_PROOF_BYTES)
        self.reject(proof)
        if self.proof["lockfile_digests"]:
            self.reject(self.resign({**self.proof, "lockfile_digests": {}}))

    def test_schema3_unicode_digest_uses_finite_utf8_and_legacy_digest_stays_unchanged(self):
        sample = {"schema_version": 3, "route": "/histoires/café", "evidence_digest": "excluded"}
        expected = hashlib.sha256(b'{"route":"/histoires/caf\xc3\xa9","schema_version":3}').hexdigest()
        self.assertEqual(sealed.sealed_evidence_digest(sample), expected)
        legacy = {"schema_version": 2, "route": "/histoires/café"}
        expected_legacy = hashlib.sha256(json.dumps(legacy, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        self.assertEqual(legacy_digest(legacy), expected_legacy)
        self.assertNotEqual(sealed.sealed_evidence_digest(legacy), expected_legacy)
        with self.assertRaises(ValueError): sealed.sealed_evidence_digest({"value": float("nan")})
        self.reject({**self.proof, "evidence_digest": "f" * 64})
