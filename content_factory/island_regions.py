"""Deterministic, database-free topic regions and stable refresh identities.

The persistence adapter in content_islands owns organisation locking. This module
only consumes island/region snapshots, making refresh decisions independently testable.
"""
import math
import re
import unicodedata
from collections import Counter

REGION_COLOR_KEYS = ('green', 'purple', 'blue', 'orange', 'teal', 'rose', 'amber', 'indigo')
REGION_MIN_LINKAGE = 0.12
REGION_MOVE_MARGIN = 0.05
REGION_NAME_ACRONYMS = frozenset(('ai', 'abn', 'seo', 'api', 'b2b'))


def _value(row, key, default=None):
    return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


def _vector(value):
    try:
        vector = [float(item) for item in value or []]
    except (TypeError, ValueError, OverflowError):
        return []
    if not vector or not all(math.isfinite(item) for item in vector):
        return []
    norm = math.sqrt(sum(item * item for item in vector))
    return [item / norm for item in vector] if norm > 0 else []


def _cosine(left, right):
    if not left or len(left) != len(right):
        return 0.0
    return max(-1.0, min(1.0, sum(a * b for a, b in zip(left, right))))


def _centroid(slugs, vectors):
    usable = [vectors[slug] for slug in sorted(slugs) if vectors.get(slug)]
    if not usable:
        return []
    # Malformed legacy dimensions cannot poison an otherwise valid region.
    size = Counter(len(vector) for vector in usable).most_common(1)[0][0]
    usable = [vector for vector in usable if len(vector) == size]
    return _vector([sum(vector[index] for vector in usable) for index in range(size)])


def _cluster(slugs, similarities, target):
    clusters = [(slug,) for slug in sorted(slugs)]
    while len(clusters) > target:
        best = None
        for index, left in enumerate(clusters):
            for other_index in range(index + 1, len(clusters)):
                right = clusters[other_index]
                linkage = sum(similarities.get(tuple(sorted((a, b))), 0.0)
                              for a in left for b in right) / (len(left) * len(right))
                candidate = (-linkage, left, right, index, other_index)
                if best is None or candidate < best:
                    best = candidate
        if best is None or -best[0] < REGION_MIN_LINKAGE:
            break
        _, left, right, index, other_index = best
        clusters = [cluster for offset, cluster in enumerate(clusters) if offset not in (index, other_index)]
        clusters.append(tuple(sorted(left + right)))
        clusters.sort()
    return [set(cluster) for cluster in clusters]


def _fallback_name(members, by_slug):
    ordered = sorted(members, key=lambda slug: (-_score(by_slug[slug]), slug))
    leading = []
    for position, slug in enumerate(ordered):
        words = str(_value(by_slug[slug], 'pillar_keyword', '')).strip().split()
        if position == 0:
            leading = words
        else:
            length = 0
            for left, right in zip(leading, words):
                if left.casefold() != right.casefold():
                    break
                length += 1
            leading = leading[:length]
        if len(leading) < 2:
            break
    name = (' '.join(word.upper() if word.casefold() in REGION_NAME_ACRONYMS else word.title()
                     for word in leading) if len(leading) >= 2
            else str(_value(by_slug[ordered[0]], 'name', ordered[0])))
    return name[:80]


def _score(island):
    try:
        value = float(_value(island, 'opportunity_score', 0))
        return value if math.isfinite(value) else 0
    except (TypeError, ValueError):
        return 0


def _slug(value, taken):
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode()
    base = re.sub(r'[^a-z0-9]+', '-', value.lower()).strip('-')[:80] or 'topics'
    candidate, suffix = base, 2
    while candidate in taken:
        tail = f'-{suffix}'
        candidate, suffix = base[:80 - len(tail)] + tail, suffix + 1
    return candidate


