import hashlib

from django.test import TestCase, override_settings

from organizations.models import Organization
from org_memory.embeddings import (
    EmbeddingInvariantError,
    store_chunk_embedding,
)
from org_memory.kernel import capture_source_version
from org_memory.models import MemoryChunkEmbedding
from org_memory.search import search_memory_chunks


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FailingEmbeddingProvider:
    def embed(self, text, *, model, dimensions):
        raise RuntimeError("provider unavailable")


@override_settings(
    ORG_MEMORY_EMBEDDING_MODEL="text-embedding-3-small",
    ORG_MEMORY_EMBEDDING_VERSION="openai-text-embedding-3-small-v1",
    ORG_MEMORY_EMBEDDING_DIMENSIONS=1536,
)
class MemorySearchFallbackTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Search Test",
            domain="search-test.mlai.test",
        )

    def _capture(self, text, *, external_id="search-1", accessible=True):
        return capture_source_version(
            organization=self.organization,
            provider="linear",
            external_account_id="search-account",
            source_type="issue",
            external_id=external_id,
            version_key="v1",
            content_hash=digest(text),
            classification="internal",
            acl={
                "is_accessible": accessible,
                "principal_refs": ["team:search"],
            },
            chunks=[{"ordinal": 0, "text": text, "token_count": 8}],
        )

    def test_text_search_remains_available_when_embedding_provider_fails(self):
        self._capture("The annual planning retreat is in Hobart in September.")

        result = search_memory_chunks(
            organization=self.organization,
            query="planning retreat Hobart",
            embedding_provider=FailingEmbeddingProvider(),
        )

        self.assertEqual(len(result.hits), 1)
        self.assertEqual(result.lanes, ("text",))
        self.assertTrue(result.degraded)
        self.assertTrue(result.degraded_reasons[0].startswith("vector_unavailable:"))

    def test_search_filters_inaccessible_and_cross_organization_evidence(self):
        self._capture("Visible roadmap priority is partner onboarding.", external_id="visible")
        self._capture(
            "Hidden roadmap priority is acquisition diligence.",
            external_id="hidden",
            accessible=False,
        )
        other = Organization.objects.create(name="Other", domain="other-search.mlai.test")
        capture_source_version(
            organization=other,
            provider="linear",
            external_account_id="other-account",
            source_type="issue",
            external_id="other",
            version_key="v1",
            content_hash=digest("Other roadmap priority is confidential."),
            classification="internal",
            acl={"is_accessible": True, "principal_refs": ["team:other"]},
            chunks=[{"ordinal": 0, "text": "Other roadmap priority is confidential."}],
        )

        result = search_memory_chunks(
            organization=self.organization,
            query="roadmap priority",
            generate_vector=False,
        )

        self.assertEqual([hit.chunk.source_version.source.external_id for hit in result.hits], ["visible"])

    def test_embedding_versions_are_immutable_and_promoted_without_mixing(self):
        _source, version, _created = self._capture("Versioned vector evidence.")
        chunk = version.chunks.get()
        first_vector = [1.0] + [0.0] * 1535
        second_vector = [0.0, 1.0] + [0.0] * 1534

        first, created = store_chunk_embedding(
            chunk=chunk,
            vector=first_vector,
            version="embedding-v1",
        )
        second, created_second = store_chunk_embedding(
            chunk=chunk,
            vector=second_vector,
            version="embedding-v2",
        )

        first.refresh_from_db()
        self.assertTrue(created)
        self.assertTrue(created_second)
        self.assertFalse(first.is_current)
        self.assertTrue(second.is_current)
        self.assertEqual(MemoryChunkEmbedding.objects.filter(chunk=chunk).count(), 2)
        with self.assertRaises(EmbeddingInvariantError):
            store_chunk_embedding(
                chunk=chunk,
                vector=second_vector,
                version="embedding-v1",
            )
