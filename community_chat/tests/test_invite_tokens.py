"""P1.3 invite tokens: opaque, signed, short-lived, and never blocking sign-in."""

import base64
import time
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from community_chat.invite_tokens import (
    TOKEN_PREFIX, InvalidInviteToken, mint_invite_token, resolve_invite_token,
)
from community_chat.onboarding_views import InviteResolveThrottle
from community_chat.tests.test_account_profiles import credentials_for

RESOLVE_URL = "/api/v1/community-chat/account/invite/resolve/"
ON = dict(COMMUNITY_CHAT_INVITE_TOKENS_ENABLED=True)


class InviteTokenUnitTests(SimpleTestCase):
    def test_round_trip_carries_cohort_email_and_name(self):
        token = mint_invite_token(intent="startup", cohort="Workshop Nov 3", email="priya@example.com",
                                  name="Priya Shah")
        self.assertTrue(token.startswith(TOKEN_PREFIX))
        claims = resolve_invite_token(token)
        self.assertEqual(claims.public_payload(), {
            "intent": "startup", "cohort": "Workshop Nov 3", "email": "priya@example.com", "name": "Priya Shah",
        })

    def test_token_text_reveals_no_email_name_or_cohort(self):
        token = mint_invite_token(cohort="melb-workshop", email="priya@example.com", name="Priya Shah")
        lowered = token.lower()
        for secret in ("priya", "example.com", "shah", "melb-workshop", "@"):
            self.assertNotIn(secret, lowered)
        # Not even a base64 decode of the body shows them: Fernet encrypts.
        body = base64.urlsafe_b64decode(token[len(TOKEN_PREFIX):] + "==")
        self.assertNotIn(b"priya", body.lower())

    def test_tampered_expired_and_wrong_prefix_tokens_never_resolve(self):
        token = mint_invite_token(cohort="melb")
        tampered = token[:-6] + ("A" if token[-6] != "A" else "B") + token[-5:]
        expired = mint_invite_token(cohort="melb", ttl_hours=1, now=time.time() - 7200)
        for bad in (tampered, expired, "inv2." + token[len(TOKEN_PREFIX):], token[len(TOKEN_PREFIX):], "", None, 7):
            with self.subTest(bad=str(bad)[:12]), self.assertRaises(InvalidInviteToken):
                resolve_invite_token(bad)

    @override_settings(COMMUNITY_CHAT_INVITE_TOKEN_MAX_TTL_HOURS=48, COMMUNITY_CHAT_INVITE_TOKEN_DEFAULT_TTL_HOURS=24)
    def test_ttl_defaults_and_is_clamped_to_the_maximum(self):
        now = 1_800_000_000
        self.assertEqual(resolve_invite_token(mint_invite_token(cohort="a", now=now), now=now).expires_at,
                         now + 24 * 3600)
        self.assertEqual(resolve_invite_token(mint_invite_token(cohort="a", ttl_hours=10_000, now=now),
                                              now=now).expires_at, now + 48 * 3600)

    def test_key_is_bound_to_secret_key(self):
        token = mint_invite_token(cohort="melb")
        with override_settings(SECRET_KEY="another-secret-key-for-this-test-only-0123456789"):
            with self.assertRaises(InvalidInviteToken):
                resolve_invite_token(token)

    def test_bad_claims_are_refused_at_mint(self):
        for kwargs in ({"intent": "investor"}, {"cohort": "x" * 41}, {"cohort": "<script>"},
                       {"email": "not-an-email"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(InvalidInviteToken):
                mint_invite_token(**{"cohort": "melb", **kwargs})


class InviteResolveTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def resolve(self, token, **extra):
        return self.client.post(RESOLVE_URL, {"token": token}, format="json", **extra)

    def test_route_name_matches_the_contract_path(self):
        self.assertEqual(reverse("community_chat_invite_resolve"), RESOLVE_URL)

    def test_resolve_is_404_while_the_flag_is_off(self):
        response = self.resolve(mint_invite_token(cohort="melb"))
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("cohort", response.data)

    @override_settings(**ON)
    def test_resolve_is_anonymous_uncached_and_returns_prefill(self):
        token = mint_invite_token(intent="startup", cohort="melb", email="priya@example.com", name="Priya")
        response = self.resolve(token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.data, {"intent": "startup", "cohort": "melb", "email": "priya@example.com",
                                         "name": "Priya"})
        cohort_only = self.resolve(mint_invite_token(intent="startup", cohort="melb")).data
        self.assertEqual(cohort_only, {"intent": "startup", "cohort": "melb", "email": None, "name": None})

    @override_settings(**ON)
    def test_invalid_tokens_get_the_expired_copy(self):
        expired = mint_invite_token(cohort="melb", ttl_hours=1, now=time.time() - 7200)
        for bad in (expired, "inv1.garbage", "nope", "x" * 3000):
            with self.subTest(bad=bad[:12]):
                response = self.resolve(bad)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data, {"detail": "This invite link has expired.", "code": "invite_invalid"})
                self.assertEqual(response["Cache-Control"], "no-store")

    @override_settings(**ON)
    def test_resolve_is_throttled_per_ip(self):
        token = mint_invite_token(cohort="melb")
        with patch.object(InviteResolveThrottle, "THROTTLE_RATES", {"community_chat_invite_resolve": "2/minute"}):
            statuses = [self.resolve(token, REMOTE_ADDR="203.0.113.9").status_code for _ in range(3)]
            other_ip = self.resolve(token, REMOTE_ADDR="203.0.113.10").status_code
        self.assertEqual(statuses, [200, 200, 429])
        self.assertEqual(other_ip, 200)


@override_settings(
    COMMUNITY_CHAT_SIGNUP_ENABLED=True,
    COMMUNITY_CHAT_MEMBERSHIP_POLICY_VERSION="test-v1",
    COMMUNITY_CHAT_ALLOWED_ORIGINS=["https://chat.mlai.au", "mlaichat://callback"],
    COMMUNITY_CHAT_ONE_STEP_JOIN_ENABLED=True,
    COMMUNITY_CHAT_INVITE_TOKENS_ENABLED=True,
)
class InviteJoinTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(email="invitee@example.com",
                                                         email_verified_at=timezone.now())
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {credentials_for(self.user).access_token}")

    def join(self, invite):
        return self.client.put(reverse("community_chat_onboarding"), {
            "step": "join", "first_name": "Priya", "last_name": "", "adult_and_terms": True,
            "policy_version": "test-v1", "intent": "startup", "invite": invite,
        }, format="json")

    def test_valid_invite_logs_its_cohort_with_ids_only(self):
        token = mint_invite_token(intent="startup", cohort="melb", email="invitee@example.com", name="Priya")
        with self.assertLogs("community_chat.onboarding", level="INFO") as logs:
            self.assertEqual(self.join(token).status_code, 200)
        line = next(line for line in logs.output if "community_chat_invite_joined" in line)
        self.assertIn(f"user={self.user.pk}", line)
        self.assertIn("cohort=melb", line)
        self.assertNotIn("invitee@example.com", line)
        self.assertNotIn("Priya", line)

    def test_invalid_invite_never_blocks_joining(self):
        with self.assertLogs("community_chat.onboarding", level="INFO") as logs:
            response = self.join("inv1.not-a-token")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["onboarding"]["status"], "approved")
        self.assertFalse(any("community_chat_invite_joined" in line for line in logs.output))


