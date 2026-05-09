from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

from django.db.models import Q
from django.utils.text import slugify

from content_factory.models import KeywordStatus, ResearchedKeyword, WrittenArticle
from content_factory.topic_feedback import normalize_topic_feedback_keyword


GENERIC_TOPIC_TOKENS = {
    "a",
    "about",
    "an",
    "and",
    "are",
    "around",
    "as",
    "at",
    "be",
    "been",
    "being",
    "by",
    "can",
    "did",
    "do",
    "does",
    "doing",
    "example",
    "examples",
    "explained",
    "for",
    "from",
    "guide",
    "has",
    "have",
    "how",
    "in",
    "into",
    "is",
    "it",
    "mean",
    "meaning",
    "means",
    "of",
    "on",
    "or",
    "should",
    "simple",
    "that",
    "the",
    "this",
    "to",
    "used",
    "use",
    "using",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
    "without",
    "word",
    "words",
    "work",
    "works",
    "you",
    "your",
}


@dataclass
class CoveredTopicRecord:
    text: str
    normalized: str
    slug: str
    tokens: frozenset[str]
    canonical: str
    source: str
    reason: str
    article: Optional[WrittenArticle] = None
    keyword: Optional[ResearchedKeyword] = None


@dataclass
class CoveredTopicMatch:
    record: CoveredTopicRecord
    match_type: str
    similarity: float = 1.0

    @property
    def source(self) -> str:
        return self.record.source

    @property
    def reason(self) -> str:
        return self.record.reason

    @property
    def article(self) -> Optional[WrittenArticle]:
        return self.record.article

    @property
    def keyword(self) -> Optional[ResearchedKeyword]:
        return self.record.keyword


def normalize_topic_text(value: Any) -> str:
    text = str(value or "").replace("-", " ").replace("_", " ")
    text = re.sub(r"\bai\b", "artificial intelligence", text, flags=re.IGNORECASE)
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text)
    return normalize_topic_feedback_keyword(text)


def topic_content_tokens(value: Any) -> frozenset[str]:
    normalized = normalize_topic_text(value)
    tokens = [
        token
        for token in normalized.split()
        if len(token) > 1 and token not in GENERIC_TOPIC_TOKENS
    ]
    return frozenset(tokens)


def canonical_topic_content(value: Any) -> str:
    return " ".join(sorted(topic_content_tokens(value)))


def _topic_text_candidates(*values: Any) -> List[str]:
    seen: set[str] = set()
    texts: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        normalized = normalize_topic_text(text)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        texts.append(text)
    return texts


def _record_for_text(
    *,
    text: str,
    source: str,
    reason: str,
    article: Optional[WrittenArticle] = None,
    keyword: Optional[ResearchedKeyword] = None,
) -> Optional[CoveredTopicRecord]:
    normalized = normalize_topic_text(text)
    if not normalized:
        return None
    return CoveredTopicRecord(
        text=text,
        normalized=normalized,
        slug=slugify(normalized),
        tokens=topic_content_tokens(text),
        canonical=canonical_topic_content(text),
        source=source,
        reason=reason,
        article=article,
        keyword=keyword,
    )


def _add_record(
    memory: Dict[str, Any],
    *,
    text: str,
    source: str,
    reason: str,
    article: Optional[WrittenArticle] = None,
    keyword: Optional[ResearchedKeyword] = None,
) -> None:
    record = _record_for_text(
        text=text,
        source=source,
        reason=reason,
        article=article,
        keyword=keyword,
    )
    if record is None:
        return
    key = (record.normalized, record.source, getattr(article, "id", None), getattr(keyword, "id", None))
    if key in memory["seen"]:
        return
    memory["seen"].add(key)
    memory["records"].append(record)
    memory["exact"].setdefault(record.normalized, record)
    if record.slug:
        memory["slugs"].setdefault(record.slug, record)


def build_topic_coverage_memory(organization, *, article_limit: Optional[int] = None) -> Dict[str, Any]:
    memory: Dict[str, Any] = {"records": [], "exact": {}, "slugs": {}, "seen": set()}

    article_qs = WrittenArticle.objects.filter(organization=organization).order_by("-created_at")
    if article_limit is not None:
        article_qs = article_qs[:article_limit]
    for article in article_qs:
        for text in _topic_text_candidates(article.primary_keyword, article.title, article.slug):
            _add_record(
                memory,
                text=text,
                source="written_article",
                reason="written_article",
                article=article,
            )

    keyword_qs = (
        ResearchedKeyword.objects.filter(organization=organization)
        .filter(Q(status__in=[KeywordStatus.WRITTEN, KeywordStatus.SKIPPED]) | Q(written_article_id__isnull=False))
        .select_related("written_article")
    )
    for keyword in keyword_qs:
        article = keyword.written_article
        reason = "written_keyword" if keyword.status == KeywordStatus.WRITTEN or article else "skipped_keyword"
        for text in _topic_text_candidates(
            keyword.keyword,
            getattr(article, "primary_keyword", ""),
            getattr(article, "title", ""),
        ):
            _add_record(
                memory,
                text=text,
                source="researched_keyword",
                reason=reason,
                article=article,
                keyword=keyword,
            )

    memory.pop("seen", None)
    return memory


def _close_topic_match(candidate_text: str, candidate_tokens: frozenset[str], record: CoveredTopicRecord) -> Optional[float]:
    if len(candidate_tokens) < 2 or len(record.tokens) < 2:
        return None

    if candidate_tokens == record.tokens:
        return 1.0

    overlap = len(candidate_tokens & record.tokens)
    if overlap < 2:
        return None

    # A shorter, generic query is covered by a longer article keyword/title.
    # The opposite direction remains eligible, so "AI for startups" is not hidden
    # just because a generic "what is artificial intelligence" article exists.
    if candidate_tokens.issubset(record.tokens):
        return overlap / max(len(record.tokens), 1)

    union = len(candidate_tokens | record.tokens)
    jaccard = overlap / max(union, 1)
    if jaccard >= 0.8:
        return jaccard

    if candidate_text and record.canonical:
        ratio = SequenceMatcher(None, canonical_topic_content(candidate_text), record.canonical).ratio()
        if ratio >= 0.9:
            return ratio

    return None


def match_covered_topic(
    *,
    keyword: str,
    title: str = "",
    memory: Optional[Dict[str, Any]] = None,
    organization=None,
) -> Optional[CoveredTopicMatch]:
    if memory is None:
        if organization is None:
            return None
        memory = build_topic_coverage_memory(organization)

    candidate_texts = _topic_text_candidates(keyword, title)
    for text in candidate_texts:
        normalized = normalize_topic_text(text)
        record = memory.get("exact", {}).get(normalized)
        if record:
            return CoveredTopicMatch(record=record, match_type="exact", similarity=1.0)
        slug = slugify(normalized)
        record = memory.get("slugs", {}).get(slug)
        if record:
            return CoveredTopicMatch(record=record, match_type="slug", similarity=1.0)

    for text in candidate_texts:
        candidate_tokens = topic_content_tokens(text)
        for record in memory.get("records", []):
            similarity = _close_topic_match(text, candidate_tokens, record)
            if similarity is not None:
                return CoveredTopicMatch(record=record, match_type="lexical_variant", similarity=similarity)

    return None
