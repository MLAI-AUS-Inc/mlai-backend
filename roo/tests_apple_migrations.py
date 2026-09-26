"""Exercise the two approved additive migrations with an existing member balance."""

from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class AppleReadinessMigrationTests(TransactionTestCase):
    def test_existing_points_and_deletion_requests_are_preserved(self):
        executor = MigrationExecutor(connection)
        after = executor.loader.graph.leaf_nodes()
        previous = {"roo": "0040_merge_coworking_operations_office_manager", "community_chat": "0012_member_onboarding"}
        before = [(app, previous.get(app, name)) for app, name in after]
        executor.migrate(before)
        try:
            old = executor.loader.project_state(before).apps
            user = old.get_model(settings.AUTH_USER_MODEL).objects.create(email="migration-iap@example.com")
            old.get_model("roo", "PointsAccount").objects.create(
                user_id=user.pk, balance=12, earned_balance=10, purchased_topup_balance=2,
                balance_microroo=12_500_000, earned_balance_microroo=10_500_000,
                purchased_topup_balance_microroo=2_000_000, microroo_initialized=True,
            )
            ledger = old.get_model("roo", "Ledger").objects.create(
                user_id=user.pk, delta=12, delta_microroo=12_500_000, kind="EARN", source="TASK",
            )
            request = old.get_model("community_chat", "AccountDeletionRequest").objects.create(
                user_id=user.pk, scope="shared_mlai_account", policy_version="test",
            )
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            current = executor.loader.project_state(after).apps
            account = current.get_model("roo", "PointsAccount").objects.get(user_id=user.pk)
            self.assertEqual(account.balance_microroo, 12_500_000)
            self.assertEqual(account.earned_balance_microroo, 10_500_000)
            self.assertEqual(account.purchased_topup_balance_microroo, 2_000_000)
            self.assertEqual(account.balance, 12)
            self.assertEqual(account.digital_balance_microroo, 0)
            self.assertEqual(account.digital_refund_debt_microroo, 0)
            updated = current.get_model("roo", "Ledger").objects.get(pk=ledger.pk)
            self.assertEqual(updated.delta_microroo, 12_500_000)
            self.assertEqual(updated.digital_delta_microroo, 0)
            self.assertIsNone(updated.purchased_delta_microroo)
            self.assertIsNone(updated.refund_of_id)
            self.assertEqual(current.get_model("community_chat", "AccountDeletionRequest").objects.get(pk=request.pk).status, "requested")
            self.assertFalse(current.get_model("community_chat", "AccountDeletionTask").objects.exists())
            self.assertTrue(current.get_model(settings.AUTH_USER_MODEL).objects.filter(pk=user.pk).exists())
        finally:
            MigrationExecutor(connection).migrate(after)
