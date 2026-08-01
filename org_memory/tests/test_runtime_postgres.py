from unittest import skipUnless

from django.db import connection
from django.test import TestCase

from organizations.models import Organization
from org_memory.kernel import create_work_item
from org_memory.models import MemoryWorkStatus, MemoryWorkTaskType
from org_memory.runtime import claim_memory_work


@skipUnless(connection.vendor == "postgresql", "PostgreSQL-only runtime contract")
class PostgreSQLMemoryRuntimeTests(TestCase):
    def test_claim_locks_only_work_item_when_nullable_relations_are_empty(self):
        organization = Organization.objects.create(
            name="Postgres Runtime",
            domain="postgres-runtime.mlai.test",
        )
        work_item, _created = create_work_item(
            organization=organization,
            provider="linear",
            task_type=MemoryWorkTaskType.RECONCILE,
            idempotency_key="postgres-runtime-nullable-claim",
        )

        claim = claim_memory_work(worker_id="postgres-runtime-worker")

        self.assertIsNotNone(claim)
        self.assertEqual(claim.work_item_id, work_item.pk)
        work_item.refresh_from_db()
        self.assertEqual(work_item.status, MemoryWorkStatus.PROCESSING)
