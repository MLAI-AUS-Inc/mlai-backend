import hashlib
import json
from pathlib import Path
from unittest import skipUnless

from django.db import connection
from django.test import TestCase, override_settings

from organizations.models import Organization
from org_memory.embeddings import store_chunk_embedding
from org_memory.kernel import capture_source_version
from org_memory.search import refresh_search_vectors, search_memory_chunks


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def plan_index_names(node):
    names = set()
    if isinstance(node, dict):
        if node.get("Index Name"):
            names.add(node["Index Name"])
        for value in node.values():
            names.update(plan_index_names(value))
    elif isinstance(node, list):
        for value in node:
            names.update(plan_index_names(value))
    return names


@skipUnless(connection.vendor == "postgresql", "PostgreSQL-only search contract")
@override_settings(
    ORG_MEMORY_EMBEDDING_MODEL="text-embedding-3-small",
    ORG_MEMORY_EMBEDDING_VERSION="openai-text-embedding-3-small-v1",
    ORG_MEMORY_EMBEDDING_DIMENSIONS=1536,
)
class PostgreSQLMemorySearchTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Postgres Search",
            domain="postgres-search.mlai.test",
        )
        self.other_organization = Organization.objects.create(
            name="Other Postgres Search",
            domain="other-postgres-search.mlai.test",
        )

    def _capture(self, organization, text, external_id):
        _source, version, _created = capture_source_version(
            organization=organization,
            provider="linear",
            external_account_id=f"account-{external_id}",
            source_type="issue",
            external_id=external_id,
            version_key="v1",
            content_hash=digest(text),
            classification="internal",
            acl={"is_accessible": True, "principal_refs": [f"team:{external_id}"]},
            chunks=[{"ordinal": 0, "text": text}],
        )
        return version.chunks.get()

    def test_hybrid_search_uses_one_version_and_enforces_organization_filter(self):
        target = self._capture(
            self.organization,
            "Hobart retreat planning and company strategy.",
            "target",
        )
        other = self._capture(
            self.other_organization,
            "Hobart retreat confidential acquisition.",
            "other",
        )
        target_vector = [1.0] + [0.0] * 1535
        store_chunk_embedding(chunk=target, vector=target_vector)
        store_chunk_embedding(chunk=other, vector=target_vector)
        refresh_search_vectors(chunk_ids=(target.pk, other.pk))

        result = search_memory_chunks(
            organization=self.organization,
            query="Hobart retreat",
            query_embedding=target_vector,
            generate_vector=False,
        )

        self.assertEqual([hit.chunk.pk for hit in result.hits], [target.pk])
        self.assertEqual(result.lanes, ("text", "vector"))
        self.assertFalse(result.degraded)

    def test_required_indexes_exist_and_are_visible_in_query_plans(self):
        fixture_path = Path(__file__).resolve().parents[1] / "evals" / "postgres_search_plan.json"
        fixture = json.loads(fixture_path.read_text())
        chunk = self._capture(
            self.organization,
            "Board planning evidence for index verification.",
            "plan",
        )
        vector = [1.0] + [0.0] * 1535
        store_chunk_embedding(chunk=chunk, vector=vector)
        refresh_search_vectors(chunk_ids=(chunk.pk,))

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
            )
            existing = {row[0] for row in cursor.fetchall()}
            self.assertTrue(set(fixture["required_indexes"]).issubset(existing))
            cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute(
                """
                EXPLAIN (FORMAT JSON)
                SELECT id FROM org_memory_memorychunk
                WHERE search_vector @@ websearch_to_tsquery('english', %s)
                LIMIT 10
                """,
                ["board planning"],
            )
            text_plan = cursor.fetchone()[0]
            cursor.execute(
                """
                EXPLAIN (FORMAT JSON)
                SELECT id FROM org_memory_memorychunkembedding
                ORDER BY vector <=> %s::vector
                LIMIT 10
                """,
                [str(vector)],
            )
            vector_plan = cursor.fetchone()[0]

        self.assertIn("orgmem_chunk_search_gin", plan_index_names(text_plan))
        self.assertIn("orgmem_embed_vector_hnsw", plan_index_names(vector_plan))
