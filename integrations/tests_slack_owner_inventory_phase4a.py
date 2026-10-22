"""Public inventory closure is membership evidence only after a complete sweep."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from integrations.models import CommunityBridgeChannelRefresh, SlackChannelMembership, SlackOwnerConversationInventory
from integrations.services import slack_channel_membership as membership
from integrations.services import slack_dm_mirror as dm
from integrations.services import slack_owner_inventory as inventory
from integrations.tests_slack_member_access import MemberAccessFixture


def public_row(channel_id, **changes):
    return {"id": channel_id, "name": "synthetic-channel", "is_channel": True,
            "is_private": False, "is_im": False, "is_mpim": False,
            "is_member": True, **changes}


def page(rows, cursor=""):
    return {"channels": rows, "response_metadata": {"next_cursor": cursor}}


class PublicInventoryEvidenceTests(SimpleTestCase):
    def test_exact_public_objects_and_boolean_membership_are_required(self):
        self.assertEqual(inventory._public_membership_evidence([public_row("CTEST")]), ({"CTEST"}, True))
        self.assertEqual(inventory._public_membership_evidence([public_row("CTEST", is_member=False)]), (set(), True))
        for changes in ({"is_private": True}, {"is_im": True}, {"is_mpim": True},
                        {"is_ext_shared": True}, {"is_ext_ws_shared": True}, {"is_org_shared": True},
                        {"is_member": None}, {"is_member": "true"}, {"is_private": None}):
            with self.subTest(changes=changes):
                self.assertEqual(inventory._public_membership_evidence([public_row("CTEST", **changes)]), (set(), False))

    def test_only_unfinished_authorized_sweep_requests_followup(self):
        for state, expected in (({"public_sweep": "opaque"}, True), ({"public_cursor": "opaque"}, True), ({}, False)):
            with patch.object(inventory, "public_inventory_allowed", return_value=True), patch.object(
                inventory, "state_for", return_value=state,
            ), override_settings(SLACK_OWNER_INVENTORY_ENABLED=True):
                self.assertIs(inventory.needs_public_sweep(SimpleNamespace(), object()), expected)
        with override_settings(SLACK_OWNER_INVENTORY_ENABLED=False):
            self.assertFalse(inventory.needs_public_sweep(SimpleNamespace(), object()))


@override_settings(
    MESSAGE_SYNC_ENABLED=True, SLACK_MEMBER_CHANNELS_ENABLED=True,
    SLACK_OWNER_INVENTORY_ENABLED=True, MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID="TTEST",
    SLACK_MEMBER_CHANNEL_ALLOWED_IDS="",
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class PublicInventoryMembershipDatabaseTests(MemberAccessFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.authority = dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)

    def state(self):
        self.grant.connection.refresh_from_db()
        return inventory.state_for(self.grant.connection, self.authority)

    def test_bounded_pages_persist_followup_and_do_not_reduce_before_final_page(self):
        membership.mark_joined(self.grant, "CROOM", observed_at=timezone.now(), public_verified=True)
        self.provider.side_effect = [page([public_row("CNEW")], "one"), page([], "two"),
                                     page([], "three"), page([])]
        inventory.collect_public_page(self.authority)
        self.assertEqual(self.provider.call_count, inventory.PUBLIC_PAGES_PER_TURN)
        self.assertTrue(all(call.kwargs["limit"] == 200 for call in self.provider.call_args_list))
        self.assertEqual(SlackChannelMembership.objects.get().state, "joined")
        state = self.state()
        self.assertEqual(state["public_cursor"], "three")
        self.assertEqual(state["public_membership_ids"], ["CNEW"])
        self.assertTrue(inventory.needs_public_sweep(self.grant.connection, self.authority))
        inventory.collect_public_page(self.authority)
        self.assertEqual(dict(SlackChannelMembership.objects.values_list("slack_channel_id", "state")),
                         {"CROOM": "left", "CNEW": "joined"})
        self.assertEqual(CommunityBridgeChannelRefresh.objects.get().slack_channel_id, "CNEW")
        self.grant.connection.refresh_from_db()
        self.assertFalse(inventory.needs_public_sweep(self.grant.connection, self.authority))

    def test_later_leave_wins_over_complete_inventory_started_before_leave(self):
        membership.mark_joined(self.grant, "CROOM", observed_at=timezone.now(), public_verified=True)

        def leave_while_request_is_in_flight(*args, **kwargs):
            membership.mark_left(self.grant, "CROOM", observed_at=timezone.now() + timedelta(seconds=1))
            return page([public_row("CROOM")])

        self.provider.side_effect = leave_while_request_is_in_flight
        inventory.collect_public_page(self.authority)
        self.assertEqual(SlackChannelMembership.objects.get().state, "left")

    def test_unknown_membership_or_external_evidence_cannot_prove_departures(self):
        membership.mark_joined(self.grant, "CROOM", observed_at=timezone.now(), public_verified=True)
        self.provider.return_value = page([public_row("CUNKNOWN", is_member=None),
                                          public_row("CEXTERNAL", is_ext_shared=True)])
        inventory.collect_public_page(self.authority)
        self.assertEqual(SlackChannelMembership.objects.get().state, "joined")
        self.assertFalse(CommunityBridgeChannelRefresh.objects.exists())
        self.assertEqual(SlackOwnerConversationInventory.objects.count(), 2)

    def test_concurrent_completion_rejects_stale_same_cursor_response(self):
        def competing_response(*args, **kwargs):
            self.provider.side_effect = None
            self.provider.return_value = page([public_row("CWINNER")])
            inventory._collect_public_page_once(self.authority)
            return page([public_row("CSTALE")])

        self.provider.side_effect = competing_response
        inventory.collect_public_page(self.authority)
        self.assertEqual(list(SlackOwnerConversationInventory.objects.values_list("slack_conversation_id", flat=True)), ["CWINNER"])
        self.assertEqual(list(SlackChannelMembership.objects.values_list("slack_channel_id", flat=True)), ["CWINNER"])

    def test_reconsent_during_provider_request_rejects_old_response(self):
        def changed_consent(*args, **kwargs):
            self.grant.consented_at = timezone.now() + timedelta(seconds=1)
            self.grant.save(update_fields=("consented_at",))
            return page([public_row("COLD")])

        self.provider.side_effect = changed_consent
        with self.assertRaises(dm.SlackDmMirrorAuthorizationError):
            inventory.collect_public_page(self.authority)
        self.assertFalse(SlackOwnerConversationInventory.objects.exists())
        self.assertFalse(SlackChannelMembership.objects.exists())

    def test_canary_scope_does_not_create_or_mark_out_of_scope_members_left(self):
        membership.mark_joined(self.grant, "COUTSIDE", observed_at=timezone.now(), public_verified=True)
        self.provider.return_value = page([public_row("CNEW"), public_row("COTHER")])
        with override_settings(SLACK_MEMBER_CHANNEL_ALLOWED_IDS="CNEW"):
            inventory.collect_public_page(self.authority)
        self.assertEqual(set(SlackChannelMembership.objects.values_list("slack_channel_id", "state")),
                         {("COUTSIDE", "joined"), ("CNEW", "joined")})
        self.assertEqual(list(CommunityBridgeChannelRefresh.objects.values_list("slack_channel_id", flat=True)), ["CNEW"])

    def test_flag_off_preserves_legacy_metadata_inventory_without_membership_writes(self):
        self.grant.connection.scopes += ["im:read"]
        self.grant.connection.save(update_fields=("scopes",))
        self.authority = dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)
        inventory.grant_metadata_consent(self.grant)
        self.grant.connection.refresh_from_db()
        self.provider.return_value = page([public_row("COLD")], "next")
        with override_settings(SLACK_MEMBER_CHANNELS_ENABLED=False):
            inventory.collect_public_page(self.authority)
        self.assertEqual(self.provider.call_count, 1)
        self.assertEqual(self.provider.call_args.kwargs["limit"], 20)
        self.assertEqual(SlackOwnerConversationInventory.objects.count(), 1)
        self.assertFalse(SlackChannelMembership.objects.exists())
        self.assertFalse(CommunityBridgeChannelRefresh.objects.exists())
