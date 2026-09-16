"""Measured multi-selection and durable evolution state, owned by the research run."""
import hashlib
import math

STATE_KEY = "island_research_selection"
MERGE_SIMILARITY = .90


def cosine(a, b):
    if not a or len(a) != len(b) or any(type(x) not in (int, float) or not math.isfinite(x) for x in [*a, *b]):
        return 0.0
    denominator = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    return sum(x*y for x, y in zip(a, b)) / denominator if denominator else 0.0


def selected_proposals(run, ids):
    from .island_research import proposal_for_adoption
    if not isinstance(ids, list) or not 1 <= len(ids) <= 5 or any(not isinstance(i, str) for i in ids):
        raise ValueError("Select between one and five researched themes.")
    return [proposal_for_adoption(run, key) for key in sorted(set(ids))]


def group_proposals(proposals):
    """Complete-link grouping prevents chains of loosely related themes merging."""
    groups = []
    def intent(p):
        values = [k.get("intent") for k in p["keywords"] if k.get("intent")]
        return max(sorted(set(values)), key=values.count) if values else None
    def compatible(a, b):
        return (not intent(a) or not intent(b) or intent(a) == intent(b)) and cosine(
            a["centroid_embedding"], b["centroid_embedding"]) >= MERGE_SIMILARITY
    for proposal in sorted(proposals, key=lambda p: (-p["metrics"]["total_volume"], p["id"])):
        group = next((g for g in groups if all(compatible(proposal, other) for other in g)), None)
        if group is None:
            groups.append([proposal])
        else:
            group.append(proposal)
    return groups


def combine_proposals(group):
    representative = group[0]
    rows = {}
    for proposal in group:
        for row in proposal["keywords"]:
            rows.setdefault(row["keyword"].strip().lower(), row)
    keywords = list(rows.values())
    vectors = [p["centroid_embedding"] for p in group]
    weights = [len(p["keywords"]) for p in group]
    centroid = [sum(v[i] * w for v, w in zip(vectors, weights)) / sum(weights) for i in range(len(vectors[0]))]
    norm = math.sqrt(sum(x*x for x in centroid))
    names = [p["name"] for p in group]
    name = representative["name"] if len(group) == 1 else " / ".join(names)[:160]
    return {**representative, "name": name,
        "description": "\n\n".join(dict.fromkeys(p["description"] for p in group)),
        "centroid_embedding": [x / norm for x in centroid] if norm else centroid,
        "keywords": keywords,
        "members": [{"keyword_normalized": key, "similarity_score": 1.0 if key == representative["pillar_keyword"].lower() else 0.0,
                     "is_centroid": key == representative["pillar_keyword"].lower()} for key in rows],
        "metrics": {"keyword_count": len(keywords), "total_volume": sum(k["volume"] for k in keywords),
            "avg_difficulty": sum(k["difficulty"] for k in keywords) / len(keywords),
            "opportunity_score": sum(k.get("opportunity_index", 0) for k in keywords),
            "ai_search_volume": sum(k.get("ai_search_volume", 0) for k in keywords)},
        "proposal_ids": sorted(p["id"] for p in group)}


def selection_preview(run, ids):
    proposals = selected_proposals(run, ids)
    state = (run.result or {}).get(STATE_KEY, {})
    added = set(state.get("selected_ids", []))
    groups = [combine_proposals(g) for g in group_proposals([p for p in proposals if p["id"] not in added])]
    return {"groups": [{key: g[key] for key in ("name", "proposal_ids", "metrics")} for g in groups],
            "already_added": sorted(p["id"] for p in proposals if p["id"] in added)}


