from types import SimpleNamespace
from unittest.mock import call, patch

from django.test import SimpleTestCase

from community_chat.throttles import enforce_bootstrap_limits


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
