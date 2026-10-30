"""Server-owned callback cohort fence, with DB/network disabled."""
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from integrations.services.message_sync import member_callback_authority as fence


class CallbackAuthorityFenceTests(SimpleTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(fence.connection, 'in_atomic_block', True))
        self.relevant = self.stack.enter_context(patch.object(fence, 'relevant_user_ids', return_value={1, 9}))
        self.users = self.stack.enter_context(patch.object(fence, 'get_user_model'))
        self.users.return_value.objects.select_for_update.return_value.filter.return_value.order_by.return_value.values_list.return_value = [1, 9]

    def test_users_are_locked_sorted_before_downstream_account_access(self):
        with fence.callback_user_fence({}):
            fence.assert_callback_user_locked(9)
            fence.assert_callback_user_locked(1)
            with self.assertRaises(fence.CallbackAuthorityChanged):
                fence.assert_callback_user_locked(5)
        query = self.users.return_value.objects.select_for_update.return_value.filter
        query.assert_called_once_with(pk__in={1, 9})
        query.return_value.order_by.assert_called_once_with('pk')
        fence.assert_callback_user_locked(5)  # Context cannot leak to another operation.

    def test_new_owner_before_or_after_channel_work_retries_without_new_lock(self):
        for sequence in ([{9}, {1, 9}], [{9}, {9}, {1, 9}]):
            self.relevant.side_effect = sequence
            self.users.return_value.objects.select_for_update.return_value.filter.return_value.order_by.return_value.values_list.return_value = [9]
            with self.assertRaises(fence.CallbackAuthorityChanged):
                with fence.callback_user_fence({}):
                    fence.assert_callback_user_locked(9)
            fence.assert_callback_user_locked(1)

    def test_no_fence_user_or_grant_assertions_preserve_baseline_without_queries(self):
        with patch.object(fence.SlackDmMirrorGrant.objects, 'filter') as grants:
            fence.assert_callback_user_locked(12345)
            fence.assert_callback_grant_locked(12345)
        grants.assert_not_called()

    def test_payload_cannot_supply_a_locked_user_proof(self):
        self.relevant.return_value = {9}
        self.users.return_value.objects.select_for_update.return_value.filter.return_value.order_by.return_value.values_list.return_value = [9]
        with fence.callback_user_fence({'_locked_users': [1], '_sync_callback_owner_proof': [1]}):
            with self.assertRaises(fence.CallbackAuthorityChanged):
                fence.assert_callback_user_locked(1)

    def test_unknown_owner_is_rejected_before_token_refresh_or_authority_lock(self):
        from integrations.services import slack_dm_mirror as dm
        from integrations.services.slack_channel_membership import _locked
        self.relevant.return_value = {9}
        self.users.return_value.objects.select_for_update.return_value.filter.return_value.order_by.return_value.values_list.return_value = [9]
        with fence.callback_user_fence({}), patch.object(dm, '_refresh_slack_grant_token_if_due') as refresh:
            with self.assertRaises(fence.CallbackAuthorityChanged):
                dm._capture_slack_grant_api_authority(SimpleNamespace(user_id=1))
            with self.assertRaises(fence.CallbackAuthorityChanged):
                dm._lock_slack_grant_api_authority(SimpleNamespace(user_id=1), required_scopes=set())
            with self.assertRaises(fence.CallbackAuthorityChanged):
                _locked(SimpleNamespace(user_id=1), None, joined=False)
        refresh.assert_not_called()

    def test_retirement_and_app_rate_paths_cannot_discover_new_owner(self):
        from integrations.services import slack_dm_mirror as dm
        self.relevant.return_value = {9}
        self.users.return_value.objects.select_for_update.return_value.filter.return_value.order_by.return_value.values_list.return_value = [9]
        with fence.callback_user_fence({}), patch.object(fence.SlackDmMirrorGrant.objects, 'filter') as grants:
            grants.return_value.values_list.return_value.first.return_value = 1
            with self.assertRaises(fence.CallbackAuthorityChanged):
                dm._retire_ineligible_conversation(4, 'DTEST', reason='synthetic')
            with self.assertRaises(fence.CallbackAuthorityChanged):
                dm._schedule_automatic_history_reconciliation.__wrapped__(SimpleNamespace(user_id=1), reason='synthetic')

    def test_live_private_candidate_guard_precedes_grant_lock(self):
        from integrations.services import slack_dm_mirror as dm
        self.relevant.return_value = {9}
        self.users.return_value.objects.select_for_update.return_value.filter.return_value.order_by.return_value.values_list.return_value = [9]
        candidate = SimpleNamespace(grant=SimpleNamespace(user_id=1, slack_user_id='URECIPIENT'))
        with fence.callback_user_fence({}), patch.object(dm, '_normalize_private_slack_event', return_value={}), patch.object(dm.SlackDmMirrorConversation.objects, 'select_related') as conversations:
            conversations.return_value.filter.return_value = [candidate]
            with self.assertRaises(fence.CallbackAuthorityChanged):
                dm.ingest_slack_dm_event({'team_id': 'TTEST', 'event': {'type': 'message', 'channel': 'DTEST'},
                                         'authorizations': [{'user_id': 'URECIPIENT'}]})

    def test_fence_requires_own_transaction_and_disallows_unsafe_nesting(self):
        with patch.object(fence.connection, 'in_atomic_block', False), self.assertRaises(RuntimeError):
            with fence.callback_user_fence({}):
                pass
        with fence.callback_user_fence({}), self.assertRaises(RuntimeError):
            with fence.callback_user_fence({}):
                pass


class CallbackCohortSelectionTests(SimpleTestCase):
    def test_signed_actor_authorized_recipients_and_retired_private_owners_are_included(self):
        grants, private = MagicMock(), MagicMock()
        grants.filter.return_value.order_by.return_value.values_list.return_value.distinct.return_value = [9]
        private.filter.return_value.order_by.return_value.values_list.return_value.distinct.return_value = [1]
        with patch.object(fence.SlackDmMirrorGrant, 'objects', grants), patch.object(fence.SlackDmMirrorConversation, 'objects', private):
            result = fence.relevant_user_ids({'team_id': 'TTEST', 'event': {'type': 'message', 'channel': 'CTEST', 'user': 'UACTOR'},
                'authorizations': [{'user_id': 'URECIPIENT'}], '_locked_users': [44]})
        self.assertEqual(result, {1, 9})
        self.assertEqual(grants.filter.call_args.kwargs['slack_user_id__in'], {'UACTOR', 'URECIPIENT'})
        private.filter.assert_called_once_with(slack_workspace_id='TTEST', slack_conversation_id='CTEST')

    def test_cohort_overflow_refuses_before_any_user_lock(self):
        with self.assertRaises(fence.CallbackAuthorityChanged):
            fence._bounded_ids(list(range(fence.MAX_CALLBACK_USERS + 1)))
