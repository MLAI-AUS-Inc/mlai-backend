"""Workshop-safe sign-in limits: rolling windows, trusted client IP, Retry-After."""

import uuid
from types import SimpleNamespace
from unittest.mock import patch

from coincurve import PrivateKey
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.exceptions import Throttled
from rest_framework.test import APITestCase

from community_chat.throttles import enforce_bootstrap_limits
from core.auth_throttles import (
    RateLimit,
    enforce_chat_email_code_request_limits,
    enforce_chat_email_code_verify_limits,
    enforce_rate_limits,
)
from core.client_ip import trusted_client_ip


LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
ORIGIN = "https://chat.mlai.au"
VENUE_IP = "198.51.100.7"


def request_from(ip=VENUE_IP, *, forwarded=None, user=None):
    meta = {"REMOTE_ADDR": "10.0.0.2"}
    meta["HTTP_X_FORWARDED_FOR"] = forwarded if forwarded is not None else ip
    return SimpleNamespace(
        META=meta,
        user=user or SimpleNamespace(pk=None, is_authenticated=False),
    )


class FakeClock:
    """Drive ``time.time`` so cache expiry and windows share one clock."""

    def __init__(self, start):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ClockedTestCase(SimpleTestCase):
    start = 1_800_000_000.0

    def setUp(self):
        cache.clear()
        self.clock = FakeClock(self.start)
        clock = patch("time.time", self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        self.addCleanup(cache.clear)


@override_settings(TRUSTED_PROXY_HOPS=1)
class TrustedClientIpTests(SimpleTestCase):
    def test_spoofed_leftmost_entries_do_not_change_the_ip(self):
        for spoof in ("1.2.3.4", "203.0.113.99, 8.8.8.8", "not-an-ip"):
            request = request_from(forwarded=f"{spoof}, {VENUE_IP}")
            self.assertEqual(trusted_client_ip(request), VENUE_IP)

    def test_single_entry_written_by_nginx_is_used(self):
        self.assertEqual(trusted_client_ip(request_from(forwarded=VENUE_IP)), VENUE_IP)

    def test_missing_or_malformed_header_falls_back_to_remote_addr(self):
        self.assertEqual(trusted_client_ip(request_from(forwarded="")), "10.0.0.2")
        self.assertEqual(trusted_client_ip(request_from(forwarded="1.2.3.4, junk")), "10.0.0.2")
        self.assertEqual(trusted_client_ip(SimpleNamespace(META={})), "unknown")

    @override_settings(TRUSTED_PROXY_HOPS=2)
    def test_hop_count_selects_the_entry_from_the_right(self):
        request = request_from(forwarded=f"1.2.3.4, {VENUE_IP}, 172.16.0.9")
        self.assertEqual(trusted_client_ip(request), VENUE_IP)
        self.assertEqual(trusted_client_ip(request_from(forwarded=VENUE_IP)), VENUE_IP)

    @override_settings(TRUSTED_PROXY_HOPS=0)
    def test_zero_hops_ignores_the_header(self):
        self.assertEqual(trusted_client_ip(request_from(forwarded="1.2.3.4")), "10.0.0.2")

    def test_ipv6_is_canonicalised(self):
        request = request_from(forwarded="2001:DB8:0:0::1")
        self.assertEqual(trusted_client_ip(request), "2001:db8::1")


@override_settings(CACHES=LOCMEM, TRUSTED_PROXY_HOPS=1)
class SlidingWindowTests(ClockedTestCase):
    window = 600
    # Ten seconds before a bucket boundary.
    start = 1_800_000_000.0 - (1_800_000_000 % 600) + 590

    def admit(self, value="key", limit=10, window=None):
        enforce_rate_limits([RateLimit("test", "dimension", value, limit, window or self.window)])

    def test_no_burst_at_a_bucket_boundary(self):
        for _ in range(10):
            self.admit()
        with self.assertRaises(Throttled):
            self.admit()
        # A fixed clock bucket would hand out ten more just after the boundary.
        self.clock.advance(15)
        admitted = 0
        for _ in range(10):
            try:
                self.admit()
            except Throttled:
                continue
            admitted += 1
        self.assertEqual(admitted, 0)

    def test_retry_after_is_an_integer_that_counts_down_and_is_honoured(self):
        for _ in range(10):
            self.admit()
        waits = []
        for _ in range(4):
            with self.assertRaises(Throttled) as caught:
                self.admit()
            waits.append(caught.exception.wait)
            self.clock.advance(7)
        self.assertTrue(all(isinstance(wait, int) and wait >= 1 for wait in waits))
        self.assertEqual(waits, sorted(waits, reverse=True))
        self.assertEqual(len(set(waits)), len(waits))
        with self.assertRaises(Throttled) as caught:
            self.admit()
        self.clock.advance(caught.exception.wait)
        self.admit()

    def test_rejected_attempts_do_not_extend_the_wait(self):
        for _ in range(10):
            self.admit()
        with self.assertRaises(Throttled) as first:
            self.admit()
        for _ in range(50):
            with self.assertRaises(Throttled):
                self.admit()
        with self.assertRaises(Throttled) as later:
            self.admit()
        self.assertEqual(later.exception.wait, first.exception.wait)

    def test_cooldown_reopens_exactly_after_its_window(self):
        self.admit(limit=1, window=60)
        self.clock.advance(59)
        with self.assertRaises(Throttled) as caught:
            self.admit(limit=1, window=60)
        self.assertEqual(caught.exception.wait, 1)
        self.clock.advance(1)
        self.admit(limit=1, window=60)

    def test_a_rejecting_dimension_leaves_the_others_uncounted(self):
        enforce_rate_limits([RateLimit("test", "ip", "full", 2, 600)])
        enforce_rate_limits([RateLimit("test", "ip", "full", 2, 600)])
        for _ in range(5):
            with self.assertRaises(Throttled):
                enforce_rate_limits([
                    RateLimit("test", "email", "member@example.com", 3, 600),
                    RateLimit("test", "ip", "full", 2, 600),
                ])
        for _ in range(3):
            enforce_rate_limits([RateLimit("test", "email", "member@example.com", 3, 600)])


@override_settings(CACHES=LOCMEM, TRUSTED_PROXY_HOPS=1)
class WorkshopLimitTests(ClockedTestCase):
    def test_sixty_founders_on_one_venue_ip_request_and_verify_without_a_429(self):
        founders = [(f"founder{index}@example.com", uuid.uuid4()) for index in range(60)]
        request = request_from(forwarded=VENUE_IP)
        for email, installation in founders:
            enforce_chat_email_code_request_limits(request, email, installation)
            self.clock.advance(2)
        # A third of the room taps "send again" once their cooldown is over.
        for email, installation in founders[:20]:
            enforce_chat_email_code_request_limits(request, email, installation)
            self.clock.advance(2)
        for _email, installation in founders:
            enforce_chat_email_code_verify_limits(request, uuid.uuid4(), installation)
            self.clock.advance(2)
        self.assertLess(self.clock.now - self.start, 600)

    def test_sixty_signed_in_founders_on_one_ip_enrol_their_devices(self):
        for index in range(60):
            user = SimpleNamespace(pk=1000 + index, is_authenticated=True)
            request = request_from(user=user)
            for action, ip_limit in (("challenge", 30), ("invite", 20), ("confirm", 40)):
                enforce_bootstrap_limits(
                    request,
                    action=action,
                    public_key=f"{index:064x}",
                    user_limit=10,
                    key_limit=5,
                    ip_limit=ip_limit,
                )
            self.clock.advance(5)

    def start_desktop_sign_in(self, public_key, *, ip_limit=30):
        enforce_bootstrap_limits(
            request_from(),
            action="auth-start",
            public_key=public_key,
            user_limit=20,
            key_limit=10,
            ip_limit=ip_limit,
        )

    def test_sixty_desktop_founders_on_one_ip_start_and_exchange(self):
        # The desktop handoff is anonymous: neither step has a signed-in user,
        # so the authenticated floor cannot be what lets the room through.
        for index in range(60):
            key = f"{index:064x}"
            self.start_desktop_sign_in(key)
            enforce_bootstrap_limits(
                request_from(),
                action="auth-exchange",
                public_key=key,
                user_limit=40,
                key_limit=30,
                ip_limit=60,
            )
            self.clock.advance(5)

    def test_one_desktop_key_is_still_bounded(self):
        key = "a" * 64
        for _ in range(10):
            self.start_desktop_sign_in(key)
        with self.assertRaises(Throttled):
            self.start_desktop_sign_in(key)

    def test_desktop_sign_in_keeps_a_per_ip_ceiling(self):
        for index in range(300):
            self.start_desktop_sign_in(f"{index:064x}")
        with self.assertRaises(Throttled):
            self.start_desktop_sign_in("f" * 64)

    @override_settings(AUTH_THROTTLE_IP_DEVICE_AUTH_PER_10M=5)
    def test_desktop_sign_in_floor_is_env_overridable_but_never_lowers_a_ceiling(self):
        for index in range(30):
            self.start_desktop_sign_in(f"{index:064x}")
        with self.assertRaises(Throttled):
            self.start_desktop_sign_in("f" * 64)

    def test_other_anonymous_bootstrap_actions_keep_their_ip_ceiling(self):
        for index in range(30):
            enforce_bootstrap_limits(
                request_from(),
                action="anonymous-test",
                public_key=f"{index:064x}",
                user_limit=20,
                key_limit=10,
                ip_limit=30,
            )
        with self.assertRaises(Throttled):
            enforce_bootstrap_limits(
                request_from(),
                action="anonymous-test",
                public_key="f" * 64,
                user_limit=20,
                key_limit=10,
                ip_limit=30,
            )

    @override_settings(AUTH_THROTTLE_IP_BOOTSTRAP_AUTHENTICATED_PER_10M=2)
    def test_an_ip_refusal_leaves_the_founders_own_budget_untouched(self):
        def invite(user_pk, key):
            enforce_bootstrap_limits(
                request_from(user=SimpleNamespace(pk=user_pk, is_authenticated=True)),
                action="invite",
                public_key=key,
                user_limit=3,
                key_limit=3,
                ip_limit=2,
            )

        invite(1, "1" * 64)
        invite(2, "2" * 64)
        # The venue IP is full: every retry is refused and must not be counted.
        for _ in range(10):
            with self.assertRaises(Throttled):
                invite(42, "4" * 64)
        # From another network the founder still has the full per-user and
        # per-key budget.
        for _ in range(3):
            enforce_bootstrap_limits(
                request_from(
                    "203.0.113.20",
                    user=SimpleNamespace(pk=42, is_authenticated=True),
                ),
                action="invite",
                public_key="4" * 64,
                user_limit=3,
                key_limit=3,
                ip_limit=10,
            )

    def test_per_email_limit_blocks_the_sixth_code_in_an_hour(self):
        email = "member@example.com"
        for _ in range(5):
            enforce_chat_email_code_request_limits(request_from(), email, uuid.uuid4())
            self.clock.advance(300)
        with self.assertRaises(Throttled) as caught:
            enforce_chat_email_code_request_limits(request_from(), email, uuid.uuid4())
        self.assertGreaterEqual(caught.exception.wait, 1)

    def test_email_and_installation_cooldown_is_kept(self):
        installation = uuid.uuid4()
        enforce_chat_email_code_request_limits(request_from(), "member@example.com", installation)
        self.clock.advance(30)
        with self.assertRaises(Throttled) as caught:
            enforce_chat_email_code_request_limits(request_from(), "member@example.com", installation)
        self.assertEqual(caught.exception.wait, 30)

    def test_per_installation_limit_is_kept(self):
        installation = uuid.uuid4()
        for index in range(10):
            enforce_chat_email_code_request_limits(request_from(), f"m{index}@example.com", installation)
        with self.assertRaises(Throttled):
            enforce_chat_email_code_request_limits(request_from(), "m10@example.com", installation)

    @override_settings(AUTH_THROTTLE_IP_CODE_REQUESTS_PER_HOUR=3)
    def test_spoofed_forwarded_for_cannot_mint_a_fresh_ip_budget(self):
        for index in range(3):
            enforce_chat_email_code_request_limits(
                request_from(forwarded=f"192.0.2.{index}, {VENUE_IP}"),
                f"m{index}@example.com",
                uuid.uuid4(),
            )
        with self.assertRaises(Throttled):
            enforce_chat_email_code_request_limits(
                request_from(forwarded=f"192.0.2.200, {VENUE_IP}"),
                "m3@example.com",
                uuid.uuid4(),
            )
        # A different real client is unaffected.
        enforce_chat_email_code_request_limits(
            request_from(forwarded="203.0.113.50"), "m4@example.com", uuid.uuid4()
        )


def public_key(private_int):
    return PrivateKey.from_int(private_int).public_key.format(compressed=True)[1:].hex()


@override_settings(
    CACHES=LOCMEM,
    TRUSTED_PROXY_HOPS=1,
    COMMUNITY_CHAT_ALLOWED_ORIGINS=[ORIGIN],
    COMMUNITY_CHAT_EMAIL_CODE_AUTH_ENABLED=True,
    COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET="test-delivery-secret",
    COMMUNITY_CHAT_EMAIL_CODE_MIN_RESPONSE_SECONDS=0,
    COMMUNITY_CHAT_EMAIL_CODE_PEPPER="test-code-pepper",
    COMMUNITY_CHAT_EMAIL_CODE_RESEND_SECONDS=60,
    COMMUNITY_CHAT_EMAIL_CODE_TTL_SECONDS=600,
)
class WorkshopSignInEndpointTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def request_code(self, email, installation_id, *, forwarded=VENUE_IP):
        return self.client.post(
            reverse("community_chat_email_code_request"),
            {
                "email": email,
                "client_id": "mlai-chat-web",
                "device": {
                    "installation_id": str(installation_id),
                    "public_key": public_key(41),
                    "platform": "web",
                    "name": "Chrome",
                },
            },
            format="json",
            HTTP_ORIGIN=ORIGIN,
            HTTP_X_FORWARDED_FOR=forwarded,
        )

    def verify(self, challenge_id, installation_id, *, forwarded=VENUE_IP):
        return self.client.post(
            reverse("community_chat_email_code_verify"),
            {
                "challenge_id": challenge_id,
                "code": "000000",
                "client_id": "mlai-chat-web",
                "installation_id": str(installation_id),
            },
            format="json",
            HTTP_ORIGIN=ORIGIN,
            HTTP_X_FORWARDED_FOR=forwarded,
        )

    def test_sixty_founders_from_one_ip_get_codes_and_reach_verify(self):
        statuses = []
        for index in range(60):
            installation = uuid.uuid4()
            requested = self.request_code(f"founder{index}@example.com", installation)
            statuses.append(requested.status_code)
            self.assertEqual(requested.status_code, status.HTTP_202_ACCEPTED, index)
            checked = self.verify(requested.data["challenge_id"], installation)
            statuses.append(checked.status_code)
            # A wrong code is a 400, never a rate limit.
            self.assertEqual(checked.status_code, status.HTTP_400_BAD_REQUEST, index)
        self.assertNotIn(status.HTTP_429_TOO_MANY_REQUESTS, statuses)

    def test_sixth_code_for_one_email_is_refused_with_retry_after(self):
        for _ in range(5):
            response = self.request_code("member@example.com", uuid.uuid4())
            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        blocked = self.request_code("member@example.com", uuid.uuid4())
        self.assertEqual(blocked.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        wait = int(blocked["Retry-After"])
        self.assertGreaterEqual(wait, 1)
        self.assertEqual(
            blocked.data["detail"],
            f"Too many authentication requests. Expected available in {wait} seconds.",
        )


DESKTOP_ORIGIN = "tauri://localhost"


@override_settings(
    CACHES=LOCMEM,
    TRUSTED_PROXY_HOPS=1,
    COMMUNITY_CHAT_ALLOWED_ORIGINS=[ORIGIN, DESKTOP_ORIGIN],
    COMMUNITY_CHAT_FRONTEND_URL=ORIGIN,
    COMMUNITY_CHAT_DEVICE_AUTH_ENABLED=True,
    COMMUNITY_CHAT_DEVICE_AUTH_TTL_SECONDS=900,
)
class WorkshopDesktopSignInEndpointTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def start(self, private_int, *, forwarded=VENUE_IP):
        return self.client.post(
            reverse("community_chat_auth_start"),
            {
                "client_id": "mlai-chat-desktop",
                "code_challenge": "c" * 43,
                "device": {
                    "installation_id": str(uuid.uuid4()),
                    "name": "MLAI Chat · Mac",
                    "platform": "macos",
                    "public_key": public_key(private_int),
                },
                "origin": DESKTOP_ORIGIN,
                "state": "state-" + "a" * 48,
            },
            format="json",
            HTTP_ORIGIN=DESKTOP_ORIGIN,
            HTTP_X_FORWARDED_FOR=forwarded,
        )

    def test_forty_desktop_founders_on_one_ip_can_start_sign_in(self):
        # Before the device-auth floor the 31st start from one venue IP in
        # ten minutes was refused with a 429.
        for index in range(40):
            response = self.start(100 + index)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED, (index, response.data))

    def test_one_desktop_key_is_still_refused_after_ten_starts(self):
        for _ in range(10):
            self.assertEqual(self.start(99).status_code, status.HTTP_201_CREATED)
        refused = self.start(99)
        self.assertEqual(refused.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertGreaterEqual(int(refused["Retry-After"]), 1)