def adopt_selection(organization, run, ids):
    from django.db import transaction
    from organizations.models import Organization
    from workflow_runs.models import ContentFactoryRun
    from .models import ContentIsland
    from .island_research import adopt_researched_island
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=organization.pk)
        run = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        proposals = selected_proposals(run, ids)
        state = dict((run.result or {}).get(STATE_KEY, {}))
        added = set(state.get("selected_ids", []))
        has_new = any(p["id"] not in added for p in proposals)
        groups = list(state.get("groups", []))
        managed = set(state.get("managed_slugs", []))
        managed_ids = set(state.get("managed_proposal_ids", []))
        ownership = set()
        for other in selection_runs(organization).exclude(pk=run.pk):
            ownership.update(other.result[STATE_KEY].get("managed_slugs", []))
        for group in group_proposals([p for p in proposals if p["id"] not in added]):
            proposal = combine_proposals(group)
            existing = ContentIsland.objects.filter(organization=organization, pillar_keyword__iexact=proposal["pillar_keyword"]).first()
            other_owned = existing and existing.slug in ownership
            island, created = adopt_researched_island(organization, run, proposal,
                merge_evidence=True, preserve_positioning=bool(other_owned))
            groups.append({"slug": island.slug, "proposal_ids": proposal["proposal_ids"]})
            # Never take over another run's evolving theme or an existing manual island.
            if island.slug not in ownership:
                if island.origin != "manual":
                    island.origin = "manual"
                    island.save(update_fields=["origin", "updated_at"])
                managed.add(island.slug)
                managed_ids.update(proposal["proposal_ids"])
        state.update(version=1, selected_ids=sorted(added | {p["id"] for p in proposals}),
                     groups=groups, managed_slugs=sorted(managed), managed_proposal_ids=sorted(managed_ids), revision=state.get("revision", 0) + int(has_new))
        if has_new:
            from django.utils import timezone
            events = list(state.get("daily_priority_events", []))
            events.append({"id": f"island-selection:{run.run_id}:{state['revision']}",
                           "created_at": timezone.now().isoformat(),
                           "keywords": list(dict.fromkeys(k["keyword"] for p in proposals if p["id"] not in added for k in p["keywords"]))[:100]})
            state["daily_priority_events"] = events[-30:]
            state.pop("pending", None)
            run.result = {**run.result, STATE_KEY: state}
            run.save(update_fields=["result", "updated_at"])
        slugs = {g["slug"] for g in groups if set(g["proposal_ids"]) & set(ids)}
        aliases = state.get("redirects", {})
        slugs = {resolve_alias(slug, aliases) for slug in slugs}
        return list(ContentIsland.objects.filter(organization=organization, slug__in=slugs).order_by("-opportunity_score", "slug"))


def selection_runs(organization):
    from workflow_runs.models import ContentFactoryRun
    return ContentFactoryRun.objects.filter(organization=organization, status="completed",
        result__island_research_selection__isnull=False).order_by("created_at", "pk")


def resolve_alias(slug, aliases):
    seen = set()
    while slug in aliases and slug not in seen:
        seen.add(slug)
        slug = aliases[slug]
    return slug


def dynamic_scopes(organization):
    """Service-only context. No embeddings or internal evolution state reach browsers."""
    from .models import ContentIsland, ResearchedKeyword
    scopes = []
    for run in selection_runs(organization):
        state = run.result[STATE_KEY]
        islands = list(ContentIsland.objects.filter(organization=organization,
            slug__in=state.get("managed_slugs", []), status="visible").prefetch_related("memberships__keyword"))
        if not islands:
            continue
        selections = [p for p in run.result.get("suggested_islands", []) if p["id"] in state.get("managed_proposal_ids", [])]
        original = {k["keyword"].strip().lower() for p in selections for k in p["keywords"]}
        original.update(m.keyword.keyword_normalized for island in islands for m in island.memberships.all())
        rows = list(ResearchedKeyword.objects.filter(organization=organization, keyword_normalized__in=original).values(
            "keyword", "keyword_normalized", "volume", "difficulty", "opportunity_index", "ai_search_volume", "status", "tier", "intent"))
        scopes.append({"run_id": run.run_id, "revision": state.get("revision", 0),
            "brief": run.run_request["island_research_brief"], "keywords": rows,
            "seeds": [{"id": p["id"], "name": p["name"], "centroid": p["centroid_embedding"],
                       "keywords": [k["keyword"].strip().lower() for k in p["keywords"]]} for p in selections],
            "islands": [{"slug": i.slug, "name": i.name, "description": i.description,
                "pillar_keyword": i.pillar_keyword, "centroid": i.centroid_embedding,
                "keywords": [m.keyword.keyword_normalized for m in i.memberships.all()]} for i in islands]})
    return scopes


def topology_signature(groups, islands):
    """Topology ignores changing metrics and new members within the same group."""
    owners = {k: i["slug"] for i in islands for k in i["keywords"]}
    partition = [sorted({owners[k] for k in g["keywords"] if k in owners}) for g in groups]
    return hashlib.sha256(repr(sorted(partition)).encode()).hexdigest()


def confirm_topology(state, signature, captured_on):
    """Two distinct increasing research dates; callback retries are not evidence."""
    pending = state.get("pending", {})
    if pending.get("signature") != signature:
        state["pending"] = {"signature": signature, "date": captured_on}
        return False
    return captured_on > pending.get("date", captured_on)