def compute_island_regions(islands, previous_regions=(), *, edges=()):
    """Return stable region snapshots, each with exactly one set of member slugs.

    Previous snapshots include ``island_slugs`` and may carry a saved centroid.
    Matching uses global greedy Jaccard >= 0.5; matched display fields are retained.
    ``edges`` optionally supplies (source, target, similarity) for unembedded islands.
    """
    by_slug = {_value(island, 'slug'): island for island in islands}
    if not by_slug:
        return []
    vectors = {slug: _vector(_value(row, 'centroid_embedding', [])) for slug, row in by_slug.items()}
    slugs = sorted(by_slug)
    target = min(len(slugs), max(3, min(8, int(math.sqrt(len(slugs) / 2) + 0.5))))
    similarities = {(left, right): _cosine(vectors[left], vectors[right])
                    for index, left in enumerate(slugs) for right in slugs[index + 1:]}
    embedded = [slug for slug in slugs if vectors[slug]]
    clusters = _cluster(embedded, similarities, target) if len(slugs) > 12 else ([set(embedded)] if embedded else [])
    # A lone topic with a measured neighbour belongs beside that audience; leaving
    # it as a singleton would make the move-margin compare against itself forever.
    for cluster in list(clusters):
        if len(cluster) != 1:
            continue
        slug = next(iter(cluster))
        choices = [(max(similarities.get(tuple(sorted((slug, other))), 0) for other in group),
                    tuple(sorted(group)), group) for group in clusters if len(group) > 1]
        if choices:
            strength, _, destination = min(choices, key=lambda item: (-item[0], item[1]))
            if strength >= REGION_MIN_LINKAGE:
                destination.update(cluster)
                clusters.remove(cluster)
    # Detached vectors share one calm catch-all instead of producing dozens of groups.
    isolated = [cluster for cluster in clusters if len(cluster) == 1 and not any(
        similarities.get(tuple(sorted((next(iter(cluster)), other))), 0) >= REGION_MIN_LINKAGE
        for other in embedded if other not in cluster)]
    if len(isolated) >= 2:
        clusters = [cluster for cluster in clusters if cluster not in isolated] + [set().union(*isolated)]
    # Sparse graphs may stop above K; absorb the smallest groups to enforce the label cap.
    while len(clusters) > 8:
        clusters.sort(key=lambda cluster: (len(cluster), tuple(sorted(cluster))))
        left, right = clusters[:2]
        clusters = clusters[2:] + [left | right]
    cap = math.ceil(len(slugs) / target) * 2
    for cluster in list(clusters):
        if len(cluster) > cap:
            split = _cluster(cluster, similarities, 2)
            if len(split) == 2 and len(clusters) < 8:
                clusters.remove(cluster)
                clusters.extend(split)
    missing = [slug for slug in slugs if not vectors[slug]]
    edge_values = []
    for edge in edges:
        if isinstance(edge, (tuple, list)):
            left, right, similarity = edge
        else:
            left, right, similarity = _value(edge, 'source'), _value(edge, 'target'), _value(edge, 'similarity', 0)
        edge_values.append((left, right, similarity))
    detached = set()
    for slug in missing:
        neighbours = sorted(((other if left == slug else left, similarity)
                             for left, other, similarity in edge_values if slug in (left, other)),
                            key=lambda pair: (-pair[1], pair[0]))
        destination = next((cluster for other, _ in neighbours for cluster in clusters if other in cluster), None)
        if destination is None:
            detached.add(slug)
        else:
            destination.add(slug)
    if detached:
        if len(slugs) <= 12 and clusters:
            clusters[0].update(detached)
        elif len(clusters) >= 8:
            min(clusters, key=lambda group: (len(group), tuple(sorted(group)))).update(detached)
        else:
            clusters.append(detached)
    clusters.sort(key=lambda cluster: tuple(sorted(cluster)))
    previous = {str(_value(region, 'slug')): region for region in previous_regions}
    old_members = {slug: set(_value(region, 'island_slugs', [])) & set(slugs) for slug, region in previous.items()}
    candidates = []
    for index, cluster in enumerate(clusters):
        for slug, members in old_members.items():
            union = cluster | members
            overlap = len(cluster & members) / len(union) if union else 0
            if overlap >= 0.5:
                candidates.append((-overlap, slug, index))
    matched, used = {}, set()
    for _, slug, index in sorted(candidates):
        if index not in matched and slug not in used:
            matched[index] = slug
            used.add(slug)
    # Preserve a current region until a candidate centroid improves similarity by 0.05.
    destination_by_slug = {slug: index for index, group in enumerate(clusters) for slug in group}
    matched_index = {slug: index for index, slug in matched.items()}
    centroids = [_centroid(group, vectors) for group in clusters]
    moves = []
    for previous_slug, members in old_members.items():
        old_index = matched_index.get(previous_slug)
        if old_index is None:
            continue
        old_centroid = _vector(_value(previous[previous_slug], 'centroid_embedding', [])) or centroids[old_index]
        for slug in sorted(members):
            new_index = destination_by_slug[slug]
            if new_index == old_index:
                continue
            candidate_centroid = _centroid(clusters[new_index] - {slug}, vectors)
            if _cosine(vectors[slug], candidate_centroid) < _cosine(vectors[slug], old_centroid) + REGION_MOVE_MARGIN:
                moves.append((slug, new_index, old_index))
    for slug, new_index, old_index in moves:
        clusters[new_index].discard(slug)
        clusters[old_index].add(slug)
    taken = set(previous)
    used_colors = [_value(previous[slug], 'color_key') for index, slug in matched.items() if clusters[index]]
    results = []
    for index, members in enumerate(clusters):
        if not members:
            continue
        ordered = sorted(members, key=lambda slug: (-_score(by_slug[slug]), slug))
        old = previous.get(matched.get(index))
        if old is not None:
            slug, name = str(_value(old, 'slug')), _value(old, 'name')
            source, color = _value(old, 'name_source', 'fallback'), _value(old, 'color_key')
        else:
            slug = _slug(ordered[0], taken)
            catch_all = members == detached or (isolated and members == set().union(*isolated))
            name = 'More topics' if catch_all else _fallback_name(members, by_slug)
            source = 'fallback'
            counts = Counter(used_colors)
            color = min(REGION_COLOR_KEYS, key=lambda key: (counts[key], REGION_COLOR_KEYS.index(key)))
            used_colors.append(color)
        taken.add(slug)
        results.append({'slug': slug, 'name': name, 'name_source': source, 'color_key': color,
                        'centroid_embedding': _centroid(members, vectors), 'island_slugs': ordered})
    return sorted(results, key=lambda region: region['slug'])
