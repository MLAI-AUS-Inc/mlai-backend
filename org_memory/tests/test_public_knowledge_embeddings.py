import hashlib
from unittest import skipUnless

from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from organizations.models import Organization
from org_memory.models import PublicKnowledgeItem, PublicKnowledgeStatus
from org_memory.public_knowledge import (
    PUBLIC_EMBEDDING_MAX_CHARS,
    PublicKnowledgeError,
    answer_public_knowledge_query,
    embed_public_knowledge_item,
    generate_public_query_embedding,
    public_embedding_text,
    schedule_public_knowledge_embedding,
    search_public_knowledge,
)


EMBEDDING_SETTINGS = dict(
    ORG_MEMORY_EMBEDDING_MODEL="text-embedding-3-small",
    ORG_MEMORY_EMBEDDING_VERSION="openai-text-embedding-3-small-v1",
    ORG_MEMORY_EMBEDDING_DIMENSIONS=1536,
)


class StubEmbeddingProvider:
    """Deterministic unit vector so cosine ordering is predictable."""

    def __init__(self, *, axis: int = 0):
        self.axis = axis
        self.calls = []

    def embed(self, text, *, model, dimensions):
        self.calls.append(text)
        vector = [0.0] * dimensions
        vector[self.axis] = 1.0
        return vector


class FailingEmbeddingProvider:
    def embed(self, text, *, model, dimensions):
        raise RuntimeError("embedding provider is unreachable")


@override_settings(**EMBEDDING_SETTINGS)
class PublicEmbeddingTextTests(TestCase):
    def test_combines_title_and_body(self):
        item = PublicKnowledgeItem(title="Coworking", body="Open weekdays.")
        self.assertEqual(public_embedding_text(item), "Coworking\n\nOpen weekdays.")

    def test_truncates_to_the_public_contract(self):
        item = PublicKnowledgeItem(title="T", body="b" * 30_000)
        self.assertEqual(
            len(public_embedding_text(item)),
            PUBLIC_EMBEDDING_MAX_CHARS,
        )

    def test_respects_a_lower_configured_embedding_ceiling(self):
        item = PublicKnowledgeItem(title="T", body="b" * 5_000)
        with override_settings(ORG_MEMORY_EMBEDDING_MAX_CHARS=64):
            self.assertEqual(len(public_embedding_text(item)), 64)


@override_settings(**EMBEDDING_SETTINGS)
class PublicQueryEmbeddingTests(TestCase):
    def test_returns_a_vector_from_the_provider(self):
        vector = generate_public_query_embedding(
            "when is coworking open",
            provider=StubEmbeddingProvider(),
        )
        self.assertIsNotNone(vector)
        self.assertEqual(len(vector), 1536)

    def test_degrades_to_none_when_the_provider_fails(self):
        self.assertIsNone(
            generate_public_query_embedding(
                "when is coworking open",
                provider=FailingEmbeddingProvider(),
            )
        )


