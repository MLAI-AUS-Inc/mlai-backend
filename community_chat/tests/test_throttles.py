from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from community_chat.throttles import (
    COMMUNITY_CHAT_RATE_NAMESPACE,
    COMMUNITY_CHAT_THROTTLE_DETAIL,
    enforce_bootstrap_limits,
)
from community_chat.throttles import CommunityChatScopedThrottle
from community_chat.slack_views import SlackDmMirrorView, SlackOwnerConversationOpenView, SlackUserDirectoryView
from core.auth_throttles import RateLimit


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class SlackPollingThrottleTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        rates = patch.object(CommunityChatScopedThrottle, "THROTTLE_RATES", {
            "community_chat_slack_snapshot_device": "2/minute",
            "community_chat_slack_snapshot_account": "5/minute",
            "community_chat_slack_read_receipt": "2/minute",
            "community_chat_slack_open": "2/minute",
            "community_chat_home": "1/minute",
            "community_chat_slack_directory": "2/minute",
        })
        rates.start()
        self.addCleanup(rates.stop)
        self.addCleanup(cache.clear)

    def allowed(self, *, device="a" * 64, user=1, method="GET", action=None):
        request = SimpleNamespace(
            method=method, data={"action": action},
            user=SimpleNamespace(pk=user, is_authenticated=True),
            community_chat_public_key=device,
        )
        view = SlackDmMirrorView()
        view.request = request
        decisions = [throttle.allow_request(request, view) for throttle in view.get_throttles()]
        return all(decisions)

    def test_busy_device_does_not_block_other_devices_or_read_acknowledgements(self):
        self.assertTrue(self.allowed())
        self.assertTrue(self.allowed())
        self.assertFalse(self.allowed())
        self.assertTrue(self.allowed(device="b" * 64))
        self.assertTrue(self.allowed(method="PATCH", action="mark_read"))
        self.assertTrue(self.allowed(method="PATCH", action="mark_read"))
        self.assertFalse(self.allowed(method="PATCH", action="mark_read"))

    def test_rotating_devices_cannot_bypass_the_account_ceiling(self):
        for index in range(5):
            self.assertTrue(self.allowed(device=f"{index:064x}"))
        self.assertFalse(self.allowed(device="f" * 64))
        self.assertTrue(self.allowed(device="f" * 64, user=2))
        self.assertTrue(self.allowed(method="PATCH", action="mark_read"))

    def test_legacy_sessions_share_a_bounded_account_fallback(self):
        self.assertTrue(self.allowed(device=None))
        self.assertTrue(self.allowed(device=None))
        self.assertFalse(self.allowed(device=None))

    def test_foreground_open_keeps_its_budget_when_background_and_home_are_full(self):
        self.assertTrue(self.allowed())
        self.assertTrue(self.allowed())
        self.assertFalse(self.allowed())
        self.assertTrue(self.allowed(method="PATCH", action="pause"))
        request = SimpleNamespace(user=SimpleNamespace(pk=1, is_authenticated=True))
        view = SlackOwnerConversationOpenView()
        decisions = [CommunityChatScopedThrottle().allow_request(request, view) for _ in range(3)]
        self.assertEqual(decisions, [True, True, False])

    def test_connection_controls_retain_their_existing_limit(self):
        self.assertTrue(self.allowed(method="PATCH", action="pause"))
        self.assertFalse(self.allowed(method="PATCH", action="resume"))
        self.assertTrue(self.allowed())
        self.assertTrue(self.allowed(method="PATCH", action="mark_read"))

    def test_directory_get_has_separate_account_budget_and_precise_retry_after(self):
        from rest_framework.exceptions import Throttled
        request = SimpleNamespace(method="GET", user=SimpleNamespace(pk=1, is_authenticated=True))
        view = SlackUserDirectoryView()
        view.request = request
        self.assertEqual([all(t.allow_request(request, view) for t in view.get_throttles()) for _ in range(3)], [True, True, False])
        self.assertTrue(self.allowed(method="PATCH", action="refresh_channel"))
        result = view.handle_exception(Throttled(wait=7))
        self.assertEqual(result.data, {"error": "slack_directory_rate_limited", "retry_after_seconds": 7})
        self.assertEqual(result["Retry-After"], "7")
        view = SlackUserDirectoryView()
        view.request = SimpleNamespace(method="POST")
        view.get_throttles()
        self.assertEqual(view.community_chat_throttle_scope, "community_chat_home")


class CommunityChatBootstrapThrottleTests(SimpleTestCase):
    def request(self, *, user, ip="203.0.113.10"):
        return SimpleNamespace(
            user=user,
            META={"REMOTE_ADDR": ip},
        )

    @patch("community_chat.throttles.enforce_rate_limits")
    def test_anonymous_requests_do_not_share_a_global_none_user_bucket(self, enforce):
        request = self.request(
            user=SimpleNamespace(pk=None, is_authenticated=False),
        )

        enforce_bootstrap_limits(
            request,
            action="anonymous-test",
            public_key="a" * 64,
            user_limit=20,
            key_limit=10,
            ip_limit=30,
        )

        enforce.assert_called_once()
        self.assertEqual(
            enforce.call_args.args[0],
            [
                RateLimit("anonymous-test", "public-key", "a" * 64, 10, 600),
                RateLimit("anonymous-test", "ip", "203.0.113.10", 30, 600),
            ],
        )
        self.assertEqual(enforce.call_args.kwargs["namespace"], COMMUNITY_CHAT_RATE_NAMESPACE)
        self.assertEqual(enforce.call_args.kwargs["detail"], COMMUNITY_CHAT_THROTTLE_DETAIL)

    @patch("community_chat.throttles.enforce_rate_limits")
    def test_desktop_sign_in_handoff_gets_a_room_sized_ip_ceiling(self, enforce):
        request = self.request(
            user=SimpleNamespace(pk=None, is_authenticated=False),
        )

        for action, ip_limit in (("auth-start", 30), ("auth-exchange", 60)):
            enforce_bootstrap_limits(
                request,
                action=action,
                public_key="a" * 64,
                user_limit=20,
                key_limit=10,
                ip_limit=ip_limit,
            )
            limits = enforce.call_args.args[0]
            self.assertEqual([limit.dimension for limit in limits], ["public-key", "ip"])
            self.assertEqual(limits[0].limit, 10)
            self.assertEqual(limits[1].limit, 300)

    @patch("community_chat.throttles.enforce_rate_limits")
    def test_authenticated_requests_keep_the_per_user_limit(self, enforce):
        request = self.request(
            user=SimpleNamespace(pk=42, is_authenticated=True),
        )

        enforce_bootstrap_limits(
            request,
            action="bootstrap",
            public_key="b" * 64,
            user_limit=12,
            key_limit=8,
            ip_limit=24,
        )

        enforce.assert_called_once()
        limits = enforce.call_args.args[0]
        self.assertEqual([limit.dimension for limit in limits], ["user", "public-key", "ip"])
        self.assertEqual((limits[0].value, limits[0].limit), (42, 12))
