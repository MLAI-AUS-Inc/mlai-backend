from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.exceptions import TokenError

from core.account_bans import guard_account_save
from core.models import User
from core.refresh_sessions import ensure_token_auth_version
from community_chat.account_ban_views import AccountBanView


class AccountBanGuardTests(SimpleTestCase):
    def test_ban_blocks_reactivation_email_replacement_and_duplicate_account(self):
        with patch("core.account_bans.AccountBan.objects") as manager:
            manager.filter.return_value.first.return_value = SimpleNamespace(
                user_id=7, email="member@example.test"
            )
            for user in [
                SimpleNamespace(pk=7, email="member@example.test", is_active=True),
                SimpleNamespace(
                    pk=7, email="replacement@example.test", is_active=False
                ),
                SimpleNamespace(pk=None, email="member@example.test", is_active=False),
            ]:
                with self.subTest(user=user), self.assertRaises(ValidationError):
                    guard_account_save(user)

    def test_banned_record_can_be_saved_without_erasing_email(self):
        with patch("core.account_bans.AccountBan.objects") as manager:
            manager.filter.return_value.first.return_value = SimpleNamespace(
                user_id=7, email="member@example.test"
            )
            guard_account_save(
                SimpleNamespace(pk=7, email="member@example.test", is_active=False)
            )

    def test_lifted_ban_allows_reactivation(self):
        with patch("core.account_bans.AccountBan.objects") as manager:
            manager.filter.return_value.first.return_value = None
            guard_account_save(
                SimpleNamespace(pk=7, email="member@example.test", is_active=True)
            )

    def test_django_sessions_do_not_revive_after_ban_and_unban(self):
        user = User(email="member@example.test", password="synthetic", auth_version=1)
        original = user.get_session_auth_hash()
        user.auth_version = 2
        banned = user.get_session_auth_hash()
        user.auth_version = 3
        unbanned = user.get_session_auth_hash()
        self.assertEqual(len({original, banned, unbanned}), 3)

    def test_inactive_account_cannot_refresh_even_with_current_version(self):
        with self.assertRaises(TokenError):
            ensure_token_auth_version(
                SimpleNamespace(payload={"auth_version": 2}),
                user=SimpleNamespace(is_active=False, auth_version=2),
            )

    def test_moderators_and_members_cannot_manage_bans(self):
        request = SimpleNamespace(
            user=object(),
            community_chat_public_key="a" * 64,
            community_chat_installation_id="install",
        )
        for role in ["moderator", "member"]:
            with patch(
                "community_chat.account_ban_views.account_chat_role", return_value=role
            ), self.assertRaises(PermissionDenied):
                AccountBanView()._require_admin(request)
