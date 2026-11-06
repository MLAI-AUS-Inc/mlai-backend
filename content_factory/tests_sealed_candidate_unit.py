"""Actual private proposal fixture and hostile transport tests; no DB or network."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
import requests

from . import website_sealed_candidate as sealed
from .website_contract import WebsiteAuthorityError
from .website_failure_contract import canonical_bytes

FIXTURES = Path(__file__).parent / "testdata/sealed-verification-v3"


def checksum(candidate):
    candidate["digest"] = hashlib.sha256(canonical_bytes(candidate["payload"])).hexdigest()


class SealedProposalTests(SimpleTestCase):
    def setUp(self):
        execution = json.loads((FIXTURES / "static.json").read_text())
        proposal = json.loads((FIXTURES / "proposal.json").read_text())
        self.request, self.response = proposal["request"], proposal["response"]
        self.supported = execution["supported_verifier"]
        self.baseline = proposal["original_setup_request"]["expected_source_sha"]
        self.target = deepcopy(execution["candidate"]["payload"]["publishing"]["target"])
        legacy = self.request["contract_digest"]
        self.target.update(contract_digest=legacy, contract={"contract_digest": legacy, "live_marker": deepcopy(self.target["live_marker"])})

    def check(self, response=None, **updates):
        arguments = {"request": self.request, "target": self.target,
            "baseline_source_sha": self.baseline, "supported_verifier": self.supported, **updates}
        return sealed.validate_proposal(self.response if response is None else response, **arguments)

    def reject(self, response=None, **updates):
        with self.assertRaises(WebsiteAuthorityError) as error:
            self.check(response, **updates)
        self.assertEqual(error.exception.code, "ci_attestation_contract_mismatch")
        self.assertFalse(error.exception.retryable)
        self.assertEqual(str(error.exception), sealed.INVALID)

    def test_actual_producer_proposal_preserves_original_legacy_identity_and_input(self):
        before = deepcopy((self.request, self.response, self.target, self.supported))
        candidate = self.check()
        self.assertEqual(candidate, self.response["publishing_inputs_v1"])
        self.assertNotEqual(candidate["digest"], self.target["contract_digest"])
        self.assertEqual(candidate["payload"]["verification"]["baseline_policy"]["source_sha"], self.baseline)
        self.assertNotEqual(self.baseline, self.request["expected_source_sha"])
        candidate["payload"]["publishing"]["target"]["route_path"] = "/changed"
        self.assertEqual(before, (self.request, self.response, self.target, self.supported))

    def test_closed_response_scope_requires_every_original_field_and_strict_type(self):
        for field in self.response:
            response = deepcopy(self.response); response.pop(field)
            with self.subTest(missing=field): self.reject(response)
        self.reject({**self.response, "allowed": True})
        self.reject({**self.response, "schema_version": True})
        self.reject({**self.response, "source_run_id": "other-parent"})
        for field, value in self.response["scope"].items():
            for replacement in (None, str(value) if type(value) is int else "other", True):
                response = deepcopy(self.response); response["scope"][field] = replacement
                with self.subTest(field=field, replacement=replacement): self.reject(response)
            response = deepcopy(self.response); response["scope"].pop(field)
            with self.subTest(missing_scope=field): self.reject(response)
        response = deepcopy(self.response); response["scope"]["extra"] = True
        self.reject(response)

    def test_request_is_closed_bounded_and_validated_before_network(self):
        for field in self.request:
            request = deepcopy(self.request); request.pop(field)
            with self.subTest(missing=field): self.reject(request=request)
        self.reject(request={**self.request, "configuration_revision": 1})
        changes = {"website_connection_id": "website", "operation_id": "operation", "connection_generation": True,
            "repository_id": "41", "operation_attempt": 1.0, "deletion_epoch": -1, "expected_source_sha": "A" * 40,
            "contract_digest": "x" * 64, "github_repo": "https://github.test/repo", "branch": "",
            "domain": "x" * 254, "source_run_id": "bad/path", "app_root": "x" * 501, "target_id": "x" * 201}
        with patch.object(sealed, "authenticated_request") as remote:
            for field, value in changes.items():
                request = {**self.request, field: value}
                with self.subTest(field=field):
                    self.reject(request=request)
                    with self.assertRaises(WebsiteAuthorityError): sealed.fetch_candidate(request)
            remote.assert_not_called()
        self.reject(request={**self.request, "connection_target_id": "other-target"})
        self.assertEqual(self.check(request={**self.request, "connection_target_id": self.request["target_id"]}), self.response["publishing_inputs_v1"])

    def test_legacy_digest_is_never_recomputed_or_replaced_with_candidate_checksum(self):
        for target in ({**self.target, "contract_digest": self.response["publishing_inputs_v1"]["digest"]},
                       {**self.target, "contract_digest": None}, {**self.target, "target_id": "another"},
                       {**self.target, "contract": {**self.target["contract"], "contract_digest": "f" * 64}}):
            with self.subTest(target=target.get("contract_digest")): self.reject(target=target)
        target = deepcopy(self.target); target["contract"].pop("contract_digest")
        self.assertEqual(self.check(target=target), self.response["publishing_inputs_v1"])

    def test_actual_frozen_target_all_fields_and_nested_marker_are_checked(self):
        for field in self.response["publishing_inputs_v1"]["payload"]["publishing"]["target"]:
            target = deepcopy(self.target); target[field] = "changed"
            with self.subTest(field=field): self.reject(target=target)
        target = deepcopy(self.target); target["contract"]["live_marker"]["value"] = "f" * 64
        self.reject(target=target)
        self.reject(target={**self.target, "unrepresented": "field"})
        target = deepcopy(self.target); target["contract"]["unrepresented"] = "field"
        self.reject(target=target)

    def test_existing_seal_cannot_be_replaced_even_with_another_valid_proposal(self):
        target = deepcopy(self.target); target["contract"]["publishing_inputs_v1"] = deepcopy(self.response["publishing_inputs_v1"])
        self.assertEqual(self.check(target=target), self.response["publishing_inputs_v1"])
        response = deepcopy(self.response)
        response["publishing_inputs_v1"]["payload"]["verification"]["baseline_policy"]["source_sha"] = "f" * 40
        checksum(response["publishing_inputs_v1"])
        self.reject(response, target=target, baseline_source_sha="f" * 40)

    def test_baseline_and_verifier_are_selected_by_original_setup_and_trusted_manifest(self):
        self.reject(baseline_source_sha=self.request["expected_source_sha"])
        for baseline in (None, True, "short", "A" * 40):
            with self.subTest(baseline=baseline): self.reject(baseline_source_sha=baseline)
        for field, value in (("runner_sha256", "f" * 64), ("profile", "custom"), ("execution_schema_version", True),
                             ("python_version", "9.0"), ("playwright_version", "9.0")):
            with self.subTest(field=field): self.reject(supported_verifier={**self.supported, field: value})
        response = deepcopy(self.response)
        response["publishing_inputs_v1"]["payload"]["verification"]["verifier"]["runner_sha256"] = "f" * 64
        checksum(response["publishing_inputs_v1"])
        self.reject(response)
        response["publishing_inputs_v1"]["digest"] = "f" * 64
        self.reject(response)

    def test_frozen_source_selection_must_match_exact_original_request(self):
        response = deepcopy(self.response)
        response["publishing_inputs_v1"]["payload"]["publishing"]["source_selection"] = {"branch": "main", "app_root": ""}
        checksum(response["publishing_inputs_v1"])
        self.assertEqual(self.check(response), response["publishing_inputs_v1"])
        for field in ("branch", "app_root"):
            request = {**self.request, field: "other"}
            changed = deepcopy(response); changed["scope"][field] = "other"
            with self.subTest(field=field): self.reject(changed, request=request)

    def test_absent_selection_does_not_allow_execution_in_another_app_root(self):
        self.assertEqual(self.response["publishing_inputs_v1"]["payload"]["publishing"]["source_selection"], {})
        request = {**self.request, "app_root": "another-app"}
        response = deepcopy(self.response); response["scope"]["app_root"] = "another-app"
        self.reject(response, request=request)

    def test_candidate_with_colliding_probe_routes_is_rejected_before_execution(self):
        response, target = deepcopy(self.response), deepcopy(self.target)
        candidate = response["publishing_inputs_v1"]
        verification = candidate["payload"]["verification"]
        execution, oracle = verification["execution"], verification["oracle"]
        detail = execution["route_template"].replace("{slug}", oracle["seed_slug"])
        old_listing = execution["listing_route"]
        execution["listing_route"] = detail
        candidate["payload"]["publishing"]["target"]["route_path"] = detail
        oracle["artifact_marker_routes"] = [detail if route == old_listing else route for route in oracle["artifact_marker_routes"]]
        checksum(candidate)
        target["route_path"] = detail
        attached = {**target, "contract": {**target["contract"], "publishing_inputs_v1": candidate}}
        self.assertEqual(sealed.candidate_for_target(attached), candidate)
        self.reject(response, target=target)

    def test_fetch_candidate_uses_only_fixed_route_after_request_validation(self):
        with patch.object(sealed, "authenticated_request", return_value=self.response) as remote:
            self.assertEqual(sealed.fetch_candidate(self.request), self.response)
        remote.assert_called_once_with("POST", sealed.CANDIDATE_PATH, data=self.request)


@override_settings(CONTENT_FACTORY_URL="https://cf.example.test", CONTENT_FACTORY_API_KEY="unit-private-service-key")
class SealedTransportTests(SimpleTestCase):
    def setUp(self):
        factory = patch.object(sealed.requests, "Session").start()
        self.addCleanup(patch.stopall)
        self.factory = factory
        self.client = factory.return_value.__enter__.return_value
        self.response = self.client.request.return_value.__enter__.return_value
        self.response.status_code = 200
        self.response.headers = {"Content-Type": "application/json"}
        self.response.iter_content.return_value = [b'{"schema_version":1}']
        patch("content_factory.website_connections.transaction.get_connection", return_value=SimpleNamespace(atomic_blocks=[])).start()

    def call(self, method="post", path=sealed.CANDIDATE_PATH, data=None):
        return sealed.authenticated_request(method, path, data={} if data is None and method.lower() == "post" else data)

    def reject(self, *, retryable=False, **kwargs):
        with self.assertRaises(WebsiteAuthorityError) as error:
            self.call(**kwargs)
        self.assertEqual(error.exception.code, "worker_unavailable" if retryable else "ci_attestation_contract_mismatch")
        self.assertEqual(error.exception.retryable, retryable)
        self.assertNotIn("private", str(error.exception))
        self.assertNotIn("provider-secret", str(error.exception))

    def test_explicit_service_transport_ignores_ambient_credentials_and_disallows_redirects(self):
        self.assertEqual(self.call(data={"request": "fixture"}), {"schema_version": 1})
        self.assertIs(self.client.trust_env, False)
        self.client.request.assert_called_once_with("POST", "https://cf.example.test" + sealed.CANDIDATE_PATH,
            headers={"X-API-Key": "unit-private-service-key", "Accept": "application/json", "Accept-Encoding": "identity", "Content-Type": "application/json"},
            data=b'{"request":"fixture"}', stream=True, allow_redirects=False, timeout=(3, 8))
        self.client.request.return_value.__exit__.assert_called_once()

    def test_exact_dispatch_and_canonical_uuid_status_routes(self):
        self.response.status_code = 202
        self.assertEqual(self.call(path=sealed.EXECUTION_PATH), {"schema_version": 1})
        self.response.status_code = 200
        path = "/api/runs/33333333-3333-4333-8333-333333333333/hosted-native-verification"
        self.assertEqual(self.call("get", path), {"schema_version": 1})
        self.assertNotIn("data", self.client.request.call_args.kwargs)
        for method, path in (("get", sealed.CANDIDATE_PATH), ("post", path), ("delete", path),
                             ("get", path + "?token=provider-secret"), ("get", path.replace("33333333-3333-4333-8333-333333333333", "../other")),
                             ("get", path.replace("33333333-3333-4333-8333-333333333333", "not-a-uuid")),
                             ("post", "https://outside.test" + sealed.CANDIDATE_PATH), ("post", sealed.CANDIDATE_PATH + "/")):
            self.factory.reset_mock()
            with self.subTest(method=method, path=path): self.reject(method=method, path=path)
            self.factory.assert_not_called()

    def test_requires_explicit_https_origin_and_real_service_key_before_network(self):
        for base in ("http://cf.example.test", "https://user:provider-secret@cf.example.test", "https://cf.example.test?x=1",
                     "https://cf.example.test#fragment", "https://cf.example.test/api", "", None, "https://cf.example.test\n"):
            self.factory.reset_mock()
            with self.subTest(base=base), override_settings(CONTENT_FACTORY_URL=base): self.reject(retryable=True)
            self.factory.assert_not_called()
        for key in (None, "", True, "provider-secret\r\nX-Other: value"):
            self.factory.reset_mock()
            with self.subTest(key_type=type(key).__name__), override_settings(CONTENT_FACTORY_API_KEY=key): self.reject(retryable=True)
            self.factory.assert_not_called()

    def test_authority_transactions_are_rejected_before_http(self):
        with patch("content_factory.website_connections.transaction.get_connection", return_value=SimpleNamespace(atomic_blocks=[SimpleNamespace(_from_testcase=False)])):
            with self.assertRaisesRegex(RuntimeError, "outside database transactions"): self.call()
        self.factory.assert_not_called()

    def test_provider_redirects_auth_admission_and_malformed_status_are_not_retried(self):
        for status in (201, 202, 204, 301, 302, 307, 308, 400, 401, 403, 404, 409, 422):
            self.response.status_code = status
            self.response.iter_content.reset_mock()
            with self.subTest(status=status): self.reject()
            self.response.iter_content.assert_not_called()

    def test_transport_and_transient_provider_failures_have_static_retryable_errors(self):
        for status in (408, 429, 500, 502, 503, 599):
            self.response.status_code = status
            with self.subTest(status=status): self.reject(retryable=True)
        for error in (requests.Timeout("provider-secret"), requests.ConnectionError("provider-secret")):
            self.client.request.side_effect = error
            with self.subTest(error=type(error).__name__): self.reject(retryable=True)

    def test_content_type_and_declared_or_actual_size_are_bounded(self):
        for content_type in ("text/html", "", "application/json-seq"):
            self.response.headers = {"Content-Type": content_type}
            with self.subTest(content_type=content_type): self.reject()
        self.response.headers = {"Content-Type": "application/json", "Content-Encoding": "gzip"}
        self.response.iter_content.reset_mock()
        self.reject()
        self.response.iter_content.assert_not_called()
        for length in (str(sealed.MAX_RESPONSE_BYTES + 1), "-1", "9" * 100, "NaN"):
            self.response.headers = {"Content-Type": "application/json", "Content-Length": length}
            self.response.iter_content.reset_mock()
            with self.subTest(length=length): self.reject()
            self.response.iter_content.assert_not_called()
        self.response.headers = {"Content-Type": "application/json"}
        self.response.iter_content.return_value = [b" " * 8192] * (sealed.MAX_RESPONSE_BYTES // 8192 + 1)
        self.reject()

    def test_elapsed_body_budget_is_bounded(self):
        with patch.object(sealed.time, "monotonic", side_effect=[0, sealed.MAX_SECONDS + 1]): self.reject(retryable=True)
        self.response.iter_content.return_value = []
        with patch.object(sealed.time, "monotonic", side_effect=[0, sealed.MAX_SECONDS + 1]): self.reject(retryable=True)

    def test_strict_json_rejects_duplicates_nonfinite_invalid_unicode_and_complexity(self):
        for raw in (b'[]', b'null', b'{"x":1,"x":2}', b'{"scope":{"x":1,"x":2}}', b'{"x":NaN}', b'{"x":Infinity}',
                    b'{"x":1e999}', b'{"x":"\\ud800"}', b'\xff', b'{"x":' + b'[' * 20 + b'0' + b']' * 20 + b'}',
                    b'{"x":' + str(2**63).encode() + b'}', b'{"x":"' + b'x' * 4097 + b'"}'):
            self.response.iter_content.return_value = [raw]
            with self.subTest(raw=raw[:50]): self.reject()

    def test_outgoing_json_is_bounded_before_network(self):
        for data in ({"x": float("nan")}, {"x": 2**63}, {"x": "x" * 4097}, {"x": object()}, {"x": "bad\ud800"},
                     {str(index): "x" * 4000 for index in range(20)}):
            self.factory.reset_mock()
            with self.subTest(value_type=type(next(iter(data.values()))).__name__): self.reject(data=data)
            self.factory.assert_not_called()

    def test_outgoing_unicode_uses_the_exact_bytes_measured_for_request_limit(self):
        data = {"target": "articles_café"}
        self.call(data=data)
        self.assertEqual(self.client.request.call_args.kwargs["data"], canonical_bytes(data))
