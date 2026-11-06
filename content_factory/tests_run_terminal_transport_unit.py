"""Real Requests streaming enforces the terminal proof byte/time boundary."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase, override_settings

from . import run_terminal_settlement as terminal
from .run_ownership import OWNER_EXECUTION_KEY, OWNER_RESULT_KEY, WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY
from .tests_run_control_transport_unit import TrickleResponse

FIXTURE = json.loads((Path(__file__).parent / 'testdata/r3/terminal_actual_free.json').read_text())


def run():
    request = deepcopy(FIXTURE['original_backend_fixture']['run_request'])
    wire, ticket = request[WIRE_REQUEST_KEY], request[WIRE_EXECUTION_KEY]
    identity = {key: item for key, item in wire.items() if key != 'event_protocol'}
    return SimpleNamespace(run_id=FIXTURE['original_backend_fixture']['run_id'], workflow=wire['workflow'],
        organization_id=int(wire['organization_id']), domain=wire['domain'], github_repo=wire['github_repo'], run_request=request,
        result={OWNER_RESULT_KEY: {'schema_version': 1, 'identity': identity,
                    'authority': {'backend_facts': True, 'event_protocol': 1}},
                OWNER_EXECUTION_KEY: {'current_execution_id': ticket['execution_id'], 'tickets': {ticket['execution_id']: ticket}}})


@override_settings(CONTENT_FACTORY_URL='https://terminal.fixture.invalid', CONTENT_FACTORY_API_KEY='synthetic-terminal-key')
class TerminalTransportTests(SimpleTestCase):
    def test_slow_trickle_cannot_buffer_whole_close_body_before_deadline(self):
        stream = TrickleResponse(json.dumps(FIXTURE['cancel_response']).encode(), step=9)
        with patch.object(terminal.requests, 'post', return_value=stream.response()) as post, \
                patch.object(terminal, 'monotonic', side_effect=lambda: stream.now):
            with self.assertRaisesRegex(terminal.TerminalSettlementPending, 'terminal_closure_read_unbounded'):
                terminal._close_remote(run())
        self.assertEqual((stream.read, stream.requested_chunk_sizes), (2, [1]))
        self.assertTrue(stream.closed)
        self.assertFalse(post.call_args.kwargs['allow_redirects'])
        self.assertEqual(post.call_args.kwargs['json'], FIXTURE['cancel_request'])

    def test_actual_fast_closed_receipt_still_parses_and_releases_connection(self):
        stream = TrickleResponse(json.dumps(FIXTURE['cancel_response']).encode(), step=0)
        with patch.object(terminal.requests, 'post', return_value=stream.response()), \
                patch.object(terminal, 'monotonic', side_effect=lambda: stream.now):
            self.assertEqual(terminal._close_remote(run()), FIXTURE['cancel_response']['terminal_settlement_v1'])
        self.assertEqual(stream.released, 1)

    def test_redirect_error_bad_utf8_and_oversized_response_never_supply_a_close_receipt(self):
        for status, body in [(302, b'{}'), (503, b'{}'), (200, b'\xff'), (200, b'x' * 131_073)]:
            stream = TrickleResponse(body, step=0)
            with self.subTest(status=status, length=len(body)), patch.object(terminal.requests, 'post', return_value=stream.response(status)), \
                    patch.object(terminal, 'monotonic', side_effect=lambda: stream.now), self.assertRaises(ValueError):
                terminal._close_remote(run())
            self.assertTrue(stream.closed or stream.released)
