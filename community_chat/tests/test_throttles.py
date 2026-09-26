from types import SimpleNamespace
from unittest.mock import call, patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from community_chat.throttles import enforce_bootstrap_limits
from community_chat.throttles import CommunityChatScopedThrottle
from community_chat.slack_views import SlackDmMirrorView, SlackOwnerConversationOpenView


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


class CommunityChatBootstrapThrottleTests(SimpleTestCase):
    def request(self, *, user, ip="203.0.113.10"):
        return SimpleNamespace(
            user=user,
            META={"REMOTE_ADDR": ip},
        )

    @patch("community_chat.throttles.enforce_dimension_limit")
    def test_anonymous_requests_do_not_share_a_global_none_user_bucket(self, enforce):
        request = self.request(
            user=SimpleNamespace(pk=None, is_authenticated=False),
        )

        enforce_bootstrap_limits(
            request,
            action="auth-start",
            public_key="a" * 64,
            user_limit=20,
            key_limit=10,
            ip_limit=30,
        )

        self.assertEqual(
            enforce.call_args_list,
            [
                call(
                    action="auth-start",
                    dimension="public-key",
                    value="a" * 64,
                    limit=10,
                    window_seconds=600,
                ),
                call(
                    action="auth-start",
                    dimension="ip",
                    value="203.0.113.10",
                    limit=30,
                    window_seconds=600,
                ),
            ],
        )

    @patch("community_chat.throttles.enforce_dimension_limit")
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

        self.assertEqual(enforce.call_args_list[0].kwargs["dimension"], "user")
        self.assertEqual(enforce.call_args_list[0].kwargs["value"], 42)
        self.assertEqual(len(enforce.call_args_list), 3)
