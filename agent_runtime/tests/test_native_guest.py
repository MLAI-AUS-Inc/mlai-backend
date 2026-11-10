"""Fixed guest replies and owner-chosen Dot avatars, without a database or network."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings
from rest_framework.exceptions import PermissionDenied, ValidationError

from agent_runtime import native_crypto, native_guest, native_installations, native_sources
from agent_runtime.tests.test_native_pilot import CHANNEL, COMMUNITY, CONFIG, NOW, OWNER_KEY, fixture

GUEST = "c" * 64
OTHER_CHANNEL = "33333333-3333-4333-8333-333333333333"
FLAGS = dict(AGENT_RUNTIME_ENABLED=True, AGENT_RUNTIME_NATIVE_ENABLED=True, AGENT_RUNTIME_NATIVE_REPLIES_ENABLED=True,
             AGENT_RUNTIME_PERSONAL_CONTEXT_ENABLED=True, AGENT_RUNTIME_NATIVE_GUEST_REPLY_ENABLED=True,
             AGENT_RUNTIME_NATIVE_COMMUNITIES=CONFIG)


def mention(event_id="d" * 64, author=GUEST, channel=OTHER_CHANNEL, **extra):
    return {"event_id": event_id, "root_event_id": event_id, "author_pubkey": author, "created_at": int(NOW.timestamp()),
            "community_id": COMMUNITY, "channel_id": channel, "channel_type": "stream", "public": True, **extra}


@override_settings(**FLAGS)
class GuestReplyTests(SimpleTestCase):
    def setUp(self):
        self.installation, self.grant, _ = fixture()
        self.pages = [{"events": [mention()], "cursor": {"since": int(NOW.timestamp()), "after_event_id": "d" * 64},
                       "has_more": False}]
        self.queries, self.sent, self.denied = [], [], set()

        def request(config, path, payload):
            if path == "/query":
                self.queries.append(payload[0]["native_agent"])
                return self.pages[len(self.queries) - 1]
            if payload["tags"][2][1] in self.denied:
                raise PermissionDenied("Native relay rejected this agent's authority.")
            self.sent.append(payload)
            return {"event_id": payload["id"], "accepted": True}

        self.transport = self.enterContext(patch.object(native_sources, "_request", side_effect=request))
        self.enterContext(patch.object(native_sources, "current_device", return_value=self.installation.owner_device))
        installations = self.enterContext(patch.object(native_guest.AgentInstallation, "objects"))
        installations.filter.return_value.exclude.return_value.order_by.return_value.values_list.return_value = [self.installation.pk]
        installations.select_related.return_value.get.return_value = self.installation
        grants = self.enterContext(patch.object(native_guest.AgentGrant, "objects"))
        grants.filter.return_value.order_by.return_value.first.return_value = self.grant
        self.audits = self.enterContext(patch.object(native_guest.AgentAuditEvent, "objects"))
        self.audits.filter.return_value.count.return_value = 0
        self.audit = self.enterContext(patch.object(native_guest.runtime, "audit"))

    def run_tick(self):
        return native_guest.answer_guest_mentions(now=NOW)

    def test_flags_default_off_and_require_personal_conversation_access(self):
        for overrides in ({"AGENT_RUNTIME_NATIVE_GUEST_REPLY_ENABLED": False},
                          {"AGENT_RUNTIME_PERSONAL_CONTEXT_ENABLED": False},
                          {"AGENT_RUNTIME_NATIVE_REPLIES_ENABLED": False}):
            with self.subTest(overrides=overrides), override_settings(**overrides):
                self.assertEqual(self.run_tick(), 0)
        self.transport.assert_not_called()

    def test_one_fixed_reply_threaded_under_the_guest_and_signed_by_the_dot(self):
        self.assertEqual(self.run_tick(), 1)
        scan = self.queries[0]
        self.assertEqual(scan["operation"], "guest_mentions")
        self.assertEqual(scan["execution_mode"], "guest")
        self.assertEqual(scan["channel_id"], CHANNEL)
        self.assertEqual(scan["since"], int((NOW - native_guest.WINDOW).timestamp()))
        self.assertNotIn("after_event_id", scan)
        event = self.sent[0]
        self.assertEqual(event["pubkey"], self.installation.public_key)
        self.assertEqual(event["kind"], 9)
        self.assertEqual(event["content"], native_guest.reply_text(self.installation))
        self.assertIn("My Dot", event["content"])
        self.assertIn("only take requests", event["content"])
        tags = {tag[0]: tag for tag in event["tags"] if tag[0] != "e"}
        self.assertEqual(tags["h"], ["h", OTHER_CHANNEL])
        self.assertEqual(tags["p"], ["p", GUEST])
        self.assertEqual([tag for tag in event["tags"] if tag[0] == "e"],
                         [["e", "d" * 64, "", "root"], ["e", "d" * 64, "", "reply"]])
        context = json.loads(tags["native_invocation"][1])
        self.assertEqual({key: context[key] for key in ("operation", "execution_mode", "channel_id", "trigger_event_id",
                                                        "root_event_id", "guest_pubkey", "owner_pubkey", "bot_pubkey")},
                         {"operation": "guest_reply", "execution_mode": "guest", "channel_id": OTHER_CHANNEL,
                          "trigger_event_id": "d" * 64, "root_event_id": "d" * 64, "guest_pubkey": GUEST,
                          "owner_pubkey": self.installation.owner_device.public_key,
                          "bot_pubkey": self.installation.public_key})
        self.assertNotIn("invocation_id", context)
        self.assertLessEqual(context["expires_at"] - event["created_at"], 900)
        # The audit trail keeps identifiers only: no text and no guest key.
        metadata = self.audit.call_args.kwargs
        self.assertEqual(self.audit.call_args.args, (native_guest.REPLIED,))
        self.assertEqual(metadata["trigger_event_id"], "d" * 64)
        self.assertNotIn(GUEST, json.dumps({k: v for k, v in metadata.items() if k != "grant"}))
        self.assertNotIn("content", metadata)

    def test_owner_mentions_and_malformed_rows_are_never_answered(self):
        owner = self.installation.owner_device.public_key
        self.pages[0]["events"] = [mention("1" * 64, author=owner), {"event_id": "nope"},
                                   mention("2" * 64, channel="not-a-uuid"), mention("3" * 64)]
        self.assertEqual(self.run_tick(), 1)
        self.assertEqual([event["tags"][2][1] for event in self.sent], ["3" * 64])

    def test_one_refused_mention_does_not_stop_the_rest(self):
        self.pages[0]["events"] = [mention("1" * 64), mention("2" * 64)]
        self.denied.add("1" * 64)
        self.assertEqual(self.run_tick(), 1)
        self.assertEqual(self.sent[0]["tags"][2][1], "2" * 64)

    def test_pages_follow_the_relay_cursor_within_one_tick(self):
        self.pages = [{"events": [mention("1" * 64)], "cursor": {"since": 1700000000, "after_event_id": "1" * 64}, "has_more": True},
                      {"events": [mention("2" * 64)], "cursor": {"since": 1700000001, "after_event_id": "2" * 64}, "has_more": False}]
        self.assertEqual(self.run_tick(), 2)
        self.assertEqual((self.queries[1]["since"], self.queries[1]["after_event_id"]), (1700000000, "1" * 64))

    def test_invalid_pages_and_scan_denials_send_nothing(self):
        for page in ({"events": None, "cursor": {}, "has_more": False}, {"events": [], "cursor": {}, "has_more": "no"},
                     {"events": [mention()], "cursor": {"since": "x"}, "has_more": True}):
            with self.subTest(page=page):
                self.queries.clear()
                self.sent.clear()
                self.pages = [page]
                self.run_tick()
                self.assertEqual([event["tags"][2][1] for event in self.sent], ["d" * 64] if page["events"] else [])
        self.transport.side_effect = PermissionDenied("denied")
        self.assertEqual(self.run_tick(), 0)

    def test_daily_cap_bounds_what_a_member_can_make_the_dot_post(self):
        self.audits.filter.return_value.count.return_value = native_guest.DAILY_REPLY_CAP
        self.assertEqual(self.run_tick(), 0)
        self.transport.assert_not_called()
        self.audits.filter.return_value.count.return_value = native_guest.DAILY_REPLY_CAP - 1
        self.pages[0]["events"] = [mention("1" * 64), mention("2" * 64)]
        self.assertEqual(self.run_tick(), 1)

    def test_revoked_or_moved_connections_never_speak_for_the_owner(self):
        grants = native_guest.AgentGrant.objects
        grants.filter.return_value.order_by.return_value.first.return_value = None
        self.assertEqual(self.run_tick(), 0)
        grants.filter.return_value.order_by.return_value.first.return_value = self.grant
        with patch.object(native_sources, "current_device", return_value=SimpleNamespace(pk=uuid4())):
            self.assertEqual(self.run_tick(), 0)
        self.installation.owner.is_active = False
        self.assertEqual(self.run_tick(), 0)
        self.transport.assert_not_called()


@override_settings(**FLAGS)
class DotPictureTests(SimpleTestCase):
    picture = "https://chat.example/media/" + "ab" * 32 + ".png"

    def setUp(self):
        self.installation, self.grant, _ = fixture()
        self.installation.owner_attestation = ["auth", "a" * 64, "", "b" * 128]
        self.installation.profile_events = []
        self.installation.profile_events = native_installations.profile_events(self.installation, self.grant, now=NOW)
        self.installation.grants = MagicMock()
        self.installation.grants.filter.return_value.order_by.return_value.first.return_value = self.grant
        self.installation.save = MagicMock()
        self.sent = []
        self.transport = self.enterContext(patch.object(native_sources, "_request", side_effect=lambda config, path, event: (
            self.sent.append((path, event)) or {"event_id": event["id"], "accepted": True})))
        self.enterContext(patch.object(native_sources, "current_device", return_value=self.installation.owner_device))
        rows = iter([self.installation.owner, self.installation])
        self.enterContext(patch.object(native_installations.runtime, "locked",
                                       side_effect=lambda queryset: SimpleNamespace(get=lambda **kw: next(rows))))
        self.audit = self.enterContext(patch.object(native_installations.runtime, "audit"))

    def set_picture(self, picture):
        return native_installations.set_picture.__wrapped__(self.installation.owner, self.installation.pk, picture, now=NOW)

    def test_only_media_upload_urls_are_accepted(self):
        for value in (self.picture, "https://chat.example/media/" + "0" * 64 + ".jpeg",
                      "https://cdn.chat.example:8443/a/b/" + "f" * 64 + ".webp"):
            self.assertTrue(native_installations.valid_picture(value), value)
        for value in ("http://chat.example/media/" + "a" * 64 + ".png", self.picture + "?x=1", self.picture + "#x",
                      "https://user@chat.example/media/" + "a" * 64 + ".png", "https://chat.example/media/" + "A" * 64 + ".png",
                      "https://chat.example/media/" + "a" * 63 + ".png", "https://chat.example/media/" + "a" * 64 + ".svg",
                      "https://chat.example/media/../" + "a" * 64 + ".png", "javascript:alert(1)", None, 7):
            self.assertFalse(native_installations.valid_picture(value), value)
        with self.assertRaises(ValidationError):
            self.set_picture("https://elsewhere.example/a.png")
        self.transport.assert_not_called()

    def test_picture_is_published_in_a_newer_signed_profile_and_kept_on_reconnect(self):
        previous = self.installation.profile_events
        self.set_picture(self.picture)
        path, event = self.sent[0]
        self.assertEqual(path, "/events")
        self.assertEqual(event["kind"], 0)
        self.assertEqual(event["pubkey"], self.installation.public_key)
        self.assertGreater(event["created_at"], previous[0]["created_at"])
        self.assertEqual(json.loads(event["content"]), {"name": "My Dot", "display_name": "My Dot", "bot": True,
                                                         "picture": self.picture})
        self.assertEqual([tag[0] for tag in event["tags"]], ["native_enrollment", "native_installation", "auth"])
        self.assertEqual(json.loads(event["tags"][0][1])["operation"], "enroll")
        self.assertEqual(self.installation.profile_events[0], event)
        self.assertEqual(self.installation.profile_events[1], previous[1])
        self.assertEqual(native_installations.current_picture(self.installation), self.picture)
        self.assertEqual(self.audit.call_args.kwargs["cleared"], False)
        renewed = native_installations.profile_events(self.installation, self.grant, now=NOW)
        self.assertEqual(json.loads(renewed[0]["content"])["picture"], self.picture)
        self.assertNotIn("picture", json.loads(renewed[1]["content"]))

    def test_picture_can_be_cleared(self):
        self.installation.profile_events = [{**self.installation.profile_events[0], "content": json.dumps(
            {"name": "My Dot", "picture": self.picture})}, self.installation.profile_events[1]]
        self.set_picture(None)
        self.assertNotIn("picture", json.loads(self.sent[0][1]["content"]))
        self.assertIsNone(native_installations.current_picture(self.installation))

    def test_relay_refusal_keeps_the_previous_profile(self):
        previous = list(self.installation.profile_events)
        self.transport.side_effect = PermissionDenied("Native relay rejected this agent's authority.")
        with self.assertRaises(PermissionDenied):
            self.set_picture(self.picture)
        self.assertEqual(self.installation.profile_events, previous)
        self.installation.save.assert_not_called()

    def test_unconnected_or_community_agents_cannot_set_a_picture(self):
        for field, value in (("app_id", "mlai-community"), ("state", "provisioning"), ("public_key", None)):
            with self.subTest(field=field):
                installation, _, _ = fixture()
                setattr(installation, field, value)
                rows = iter([installation.owner, installation])
                with patch.object(native_installations.runtime, "locked",
                                  side_effect=lambda queryset: SimpleNamespace(get=lambda **kw: next(rows))), \
                        self.assertRaises(PermissionDenied):
                    native_installations.set_picture.__wrapped__(installation.owner, installation.pk, self.picture, now=NOW)
        self.transport.assert_not_called()

    def test_owner_key_is_never_the_signer(self):
        self.set_picture(self.picture)
        self.assertNotEqual(self.sent[0][1]["pubkey"], native_crypto.public_key(OWNER_KEY))
