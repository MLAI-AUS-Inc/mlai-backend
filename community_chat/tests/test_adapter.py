import uuid
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from community_chat.adapter import (
    MembershipAdapterConflict,
    MembershipAdapterUnavailable,
    issue_member_invite,
    revoke_relay_membership,
)


@override_settings(
    COMMUNITY_CHAT_ADAPTER_URL="http://membership.internal:3100",
    COMMUNITY_CHAT_ADAPTER_TOKEN="exact-secret",
    COMMUNITY_CHAT_ADAPTER_TIMEOUT_SECONDS=3,
)
class MembershipAdapterClientTests(SimpleTestCase):
    @patch("community_chat.adapter.requests.request")
    def test_invite_request_uses_exact_bearer_and_validates_member_contract(self, request):
        response = Mock(status_code=200)
        response.json.return_value = {
            "invite_code": "v2.secret",
            "invite_id": "invite-id",
            "expires_at": "2030-01-01T00:00:00Z",
            "role": "member",
            "max_uses": 1,
        }
        request.return_value = response
        invite = issue_member_invite("a" * 64)
        self.assertEqual(invite.code, "v2.secret")
        kwargs = request.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer exact-secret")
        self.assertEqual(kwargs["json"], {"public_key": "a" * 64})
        self.assertNotIn("private_key", kwargs["json"])

    @patch("community_chat.adapter.requests.request")
    def test_invite_rejects_non_member_or_multi_use_response(self, request):
        response = Mock(status_code=200)
        response.json.return_value = {
            "invite_code": "v2.secret",
            "invite_id": "invite-id",
            "expires_at": "2030-01-01T00:00:00Z",
            "role": "admin",
            "max_uses": 2,
        }
        request.return_value = response
        with self.assertRaises(MembershipAdapterUnavailable):
            issue_member_invite("a" * 64)

    @patch("community_chat.adapter.requests.request")
    def test_protected_role_conflict_is_preserved_without_response_body_leak(self, request):
        response = Mock(status_code=409)
        response.json.return_value = {"error": "protected_role", "private_key": "must-not-flow"}
        request.return_value = response
        with self.assertRaises(MembershipAdapterConflict) as raised:
            revoke_relay_membership("a" * 64)
        self.assertEqual(raised.exception.code, "protected_role")

    @patch("community_chat.adapter.requests.request")
    def test_revocation_is_idempotent_for_missing_members(self, request):
        response = Mock(status_code=200)
        response.json.return_value = {"status": "not_found"}
        request.return_value = response
        result, request_id = revoke_relay_membership("a" * 64)
        self.assertEqual(result, "not_found")
        self.assertIsInstance(request_id, uuid.UUID)

