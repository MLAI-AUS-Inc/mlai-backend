"""Actual paired CF withdrawal bytes and closed transport refusal tests."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from . import run_dispatch_settlement as settlement
from .run_ownership import OWNER_EXECUTION_KEY, OWNER_RESULT_KEY, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY

FIXTURE = json.loads((Path(__file__).parent / "tests/fixtures/r3/cf-dispatch-withdrawal-v1.json").read_text())


def run():
    admission = deepcopy(FIXTURE["request"][WIRE_REQUEST_KEY])
    ticket = deepcopy(FIXTURE["request"][WIRE_EXECUTION_KEY])
    identity = {key: value for key, value in admission.items() if key != "event_protocol"}
    return SimpleNamespace(run_id="synthetic-withdrawal-worker", organization_id=int(identity["organization_id"]),
        workflow=identity["workflow"], domain=identity["domain"], github_repo=identity["github_repo"], result={
            OWNER_RESULT_KEY: {"schema_version": 1, "identity": identity,
                               "authority": {"backend_facts": True, "event_protocol": 1}},
            OWNER_EXECUTION_KEY: {"current_execution_id": ticket["execution_id"], "tickets": {ticket["execution_id"]: ticket}}})


@override_settings(CONTENT_FACTORY_URL="https://cf-withdraw.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-withdraw-key")
class WithdrawalReceiptTests(SimpleTestCase):
    def test_actual_cf_computed_fixture_matches_exact_receipt_bytes_and_echoes(self):
        raw = deepcopy(FIXTURE["response"])
        self.assertEqual(settlement.withdrawal_digest(raw), "2ae65d468a8887b9aed13649cfa08d3380b5191d862cb2a43a44c598e0c7e4a0")
        self.assertEqual(settlement.validate_closed_receipt(run(), raw), raw)

    def test_hash_uses_ascii_canonical_json_and_never_secret_field_sanitization(self):
        self.assertEqual(settlement.withdrawal_digest({"value": "树"}), hashlib.sha256(b'{"value":"\\u6811"}').hexdigest())
        self.assertNotEqual(settlement.withdrawal_digest({"authorization": "one"}),
                            settlement.withdrawal_digest({"authorization": "two"}))

    def test_exact_tuple_and_schema_cannot_be_forged_even_with_a_new_valid_hash(self):
        for mutate in (lambda value: value.update(client_request_id="foreign-key"),
                       lambda value: value.update(schema_version=True),
                       lambda value: value[WIRE_REQUEST_KEY].update(organization_id="999"),
                       lambda value: value[WIRE_EXECUTION_KEY].update(generation=1),
                       lambda value: value[WIRE_EXECUTION_KEY]["scope"].update(source_run_id="foreign-source")):
            raw = deepcopy(FIXTURE["response"])
            mutate(raw)
            raw["receipt_sha256"] = settlement.withdrawal_digest(raw)
            with self.assertRaises(settlement.DispatchSettlementPending):
                settlement.validate_closed_receipt(run(), raw)

    def test_old_ticket_closure_does_not_settle_a_different_current_execution(self):
        owner = run()
        old = FIXTURE["request"][WIRE_EXECUTION_KEY]
        newer = {**deepcopy(old), "execution_id": "control:" + "b" * 64}
        owner.result[OWNER_EXECUTION_KEY]["tickets"][newer["execution_id"]] = newer
        owner.result[OWNER_EXECUTION_KEY]["current_execution_id"] = newer["execution_id"]
        with self.assertRaises(settlement.DispatchSettlementPending):
            settlement.validate_closed_receipt(owner, FIXTURE["response"])

    def test_absence_claimed_failed_unknown_or_malformed_receipt_never_proves_closure(self):
        for fields in ({"status": "not_received"}, {"status": "claimed"}, {"status": "failed"},
                       {"status": "unknown"}, {"closed_at": "2026-11-03"}, {"receipt_sha256": "b" * 64},
                       {"result": {"no_effects": True}}):
            with self.subTest(fields=fields), self.assertRaises(settlement.DispatchSettlementPending):
                settlement.validate_closed_receipt(run(), {**deepcopy(FIXTURE["response"]), **fields})

    def test_transport_requires_200_bounded_json_and_never_uses_error_body_as_proof(self):
        for status, data in ((409, json.dumps(FIXTURE["response"]).encode()), (404, b"not received"),
                             (503, b"unknown"), (200, b"not-json")):
            remote = Mock(status_code=status, iter_content=Mock(return_value=[data]))
            with self.subTest(status=status), patch.object(settlement.requests, "post", return_value=remote):
                with self.assertRaises(settlement.DispatchSettlementPending):
                    settlement._withdraw(run())
            remote.close.assert_called_once()
            if status != 200:
                remote.iter_content.assert_not_called()

    def test_authenticated_post_sends_only_original_key_and_exact_two_echoes(self):
        raw = deepcopy(FIXTURE["response"])
        remote = Mock(status_code=200, iter_content=Mock(return_value=[json.dumps(raw).encode()]))
        with patch.object(settlement.requests, "post", return_value=remote) as post:
            self.assertEqual(settlement._withdraw(run()), raw)
        self.assertEqual(post.call_args.args[0], "https://cf-withdraw.fixture.invalid/api/runs/withdraw-dispatch")
        self.assertEqual(post.call_args.kwargs["json"], FIXTURE["request"])
        self.assertEqual(post.call_args.kwargs["headers"]["X-API-KEY"], "synthetic-withdraw-key")
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        remote.close.assert_called_once()

    def test_large_or_slow_streams_close_and_leave_the_charge_unresolved(self):
        for maximum, times in ((1, [0, 1]), (100, [0, 16])):
            remote = Mock(status_code=200, iter_content=Mock(return_value=[b"{}x"]))
            with patch.object(settlement.requests, "post", return_value=remote), \
                    patch.object(settlement, "MAX_WITHDRAWAL_BYTES", maximum), \
                    patch.object(settlement, "monotonic", side_effect=times):
                with self.assertRaises(settlement.DispatchSettlementPending):
                    settlement._withdraw(run())
            remote.close.assert_called_once()
