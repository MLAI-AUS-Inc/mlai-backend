"""Database-free regressions for explicit native person handoff."""

from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from integrations.api_views_bridge import BuzzCommunityBridgeEventView
from integrations.services.community_bridge.native_mentions import native_mention_bindings, native_mention_tags, native_recipient_targets
from integrations.services.community_bridge.identity import community_account_for_key, community_account_for_key_at
from types import SimpleNamespace


class NativeMentionTests(SimpleTestCase):
    tag = ["mlai-mention", "aa" * 32, "Member Name"]
    profile = "00000000-0000-4000-8000-000000000001"
    other_profile = "00000000-0000-4000-8000-000000000002"

    def bindings(self, tags, profiles=None):
        return [{"public_key": tag[1], "profile_id": profile} for tag, profile in
                zip(tags, profiles or [self.profile] * len(tags))]

    def test_explicit_selected_person_is_bound_to_visible_prose(self):
        self.assertEqual(native_mention_tags("Give @Member Name 4 points", [self.tag]), [self.tag])
        for content in ("@Member Names", "`@Member Name`", "```\n@Member Name\n```", "    @Member Name", "no selected person"):
            with self.subTest(content=content):
                self.assertEqual(native_mention_tags(content, [self.tag]), [])

    def test_broadcast_reply_routing_and_plain_names_never_become_targets(self):
        self.assertEqual(native_mention_tags("@Member Name @everyone", [
            ["p", "aa" * 32], ["p", "bb" * 32], ["agent", "aa" * 32], ["slack-mention", "UONE", "Member Name"],
        ]), [])

    def test_invalid_labels_keys_and_bounds_are_rejected(self):
        for tag in (["mlai-mention", "invalid", "Member Name"], ["mlai-mention", "AA" * 32, "Member Name"],
                    ["mlai-mention", "aa" * 32, "@Member"], ["mlai-mention", "aa" * 32, "x" * 81],
                    ["mlai-mention", "aa" * 32, "line\nbreak"], ["mlai-mention", "aa" * 32]):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                native_mention_tags("@Member Name", [tag])
        for control in ("\x7f", "\x85", "\x9f"):
            with self.subTest(control=control), self.assertRaises(ValueError):
                native_mention_tags("@Member Name", [["mlai-mention", "aa" * 32, "Member" + control]])
        with self.assertRaises(ValueError):
            native_mention_tags("@Member Name", [self.tag] * 21)

    def test_duplicate_labels_cannot_choose_between_different_people(self):
        tags = [self.tag, ["mlai-mention", "bb" * 32, "member name"]]
        with patch("integrations.services.community_bridge.identity.community_account_for_key", side_effect=[
            {"user_profile_id": self.profile, "display_name": "Same Name"},
            {"user_profile_id": self.other_profile, "display_name": "Same Name"},
        ]):
            self.assertEqual(native_recipient_targets(workspace_id="TMLAI", text="@Member Name", tags=tags, bindings=self.bindings(tags, [self.profile, self.other_profile])), [])

    def test_same_label_on_multiple_devices_for_one_account_is_one_recipient(self):
        tags = [self.tag, ["mlai-mention", "bb" * 32, "member name"]]
        with patch("integrations.services.community_bridge.identity.community_account_for_key", return_value={
            "user_profile_id": self.profile, "display_name": "Canonical Name",
        }):
            self.assertEqual(native_recipient_targets(workspace_id="TMLAI", text="@Member Name", tags=tags, bindings=self.bindings(tags)), [{
                "kind": "mlai_profile", "profile_id": self.profile, "display_name": "Canonical Name",
                "source_labels": ["Member Name", "member name"],
            }])

    def test_devices_resolve_one_canonical_account_and_labels_do_not_supply_names(self):
        tags = [self.tag, ["mlai-mention", "bb" * 32, "Other Device"], ["mlai-mention", "cc" * 32, "Revoked"]]
        with patch("integrations.services.community_bridge.identity.community_account_for_key", side_effect=[
            {"user_profile_id": self.profile, "display_name": "Canonical Name"},
            {"user_profile_id": self.profile, "display_name": "Canonical Name"}, None,
        ]) as resolve:
            result = native_recipient_targets(workspace_id="TMLAI", text="@Member Name @Other Device @Revoked", tags=tags, bindings=self.bindings(tags))
        self.assertEqual(result, [{"kind": "mlai_profile", "profile_id": self.profile, "display_name": "Canonical Name", "source_labels": ["Member Name", "Other Device"]}])
        self.assertEqual(resolve.call_count, 3)

    def test_missing_or_changed_original_owner_never_authorizes_a_current_device(self):
        for bindings in (None, [], self.bindings([self.tag], [self.other_profile])):
            with self.subTest(bindings=bindings), patch(
                "integrations.services.community_bridge.identity.community_account_for_key",
                return_value={"user_profile_id": self.profile, "display_name": "Current owner"},
            ):
                self.assertEqual(native_recipient_targets(
                    workspace_id="TMLAI", text="@Member Name", tags=[self.tag], bindings=bindings,
                ), [])

    def test_snapshot_uses_signed_source_time_and_never_display_name_identity(self):
        with patch("integrations.services.community_bridge.identity.community_account_for_key_at",
                   return_value={"user_profile_id": self.profile}) as resolve:
            self.assertEqual(native_mention_bindings(
                workspace_id="TMLAI", text="@Member Name", tags=[self.tag], source_created_at=100,
            ), self.bindings([self.tag]))
        resolve.assert_called_once_with(slack_workspace_id="TMLAI", buzz_pubkey="aa" * 32, source_created_at=100)

    def test_original_ambiguous_label_does_not_become_authority_after_other_owner_revocation(self):
        tags = [self.tag, ["mlai-mention", "bb" * 32, "member name"]]
        with patch("integrations.services.community_bridge.identity.community_account_for_key", side_effect=[
            {"user_profile_id": self.profile, "display_name": "Current owner"}, None,
        ]):
            self.assertEqual(native_recipient_targets(workspace_id="TMLAI", text="@Member Name", tags=tags,
                bindings=self.bindings(tags, [self.profile, self.other_profile])), [])

    def test_current_canonical_slack_identity_is_optional_target_context(self):
        with patch("integrations.services.community_bridge.identity.community_account_for_key", return_value={
            "user_profile_id": self.profile, "display_name": "Member", "slack_user_id": "UMEMBER",
        }):
            result = native_recipient_targets(workspace_id="TMLAI", text="@Member Name", tags=[self.tag],
                                              bindings=self.bindings([self.tag]))
        self.assertEqual(result[0]["slack_user_id"], "UMEMBER")

    def test_malformed_or_conflicting_binding_snapshots_fail_closed(self):
        for bindings in ("bad", [{}], [{"public_key": "aa" * 32, "profile_id": "name"}],
                         self.bindings([self.tag]) * 21,
                         self.bindings([self.tag, self.tag], [self.profile, self.other_profile])):
            with self.subTest(bindings=bindings), self.assertRaises(ValueError):
                native_recipient_targets(workspace_id="TMLAI", text="@Member Name", tags=[self.tag], bindings=bindings)


