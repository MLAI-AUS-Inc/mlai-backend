import hashlib
import hmac
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from community_chat.adapter import MembershipAdapterConflict, MembershipAdapterUnavailable
from community_chat.inbox_accounts import account_key, bind_verified_device

COMMUNITY = "11111111-1111-4111-8111-111111111111"
PUBLIC_KEY = "a" * 64
SECRET = "synthetic-inbox-account-secret-32-bytes"


@override_settings(
    COMMUNITY_CHAT_MEMBER_ACCOUNTS_ENABLED=True,
    MLAI_CHAT_ACCOUNT_KEY_SECRET=SECRET,
)
class InboxAccountTests(SimpleTestCase):
    def replies(self, status="bound"):
        return [
            ({"member_account_protocols": ["member_accounts_v1"], "community_id": COMMUNITY}, None),
            ({"public_key": PUBLIC_KEY, "generation": 7}, None),
            ({"status": status, "public_key": PUBLIC_KEY,
              "account_key": account_key(COMMUNITY, 42), "generation": 7}, None),
        ]

    def test_hmac_is_stable_and_scoped_to_user_and_community(self):
        expected = hmac.new(SECRET.encode(), f"{COMMUNITY}:42".encode(), hashlib.sha256).hexdigest()
        self.assertEqual(account_key(COMMUNITY, 42), expected)
        self.assertNotEqual(expected, account_key(COMMUNITY, 43))
        self.assertNotEqual(expected, account_key("22222222-2222-4222-8222-222222222222", 42))
        self.assertEqual(len(bytes.fromhex(expected)), 32)

    @override_settings(MLAI_CHAT_ACCOUNT_KEY_SECRET="")
    def test_no_fallback_to_django_signing_secret(self):
        with self.assertRaises(MembershipAdapterUnavailable):
            account_key(COMMUNITY, 42)

    @patch("community_chat.inbox_accounts.adapter._request")
    def test_bind_captures_generation_and_sends_only_opaque_key(self, request):
        request.side_effect = self.replies()
        self.assertTrue(bind_verified_device(42, PUBLIC_KEY))
        call = request.call_args_list[-1]
        self.assertEqual(call.args, ("PUT", f"/v2/member-accounts/{PUBLIC_KEY}"))
        self.assertEqual(call.kwargs["json_body"], {"account_key": account_key(COMMUNITY, 42), "generation": 7})

    @patch("community_chat.inbox_accounts.adapter._request")
    def test_idempotent_retry_is_unchanged(self, request):
        request.side_effect = self.replies("unchanged")
        self.assertFalse(bind_verified_device(42, PUBLIC_KEY))

    @override_settings(COMMUNITY_CHAT_MEMBER_ACCOUNTS_ENABLED=False)
    @patch("community_chat.inbox_accounts.adapter._request")
    def test_disabled_is_noop(self, request):
        self.assertFalse(bind_verified_device(42, PUBLIC_KEY))
        request.assert_not_called()

    @patch("community_chat.inbox_accounts.adapter._request")
    def test_missing_protocol_fails_closed_without_bind(self, request):
        request.return_value = ({"member_account_protocols": [], "community_id": COMMUNITY}, None)
        with self.assertRaises(MembershipAdapterUnavailable):
            bind_verified_device(42, PUBLIC_KEY)
        self.assertEqual(request.call_count, 1)

    @patch("community_chat.inbox_accounts.adapter._request")
    def test_malformed_generations_never_write(self, request):
        for generation in (True, -1, 2**63, "7", None):
            request.reset_mock()
            request.side_effect = self.replies()[:1] + [({"public_key": PUBLIC_KEY, "generation": generation}, None)]
            with self.assertRaises(MembershipAdapterUnavailable):
                bind_verified_device(42, PUBLIC_KEY)
            self.assertEqual(request.call_count, 2)

    @patch("community_chat.inbox_accounts.adapter._request")
    def test_revocation_conflict_propagates(self, request):
        request.side_effect = self.replies()[:2] + [MembershipAdapterConflict("account_generation_conflict")]
        with self.assertRaises(MembershipAdapterConflict):
            bind_verified_device(42, PUBLIC_KEY)

    @patch("community_chat.inbox_accounts.adapter._request")
    def test_incorrect_echo_is_rejected(self, request):
        replies = self.replies()
        replies[-1][0]["account_key"] = "b" * 64
        request.side_effect = replies
        with self.assertRaises(MembershipAdapterUnavailable):
            bind_verified_device(42, PUBLIC_KEY)


