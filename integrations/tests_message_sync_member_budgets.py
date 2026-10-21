"""Channel write pacing retains the existing app/workspace/method floor."""

from unittest.mock import MagicMock, call, patch

from django.test import SimpleTestCase, override_settings
from slack_sdk.errors import SlackApiError

from integrations.services.message_sync.scheduler import BudgetDeferred
from integrations.services.message_sync.slack_client import (
    POSTING_METHODS, budgeted_client, channel_budget_method, provider_interval,
)


@override_settings(MESSAGE_SYNC_ENABLED=True, MESSAGE_SYNC_SLACK_APP_ID="ABOT")
class MemberPostingBudgetTests(SimpleTestCase):
    def test_each_write_charges_method_floor_and_exact_channel(self):
        for method in sorted(POSTING_METHODS):
            for argument in ("json", "data", "params"):
                with self.subTest(method=method, argument=argument), patch(
                    "integrations.services.message_sync.slack_client.admit_request"
                ) as admit:
                    client = budgeted_client(MagicMock(), workspace_id="T1", app_id="AUSER")
                    client.api_call(method, **{argument: {"channel": "CONE"}})
                    self.assertEqual(admit.call_args_list, [
                        call(app_id="AUSER", workspace_id="T1", method=method,
                             interval_seconds=3.0, priority="foreground"),
                        call(app_id="AUSER", workspace_id="T1", method=f"{method}:CONE",
                             interval_seconds=1.0, priority="foreground"),
                    ])
                    self.assertEqual(provider_interval(f"{method}:CONE"), 1.0)

    def test_channels_apps_and_workspaces_never_share_channel_keys(self):
        with patch("integrations.services.message_sync.slack_client.admit_request") as admit:
            for app, workspace, channel in (("A1", "T1", "C1"), ("A1", "T1", "C2"),
                                             ("A2", "T1", "C1"), ("A1", "T2", "C1")):
                budgeted_client(MagicMock(), workspace_id=workspace, app_id=app).api_call(
                    "chat.update", json={"channel": channel},
                )
            keys = {(entry.kwargs["app_id"], entry.kwargs["workspace_id"], entry.kwargs["method"])
                    for entry in admit.call_args_list if ":" in entry.kwargs["method"]}
            self.assertEqual(len(keys), 4)

    def test_channel_deferral_never_calls_provider(self):
        client = MagicMock()
        original = client.api_call
        with patch("integrations.services.message_sync.slack_client.admit_request",
                   side_effect=[None, BudgetDeferred(1)]):
            with self.assertRaises(BudgetDeferred):
                budgeted_client(client, workspace_id="T1").api_call(
                    "chat.postMessage", json={"channel": "CONE"},
                )
        original.assert_not_called()

    def test_retry_after_fences_both_method_and_channel(self):
        response = {"error": "ratelimited"}
        client = MagicMock()
        error = SlackApiError("synthetic rate limit", response)
        client.api_call.side_effect = error
        with patch("integrations.services.message_sync.slack_client.admit_request"), patch(
            "integrations.services.message_sync.slack_client.record_cooldown"
        ) as cooldown:
            with self.assertRaises(BudgetDeferred):
                budgeted_client(client, workspace_id="T1").api_call(
                    "reactions.add", params={"channel": "CONE"},
                )
        self.assertEqual([entry.kwargs["method"] for entry in cooldown.call_args_list],
                         ["reactions.add", "reactions.add:CONE"])
        self.assertTrue(all(entry.kwargs["retry_after"] == 60 for entry in cooldown.call_args_list))

    def test_other_methods_and_absent_channel_keep_existing_floor(self):
        for method, arguments in (("users.list", {"params": {"channel": "CONE"}}),
                                  ("chat.delete", {"params": {}})):
            with self.subTest(method=method), patch(
                "integrations.services.message_sync.slack_client.admit_request"
            ) as admit:
                budgeted_client(MagicMock(), workspace_id="T1").api_call(method, **arguments)
                self.assertEqual(admit.call_count, 1)
                self.assertEqual(admit.call_args.kwargs["method"], method)

    def test_long_source_ids_have_bounded_distinct_persisted_keys(self):
        keys = [channel_budget_method("chat.postMessage", {"json": {"channel": value * 100}})
                for value in ("C", "G")]
        self.assertTrue(all(len(key) <= 100 for key in keys))
        self.assertNotEqual(*keys)

    @override_settings(MESSAGE_SYNC_ENABLED=False)
    def test_disabled_message_sync_preserves_original_client(self):
        client = MagicMock()
        self.assertIs(budgeted_client(client, workspace_id="T1"), client)
