"""Chat authority regressions for verified devices and separate account roles."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.admin.models import LogEntry
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from community_chat.account_sessions import issue_account_session
from community_chat.models import (
    CommunityChatDevice,
    CommunityChatEmailCodeChallenge,
    Moderator,
)
from community_chat.permission_views import (
    ChatModeratorView,
    ChatMemberRolesView,
    RelayChatRoleView,
)
from community_chat.permissions import chat_role, device_chat_role, role_capabilities
from roo.models import PointsAdmin
from roo.permissions import is_points_admin_user


@override_settings(COMMUNITY_CHAT_ROLE_SERVICE_TOKEN="test-role-service-" + "x" * 32)
class ChatPermissionsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="member@example.test")
        self.admin = get_user_model().objects.create_user(email="admin@example.test")
        PointsAdmin.objects.create(
            user=self.admin, slack_user_id="UTESTADMIN", role="committee"
        )
        self.key, self.admin_key = "a" * 64, "b" * 64
        self.device = CommunityChatDevice.objects.create(
            user=self.user, public_key=self.key, status="verified"
        )
        CommunityChatDevice.objects.create(
            user=self.admin, public_key=self.admin_key, status="verified"
        )
        self.factory = APIRequestFactory()

    def appoint(self, actor, key, enabled, *, actor_key=None):
        request = self.factory.post("/moderators/", {"enabled": enabled}, format="json")
        request.community_chat_public_key = actor_key or (
            self.admin_key if actor == self.admin else self.key
        )
        request.community_chat_installation_id = CommunityChatDevice.objects.get(
            user=actor
        ).installation_id
        force_authenticate(request, user=actor)
        return ChatModeratorView.as_view()(request, public_key=key)

    def test_moderator_is_independent_of_every_full_admin_class(self):
        Moderator.objects.create(user=self.user)
        self.assertEqual(chat_role(self.user), "moderator")
        self.assertEqual(device_chat_role(self.key), "moderator")
        self.assertFalse(is_points_admin_user(self.user))
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)
        self.assertFalse(PointsAdmin.objects.filter(user=self.user).exists())
        self.assertEqual(
            role_capabilities("moderator"),
            {
                "role": "moderator",
                "can_create_channels": True,
                "can_mention_channel": True,
                "can_manage_channels": False,
                "can_manage_members": False,
                "can_moderate": False,
            },
        )

    def test_only_active_admin_and_committee_roles_map_to_chat_admin(self):
        record = PointsAdmin.objects.get(user=self.admin)
        for role in ("admin", "committee", "portfolio_lead", "partner"):
            record.role = role
            record.save()
            self.assertEqual(
                chat_role(self.admin),
                "admin" if role in ("admin", "committee") else "member",
            )
        record.role, record.is_active = "admin", False
        record.save()
        self.assertEqual(chat_role(self.admin), "member")

    def test_revocation_and_unverified_bindings_remove_authority_immediately(self):
        appointment = Moderator.objects.create(user=self.user)
        for status in ("pending", "revoked"):
            self.device.status = status
            self.device.save()
            self.assertEqual(device_chat_role(self.key), "member")
        self.device.status = "verified"
        self.device.save()
        appointment.is_active = False
        appointment.save()
        self.assertEqual(device_chat_role(self.key), "member")
        appointment.is_active = True
        appointment.save()
        self.user.is_active = False
        self.user.save()
        self.assertEqual(device_chat_role(self.key), "member")

    def test_committee_can_appoint_and_revoke_with_audit(self):
        self.assertEqual(self.appoint(self.admin, self.key, True).status_code, 200)
        self.assertEqual(device_chat_role(self.key), "moderator")
        self.assertEqual(self.appoint(self.admin, self.key, False).status_code, 200)
        self.assertEqual(device_chat_role(self.key), "member")
        self.assertEqual(LogEntry.objects.filter(user=self.admin).count(), 2)

    def test_moderator_cannot_appoint_or_read_admin_roster(self):
        Moderator.objects.create(user=self.user)
        self.assertEqual(self.appoint(self.user, self.admin_key, True).status_code, 403)
        request = self.factory.post(
            "/member-roles/", {"public_keys": [self.admin_key]}, format="json"
        )
        request.community_chat_public_key = self.key
        request.community_chat_installation_id = self.device.installation_id
        force_authenticate(request, user=self.user)
        self.assertEqual(ChatMemberRolesView.as_view()(request).status_code, 403)

    def test_protects_admin_self_unknown_keys_and_invalid_input(self):
        self.assertEqual(
            self.appoint(self.admin, self.admin_key, False).status_code, 403
        )
        self.assertEqual(self.appoint(self.admin, "c" * 64, True).status_code, 404)
        self.assertEqual(self.appoint(self.admin, self.key, "true").status_code, 400)
        self.assertEqual(
            self.appoint(self.admin, self.key, True, actor_key="c" * 64).status_code,
            403,
        )

    def test_role_service_requires_separate_credential_and_exposes_only_role(self):
        view = RelayChatRoleView.as_view()
        request = self.factory.get("/relay-roles/")
        self.assertEqual(view(request, public_key=self.admin_key).status_code, 403)
        request = self.factory.get(
            "/relay-roles/", HTTP_AUTHORIZATION="Bearer test-role-service-" + "x" * 32
        )
        response = view(request, public_key=self.admin_key)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.data), {"role", "public_key", "relay_url"})
        self.assertEqual(response.data["role"], "admin")
        self.assertEqual(response["Cache-Control"], "no-store")

    def account_client(self, user, key):
        device = CommunityChatDevice.objects.get(public_key=key)
        challenge = CommunityChatEmailCodeChallenge.objects.create(
            user=user,
            email_digest="c" * 64,
            code_digest="d" * 64,
            public_key=key,
            client_id="mlai-chat-web",
            installation_id=device.installation_id,
            origin="https://chat.mlai.au",
            platform="web",
            device_name="Test browser",
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        credentials = issue_account_session(user, challenge)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {credentials.access_token}")
        return client, credentials

    def test_permissions_route_uses_authenticated_device_and_rechecks_revocation(self):
        Moderator.objects.create(user=self.user)
        client, _ = self.account_client(self.user, self.key)
        response = client.get(
            reverse("community_chat_permissions"), {"public_key": self.admin_key}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["public_key"], self.key)
        self.assertEqual(response.data["role"], "moderator")
        self.assertFalse(response.data["can_manage_members"])
        self.assertEqual(response["Cache-Control"], "no-store")
        self.device.revoked_at = timezone.now()
        self.device.save(update_fields=["revoked_at"])
        response = client.get(reverse("community_chat_permissions"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["role"], "member")
        self.assertFalse(response.data["can_create_channels"])

    def test_admin_roster_is_bounded_and_contains_only_live_roles(self):
        Moderator.objects.create(user=self.user)
        client, _ = self.account_client(self.admin, self.admin_key)
        url = reverse("community_chat_member_roles")
        keys = [self.key, self.admin_key, "c" * 64]
        response = client.post(url, {"public_keys": keys}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data, {"roles": dict(zip(keys, ["moderator", "admin", "member"]))}
        )
        self.assertEqual(response["Cache-Control"], "no-store")
        for payload in (
            [self.key],
            {"public_keys": [self.key] * 201},
            {"public_keys": [False]},
        ):
            self.assertEqual(client.post(url, payload, format="json").status_code, 400)

    @override_settings(COMMUNITY_CHAT_ALLOWED_ORIGINS=["https://chat.mlai.au"])
    def test_moderator_write_requires_account_auth_and_cookie_origin(self):
        _, credentials = self.account_client(self.admin, self.admin_key)
        url = reverse("community_chat_moderator", kwargs={"public_key": self.key})
        client = APIClient()
        self.assertEqual(
            client.post(url, {"enabled": True}, format="json").status_code, 401
        )
        client.cookies["mlai_chat_access"] = credentials.access_token
        self.assertEqual(
            client.post(
                url,
                {"enabled": True},
                format="json",
                HTTP_ORIGIN="https://other.example",
            ).status_code,
            401,
        )
        self.assertFalse(Moderator.objects.exists())
        response = client.post(
            url, {"enabled": True}, format="json", HTTP_ORIGIN="https://chat.mlai.au"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(device_chat_role(self.key), "moderator")
        self.assertEqual(
            client.post(
                url, [], format="json", HTTP_ORIGIN="https://chat.mlai.au"
            ).status_code,
            400,
        )

    def test_role_service_token_cannot_appoint_moderators(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer test-role-service-" + "x" * 32)
        response = client.post(
            reverse("community_chat_moderator", kwargs={"public_key": self.key}),
            {"enabled": True},
            format="json",
        )
        self.assertEqual(response.status_code, 401)
        self.assertFalse(Moderator.objects.exists())

    def test_session_cannot_inherit_another_accounts_verified_key_role(self):
        client, credentials = self.account_client(self.user, self.key)
        # Even a stale session retaining a key that was later rebound must not
        # resolve the new owner's administrator authority.
        admin_device = CommunityChatDevice.objects.get(public_key=self.admin_key)
        credentials.session.public_key = self.admin_key
        credentials.session.installation_id = admin_device.installation_id
        credentials.session.save(update_fields=["public_key", "installation_id"])
        response = client.get(reverse("community_chat_permissions"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["role"], "member")
        response = client.post(
            reverse("community_chat_moderator", kwargs={"public_key": self.key}),
            {"enabled": True},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Moderator.objects.exists())

    def test_account_permission_requires_matching_installation(self):
        client, credentials = self.account_client(self.admin, self.admin_key)
        credentials.session.installation_id = self.device.installation_id
        credentials.session.save(update_fields=["installation_id"])
        response = client.get(reverse("community_chat_permissions"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["role"], "member")