class InboxVerificationBoundaryTests(SimpleTestCase):
    def confirm(self, bind_error=None):
        from community_chat import views
        from community_chat.models import DeviceBindingStatus

        user = SimpleNamespace(pk=42)
        request = SimpleNamespace(user=user, data={"public_key": PUBLIC_KEY})
        device = SimpleNamespace(status=DeviceBindingStatus.PENDING, verified_at=None, save=Mock())
        in_transaction = []

        @contextmanager
        def atomic():
            in_transaction.append(True)
            try:
                yield
            finally:
                in_transaction.pop()

        def bind(user_id, key):
            self.assertTrue(in_transaction)
            self.assertEqual((user_id, key), (42, PUBLIC_KEY))
            self.assertEqual(device.status, DeviceBindingStatus.PENDING)
            device.save.assert_not_called()
            if bind_error:
                raise bind_error

        with ExitStack() as stack:
            for name in ("_require_eligible", "_require_token_key", "_request_origin",
                         "enforce_bootstrap_limits", "_require_current_chat_credential_locked"):
                stack.enter_context(patch.object(views, name))
            stack.enter_context(patch.object(views, "_public_key", return_value=PUBLIC_KEY))
            stack.enter_context(patch.object(views.transaction, "atomic", side_effect=atomic))
            users = stack.enter_context(patch.object(views, "get_user_model"))
            users.return_value.objects.select_for_update.return_value.get.return_value = user
            devices = stack.enter_context(patch.object(views.CommunityChatDevice, "objects"))
            devices.select_for_update.return_value.filter.return_value.order_by.return_value.first.return_value = device
            stack.enter_context(patch.object(views.CommunityChatInviteAudit, "objects"))
            stack.enter_context(patch.object(views, "get_relay_membership", return_value=SimpleNamespace(is_member=True, role="member")))
            binding = stack.enter_context(patch.object(views, "bind_verified_device", side_effect=bind))
            stack.enter_context(patch.object(views, "_device_payload", return_value={}))
            stack.enter_context(patch.object(views, "public_chat_profile", return_value={}))
            stack.enter_context(patch("integrations.services.message_sync.device_recovery.lock_enrollment_recovery_grants", return_value=[]))
            recovery = stack.enter_context(patch("integrations.services.message_sync.device_recovery.schedule_enrollment_recovery"))
            response = views.ConfirmView().post(request)
        binding.assert_called_once()
        return response, device, recovery

    def test_binding_precedes_verified_commit_under_authority_locks(self):
        response, device, recovery = self.confirm()
        self.assertEqual(response.status_code, 200)
        device.save.assert_called_once()
        recovery.assert_called_once()

    def test_binding_conflict_does_not_verify_or_schedule_recovery(self):
        response, device, recovery = self.confirm(MembershipAdapterConflict("account_generation_conflict"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data, {"error": "device_authority_changed"})
        device.save.assert_not_called()
        recovery.assert_not_called()

    def test_binding_failure_keeps_verification_pending(self):
        response, device, recovery = self.confirm(MembershipAdapterUnavailable("adapter_unavailable"))
        self.assertEqual(response.status_code, 503)
        device.save.assert_not_called()
        recovery.assert_not_called()


class InboxBindingCommandTests(SimpleTestCase):
    def test_default_dry_run_reads_only_and_does_not_contact_adapter(self):
        from io import StringIO
        from community_chat.management.commands import bind_chat_device_accounts as command
        output = StringIO()
        with patch.object(command.CommunityChatDevice, "objects") as devices, patch.object(command, "bind_verified_device") as bind:
            devices.filter.return_value.order_by.return_value.values_list.return_value.__getitem__.return_value = [(10, 42), (11, 43)]
            command.Command(stdout=output).handle(apply=False, dry_run=True, limit=100, after_device_id=0)
        bind.assert_not_called()
        self.assertIn("mode=dry-run candidates=2", output.getvalue())
        self.assertIn("next_after_device_id=11", output.getvalue())

    @override_settings(COMMUNITY_CHAT_MEMBER_ACCOUNTS_ENABLED=False)
    def test_apply_requires_feature_and_invalid_limits_do_not_read(self):
        from django.core.management.base import CommandError
        from community_chat.management.commands import bind_chat_device_accounts as command
        with patch.object(command.CommunityChatDevice, "objects") as devices:
            for apply, limit in ((True, 100), (False, 0), (False, 10001)):
                with self.assertRaises(CommandError):
                    command.Command().handle(apply=apply, dry_run=False, limit=limit, after_device_id=0)
        devices.filter.assert_not_called()
