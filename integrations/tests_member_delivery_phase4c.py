"""Synthetic provider calls with real committed command and authority boundaries."""

from datetime import timedelta
import threading
from unittest.mock import patch
import uuid

from django.db import close_old_connections, transaction
from django.test import (
    SimpleTestCase,
    TransactionTestCase,
    override_settings,
    tag,
    skipUnlessDBFeature,
)
from django.utils import timezone

from community_chat.models import CommunityChatDevice
from integrations.models import (
    CommunityBridgeDelivery,
    CommunityBridgeMessageLink,
    CommunityBridgeIdentityLink,
    ExternalServiceConnection,
    SlackDmMirrorGrant,
)
from integrations.services import slack_dm_mirror as dm
from integrations.services.community_bridge import member_delivery as member
from integrations.services.community_bridge.store import (
    claim_ready_deliveries,
    ingest_slack_event,
    ingest_inbound_event,
)
from integrations.services.message_sync.delivery import delivery_context
from integrations.services.message_sync.scheduler import LeaseLost, BudgetDeferred
from integrations.tests_message_sync_member_history_phase4b import Fixture, SETTINGS


class MemberDeliveryFixture(Fixture):
    def setUp(self):
        super().setUp()
        for grant in (self.a, self.b):
            grant.connection.scopes += ["chat:write", "reactions:write", "users:read"]
            grant.connection.save()
            CommunityBridgeIdentityLink.objects.create(
                user=grant.user,
                slack_workspace_id="TTEST",
                slack_user_id=grant.slack_user_id,
                buzz_pubkey=str(1 if grant.pk == self.a.pk else 2) * 64,
                display_name="Synthetic",
                verification_method="operator_attested",
                verification_reference="fixture",
                verified_at=timezone.now(),
            )
        self.mutations = []
        self.history_messages = []

    def delivery(
        self, op="create", source="buzz", source_id=None, author=None, parent=""
    ):
        source_id = source_id or (
            "a" * 64 if source == "buzz" else f"{self.now - 3}.000001"
        )
        row = CommunityBridgeDelivery.objects.create(
            channel=self.channel,
            source_platform=source,
            target_platform="slack" if source == "buzz" else "buzz",
            source_channel_id=self.channel.destination_channel_id
            if source == "buzz"
            else "CROOM",
            source_message_id=source_id,
            source_parent_message_id=parent,
            delivery_type=op,
            target_channel_id="CROOM"
            if source == "buzz"
            else self.channel.destination_channel_id,
            payload={
                "source_author_id": author
                or ("1" * 64 if source == "buzz" else "UHISTORY1"),
                "text": "hello",
                "attachments": [],
                "metadata": {"slack_raw_text": "hello"},
            },
            available_at=timezone.now(),
        )
        return row

    def send(self, row):
        items = claim_ready_deliveries(1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], row.pk)
        with delivery_context(items[0]):
            member.execute(items[0])
        row.refresh_from_db()

    def transport(self, authority, conn, method, **kwargs):
        fence = kwargs.pop("before_request", None)
        if fence:
            fence()
        self.calls.append((authority.grant_id, method, kwargs))
        if method == "conversations_info":
            return {
                "ok": True,
                "channel": {
                    "id": "CROOM",
                    "is_channel": True,
                    "is_private": False,
                    "is_member": True,
                    "num_members": 2,
                },
            }
        if method in {"conversations_history", "conversations_replies"}:
            return {"ok": True, "messages": self.history_messages, "has_more": False}
        self.mutations.append((authority.grant_id, method, kwargs))
        return {
            "ok": True,
            "channel": "CROOM",
            "ts": kwargs.get("ts") or f"{self.now}.000001",
            "message": {
                "user": authority.slack_user_id,
                "client_msg_id": kwargs.get("client_msg_id"),
            },
        }

    def retry(self, row):
        row.refresh_from_db()
        row.status = "failed"
        row.lease_token = None
        row.lease_expires_at = None
        row.available_at = timezone.now()
        row.save()


