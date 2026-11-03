"""Bounded canonical research reads and closed positive payload data."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from . import run_topic_delivery as topic
from .run_ownership import OWNER_RESULT_KEY, OWNER_EXECUTION_KEY, WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY
from .tests_run_control_transport_unit import TrickleResponse

FIXTURE = json.loads((Path(__file__).parent / "testdata/r3/topic_actual_paid_delivery.json").read_text())


def run():
    request = deepcopy(FIXTURE["original_backend_admission"]["run_request"])
    wire, ticket = request[WIRE_REQUEST_KEY], request[WIRE_EXECUTION_KEY]
    return SimpleNamespace(run_id=FIXTURE["run_id"], workflow=wire["workflow"], organization_id=int(wire["organization_id"]),
        domain=wire["domain"], github_repo=wire["github_repo"], run_request=request,
        result={OWNER_RESULT_KEY: {"schema_version": 1, "identity": {key: value for key, value in wire.items() if key != "event_protocol"},
            "authority": {"backend_facts": True, "event_protocol": 1}}, OWNER_EXECUTION_KEY: {
            "current_execution_id": ticket["execution_id"], "tickets": {ticket["execution_id"]: ticket}}})


@override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
class TopicTransportTests(SimpleTestCase):
    def test_actual_cf_payload_is_valid_without_renaming_or_dropping_existing_choices(self):
        payload = FIXTURE["canonical_topic_response"]["topic_selection_v1"]
        self.assertTrue(topic.valid_topic_payload("topic_selection_v1", payload))
        stream = TrickleResponse(json.dumps(FIXTURE["canonical_topic_response"]).encode(), step=0)
        with patch.object(topic.requests, "get", return_value=stream.response()) as get, \
                patch.object(topic, "monotonic", side_effect=lambda: stream.now):
            self.assertEqual(topic._fetch_topic(run()), FIXTURE["canonical_topic_response"])
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertEqual(stream.released, 1)

    def test_canonical_topic_stream_stops_trickling_before_buffering_whole_body(self):
        stream = TrickleResponse(json.dumps(FIXTURE["canonical_topic_response"]).encode(), step=9)
        with patch.object(topic.requests, "get", return_value=stream.response()), \
                patch.object(topic, "monotonic", side_effect=lambda: stream.now), \
                self.assertRaisesRegex(ValueError, "exceeded its bound"):
            topic._fetch_topic(run())
        self.assertEqual((stream.read, stream.requested_chunk_sizes), (2, [1]))
        self.assertTrue(stream.closed)

    def test_error_redirect_non_utf8_and_oversized_source_close_without_choices(self):
        for status, data in ((302, b"{}"), (503, b"{}"), (200, b"\xff"),
                (200, b"[" * 2000 + b"]" * 2000), (200, b"x" * (topic.MAX_BYTES + 1))):
            stream = TrickleResponse(data, step=0)
            with self.subTest(status=status, length=len(data)), \
                    patch.object(topic.requests, "get", return_value=stream.response(status)), \
                    patch.object(topic, "monotonic", side_effect=lambda: stream.now), self.assertRaises(ValueError):
                topic._fetch_topic(run())
            self.assertTrue(stream.closed or stream.released)

    def test_empty_wrong_hash_private_keys_and_nonfinite_options_are_not_benefits(self):
        original = FIXTURE["canonical_topic_response"]["topic_selection_v1"]
        variants = []
        for change in (lambda p: p.update(schema_version=True), lambda p: p.update(selection_sha256="f" * 64),
                lambda p: p["selection"].update(options=[]), lambda p: p["selection"].update(backend_run_budget_v1={}),
                lambda p: p["selection"].update(impossible_metric=10**1000), lambda p: p["selection"].update(impossible_metric=float("inf"))):
            value = deepcopy(original); change(value); variants.append(value)
        for value in variants:
            self.assertFalse(topic.valid_topic_payload("topic_selection_v1", value))
