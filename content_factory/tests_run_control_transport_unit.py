"""Real requests byte iteration stops slow trickles before buffering a full body."""
from copy import deepcopy
import json
from unittest.mock import patch

import requests
from django.test import SimpleTestCase, override_settings

from . import run_dispatch_settlement as withdrawal, run_execution_control_views as control
from .run_execution_control import ExecutionControlDenied
from .tests_run_dispatch_settlement_unit import FIXTURE, run


class TrickleResponse:
    """Simulate a server staying inside its socket inactivity limit per byte."""

    def __init__(self, body, *, step=9):
        self.body, self.step, self.now = body, step, 0
        self.read = 0
        self.closed = False
        self.released = 0
        self.requested_chunk_sizes = []

    def stream(self, amount, decode_content=False):
        self.requested_chunk_sizes.append(amount)
        buffer = bytearray()
        for byte in self.body:
            self.now += self.step
            self.read += 1
            buffer.append(byte)
            if len(buffer) == amount:
                yield bytes(buffer)
                buffer.clear()
        if buffer:
            yield bytes(buffer)

    def close(self):
        self.closed = True

    def release_conn(self):
        self.released += 1

    def response(self, status=200):
        response = requests.Response()
        response.status_code, response.raw = status, self
        return response


@override_settings(CONTENT_FACTORY_URL="https://cf-control-transport.fixture.invalid",
                   CONTENT_FACTORY_API_KEY="synthetic-transport-key")
class ControlTransportTests(SimpleTestCase):
    def test_budget_slow_trickle_refuses_after_two_bytes_without_an_execution_grant(self):
        stream = TrickleResponse(b'{"schema_version":1}')
        with patch.object(control.requests, "get", return_value=stream.response()) as get, \
                patch.object(control, "monotonic", side_effect=lambda: stream.now):
            with self.assertRaises(ExecutionControlDenied) as error:
                control._budget("original-worker")
        self.assertTrue(error.exception.pending)
        self.assertEqual(stream.read, 2)
        self.assertEqual(stream.requested_chunk_sizes, [1])
        self.assertTrue(stream.closed)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertEqual(get.call_args.kwargs["timeout"], (3, 10))

    def test_withdrawal_slow_trickle_holds_before_reading_or_accepting_the_valid_receipt(self):
        stream = TrickleResponse(json.dumps(FIXTURE["response"]).encode())
        with patch.object(withdrawal.requests, "post", return_value=stream.response()) as post, \
                patch.object(withdrawal, "monotonic", side_effect=lambda: stream.now):
            with self.assertRaises(withdrawal.DispatchSettlementPending):
                withdrawal._withdraw(run())
        self.assertEqual(stream.read, 2)
        self.assertEqual(stream.requested_chunk_sizes, [1])
        self.assertTrue(stream.closed)
        self.assertFalse(post.call_args.kwargs["allow_redirects"])

    def test_fast_budget_and_actual_cf_receipt_still_parse_through_real_byte_iterator(self):
        for module, method, value, invoke in (
            (control, "get", {"schema_version": 1}, lambda: control._budget("original-worker")),
            (withdrawal, "post", deepcopy(FIXTURE["response"]), lambda: withdrawal._withdraw(run())),
        ):
            stream = TrickleResponse(json.dumps(value).encode(), step=0)
            with self.subTest(module=module.__name__), patch.object(module.requests, method, return_value=stream.response()), \
                    patch.object(module, "monotonic", side_effect=lambda: stream.now):
                self.assertEqual(invoke(), value)
                self.assertEqual(stream.read, len(stream.body))
                # Fully consumed requests responses release their connection;
                # close need not call raw.close after consuming the whole body.
                self.assertEqual(stream.released, 1)

    def test_invalid_budget_status_or_oversized_body_closes_without_parsing(self):
        for status, body in ((503, b"unknown"), (200, b"x" * 131_073)):
            stream = TrickleResponse(body, step=0)
            with self.subTest(status=status), patch.object(control.requests, "get", return_value=stream.response(status)), \
                    patch.object(control, "monotonic", side_effect=lambda: stream.now), \
                    self.assertRaises(ExecutionControlDenied):
                control._budget("original-worker")
            self.assertTrue(stream.closed)
