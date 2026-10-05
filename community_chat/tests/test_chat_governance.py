"""Account-level authority, one owner, and appointment/revocation boundaries."""

from io import StringIO
from unittest.mock import patch

from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat.account_bans import ban_account, lift_account_ban
from community_chat.models import ChatRole, CommunityChatDevice, Moderator
from community_chat.permission_views import (
    ChatAdminView,
    ChatMemberRolesView,
    ChatModeratorView,
)
from community_chat.permissions import (
    chat_role,
    device_chat_role,
    device_protection_role,
    role_capabilities,
)
from core.models import AccountBan
from roo.models import PointsAdmin


class ChatGovernanceTests(TestCase):
    def setUp(self):
        self.users = [
            get_user_model().objects.create_user(email=f"{name}@example.test")
            for name in ("owner", "admin", "member", "other-admin")
        ]
        self.owner, self.admin, self.member, self.other_admin = self.users
        self.devices = [
            CommunityChatDevice.objects.create(
                user=user,
                public_key=letter * 64,
                status="verified",
            )
            for user, letter in zip(self.users, "abcd")
        ]
        ChatRole.objects.create(user=self.owner, role="owner")
        ChatRole.objects.create(user=self.admin, role="admin")
        ChatRole.objects.create(user=self.other_admin, role="admin")
        self.factory = APIRequestFactory()

    def request(self, actor, data):
        device = next(d for d in self.devices if d.user_id == actor.pk)
        request = self.factory.post("/admins/", data, format="json")
        request.community_chat_public_key = device.public_key
        request.community_chat_installation_id = device.installation_id
        force_authenticate(request, user=actor)
        return request

    def appoint(self, actor, target, enabled):
        device = next(d for d in self.devices if d.user_id == target.pk)
        return ChatAdminView.as_view()(
            self.request(actor, {"enabled": enabled}), public_key=device.public_key
        )

    def test_only_owner_can_appoint_and_demote_admins_on_all_devices(self):
        another_device = CommunityChatDevice.objects.create(
            user=self.member,
            public_key="e" * 64,
            status="verified",
        )
        for actor in (self.member, self.admin, self.other_admin):
            self.assertEqual(self.appoint(actor, self.member, True).status_code, 403)
        self.assertEqual(self.appoint(self.owner, self.member, True).status_code, 200)
        self.assertEqual(device_chat_role(another_device.public_key), "admin")
        self.assertEqual(self.appoint(self.admin, self.member, False).status_code, 403)
        self.assertEqual(self.appoint(self.owner, self.member, False).status_code, 200)
        self.assertEqual(device_chat_role(another_device.public_key), "member")
        self.assertEqual(LogEntry.objects.filter(user=self.owner).count(), 2)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_staff)
        self.assertFalse(self.member.is_superuser)

    def test_owner_is_unique_and_cannot_demote_self(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            ChatRole.objects.create(user=self.member, role="owner")
        self.assertEqual(self.appoint(self.owner, self.owner, False).status_code, 403)
        self.assertEqual(ChatRole.objects.filter(role="owner").count(), 1)

    def test_revoked_or_mismatched_owner_installation_cannot_change_roles(self):
        request = self.request(self.owner, {"enabled": True})
        request.community_chat_installation_id = self.devices[1].installation_id
        self.assertEqual(
            ChatAdminView.as_view()(
                request, public_key=self.devices[2].public_key
            ).status_code,
            403,
        )
        self.devices[0].status = "revoked"
        self.devices[0].save()
        self.assertEqual(self.appoint(self.owner, self.member, True).status_code, 403)

    def test_roster_and_capabilities_include_owner(self):
        keys = [d.public_key for d in self.devices]
        response = ChatMemberRolesView.as_view()(
            self.request(self.owner, {"public_keys": keys})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(response.data["roles"].values()), ["owner", "admin", "member", "admin"]
        )
        self.assertTrue(role_capabilities("owner")["can_manage_admins"])
        self.assertFalse(role_capabilities("admin")["can_manage_admins"])
        self.assertTrue(role_capabilities("owner")["can_moderate"])

    @patch(
        "community_chat.account_bans.revoke_relay_membership",
        return_value=("revoked", None),
    )
    def test_admin_cannot_ban_or_unban_fellow_admin(self, revoke):
        with self.assertRaises(PermissionDenied):
            ban_account(actor=self.admin, user_id=self.other_admin.pk)
        ban = ban_account(actor=self.owner, user_id=self.other_admin.pk)
        with self.assertRaises(PermissionDenied):
            lift_account_ban(actor=self.admin, ban_id=ban.pk)
        lift_account_ban(actor=self.owner, ban_id=ban.pk)
        self.other_admin.refresh_from_db()
        self.assertTrue(self.other_admin.is_active)
        self.assertEqual(chat_role(self.other_admin), "admin")

    def test_owner_cannot_be_banned(self):
        for actor in (self.owner, self.admin):
            with self.assertRaises(PermissionDenied):
                ban_account(actor=actor, user_id=self.owner.pk)
        self.assertFalse(AccountBan.objects.exists())

    def test_admin_cannot_change_peer_or_owner_moderator_appointments(self):
        for target in (self.owner, self.other_admin):
            for enabled in (False, True):
                request = self.request(self.admin, {"enabled": enabled})
                device = next(d for d in self.devices if d.user_id == target.pk)
                response = ChatModeratorView.as_view()(
                    request, public_key=device.public_key
                )
                self.assertEqual(response.status_code, 403)
        self.assertFalse(Moderator.objects.exists())

    def test_revoked_admin_target_stays_protected_without_retaining_authority(self):
        device = self.devices[1]
        device.status = "revoked"
        device.save()
        self.admin.is_active = False
        self.admin.save()
        self.assertEqual(device_chat_role(device.public_key), "member")
        self.assertEqual(device_protection_role(device.public_key), "admin")
        # A reassigned key protects its current account, never its former owner.
        CommunityChatDevice.objects.create(
            user=self.member, public_key=device.public_key, status="verified"
        )
        self.assertEqual(device_protection_role(device.public_key), "member")

    def test_bootstrap_preview_is_read_only_and_conflicting_roster_is_refused(self):
        output = StringIO()
        call_command(
            "bootstrap_chat_roles",
            owner_user_id=self.owner.pk,
            admin_user_id=[self.admin.pk, self.other_admin.pk],
            stdout=output,
        )
        self.assertIn("Preview only", output.getvalue())
        self.assertEqual(ChatRole.objects.count(), 3)
        with self.assertRaises(CommandError):
            call_command(
                "bootstrap_chat_roles", owner_user_id=self.admin.pk, apply=True
            )
        self.assertEqual(chat_role(self.owner), "owner")

    def test_bootstrap_apply_is_idempotent_without_backend_or_roo_privileges(self):
        ChatRole.objects.all().delete()
        points_admin = PointsAdmin.objects.create(
            user=self.admin, slack_user_id="SYNTHETIC_COMMITTEE", role="committee"
        )
        original_points = PointsAdmin.objects.values().get(pk=points_admin.pk)
        args = {
            "owner_user_id": self.owner.pk,
            "admin_user_id": [self.admin.pk, self.other_admin.pk, self.member.pk],
            "stdout": StringIO(),
        }
        call_command("bootstrap_chat_roles", **args)
        self.assertFalse(ChatRole.objects.exists())
        for _ in range(2):
            call_command("bootstrap_chat_roles", apply=True, **args)
        self.assertEqual(ChatRole.objects.filter(role="owner").count(), 1)
        self.assertEqual(ChatRole.objects.filter(role="admin").count(), 3)
        self.assertEqual(LogEntry.objects.count(), 4)
        self.assertEqual(
            PointsAdmin.objects.values().get(pk=points_admin.pk), original_points
        )
        for user in self.users:
            user.refresh_from_db()
            self.assertFalse(user.is_staff)
            self.assertFalse(user.is_superuser)

    def test_bootstrap_missing_verified_installation_does_not_partially_appoint(self):
        ChatRole.objects.all().delete()
        self.devices[1].status = "pending"
        self.devices[1].save()
        with self.assertRaises(CommandError):
            call_command(
                "bootstrap_chat_roles",
                owner_user_id=self.owner.pk,
                admin_user_id=[self.admin.pk],
                apply=True,
                stdout=StringIO(),
            )
        self.assertFalse(ChatRole.objects.exists())

    def test_ownership_transfer_requires_expected_owner_and_preserves_one_owner(self):
        for apply in (False, True):
            with self.assertRaises(CommandError):
                call_command(
                    "transfer_chat_owner",
                    current_owner_user_id=self.admin.pk,
                    new_owner_user_id=self.member.pk,
                    apply=apply,
                    stdout=StringIO(),
                )
        call_command(
            "transfer_chat_owner",
            current_owner_user_id=self.owner.pk,
            new_owner_user_id=self.member.pk,
            stdout=StringIO(),
        )
        self.assertEqual(chat_role(self.owner), "owner")
        call_command(
            "transfer_chat_owner",
            current_owner_user_id=self.owner.pk,
            new_owner_user_id=self.member.pk,
            apply=True,
            stdout=StringIO(),
        )
        self.assertEqual(chat_role(self.owner), "admin")
        self.assertEqual(chat_role(self.member), "owner")
        self.assertEqual(ChatRole.objects.filter(role="owner").count(), 1)
