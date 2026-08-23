"""
Shared constants and pure helpers for the content-island topic graph.

This module is the source of truth for the island visual enums. content-factory
mirrors ``ISLAND_COLOR_KEYS`` / ``ISLAND_ICON_KEYS`` in ``island_synthesis.py``
and the frontend keeps its own fallbacks - a generated key outside these sets is
the crash class this triple validation exists to prevent.
"""
import logging
import math

from django.utils.text import slugify

logger = logging.getLogger(__name__)

ISLAND_COLOR_KEYS = [
    "green",
    "purple",
    "blue",
    "orange",
    "teal",
    "rose",
    "amber",
    "indigo",
    "cyan",
    "lime",
]
ISLAND_ICON_KEYS = [
    "brain",
    "community",
    "rocket",
    "tools",
    "chart",
    "globe",
    "shield",
    "leaf",
    "bolt",
    "default",
]

DEFAULT_ISLAND_ICON_KEY = "default"
DEFAULT_ISLAND_COLOR_KEY = "purple"

# Server-side edge recompute (see plan D1/Phase 1.3).
ISLAND_EDGE_MIN_SIMILARITY = 0.30
ISLAND_EDGE_TOP_N = 3

ISLAND_SLUG_MAX_LENGTH = 80
ISLAND_NAME_MAX_LENGTH = 160
ISLAND_PILLAR_KEYWORD_MAX_LENGTH = 200


def island_payload_value(data, *keys, default=None):
    """Read a wire field by any of its camelCase/snake_case spellings."""
    if not isinstance(data, dict):
        return default
    for key in keys:
        if key in data:
            return data.get(key)
    return default


def normalize_icon_key(value):
    """Return a known icon key, falling back to ``default`` for anything else."""
    candidate = str(value or "").strip().lower()
    if candidate in ISLAND_ICON_KEYS:
        return candidate
    return DEFAULT_ISLAND_ICON_KEY


def normalize_color_key(value, *, used_color_keys=None):
    """
    Return a known colour key.

    An unknown or missing key falls back to the least-used colour across
    ``used_color_keys`` so a fresh island still lands on a distinct colour.
    """
    candidate = str(value or "").strip().lower()
    if candidate in ISLAND_COLOR_KEYS:
        return candidate
    return least_used_color_key(used_color_keys)


def least_used_color_key(used_color_keys=None):
    counts = {color: 0 for color in ISLAND_COLOR_KEYS}
    for color in used_color_keys or []:
        key = str(color or "").strip().lower()
        if key in counts:
            counts[key] += 1
    return min(ISLAND_COLOR_KEYS, key=lambda color: (counts[color], ISLAND_COLOR_KEYS.index(color)))


def normalize_island_slug(value, *, fallback="island"):
    slug = slugify(str(value or "").strip())[:ISLAND_SLUG_MAX_LENGTH]
    return slug or fallback


def unique_island_slug(value, taken_slugs, *, fallback="island"):
    """Slugify ``value`` and suffix ``-2``, ``-3`` ... until it is free."""
    base = normalize_island_slug(value, fallback=fallback)
    if base not in taken_slugs:
        return base
    suffix = 2
    while True:
        tail = f"-{suffix}"
        candidate = f"{base[:ISLAND_SLUG_MAX_LENGTH - len(tail)]}{tail}"
        if candidate not in taken_slugs:
            return candidate
        suffix += 1


