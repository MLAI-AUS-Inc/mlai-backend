"""Pure closed-metadata contracts; no Django startup or database is necessary."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import importlib.util
from pathlib import Path
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

_spec = importlib.util.spec_from_file_location(
    "community_source_codec_pure", Path(__file__).parent / "services/community_bridge/community_source_codec.py")
codec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(codec)


class PublicBridgeObservabilityUnitTests(unittest.TestCase):
    def setUp(self):
        from integrations.services.community_bridge import observability
        self.logs = observability
        self.callbacks = []
        self.connection = SimpleNamespace(connection=object(), in_atomic_block=True, autocommit=False,
            on_commit=lambda callback, robust: self.callbacks.append(callback))
        self.row = SimpleNamespace(pk=1, audience="members", destination_platform="buzz",
            slack_workspace_id="TTEST", slack_channel_id="CROOM", destination_channel_id=str(uuid.UUID(int=1)))
        self.connections = patch.object(observability, "connections", SimpleNamespace(
            _connections=SimpleNamespace(default=self.connection)))
        self.connections.start()
        self.addCleanup(self.connections.stop)

    def commit(self):
        for callback in self.callbacks:
            callback()
        self.callbacks.clear()

    def test_logs_capture_primitives_until_commit_and_discard_rollback(self):
        with patch.object(self.logs.logger, "info") as logger:
            self.logs.room(self.row, "room_created")
            logger.assert_not_called()
            self.row.slack_channel_id = "COTHER"
            self.commit()
            self.assertIn("channel_id=CROOM", logger.call_args.args[2])
            self.assertNotIn("COTHER", str(logger.call_args))
            logger.reset_mock()
            self.logs.room(self.row, "room_updated")
            self.callbacks.clear()  # Model transaction rollback drops callbacks.
            self.commit()
            logger.assert_not_called()

    def test_closed_fields_no_payload_text_recipient_or_exception_exports(self):
        with patch.object(self.logs.logger, "info") as logger:
            for fields in ({"outcome": "held", "payload": {"text": "secret"}},
                           {"outcome": "secret\nheld"}, {"mapping_id": True},
                           {"mapping_id": 0}, {"channel_id": "DPRIVATE"}):
                self.logs.emit("slack_member_channel", **fields)
            self.logs.send(self.row, {"kind": "member", "access_token": "secret", "recipients": ["secret"]}, 2, "settled")
            self.commit()
            self.assertEqual(logger.call_count, 1)
            self.assertNotIn("secret", str(logger.call_args))
            self.assertIn("poster=member", logger.call_args.args[2])

    def test_private_rows_never_enter_route_or_room_analytics(self):
        with patch.object(self.logs.logger, "info") as logger, self.logs.capture_route() as route:
            self.row.audience = "private"
            self.logs.public_route(self.row)
            self.logs.room(self.row, "held")
            self.logs.compare(self.row, kind="report", outcome="pass", N=10, M=0)
            self.logs.event(route, {"status": "enqueued"})
            self.commit()
            logger.assert_not_called()
            self.assertEqual(route, {})

    def test_former_public_converted_room_retains_only_lifecycle_ids(self):
        self.row.slack_visibility = "private"
        with patch.object(self.logs.logger, "info") as logger:
            self.logs.room(self.row, "held")
            self.commit()
            self.assertEqual(logger.call_count, 1)
            self.assertIn("outcome=held", logger.call_args.args[2])
            self.assertNotIn("slack_visibility", str(logger.call_args))

    def test_logging_registration_and_format_failures_do_not_escape(self):
        with patch.object(self.logs.logger, "info", side_effect=RuntimeError("secret")):
            self.logs.room(self.row, "room_created")
            self.commit()
        self.connection.on_commit = Mock(side_effect=RuntimeError("secret"))
        self.logs.room(self.row, "held")

    def test_closed_or_manual_connections_are_not_opened_to_log(self):
        with patch.object(self.logs.logger, "info") as logger:
            self.connection.in_atomic_block = False
            self.logs.room(self.row, "held")  # Manual autocommit=False transaction.
            self.connection.connection = None
            self.connection.autocommit = True
            self.logs.room(self.row, "held")
            self.assertEqual(self.callbacks, [])
            logger.assert_not_called()
            self.connection.connection = object()
            self.logs.room(self.row, "held")
            self.assertEqual(logger.call_count, 1)

    def test_authenticated_route_uses_owned_row_snapshot_not_result_metadata(self):
        with patch.object(self.logs.logger, "info") as logger, self.logs.capture_route() as route:
            self.logs.public_route(self.row)
            self.logs.event(route, {"status": "ignored", "receipt_id": 7,
                                    "text": "secret", "route": "private", "token": "secret"})
            self.commit()
            self.assertIn("route=members", logger.call_args.args[2])
            self.assertIn("receipt_id=7", logger.call_args.args[2])
            self.assertNotIn("secret", str(logger.call_args))

    def test_compare_counts_are_closed_existing_report_values(self):
        self.row.audience = "community"
        with patch.object(self.logs.logger, "info") as logger:
            self.logs.compare(self.row, kind="report", outcome="inconclusive",
                              only_bot=1, only_members=2, both=3, N=4, M=1)
            self.logs.compare(self.row, kind="report", outcome="pass", N=None, M=0)
            self.commit()
            self.assertEqual(logger.call_count, 1)
            self.assertIn("N=4", logger.call_args.args[2])


class CommunitySourceCodecTests(unittest.TestCase):
    def setUp(self):
        self.start = 1_800_000_000 * 10**9
        self.epoch = str(uuid.uuid4())
        self.rooms = [{"row_id": n, "workspace_id": "TTEST", "channel_id": f"CROOM{n}",
                       "destination_id": str(uuid.UUID(int=n))} for n in range(1, 16)]
        self.cohort = codec.digest(b"mlai:community-source-cohort:v1\0", self.rooms)
        self.readers = {n: 2 for n in range(1, 16)}

    def sighting(self, n=1, *, role="bot", room=1, at=None, **changes):
        return dict(version=1, epoch=self.epoch, cohort=self.cohort, room=self.rooms[room-1],
                    comparison_id=codec.digest(b"test", n), operation="message", kind="message",
                    message_ns=self.start+n*1000, own_send_ns=0, sources={role: at or self.start+n*1000+1000}, **changes)

    def report(self, values, **kwargs):
        return codec.report(values, **(dict(epoch_id=self.epoch, cohort=self.rooms,
            start_ns=self.start, cutoff_ns=self.start+codec.WEEK_NS,
            complete=True, reader_counts=self.readers) | kwargs))

    def test_exact_utc_and_slack_decimal_normalization(self):
        self.assertEqual(codec.timestamp("1800000000.010000"), ("1800000000.01", self.start+10_000_000))
        self.assertEqual(codec.stamp(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                         codec.stamp(datetime(2026, 1, 1, 11, tzinfo=timezone(timedelta(hours=11)))))
        for raw in (True, 1.2, "NaN", "1.0000000000", "-1.2", "0.0"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                codec.timestamp(raw)
        with self.assertRaises(ValueError):
            codec.stamp(datetime(2026, 1, 1))

    def test_marker_closed_types_no_raw_fields_or_boolean_times(self):
        original = self.sighting()
        self.assertEqual(codec.marker(original), original)
        for bad in (dict(original, text="private"), dict(original, sources={"bot": True}),
                    dict(original, sources={"other": 3}), dict(original, operation=[]),
                    dict(original, kind={}), dict(original, version=True),
                    dict(original, room=dict(self.rooms[0], row_id=True))):
            self.assertIsNone(codec.marker(bad))

    def test_merge_minimum_ingress_for_each_app_is_order_independent(self):
        a, b = self.sighting(at=self.start+4000), self.sighting(role="members", at=self.start+5000)
        merged = codec.merge(a, b)
        self.assertEqual(merged, codec.merge(b, a))
        self.assertEqual(codec.merge(merged, self.sighting(at=self.start+3000))["sources"]["bot"], self.start+3000)
        self.assertEqual(codec.merge(merged, dict(b, kind="own_send", own_send_ns=self.start+5000))["kind"], "own_send")
        with self.assertRaises(ValueError):
            codec.merge(merged, self.sighting(n=2))

    def test_message_and_broadcast_duplicate_grouping_has_one_denominator(self):
        a, b = self.sighting(), self.sighting(role="members")
        result = self.report([a, b, deepcopy(a), deepcopy(b)])
        self.assertEqual((result["N"], result["M"], result["rooms"][0]["both"]), (1, 0, 1))
        self.assertEqual(result["status"], "pass")
        self.assertFalse(result["provider_delivery_completeness_proved"])

    def test_provisional_counts_never_pass_before_seven_days(self):
        result = self.report([self.sighting(), self.sighting(role="members")],
                             cutoff_ns=self.start+1_000_000, partial=True)
        self.assertEqual((result["N"], result["M"], result["status"]), (1, 0, "inconclusive"))
        self.assertFalse(result["window_complete"])

    def test_late_own_send_proof_cannot_rewrite_an_earlier_cutoff_population(self):
        end = self.start+codec.WEEK_NS
        earlier = self.sighting()
        later = dict(self.sighting(role="members", at=end+1), kind="own_send", own_send_ns=end+1)
        merged = codec.merge(earlier, later)
        before = self.report([merged])
        after = self.report([merged], cutoff_ns=end+2)
        self.assertEqual((before["N"], before["rooms"][0]["excluded_own_send"]), (1,0))
        self.assertEqual((after["N"], after["rooms"][0]["excluded_own_send"]), (0,1))
        future_only = self.report([later])
        self.assertEqual(future_only["rooms"][0]["excluded_own_send"], 0)

    def test_zero_reference_is_inconclusive_and_never_ratio_zero(self):
        result = self.report([self.sighting(role="members")])
        self.assertIsNone(result["miss_ratio"])
        self.assertEqual(result["status"], "inconclusive")
        self.assertFalse(result["strict_under_half_percent"])
        self.assertIsNone(result["rooms"][1]["miss_ratio"])

    def test_strict_half_percent_uses_integer_inequality(self):
        for total, missing, status in ((200, 1, "fail"), (201, 1, "pass"), (40000, 200, "fail"), (40000, 199, "pass")):
            with self.subTest(total=total, missing=missing):
                values = [self.sighting(n=n) for n in range(1, total+1)]
                values += [self.sighting(n=n, role="members") for n in range(missing+1, total+1)]
                result = self.report(values)
                self.assertEqual((result["N"], result["M"], result["status"]), (total, missing, status))

    def test_every_room_needs_two_current_readers_without_per_room_sample_rule(self):
        self.assertEqual(self.report([self.sighting(), self.sighting(role="members")])["status"], "pass")
        for count in (None, True, 0, 1):
            self.readers[15] = count
            self.assertEqual(self.report([self.sighting(), self.sighting(role="members")])["status"], "inconclusive")

    def test_own_sends_and_mutations_are_separate_from_message_denominator(self):
        values = [dict(self.sighting(), kind="own_send", own_send_ns=self.start+2000), self.sighting(role="members"),
                  dict(self.sighting(n=2), kind="diagnostic", operation="edit")]
        result = self.report(values)
        self.assertEqual((result["N"], result["rooms"][0]["excluded_own_send"],
                          result["rooms"][0]["mutation_diagnostics"]), (0, 1, 1))

    def test_exact_window_and_cutoff_no_invented_grace(self):
        end = self.start+codec.WEEK_NS
        values = [dict(self.sighting(), message_ns=end), dict(self.sighting(n=2), message_ns=self.start-1),
                  self.sighting(n=3, at=end), self.sighting(n=3, role="members", at=end-1)]
        result = self.report(values)
        self.assertEqual((result["N"], result["rooms"][0]["only_members"]), (0, 1))
        with self.assertRaises(ValueError):
            self.report([], cutoff_ns=end-1)

    def test_mutation_diagnostics_respect_the_same_ingress_cutoff(self):
        end = self.start+codec.WEEK_NS
        value = dict(self.sighting(at=end), kind="diagnostic", operation="reaction_add")
        self.assertEqual(self.report([value])["rooms"][0]["mutation_diagnostics"], 0)
        value["sources"]["bot"] = end-1
        self.assertEqual(self.report([value])["rooms"][0]["mutation_diagnostics"], 1)

    def test_censoring_invalid_marker_clock_or_cohort_never_pass(self):
        good = [self.sighting(), self.sighting(role="members")]
        self.assertEqual(self.report(good, complete=False)["status"], "inconclusive")
        for bad in (dict(self.sighting(n=2), sources={"bot": self.start-1}),
                    dict(self.sighting(n=2), cohort="a"*64), {"unexpected": "private"}):
            self.assertEqual(self.report(good+[bad])["status"], "inconclusive")
        with self.assertRaises(ValueError):
            self.report(good, cohort=self.rooms[:14])


if __name__ == "__main__":
    unittest.main()
