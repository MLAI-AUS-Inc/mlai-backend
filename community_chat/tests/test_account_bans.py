"""Database regressions; run only after approval of the account-ban migration."""

from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIClient

from core.email_utils import generate_magic_link
from core.models import AccountBan, User
from core.refresh_sessions import issue_refresh_token
from community_chat.account_sessions import (
    InvalidAccountSession,
    authenticate_access_token,
    issue_account_session,
    rotate_account_session,
)
from community_chat.account_bans import (
    ban_account,
    finish_ban_revocations,
    lift_account_ban,
)
from community_chat.adapter import MembershipAdapterUnavailable
from community_chat.models import (
    CommunityChatDevice,
    CommunityChatEmailCodeChallenge,
    Moderator,
)


class AccountBanTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_superuser("admin@example.test")
        self.user = User.objects.create_user(" Member@Example.Test ")
        self.devices = [
            CommunityChatDevice.objects.create(
                user=self.user, public_key=key * 64, status="verified"
            )
            for key in ("a", "b")
        ]

    def account_client(self, user, device):
        challenge = CommunityChatEmailCodeChallenge.objects.create(
            user=user, email_digest="c" * 64, code_digest="d" * 64,
            public_key=device.public_key, client_id="mlai-chat-ios",
            installation_id=device.installation_id, origin="mlaichat://callback",
            platform="ios", device_name="Synthetic test phone",
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        credentials = issue_account_session(user, challenge)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {credentials.access_token}")
        return client, credentials, challenge

    @patch(
        "community_chat.account_bans.revoke_relay_membership",
        return_value=("revoked", None),
    )
    def test_ban_preserves_email_revokes_every_device_and_blocks_reactivation(
        self, revoke
    ):
        stale_user = User.objects.get(pk=self.user.pk)
        ban = ban_account(actor=self.admin, user_id=self.user.pk)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertEqual(self.user.email, "member@example.test")
        self.assertEqual(self.user.auth_version, 2)
        self.assertFalse(ban.revocation_pending)
        self.assertEqual(
            {call.args[0] for call in revoke.call_args_list}, {"a" * 64, "b" * 64}
        )
        self.assertFalse(
            CommunityChatDevice.objects.filter(
                user=self.user, status="verified"
            ).exists()
        )
        with self.assertRaises(ValidationError):
            stale_user.save()
        with self.assertRaises(ProtectedError):
            self.user.delete()
        with self.assertRaises(ValidationError):
            User.objects.create_user("MEMBER@example.test")

    @patch(
        "community_chat.account_bans.revoke_relay_membership",
        side_effect=MembershipAdapterUnavailable("offline"),
    )
    def test_adapter_outage_keeps_account_disabled_and_retries(self, revoke):
        ban = ban_account(actor=self.admin, user_id=self.user.pk)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertTrue(ban.revocation_pending)
        revoke.side_effect = None
        revoke.return_value = ("revoked", None)
        finish_ban_revocations(ban.pk)
        ban.refresh_from_db()
        self.assertFalse(ban.revocation_pending)

    @patch(
        "community_chat.account_bans.revoke_relay_membership",
        return_value=("revoked", None),
    )
    def test_idempotency_and_unban_do_not_revive_sessions_or_devices(self, revoke):
        original_hash = self.user.get_session_auth_hash()
        ban = ban_account(actor=self.admin, user_id=self.user.pk)
        ban_account(actor=self.admin, user_id=self.user.pk)
        self.user.refresh_from_db()
        self.assertEqual(self.user.auth_version, 2)
        lift_account_ban(actor=self.admin, ban_id=ban.pk)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertEqual(self.user.auth_version, 3)
        self.assertNotEqual(original_hash, self.user.get_session_auth_hash())
        self.assertEqual(AccountBan.objects.count(), 1)
        self.assertFalse(
            CommunityChatDevice.objects.filter(
                user=self.user, status="verified"
            ).exists()
        )

    def test_moderator_self_and_admin_targets_are_protected(self):
        Moderator.objects.create(user=self.user)
        for actor, target in [(self.user, self.admin), (self.admin, self.admin)]:
            with self.assertRaises(PermissionDenied):
                ban_account(actor=actor, user_id=target.pk)

    @patch("community_chat.account_bans.revoke_relay_membership", return_value=("revoked", None))
    def test_existing_login_credentials_and_case_variant_signup_are_denied(self, revoke):
        refresh = issue_refresh_token(self.user)
        token = parse_qs(urlparse(generate_magic_link(self.user)).query)["token"][0]
        client, credentials, challenge = self.account_client(self.user, self.devices[0])
        stale_user = User.objects.get(pk=self.user.pk)
        ban = ban_account(actor=self.admin, user_id=self.user.pk)

        self.assertEqual(client.get(reverse("community_chat_account")).status_code, 401)
        with self.assertRaises(InvalidAccountSession):
            issue_account_session(stale_user, challenge)
        public = APIClient()
        response = public.get(reverse("verify_magic_link"), {"token": token, "app": "vibe-raising"})
        self.assertEqual(response.status_code, 403)
        response = public.post(reverse("create_user"), {"email": self.user.email.upper(), "app": "vibe-raising"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(User.objects.filter(email=self.user.email).count(), 1)

        jwt_client = APIClient()
        jwt_client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
        for lifted in (False, True):
            if lifted:
                lift_account_ban(actor=self.admin, ban_id=ban.pk)
            with self.subTest(lifted=lifted):
                self.assertEqual(jwt_client.get(reverse("current_user")).status_code, 401)
                self.assertEqual(public.post(reverse("token_refresh"), {"refresh": str(refresh)}, format="json").status_code, 401)
                with self.assertRaises(InvalidAccountSession):
                    authenticate_access_token(credentials.access_token)
                with self.assertRaises(InvalidAccountSession):
                    rotate_account_session(credentials.refresh_token)

    @patch("community_chat.account_bans.revoke_relay_membership", return_value=("revoked", None))
    def test_admin_api_bans_lists_retained_email_and_lifts(self, revoke):
        admin_device = CommunityChatDevice.objects.create(user=self.admin, public_key="c" * 64, status="verified")
        client, _, _ = self.account_client(self.admin, admin_device)
        url = reverse("community_chat_account_bans")
        response = client.post(url, {"public_key": self.devices[0].public_key, "reason": "Policy violation"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        ban_id = response.data["id"]
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.data["bans"][0]["email"], self.user.email)
        self.assertEqual(response.data["bans"][0]["reason"], "Policy violation")
        response = client.post(reverse("community_chat_account_ban", args=[ban_id]), {"enabled": False}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(client.get(url).data["bans"], [])
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_member_and_moderator_api_cannot_read_emails_or_ban(self):
        client, _, _ = self.account_client(self.user, self.devices[0])
        url = reverse("community_chat_account_bans")
        for moderator in (False, True):
            if moderator:
                Moderator.objects.create(user=self.user)
            with self.subTest(moderator=moderator):
                self.assertEqual(client.get(url).status_code, 403)
                self.assertEqual(client.post(url, {"public_key": self.devices[1].public_key}, format="json").status_code, 403)
        self.assertFalse(AccountBan.objects.exists())

    def test_database_rejects_case_insensitive_duplicate_retained_email(self):
        other = User.objects.create_user("other@example.test")
        AccountBan.objects.create(user=self.user, email=self.user.email)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AccountBan.objects.create(user=other, email=self.user.email.upper())