class MintChatInviteCommandTests(SimpleTestCase):
    def test_prints_an_opaque_startup_link(self):
        out, err = StringIO(), StringIO()
        call_command("mint_chat_invite", "--intent", "startup", "--cohort", "melb", "--email",
                     "priya@example.com", "--name", "Priya", stdout=out, stderr=err)
        link = out.getvalue().strip()
        self.assertTrue(link.startswith("https://chat.mlai.au/?intent=startup&invite=inv1."))
        self.assertNotIn("priya", link.lower())
        self.assertNotIn("melb", link)
        token = link.split("invite=", 1)[1]
        self.assertEqual(resolve_invite_token(token).email, "priya@example.com")

    @override_settings(COMMUNITY_CHAT_INVITE_TOKEN_MAX_TTL_HOURS=336)
    def test_ttl_is_capped_at_the_maximum(self):
        out, err = StringIO(), StringIO()
        before = time.time()
        call_command("mint_chat_invite", "--intent", "startup", "--cohort", "melb", "--ttl-hours", "9999",
                     stdout=out, stderr=err)
        token = out.getvalue().strip().split("invite=", 1)[1]
        self.assertLessEqual(resolve_invite_token(token).expires_at, before + 336 * 3600 + 5)
        self.assertIn("capped at 336 hours", err.getvalue())