@override_settings(**SETTINGS, COMMUNITY_CHAT_AI_CONSENT_REQUIRED=False)
class MemberDeliveryTests(MemberDeliveryFixture, TransactionTestCase):
    def test_member_create_uses_author_without_bot_header_and_persists_actor(self):
        row = self.delivery()
        with patch("integrations.services.community_bridge.observability.logger.info") as logger:
            self.send(row)
        sends = [call for call in logger.call_args_list if call.args[1] == "slack_member_send"]
        self.assertEqual(len(sends), 1)
        self.assertIn("poster=member", sends[0].args[2])
        self.assertIn(f"delivery_id={row.pk}", sends[0].args[2])
        self.assertIn("outcome=settled", sends[0].args[2])
        self.assertEqual(row.status, "completed")
        actor = row.payload[member.MEMBER_STAMP]["actor"]
        self.assertEqual(actor["grant_id"], self.a.pk)
        self.assertNotIn("token", str(actor))
        self.assertEqual(self.mutations[0][2]["text"], "hello")
        link = CommunityBridgeMessageLink.objects.get()
        self.assertEqual(link.destination_payload["poster"], actor)
        self.assertEqual(self.calls[0][1], "conversations_info")
        self.bot.assert_not_called()

    def test_create_identity_is_committed_before_first_provider_request(self):
        row = self.delivery()

        def check(authority, conn, method, **kwargs):
            current = CommunityBridgeDelivery.objects.get(pk=row.pk)
            self.assertIn(member.SLACK_STAMP, current.payload)
            return self.transport(authority, conn, method, **kwargs)

        with patch.object(
            dm, "_invoke_slack_with_locked_connection", side_effect=check
        ):
            self.send(row)

    def test_timeout_preserves_exact_command_and_same_actor_on_retry(self):
        row = self.delivery()

        def timeout(authority, conn, method, **kwargs):
            if method == "chat_postMessage":
                raise TimeoutError("synthetic transport ambiguity")
            return self.transport(authority, conn, method, **kwargs)

        with patch.object(
            dm, "_invoke_slack_with_locked_connection", side_effect=timeout
        ):
            with self.assertRaises(TimeoutError):
                self.send(row)
        row.refresh_from_db()
        stamp = row.payload[member.MEMBER_STAMP]
        self.retry(row)
        self.send(row)
        self.assertEqual(row.payload[member.MEMBER_STAMP], stamp)
        self.assertEqual(self.mutations[-1][2], stamp["wire"]["kwargs"])
        self.assertEqual(len(self.mutations), 1)

    def test_selected_poster_revocation_never_chooses_another_grant_or_bot(self):
        row = self.delivery()
        with patch.object(member, "_finish_slack", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.send(row)
        row.refresh_from_db()
        actor = row.payload[member.MEMBER_STAMP]["actor"]
        SlackDmMirrorGrant.objects.filter(pk=self.a.pk).update(status="paused")
        self.retry(row)
        self.mutations.clear()
        with self.assertRaises(member.MemberDeliveryUnavailable):
            self.send(row)
        row.refresh_from_db()
        self.assertEqual(row.payload[member.MEMBER_STAMP]["actor"], actor)
        self.assertEqual(self.mutations, [])
        self.bot.assert_not_called()

    @override_settings(MESSAGE_SYNC_ENABLED=False)
    def test_flag_off_member_never_uses_bot_or_provider(self):
        row = self.delivery()
        with self.assertRaises(member.MemberDeliveryDeferred):
            self.send(row)
        self.assertEqual(self.calls, [])
        self.bot.assert_not_called()

    def test_pending_audience_stops_before_provider(self):
        row = self.delivery()
        self.channel.audience_sync_pending = True
        self.channel.save()
        with self.assertRaises(member.MemberDeliveryDeferred):
            self.send(row)
        self.assertEqual(self.calls, [])

    def test_changed_audience_does_not_rebase_committed_request(self):
        row = self.delivery()
        with patch.object(member, "_finish_slack", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.send(row)
        row.refresh_from_db()
        saved = row.payload[member.MEMBER_STAMP]
        self.ack()
        self.retry(row)
        self.mutations.clear()
        with self.assertRaises(member.MemberDeliveryUnavailable):
            self.send(row)
        row.refresh_from_db()
        self.assertEqual(row.payload[member.MEMBER_STAMP], saved)
        self.assertEqual(self.mutations, [])

    def test_exact_lease_expiry_after_quota_blocks_mutation(self):
        row = self.delivery()

        def expire(authority, conn, method, **kwargs):
            if method == "chat_postMessage":
                # Row locks cannot extend a lease's clock.
                with patch.object(
                    member.timezone,
                    "now",
                    return_value=timezone.now() + timedelta(minutes=5),
                ):
                    kwargs["before_request"]()
                self.fail("expired lease reached SDK")
            return self.transport(authority, conn, method, **kwargs)

        with patch.object(
            dm, "_invoke_slack_with_locked_connection", side_effect=expire
        ):
            with self.assertRaises(LeaseLost):
                self.send(row)
        self.assertEqual(self.mutations, [])

    def test_edit_delete_use_original_poster_cleanup_without_ai(self):
        create = self.delivery()
        self.send(create)
        for operation in ("edit", "delete"):
            row = self.delivery(operation)
            with (
                override_settings(COMMUNITY_CHAT_AI_CONSENT_REQUIRED=True),
                patch.object(
                    member, "has_ai_consent", return_value=operation == "edit"
                ),
            ):
                self.send(row)
            self.assertEqual(self.mutations[-1][0], self.a.pk)
            self.assertEqual(row.status, "completed")

    def test_reactor_b_uses_b_not_parent_a_and_remove_uses_recorded_b(self):
        create = self.delivery()
        self.send(create)
        add = self.delivery(
            "reaction_add",
            source_id="b" * 64,
            author="2" * 64,
            parent=create.source_message_id,
        )
        add.payload["text"] = "👍"
        add.save()
        self.send(add)
        self.assertEqual(self.mutations[-1][0], self.b.pk)
        remove = self.delivery(
            "reaction_remove",
            source_id=add.source_message_id,
            author="2" * 64,
            parent=create.source_message_id,
        )
        remove.payload["text"] = "👍"
        remove.save()
        self.send(remove)
        self.assertEqual(self.mutations[-1][0], self.b.pk)

    def test_malformed_info_does_not_write_or_remove_membership(self):
        row = self.delivery()
        with patch.object(
            dm,
            "_invoke_slack_with_locked_connection",
            return_value={"ok": True, "channel": {"id": "CROOM"}},
        ):
            with self.assertRaises(member.MemberDeliveryDeferred):
                self.send(row)
        self.assertEqual(self.a.public_channel_memberships.first().state, "joined")
        self.assertEqual(self.mutations, [])

    def test_source_private_is_not_a_public_send(self):
        row = self.delivery()
        with patch.object(
            dm,
            "_invoke_slack_with_locked_connection",
            return_value={"ok": True, "channel": {"id": "CROOM", "is_private": True}},
        ):
            with self.assertRaises(member.MemberDeliveryDeferred):
                self.send(row)
        self.assertEqual(self.mutations, [])

    def test_buzz_send_has_exact_saved_scope_no_bot_profile_io(self):
        row = self.delivery(source="slack")
        result = {
            "channel_id": self.channel.destination_channel_id,
            "message_id": "d" * 64,
            "parent_message_id": "",
        }
        with patch.object(
            member.BuzzBridgeClient, "deliver", return_value=result
        ) as adapter:
            self.send(row)
        command = row.payload[member.MEMBER_STAMP]
        self.assertEqual(adapter.call_args.kwargs, command["wire"])
        self.assertEqual(command["scope"], command["wire"]["member_scope"])
        self.assertEqual(row.status, "completed")
        self.bot.assert_not_called()

    def test_custom_emoji_image_is_frozen_with_the_member_command(self):
        from integrations.services.slack_custom_emoji import resolve_catalog
        row = self.delivery(source="slack")
        row.payload["text"] = ":party_parrot: :thumbsup::skin-tone-4:"
        row.save()
        catalog = resolve_catalog({"party_parrot": "https://emoji.slack-edge.com/TTEST/parrot/id.gif"})
        response = {"channel_id": self.channel.destination_channel_id, "message_id": "d"*64, "parent_message_id":""}
        with patch("integrations.services.slack_custom_emoji.load_catalog",return_value=catalog), patch.object(member.BuzzBridgeClient,"deliver",return_value=response) as adapter:
            self.send(row)
        wire = row.payload[member.MEMBER_STAMP]["wire"]
        self.assertIn(":party_parrot: 👍🏽", wire["text"])
        self.assertEqual(wire["emojis"], [{"shortcode":"party_parrot","url":catalog["party_parrot"]["url"]}])
        self.assertEqual(adapter.call_args.kwargs,wire)
        self.assertEqual(row.canonical_envelope,wire)

    def test_long_custom_reaction_restores_provider_name_for_write_and_reconciliation(self):
        from integrations.services.slack_custom_emoji import resolve_catalog
        from integrations.services.slack_emoji import slack_reaction_to_emoji
        create=self.delivery()
        self.send(create)
        name="x"*100
        add=self.delivery("reaction_add",source_id="b"*64,parent=create.source_message_id)
        add.payload["text"]=slack_reaction_to_emoji(name)
        add.save()
        catalog=resolve_catalog({name:"https://emoji.slack-edge.com/TTEST/custom/id.gif"})
        with patch("integrations.services.slack_custom_emoji.load_catalog",return_value=catalog):
            self.send(add)
        self.assertEqual(self.mutations[-1][2]["name"],name)
        self.assertEqual(add.payload[member.MEMBER_STAMP]["wire"]["reaction"],name)

    def test_forged_server_metadata_is_stripped(self):
        result = ingest_inbound_event(
            source_platform="buzz",
            receipt_key="synthetic-forgery",
            source_channel_id=self.channel.destination_channel_id,
            event_type="create",
            raw_payload={},
            normalized_event={
                "delivery_type": "create",
                "source_message_id": "f" * 64,
                "source_author_id": "1" * 64,
                "text": "hello",
                "metadata": {member.MEMBER_STAMP: {"actor": "forged"}, "safe": True},
            },
        )
        row = CommunityBridgeDelivery.objects.get(pk=result["delivery_id"])
        self.assertEqual(row.payload["metadata"], {"safe": True})

    def echo(self, row, op="create", actor="UHISTORY1", text="hello", **options):
        stamp = row.payload[member.MEMBER_STAMP]["wire"]
        event = {
            "type": "message",
            "channel": "CROOM",
            "channel_type": "channel",
            "ts": f"{self.now}.000001",
            "user": actor,
            "text": text,
            "client_msg_id": stamp["kwargs"].get("client_msg_id", ""),
        }
        if op == "edit":
            event = {
                "type": "message",
                "channel": "CROOM",
                "channel_type": "channel",
                "subtype": "message_changed",
                "event_ts": f"{self.now + 1}.000001",
                "message": event,
            }
        if op == "delete":
            event = {
                "type": "message",
                "channel": "CROOM",
                "channel_type": "channel",
                "subtype": "message_deleted",
                "event_ts": f"{self.now + 2}.000001",
                "deleted_ts": event["ts"],
                "previous_message": event,
            }
        event.update(options)
        return ingest_slack_event(
            {
                "team_id": "TTEST",
                "api_app_id": "AUSER",
                "event_id": str(uuid.uuid4()),
                "event": event,
            },
            public_source_verified=True,
        )

    def test_create_echo_before_ack_after_timeout_failed_is_ignored(self):
        row = self.delivery()
        with patch.object(member, "_finish_slack", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.send(row)
        self.retry(row)
        result = self.echo(row)
        self.assertEqual(result["reason"], "member_slack_echo")
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 1)

    def test_own_unfurl_ignored_different_edit_and_actor_not_echo(self):
        row = self.delivery()
        self.send(row)
        self.assertEqual(self.echo(row, op="edit")["reason"], "member_slack_echo")
        self.assertEqual(
            self.echo(
                row,
                op="edit",
                text="actual manual edit",
                event_ts=f"{self.now + 3}.000001",
            )["status"],
            "enqueued",
        )
        self.assertEqual(
            self.echo(row, actor="UOTHER", ts=f"{self.now + 4}.000001")["status"],
            "enqueued",
        )

    def test_edit_and_delete_echo_match_exact_mutation(self):
        create = self.delivery()
        self.send(create)
        edit = self.delivery("edit")
        edit.payload["text"] = "changed"
        edit.save()
        self.send(edit)
        self.assertEqual(
            self.echo(edit, op="edit", text="changed")["reason"], "member_slack_echo"
        )
        delete = self.delivery("delete")
        self.send(delete)
        self.assertEqual(
            self.echo(delete, op="delete", text="")["reason"], "member_slack_echo"
        )

    def test_lost_parent_remains_parkable_without_side_effects(self):
        row = self.delivery(parent="missing" * 8)
        from integrations.services.community_bridge.worker import ParentMappingPending

        with self.assertRaises(ParentMappingPending):
            self.send(row)
        self.assertEqual(self.mutations, [])

    def test_timeout_after_sdk_entry_requires_exact_read_reconciliation_no_resend(self):
        row = self.delivery()
        accepted = {}

        def ambiguous(authority, conn, method, **kwargs):
            if method == "chat_postMessage":
                kwargs["before_request"]()
                accepted.update(
                    ts=f"{self.now}.000001",
                    user=authority.slack_user_id,
                    client_msg_id=kwargs["client_msg_id"],
                    text=kwargs["text"],
                )
                raise TimeoutError("synthetic accepted-before-timeout")
            return self.transport(authority, conn, method, **kwargs)

        with patch.object(
            dm, "_invoke_slack_with_locked_connection", side_effect=ambiguous
        ):
            with self.assertRaises(TimeoutError):
                self.send(row)
        from integrations.services.community_bridge.member_attempts import ATTEMPT_KEY

        row.refresh_from_db()
        self.assertEqual(row.payload[ATTEMPT_KEY]["status"], "uncertain")
        self.history_messages = [accepted]
        self.retry(row)
        self.send(row)
        self.assertEqual(row.status, "completed")
        self.assertEqual(self.mutations, [])
        self.assertEqual(
            CommunityBridgeMessageLink.objects.get().destination_message_id,
            accepted["ts"],
        )

    def test_ambiguous_missing_history_stays_unresolved_without_another_post(self):
        row = self.delivery()

        def ambiguous(authority, conn, method, **kwargs):
            if method == "chat_postMessage":
                kwargs["before_request"]()
                raise TimeoutError("synthetic ambiguity")
            return self.transport(authority, conn, method, **kwargs)

        with patch.object(
            dm, "_invoke_slack_with_locked_connection", side_effect=ambiguous
        ):
            with self.assertRaises(TimeoutError):
                self.send(row)
        self.retry(row)
        with self.assertRaises(member.MemberDeliveryDeferred):
            self.send(row)
        self.assertEqual(self.mutations, [])
        self.assertFalse(CommunityBridgeMessageLink.objects.exists())

    def test_admission_refusal_is_definitely_unsent_and_can_retry_same_command(self):
        row = self.delivery()

        def refusal(authority, conn, method, **kwargs):
            if method == "chat_postMessage":
                raise BudgetDeferred(1, before_request_method="chat.postMessage")
            return self.transport(authority, conn, method, **kwargs)

        with patch.object(
            dm, "_invoke_slack_with_locked_connection", side_effect=refusal
        ):
            with self.assertRaises(BudgetDeferred):
                self.send(row)
        from integrations.services.community_bridge.member_attempts import ATTEMPT_KEY

        row.refresh_from_db()
        self.assertEqual(row.payload[ATTEMPT_KEY]["status"], "not_sent")
        command = row.payload[member.MEMBER_STAMP]
        self.retry(row)
        self.send(row)
        self.assertEqual(row.status, "completed")
        self.assertEqual(row.payload[member.MEMBER_STAMP], command)
        self.assertEqual(len(self.mutations), 1)

    def test_matching_user_app_markers_are_api_actor_proof(self):
        row = self.delivery()

        def app_post(authority, conn, method, **kwargs):
            response = self.transport(authority, conn, method, **kwargs)
            if method == "chat_postMessage":
                response["message"].update(
                    bot_id="BSYNTHETIC", app_id="AUSER", bot_profile={"app_id": "AUSER"}
                )
            return response

        with patch.object(
            dm, "_invoke_slack_with_locked_connection", side_effect=app_post
        ):
            self.send(row)
        self.assertEqual(row.status, "completed")
        self.assertNotIn("as_user", self.mutations[-1][2])

    def test_wrong_app_or_subject_retains_target_and_never_blindly_resends(self):
        from integrations.services.community_bridge.member_attempts import ATTEMPT_KEY

        row = self.delivery()

        def unproven(authority, conn, method, **kwargs):
            response = self.transport(authority, conn, method, **kwargs)
            if method == "chat_postMessage":
                response["message"].update(
                    bot_id="BSYNTHETIC", bot_profile={"app_id": "AOTHER"}
                )
            return response

        with patch.object(
            dm, "_invoke_slack_with_locked_connection", side_effect=unproven
        ):
            with self.assertRaises(member.MemberDeliveryDeferred):
                self.send(row)
        row.refresh_from_db()
        fact = row.payload[ATTEMPT_KEY]["receipt"]
        self.assertEqual(fact["ts"], f"{self.now}.000001")
        self.assertIs(fact["actor_verified"], False)
        self.retry(row)
        self.mutations.clear()
        with self.assertRaises(member.MemberDeliveryDeferred):
            self.send(row)
        self.assertEqual(self.mutations, [])

    def test_late_evidence_after_expiry_preserved_without_new_lease_settlement(self):
        from integrations.services.community_bridge import member_attempts as attempts

        row = self.delivery()
        with patch.object(
            member,
            "_finish_slack",
            side_effect=LeaseLost("synthetic expiry after response"),
        ):
            with self.assertRaises(LeaseLost):
                self.send(row)
        row.refresh_from_db()
        attempt = row.payload[attempts.ATTEMPT_KEY]
        command = row.payload[member.MEMBER_STAMP]
        old_lease = row.lease_token
        row.lease_expires_at = timezone.now() - timedelta(seconds=1)
        row.save()
        self.assertTrue(
            attempts.record(row.pk, command, attempt, "uncertain", attempt["receipt"])
        )
        row.refresh_from_db()
        self.assertEqual(row.lease_token, old_lease)
        self.assertNotEqual(row.status, "completed")
        self.assertEqual(
            row.payload[attempts.ATTEMPT_KEY]["receipt"], attempt["receipt"]
        )
        # A later result has a different captured attempt. Old evidence becomes
        # a separate bounded conflict proof and cannot overwrite that result.
        newer = {**attempt, "id": str(uuid.uuid4()), "status": "acknowledged"}
        row.payload = {**row.payload, attempts.ATTEMPT_KEY: newer}
        row.save()
        self.assertFalse(
            attempts.record(row.pk, command, attempt, "uncertain", attempt["receipt"])
        )
        row.refresh_from_db()
        self.assertEqual(row.payload[attempts.ATTEMPT_KEY], newer)
        self.assertEqual(row.lease_token, old_lease)
        self.assertEqual(len(row.payload[attempts.CONFLICT_KEY]), 1)

    def test_acknowledged_evidence_is_idempotent_and_conflicts_do_not_replace_it(self):
        from integrations.services.community_bridge import member_attempts as attempts

        row = self.delivery()
        self.send(row)
        attempt = row.payload[attempts.ATTEMPT_KEY]
        command = row.payload[member.MEMBER_STAMP]
        self.assertTrue(
            attempts.record(
                row.pk, command, attempt, "acknowledged", attempt["receipt"]
            )
        )
        self.assertFalse(
            attempts.record(
                row.pk,
                command,
                attempt,
                "acknowledged",
                {**attempt["receipt"], "ts": f"{self.now + 1}.000001"},
            )
        )
        row.refresh_from_db()
        self.assertEqual(row.payload[attempts.ATTEMPT_KEY], attempt)
        self.assertEqual(len(row.payload[attempts.CONFLICT_KEY]), 1)

    def test_read_only_ack_reconciliation_after_ai_withdrawal_does_not_write(self):
        row = self.delivery()
        with patch.object(member, "_finish_slack", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.send(row)
        self.retry(row)
        self.mutations.clear()
        with (
            override_settings(COMMUNITY_CHAT_AI_CONSENT_REQUIRED=True),
            patch.object(member, "has_ai_consent", return_value=False),
        ):
            self.send(row)
        self.assertEqual(row.status, "completed")
        self.assertEqual(self.mutations, [])

    def test_changed_consent_does_not_refresh_pinned_actor_credentials(self):
        row = self.delivery()
        self.send(row)
        self.retry(row)
        captured = row.payload[member.MEMBER_STAMP]
        self.a.consented_at = timezone.now()
        self.a.save()
        with patch.object(dm, "_refresh_slack_grant_token_if_due") as refresh:
            with self.assertRaises(member.MemberDeliveryUnavailable):
                self.send(row)
        refresh.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.payload[member.MEMBER_STAMP], captured)

    def test_revoked_pinned_signer_does_not_refresh_credentials(self):
        row = self.delivery()
        self.send(row)
        self.retry(row)
        CommunityChatDevice.objects.filter(user=self.a.user).update(
            revoked_at=timezone.now(), status="revoked"
        )
        with patch.object(dm, "_refresh_slack_grant_token_if_due") as refresh:
            with self.assertRaises(member.MemberDeliveryUnavailable):
                self.send(row)
        refresh.assert_not_called()

    def test_reverse_native_mutations_terminate_without_parent_retry_or_send(self):
        for source, source_id, destination in (
            ("buzz", "f" * 64, f"{self.now}.000001"),
            ("slack", f"{self.now + 1}.000001", "e" * 64),
        ):
            with self.subTest(source=source):
                CommunityBridgeMessageLink.objects.create(
                    channel=self.channel,
                    source_platform="slack" if source == "buzz" else "buzz",
                    source_channel_id="CROOM"
                    if source == "buzz"
                    else self.channel.destination_channel_id,
                    source_message_id=destination,
                    destination_platform=source,
                    destination_channel_id=self.channel.destination_channel_id
                    if source == "buzz"
                    else "CROOM",
                    destination_message_id=source_id,
                )
                row = self.delivery("edit", source=source, source_id=source_id)
                with self.assertRaisesMessage(
                    member.MemberDeliveryUnavailable,
                    "reverse_origin_mutation_unsupported",
                ):
                    self.send(row)
                row.refresh_from_db()
                row.status, row.lease_token, row.lease_expires_at = "dead", None, None
                row.save()
        self.assertEqual(self.mutations, [])

    def fresh_after_reconsent(self, operation):
        self.send(self.delivery())
        source_id = "a" * 64
        if operation == "reaction_remove":
            source_id = "b" * 64
            self.native_reaction("reaction_add", source_id)
        link = CommunityBridgeMessageLink.objects.get(
            source_platform="buzz", source_message_id=source_id
        )
        original = link.destination_payload["poster"]
        self.a.consented_at = timezone.now() - timedelta(seconds=1)
        self.a.save()
        self.a.public_channel_memberships.update(observed_at=timezone.now())
        self.ack()  # the real reconnect flow requires fresh membership/full ACK
        row = self.delivery(operation, source_id=source_id)
        row.payload["text"] = "fresh edit" if operation == "edit" else "👍"
        row.save()
        with (
            override_settings(COMMUNITY_CHAT_AI_CONSENT_REQUIRED=operation != "edit"),
            patch.object(member, "has_ai_consent", return_value=False),
        ):
            self.send(row)
        current = row.payload[member.MEMBER_STAMP]["actor"]
        self.assertNotEqual(
            current["consent_generation"], original["consent_generation"]
        )
        self.assertEqual(member._poster_owner(current), member._poster_owner(original))
        link.refresh_from_db()
        self.assertEqual(link.destination_payload["poster"], original)
        self.assertEqual(row.status, "completed")
        self.assertEqual(self.mutations[-1][0], self.a.pk)
        self.assertEqual(self.mutations[-1][1], member.WRITES[operation])
        return row

    def test_fresh_edit_after_same_account_reconsent_retains_original_ownership(self):
        self.fresh_after_reconsent("edit")

    def test_fresh_delete_after_reconsent_is_cleanup_with_ai_off(self):
        self.fresh_after_reconsent("delete")

    def test_fresh_reaction_cleanup_after_same_account_reconsent(self):
        self.fresh_after_reconsent("reaction_remove")

    def test_fresh_cleanup_cannot_borrow_another_accounts_verified_device(self):
        self.send(self.delivery())
        row = self.delivery("delete", author="2" * 64)
        with patch.object(dm, "_refresh_slack_grant_token_if_due") as refresh:
            with self.assertRaises(member.MemberDeliveryUnavailable):
                self.send(row)
        refresh.assert_not_called()
        self.assertEqual([call[1] for call in self.mutations], ["chat_postMessage"])

    def test_fresh_cleanup_cannot_rebind_original_account_connection(self):
        self.send(self.delivery())
        replacement = ExternalServiceConnection.objects.create(
            user=self.a.user,
            provider="slack",
            status="connected",
            external_account_id="TTEST",
            access_token="synthetic-unused-replacement",
            scopes=list(self.a.connection.scopes),
            provider_metadata={
                "team": {"id": "TTEST"},
                "authed_user": {"id": self.a.slack_user_id},
            },
        )
        self.a.connection = replacement
        self.a.save()
        row = self.delivery("delete")
        with patch.object(dm, "_refresh_slack_grant_token_if_due") as refresh:
            with self.assertRaisesMessage(
                member.MemberDeliveryUnavailable, "original_member_poster_changed"
            ):
                self.send(row)
        refresh.assert_not_called()
        self.assertEqual([call[1] for call in self.mutations], ["chat_postMessage"])

    def test_member_alias_add_remove_readd_uses_exact_current_imported_reaction(self):
        self.send(self.delivery())
        source_id = None
        for kind, revision, spelling, event_id in (
            ("reaction_added", 10, "+1", "d" * 64),
            ("reaction_removed", 20, "thumbsup", None),
            ("reaction_added", 30, "thumbsup", "e" * 64),
        ):
            result = self.reaction_callback(kind, revision, reaction=spelling)
            row = CommunityBridgeDelivery.objects.get(pk=result["delivery_id"])
            source_id = source_id or row.source_message_id
            self.assertEqual(row.source_message_id, source_id)
            response = {
                "channel_id": self.channel.destination_channel_id,
                "message_id": event_id or "d" * 64,
                "parent_message_id": "a" * 64,
            }
            with patch.object(
                member.BuzzBridgeClient, "deliver", return_value=response
            ) as adapter:
                self.send(row)
            link = CommunityBridgeMessageLink.objects.get(
                source_platform="slack", source_message_id=source_id
            )
            if kind == "reaction_removed":
                self.assertEqual(
                    adapter.call_args.kwargs["target_message_id"], "d" * 64
                )
                self.assertIsNotNone(link.source_deleted_at)
            else:
                self.assertEqual(link.destination_message_id, event_id)
                self.assertIsNone(link.source_deleted_at)
                self.assertIsNone(link.destination_deleted_at)
        self.assertEqual(
            CommunityBridgeMessageLink.objects.filter(source_platform="slack").count(),
            1,
        )

    def test_community_reaction_source_ids_keep_existing_raw_spelling(self):
        from integrations.services.community_bridge.formatting import reaction_object_id

        self.channel.audience = "community"
        self.channel.save()
        ids = []
        for kind, revision, spelling in (
            ("reaction_added", 10, "+1"),
            ("reaction_removed", 20, "thumbsup"),
        ):
            result = self.reaction_callback(kind, revision, reaction=spelling)
            row = CommunityBridgeDelivery.objects.get(pk=result["delivery_id"])
            self.assertEqual(
                row.source_message_id,
                reaction_object_id(
                    message_id=f"{self.now}.000001",
                    reaction=spelling,
                    author_id="UHISTORY1",
                ),
            )
            ids.append(row.source_message_id)
        self.assertNotEqual(*ids)

    def test_old_completed_edit_does_not_hide_manual_return_to_older_text(self):
        self.send(self.delivery())
        for text in ("edit A", "edit B"):
            row = self.delivery("edit")
            row.payload["text"] = text
            row.save()
            self.send(row)
        result = self.echo(
            row, op="edit", text="edit A", event_ts=f"{self.now + 30}.000001"
        )
        self.assertEqual(result["status"], "enqueued")
        self.assertEqual(
            CommunityBridgeDelivery.objects.get(pk=result["delivery_id"]).payload[
                "text"
            ],
            "edit A",
        )

    def test_newer_echo_receipt_preserves_current_native_add_against_older_remove(self):
        self.send(self.delivery())
        reaction = self.native_reaction("reaction_add", "b" * 64)
        link = CommunityBridgeMessageLink.objects.get(
            source_platform="buzz", source_message_id=reaction.source_message_id
        )
        destination_id = link.destination_message_id
        newer = self.reaction_callback("reaction_added", 20)
        self.assertEqual(newer["reason"], "member_slack_echo")
        old = self.reaction_callback("reaction_removed", 10)
        self.assertEqual(old["reason"], "superseded_member_reaction")
        self.assertEqual(
            self.reaction_callback("reaction_added", 20)["status"], "duplicate"
        )
        link.refresh_from_db()
        self.assertIsNone(link.source_deleted_at)
        self.assertIsNone(link.destination_deleted_at)
        self.assertEqual(link.destination_message_id, destination_id)
        self.assertFalse(
            CommunityBridgeDelivery.objects.filter(source_platform="slack").exists()
        )
        # The existing native effect remains current; no new imported body is
        # claimed from the ignored newer observation and no removal is sent.

    def test_queued_old_reaction_is_rechecked_before_adapter_effect(self):
        self.send(self.delivery())
        added = self.reaction_callback("reaction_added", 10)
        removed = self.reaction_callback("reaction_removed", 20)
        self.assertEqual(added["status"], "enqueued")
        self.assertEqual(removed["status"], "enqueued")
        row = CommunityBridgeDelivery.objects.get(pk=added["delivery_id"])
        with patch.object(member.BuzzBridgeClient, "deliver") as adapter:
            self.send(row)
        adapter.assert_not_called()
        self.assertEqual(row.status, "completed")

    def reaction_callback(self, kind, revision, reaction="thumbsup"):
        return ingest_slack_event(
            {
                "team_id": "TTEST",
                "api_app_id": "AUSER",
                "event_id": str(uuid.uuid4()),
                "event": {
                    "type": kind,
                    "user": "UHISTORY1",
                    "event_ts": f"{self.now + revision}.000001",
                    "reaction": reaction,
                    "item": {
                        "type": "message",
                        "channel": "CROOM",
                        "ts": f"{self.now}.000001",
                    },
                },
            },
            public_source_verified=True,
        )

    def native_reaction(self, operation, source_id):
        row = self.delivery(operation, source_id=source_id, parent="a" * 64)
        row.payload["text"] = "👍"
        row.save()
        self.send(row)
        return row

    def test_native_add_remove_then_manual_slack_readd_is_not_old_add_echo(self):
        self.send(self.delivery())
        self.native_reaction("reaction_add", "b" * 64)
        self.native_reaction("reaction_remove", "b" * 64)
        self.assertEqual(
            self.reaction_callback("reaction_added", 10)["status"], "enqueued"
        )

    def test_old_remove_cannot_hide_manual_removal_after_new_native_add(self):
        self.send(self.delivery())
        self.native_reaction("reaction_add", "b" * 64)
        self.native_reaction("reaction_remove", "b" * 64)
        self.native_reaction("reaction_add", "c" * 64)
        self.assertEqual(
            self.reaction_callback("reaction_removed", 12)["status"], "enqueued"
        )

    def test_manual_remove_then_manual_readd_invalidates_completed_add_fingerprint(
        self,
    ):
        self.send(self.delivery())
        self.native_reaction("reaction_add", "b" * 64)
        self.assertEqual(
            self.reaction_callback("reaction_removed", 10)["status"], "enqueued"
        )
        self.assertEqual(
            self.reaction_callback("reaction_added", 11)["status"], "enqueued"
        )

    def test_late_old_callback_cannot_remove_newer_imported_readd(self):
        from integrations.services.community_bridge.store import (
            complete_create_delivery,
        )

        self.send(self.delivery())
        newer = self.reaction_callback("reaction_added", 20)
        complete_create_delivery(
            delivery_id=newer["delivery_id"],
            destination_message_id="d" * 64,
            destination_channel_id=self.channel.destination_channel_id,
            destination_parent_message_id="a" * 64,
        )
        old = self.reaction_callback("reaction_removed", 10)
        self.assertEqual(old["reason"], "superseded_member_reaction")
        self.assertEqual(claim_ready_deliveries(1), [])
        link = CommunityBridgeMessageLink.objects.get(
            source_platform="slack",
            source_message_id=CommunityBridgeDelivery.objects.get(
                pk=newer["delivery_id"]
            ).source_message_id,
        )
        self.assertIsNone(link.source_deleted_at)
        self.assertEqual(link.destination_message_id, "d" * 64)

    def test_ai_off_never_mutates_not_sent_or_unresolved_content_attempts(self):
        from integrations.services.community_bridge import member_attempts as attempts

        for operation in ("create", "edit", "reaction_add"):
            for status in ("not_sent", "uncertain"):
                with self.subTest(operation=operation, status=status):
                    # Independent semantic objects avoid outbox/receipt reuse.
                    CommunityBridgeDelivery.objects.all().delete()
                    CommunityBridgeMessageLink.objects.all().delete()
                    root = self.delivery()
                    self.send(root)
                    if operation == "create":
                        row = self.delivery(source_id="c" * 64)
                    elif operation == "edit":
                        row = self.delivery("edit")
                    else:
                        row = self.delivery(
                            "reaction_add", source_id="b" * 64, parent="a" * 64
                        )
                        row.payload["text"] = "👍"
                        row.save()
                    claimed = claim_ready_deliveries(1)[0]
                    with delivery_context(claimed):
                        original, _, saved = member._stamp_identity(claimed)
                        authority, actor = member._choose(claimed, original, saved)
                        command = member._prepare(
                            claimed, original, authority, actor, saved
                        )
                        attempt, _ = attempts.arm(claimed, original, command)
                    attempts.record(
                        row.pk, command, attempt, status, {"malformed": "receipt"}
                    )
                    self.retry(row)
                    self.mutations.clear()
                    self.history_messages = []
                    from integrations.services.community_bridge.ai_consent import (
                        PublicBridgeConsentRequired,
                    )

                    with (
                        override_settings(COMMUNITY_CHAT_AI_CONSENT_REQUIRED=True),
                        patch.object(member, "has_ai_consent", return_value=False),
                    ):
                        with self.assertRaises(
                            (PublicBridgeConsentRequired, member.MemberDeliveryDeferred)
                        ):
                            self.send(row)
                    self.assertEqual(self.mutations, [])

    def test_prepared_but_never_armed_command_is_not_echo_evidence(self):
        row = self.delivery()
        claimed = claim_ready_deliveries(1)[0]
        with delivery_context(claimed):
            original, _, saved = member._stamp_identity(claimed)
            authority, actor = member._choose(claimed, original, saved)
            member._prepare(claimed, original, authority, actor, saved)
        row.refresh_from_db()
        self.assertEqual(self.echo(row)["status"], "enqueued")

    def test_unproven_broadcast_metadata_is_silent_and_external_alias_is_omitted(self):
        row = self.delivery(source="slack", author="UUNLINKED")
        row.payload["metadata"]["broadcast"] = "truthy forged flag"
        row.save()
        result = {
            "channel_id": self.channel.destination_channel_id,
            "message_id": "d" * 64,
            "parent_message_id": "",
        }
        with patch.object(
            member.BuzzBridgeClient, "deliver", return_value=result
        ) as adapter:
            self.send(row)
        self.assertIs(adapter.call_args.kwargs["broadcast"], False)
        self.assertEqual(adapter.call_args.kwargs["linked_pubkey"], "")

    def test_stale_community_claim_uses_current_members_path(self):
        row = self.delivery()
        claimed = claim_ready_deliveries(1)[0]
        claimed["channel"]["audience"] = "community"
        self.assertTrue(member.required(claimed))
        with delivery_context(claimed):
            member.execute(claimed)
        self.bot.assert_not_called()

    def test_stale_member_claim_cannot_become_legacy_bot(self):
        row = self.delivery()
        claimed = claim_ready_deliveries(1)[0]
        self.channel.audience = "community"
        self.channel.save()
        self.assertTrue(member.required(claimed))
        with (
            delivery_context(claimed),
            self.assertRaises(member.MemberDeliveryUnavailable),
        ):
            member.execute(claimed)
        self.assertEqual(self.calls, [])

    def test_legacy_boundary_rechecks_members_and_disabled_after_claim(self):
        row = self.delivery()
        claimed = claim_ready_deliveries(1)[0]
        claimed["channel"]["audience"] = "community"
        send = __import__("unittest.mock", fromlist=["Mock"]).Mock()
        with (
            delivery_context(claimed),
            self.assertRaises(member.MemberDeliveryDeferred),
        ):
            member.legacy_send(claimed, send)
        send.assert_not_called()
        self.channel.audience = "community"
        self.channel.enabled = False
        self.channel.save()
        with (
            delivery_context(claimed),
            self.assertRaises(member.MemberDeliveryDeferred),
        ):
            member.legacy_send(claimed, send)
        send.assert_not_called()

    def test_legacy_boundary_postquota_context_is_reset_and_expired_request_denied(
        self,
    ):
        from integrations.services.message_sync.delivery import (
            guard_legacy_provider_request,
        )

        row = self.delivery(source="slack")
        self.channel.audience = "community"
        self.channel.save()
        claimed = claim_ready_deliveries(1)[0]

        def admitted():
            with patch.object(
                member.timezone,
                "now",
                return_value=timezone.now() + timedelta(minutes=5),
            ):
                guard_legacy_provider_request()
            self.fail("expired request reached provider")

        with delivery_context(claimed), self.assertRaises(LeaseLost):
            member.legacy_send(claimed, admitted)
        guard_legacy_provider_request()  # no operation callback survives failure

    def test_reaction_echo_matches_reactor_target_emoji_and_remove_revision(self):
        create = self.delivery()
        self.send(create)
        add = self.delivery(
            "reaction_add",
            source_id="b" * 64,
            author="2" * 64,
            parent=create.source_message_id,
        )
        add.payload["text"] = "👍"
        add.save()
        self.send(add)
        raw = {
            "team_id": "TTEST",
            "api_app_id": "AUSER",
            "event_id": "reaction-echo",
            "event": {
                "type": "reaction_added",
                "user": "UHISTORY2",
                "event_ts": f"{self.now + 1}.000001",
                "reaction": "thumbsup",
                "item": {
                    "type": "message",
                    "channel": "CROOM",
                    "ts": f"{self.now}.000001",
                },
            },
        }
        self.assertEqual(
            ingest_slack_event(raw, public_source_verified=True)["reason"],
            "member_slack_echo",
        )
        raw["event_id"] = "other-reactor"
        raw["event"]["user"] = "UOTHER"
        self.assertEqual(
            ingest_slack_event(raw, public_source_verified=True)["status"], "enqueued"
        )

    def test_old_reaction_remove_never_erases_new_same_actor_add(self):
        create = self.delivery()
        self.send(create)
        add = self.delivery(
            "reaction_add", source_id="b" * 64, parent=create.source_message_id
        )
        add.payload["text"] = "👍"
        add.save()
        self.send(add)
        remove = self.delivery(
            "reaction_remove",
            source_id=add.source_message_id,
            parent=create.source_message_id,
        )
        remove.payload["text"] = "👍"
        remove.save()
        claimed = claim_ready_deliveries(1)[0]
        # A newer successful add can exist after an older ambiguous removal
        # gave up its lease. It must remain visible, never be removed by retry.
        link = CommunityBridgeMessageLink.objects.get(
            source_message_id=add.source_message_id
        )
        link.pk = None
        link.source_message_id = "c" * 64
        link.destination_message_id = "reaction:new"
        link.save()
        self.mutations.clear()
        with (
            delivery_context(claimed),
            self.assertRaises(member.MemberDeliveryUnavailable),
        ):
            member.execute(claimed)
        self.assertEqual(self.mutations, [])


@tag("postgres-only")
@override_settings(**SETTINGS, COMMUNITY_CHAT_AI_CONSENT_REQUIRED=False)
class MemberDeliveryConcurrencyTests(MemberDeliveryFixture, TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_direct_device_revoke_waits_through_mutation_and_settlement(self):
        self._interleave("device")

    @skipUnlessDBFeature("has_select_for_update")
    def test_source_reduction_waits_for_current_send_channel_fence(self):
        self._interleave("source")

    @skipUnlessDBFeature("has_select_for_update")
    def test_legacy_negative_guard_serializes_conversion_through_call(self):
        self.channel.audience = "community"
        self.channel.save()
        self._interleave("legacy")

    def _interleave(self, kind):
        row = self.delivery(source="slack" if kind == "legacy" else "buzz")
        claimed = claim_ready_deliveries(1)[0]
        entered, release, attempting, changed = [threading.Event() for _ in range(4)]
        errors = []

        def slow(authority, conn, method, **kwargs):
            if method == "chat_postMessage":
                entered.set()
                if not release.wait(5):
                    raise AssertionError("test release timeout")
            return self.transport(authority, conn, method, **kwargs)

        def send():
            close_old_connections()
            try:
                with delivery_context(claimed):
                    if kind == "legacy":
                        member.legacy_send(
                            claimed, lambda: (entered.set(), release.wait(5))
                        )
                    else:
                        with patch.object(
                            dm, "_invoke_slack_with_locked_connection", side_effect=slow
                        ):
                            member.execute(claimed)
            except BaseException as error:
                errors.append(error)
            finally:
                close_old_connections()

        def reduce():
            close_old_connections()
            try:
                attempting.set()
                with transaction.atomic():
                    if kind == "device":
                        CommunityChatDevice.objects.select_for_update().filter(
                            user_id=self.a.user_id
                        ).update(status="revoked", revoked_at=timezone.now())
                    else:
                        CommunityBridgeChannel = type(self.channel)
                        CommunityBridgeChannel.objects.select_for_update().filter(
                            pk=self.channel.pk
                        ).update(
                            audience="members" if kind == "legacy" else "members",
                            enabled=False,
                        )
                changed.set()
            except BaseException as error:
                errors.append(error)
            finally:
                close_old_connections()

        sender = threading.Thread(target=send)
        sender.start()
        self.assertTrue(entered.wait(8))
        reducer = threading.Thread(target=reduce)
        reducer.start()
        self.assertTrue(attempting.wait(2))
        self.assertFalse(changed.wait(0.2))
        release.set()
        sender.join(8)
        reducer.join(8)
        self.assertFalse(sender.is_alive())
        self.assertFalse(reducer.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(changed.is_set())
        if kind != "legacy":
            row.refresh_from_db()
            self.assertEqual(row.status, "completed")


class MemberScopeTests(SimpleTestCase):
    def test_scope_closed_schema_and_canonical_identifiers(self):
        from integrations.services.community_bridge.buzz import (
            validate_member_scope,
            BuzzBridgeError,
        )

        scope = {
            "channel_id": str(uuid.uuid4()),
            "slack_workspace_id": "TTEST",
            "slack_channel_id": "CROOM",
            "audience": "members",
            "expected_generation": str(uuid.uuid4()),
            "expected_audience_hash": "a" * 64,
        }
        self.assertEqual(validate_member_scope(scope), scope)
        for value in (
            {**scope, "extra": True},
            {**scope, "expected_generation": str(uuid.UUID(int=0))},
            {**scope, "expected_audience_hash": "A" * 64},
            {**scope, "audience": "community"},
        ):
            with self.assertRaises(BuzzBridgeError):
                validate_member_scope(value)

    def test_canonical_transport_text_handles_entities_links_but_not_different_text(
        self,
    ):
        self.assertEqual(
            member.text_hash("&amp; <https://example.invalid>"),
            member.text_hash("& https://example.invalid"),
        )
        self.assertNotEqual(member.text_hash("real edit"), member.text_hash("old"))
        self.assertNotEqual(
            member.text_hash("<https://example.invalid|old>"),
            member.text_hash("<https://example.invalid|new>"),
        )
        self.assertEqual(member.text_hash("👍"), member.text_hash(":thumbsup:"))


@override_settings(MESSAGE_SYNC_SLACK_USER_APP_ID="AUSER")
class MemberApiActorTests(SimpleTestCase):
    def test_only_current_subject_and_unambiguous_configured_app_markers_pass(self):
        from integrations.services.community_bridge.member_attempts import actor_proven

        actor = {"slack_user_id": "USYNTHETIC"}
        ordinary = {"user": "USYNTHETIC"}
        marked = {
            **ordinary,
            "bot_id": "BSYNTHETIC",
            "app_id": "AUSER",
            "bot_profile": {"app_id": "AUSER"},
        }
        self.assertTrue(actor_proven(ordinary, actor))
        self.assertTrue(actor_proven(marked, actor))
        for invalid in (
            {**marked, "user": "UOTHER"},
            {**marked, "app_id": "AOTHER"},
            {**ordinary, "bot_id": "BSYNTHETIC"},
            {**marked, "bot_profile": {}},
            {**marked, "bot_profile": {"app_id": "AOTHER"}},
            {**marked, "app_id": None},
            {**marked, "bot_profile": "malformed"},
        ):
            with self.subTest(shape=sorted(invalid)):
                self.assertFalse(actor_proven(invalid, actor))
        self.assertFalse(actor_proven(marked, actor, {"app_id": "AOTHER"}))
        for response in (
            {"bot_profile": {}},
            {"bot_profile": {"app_id": "AOTHER"}},
            {"bot_id": "BSYNTHETIC"},
            {"bot_profile": "malformed"},
            {"bot_id": ["malformed"]},
            {"app_id": None},
        ):
            self.assertFalse(actor_proven(ordinary, actor, response))
        self.assertTrue(
            actor_proven(
                ordinary,
                actor,
                {"bot_id": "BSYNTHETIC", "bot_profile": {"app_id": "AUSER"}},
            )
        )

        class WrappedResponse:
            data = {"app_id": None, "bot_profile": {"app_id": "AUSER"}}

        self.assertFalse(actor_proven(ordinary, actor, WrappedResponse()))

    def test_metadata_recursion_and_node_bounds_reject_without_authority(self):
        nested = {}
        cursor = nested
        for _ in range(18):
            cursor["nested"] = {}
            cursor = cursor["nested"]
        with self.assertRaises(ValueError):
            member.clean_metadata(nested)
        with self.assertRaises(ValueError):
            member.clean_metadata({"values": list(range(2100))})
        self.assertEqual(
            member.clean_metadata(
                {"nested": {member.MEMBER_STAMP: {"actor": "forged"}, "safe": True}}
            ),
            {"nested": {"safe": True}},
        )


@override_settings(MESSAGE_SYNC_SLACK_USER_APP_ID="AUSER")
class MemberReconciliationTests(SimpleTestCase):
    def fixture(self, operation="create"):
        from types import SimpleNamespace

        actor = {"slack_user_id": "USYNTHETIC"}
        wire = {
            "kwargs": {
                "channel": "CROOM",
                "client_msg_id": str(uuid.uuid4()),
                "timestamp": "123.456",
            },
            "text_hash": member.text_hash("hello"),
            "reaction": "thumbsup",
        }
        fact = {
            "version": 1,
            "channel": "CROOM",
            "ts": "123.456",
            "operation": operation,
            "actor_verified": True,
            "outcome": "accepted",
            "text_hash": wire["text_hash"],
        }
        if operation == "create":
            fact["client_msg_id"] = wire["kwargs"]["client_msg_id"]
        row, channel = (
            SimpleNamespace(delivery_type=operation),
            SimpleNamespace(slack_channel_id="CROOM"),
        )
        return actor, wire, fact, row, channel

    def test_cached_proof_is_closed_typed_and_operation_specific(self):
        from integrations.services.community_bridge import member_attempts as attempts

        actor, wire, fact, _, _ = self.fixture()
        self.assertIsNotNone(
            attempts.response_from_receipt(fact, actor, wire, "create")
        )
        for invalid in (
            {**fact, "version": True},
            {**fact, "outcome": "already_reacted"},
            {**fact, "ts": 123.456},
            {**fact, "extra": True},
            {**fact, "actor_verified": 1},
            {**fact, "outcome": {}},
            {key: value for key, value in fact.items() if key != "outcome"},
        ):
            with self.subTest(shape=sorted(invalid)):
                self.assertIsNone(
                    attempts.response_from_receipt(invalid, actor, wire, "create")
                )

    def reconcile(self, response, operation="create"):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from integrations.services.community_bridge import member_attempts as attempts

        actor, wire, _, row, channel = self.fixture(operation)
        for message in response.get("messages", []):
            if isinstance(message, dict) and message.get("client_msg_id") == "REPLACE":
                message["client_msg_id"] = wire["kwargs"]["client_msg_id"]
        client = SimpleNamespace(call=Mock(return_value=response), verify=Mock())
        result = attempts.reconcile(
            client,
            row,
            channel,
            actor,
            wire,
            {"started_at": timezone.now().isoformat()},
        )
        client.verify.assert_called_once()
        return result

    def test_wrong_or_malformed_returned_channel_cannot_prove_completion(self):
        message = {
            "user": "USYNTHETIC",
            "text": "hello",
            "ts": "123.456",
            "client_msg_id": "REPLACE",
        }
        for channel in ("COTHER", None, {}, [], {"id": "COTHER"}):
            with (
                self.subTest(channel=channel),
                self.assertRaises(member.MemberDeliveryDeferred),
            ):
                self.reconcile(
                    {"ok": True, "channel": channel, "messages": [dict(message)]}
                )
        for channel in ("CROOM", {"id": "CROOM"}):
            self.assertEqual(
                self.reconcile(
                    {"ok": True, "channel": channel, "messages": [dict(message)]}
                )[1],
                "accepted",
            )

    def test_non_string_message_text_is_not_coerced_into_proof(self):
        for value in ({"hello": True}, ["hello"], None, 7):
            with (
                self.subTest(value=value),
                self.assertRaises(member.MemberDeliveryDeferred),
            ):
                self.reconcile(
                    {
                        "ok": True,
                        "messages": [
                            {
                                "user": "USYNTHETIC",
                                "text": value,
                                "ts": "123.456",
                                "client_msg_id": "REPLACE",
                            }
                        ],
                    }
                )

    def test_reaction_alias_can_reconcile_exact_same_effect(self):
        result = self.reconcile(
            {
                "ok": True,
                "messages": [
                    {
                        "ts": "123.456",
                        "reactions": [{"name": "+1", "users": ["USYNTHETIC"]}],
                    }
                ],
            },
            "reaction_add",
        )
        self.assertEqual(result[1], "accepted")
