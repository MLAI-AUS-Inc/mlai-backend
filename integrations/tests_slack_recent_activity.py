"""Recent Slack import policy regressions; database access is forbidden."""

import os
import unittest
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class SlackRecentActivityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mlai.settings")
        import django

        django.setup()
        from integrations.services import slack_dm_mirror

        cls.service = slack_dm_mirror

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch(
                "django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection",
                side_effect=AssertionError(
                    "Recent activity tests must not open a database"
                ),
            )
        )
        self.now = 1_789_084_800
        self.authority = SimpleNamespace(
            grant_id=17,
            consent_generation=2,
            consent_version="slack-chat-v4-private-channels",
            oauth_generation=3,
            workspace_id="TONE",
            slack_user_id="UONE",
        )
        self.connection = SimpleNamespace(sync_cursor={}, save=MagicMock())
        self.grant = SimpleNamespace(
            history_days=30, consent_version=self.authority.consent_version
        )
        self.stack.enter_context(
            patch.object(
                self.service.transaction, "atomic", side_effect=lambda: nullcontext()
            )
        )
        self.stack.enter_context(
            patch.object(self.service.time, "time", return_value=self.now)
        )
        self.lock = self.stack.enter_context(
            patch.object(
                self.service,
                "_lock_slack_grant_api_authority",
                return_value=(self.grant, self.connection),
            )
        )
        self.call = self.stack.enter_context(
            patch.object(self.service, "_call_slack_with_grant_authority")
        )
        self.call.side_effect = [
            {"channel": {"id": "DTEST"}},
            {
                "messages": [
                    {
                        "ts": (str(self.now - 10) + ".000001"),
                        "text": "never cache this body",
                    }
                ]
            },
        ]

    def probe(self, raw=None):
        return self.service._recent_discovery_activity(
            self.grant,
            self.authority,
            raw or {"id": "DTEST"},
            required_scopes=self.service.DIRECT_DM_SCOPES,
        )

    def test_missing_activity_uses_one_bounded_message_and_caches_timestamps_only(self):
        self.assertEqual(self.probe(), self.now - 10)
        args, kwargs = self.call.call_args
        self.assertEqual(args, (self.authority, "conversations_history"))
        self.assertEqual(kwargs["oldest"], (str(self.now - 30 * 86400)))
        self.assertEqual(kwargs["limit"], 1)
        self.assertTrue(kwargs["inclusive"])
        self.assertEqual(self.probe(), self.now - 10)
        self.assertEqual(self.call.call_count, 2)
        entry = self.connection.sync_cursor[self.service.RECENT_ACTIVITY_CACHE_KEY][
            "entries"
        ]["DTEST"]
        self.assertEqual(entry, {"activity": self.now - 10, "checked_at": self.now})
        self.assertNotIn("never cache", str(self.connection.sync_cursor))
        self.assertTrue(self.lock.called)

    def test_known_activity_skips_info_history_and_cache(self):
        self.assertEqual(
            self.probe(
                {"id": "DTEST", "latest": (str(self.now - 31 * 86400) + ".000001")}
            ),
            self.now - 31 * 86400,
        )
        self.call.assert_not_called()
        self.connection.save.assert_not_called()

    def test_new_reply_on_an_old_root_counts_as_activity(self):
        self.assertEqual(
            self.probe(
                {
                    "id": "DTEST",
                    "latest": {
                        "ts": (str(self.now - 90 * 86400) + ".000001"),
                        "latest_reply": (str(self.now - 20) + ".000001"),
                    },
                }
            ),
            self.now - 20,
        )
        self.call.assert_not_called()

    def test_metadata_update_and_transport_dates_are_not_activity(self):
        self.probe({"id": "DTEST", "updated": self.now * 1000, "created": self.now})
        self.assertEqual(self.call.call_count, 2)

    def test_info_activity_avoids_history_request(self):
        self.call.side_effect = [
            {
                "channel": {
                    "id": "DTEST",
                    "latest": (str(self.now - 40 * 86400) + ".000001"),
                }
            }
        ]
        self.assertEqual(self.probe(), self.now - 40 * 86400)
        self.assertEqual(self.call.call_count, 1)

    def test_quiet_probe_rechecks_after_one_hour(self):
        self.call.side_effect = [{"channel": {"id": "DTEST"}}, {"messages": []}]
        self.assertEqual(self.probe(), 0)
        with patch.object(self.service.time, "time", return_value=self.now + 3599):
            self.assertEqual(self.probe(), 0)
        self.call.side_effect = [
            {"channel": {"id": "DTEST", "latest": (str(self.now + 3600) + ".000001")}}
        ]
        with patch.object(self.service.time, "time", return_value=self.now + 3600):
            self.assertEqual(self.probe(), self.now + 3600)
        self.assertEqual(self.call.call_count, 3)

    def test_recent_probe_rechecks_after_five_minutes(self):
        self.probe()
        self.call.side_effect = [
            {"channel": {"id": "DTEST", "latest": (str(self.now + 300) + ".000001")}}
        ]
        with patch.object(self.service.time, "time", return_value=self.now + 300):
            self.assertEqual(self.probe(), self.now + 300)

    def test_cache_is_scoped_to_consent_oauth_owner_workspace_and_window(self):
        for field, value in [
            ("grant_id", 19),
            ("consent_generation", 4),
            ("oauth_generation", 5),
            ("workspace_id", "TTWO"),
            ("slack_user_id", "UTWO"),
            ("consent_version", "different"),
            ("history_days", 7),
        ]:
            with self.subTest(field=field):
                self.connection.sync_cursor = {}
                self.call.side_effect = None
                self.call.return_value = {
                    "channel": {"id": "DTEST", "latest": (str(self.now) + ".000001")}
                }
                self.probe()
                self.connection.sync_cursor[self.service.RECENT_ACTIVITY_CACHE_KEY][
                    field
                ] = value
                self.call.reset_mock()
                self.probe()
                self.call.assert_called_once()

    def test_malformed_response_does_not_cache_false_inactivity(self):
        for response in [
            {},
            {"messages": None},
            {"messages": [None]},
            {"messages": [{"ts": "bad"}]},
        ]:
            with self.subTest(response=response):
                self.call.side_effect = [{"channel": {"id": "DTEST"}}, response]
                with self.assertRaises(self.service.SlackDmMirrorUpstreamError):
                    self.probe()
                self.assertEqual(self.connection.sync_cursor, {})

    def test_unknown_cache_shape_is_discarded(self):
        self.connection.sync_cursor = {
            self.service.RECENT_ACTIVITY_CACHE_KEY: ["invalid"]
        }
        self.assertEqual(self.probe(), self.now - 10)

    def test_probe_rate_limit_propagates_without_caching_empty(self):
        self.call.side_effect = self.service.SlackDmMirrorRateLimited("retry")
        with self.assertRaises(self.service.SlackDmMirrorRateLimited):
            self.probe()
        self.assertEqual(self.connection.sync_cursor, {})

    def test_revocation_blocks_cached_result(self):
        self.probe()
        self.lock.side_effect = self.service.SlackDmMirrorAuthorizationError("revoked")
        with self.assertRaises(self.service.SlackDmMirrorAuthorizationError):
            self.probe()

    def test_external_info_retires_without_reading_history(self):
        self.call.side_effect = [{"channel": {"id": "DTEST", "is_ext_shared": True}}]
        with patch.object(
            self.service, "_retire_ineligible_from_slack_response"
        ) as retire:
            with self.assertRaises(self.service.SlackDmMirrorError):
                self.probe()
        retire.assert_called_once()
        self.assertEqual(self.call.call_count, 1)

    def test_mismatched_info_is_not_treated_as_an_empty_conversation(self):
        self.call.side_effect = [{"channel": {"id": "DOTHER"}}]
        with self.assertRaises(self.service.SlackDmMirrorUpstreamError):
            self.probe()
        self.assertEqual(self.call.call_count, 1)

    def discover(self, raw, *, staged=None, existing=False, history_days=30, seen=None):
        s = self.service
        grant = self.grant
        grant.pk = 17
        grant.connection = SimpleNamespace(
            provider="slack",
            status="connected",
            access_token="test-only",
            scopes=list(s.DIRECT_DM_SCOPES),
        )
        grant.history_days = history_days
        if history_days == 0:
            grant.consent_version = s.ALL_HISTORY_CONSENT
        grant.slack_workspace_id = "TONE"
        grant.slack_user_id = "UONE"
        grant.conversations = MagicMock()
        grant.conversations.values_list.return_value = []
        self.stack.enter_context(
            patch.object(s, "ensure_owner_identity", return_value=(None, False, None))
        )
        self.stack.enter_context(
            patch.object(s, "_connection_identity", return_value=("TONE", "UONE"))
        )
        self.stack.enter_context(
            patch.object(
                s, "_capture_slack_grant_api_authority", return_value=self.authority
            )
        )
        self.stack.enter_context(
            patch.object(s, "private_channels_enabled", return_value=False)
        )
        grants = self.stack.enter_context(patch.object(s.SlackDmMirrorGrant, "objects"))
        grants.select_related.return_value.filter.return_value.first.return_value = (
            grant
        )
        conversations = self.stack.enter_context(
            patch.object(s.SlackDmMirrorConversation, "objects")
        )
        conversations.filter.return_value.exists.return_value = existing
        self.stack.enter_context(
            patch.object(
                s,
                "_load_discovery_checkpoint",
                return_value=("page-1", seen or set(), [], None),
            )
        )
        self.stack.enter_context(
            patch.object(s, "_staged_slack_channel_ids", return_value=staged or set())
        )
        self.stack.enter_context(patch.object(s, "_reconcile_registration_cleanup"))
        self.stack.enter_context(
            patch.object(s, "_complete_inactive_conversation_history")
        )
        self.stack.enter_context(
            patch.object(s, "_drain_staged_events_for_conversation")
        )
        self.checkpoint = self.stack.enter_context(
            patch.object(s, "_save_discovery_checkpoint")
        )
        self.mirror = self.stack.enter_context(
            patch.object(s, "_discover_conversation")
        )
        self.call.side_effect = None
        self.call.return_value = {
            "channels": raw,
            "response_metadata": {"next_cursor": "page-2"},
        }
        return s.discover_conversations(grant)

    def test_inactive_new_channel_never_provisions_or_reads_members_profiles_history(
        self,
    ):
        self.assertEqual(
            self.discover(
                [
                    {
                        "id": "DOLD",
                        "user": "UTWO",
                        "latest": (str(self.now - 31 * 86400) + ".000001"),
                    }
                ]
            ),
            0,
        )
        self.mirror.assert_not_called()
        self.assertEqual(self.call.call_count, 1)

    def test_existing_quiet_channel_revalidates_membership_without_new_history(self):
        self.assertEqual(
            self.discover(
                [
                    {
                        "id": "DOLD",
                        "user": "UTWO",
                        "latest": (str(self.now - 31 * 86400) + ".000001"),
                    }
                ],
                existing=True,
            ),
            1,
        )
        self.assertTrue(self.mirror.call_args.kwargs["check_recent_activity"])

    def test_live_event_bypasses_cached_quiet_result(self):
        self.assertEqual(
            self.discover(
                [
                    {
                        "id": "DOLD",
                        "user": "UTWO",
                        "latest": (str(self.now - 31 * 86400) + ".000001"),
                    }
                ],
                staged={"DOLD"},
            ),
            1,
        )
        self.assertTrue(self.mirror.call_args.kwargs["recent_activity"])

    def test_explicit_full_history_keeps_old_conversations_eligible(self):
        self.assertEqual(
            self.discover(
                [
                    {
                        "id": "DOLD",
                        "user": "UTWO",
                        "latest": (str(self.now - 31 * 86400) + ".000001"),
                    }
                ],
                history_days=0,
            ),
            1,
        )
        self.assertTrue(self.mirror.call_args.kwargs["recent_activity"])

    def test_rate_limit_saves_current_page_and_completed_prefix_for_resume(self):
        with patch.object(
            self.service,
            "_recent_discovery_activity",
            side_effect=[self.now, self.service.SlackDmMirrorRateLimited("retry")],
        ):
            with self.assertRaises(self.service.SlackDmMirrorRateLimited):
                self.discover(
                    [
                        {"id": "DONE", "user": "UTWO"},
                        {"id": "DPENDING", "user": "UTHREE"},
                    ]
                )
        self.assertEqual(self.checkpoint.call_args.kwargs["cursor"], "page-1")
        self.assertEqual(self.checkpoint.call_args.kwargs["seen_channel_ids"], {"DONE"})

    def test_resume_skips_completed_prefix(self):
        self.assertEqual(
            self.discover(
                [
                    {
                        "id": "DONE",
                        "user": "UTWO",
                        "latest": (str(self.now) + ".000001"),
                    },
                    {
                        "id": "DPENDING",
                        "user": "UTHREE",
                        "latest": (str(self.now) + ".000001"),
                    },
                ],
                seen={"DONE"},
            ),
            1,
        )
        self.assertEqual(self.mirror.call_args.args[2]["id"], "DPENDING")

    def test_existing_membership_is_fenced_before_a_throttled_activity_probe(self):
        s = self.service
        order = []
        self.grant.pk = 17
        self.grant.slack_user_id = "UONE"
        conversation = SimpleNamespace(pk=42, participant_profiles={})

        def membership(*args, **kwargs):
            order.append("membership")
            return conversation, True

        def probe(*args, **kwargs):
            order.append("probe")
            raise s.SlackDmMirrorRateLimited("retry")

        with (
            patch.object(
                s, "_capture_slack_grant_api_authority", return_value=self.authority
            ),
            patch.object(
                s, "_conversation_participant_ids", return_value=["UONE", "UTWO"]
            ),
            patch.object(
                s, "_store_conversation_membership_intent", side_effect=membership
            ),
            patch.object(s, "_recent_discovery_activity", side_effect=probe),
            patch.object(s, "_preload_slack_profiles") as profiles,
            self.assertRaises(s.SlackDmMirrorRateLimited),
        ):
            s._discover_conversation(
                self.grant,
                self.authority,
                {"id": "DTEST", "user": "UTWO"},
                profile_cache={},
                force_backfill=False,
                reset_history=False,
                check_recent_activity=True,
            )
        self.assertEqual(order, ["membership", "probe"])
        profiles.assert_not_called()

    def test_removed_membership_retires_before_optional_activity_requests(self):
        s = self.service
        self.grant.pk = 17
        self.grant.slack_user_id = "UONE"
        with (
            patch.object(
                s, "_capture_slack_grant_api_authority", return_value=self.authority
            ),
            patch.object(s, "_conversation_participant_ids", return_value=[]),
            patch.object(s, "_retire_ineligible_from_slack_response") as retire,
            patch.object(s, "_recent_discovery_activity") as probe,
        ):
            self.assertIsNone(
                s._discover_conversation(
                    self.grant,
                    self.authority,
                    {"id": "DTEST", "user": "UTWO"},
                    profile_cache={},
                    force_backfill=False,
                    reset_history=False,
                    check_recent_activity=True,
                )
            )
        retire.assert_called_once()
        probe.assert_not_called()
