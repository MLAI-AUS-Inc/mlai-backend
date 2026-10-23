"""Pure closed-metadata contracts; no Django startup or database is necessary."""
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

    def test_retired_comparison_family_cannot_emit_new_observations(self):
        with patch.object(self.logs.logger, "info") as logger:
            self.logs.emit("slack_community_source compare", kind="report", outcome="pass", N=10, M=0)
            self.commit()
            logger.assert_not_called()


class CommunitySourceCodecTests(unittest.TestCase):
    def test_authenticated_callback_stamp_keeps_exact_utc(self):
        self.assertEqual(codec.stamp(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                         codec.stamp(datetime(2026, 1, 1, 11, tzinfo=timezone(timedelta(hours=11)))))
        with self.assertRaises(ValueError):
            codec.stamp(datetime(2026, 1, 1))

    def test_historical_marker_remains_closed_and_returns_an_independent_copy(self):
        original = dict(version=1, epoch=str(uuid.uuid4()), cohort="a"*64,
            room={"row_id":1,"workspace_id":"TTEST","channel_id":"CROOM","destination_id":str(uuid.uuid4())},
            comparison_id="b"*64,operation="message",kind="message",message_ns=123,own_send_ns=0,sources={"bot":124,"members":125})
        decoded = codec.marker(original)
        self.assertEqual(decoded, original)
        decoded["sources"]["bot"] = 999
        self.assertEqual(original["sources"]["bot"], 124)
        for bad in (dict(original, text="private"),dict(original,sources={"bot":True}),
                    dict(original,sources={"other":3}),dict(original,operation=[]),
                    dict(original,kind={}),dict(original,version=True),
                    dict(original,room=dict(original["room"],row_id=True))):
            with self.subTest(bad=bad):
                self.assertIsNone(codec.marker(bad))


if __name__ == "__main__":
    unittest.main()