class NativeMentionIngressTests(SimpleTestCase):
    def post(self, raw_tags, *, metadata=None, raw_payload=None):
        payload = {"receipt_key": "synthetic-receipt", "source_channel_id": "channel", "event_type": "create",
                   "normalized_event": {"text": "@Member Name", "metadata": metadata or {}}, "raw_payload": {"tags": raw_tags}}
        if raw_payload is not None:
            payload["raw_payload"] = raw_payload
        with (patch("integrations.api_views_bridge.BuzzBridgeClient.validate_callback_signature", return_value=True),
              patch("integrations.api_views_bridge.ingest_mlai_dm_event", return_value=None),
              patch("integrations.api_views_bridge.ingest_inbound_event", return_value={"status": "enqueued"}) as ingest):
            result = BuzzCommunityBridgeEventView.as_view()(APIRequestFactory().post("/callback", payload, format="json"))
        return result, ingest

    def test_only_authenticated_raw_explicit_tags_replace_normalized_hints(self):
        tag = NativeMentionTests.tag
        response, ingest = self.post([tag, ["p", "bb" * 32]], metadata={"mlai_mention_tags": [["mlai-mention", "cc" * 32, "Forged"]]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ingest.call_args.kwargs["normalized_event"]["metadata"]["mlai_mention_tags"], [tag])
        response, ingest = self.post([], metadata={"mlai_mention_tags": [tag]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ingest.call_args.kwargs["normalized_event"]["metadata"]["mlai_mention_tags"], [])

    def test_malformed_explicit_target_fails_before_enqueue(self):
        response, ingest = self.post([["mlai-mention", "invalid", "Member Name"]])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"error": "invalid_native_mentions"})
        ingest.assert_not_called()

    def test_non_object_raw_source_cannot_keep_normalized_recipient_hints(self):
        response, ingest = self.post([], raw_payload="invalid", metadata={"mlai_mention_tags": [NativeMentionTests.tag]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ingest.call_args.kwargs["normalized_event"]["metadata"]["mlai_mention_tags"], [])

    def test_client_supplied_binding_metadata_is_overwritten_by_server_snapshot(self):
        trusted = [{"public_key": "aa" * 32, "profile_id": NativeMentionTests.profile}]
        forged = [{"public_key": "aa" * 32, "profile_id": NativeMentionTests.other_profile}]
        with patch("integrations.api_views_bridge.native_mention_bindings", return_value=trusted) as snapshot:
            response, ingest = self.post([NativeMentionTests.tag], metadata={"mlai_mention_bindings": forged},
                raw_payload={"created_at": 100, "tags": [NativeMentionTests.tag]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ingest.call_args.kwargs["normalized_event"]["metadata"]["mlai_mention_bindings"], trusted)
        self.assertEqual(snapshot.call_args.kwargs["source_created_at"], 100)

    def test_missing_source_time_cannot_keep_a_forged_binding(self):
        response, ingest = self.post([NativeMentionTests.tag], metadata={"mlai_mention_bindings": [
            {"public_key": "aa" * 32, "profile_id": NativeMentionTests.profile},
        ]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ingest.call_args.kwargs["normalized_event"]["metadata"]["mlai_mention_bindings"], [])


@override_settings(MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID="TMLAI")
class NativeRecipientDeviceTests(SimpleTestCase):
    def test_invalid_source_timestamps_do_not_query_or_rebind_current_owners(self):
        with patch("integrations.services.community_bridge.identity.CommunityChatDevice.objects") as devices:
            for timestamp in (None, True, -1, 0, "100", 10 ** 100):
                with self.subTest(timestamp=timestamp):
                    self.assertIsNone(community_account_for_key_at(
                        slack_workspace_id="TMLAI", buzz_pubkey="aa" * 32, source_created_at=timestamp,
                    ))
            devices.filter.assert_not_called()

    def test_chat_only_recipient_needs_active_verified_device_and_community_scope(self):
        with patch("integrations.services.community_bridge.identity.CommunityChatDevice.objects") as devices:
            user = SimpleNamespace(community_chat_profile_id="canonical-profile", full_name="Chat Member", slack_id=None)
            devices.select_related.return_value.filter.return_value.first.return_value = SimpleNamespace(user_id=42, pk=8, user=user)
            result = community_account_for_key(slack_workspace_id="TMLAI", buzz_pubkey="aa" * 32)
            self.assertEqual(result["user_profile_id"], "canonical-profile")
            devices.select_related.return_value.filter.assert_called_once_with(
                public_key="aa" * 32, status="verified", revoked_at__isnull=True, user__is_active=True,
            )
            devices.reset_mock()
            self.assertIsNone(community_account_for_key(slack_workspace_id="TOTHER", buzz_pubkey="aa" * 32))
            devices.select_related.assert_not_called()
            devices.select_related.return_value.filter.return_value.first.return_value = None
            self.assertIsNone(community_account_for_key(slack_workspace_id="TMLAI", buzz_pubkey="aa" * 32))
