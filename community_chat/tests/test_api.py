import hashlib
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from coincurve import PrivateKey
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from community_chat.adapter import (
    IssuedInvite,
    MembershipAdapterConflict,
    MembershipAdapterUnavailable,
    RelayMembership,
)
from community_chat.models import (
    CommunityChatChallenge,
    CommunityChatDevice,
    CommunityChatInviteAudit,
    DeviceBindingStatus,
)


ORIGIN = "https://chat.mlai.au"


def public_key(private_key):
    return private_key.public_key.format(compressed=True)[1:].hex()


def sign_challenge(private_key, challenge, *, created_at=None):
    unsigned = challenge["unsigned_event"]
    created_at = int(created_at or timezone.now().timestamp())
    pubkey = public_key(private_key)
    serialized = json.dumps(
        [0, pubkey, created_at, unsigned["kind"], unsigned["tags"], unsigned["content"]],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    event_id = hashlib.sha256(serialized).hexdigest()
    signature = private_key.sign_schnorr(bytes.fromhex(event_id), aux_randomness=b"\x00" * 32)
    return {
        "id": event_id,
        "pubkey": pubkey,
        "created_at": created_at,
        "kind": unsigned["kind"],
        "tags": unsigned["tags"],
        "content": unsigned["content"],
        "sig": signature.hex(),
    }


@override_settings(
    COMMUNITY_CHAT_ALLOWED_ORIGINS=[ORIGIN],
    COMMUNITY_CHAT_ADAPTER_TOKEN="adapter-token",
    COMMUNITY_CHAT_RELAY_URL="wss://chat.mlai.au",
)
class CommunityChatApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(email="member@example.com")
        self.client.force_authenticate(user=self.user)
        self.private_key = PrivateKey.from_int(1)
        self.public_key = public_key(self.private_key)

    def challenge(self, *, public_key_value=None, origin=ORIGIN):
        return self.client.post(
            reverse("community_chat_challenge"),
            {"public_key": public_key_value or self.public_key},
            format="json",
            HTTP_ORIGIN=origin,
        )

    def invite_payload(self, challenge):
        return {
            "challenge_id": challenge["challenge_id"],
            "nonce": challenge["nonce"],
            "event": sign_challenge(self.private_key, challenge),
        }

    def test_session_requires_authentication_and_returns_public_bindings_only(self):
        self.client.force_authenticate(user=None)
        anonymous = self.client.get(reverse("community_chat_session"))
        self.assertEqual(anonymous.status_code, status.HTTP_401_UNAUTHORIZED)

        self.client.force_authenticate(user=self.user)
        device = CommunityChatDevice.objects.create(user=self.user, public_key=self.public_key)
        response = self.client.get(reverse("community_chat_session"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["authenticated"])
        self.assertTrue(response.data["eligible"])
        self.assertEqual(response.data["devices"][0]["id"], str(device.id))
        self.assertNotIn("email", response.data)
        self.assertNotIn("private_key", response.data["devices"][0])

    def test_challenge_binds_key_action_audience_and_exact_origin(self):
        response = self.challenge()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["origin"], ORIGIN)
        self.assertEqual(response.data["action"], "community-chat:enrol-device")
        self.assertEqual(response.data["unsigned_event"]["kind"], 27235)
        self.assertIn(["origin", ORIGIN], response.data["unsigned_event"]["tags"])

        rejected = self.challenge(origin="https://attacker.example")
        self.assertEqual(rejected.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_ineligible_and_rate_limited_challenges_fail_closed(self):
        self.client.force_authenticate(user=None)
        anonymous = self.challenge()
        self.assertEqual(anonymous.status_code, status.HTTP_401_UNAUTHORIZED)

        self.user.is_active = False
        self.user.save(update_fields=("is_active",))
        self.client.force_authenticate(user=self.user)
        ineligible = self.challenge()
        self.assertEqual(ineligible.status_code, status.HTTP_403_FORBIDDEN)

        self.user.is_active = True
        self.user.save(update_fields=("is_active",))
        self.client.force_authenticate(user=self.user)
        cache.clear()
        for _ in range(10):
            self.assertEqual(self.challenge().status_code, status.HTTP_201_CREATED)
        limited = self.challenge()
        self.assertEqual(limited.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    @patch("community_chat.views.issue_member_invite")
    def test_valid_device_proof_issues_invite_and_audits_without_code(self, mock_issue):
        expires_at = timezone.now() + timedelta(minutes=5)
        mock_issue.return_value = IssuedInvite(
            code="v2.one-use-secret",
            invite_id="relay-invite-id",
            expires_at=expires_at,
            request_id=uuid.uuid4(),
        )
        challenge = self.challenge().data
        response = self.client.post(
            reverse("community_chat_invite"),
            self.invite_payload(challenge),
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "invite_issued")
        self.assertEqual(response.data["invite_code"], "v2.one-use-secret")
        mock_issue.assert_called_once_with(self.public_key)

        device = CommunityChatDevice.objects.get(public_key=self.public_key)
        self.assertEqual(device.status, DeviceBindingStatus.PENDING)
        audit = CommunityChatInviteAudit.objects.get(device=device)
        self.assertEqual(audit.adapter_invite_id, "relay-invite-id")
        self.assertNotIn("invite_code", {field.name for field in audit._meta.fields})
        self.assertNotIn("code", {field.name for field in audit._meta.fields})

    @patch("community_chat.views.issue_member_invite")
    def test_challenge_is_single_use_and_replay_fails_closed(self, mock_issue):
        mock_issue.return_value = IssuedInvite(
            code="v2.secret",
            invite_id="relay-id",
            expires_at=timezone.now() + timedelta(minutes=5),
            request_id=uuid.uuid4(),
        )
        challenge = self.challenge().data
        payload = self.invite_payload(challenge)
        first = self.client.post(
            reverse("community_chat_invite"), payload, format="json", HTTP_ORIGIN=ORIGIN
        )
        second = self.client.post(
            reverse("community_chat_invite"), payload, format="json", HTTP_ORIGIN=ORIGIN
        )
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(second.data["error"], "challenge_replayed")
        self.assertEqual(mock_issue.call_count, 1)

    @patch("community_chat.views.issue_member_invite")
    def test_invalid_signature_and_expired_challenge_never_mint(self, mock_issue):
        challenge = self.challenge().data
        invalid = self.invite_payload(challenge)
        invalid["event"]["sig"] = "00" * 64
        bad_signature = self.client.post(
            reverse("community_chat_invite"), invalid, format="json", HTTP_ORIGIN=ORIGIN
        )
        self.assertEqual(bad_signature.status_code, status.HTTP_403_FORBIDDEN)

        challenge = self.challenge().data
        CommunityChatChallenge.objects.filter(id=challenge["challenge_id"]).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        expired = self.client.post(
            reverse("community_chat_invite"),
            self.invite_payload(challenge),
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )
        self.assertEqual(expired.status_code, status.HTTP_410_GONE)
        mock_issue.assert_not_called()

    @patch("community_chat.views.issue_member_invite")
    def test_origin_mismatch_does_not_consume_challenge(self, mock_issue):
        challenge = self.challenge().data
        mismatch = self.client.post(
            reverse("community_chat_invite"),
            self.invite_payload(challenge),
            format="json",
            HTTP_ORIGIN="https://attacker.example",
        )
        self.assertEqual(mismatch.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIsNone(CommunityChatChallenge.objects.get(id=challenge["challenge_id"]).used_at)
        mock_issue.assert_not_called()

    @patch("community_chat.views.issue_member_invite")
    def test_adapter_failure_returns_coarse_error_and_never_persists_invite_secret(self, mock_issue):
        mock_issue.side_effect = MembershipAdapterUnavailable("sensitive-upstream-detail")
        challenge = self.challenge().data
        response = self.client.post(
            reverse("community_chat_invite"),
            self.invite_payload(challenge),
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data, {"error": "membership_service_unavailable"})
        self.assertFalse(CommunityChatInviteAudit.objects.exists())

    def test_active_public_key_cannot_be_bound_to_a_second_user(self):
        CommunityChatDevice.objects.create(user=self.user, public_key=self.public_key)
        second_user = get_user_model().objects.create_user(email="other@example.com")
        self.client.force_authenticate(user=second_user)
        response = self.challenge()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["error"], "public_key_already_bound")

    @patch("community_chat.views.get_relay_membership")
    def test_confirm_marks_membership_verified(self, mock_membership):
        device = CommunityChatDevice.objects.create(user=self.user, public_key=self.public_key)
        mock_membership.return_value = RelayMembership(True, "member", timezone.now())
        response = self.client.post(
            reverse("community_chat_confirm"),
            {"public_key": self.public_key},
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        device.refresh_from_db()
        self.assertEqual(device.status, DeviceBindingStatus.VERIFIED)
        self.assertIsNotNone(device.last_verified_membership_at)

    @patch("community_chat.views.get_relay_membership")
    def test_confirm_rejects_escalated_relay_role(self, mock_membership):
        device = CommunityChatDevice.objects.create(user=self.user, public_key=self.public_key)
        mock_membership.return_value = RelayMembership(True, "admin", timezone.now())
        response = self.client.post(
            reverse("community_chat_confirm"),
            {"public_key": self.public_key},
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        device.refresh_from_db()
        self.assertEqual(device.status, DeviceBindingStatus.PENDING)

    @patch("community_chat.views.revoke_relay_membership")
    def test_revoke_retains_auditable_binding_history(self, mock_revoke):
        device = CommunityChatDevice.objects.create(
            user=self.user,
            public_key=self.public_key,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        mock_revoke.return_value = ("revoked", uuid.uuid4())
        response = self.client.delete(
            reverse("community_chat_device", args=(self.public_key,)),
            {"reason": "lost phone"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        device.refresh_from_db()
        self.assertEqual(device.status, DeviceBindingStatus.REVOKED)
        self.assertEqual(device.revoked_by, self.user)
        self.assertEqual(device.revocation_reason, "lost phone")
        self.assertIsNotNone(device.revoked_at)

    @patch("community_chat.views.revoke_relay_membership")
    def test_protected_relay_role_cannot_be_revoked(self, mock_revoke):
        device = CommunityChatDevice.objects.create(
            user=self.user,
            public_key=self.public_key,
            status=DeviceBindingStatus.VERIFIED,
        )
        mock_revoke.side_effect = MembershipAdapterConflict("protected_role")
        response = self.client.delete(
            reverse("community_chat_device", args=(self.public_key,)),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        device.refresh_from_db()
        self.assertEqual(device.status, DeviceBindingStatus.VERIFIED)
