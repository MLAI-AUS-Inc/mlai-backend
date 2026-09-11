"""Database-free consent and resumable-history regressions; no migration runner."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from community_chat import slack_views
from integrations.services import slack_dm_mirror as mirror
from integrations.services.slack_chat_catalog import (
    ALL_HISTORY_CONSENT,
    PRIVATE_CHANNEL_CONSENT,
    PRIVATE_CHANNEL_SCOPES,
    private_channels_enabled,
)


@override_settings(SLACK_DM_MIRROR_HISTORY_DAYS=30)
class SlackAllHistoryTests(SimpleTestCase):
    def grant(self, days=0, consent=ALL_HISTORY_CONSENT):
        return SimpleNamespace(
            pk=1,
            history_days=days,
            consent_version=consent,
            connection=SimpleNamespace(scopes=list(PRIVATE_CHANNEL_SCOPES)),
        )

    def conversation(self, *, days=0, latest=""):
        return SimpleNamespace(
            pk=2,
            grant=self.grant(days),
            slack_conversation_id="DPRIVATE",
            latest_synced_ts=latest,
            oldest_synced_ts="123.000001",
            history_backfilled_at=None,
            last_error="",
            save=MagicMock(),
        )

    def authority(self, oldest=""):
        return mirror._SlackHistoryScanAuthority(
            epoch="epoch",
            participant_hash="participants",
            mlai_channel_id="channel",
            registration_id="registration",
            registration_generation="generation",
            history_days=0,
            oldest=oldest,
        )

    def test_all_is_an_explicit_numeric_choice(self):
        self.assertEqual(slack_views._import_history_days({}), 30)
        self.assertEqual(slack_views._import_history_days({"history_days": 0}), 0)
        for value in (False, "0", None, -1, 365):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                slack_views._import_history_days({"history_days": value})

    def test_legacy_zero_does_not_silently_widen_consent(self):
        self.assertEqual(
            mirror._grant_history_days(self.grant(consent=PRIVATE_CHANNEL_CONSENT)), 30
        )
        self.assertEqual(mirror._bounded_history_days(0), 30)
        self.assertEqual(mirror._grant_history_days(self.grant()), 0)
        for days in (7, 30):
            self.assertEqual(mirror._grant_history_days(self.grant(days)), days)

    def test_all_consent_includes_private_channels_but_still_requires_scopes(self):
        grant = self.grant()
        self.assertTrue(private_channels_enabled(grant))
        grant.connection.scopes = []
        self.assertFalse(private_channels_enabled(grant))

    def test_initial_all_history_scan_is_unbounded(self):
        self.assertEqual(
            mirror._reconciliation_oldest(self.conversation(), recent_only=False), ""
        )

    @patch.object(mirror.time, "time", return_value=10_000_000)
    def test_refresh_only_scans_recent_window_after_archive_import(self, _clock):
        self.assertEqual(
            mirror._reconciliation_oldest(
                self.conversation(latest="9999999.000001"), recent_only=True
            ),
            str(10_000_000 - 30 * 86400),
        )

    @patch.object(mirror.time, "time", return_value=10_000_000)
    def test_refresh_covers_gap_after_a_long_offline_period(self, _clock):
        self.assertEqual(
            mirror._reconciliation_oldest(
                self.conversation(latest="1000000.000001"), recent_only=True
            ),
            str(1_000_000 - 86400),
        )

    @patch.object(mirror.time, "time", return_value=10_000_000)
    def test_bounded_consent_stays_bounded_after_long_offline_period(self, _clock):
        self.assertEqual(
            mirror._reconciliation_oldest(
                self.conversation(days=7, latest="1000000.000001"), recent_only=True
            ),
            str(10_000_000 - 7 * 86400),
        )

    @patch.object(mirror, "_clear_history_scan_states")
    def test_refresh_does_not_reset_partial_archive_cursor(self, clear):
        conversation = self.conversation()
        mirror._mark_conversation_history_due(
            conversation,
            reason="App rate limited",
            reset_deliveries=False,
            reconcile_current_state=True,
        )
        clear.assert_not_called()
        conversation.save.assert_not_called()
        self.assertEqual(conversation.oldest_synced_ts, "123.000001")

    @patch.object(mirror, "_mark_history_reconciliation_candidates_locked")
    @patch.object(mirror, "_clear_history_scan_states")
    def test_completed_archive_refresh_uses_recent_reconciliation(
        self, clear, candidates
    ):
        conversation = self.conversation()
        conversation.history_backfilled_at = timezone.now() - timedelta(hours=1)
        mirror._mark_conversation_history_due(
            conversation,
            reason="Opened",
            reset_deliveries=False,
            reconcile_current_state=True,
        )
        clear.assert_called_once_with([conversation.pk])
        candidates.assert_called_once_with(conversation, recent_only=True)

    @patch.object(mirror, "_history_state")
    @patch.object(mirror.SlackDmMirrorDelivery, "objects")
    def test_empty_conversation_keeps_saved_refresh_boundary(self, rows, state):
        rows.select_for_update.return_value.filter.return_value.values_list.return_value = (
            []
        )
        state.return_value = SimpleNamespace(
            metadata={
                mirror.HISTORY_RECONCILE_EPOCH_KEY: "epoch",
                mirror.HISTORY_RECONCILE_OLDEST_KEY: "7000000",
            }
        )
        self.assertEqual(
            mirror._history_reconciliation_boundary_locked(self.conversation()),
            ("epoch", "7000000"),
        )

    @patch.object(mirror, "_ensure_history_state")
    @patch.object(mirror.SlackDmMirrorDelivery, "objects")
    def test_reconcile_boundary_is_persisted_even_without_messages(self, rows, ensure):
        rows.select_for_update.return_value.filter.return_value = []
        conversation = self.conversation()
        mirror._mark_history_reconciliation_candidates_locked(
            conversation, recent_only=True
        )
        ensure.assert_called_once()
        self.assertEqual(
            ensure.call_args.kwargs["source_message_id"],
            mirror.HISTORY_RECONCILIATION_STATE_ID,
        )
        self.assertTrue(
            ensure.call_args.kwargs["metadata"][
                mirror.HISTORY_RECONCILE_OLDEST_KEY
            ].isdigit()
        )

    def history_request(self, *, oldest="", cursor=""):
        conversation = self.conversation()
        with patch.object(mirror, "_capture_slack_grant_api_authority"), patch.object(
            mirror, "conversation_kind", return_value="im"
        ), patch.object(
            mirror,
            "_prepare_history_scan_page",
            return_value=(self.authority(oldest), "DPRIVATE", cursor, None, False),
        ), patch.object(
            mirror, "_call_slack_with_grant_authority"
        ) as slack, patch.object(
            mirror, "_persist_history_page", return_value=1
        ):
            self.assertEqual(
                mirror._enqueue_history_page(conversation, conversation.grant), 1
            )
        return slack.call_args.kwargs

    def test_initial_all_page_has_no_oldest_cutoff(self):
        self.assertNotIn("oldest", self.history_request())

    def test_resumed_all_page_uses_durable_timestamp_cursor(self):
        request = self.history_request(cursor="123.000001")
        self.assertEqual(request["latest"], "123.000001")
        self.assertFalse(request["inclusive"])
        self.assertNotIn("oldest", request)

    def test_all_grant_refresh_still_sends_the_recent_cutoff(self):
        request = self.history_request(oldest="7000000")
        self.assertEqual(request["oldest"], "7000000")
        self.assertTrue(request["inclusive"])

    def test_all_archive_queue_does_not_expire_while_waiting_for_delivery(self):
        delivery = SimpleNamespace(
            conversation=self.conversation(),
            source_platform="slack",
            metadata={"backfill": True},
        )
        with patch.object(mirror, "_delivery_created_at", return_value=1):
            self.assertFalse(
                mirror._backfill_delivery_is_outside_history_window(delivery)
            )
            delivery.conversation.grant.consent_version = PRIVATE_CHANNEL_CONSENT
            self.assertTrue(
                mirror._backfill_delivery_is_outside_history_window(delivery)
            )

    def test_scan_authority_accepts_archive_and_recent_refresh_boundaries(self):
        for oldest in ("", "7000000"):
            with self.subTest(oldest=oldest):
                expected = self.authority(oldest)
                metadata = {
                    "scan_epoch": expected.epoch,
                    "participant_hash": expected.participant_hash,
                    "mlai_channel_id": expected.mlai_channel_id,
                    "registration_id": expected.registration_id,
                    "registration_generation": expected.registration_generation,
                    "history_days": 0,
                    "oldest": oldest,
                }
                self.assertEqual(
                    mirror._history_scan_authority_from_state(
                        SimpleNamespace(metadata=metadata)
                    ),
                    expected,
                )
        metadata["oldest"] = "invalid"
        with self.assertRaises(mirror.SlackDmMirrorAuthorizationError):
            mirror._history_scan_authority_from_state(
                SimpleNamespace(metadata=metadata)
            )

    def test_archive_deliveries_never_move_recency_backwards(self):
        conversation = self.conversation(latest="1000.000002")
        mirror._advance_latest_synced_ts(conversation, "100.000001")
        self.assertEqual(conversation.latest_synced_ts, "1000.000002")
        mirror._advance_latest_synced_ts(conversation, "1000.000003")
        self.assertEqual(conversation.latest_synced_ts, "1000.000003")

    def test_repeated_history_boundary_is_an_error_not_a_completed_scan(self):
        conversation = self.conversation()
        state = SimpleNamespace(metadata={}, save=MagicMock())
        with self.assertRaisesRegex(mirror.SlackDmMirrorError, "no progress"):
            mirror._persist_history_page_locked(
                conversation,
                state,
                self.authority(),
                {
                    "has_more": True,
                    "messages": [{"ts": conversation.oldest_synced_ts}],
                },
            )
        state.save.assert_not_called()
        conversation.save.assert_not_called()

    def test_repeated_thread_cursor_is_an_error_not_a_completed_scan(self):
        conversation = self.conversation()
        conversation.participant_slack_ids = []
        state = SimpleNamespace(metadata={"cursor": "same"}, save=MagicMock())
        with self.assertRaisesRegex(mirror.SlackDmMirrorError, "no progress"):
            mirror._persist_reply_page_locked(
                conversation,
                state,
                "1.000001",
                self.authority(),
                {
                    "has_more": True,
                    "messages": [],
                    "response_metadata": {"next_cursor": "same"},
                },
            )
        state.save.assert_not_called()
        conversation.save.assert_not_called()

    @patch.object(mirror, "conversation_kind", return_value="private_channel")
    def test_departed_member_history_is_kept_without_changing_recipients(self, _kind):
        conversation = self.conversation()
        conversation.participant_slack_ids = ["UOWNER", "UCURRENT"]
        conversation.participant_buzz_pubkeys = ["owner-device", "import-key"]
        conversation.oldest_synced_ts = ""
        state = SimpleNamespace(metadata={}, save=MagicMock())
        with patch.object(mirror, "_enqueue_history_message") as enqueue, patch.object(
            mirror, "_next_incomplete_thread_state", return_value=None
        ), patch.object(mirror, "_finish_history_scan"):
            count = mirror._persist_history_page_locked(
                conversation,
                state,
                self.authority(),
                {
                    "messages": [
                        {
                            "ts": "100.000001",
                            "user": "UFORMER",
                            "text": "Earlier message",
                            "user_profile": {"display_name": "Former member"},
                        }
                    ],
                },
            )
        self.assertEqual(count, 1)
        self.assertEqual(
            enqueue.call_args.args[1]["_mlai_history_author"]["display_name"],
            "Former member",
        )
        self.assertEqual(conversation.participant_slack_ids, ["UOWNER", "UCURRENT"])
        self.assertEqual(
            conversation.participant_buzz_pubkeys, ["owner-device", "import-key"]
        )

    @patch.object(mirror, "conversation_kind", return_value="private_channel")
    def test_bot_history_retains_attribution(self, _kind):
        conversation = self.conversation()
        message = {
            "bot_id": "BARCHIVE",
            "subtype": "bot_message",
            "username": "Build bot",
        }
        self.assertTrue(mirror._history_message_author_allowed(conversation, message))
        result = mirror._normalize_history_author(conversation, message)
        self.assertEqual(result["user"], "BARCHIVE")
        self.assertEqual(result["_mlai_history_author"]["display_name"], "Build bot")

    @patch.object(mirror, "_shadow_pubkey", return_value="import-key")
    @patch.object(mirror, "conversation_kind", return_value="private_channel")
    def test_historical_attribution_cannot_add_recipients_or_impersonate_owner(
        self, _kind, _key
    ):
        conversation = self.conversation()
        conversation.participant_identity_map = {"UOWNER": "owner-device"}
        conversation.participant_buzz_pubkeys = ["owner-device", "import-key"]
        delivery = SimpleNamespace(
            conversation=conversation,
            source_author_id="UFORMER",
            source_platform="slack",
            metadata={"backfill": True},
        )
        self.assertEqual(mirror._history_delivery_author_pubkey(delivery), "import-key")
        conversation.participant_buzz_pubkeys = ["owner-device"]
        self.assertEqual(mirror._history_delivery_author_pubkey(delivery), "")
        conversation.participant_buzz_pubkeys.append("import-key")
        delivery.source_platform = "buzz"
        self.assertEqual(mirror._history_delivery_author_pubkey(delivery), "")
        delivery.source_platform = "slack"
        conversation.grant.consent_version = PRIVATE_CHANNEL_CONSENT
        self.assertEqual(mirror._history_delivery_author_pubkey(delivery), "")

    @patch.object(
        mirror, "conversation_metadata", return_value={"source_archived": True}
    )
    def test_archived_history_rejects_writes_before_slack_io(self, _metadata):
        with patch.object(mirror, "WebClient") as client:
            with self.assertRaisesRegex(
                mirror.SlackDmMirrorAuthorizationError, "read-only"
            ):
                mirror._deliver_to_slack(
                    SimpleNamespace(conversation=self.conversation())
                )
            client.assert_not_called()

    @patch.object(slack_views, "status_payload", return_value={})
    @patch.object(slack_views, "slack_connection_for_user", return_value=None)
    def test_connect_summary_records_the_explicit_all_history_consent(
        self, _connection, _status
    ):
        request = SimpleNamespace(data={"history_days": 0}, user=object())
        with patch.object(
            slack_views.SlackDmMirrorView,
            "_authorization_url",
            return_value="https://example.test/oauth",
        ):
            response = slack_views.SlackDmMirrorView().post(request)
        self.assertEqual(response.data["consent"]["version"], ALL_HISTORY_CONSENT)
        self.assertIn("All available history", response.data["consent"]["summary"])

    def backfill(self, *, selected_days, legacy_alias=False):
        grant = self.grant(7, PRIVATE_CHANNEL_CONSENT)
        grant.status = "active"
        grant.revoked_at = None
        grant.save = MagicMock()
        with patch.object(mirror.SlackDmMirrorGrant, "objects") as grants, patch.object(
            mirror.SlackDmMirrorConversation, "objects"
        ) as conversations, patch.object(
            mirror, "_clear_history_scan_states"
        ), patch.object(
            mirror, "_clear_permanent_recovery_fences_locked"
        ), patch.object(
            mirror, "_mark_backfill_rows_for_recovery_locked"
        ):
            grants.select_for_update.return_value.get.return_value = grant
            conversations.select_for_update.return_value.filter.return_value.order_by.return_value = (
                []
            )
            mirror.backfill_grant.__wrapped__(
                grant, history_days=selected_days, full_history=legacy_alias
            )
        return grant

    def test_explicit_backfill_upgrades_the_existing_bounded_grant(self):
        grant = self.backfill(selected_days=0)
        self.assertEqual(grant.history_days, 0)
        self.assertEqual(grant.consent_version, ALL_HISTORY_CONSENT)

    def test_legacy_alias_never_creates_all_history_consent(self):
        grant = self.backfill(selected_days=0, legacy_alias=True)
        self.assertEqual(grant.history_days, 30)
        self.assertEqual(grant.consent_version, PRIVATE_CHANNEL_CONSENT)

    @patch.object(mirror.time, "time", return_value=10_000_000)
    def test_empty_recent_history_cannot_delete_existing_reply_on_old_root(
        self, _clock
    ):
        conversation = self.conversation(latest="9999999.000001")
        root_ts = f"{10_000_000 - 60 * 86400}.000001"
        root = SimpleNamespace(
            source_platform="slack",
            source_message_id=root_ts,
            metadata={},
            save=MagicMock(),
        )
        reply = SimpleNamespace(
            source_platform="slack",
            source_message_id="9999999.000001",
            metadata={"thread_ts": root_ts},
            save=MagicMock(),
        )
        stored = [root, reply]
        main_state = SimpleNamespace(metadata={})

        def query_rows(**kwargs):
            queryset = MagicMock()
            queryset.order_by.return_value = queryset
            queryset.first.return_value = main_state
            filtered = stored
            if kwargs.get("metadata__history_reconcile_candidate"):
                filtered = [
                    row
                    for row in stored
                    if row.metadata.get(mirror.HISTORY_RECONCILE_CANDIDATE_KEY)
                ]
            queryset.__iter__.side_effect = lambda: iter(filtered)
            return queryset

        with patch.object(
            mirror.SlackDmMirrorDelivery, "objects"
        ) as manager, patch.object(mirror, "_ensure_history_state") as ensure:
            manager.select_for_update.return_value.filter.side_effect = query_rows
            mirror._mark_history_reconciliation_candidates_locked(
                conversation, recent_only=True
            )
            saved_boundary = ensure.call_args.kwargs["metadata"]
            main_state.metadata = {
                "oldest": saved_boundary[mirror.HISTORY_RECONCILE_OLDEST_KEY],
                mirror.HISTORY_RECONCILE_EPOCH_KEY: saved_boundary[
                    mirror.HISTORY_RECONCILE_EPOCH_KEY
                ],
            }
            # A complete main scan returned zero messages, with no old-thread
            # replies request. It cannot provide evidence that this reply died.
            mirror._reconcile_absent_slack_state_locked(conversation)
            manager.get_or_create.assert_not_called()
            manager.create.assert_not_called()
        self.assertNotIn(mirror.HISTORY_RECONCILE_CANDIDATE_KEY, reply.metadata)
        reply.save.assert_not_called()

    def test_unknown_outbound_thread_root_is_not_deletion_evidence(self):
        row = SimpleNamespace(
            source_platform="buzz",
            source_message_id="relay-event",
            metadata={
                "source_parent_message_id": "relay-parent",
                "slack_ts": "9999999.000001",
            },
        )
        self.assertFalse(mirror._message_is_within_reconciliation_scope(row, "7000000"))
        self.assertTrue(mirror._message_is_within_reconciliation_scope(row, ""))

    @patch.object(mirror, "conversation_kind", return_value="private_channel")
    def test_all_grant_refresh_preserves_existing_old_thread_parent(self, _kind):
        conversation = self.conversation()
        conversation.participant_slack_ids = ["UOWNER", "UCURRENT"]
        conversation.oldest_synced_ts = ""
        state = SimpleNamespace(metadata={}, save=MagicMock())
        with patch.object(mirror, "_enqueue_history_message") as enqueue, patch.object(
            mirror, "_ensure_thread_state"
        ) as thread, patch.object(
            mirror, "_next_incomplete_thread_state", return_value=None
        ), patch.object(
            mirror, "_finish_history_scan"
        ):
            mirror._persist_history_page_locked(
                conversation,
                state,
                self.authority(oldest="7000000"),
                {
                    "messages": [
                        {
                            "ts": "9999999.000001",
                            "thread_ts": "1000000.000001",
                            "user": "UCURRENT",
                            "text": "New reply to old root",
                        }
                    ],
                },
            )
        message = enqueue.call_args.args[1]
        self.assertEqual(message["thread_ts"], "1000000.000001")
        self.assertNotIn("_mlai_original_thread_ts", message)
        thread.assert_called_once()

    def test_upgrade_and_later_archive_pages_preserve_known_recency(self):
        grant = self.grant(7, PRIVATE_CHANNEL_CONSENT)
        grant.status = "active"
        grant.revoked_at = None
        grant.save = MagicMock()
        conversation = self.conversation(latest="9999999.000001")
        conversation.grant = grant
        with patch.object(mirror.SlackDmMirrorGrant, "objects") as grants, patch.object(
            mirror.SlackDmMirrorConversation, "objects"
        ) as conversations, patch.object(
            mirror, "_clear_history_scan_states"
        ), patch.object(
            mirror, "_clear_permanent_recovery_fences_locked"
        ), patch.object(
            mirror, "_mark_backfill_rows_for_recovery_locked"
        ), patch.object(
            mirror, "_mark_history_reconciliation_candidates_locked"
        ):
            grants.select_for_update.return_value.get.return_value = grant
            conversations.select_for_update.return_value.filter.return_value.order_by.return_value = [
                conversation
            ]

            def update(**fields):
                for key, value in fields.items():
                    setattr(conversation, key, value)

            conversations.filter.return_value.update.side_effect = update
            mirror.backfill_grant.__wrapped__(grant, history_days=0)
        self.assertEqual(conversation.latest_synced_ts, "9999999.000001")
        mirror._advance_latest_synced_ts(conversation, "123.000001")
        self.assertEqual(conversation.latest_synced_ts, "9999999.000001")

    @patch.object(mirror.time, "time", return_value=10_000_000)
    def test_only_replies_whose_roots_are_in_the_scan_are_deletion_candidates(
        self, _clock
    ):
        old_reply = SimpleNamespace(
            source_platform="slack",
            source_message_id="9999999.000001",
            metadata={"thread_ts": "1000000.000001"},
        )
        recent_reply = SimpleNamespace(
            source_platform="slack",
            source_message_id="9999999.000002",
            metadata={"thread_ts": "9000000.000001"},
        )
        with patch.object(
            mirror.SlackDmMirrorDelivery, "objects"
        ) as manager, patch.object(mirror, "_ensure_history_state"):
            manager.select_for_update.return_value.filter.return_value = [
                old_reply,
                recent_reply,
            ]
            mirror._mark_history_reconciliation_candidates_locked(
                self.conversation(), recent_only=True
            )
        self.assertNotIn(mirror.HISTORY_RECONCILE_CANDIDATE_KEY, old_reply.metadata)
        self.assertTrue(recent_reply.metadata[mirror.HISTORY_RECONCILE_CANDIDATE_KEY])
