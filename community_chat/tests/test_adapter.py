import uuid
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase, override_settings

from community_chat.adapter import (
    MAX_MEMBER_INVITE_GENERATION,
    MembershipAdapterConflict,
    MembershipAdapterUnavailable,
    MEMBER_INVITE_PROTOCOL,
    issue_member_invite,
    revoke_member_invite,
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
        capabilities = Mock(status_code=200)
        capabilities.json.return_value = {
            "member_invite_protocols": [MEMBER_INVITE_PROTOCOL, "legacy_v1"]
        }
        intent = Mock(status_code=200)
        intent.json.return_value = {
            "public_key": "a" * 64,
            "generation": 7,
        }
        response = Mock(status_code=200)
        response.json.return_value = {
            "invite_code": "v2.secret",
            "invite_id": "invite-id",
            "expires_at": "2030-01-01T00:00:00Z",
            "role": "member",
            "max_uses": 1,
        }
        request.side_effect = [capabilities, intent, response]
        invite = issue_member_invite("a" * 64)
        self.assertEqual(invite.code, "v2.secret")
        self.assertEqual(
            [call.args[:2] for call in request.call_args_list],
            [
                ("GET", "http://membership.internal:3100/v1/capabilities"),
                (
                    "POST",
                    "http://membership.internal:3100/v2/member-invite-intents",
                ),
                ("POST", "http://membership.internal:3100/v2/member-invites"),
            ],
        )
        for call in request.call_args_list:
            self.assertEqual(
                call.kwargs["headers"]["Authorization"],
                "Bearer exact-secret",
            )
        self.assertEqual(
            request.call_args_list[1].kwargs["json"],
            {"public_key": "a" * 64},
        )
        self.assertEqual(
            request.call_args_list[2].kwargs["json"],
            {"public_key": "a" * 64, "expected_generation": 7},
        )
        self.assertNotIn("private_key", request.call_args_list[2].kwargs["json"])

    @patch("community_chat.adapter.requests.request")
    def test_invite_rejects_non_member_or_multi_use_response(self, request):
        capabilities = Mock(status_code=200)
        capabilities.json.return_value = {
            "member_invite_protocols": [MEMBER_INVITE_PROTOCOL]
        }
        intent = Mock(status_code=200)
        intent.json.return_value = {
            "public_key": "a" * 64,
            "generation": 2,
        }
        response = Mock(status_code=200)
        response.json.return_value = {
            "invite_code": "v2.secret",
            "invite_id": "invite-id",
            "expires_at": "2030-01-01T00:00:00Z",
            "role": "admin",
            "max_uses": 2,
        }
        request.side_effect = [capabilities, intent, response]
        with self.assertRaises(MembershipAdapterUnavailable):
            issue_member_invite("a" * 64)

    @patch("community_chat.adapter.requests.request")
    def test_invite_fails_closed_when_generation_protocol_is_missing(self, request):
        capabilities = Mock(status_code=200)
        capabilities.json.return_value = {"member_invite_protocols": ["legacy_v1"]}
        request.return_value = capabilities

        with self.assertRaises(MembershipAdapterUnavailable) as raised:
            issue_member_invite("a" * 64)

        self.assertEqual(str(raised.exception), "adapter_protocol_unavailable")
        self.assertEqual(request.call_count, 1)
        self.assertNotIn(
            "/v1/member-invites",
            request.call_args.args[1],
        )

    @patch("community_chat.adapter.requests.request")
    def test_intent_timeout_never_attempts_a_mint_or_legacy_fallback(self, request):
        capabilities = Mock(status_code=200)
        capabilities.json.return_value = {
            "member_invite_protocols": [MEMBER_INVITE_PROTOCOL, "legacy_v1"]
        }
        request.side_effect = [capabilities, requests.Timeout("late intent")]

        with self.assertRaises(MembershipAdapterUnavailable):
            issue_member_invite("a" * 64)

        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            request.call_args.args[1],
            "http://membership.internal:3100/v2/member-invite-intents",
        )

    @patch("community_chat.adapter.requests.request")
    def test_intent_rejects_generation_above_adapter_i64_contract(self, request):
        capabilities = Mock(status_code=200)
        capabilities.json.return_value = {
            "member_invite_protocols": [MEMBER_INVITE_PROTOCOL]
        }
        intent = Mock(status_code=200)
        intent.json.return_value = {
            "public_key": "a" * 64,
            "generation": MAX_MEMBER_INVITE_GENERATION + 1,
        }
        request.side_effect = [capabilities, intent]

        with self.assertRaises(MembershipAdapterUnavailable) as raised:
            issue_member_invite("a" * 64)

        self.assertEqual(str(raised.exception), "adapter_invalid_response")
        self.assertEqual(request.call_count, 2)

    @patch("community_chat.adapter.requests.request")
    def test_stale_generation_conflict_is_preserved(self, request):
        capabilities = Mock(status_code=200)
        capabilities.json.return_value = {
            "member_invite_protocols": [MEMBER_INVITE_PROTOCOL]
        }
        intent = Mock(status_code=200)
        intent.json.return_value = {
            "public_key": "a" * 64,
            "generation": 3,
        }
        conflict = Mock(status_code=409)
        conflict.json.return_value = {"error": "invite_attempt_revoked"}
        request.side_effect = [capabilities, intent, conflict]

        with self.assertRaises(MembershipAdapterConflict) as raised:
            issue_member_invite("a" * 64)

        self.assertEqual(raised.exception.code, "invite_attempt_revoked")
        self.assertEqual(
            request.call_args.kwargs["json"],
            {"public_key": "a" * 64, "expected_generation": 3},
        )

    @patch("community_chat.adapter.requests.request")
    def test_invite_revocation_is_authenticated_encoded_and_idempotent(self, request):
        response = Mock(status_code=200)
        response.json.return_value = {"status": "not_found"}
        request.return_value = response

        result, request_id = revoke_member_invite("invite/id")

        self.assertEqual(result, "not_found")
        self.assertIsInstance(request_id, uuid.UUID)
        args = request.call_args.args
        kwargs = request.call_args.kwargs
        self.assertEqual(args[0], "DELETE")
        self.assertEqual(
            args[1],
            "http://membership.internal:3100/v1/member-invites/invite%2Fid",
        )
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer exact-secret")
        self.assertIsNone(kwargs["json"])

    @patch("community_chat.adapter.requests.request")
    def test_invite_revocation_rejects_an_unknown_adapter_outcome(self, request):
        response = Mock(status_code=200)
        response.json.return_value = {"status": "still_active"}
        request.return_value = response

        with self.assertRaises(MembershipAdapterUnavailable):
            revoke_member_invite("invite-id")

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
