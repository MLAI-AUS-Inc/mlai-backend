"""``manage.py article_flags`` prints every founder setting without leaking secrets."""
from io import StringIO
import json
import os
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings

from content_factory.article_flags import flag_rows


class ArticleFlagsTests(SimpleTestCase):
    def rows(self):
        return {row["name"]: row for row in flag_rows()}

    @override_settings(ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED=True, ARTICLES_MERGE_WORKER_ENABLED=False,
                       CONTENT_FACTORY_API_KEY="sensitive-key", ARTICLES_GOAL_NOTIFIER_PRIVATE_KEY="")
    def test_every_setting_with_its_effective_value_and_no_secret(self):
        with patch.dict(os.environ, {"ARTICLES_MERGE_WORKER_ENABLED": "false"}):
            rows = self.rows()
        self.assertEqual(rows["ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED"]["value"], "true")
        self.assertEqual(rows["ARTICLES_MERGE_WORKER_ENABLED"], {
            "name": "ARTICLES_MERGE_WORKER_ENABLED", "value": "false", "source": "env", "deploy": True, "secret": False})
        self.assertEqual(rows["CONTENT_FACTORY_API_KEY"]["value"], "set")
        self.assertEqual(rows["ARTICLES_GOAL_NOTIFIER_PRIVATE_KEY"]["value"], "unset")
        # Release-installed settings outside the prefixes are listed too.
        self.assertTrue(rows["COMMUNITY_CHAT_ONE_STEP_JOIN_ENABLED"]["deploy"])
        self.assertFalse(rows["CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID"]["deploy"])

    @override_settings(CONTENT_FACTORY_API_KEY="sensitive-key")
    def test_command_text_and_json(self):
        text, data = StringIO(), StringIO()
        call_command("article_flags", "--only-flags", stdout=text)
        call_command("article_flags", "--json", stdout=data)
        self.assertIn("ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED", text.getvalue())
        self.assertNotIn("CONTENT_FACTORY_URL", text.getvalue())
        self.assertNotIn("sensitive-key", text.getvalue() + data.getvalue())
        self.assertIn("ARTICLES_STAFF_ALERT_RULES_ENABLED", {row["name"] for row in json.loads(data.getvalue())})

    @override_settings(ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED=True, ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED=False,
                       CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID="")
    def test_warns_when_the_stuck_alert_is_on_but_has_no_slack_channel(self):
        text = StringIO()
        call_command("article_flags", "--only-flags", stdout=text)
        self.assertIn("ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED is on, but CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID is not set",
                      text.getvalue())
        with override_settings(CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID="C0000000000"):
            text = StringIO()
            call_command("article_flags", "--only-flags", stdout=text)
        self.assertNotIn("WARNING", text.getvalue())