@override_settings(**EMBEDDING_SETTINGS)
class PublicKnowledgeAnswerTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Public Brain",
            domain="public-brain.test",
        )

    def _item(self, *, key="coworking", title="Coworking", body="Open weekdays."):
        item = PublicKnowledgeItem(
            organization=self.organization,
            public_key=key,
            revision=1,
            title=title,
            body=body,
            tags=[],
            status=PublicKnowledgeStatus.ACTIVE,
            content_hash=hashlib.sha256(key.encode("utf-8")).hexdigest(),
            published_at=timezone.now(),
        )
        item.full_clean()
        item.save()
        return item

    def test_rejects_an_overlong_query_before_embedding(self):
        provider = StubEmbeddingProvider()
        with self.assertRaises(PublicKnowledgeError):
            answer_public_knowledge_query(
                query="q" * 501,
                organization_id=self.organization.pk,
                embedding_provider=provider,
            )
        self.assertEqual(provider.calls, [])

    def test_abstains_with_a_warnings_field(self):
        payload = answer_public_knowledge_query(
            query="unpublished topic",
            organization_id=self.organization.pk,
            semantic=False,
        )
        self.assertEqual(payload["status"], "abstained")
        self.assertEqual(payload["warnings"], [])

    def test_answers_from_the_text_lane(self):
        self._item()
        payload = answer_public_knowledge_query(
            query="coworking",
            organization_id=self.organization.pk,
            semantic=False,
        )
        self.assertEqual(payload["status"], "answered")
        self.assertIn("Open weekdays.", payload["answer"])
        self.assertEqual(payload["citations"][0]["public_key"], "coworking")

    def test_schedule_embeds_the_item_after_commit(self):
        item = self._item()
        provider = StubEmbeddingProvider()
        with self.captureOnCommitCallbacks(execute=True):
            schedule_public_knowledge_embedding(item, provider=provider)
        self.assertEqual(provider.calls, ["Coworking\n\nOpen weekdays."])

    def test_schedule_never_raises_when_embedding_fails(self):
        item = self._item()
        with self.captureOnCommitCallbacks(execute=True):
            schedule_public_knowledge_embedding(
                item,
                provider=FailingEmbeddingProvider(),
            )
        item.refresh_from_db()
        self.assertIsNone(item.embedding)
        self.assertEqual(item.status, PublicKnowledgeStatus.ACTIVE)

    def test_schedule_skips_an_item_revoked_before_commit(self):
        item = self._item()
        provider = StubEmbeddingProvider()
        with self.captureOnCommitCallbacks(execute=True):
            schedule_public_knowledge_embedding(item, provider=provider)
            PublicKnowledgeItem.objects.filter(pk=item.pk).update(
                status=PublicKnowledgeStatus.REVOKED,
                revoked_at=timezone.now(),
            )
        self.assertEqual(provider.calls, [])


@skipUnless(connection.vendor == "postgresql", "pgvector-only retrieval contract")
@override_settings(**EMBEDDING_SETTINGS)
class PublicVectorLaneTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Public Brain",
            domain="public-brain.test",
        )

    def _item(self, *, key, title, body):
        item = PublicKnowledgeItem(
            organization=self.organization,
            public_key=key,
            revision=1,
            title=title,
            body=body,
            tags=[],
            status=PublicKnowledgeStatus.ACTIVE,
            content_hash=hashlib.sha256(key.encode("utf-8")).hexdigest(),
            published_at=timezone.now(),
        )
        item.full_clean()
        item.save()
        return item

    def test_embedding_is_stored_and_pinned_to_the_target(self):
        item = self._item(key="coworking", title="Coworking", body="Open weekdays.")
        embedded = embed_public_knowledge_item(
            item=item,
            provider=StubEmbeddingProvider(),
        )
        self.assertIsNotNone(embedded.embedding)
        self.assertEqual(embedded.embedding_model, "text-embedding-3-small")
        self.assertEqual(
            embedded.embedding_version,
            "openai-text-embedding-3-small-v1",
        )

    def test_vector_lane_surfaces_an_item_the_text_lane_misses(self):
        item = self._item(
            key="desks",
            title="Desk availability",
            body="Hot desks are bookable on weekdays.",
        )
        embed_public_knowledge_item(item=item, provider=StubEmbeddingProvider(axis=0))
        # A lexically unrelated query: only the vector lane can retrieve it.
        hits = search_public_knowledge(
            query="zzzz unrelated lexical tokens",
            organization_id=self.organization.pk,
            query_embedding=[1.0] + [0.0] * 1535,
        )
        self.assertEqual([hit.item.pk for hit in hits], [item.pk])
        self.assertEqual(hits[0].text_rank, None)
        self.assertEqual(hits[0].vector_rank, 1)

    def test_only_active_items_are_embedded(self):
        item = self._item(key="retired", title="Retired", body="Old policy.")
        PublicKnowledgeItem.objects.filter(pk=item.pk).update(
            status=PublicKnowledgeStatus.REVOKED,
            revoked_at=timezone.now(),
        )
        item.refresh_from_db()
        with self.assertRaises(PublicKnowledgeError):
            embed_public_knowledge_item(item=item, provider=StubEmbeddingProvider())
