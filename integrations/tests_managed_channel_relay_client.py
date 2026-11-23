"""Contract tests for retry readback and private room roster reconciliation."""

from unittest import TestCase, mock

from integrations.services.managed_channels import relay_client


class ManagedChannelRelayClientTests(TestCase):
    def test_matching_revision_alone_does_not_confirm_delivery(self):
        projection = {"tags": [
            ["managed_revision", "1002"], ["channel_section", "project"],
            ["managed_title", "Older title"],
        ]}
        desired = {"revision": 1002, "section": "project", "title": "Current title"}
        self.assertFalse(relay_client._projection_matches(projection, desired))
        projection["tags"][-1][1] = desired["title"]
        self.assertTrue(relay_client._projection_matches(projection, desired))

    def test_native_private_room_reconciles_removed_members_first(self):
        with mock.patch.object(relay_client, "_read", side_effect=[
            {"tags": [["p", "owner"], ["p", "former"]]},
            {"tags": [["p", "owner"], ["p", "next"]]},
        ]), mock.patch.object(relay_client, "_write") as write:
            relay_client._verify_members("relay", "room", {"owner", "next"}, adopt=False)
        self.assertEqual(write.call_args_list, [
            mock.call("relay", relay_client.REMOVE_MEMBER_KIND, [["h", "room"], ["p", "former"]]),
            mock.call("relay", relay_client.ADD_MEMBER_KIND, [["h", "room"], ["p", "next"], ["role", "member"]]),
        ])

    def test_adopted_room_holds_when_its_roster_no_longer_matches(self):
        with mock.patch.object(relay_client, "_read", return_value={
            "tags": [["p", "owner"], ["p", "other"]],
        }), mock.patch.object(relay_client, "_write") as write:
            with self.assertRaisesRegex(ValueError, "differs"):
                relay_client._verify_members("relay", "room", {"owner"}, adopt=True)
        write.assert_not_called()
