import hashlib
import json
import threading
import uuid
from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

from coincurve import PrivateKey
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from community_chat.adapter import (
    IssuedInvite,
    MembershipAdapterConflict,
    MembershipAdapterUnavailable,
    RelayMembership,
)
from community_chat.account_sessions import issue_account_session
from community_chat.models import (
    CommunityChatBootstrapToken,
    CommunityChatChallenge,
    CommunityChatDevice,
    CommunityChatEmailCodeChallenge,
    CommunityChatInviteAudit,
    DeviceBindingStatus,
)
from community_chat.authentication import TOKEN_PREFIX


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

    @patch("community_chat.views.issue_member_invite")
    def test_generation_delete_winner_is_returned_without_an_audited_invite(
        self,
        mock_issue,
    ):
        mock_issue.side_effect = MembershipAdapterConflict(
            "invite_attempt_revoked"
        )
        challenge = self.challenge().data

        response = self.client.post(
            reverse("community_chat_invite"),
            self.invite_payload(challenge),
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data, {"error": "invite_attempt_revoked"})
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
            HTTP_ORIGIN=ORIGIN,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        device.refresh_from_db()
        self.assertEqual(device.status, DeviceBindingStatus.REVOKED)
        self.assertEqual(device.revoked_by, self.user)
        self.assertEqual(device.revocation_reason, "lost phone")
        self.assertIsNotNone(device.revoked_at)

    @patch("community_chat.views.revoke_relay_membership")
    def test_key_bound_delete_fences_orphan_invite_without_local_device(
        self,
        mock_revoke,
    ):
        raw_token = f"{TOKEN_PREFIX}{'x' * 60}"
        bootstrap_token = CommunityChatBootstrapToken.objects.create(
            user=self.user,
            public_key=self.public_key,
            client_id="mlai-chat-desktop",
            origin=ORIGIN,
            platform="macos",
            token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        mock_revoke.return_value = ("not_found", uuid.uuid4())
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw_token}")

        response = client.delete(
            reverse("community_chat_device", args=(self.public_key,)),
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            {"status": "revoked", "relay_status": "not_found"},
        )
        mock_revoke.assert_called_once_with(self.public_key)
        bootstrap_token.refresh_from_db()
        self.assertIsNotNone(bootstrap_token.revoked_at)

    @patch("community_chat.views.revoke_relay_membership")
    def test_unscoped_delete_cannot_fence_unknown_public_key(self, mock_revoke):
        response = self.client.delete(
            reverse("community_chat_device", args=(self.public_key,)),
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data, {"error": "device_not_found"})
        mock_revoke.assert_not_called()

    @patch("community_chat.views.revoke_relay_membership")
    def test_stale_key_bound_delete_cannot_revoke_another_users_reenrollment(
        self,
        mock_revoke,
    ):
        raw_token = f"{TOKEN_PREFIX}{'y' * 60}"
        bootstrap_token = CommunityChatBootstrapToken.objects.create(
            user=self.user,
            public_key=self.public_key,
            client_id="mlai-chat-desktop",
            origin=ORIGIN,
            platform="macos",
            token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        other_user = get_user_model().objects.create_user(email="new-owner@example.com")
        CommunityChatDevice.objects.create(
            user=other_user,
            public_key=self.public_key,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw_token}")

        response = client.delete(
            reverse("community_chat_device", args=(self.public_key,)),
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data, {"error": "device_not_found"})
        mock_revoke.assert_not_called()
        bootstrap_token.refresh_from_db()
        self.assertIsNone(bootstrap_token.revoked_at)

    @patch("community_chat.views.revoke_relay_membership")
    def test_historical_owner_cannot_revoke_another_users_reenrollment(
        self,
        mock_revoke,
    ):
        CommunityChatDevice.objects.create(
            user=self.user,
            public_key=self.public_key,
            status=DeviceBindingStatus.REVOKED,
            revoked_at=timezone.now() - timedelta(minutes=1),
            revoked_by=self.user,
        )
        other_user = get_user_model().objects.create_user(email="active-owner@example.com")
        active_device = CommunityChatDevice.objects.create(
            user=other_user,
            public_key=self.public_key,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )

        response = self.client.delete(
            reverse("community_chat_device", args=(self.public_key,)),
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data, {"error": "device_not_found"})
        mock_revoke.assert_not_called()
        active_device.refresh_from_db()
        self.assertEqual(active_device.status, DeviceBindingStatus.VERIFIED)

    @patch("community_chat.views.revoke_relay_membership")
    @patch("community_chat.views.revoke_member_invite")
    def test_revoke_cancels_unconfirmed_invites_before_member_delete(
        self,
        mock_revoke_invite,
        mock_revoke_member,
    ):
        device = CommunityChatDevice.objects.create(
            user=self.user,
            public_key=self.public_key,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        challenge_data = self.challenge().data
        challenge = CommunityChatChallenge.objects.get(
            id=challenge_data["challenge_id"]
        )
        invite_id = str(uuid.uuid4())
        CommunityChatInviteAudit.objects.create(
            device=device,
            challenge=challenge,
            adapter_invite_id=invite_id,
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        calls = []

        def revoke_invite(value):
            calls.append(("invite", value))
            return "revoked", uuid.uuid4()

        def revoke_member(value):
            calls.append(("member", value))
            return "revoked", uuid.uuid4()

        mock_revoke_invite.side_effect = revoke_invite
        mock_revoke_member.side_effect = revoke_member

        response = self.client.delete(
            reverse("community_chat_device", args=(self.public_key,)),
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            calls,
            [("invite", invite_id), ("member", self.public_key)],
        )
        device.refresh_from_db()
        self.assertEqual(device.status, DeviceBindingStatus.REVOKED)

    @patch("community_chat.views.revoke_relay_membership")
    @patch("community_chat.views.revoke_member_invite")
    def test_invite_cancel_failure_rolls_back_device_and_member_revocation(
        self,
        mock_revoke_invite,
        mock_revoke_member,
    ):
        device = CommunityChatDevice.objects.create(
            user=self.user,
            public_key=self.public_key,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        challenge_data = self.challenge().data
        CommunityChatInviteAudit.objects.create(
            device=device,
            challenge=CommunityChatChallenge.objects.get(
                id=challenge_data["challenge_id"]
            ),
            adapter_invite_id=str(uuid.uuid4()),
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        mock_revoke_invite.side_effect = MembershipAdapterUnavailable(
            "adapter_unavailable"
        )

        response = self.client.delete(
            reverse("community_chat_device", args=(self.public_key,)),
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        device.refresh_from_db()
        self.assertEqual(device.status, DeviceBindingStatus.VERIFIED)
        mock_revoke_member.assert_not_called()

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
            HTTP_ORIGIN=ORIGIN,
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        device.refresh_from_db()
        self.assertEqual(device.status, DeviceBindingStatus.VERIFIED)

    @patch("community_chat.views.revoke_relay_membership")
    def test_revoke_rejects_missing_or_unapproved_origin(self, mock_revoke):
        CommunityChatDevice.objects.create(
            user=self.user,
            public_key=self.public_key,
            status=DeviceBindingStatus.VERIFIED,
        )

        missing = self.client.delete(
            reverse("community_chat_device", args=(self.public_key,)),
            format="json",
        )
        unapproved = self.client.delete(
            reverse("community_chat_device", args=(self.public_key,)),
            format="json",
            HTTP_ORIGIN="https://attacker.example",
        )

        self.assertEqual(missing.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(unapproved.status_code, status.HTTP_403_FORBIDDEN)
        mock_revoke.assert_not_called()


@skipUnless(
    connection.features.has_select_for_update,
    "Requires row-level locks to verify device authority serialization",
)
@override_settings(
    COMMUNITY_CHAT_ALLOWED_ORIGINS=[ORIGIN],
    COMMUNITY_CHAT_ADAPTER_TOKEN="adapter-token",
    COMMUNITY_CHAT_RELAY_URL="wss://chat.mlai.au",
)
class CommunityChatDeviceAuthorityTransactionTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(
            email="device-authority-race@example.com"
        )
        self.private_key = PrivateKey.from_int(11)
        self.public_key = public_key(self.private_key)

    def _client(self):
        client = APIClient()
        client.force_authenticate(
            user=get_user_model().objects.get(pk=self.user.pk)
        )
        return client

    def _challenge(self):
        response = self._client().post(
            reverse("community_chat_challenge"),
            {"public_key": self.public_key},
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return response.data

    def test_invite_mint_and_device_delete_serialize_then_cancel_capability(self):
        challenge = self._challenge()
        invite_id = str(uuid.uuid4())
        issue_started = threading.Event()
        finish_issue = threading.Event()
        delete_returned = threading.Event()
        errors = []
        invite_responses = []
        delete_responses = []
        remote_order = []

        def blocked_issue(public_key_value):
            self.assertEqual(public_key_value, self.public_key)
            issue_started.set()
            if not finish_issue.wait(timeout=5):
                raise RuntimeError("test timed out waiting to finish invite mint")
            return IssuedInvite(
                code="v2.race-secret",
                invite_id=invite_id,
                expires_at=timezone.now() + timedelta(minutes=5),
                request_id=uuid.uuid4(),
            )

        def revoke_invite(value):
            remote_order.append(("invite", value))
            return "revoked", uuid.uuid4()

        def revoke_member(value):
            remote_order.append(("member", value))
            return "revoked", uuid.uuid4()

        def invite_request():
            close_old_connections()
            try:
                invite_responses.append(
                    self._client().post(
                        reverse("community_chat_invite"),
                        {
                            "challenge_id": challenge["challenge_id"],
                            "nonce": challenge["nonce"],
                            "event": sign_challenge(self.private_key, challenge),
                        },
                        format="json",
                        HTTP_ORIGIN=ORIGIN,
                    )
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        def delete_request():
            close_old_connections()
            try:
                delete_responses.append(
                    self._client().delete(
                        reverse("community_chat_device", args=(self.public_key,)),
                        format="json",
                        HTTP_ORIGIN=ORIGIN,
                    )
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                delete_returned.set()
                close_old_connections()

        with (
            patch("community_chat.views.issue_member_invite", side_effect=blocked_issue),
            patch("community_chat.views.revoke_member_invite", side_effect=revoke_invite),
            patch("community_chat.views.revoke_relay_membership", side_effect=revoke_member),
        ):
            invite_thread = threading.Thread(target=invite_request)
            invite_thread.start()
            self.assertTrue(issue_started.wait(timeout=5))
            delete_thread = threading.Thread(target=delete_request)
            delete_thread.start()
            self.assertFalse(delete_returned.wait(timeout=0.2))
            self.assertEqual(remote_order, [])

            finish_issue.set()
            invite_thread.join(timeout=5)
            delete_thread.join(timeout=5)

        self.assertFalse(invite_thread.is_alive())
        self.assertFalse(delete_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(invite_responses[0].status_code, status.HTTP_200_OK)
        self.assertEqual(delete_responses[0].status_code, status.HTTP_200_OK)
        self.assertEqual(
            remote_order,
            [("invite", invite_id), ("member", self.public_key)],
        )
        device = CommunityChatDevice.objects.get(
            user=self.user,
            public_key=self.public_key,
        )
        self.assertEqual(device.status, DeviceBindingStatus.REVOKED)

    def test_delete_wins_over_a_pre_authenticated_invite_waiting_on_user_lock(self):
        installation_id = uuid.uuid4()
        device = CommunityChatDevice.objects.create(
            user=self.user,
            public_key=self.public_key,
            installation_id=installation_id,
            client_id="mlai-chat-desktop",
            platform="macos",
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        session_context = CommunityChatEmailCodeChallenge.objects.create(
            user=self.user,
            email_digest="a" * 64,
            code_digest="b" * 64,
            client_id="mlai-chat-desktop",
            installation_id=installation_id,
            origin=ORIGIN,
            platform="macos",
            device_name="Lost Mac",
            public_key=self.public_key,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        credentials = issue_account_session(self.user, session_context)
        enrollment_client = APIClient()
        enrollment_client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {credentials.access_token}"
        )
        challenge_response = enrollment_client.post(
            reverse("community_chat_challenge"),
            {"public_key": self.public_key},
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )
        self.assertEqual(challenge_response.status_code, status.HTTP_201_CREATED)
        challenge = challenge_response.data

        revoke_started = threading.Event()
        finish_revoke = threading.Event()
        invite_authenticated = threading.Event()
        finish_invite_authentication = threading.Event()
        errors = []
        delete_responses = []
        invite_responses = []

        def blocked_revoke(public_key_value):
            self.assertEqual(public_key_value, self.public_key)
            revoke_started.set()
            if not finish_revoke.wait(timeout=5):
                raise RuntimeError("test timed out waiting to finish device delete")
            return "revoked", uuid.uuid4()

        from community_chat.account_sessions import (
            authenticate_access_token as authenticate_access_token_original,
        )

        def observed_authenticate(raw_token):
            session = authenticate_access_token_original(raw_token)
            invite_authenticated.set()
            if not finish_invite_authentication.wait(timeout=5):
                raise RuntimeError(
                    "test timed out waiting to finish invite authentication"
                )
            return session

        def delete_request():
            close_old_connections()
            try:
                delete_responses.append(
                    self._client().delete(
                        reverse("community_chat_device", args=(self.public_key,)),
                        format="json",
                        HTTP_ORIGIN=ORIGIN,
                    )
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        def invite_request():
            close_old_connections()
            try:
                client = APIClient()
                client.credentials(
                    HTTP_AUTHORIZATION=f"Bearer {credentials.access_token}"
                )
                invite_responses.append(
                    client.post(
                        reverse("community_chat_invite"),
                        {
                            "challenge_id": challenge["challenge_id"],
                            "nonce": challenge["nonce"],
                            "event": sign_challenge(self.private_key, challenge),
                        },
                        format="json",
                        HTTP_ORIGIN=ORIGIN,
                    )
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        with (
            patch(
                "community_chat.views.revoke_relay_membership",
                side_effect=blocked_revoke,
            ),
            patch(
                "community_chat.authentication.authenticate_access_token",
                side_effect=observed_authenticate,
            ),
            patch("community_chat.views.issue_member_invite") as issue_invite,
        ):
            invite_thread = threading.Thread(target=invite_request)
            invite_thread.start()
            self.assertTrue(invite_authenticated.wait(timeout=5))

            delete_thread = threading.Thread(target=delete_request)
            delete_thread.start()
            self.assertTrue(revoke_started.wait(timeout=5))
            finish_invite_authentication.set()
            self.assertTrue(invite_thread.is_alive())

            finish_revoke.set()
            delete_thread.join(timeout=5)
            invite_thread.join(timeout=5)

        self.assertFalse(delete_thread.is_alive())
        self.assertFalse(invite_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(delete_responses[0].status_code, status.HTTP_200_OK)
        self.assertEqual(invite_responses[0].status_code, status.HTTP_403_FORBIDDEN)
        issue_invite.assert_not_called()
        device.refresh_from_db()
        credentials.session.refresh_from_db()
        self.assertEqual(device.status, DeviceBindingStatus.REVOKED)
        self.assertIsNotNone(credentials.session.revoked_at)

    def test_confirm_membership_observation_cannot_resurrect_after_delete(self):
        device = CommunityChatDevice.objects.create(
            user=self.user,
            public_key=self.public_key,
        )
        membership_started = threading.Event()
        finish_membership = threading.Event()
        delete_returned = threading.Event()
        errors = []
        confirm_responses = []
        delete_responses = []

        def blocked_membership(public_key_value):
            self.assertEqual(public_key_value, self.public_key)
            membership_started.set()
            if not finish_membership.wait(timeout=5):
                raise RuntimeError("test timed out waiting to finish membership read")
            return RelayMembership(True, "member", timezone.now())

        def confirm_request():
            close_old_connections()
            try:
                confirm_responses.append(
                    self._client().post(
                        reverse("community_chat_confirm"),
                        {"public_key": self.public_key},
                        format="json",
                        HTTP_ORIGIN=ORIGIN,
                    )
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        def delete_request():
            close_old_connections()
            try:
                delete_responses.append(
                    self._client().delete(
                        reverse("community_chat_device", args=(self.public_key,)),
                        format="json",
                        HTTP_ORIGIN=ORIGIN,
                    )
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                delete_returned.set()
                close_old_connections()

        with (
            patch("community_chat.views.get_relay_membership", side_effect=blocked_membership),
            patch(
                "community_chat.views.revoke_relay_membership",
                return_value=("revoked", uuid.uuid4()),
            ),
        ):
            confirm_thread = threading.Thread(target=confirm_request)
            confirm_thread.start()
            self.assertTrue(membership_started.wait(timeout=5))
            delete_thread = threading.Thread(target=delete_request)
            delete_thread.start()
            self.assertFalse(delete_returned.wait(timeout=0.2))

            finish_membership.set()
            confirm_thread.join(timeout=5)
            delete_thread.join(timeout=5)

        self.assertFalse(confirm_thread.is_alive())
        self.assertFalse(delete_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(confirm_responses[0].status_code, status.HTTP_200_OK)
        self.assertEqual(delete_responses[0].status_code, status.HTTP_200_OK)
        device.refresh_from_db()
        self.assertEqual(device.status, DeviceBindingStatus.REVOKED)
