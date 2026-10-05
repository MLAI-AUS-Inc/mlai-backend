"""Persistence regression suite; requires separately approved test migrations."""

import threading

from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature, tag
from django.contrib.auth import get_user_model

from organizations.models import Organization
from integrations.models import ExternalFinancialRecord, ExternalServiceConnection
from integrations.services.financial_records import FinancialRecordOwnershipError, upsert_financial_record


class FinancialRecordOwnershipTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(email="financial-owner@example.test")
        self.connections = [
            ExternalServiceConnection.objects.create(
                user=user, organization=Organization.objects.create(domain=f"startup-{i}.example"),
                provider="xero", external_account_id="shared-account",
            )
            for i in (1, 2)
        ]

    def write(self, connection, amount):
        return upsert_financial_record(
            connection=connection, external_record_id="invoice-1",
            defaults={"record_type": "xero_invoice", "amount": amount},
        )

    def test_second_startup_sync_cannot_reassign_record(self):
        original, _ = self.write(self.connections[0], 10)
        with self.assertRaises(FinancialRecordOwnershipError):
            self.write(self.connections[1], 20)
        original.refresh_from_db()
        self.assertEqual(original.connection_id, self.connections[0].pk)
        self.assertEqual(original.organization_id, self.connections[0].organization_id)
        self.assertEqual(original.amount, 10)
        self.assertEqual(ExternalFinancialRecord.objects.count(), 1)

    def test_repeated_sync_updates_same_record(self):
        original, created = self.write(self.connections[0], 10)
        updated, created_again = self.write(self.connections[0], 20)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(original.pk, updated.pk)
        self.assertEqual(updated.amount, 20)


@tag("postgres-only")
@skipUnlessDBFeature("has_select_for_update")
class FinancialRecordOwnershipRaceTests(TransactionTestCase):
    def test_competing_first_syncs_preserve_the_winning_tenant(self):
        user = get_user_model().objects.create_user(email="financial-race@example.test")
        owners = [
            ExternalServiceConnection.objects.create(
                user=user, organization=Organization.objects.create(domain=f"race-{i}.example.test"),
                provider="xero", external_account_id="shared-race-account",
            ) for i in (1, 2)
        ]
        barrier = threading.Barrier(2)
        results, errors = [], []

        def sync(owner):
            close_old_connections()
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SET statement_timeout = '8s'")
                barrier.wait(timeout=5)
                _, created = upsert_financial_record(
                    connection=owner, external_record_id="race-invoice",
                    defaults={"record_type": "xero_invoice", "amount": 10},
                )
                results.append((owner.pk, created))
            except FinancialRecordOwnershipError:
                results.append((owner.pk, "conflict"))
            except Exception as exc:
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=sync, args=(owner,)) for owner in owners]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=12)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        winner = next(owner for owner, result in results if result is True)
        self.assertEqual(sum(result == "conflict" for _, result in results), 1)
        record = ExternalFinancialRecord.objects.get(external_record_id="race-invoice")
        self.assertEqual(record.connection_id, winner)
        self.assertEqual(record.organization_id, next(owner.organization_id for owner in owners if owner.pk == winner))