def cosine_similarity(vector_a, vector_b):
    """Pure-python cosine; island counts are tens, so no numpy/pgvector needed."""
    if not vector_a or not vector_b or len(vector_a) != len(vector_b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for left, right in zip(vector_a, vector_b):
        try:
            left_value = float(left)
            right_value = float(right)
        except (TypeError, ValueError):
            return 0.0
        dot += left_value * right_value
        norm_a += left_value * left_value
        norm_b += right_value * right_value
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def compute_island_edges(
    islands,
    *,
    min_similarity=ISLAND_EDGE_MIN_SIMILARITY,
    top_n=ISLAND_EDGE_TOP_N,
):
    """
    Build the canonical edge set for a list of islands from stored centroids.

    Returns ``[(slug_a, slug_b, similarity)]`` with ``slug_a < slug_b``, keeping
    each island's ``top_n`` strongest neighbours above ``min_similarity`` and
    taking the union (an edge kept by either endpoint survives).
    """
    ranked = []
    for island in islands:
        centroid = island.centroid_embedding or []
        if not centroid:
            continue
        ranked.append((island.slug, centroid))

    similarities = {}
    for index, (slug_a, centroid_a) in enumerate(ranked):
        for slug_b, centroid_b in ranked[index + 1:]:
            similarity = cosine_similarity(centroid_a, centroid_b)
            if similarity < min_similarity:
                continue
            pair = (slug_a, slug_b) if slug_a < slug_b else (slug_b, slug_a)
            similarities[pair] = similarity

    neighbours = {}
    for (slug_a, slug_b), similarity in similarities.items():
        neighbours.setdefault(slug_a, []).append((slug_b, similarity))
        neighbours.setdefault(slug_b, []).append((slug_a, similarity))

    kept = set()
    for slug, entries in neighbours.items():
        entries.sort(key=lambda entry: (-entry[1], entry[0]))
        for other_slug, _similarity in entries[:top_n]:
            kept.add((slug, other_slug) if slug < other_slug else (other_slug, slug))

    return sorted(
        ((pair[0], pair[1], similarities[pair]) for pair in kept),
        key=lambda edge: (-edge[2], edge[0], edge[1]),
    )


def rebuild_island_edges(organization):
    """
    Replace the org's edge set with a fresh recompute over stored centroids.

    Server-side computation keeps edges stable for a still-visible island whose
    cluster did not re-form this run, and keeps birth slugs out of the wire
    contract entirely.
    """
    from content_factory.models import ContentIsland, ContentIslandEdge, ContentIslandStatus

    islands = list(
        ContentIsland.objects.filter(organization=organization).exclude(
            status=ContentIslandStatus.ARCHIVED
        )
    )
    islands_by_slug = {island.slug: island for island in islands}
    edges = compute_island_edges(islands)
    ContentIslandEdge.objects.filter(organization=organization).delete()
    ContentIslandEdge.objects.bulk_create([
        ContentIslandEdge(
            organization=organization,
            island_a=islands_by_slug[slug_a],
            island_b=islands_by_slug[slug_b],
            similarity=similarity,
        )
        for slug_a, slug_b, similarity in edges
    ])
    return len(edges)


def seed_islands_from_bootstrap_pillars(organization):
    """
    Create the org's first islands from the pillar list the dashboard serves today.

    The slug/name/description/iconKey/colorKey are copied verbatim from
    ``_topic_pillars_for_bootstrap`` so seeded islands are indistinguishable from
    what the founder already sees (cluster-derived slugs and position-among-
    survivors colours included). Centroids stay empty - content-factory fills
    them on the first refresh.
    """
    # Function-local: vibe_marketing_views is ~17k lines and pulls in most of the
    # app, so importing it at module scope would create an import cycle.
    from django.utils import timezone

    from content_factory.models import (
        ContentIsland,
        ContentIslandOrigin,
        ContentIslandStatus,
        OrganizationContentConfig,
    )
    from content_factory.vibe_marketing_views import _topic_pillars_for_bootstrap

    config = OrganizationContentConfig.objects.filter(organization=organization).first()
    try:
        pillars = _topic_pillars_for_bootstrap(organization, config)
    except Exception as exc:  # pragma: no cover - seeding must never break a read
        logger.warning(
            "content_islands seed failed to derive pillars for %s: %s",
            organization.domain,
            exc,
        )
        return []

    now = timezone.now()
    taken_slugs = set(
        ContentIsland.objects.filter(organization=organization).values_list('slug', flat=True)
    )
    used_color_keys = list(
        ContentIsland.objects.filter(organization=organization).values_list('color_key', flat=True)
    )
    created = []
    for pillar in pillars:
        if not isinstance(pillar, dict):
            continue
        slug = str(pillar.get('slug') or "").strip()[:ISLAND_SLUG_MAX_LENGTH]
        if not slug or slug in taken_slugs:
            continue
        name = str(pillar.get('name') or slug).strip()[:ISLAND_NAME_MAX_LENGTH]
        color_key = normalize_color_key(pillar.get('colorKey'), used_color_keys=used_color_keys)
        island = ContentIsland.objects.create(
            organization=organization,
            slug=slug,
            name=name,
            description=str(pillar.get('description') or "").strip(),
            pillar_keyword=_pillar_keyword_from_bootstrap_pillar(pillar)[:ISLAND_PILLAR_KEYWORD_MAX_LENGTH],
            icon_key=normalize_icon_key(pillar.get('iconKey')),
            color_key=color_key,
            status=ContentIslandStatus.VISIBLE,
            origin=ContentIslandOrigin.PILLAR_STRATEGY_SEED,
            centroid_embedding=[],
            first_seen_at=now,
            promoted_at=now,
        )
        taken_slugs.add(slug)
        used_color_keys.append(color_key)
        created.append(island)
    return created


def _pillar_keyword_from_bootstrap_pillar(pillar):
    """Mirror the frontend's dispatch-keyword resolution for a bootstrap pillar."""
    for candidate in pillar.get('topicCandidates') or []:
        if not isinstance(candidate, dict):
            continue
        keyword = str(candidate.get('pillarKeyword') or candidate.get('pillar_keyword') or "").strip()
        if keyword:
            return keyword
    return str(pillar.get('name') or pillar.get('slug') or "").strip()
