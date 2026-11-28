"""Exercise the rendered email and provider payload without a database or network."""

from datetime import date
from html import unescape
import re
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from django.core import signing
from django.test import SimpleTestCase, override_settings

from community_chat import onboarding_admin
from content_analytics.services.report_notifications import _report_email_html
from integrations.services import notification_adapters as notifications
from integrations.services.notification_channels import send_email_verification


@override_settings(DEFAULT_BACKEND_URL="https://api.example.test")
class ArticleNotificationEmailTests(SimpleTestCase):
    def setUp(self):
        self.channel = SimpleNamespace(
            id="email-channel", channel_type="email", route_id="member@example.test", user_id=42,
        )
        organization = SimpleNamespace(domain="founder.example.test")
        self.run = SimpleNamespace(
            id="research-run", article_content_factory_run_id="article-run",
            automation=SimpleNamespace(organization=organization, user=object()),
            save=Mock(),
        )

    def test_daily_topic_buttons_and_opt_out_preserve_signed_confirmation(self):
        html = notifications._topic_email_html(
            self.run,
            {"selection": {"options": [
                {"keyword": "research", "suggested_title": "<A title>", "chart_url": "https://charts.example.test/chart.png"},
                {"keyword": "automation", "suggested_title": "Second article", "chart_url": "javascript:bad()"},
            ]}},
            self.channel,
        )
        self.assertIn("mlai-roo.png", html)
        self.assertIn("&lt;A title&gt;", html)
        self.assertIn("https://charts.example.test/chart.png", html)
        self.assertNotIn("javascript:", html)
        self.assertEqual(html.count("Approve this topic"), 2)
        links = [unescape(href) for href in re.findall(r'href="([^"]+)"', html)]
        actions = [
            signing.loads(parse_qs(urlparse(link).query)["token"][0], salt=notifications.AUTOMATION_ACTION_SALT)
            for link in links if link.startswith("https://api.example.test")
        ]
        self.assertEqual([action["action"] for action in actions], ["approve_topic", "approve_topic", "unsubscribe"])
        self.assertTrue(all(action["confirmation_required"] for action in actions))
        self.assertEqual([action["option_index"] for action in actions[:2]], [0, 1])
        self.assertEqual(actions[2]["channel_id"], self.channel.id)

    def test_content_ready_keeps_primary_and_secondary_destinations(self):
        review_url = "https://chat.example.test/review?one=1&two=2"
        preview_url = "https://preview.example.test/ready"
        pr_url = "https://github.com/example/project/pull/3"
        with (
            patch.object(notifications, "resolve_automation_run", return_value=self.run),
            patch.object(notifications, "_founder_tools_run_url", return_value=review_url),
            patch.object(notifications, "_fan_out_event", side_effect=lambda **kw: kw["build_kwargs"](self.channel)),
        ):
            message = notifications.send_content_ready({
                "title": 'Article <draft>', "preview_url": preview_url, "pr_url": pr_url,
            })
        self.assertIn("Article &lt;draft&gt;", message["html_body"])
        for url in (review_url, preview_url, pr_url):
            self.assertIn(url, message["text"])
            self.assertIn(f'href="{url.replace("&", "&amp;")}"', message["html_body"])
        self.assertLess(message["html_body"].index("Review and approve"), message["html_body"].index(preview_url))
        self.assertLess(message["html_body"].index(preview_url), message["html_body"].index(pr_url))

    def test_review_email_keeps_the_copyable_link_below_the_primary_button(self):
        review_url = "https://chat.example.test/review?one=1&two=2"
        with patch.object(notifications, "_review_ready_url", return_value=review_url):
            html = notifications._review_ready_email_html(self.run, {"title": "My article"}, self.channel)
        escaped_url = review_url.replace("&", "&amp;")
        self.assertEqual(html.count(f'href="{escaped_url}"'), 2)
        self.assertLess(html.index("Review article"), html.index(f">{escaped_url}</a>"))
        self.assertIn("Unsubscribe</a>", html)

    @override_settings(COMMUNITY_CHAT_FRONTEND_URL="https://chat.example.test")
    def test_research_empty_uses_the_same_message_and_edit_settings_destination_in_both_parts(self):
        self.run.content_factory_run_id = "research-job"
        with (
            patch.object(notifications, "resolve_automation_run", return_value=self.run),
            patch.object(notifications, "research_empty_applies", return_value=True),
            patch("integrations.services.daily_research_policy.record_empty_night", return_value=True),
            patch.object(notifications, "_fan_out_event", side_effect=lambda **kw: kw["build_kwargs"](self.channel)),
        ):
            message = notifications.send_research_empty({"job_id": "research-job"})
        self.assertEqual(
            message["text"],
            "Research for founder.example.test has found nothing new for a few nights. "
            "Add seed topics or competitors so it has fresh ground to cover: https://chat.example.test/my-startup/settings",
        )
        self.assertIn('href="https://chat.example.test/my-startup/settings"', message["html_body"])
        self.assertIn("Edit research settings", message["html_body"])

    @override_settings(COMMUNITY_CHAT_FRONTEND_URL="https://chat.example.test")
    def test_points_pause_uses_the_same_amounts_and_credits_destination_in_both_parts(self):
        with patch.object(
            notifications, "_fan_out_event", side_effect=lambda **kw: kw["build_kwargs"](self.channel),
        ):
            message = notifications.send_automation_paused_points(self.run, required_points=6, current_balance=2)
        self.assertEqual(
            message["text"],
            "Daily research for founder.example.test is paused because there are not enough points. "
            "The next run uses 6 points and you have 2, so you need 4 more. "
            "Get points and it starts again on its own: https://chat.example.test/my-startup/credits",
        )
        self.assertIn("so you need 4 more", message["html_body"])
        self.assertIn('href="https://chat.example.test/my-startup/credits"', message["html_body"])

    def test_email_verification_keeps_signed_target_plain_fallback_and_resend_key(self):
        self.channel.verification_send_count = 0
        self.channel.save = Mock()
        target = "https://api.example.test/verify?token=signed"
        with (
            patch("integrations.services.notification_channels._enforce_send_limits"),
            patch("integrations.services.notification_channels.build_email_verification_url", return_value=target),
            patch("integrations.services.notification_channels._send_email", return_value=(True, "accepted", {})) as send,
        ):
            send_email_verification(self.channel)
        self.assertEqual(send.call_args.kwargs["idempotency_key"], "channel-verify:email-channel:1")
        self.assertIn(f"Confirm: {target}", send.call_args.kwargs["text"])
        self.assertIn(f'href="{target}"', send.call_args.kwargs["html_body"])
        self.assertIn("Confirm this email", send.call_args.kwargs["html_body"])

    @override_settings(CUSTOMERIO_API_KEY="api-test", CUSTOMERIO_FROM_EMAIL="notifications@example.test")
    def test_inline_fallback_is_branded_and_customerio_template_path_stays_provider_rendered(self):
        client = Mock()
        client.send_email.return_value = {"delivery_id": "accepted"}
        with (
            patch.object(notifications, "_customerio_client", return_value=client),
            patch.object(notifications.system_mail, "_record"),
        ):
            kwargs = dict(subject="Sample notice", text="Keep the <plain> fallback.", html_body=None,
                          idempotency_key="sample-notice")
            notifications._send_email(self.channel, **kwargs)
            notifications._send_email(
                self.channel, **kwargs, message_data={"sample": True}, transactional_message_id="template-3",
            )
        inline, hosted = [call.args[0] for call in client.send_email.call_args_list]
        self.assertEqual(inline["body_plain"], kwargs["text"])
        self.assertIn("Keep the &lt;plain&gt; fallback.", inline["body"])
        self.assertIn("mlai-roo.png", inline["body"])
        self.assertEqual(hosted["transactional_message_id"], "template-3")
        self.assertEqual(hosted["message_data"], {"sample": True})
        self.assertNotIn("body", hosted)

    @override_settings(FOUNDER_TOOLS_URL="https://chat.example.test")
    def test_performance_brief_keeps_metrics_and_destination(self):
        report = SimpleNamespace(
            organization=SimpleNamespace(domain="founder.example.test"), report_date=date(2026, 11, 28),
            payload={"headline": {"humanVisits": 48, "ctaClickers": 3}, "window": {"days": 14}},
        )
        html = _report_email_html(report)
        self.assertIn("mlai-roo.png", html)
        self.assertIn("Last 14 days:", html)
        self.assertIn("<strong>48</strong> visits", html)
        self.assertIn('href="https://chat.example.test/founder-tools/marketing#analytics"', html)

    @override_settings(RESEND_API_KEY="resend-test", COMMUNITY_CHAT_FRONTEND_URL="https://chat.example.test")
    def test_membership_approval_adds_the_chat_link_to_both_parts_and_keeps_delivery_key(self):
        user_model = Mock()
        user_model.objects.filter.return_value.only.return_value.first.return_value = SimpleNamespace(
            email="approved@example.test",
        )
        response = Mock(status_code=200)
        response.json.return_value = {"id": "accepted"}
        with (
            patch.object(onboarding_admin.cache, "add", return_value=True),
            patch.object(onboarding_admin, "get_user_model", return_value=user_model),
            patch.object(onboarding_admin.http_client, "post", return_value=response) as post,
            patch.object(onboarding_admin.system_mail, "_record"),
        ):
            self.assertTrue(onboarding_admin.send_review_cleared_email("member-1", 42))
        self.assertEqual(post.call_args.kwargs["headers"]["Idempotency-Key"], "chat-review-cleared/member-1")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["to"], ["approved@example.test"])
        self.assertIn("Open MLAI Chat: https://chat.example.test", payload["text"])
        self.assertIn("mlai-roo.png", payload["html"])
        self.assertIn('href="https://chat.example.test"', payload["html"])
