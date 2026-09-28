"""Database-free regressions for the actual public and private ingestion paths."""

from unittest.mock import patch
from django.test import SimpleTestCase, override_settings

from integrations.services.community_bridge.formatting import (
    sanitize_slack_message,
    sanitize_slack_text,
)
from integrations.services.community_bridge.store import _normalize_slack_event
from integrations.services.slack_dm_mirror import _slack_message_text
from integrations.services.community_bridge.slack import SlackBridgeClient
from integrations.services.community_bridge.slack_actions import action_digest


@override_settings(
    COMMUNITY_CHAT_RELAY_URL="wss://chat.mlai.au",
    COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID="TMLAI",
    COMMUNITY_CHAT_ROO_SLACK_USER_ID="UROO",
)
class SlackDisplayContentTests(SimpleTestCase):
    def jobs(self):
        return {
            "user": "UROO",
            "ts": "1790000000.000001",
            "text": "Flattened fallback",
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": "Top 3 AI + startup jobs"},
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*1. <https://jobs.example/a_(b)?x=1&amp;y=2|AI Engineer>* — Company\nSydney · Australia fit",
                    },
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "20 matches · <https://jobs.example/all|View all jobs →>",
                        }
                    ],
                },
            ],
        }

    def topics(self):
        return {
            "user": "UROO",
            "ts": "1790000000.000002",
            "text": "Topic selection ready for review",
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "📊 Article Topics Selected",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*1. AI Assist*\n`ai assist`\n📈 27100/mo • Difficulty: 9/100\n_Low competition_",
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Op 1: ai assist"},
                            "action_id": "confirm_topic_btn_0",
                            "value": "confirm_topic:job-id:0",
                        }
                    ],
                },
            ],
        }

    def test_jobs_preserve_blocks_bold_named_links_and_lines(self):
        text = sanitize_slack_message(self.jobs())
        self.assertEqual(
            text,
            "**Top 3 AI + startup jobs**\n\n**1. [AI Engineer](https://jobs.example/a_%28b%29?x=1&y=2)** — Company\nSydney · Australia fit\n\n20 matches · [View all jobs →](https://jobs.example/all)",
        )

    def test_topic_content_and_actions_survive_public_create_and_edit(self):
        for subtype in ("", "message_changed"):
            card = self.topics()
            event = {
                "type": "message",
                "channel_type": "channel",
                "channel": "CTOPICS",
                "subtype": subtype,
            }
            event.update({"message": card} if subtype else card)
            result = _normalize_slack_event({"team_id": "TMLAI", "event": event})
            self.assertIn("**1. AI Assist**\n`ai assist`", result["text"])
            self.assertNotIn("Topic selection ready for review", result["text"])
            self.assertIn("mlai_action=confirm_topic_btn_0", result["text"])
            self.assertIn(
                action_digest(card, card["blocks"][-1]["elements"][0]), result["text"]
            )
            with patch.object(SlackBridgeClient, "get_user_display_name"), patch.object(
                SlackBridgeClient, "get_channel_display_name"
            ):
                resolved = SlackBridgeClient.resolve_markdown_entities(
                    result["metadata"]["slack_display_markdown"],
                )
            self.assertEqual(resolved, result["text"])

    def test_private_live_and_history_share_full_content_renderer(self):
        text = _slack_message_text(
            self.topics(), workspace_id="TMLAI", channel_id="DROO"
        )
        self.assertIn("27100/mo", text)
        self.assertIn("mlai_action=confirm_topic_btn_0", text)

    def test_other_bots_actions_open_slack_without_native_authority(self):
        card = self.topics()
        card["user"] = "UOTHER"
        text = sanitize_slack_message(card, workspace_id="TMLAI", channel_id="CTOPICS")
        self.assertIn("https://app.slack.com/client/TMLAI/CTOPICS", text)
        self.assertNotIn("mlai_action=", text)

    def test_plain_text_markdown_code_entities_and_link_labels(self):
        self.assertEqual(
            sanitize_slack_text("*bold* _italic_ ~old~ and `*literal*`\nnext"),
            "**bold** _italic_ ~~old~~ and `*literal*`\nnext",
        )
        self.assertEqual(
            sanitize_slack_text(
                "Ask <@UONE> in <#CONE|general>", user_name_resolver=lambda _: "Alice"
            ),
            "Ask @Alice in #general",
        )
        self.assertEqual(
            sanitize_slack_text(
                "before ```*literal*\n<https://example.com|x>``` after"
            ),
            "before ```*literal*\n<https://example.com|x>``` after",
        )
        self.assertIn(
            r"[a\]b](https://example.com)",
            sanitize_slack_text("<https://example.com|a]b>"),
        )

    def test_nested_rich_text_and_attachment_blocks(self):
        message = {
            "blocks": [
                {
                    "type": "rich_text",
                    "elements": [
                        {
                            "type": "rich_text_list",
                            "style": "ordered",
                            "elements": [
                                {
                                    "type": "rich_text_section",
                                    "elements": [
                                        {
                                            "type": "text",
                                            "text": "Job",
                                            "style": {"bold": True},
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
            "attachments": [
                {
                    "blocks": [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": "*Details*"},
                        }
                    ]
                }
            ],
        }
        self.assertEqual(sanitize_slack_message(message), "1. **Job**\n\n**Details**")

    def test_deferred_mentions_keep_markdown_and_literal_code_intact(self):
        card = self.topics()
        card["blocks"].insert(
            1,
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Ask <@UONE>, keep `<@UONE>` and &lt;@UONE&gt; literal.",
                },
            },
        )
        event = {
            **card,
            "type": "message",
            "channel_type": "channel",
            "channel": "CTOPICS",
        }
        result = _normalize_slack_event({"team_id": "TMLAI", "event": event})
        self.assertNotIn("slack_display_message", result["metadata"])
        self.assertNotIn("confirm_topic:job-id:0", str(result["metadata"]))
        with patch.object(
            SlackBridgeClient, "get_user_display_name", return_value="Alice"
        ):
            resolved = SlackBridgeClient.resolve_markdown_entities(
                result["metadata"]["slack_display_markdown"]
            )
        self.assertIn("Ask @Alice, keep `<@UONE>` and \\<@UONE\\> literal.", resolved)
        self.assertIn("**1. AI Assist**", resolved)

    def test_fallback_and_unsafe_links(self):
        self.assertEqual(
            sanitize_slack_message(
                {"text": "fallback", "blocks": [{"type": "unknown"}]}
            ),
            "fallback",
        )
        self.assertNotIn(
            "javascript:",
            sanitize_slack_message(
                {
                    "blocks": [
                        {
                            "type": "actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"text": "Bad"},
                                    "url": "javascript:alert(1)",
                                }
                            ],
                        }
                    ]
                }
            ),
        )
