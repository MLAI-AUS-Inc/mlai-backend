"""Founder-initiated content island removal.

A removed island is archived and stamped ``removed_at``. Refreshes only see
non-archived islands, so the same keywords could otherwise be born again as a
new island the next day; ``suppressed_by_removed_island`` stops that. Adding the
theme again through research or a custom island clears the mark.
"""
from django.db import transaction
from django.utils import timezone

# The same-theme threshold used when grouping selected research themes.
REMOVED_ISLAND_SIMILARITY = 0.90


def remove_island(organization, slug, *, now=None):
    """Archive one island for the founder. Returns None when it does not exist."""
    from organizations.models import Organization
    from .content_islands import rebuild_island_edges
    from .models import ContentIsland, ContentIslandStatus

    now = now or timezone.now()
    with transaction.atomic():
        org = Organization.objects.select_for_update().get(pk=organization.pk)
        island = ContentIsland.objects.filter(organization=org, slug=slug).first()
        if island is None:
            return None
        if island.removed_at is None:
            island.status = ContentIslandStatus.ARCHIVED
            island.archived_at = island.archived_at or now
            island.removed_at = now
            island.save(update_fields=["status", "archived_at", "removed_at", "updated_at"])
            release_from_selections(org, island.slug)
            rebuild_island_edges(org)
    return island


def release_from_selections(organization, slug):
    """Let the founder pick a removed theme again and stop it evolving.

    Called inside the org-locked removal transaction. Bumping the revision makes
    an in-flight refresh built from the old selection stale.
    """
    from .island_selection import STATE_KEY, resolve_alias, selection_runs

    for run in selection_runs(organization).select_for_update():
        state = dict(run.result[STATE_KEY])
        aliases = state.get("redirects", {})
        groups = state.get("groups", [])
        released = [group for group in groups if resolve_alias(group.get("slug"), aliases) == slug]
        if not released and slug not in state.get("managed_slugs", []):
            continue
        ids = {proposal_id for group in released for proposal_id in group.get("proposal_ids", [])}
        state.update(
            groups=[group for group in groups if group not in released],
            selected_ids=sorted(set(state.get("selected_ids", [])) - ids),
            managed_proposal_ids=sorted(set(state.get("managed_proposal_ids", [])) - ids),
            managed_slugs=sorted(set(state.get("managed_slugs", [])) - {slug}),
            revision=state.get("revision", 0) + 1,
        )
        state.pop("pending", None)
        run.result = {**run.result, STATE_KEY: state}
        run.save(update_fields=["result", "updated_at"])


def removed_island_centroids(islands):
    return [island.centroid_embedding for island in islands
            if island.removed_at is not None and island.centroid_embedding]


def suppressed_by_removed_island(centroid, removed_centroids):
    """True when a proposed island is the same theme as one the founder removed."""
    from .content_islands import cosine_similarity
    return bool(centroid) and any(
        cosine_similarity(centroid, removed) >= REMOVED_ISLAND_SIMILARITY for removed in removed_centroids
    )
