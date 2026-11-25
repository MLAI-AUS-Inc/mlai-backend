"""Roo's Slack welcome for intros written in MLAI Chat."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from community_chat.intro_welcome import MAX_ATTEMPTS, run_chat_intro_welcome_sweep
from community_chat.volunteer.models import (
    ChatIntroWelcome,
    VolunteerRecognition,
    VolunteerSourceReceipt,
)
from integrations.models import CommunityBridgeChannel, CommunityBridgeMessageLink
from roo.services import PointsService

START = "5d0119d7-33a6-5351-b617-f5de1149b5f3"
MESSAGE = "a" * 64
SLACK_TS = "1794876120.000100"
SEND = "integrations.services.slack.SlackService.send_message"


@override_settings(
    COMMUNITY_CHAT_VOLUNTEER_COMMUNITY="intro-welcome-tests",
    COMMUNITY_CHAT_VOLUNTEER_CHANNELS={"start_here": START},
    COMMUNITY_CHAT_INTRO_WELCOME_ENABLED=True,
)
class ChatIntroWelcomeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="isaak@example.com", first_name="Isaak", last_name="Eriksson",
        )
        self.mapping = CommunityBridgeChannel.objects.create(
            slack_workspace_id="TTEST", slack_channel_id="CSTART",
            destination_platform="buzz", destination_channel_id=START,
            audience="community", slack_visibility="public",
        )

    def intro(self, *, message=MESSAGE, occurred=None, recognised_message=None):
        occurred = occurred or timezone.now() - timedelta(minutes=5)
        ledger, _ = PointsService.award(
            user=self.user, delta=4, source="EVENT", description="intro",
            created_by_slack_id="SYSTEM", idempotency_key=f"intro-test:{message}",
        )
        recognition = VolunteerRecognition.objects.create(
            community="intro-welcome-tests", user=self.user,
            outcome_key=f"intro:{message}", action_key="introduce_yourself",
            source={"channel_id": START, "message_id": recognised_message or message},
            occurred_at=occurred, status="approved", reward_microroo=4_000_000,
            ledger=ledger,
        )
        return VolunteerSourceReceipt.objects.create(
            community="intro-welcome-tests", source_key=f"relay:nostr:{message}",
            origin="relay", kind="post", actor=self.user,
            source={"channel_id": START, "message_id": message, "source_id": message},
            metadata={"original": True, "top_level": True, "has_text": True},
            occurred_at=occurred, status="processed", recognition=recognition,
        )

    def bridge(self, message=MESSAGE, slack_ts=SLACK_TS):
        return CommunityBridgeMessageLink.objects.create(
            channel=self.mapping, source_platform="buzz", destination_platform="slack",
            source_channel_id=START, source_message_id=message,
            destination_channel_id="CSTART", destination_message_id=slack_ts,
        )

    def test_welcomes_chat_intro_once_in_its_slack_thread(self):
        self.intro()
        self.bridge()
        with patch(SEND, return_value=(True, "1794876200.000200")) as send:
            first = run_chat_intro_welcome_sweep()
            second = run_chat_intro_welcome_sweep()

        send.assert_called_once()
        channel, text = send.call_args.args
        self.assertEqual(channel, "CSTART")
        self.assertEqual(send.call_args.kwargs["thread_ts"], SLACK_TS)
        self.assertEqual(
            text,
            "Welcome Isaak Eriksson! You've earned 4 Roo points "
            "for introducing yourself or your startup.",
        )
        self.assertEqual(first["sent"], 1)
        self.assertEqual(second["sent"], 0)
        row = ChatIntroWelcome.objects.get()
        self.assertEqual((row.status, row.slack_message_ts), ("sent", "1794876200.000200"))

    def test_waits_until_the_bridge_has_posted_to_slack(self):
        self.intro()
        with patch(SEND, return_value=(True, "1.2")) as send:
            waiting = run_chat_intro_welcome_sweep()
            self.bridge()
            ready = run_chat_intro_welcome_sweep()

        self.assertEqual(waiting["waiting_for_slack"], 1)
        self.assertEqual(ready["sent"], 1)
        send.assert_called_once()

    def test_slack_failure_retries_then_stops(self):
        self.intro()
        self.bridge()
        with patch(SEND, return_value=(False, None)) as send:
            for _ in range(MAX_ATTEMPTS + 2):
                run_chat_intro_welcome_sweep()

        self.assertEqual(send.call_count, MAX_ATTEMPTS)
        row = ChatIntroWelcome.objects.get()
        self.assertEqual((row.status, row.attempts, row.last_error), ("pending", MAX_ATTEMPTS, "slack_error"))

    def test_skips_old_intros_and_awards_from_another_post(self):
        self.intro(occurred=timezone.now() - timedelta(days=3))
        self.bridge()
        self.intro(message="b" * 64, recognised_message="c" * 64)
        self.bridge(message="b" * 64, slack_ts="1794876130.000100")
        with patch(SEND, return_value=(True, "1.2")) as send:
            result = run_chat_intro_welcome_sweep()

        send.assert_not_called()
        self.assertEqual(result["sent"], 0)
        self.assertFalse(ChatIntroWelcome.objects.exists())

    @override_settings(COMMUNITY_CHAT_INTRO_WELCOME_ENABLED=False)
    def test_kill_switch_disables_welcomes(self):
        self.intro()
        self.bridge()
        with patch(SEND) as send:
            self.assertEqual(run_chat_intro_welcome_sweep(), {"status": "disabled"})
        send.assert_not_called()
