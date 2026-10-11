import hashlib
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from community_chat import adapter, inbox_erasure
from community_chat.inbox_accounts import account_key
from community_chat.models import AccountDeletionTask

COMMUNITY = "11111111-1111-4111-8111-111111111111"
KEY = "a" * 64


@override_settings(MLAI_CHAT_ACCOUNT_KEY_SECRET="synthetic-erasure-account-secret-32-bytes")
class ErasureTransportTests(SimpleTestCase):
    def replies(self):
        keys = sorted({account_key(COMMUNITY, 42), hashlib.sha256(b"singleton:" + bytes.fromhex(KEY)).hexdigest()})
        return [({"community_id": COMMUNITY, "member_account_protocols": [inbox_erasure.PROTOCOL]}, None)] + [
            ({"community_id": COMMUNITY, "status": "erased", "account_key": key,
              "deleted_rows": 3, "remaining_rows": 0}, None) for key in keys
        ]

    @patch("community_chat.inbox_erasure.adapter._request")
    def test_grouped_and_legacy_singletons_are_erased_without_retaining_identity(self, request):
        request.side_effect = self.replies()
        result = inbox_erasure.purge_relay_cursors(42, [KEY, KEY])
        self.assertEqual(result, {"deleted_rows": 6, "remaining_rows": 0, "verified_targets": 2})
        self.assertEqual(request.call_count, 3)
        self.assertTrue(all(call.args[0] == "DELETE" for call in request.call_args_list[1:]))

    @patch("community_chat.inbox_erasure.adapter._request")
    def test_partial_or_malformed_evidence_never_confirms_erasure(self, request):
        for field, value in [("remaining_rows", 1), ("remaining_rows", False),
                             ("deleted_rows", -1), ("deleted_rows", "3"),
                             ("community_id", "other"), ("account_key", KEY)]:
            with self.subTest(field=field, value=value):
                replies = self.replies()
                replies[1][0][field] = value
                request.side_effect = replies
                with self.assertRaises(inbox_erasure.ErasureVerificationError):
                    inbox_erasure.purge_relay_cursors(42, [KEY])

    @patch("community_chat.inbox_erasure.adapter._request")
    def test_missing_capability_fails_closed(self, request):
        request.return_value = ({"member_account_protocols": [], "community_id": COMMUNITY}, None)
        with self.assertRaises(adapter.MembershipAdapterUnavailable):
            inbox_erasure.purge_relay_cursors(42, [KEY])
        request.assert_called_once()


@override_settings(COMMUNITY_CHAT_INBOX_ERASURE_ENABLED=True)
class ErasureTaskTests(SimpleTestCase):
    @override_settings(COMMUNITY_CHAT_INBOX_ERASURE_ENABLED=False)
    def test_disabled_command_does_not_touch_the_queue(self):
        from io import StringIO
        from django.core.management import call_command
        with patch("community_chat.management.commands.run_inbox_erasure_worker.AccountDeletionTask.objects") as tasks:
            output = StringIO()
            call_command("run_inbox_erasure_worker", stdout=output)
            self.assertIn("disabled", output.getvalue())
            tasks.filter.assert_not_called()

    def test_deletion_scopes_include_the_cursor_boundary(self):
        from community_chat.deletion_tasks import targets_for
        from community_chat.models import AccountDeletionRequest
        for scope in AccountDeletionRequest.Scope.values:
            self.assertIn(inbox_erasure.TARGET, targets_for(scope))

    def run_task(self, *, access=True, active=False, current_attempt=1, failure=None, complete=True):
        task = SimpleNamespace(pk="task", target=inbox_erasure.TARGET, attempts=1, request_id="request",
                               request=SimpleNamespace(user_id=42))
        record = SimpleNamespace(user_id=42, tasks=Mock())
        record.tasks.filter.return_value.exists.return_value = access
        current = SimpleNamespace(status=AccountDeletionTask.Status.PROCESSING, attempts=current_attempt)
        devices = Mock()
        devices.filter.return_value.exclude.return_value.exists.return_value = active
        devices.filter.return_value.order_by.return_value.values_list.return_value.distinct.return_value = [KEY]
        with ExitStack() as stack:
            stack.enter_context(patch.object(inbox_erasure.transaction, "atomic", return_value=nullcontext()))
            user = stack.enter_context(patch.object(inbox_erasure, "get_user_model"))
            user.return_value.objects.select_for_update.return_value.get.return_value = SimpleNamespace(pk=42)
            user.return_value.DoesNotExist = RuntimeError
            stack.enter_context(patch.object(inbox_erasure.AccountDeletionRequest, "objects", Mock()))
            inbox_erasure.AccountDeletionRequest.objects.select_for_update.return_value.get.return_value = record
            stack.enter_context(patch.object(inbox_erasure.AccountDeletionTask, "objects", Mock()))
            inbox_erasure.AccountDeletionTask.objects.select_for_update.return_value.get.return_value = current
            stack.enter_context(patch.object(inbox_erasure.CommunityChatDevice, "objects", devices))
            stack.enter_context(patch.object(inbox_erasure.deletion_tasks, "claim_task", return_value=task))
            completed = stack.enter_context(patch.object(inbox_erasure.deletion_tasks, "complete_task", return_value=complete))
            failed = stack.enter_context(patch.object(inbox_erasure.deletion_tasks, "fail_task"))
            purge = stack.enter_context(patch.object(inbox_erasure, "purge_relay_cursors", side_effect=failure,
                                                     return_value={"remaining_rows": 0, "deleted_rows": 2}))
            result = inbox_erasure.execute_task("task")
            return result, completed, failed, purge

    @override_settings(COMMUNITY_CHAT_INBOX_ERASURE_ENABLED=False)
    @patch("community_chat.inbox_erasure.deletion_tasks.claim_task")
    def test_flag_off_does_not_claim_or_connect(self, claim):
        self.assertFalse(inbox_erasure.execute_task("task"))
        claim.assert_not_called()

    def test_access_cleanup_and_all_device_revocations_must_precede_erasure(self):
        for options in ({"access": False}, {"active": True}):
            result, complete, failed, purge = self.run_task(**options)
            self.assertFalse(result)
            complete.assert_not_called()
            purge.assert_not_called()
            failed.assert_called_once_with("task", attempt=1, error_code="verification_failed")

    def test_verified_cleanup_completes_only_the_leased_target(self):
        result, complete, failed, purge = self.run_task()
        self.assertTrue(result)
        complete.assert_called_once_with("task", attempt=1, verified_counts={"remaining_rows": 0, "deleted_rows": 2})
        purge.assert_called_once_with(42, [KEY])
        failed.assert_not_called()

    def test_stale_workers_and_failed_transport_never_complete(self):
        result, complete, failed, purge = self.run_task(current_attempt=2)
        self.assertFalse(result)
        complete.assert_not_called()
        failed.assert_not_called()
        purge.assert_not_called()
        for error, code in [(adapter.MembershipAdapterUnavailable("test"), "provider_unavailable"),
                            (adapter.MembershipAdapterConflict("test"), "operator_review_required")]:
            result, complete, failed, _ = self.run_task(failure=error)
            self.assertFalse(result)
            complete.assert_not_called()
            failed.assert_called_once_with("task", attempt=1, error_code=code)
