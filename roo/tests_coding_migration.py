from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from core.models import User

class MicrorooMigrationPreservationTests(TransactionTestCase):
    """Prove whole-Roo production rows are copied exactly into microroo."""

    migrate_from = ("roo", "0029_boostpostadmission_and_more")
    migrate_to = ("roo", "0030_microroo_and_coding_billing")

    def setUp(self):
        super().setUp()
        # Create the unchanged FK target while the current core schema/model
        # still agree. This test migrates only the roo app, so its historical
        # project state can otherwise describe an older core.User than the
        # physical core_user table.
        self.user_id = User.objects.create_user(email="migration@mlai.au").id
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        PointsAccount = old_apps.get_model("roo", "PointsAccount")
        Ledger = old_apps.get_model("roo", "Ledger")
        PointsAccount.objects.create(
            user_id=self.user_id,
            balance=17,
            earned_balance=12,
            purchased_topup_balance=5,
            lifetime_earned=24,
            lifetime_purchased_topup=5,
            lifetime_spent=12,
            expired_or_reversed_points=3,
        )
        self.ledger_id = Ledger.objects.create(
            user_id=self.user_id,
            delta=-7,
            points_delta=-7,
            kind="SPEND",
            source="TOOLS",
            idempotency_key="migration-preservation",
        ).id
        self.legacy_ledger_id = Ledger.objects.create(
            user_id=self.user_id,
            delta=None,
            points_delta=4,
            source="LEGACY",
            idempotency_key="migration-legacy-preservation",
        ).id

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        # Always restore the latest schema for tests that follow this class.
        MigrationExecutor(connection).migrate([self.migrate_to])
        super().tearDown()

    def test_balances_ledger_and_pricing_are_preserved(self):
        PointsAccount = self.apps.get_model("roo", "PointsAccount")
        Ledger = self.apps.get_model("roo", "Ledger")
        CodingPricingVersion = self.apps.get_model("roo", "CodingPricingVersion")
        account = PointsAccount.objects.get(user_id=self.user_id)
        self.assertTrue(account.microroo_initialized)
        self.assertEqual(account.balance_microroo, 17_000_000)
        self.assertEqual(account.earned_balance_microroo, 12_000_000)
        self.assertEqual(account.purchased_topup_balance_microroo, 5_000_000)
        self.assertEqual(account.lifetime_earned_microroo, 24_000_000)
        self.assertEqual(account.lifetime_purchased_topup_microroo, 5_000_000)
        self.assertEqual(account.lifetime_spent_microroo, 12_000_000)
        self.assertEqual(account.expired_or_reversed_microroo, 3_000_000)
        ledger = Ledger.objects.get(id=self.ledger_id)
        self.assertEqual(ledger.delta_microroo, -7_000_000)
        self.assertEqual(ledger.points_delta_microroo, -7_000_000)
        legacy_ledger = Ledger.objects.get(id=self.legacy_ledger_id)
        self.assertEqual(legacy_ledger.delta_microroo, 4_000_000)
        self.assertEqual(legacy_ledger.points_delta_microroo, 4_000_000)
        pricing = CodingPricingVersion.objects.get(version="do-kimi-k3-2026-08")
        self.assertEqual(str(pricing.margin_multiplier), "1.300000")
