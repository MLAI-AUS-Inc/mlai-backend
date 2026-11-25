"""Exercise the file-share wire contract without a database or Slack token."""

import hashlib
import io
import wave
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from integrations.services.community_bridge import member_attempts, member_delivery, voice
from integrations.services.community_bridge.store import _normalize_slack_event


def sample_audio():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(b"\x00\x01" * 160)
    return buffer.getvalue()


@override_settings(SLACK_VOICE_UPLOAD_CHANNELS="CVOICE", MESSAGE_SYNC_SLACK_USER_APP_ID="")
class SlackVoiceBridgeTests(SimpleTestCase):
    def test_member_worker_uses_read_only_reconciliation_on_the_same_file_after_a_retry(self):
        from integrations.services.community_bridge import observability

        channel = SimpleNamespace(slack_channel_id="CVOICE", slack_workspace_id="TVOICE")
        actor = {"kind": "member", "slack_user_id": "UVOICE"}
        wire = {
            "method": "chat_postMessage", "voice_file_id": "FVOICE", "parent": "",
            "text_hash": member_delivery.text_hash("hello"),
            "kwargs": {"channel": "CVOICE", "client_msg_id": "example", "text": "hello"},
        }
        command = {"version": 1, "actor": actor, "wire": wire}
        row = SimpleNamespace(
            payload={member_delivery.MEMBER_STAMP: command}, source_platform="buzz",
            target_platform="slack", source_parent_message_id="", delivery_type="create", save=Mock(),
        )
        delivery = {"id": 1}
        attempt = {"id": "same-attempt", "started_at": "2023-11-14T22:13:20+00:00"}
        client = Mock()
        client.mutation_started = False
        client.call.side_effect = [
            {"ok": True, "messages": []},
            {"ok": True, "messages": [
                {"ts": "1700000000.000001", "user": "UVOICE", "files": [{"id": "FVOICE"}]},
            ]},
        ]

        def file_write(method, **kwargs):
            self.assertEqual(method, voice.COMPLETE)
            self.assertEqual(kwargs["files"], [{"id": "FVOICE", "title": "Voice message.wav"}])
            client.mutation_started = True

        client.voice_call.side_effect = file_write
        with ExitStack() as stack:
            mocks = {
                "_stamp_identity": Mock(return_value=(channel, {}, None)),
                "_choose": Mock(return_value=(object(), actor)),
                "_prepare": Mock(return_value=command),
                "_fence": Mock(side_effect=lambda *_, **__: nullcontext((row, channel, None, client))),
                "_same_command": Mock(),
                "_check_frozen_broadcast": Mock(),
                "_finish_slack": Mock(),
                "guard_delivery": Mock(),
            }
            for name, mocked in mocks.items():
                stack.enter_context(patch.object(member_delivery, name, mocked))
            stack.enter_context(patch.object(member_delivery.access, "enabled", return_value=True))
            stack.enter_context(patch.object(observability, "send"))
            stack.enter_context(patch.object(observability, "send_failure"))
            stack.enter_context(patch.object(member_attempts, "arm", side_effect=[(attempt, False), (attempt, True)]))
            record = stack.enter_context(patch.object(member_attempts, "record"))
            with self.assertRaises(member_delivery.MemberDeliveryDeferred):
                member_delivery.execute(delivery)
            self.assertEqual(record.call_args.args[3], "uncertain")
            member_delivery.execute(delivery)

        self.assertEqual(client.voice_call.call_count, 1)
        self.assertEqual([call.args[0] for call in client.call.call_args_list],
                         ["conversations_history", "conversations_history"])
        self.assertEqual(row.payload[member_attempts.ATTEMPT_KEY]["receipt"]["voice_file_id"], "FVOICE")
        mocks["_finish_slack"].assert_called_once()

    def test_signed_wav_uses_one_native_slack_file_and_exact_share_receipt(self):
        audio = sample_audio()
        digest = hashlib.sha256(audio).hexdigest()
        url = f"https://chat.example/media/{digest}.wav"
        body = f"Release check\n[Voice message]({url})"
        author, event_id = "a" * 64, "b" * 64
        event = {"delivery_type": "create", "source_message_id": event_id, "source_author_id": author, "text": body}
        raw = {"kind": 9, "event_id": event_id, "pubkey": author, "tags": [
            ["imeta", f"url {url}", f"x {digest}", f"size {len(audio)}", "m audio/wav", "duration 0.01"],
        ]}
        attachment = voice.from_callback(event, raw)
        self.assertEqual(attachment, {"url": url, "sha256": digest, "size": len(audio)})
        row = SimpleNamespace(
            delivery_type="create", source_platform="buzz", source_channel_id="chat-room", source_message_id=event_id,
            payload={**event, "metadata": {voice.META: attachment}},
        )
        channel = SimpleNamespace(slack_channel_id="CVOICE", slack_workspace_id="TVOICE")
        client = Mock()
        client.voice_call.return_value = {"ok": True, "file_id": "FVOICE", "upload_url": "https://files.slack.com/upload/v1/test"}
        upload = Mock()
        upload.__enter__ = Mock(return_value=SimpleNamespace(status_code=200))
        upload.__exit__ = Mock(return_value=False)
        wire = {
            "kwargs": {"channel": "CVOICE", "client_msg_id": "stable", "text": body},
            "parent": "1700000000.000001",
        }
        with (
            patch.object(voice, "guard_delivery"),
            patch.object(voice.BuzzBridgeClient, "read_voice", return_value=audio) as read,
            patch.object(voice.requests, "post", return_value=upload) as post,
        ):
            wire = voice.prepare(row, channel, client, wire)
        self.assertEqual(read.call_args.kwargs["event_id"], event_id)
        self.assertEqual(post.call_args.kwargs["data"], audio)
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertEqual(wire["voice_file_id"], "FVOICE")
        self.assertEqual(wire["kwargs"]["text"], "Release check")
        voice.share(client, wire)
        client.voice_call.assert_called_with(
            voice.COMPLETE, channel_id="CVOICE", files=[{"id": "FVOICE", "title": "Voice message.wav"}],
            initial_comment="Release check", thread_ts="1700000000.000001",
        )
        before = client.voice_call.call_count
        attempt = {"started_at": "2023-11-14T22:13:20+00:00"}
        actor = {"kind": "member", "slack_user_id": "UVOICE"}
        message = {"ts": "1700000000.000002", "thread_ts": wire["parent"], "user": "UVOICE",
                   "files": [{"id": "FVOICE"}]}
        client.call.return_value = {"ok": True, "messages": [message, {**message, "user": "UOTHER"}]}
        result = voice.reconcile(client, channel, actor, wire, attempt)
        self.assertEqual(result["ts"], message["ts"])
        fact = member_attempts.receipt(result, channel, actor, wire, "create")
        self.assertTrue(fact["actor_verified"])
        self.assertEqual(fact["voice_file_id"], "FVOICE")
        self.assertIsNotNone(member_attempts.response_from_receipt(fact, actor, wire, "create"))
        client.call.return_value = {"ok": True, "messages": [{**message, "files": [{"id": "FOTHER"}]}]}
        with self.assertRaises(member_delivery.MemberDeliveryDeferred):
            voice.reconcile(client, channel, actor, wire, attempt)
        self.assertEqual(client.voice_call.call_count, before)

    def test_slack_audio_file_share_enters_existing_protected_attachment_path(self):
        event = {
            "type": "message", "subtype": "file_share", "channel": "CVOICE",
            "channel_type": "channel", "ts": "1700000000.000001", "user": "UVOICE",
            "files": [{"id": "FVOICE", "title": "Voice message", "mimetype": "audio/mp4",
                       "permalink": "https://workspace.slack.com/files/UVOICE/FVOICE/voice.m4a"}],
        }
        normalized = _normalize_slack_event({"team_id": "TVOICE", "event": event})
        self.assertEqual(normalized["metadata"]["slack_file_ids"], ["FVOICE"])
        self.assertEqual(normalized["attachments"], [
            {"title": "Voice message", "url": event["files"][0]["permalink"]},
        ])
        event["files"][0]["mimetype"] = "application/pdf"
        self.assertIsNone(_normalize_slack_event({"team_id": "TVOICE", "event": event}))
