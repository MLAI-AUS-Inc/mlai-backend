"""Owned device management must never become arbitrary-key revocation."""

import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from agent_runtime.models import AgentInstallation
from community_chat.account_sessions import issue_account_session
from community_chat.models import CommunityChatDevice, DeviceBindingStatus


@override_settings(COMMUNITY_CHAT_SIGNUP_ENABLED=False,
    COMMUNITY_CHAT_ALLOWED_ORIGINS=["https://chat.mlai.au"])
class AccountDeviceManagementTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="device-owner@example.com")
        self.other = get_user_model().objects.create_user(email="other-device-owner@example.com")
        self.current = self.device(self.user, "1" * 64)
        self.target = self.device(self.user, "2" * 64)
        self.foreign = self.device(self.other, "3" * 64)
        self.credentials = self.session(self.current)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.credentials.access_token}")

    def device(self, user, key):
        return CommunityChatDevice.objects.create(user=user, public_key=key,
            installation_id=uuid.uuid4(), client_id="mlai-chat-desktop",
            platform="macos", name="Mac", status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now())

    def session(self, device):
        return issue_account_session(device.user, SimpleNamespace(
            public_key=device.public_key, installation_id=device.installation_id,
            client_id=device.client_id, origin="https://chat.mlai.au",
            platform=device.platform, device_name=device.name))

    def url(self, device):
        return reverse("community_chat_account_device", args=[device.pk])

    def remove(self, device, **changes):
        return self.client.delete(self.url(device), {
            "confirmed": True, "expected_public_key": device.public_key, **changes,
        }, format="json")

    def test_list_is_owner_only_and_uses_authenticated_activity(self):
        historical = self.session(self.target)
        used = timezone.now() - timedelta(hours=2)
        historical.session.last_used_at = used
        historical.session.revoked_at = timezone.now()
        historical.session.save(update_fields=("last_used_at", "revoked_at"))
        AgentInstallation.objects.create(owner=self.user, name="Maek", owner_device=self.target,
            state="paused", is_active=False)
        response = self.client.get(reverse("community_chat_account_devices"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        rows = {r["id"]: r for r in response.data["devices"]}
        self.assertEqual(set(rows), {str(self.current.pk), str(self.target.pk)})
        self.assertTrue(rows[str(self.current.pk)]["is_current"])
        self.assertEqual(rows[str(self.target.pk)]["last_authenticated_at"], used)
        self.assertEqual(rows[str(self.target.pk)]["agent_connections"], ["Maek"])

    def test_bootstrap_tokens_cannot_manage_account_devices(self):
        from community_chat.models import CommunityChatBootstrapToken
        import hashlib
        token = "mlai_chat_" + "a" * 64
        CommunityChatBootstrapToken.objects.create(user=self.user,
            public_key=self.current.public_key, installation_id=self.current.installation_id,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(minutes=5))
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(self.client.get(reverse("community_chat_account_devices")).status_code, 401)

    def test_rename_does_not_change_authority_or_other_accounts(self):
        response = self.client.patch(self.url(self.target), {"name": "My laptop"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.target.refresh_from_db()
        self.assertEqual(self.target.name, "My laptop")
        self.assertEqual(self.target.public_key, "2" * 64)
        self.assertEqual(self.client.patch(self.url(self.foreign), {"name": "Mine"}, format="json").status_code, 404)
        self.assertEqual(self.client.patch(self.url(self.target), {"name": "", "user_id": self.other.pk}, format="json").status_code, 400)

    @patch("community_chat.device_management.revoke_relay_membership", return_value=("revoked", {}))
    @patch("community_chat.device_management.revoke_member_invite", return_value=("revoked", {}))
    def test_remove_revokes_owned_credentials_and_keeps_current_device(self, _invite, relay):
        target_credentials = self.session(self.target)
        response = self.remove(self.target)
        self.assertEqual(response.status_code, 200)
        self.target.refresh_from_db()
        self.current.refresh_from_db()
        target_credentials.session.refresh_from_db()
        self.assertEqual(self.target.status, DeviceBindingStatus.REVOKED)
        self.assertEqual(self.current.status, DeviceBindingStatus.VERIFIED)
        self.assertIsNotNone(target_credentials.session.revoked_at)
        relay.assert_called_once_with(self.target.public_key)
        self.assertEqual(self.client.get(reverse("community_chat_account_devices")).status_code, 200)

    @patch("community_chat.device_management.revoke_relay_membership")
    def test_foreign_current_agent_and_stale_targets_are_rejected(self, relay):
        self.assertEqual(self.remove(self.foreign).status_code, 404)
        self.assertEqual(self.remove(self.current).status_code, 403)
        self.assertEqual(self.remove(self.target, expected_public_key="4" * 64).status_code, 404)
        AgentInstallation.objects.create(owner=self.user, name="Maek", owner_device=self.target,
            state="paused", is_active=False)
        self.assertEqual(self.remove(self.target).data["code"], "agent_device_in_use")
        relay.assert_not_called()

    @patch("community_chat.device_management.revoke_relay_membership")
    def test_recent_proof_and_explicit_confirmation_are_required(self, relay):
        self.assertEqual(self.remove(self.target, confirmed=False).status_code, 400)
        self.credentials.session.created_at = timezone.now() - timedelta(minutes=11)
        self.credentials.session.save(update_fields=("created_at",))
        response = self.remove(self.target)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "reauthentication_required")
        relay.assert_not_called()

    def test_cookie_mutations_require_exact_origin(self):
        self.client.credentials()
        self.client.cookies["mlai_chat_access"] = self.credentials.access_token
        response = self.client.patch(self.url(self.target), {"name": "Renamed"}, format="json",
            HTTP_ORIGIN="https://untrusted.example")
        self.assertEqual(response.status_code, 401)