def apply_evolution(organization, proposals, captured_on, now):
    """Called inside the org-locked bulk-sync transaction; all-or-nothing per run."""
    from .models import ContentIsland, ContentIslandSnapshot
    from .service_views import _replace_island_members, _island_articles_written
    from .custom_islands import custom_island_description
    from django.utils.text import slugify
    results = []
    for proposed in proposals if isinstance(proposals, list) else []:
        run = selection_runs(organization).select_for_update().filter(run_id=proposed.get("run_id")).first()
        if not run:
            continue
        state = dict(run.result[STATE_KEY])
        day = captured_on.isoformat()
        if proposed.get("revision") != state.get("revision", 0) or day <= state.get("last_applied_on", "") or day < state.get("last_observed_on", ""):
            continue  # stale/replayed refresh cannot reverse a user's newer selection
        islands = list(ContentIsland.objects.filter(organization=organization,
            slug__in=state.get("managed_slugs", []), status="visible").prefetch_related("memberships__keyword"))
        old = [{"slug": i.slug, "keywords": [m.keyword.keyword_normalized for m in i.memberships.all()]} for i in islands]
        groups = proposed.get("groups") or []
        if not old or not groups or len(groups) > 30:
            continue
        # Resolve evidence in this organisation; missing/partial syncs must not retire themes.
        from .models import ResearchedKeyword
        requested = {k for g in groups for k in g.get("keywords", [])}
        known = set(ResearchedKeyword.objects.filter(organization=organization,
            keyword_normalized__in=requested).values_list("keyword_normalized", flat=True))
        if requested != known or any(not g.get("members") or not g.get("centroid_embedding") for g in groups):
            continue
        overlaps = [[len(set(g["keywords"]) & set(o["keywords"])) for o in old] for g in groups]
        if any(not any(row[j] for row in overlaps) for j in range(len(old))):
            continue
        structural = len(groups) != len(old) or any(sum(n > 0 for n in row) != 1 for row in overlaps) or any(
            sum(row[j] > 0 for row in overlaps) != 1 for j in range(len(old)))
        state["last_observed_on"] = day
        if structural and not confirm_topology(state, topology_signature(groups, old), day):
            run.result = {**run.result, STATE_KEY: state}
            run.save(update_fields=["result", "updated_at"])
            continue
        matches, used = {}, set()
        # Largest overlap owns the stable link, independent of opportunity order.
        for count, group_index, old_index in sorted(
                ((n, g, o) for g, row in enumerate(overlaps) for o, n in enumerate(row) if n),
                key=lambda item: (-item[0], old[item[2]]["slug"], item[1])):
            if group_index not in matches and old_index not in used:
                matches[group_index] = old_index
                used.add(old_index)
        active = []
        brief = run.run_request["island_research_brief"]
        context = custom_island_description({**brief, "focus": brief.get("focus") or brief.get("intent", "any")})
        for index, group in enumerate(groups):
            matched = matches.get(index)
            if matched is not None:
                island = islands[matched]
            else:
                identity = hashlib.sha256((run.run_id + ':' + group['pillar_keyword'].lower()).encode()).hexdigest()[:16]
                slug = f"{slugify(group['pillar_keyword'])[:50] or 'island'}-{identity}"
                island, _ = ContentIsland.objects.get_or_create(organization=organization, slug=slug,
                    defaults={"origin": "manual", "name": group['name'][:160], "description": group['description'] + '\n\n' + context,
                              "pillar_keyword": group['pillar_keyword'][:200]})
            # A changed partition gets a matching title; unchanged islands keep familiar names.
            if structural:
                island.name = group['name'][:160]
                island.description = group['description'] + '\n\n' + context
                island.pillar_keyword = group['pillar_keyword'][:200]
            island.centroid_embedding = group['centroid_embedding']
            _replace_island_members(island, organization, group['members'])
            for field in ('keyword_count', 'total_volume', 'avg_difficulty', 'opportunity_score', 'ai_search_volume'):
                setattr(island, field, group['metrics'][field])
            island.status = 'visible'
            island.archived_at = None
            island.promoted_at = island.promoted_at or now
            island.last_refreshed_at = island.last_matched_at = now
            island.articles_written = _island_articles_written(island)
            island.save()
            active.append(island)
        retired = [island for index, island in enumerate(islands) if index not in used]
        aliases = dict(state.get('redirects', {}))
        for island in retired:
            old_index = next(i for i, o in enumerate(old) if o['slug'] == island.slug)
            target = max(range(len(groups)), key=lambda g: overlaps[g][old_index])
            aliases[island.slug] = active[target].slug
            island.status, island.archived_at = 'archived', now
            island.save(update_fields=['status', 'archived_at', 'updated_at'])
            ContentIslandSnapshot.objects.update_or_create(island=island, captured_on=captured_on, defaults={
                'keyword_count': island.keyword_count, 'total_volume': island.total_volume,
                'avg_difficulty': island.avg_difficulty, 'opportunity_score': island.opportunity_score,
                'ai_search_volume': island.ai_search_volume, 'status': 'archived'})
        for island in active:
            aliases.pop(island.slug, None)
        if structural:
            state['history'] = [*state.get('history', []), {'date': day,
                'from': sorted(i.slug for i in islands), 'to': sorted(i.slug for i in active)}][-100:]
        state.update(managed_slugs=sorted(i.slug for i in active), redirects=aliases,
                     last_applied_on=day, revision=state.get('revision', 0) + 1)
        state.pop('pending', None)
        run.result = {**run.result, STATE_KEY: state}
        run.save(update_fields=['result', 'updated_at'])
        results.append({'run_id': run.run_id, 'islands': state['managed_slugs'], 'changed': structural})
    return results
