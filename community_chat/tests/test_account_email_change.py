"""Regression coverage for the sign-in email change and its credential boundary."""

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.core.cache import cache
from django.db import close_old_connections, connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from core.email_utils import generate_magic_link
from core.models import User, UserEmailAlias
from core.password_auth import issue_password_reset
from core.refresh_sessions import issue_refresh_token
from community_chat.account_sessions import rotate_account_session, InvalidAccountSession
from community_chat.email_change_delivery import process_email_change_jobs
from community_chat.email_codes import issue_email_code_challenge
from community_chat.email_delivery import decrypt_signup_email
from community_chat.models import (
    CommunityChatDevice, CommunityChatEmailChange, CommunityChatEmailChangeJob,
    CommunityMemberProfile, DeviceBindingStatus, EmailChangeStatus,
)
from .test_account_profiles import credentials_for, ORIGIN


@override_settings(
    COMMUNITY_CHAT_EMAIL_CHANGE_ENABLED=True,
    MLAI_LEGACY_EMAIL_MAGIC_LINKS_ENABLED=False,
    SYSTEM_EMAIL_RESEND_API_KEY="development-only",
    COMMUNITY_CHAT_ALLOWED_ORIGINS=[ORIGIN],
)
class AccountEmailChangeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(email="old@example.test", first_name="Example")
        self.creds = credentials_for(self.user)
        self.client = self.bearer(self.creds)

    @staticmethod
    def bearer(creds):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {creds.access_token}")
        return client

    def post(self, action, *, client=None, idempotency=None, **body):
        return (client or self.client).post(
            reverse(f"community_chat_email_change_{action.replace('-', '_')}"),
            body, format="json", HTTP_IDEMPOTENCY_KEY=idempotency or str(uuid.uuid4()),
        )

    def mail(self, kind):
        job = CommunityChatEmailChangeJob.objects.get(kind=kind, finished_at__isnull=True)
        return json.loads(decrypt_signup_email(job.ciphertext))

    def start(self):
        result = self.post("start", new_email="New@EXample.test")
        self.assertEqual(result.status_code, 202, result.data)
        self.assertIn("no-store", result["Cache-Control"])
        return result.data["request_id"]

    def verify_current(self, request_id):
        result = self.post("verify-current", request_id=request_id, code=self.mail("current_code")["code"])
        self.assertEqual(result.status_code, 200, result.data)
        return result

    def test_both_inboxes_change_same_account_and_retire_old_proofs(self):
        stable_id = self.user.community_chat_profile_id
        old_magic_link = generate_magic_link(self.user).split("?token=")[-1]
        old_jwt = issue_refresh_token(self.user)
        old_chat_code = issue_email_code_challenge(
            email=self.user.email, client_id="mlai-chat-ios", installation_id=uuid.uuid4(),
            origin="mlaichat://callback", platform="ios", device_name="Other phone", public_key="b" * 64,
        )
        self.assertTrue(issue_password_reset(self.user.email))
        member = CommunityMemberProfile.objects.create(
            user=self.user, status=CommunityMemberProfile.Status.APPROVED, marketing_opt_in=True,
        )
        device = CommunityChatDevice.objects.create(
            user=self.user, public_key=self.creds.session.public_key,
            installation_id=self.creds.session.installation_id, status=DeviceBindingStatus.VERIFIED,
        )
        request_id = self.start()
        first = self.mail("current_code")
        self.assertEqual(first["recipient"], "old@example.test")
        self.assertEqual(self.post("confirm", request_id=request_id, code=first["code"]).status_code, 409)
        self.verify_current(request_id)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "old@example.test")
        new = self.mail("new_code")
        self.assertEqual(new["recipient"], "new@example.test")
        result = self.post("confirm", request_id=request_id, code=new["code"])
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["status"], "reauthentication_required")
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "new@example.test")
        self.assertEqual(self.user.community_chat_profile_id, stable_id)
        self.assertEqual(self.user.auth_version, 2)
        self.assertIsNotNone(self.user.email_verified_at)
        self.assertFalse(self.user.has_usable_password())
        self.assertFalse(User.objects.for_email("old@example.test").exists())
        member.refresh_from_db()
        self.assertFalse(member.marketing_opt_in)
        device.refresh_from_db()
        self.assertEqual(device.status, DeviceBindingStatus.REVOKED)
        old_chat_code.refresh_from_db()
        self.assertIsNotNone(old_chat_code.invalidated_at)
        self.assertFalse(self.user.password_reset_challenges.filter(consumed_at__isnull=True).exists())

        with self.assertRaises(InvalidAccountSession):
            rotate_account_session(self.creds.refresh_token)
        self.assertEqual(self.client.get(reverse("community_chat_email_change_status")).status_code, 401)
        self.assertEqual(APIClient().get(reverse("current_user"), HTTP_AUTHORIZATION=f"Bearer {old_jwt.access_token}").status_code, 401)
        User.objects.create_user(email="old@example.test")
        self.assertEqual(APIClient().get(reverse("verify_magic_link"), {"token": old_magic_link}).status_code, 400)
        with patch("community_chat.email_change_delivery.system_email.send") as send, patch(
            "community_chat.email_change_delivery.revoke_relay_membership",
        ) as revoke, patch("community_chat.email_change_delivery.revoke_member_invite"):
            self.assertEqual(process_email_change_jobs(limit=10), 3)
            self.assertEqual({call.kwargs["to"] for call in send.call_args_list}, {"old@example.test", "new@example.test"})
            revoke.assert_called_once_with(device.public_key)
            self.assertEqual(process_email_change_jobs(limit=10), 0)

    def test_proof_bound_to_session_and_wrong_code_retry_does_not_spend_twice(self):
        request_id = self.start()
        other_client = self.bearer(credentials_for(self.user, web=True))
        self.assertEqual(self.post(
            "verify-current", client=other_client, request_id=request_id,
            code=self.mail("current_code")["code"],
        ).status_code, 400)
        wrong = "111111" if self.mail("current_code")["code"] != "111111" else "222222"
        key = str(uuid.uuid4())
        for _ in range(2):
            result = self.post("verify-current", request_id=request_id, code=wrong, idempotency=key)
            self.assertEqual(result.data, {"error": "invalid_code"})
        self.assertEqual(CommunityChatEmailChange.objects.get(pk=request_id).attempts, 1)

    def test_alias_conflict_disclosed_only_after_destination_proof(self):
        other = User.objects.create_user(email="other@example.test")
        UserEmailAlias.objects.create(user=other, email="new@example.test")
        request_id = self.start()
        self.verify_current(request_id)
        result = self.post("confirm", request_id=request_id, code=self.mail("new_code")["code"])
        self.assertEqual(result.data, {"error": "address_unavailable"})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(CommunityChatEmailChange.objects.get().status, EmailChangeStatus.CONFLICT)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "old@example.test")

    def test_cookie_requires_bound_key_and_trusted_origin(self):
        web = credentials_for(self.user, web=True)
        browser = APIClient()
        browser.cookies["mlai_chat_access"] = web.access_token
        args = {"new_email": "new@example.test"}
        url = reverse("community_chat_email_change_start")
        for origin, key in [(ORIGIN, ""), ("https://foreign.example", web.session.public_key)]:
            reply = browser.post(url, args, format="json", HTTP_ORIGIN=origin,
                                 HTTP_X_MLAI_PUBLIC_KEY=key, HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
            self.assertEqual(reply.status_code, 401)
        self.assertFalse(CommunityChatEmailChange.objects.exists())

    def test_legacy_profile_routes_keep_email_readonly_and_preserve_profile_writes(self):
        auth = f"Bearer {issue_refresh_token(self.user).access_token}"
        for route in ("update_user", "update_profile"):
            response = APIClient().patch(
                reverse(route), {"email": "elsewhere@example.test", "first_name": "Wrong"},
                format="json", HTTP_AUTHORIZATION=auth,
            )
            self.assertEqual(response.data, {"error": "email_change_required"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Example")
        response = APIClient().patch(
            reverse("update_profile"), {"email": "OLD@example.test", "first_name": "Renamed"},
            format="json", HTTP_AUTHORIZATION=auth,
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Renamed")

    @override_settings(MLAI_LEGACY_EMAIL_MAGIC_LINKS_ENABLED=True)
    def test_feature_requires_legacy_magic_links_to_be_disabled(self):
        self.assertEqual(self.post("start", new_email="new@example.test").status_code, 404)


@override_settings(
    COMMUNITY_CHAT_EMAIL_CHANGE_ENABLED=True,
    MLAI_LEGACY_EMAIL_MAGIC_LINKS_ENABLED=False,
    SYSTEM_EMAIL_RESEND_API_KEY="development-only",
)
class AccountEmailChangeConcurrencyTests(TransactionTestCase):
    def test_two_accounts_can_confirm_the_same_destination_only_once(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL advisory and row locks required")
        cache.clear()
        requests = []
        for i in range(2):
            user = User.objects.create_user(email=f"candidate-{i}@example.test")
            credentials = credentials_for(user)
            credentials.session.public_key = str(i + 1) * 64
            credentials.session.save(update_fields=("public_key",))
            old_client = AccountEmailChangeTests.bearer(credentials)
            def post(action, **body):
                return AccountEmailChangeTests.post(self, action, client=old_client, **body)
            started = post("start", new_email="target@example.test")
            self.assertEqual(started.status_code, 202, started.data)
            request_id = started.data["request_id"]
            current = CommunityChatEmailChangeJob.objects.get(change_id=request_id, kind="current_code")
            code = json.loads(decrypt_signup_email(current.ciphertext))["code"]
            self.assertEqual(post("verify-current", request_id=request_id, code=code).status_code, 200)
            target = CommunityChatEmailChangeJob.objects.get(change_id=request_id, kind="new_code")
            requests.append((credentials, request_id, json.loads(decrypt_signup_email(target.ciphertext))["code"]))

        barrier = Barrier(2)

        def finish_request(values):
            close_old_connections()
            try:
                credentials, request_id, code = values
                client = AccountEmailChangeTests.bearer(credentials)
                barrier.wait(timeout=10)
                return AccountEmailChangeTests.post(self, "confirm", client=client, request_id=request_id, code=code).status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(pool.map(finish_request, requests), [200, 409])
        self.assertEqual(User.objects.filter(email="target@example.test").count(), 1)
        self.assertEqual(CommunityChatEmailChangeJob.objects.filter(kind="receipt").count(), 2)
