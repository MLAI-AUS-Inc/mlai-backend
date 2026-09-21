"""Brief-led island research: validated input, payment recovery and adoption."""
import hashlib
import json
import math


def validate_research_brief(data):
    if not isinstance(data, dict):
        raise ValueError("Describe a topic to explore.")
    clean = {}
    for key, minimum, maximum in (("subject", 1, 1000), ("description", 0, 2000),
                                   ("audience", 0, 300), ("focus", 0, 500)):
        value = data.get(key, "")
        if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
            raise ValueError(f"{key.capitalize()} must be {minimum}–{maximum} characters.")
        clean[key] = value.strip()
    intent = data.get("searchIntent", "any")
    if not isinstance(intent, str) or intent not in {"any", "informational", "commercial", "transactional", "navigational", "custom"}:
        raise ValueError("Choose a valid search intent.")
    if intent == "custom" and len(clean["focus"]) < 3:
        raise ValueError("Describe your content direction.")
    if intent != "custom":
        clean["focus"] = ""
    clean["intent"] = intent
    return clean


def research_request_key(organization_id, user_id, nonce, brief):
    if not isinstance(nonce, str) or not 8 <= len(nonce.strip()) <= 100:
        raise ValueError("A research request identifier is required. Please reopen the island builder.")
    identity = json.dumps([str(organization_id), str(user_id), nonce.strip(), brief], sort_keys=True, ensure_ascii=False)
    return "island-research:" + hashlib.sha256(identity.encode()).hexdigest()


def refund_empty_or_failed_research(run):
    """Refund the recorded payer once, including when the browser has closed."""
    request = run.run_request if isinstance(run.run_request, dict) else {}
    result = run.result if isinstance(run.result, dict) else {}
    if not request.get("island_research_brief") or request.get("dispatch_pending_resolution"):
        return
    if result.get("island_research_refunded"):
        return
    failed = run.status in {"failed", "cancelled", "blocked"}
    empty = run.status == "completed" and result.get("island_research") and not result.get("suggested_islands")
    if not (failed or empty):
        return
    key = request.get("client_request_id")
    if not key:
        return
    from roo.models import Ledger
    from roo.services import PointsService
    charge = Ledger.objects.select_related("user").filter(
        idempotency_key=f"content_factory:topic_generation:charge:{key}", kind="SPEND",
    ).first()
    if not charge or not charge.user or not charge.delta or charge.delta >= 0:
        return
    PointsService.refund(
        user=charge.user, delta=-charge.delta, source=charge.source,
        original_spend_key=charge.idempotency_key,
        description=f"Island research refund: no usable islands for {run.domain}",
        created_by_slack_id=charge.created_by_slack_id,
        idempotency_key=f"content_factory:topic_generation:refund:{key}",
        reference_type="CONTENT_FACTORY", reference_id=charge.reference_id,
    )
    run.result = {**result, "island_research_refunded": True, "refunded_points": -charge.delta}
    run.save(update_fields=["result", "updated_at"])


def proposal_for_adoption(run, proposal_id):
    request = run.run_request if isinstance(run.run_request, dict) else {}
    result = run.result if isinstance(run.result, dict) else {}
    if not request.get("island_research_brief") or run.status != "completed" or not result.get("island_research"):
        raise ValueError("Wait for island research to finish before adding a result.")
    proposal = next((item for item in result.get("suggested_islands", [])
                     if isinstance(item, dict) and item.get("id") == proposal_id), None)
    evidence = proposal.get("keywords") if proposal else None
    if not isinstance(evidence, list) or not evidence or not proposal.get("centroid_embedding"):
        raise ValueError("Choose one of this research run’s measured islands.")
    # A small measured starting point is useful too. Keep genuine evidence as
    # the gate, rather than rejecting every cluster with fewer than 3 queries.
    if any(not isinstance(row, dict) or not isinstance(row.get("keyword"), str) or not row["keyword"].strip()
           or type(row.get("volume")) not in (int, float) or not math.isfinite(row["volume"]) or row["volume"] <= 0
           or type(row.get("difficulty")) not in (int, float) or not 0 <= row["difficulty"] <= 100
           for row in evidence):
        raise ValueError("Choose one of this research run’s measured islands.")
    return proposal


def adopt_researched_island(organization, run, proposal, *, merge_evidence=False, preserve_positioning=False):
    from django.db import transaction
    from django.utils import timezone
    from django.utils.text import slugify
    from organizations.models import Organization
    from .models import ContentIsland, ContentIslandKeyword, ContentIslandSnapshot, ResearchedKeyword
    from .content_islands import normalize_color_key, normalize_icon_key, rebuild_island_edges, seed_islands_from_bootstrap_pillars
    from .custom_islands import custom_island_description

    brief = run.run_request["island_research_brief"]
    keyword = proposal["pillar_keyword"]
    # Stable across repeat searches whose cluster membership shifts slightly.
    identity = hashlib.sha256(keyword.strip().lower().encode()).hexdigest()[:16]
    slug = f"{slugify(keyword)[:52] or 'researched-island'}-{identity}"
    now = timezone.now()
    with transaction.atomic():
        org = Organization.objects.select_for_update().get(pk=organization.pk)
        if not ContentIsland.objects.filter(organization=org, status="visible").exists():
            seed_islands_from_bootstrap_pillars(org)
        # An existing measured theme is reused; its members and positioning stay intact.
        existing = ContentIsland.objects.filter(organization=org, pillar_keyword__iexact=keyword).first()
        if existing and existing.keyword_count > 0 and not merge_evidence:
            if existing.status != "visible":
                existing.status, existing.promoted_at = "visible", now
                existing.archived_at = None
                existing.save(update_fields=["status", "promoted_at", "archived_at", "updated_at"])
            return existing, False
        scope = {**brief, "focus": brief.get("focus") or f"{brief['intent'].capitalize()} search intent"}
        defaults = {
            "name": proposal["name"][:160], "pillar_keyword": keyword[:200],
            "description": proposal["description"] + "\n\n" + custom_island_description(scope),
            "origin": "manual", "status": "visible", "promoted_at": now,
            "last_matched_at": now, "last_refreshed_at": now,
            "centroid_embedding": proposal["centroid_embedding"],
            "icon_key": normalize_icon_key(proposal.get("icon_key")),
            "color_key": normalize_color_key(proposal.get("color_key")),
            **proposal["metrics"],
        }
        if existing:
            island, created = existing, False
        else:
            island, created = ContentIsland.objects.get_or_create(organization=org, slug=slug, defaults=defaults)
        if not created:
            if island.keyword_count > 0 and island.status == "visible" and not merge_evidence:
                return island, False
            for field in ("status", "promoted_at", "last_matched_at", "last_refreshed_at", "centroid_embedding", *proposal["metrics"]):
                setattr(island, field, defaults[field])
            island.archived_at = None
            island.save()
        members = {m["keyword_normalized"]: m for m in proposal["members"]}
        for row in proposal["keywords"]:
            normalized = row["keyword"].strip().lower()
            # Do not reset writing status, rejection memory or newer measurements.
            researched, _ = ResearchedKeyword.objects.get_or_create(
                organization=org, keyword_normalized=normalized, defaults={
                    "keyword": row["keyword"], "volume": row["volume"], "difficulty": round(row["difficulty"]),
                    "difficulty_source": "dataforseo_labs", "intent": row.get("intent") or "informational",
                    "opportunity_index": row["opportunity_index"], "source": "related",
                    "tier": "tier_2_authority",
                },
            )
            member = members.get(normalized, {})
            ContentIslandKeyword.objects.update_or_create(island=island, keyword=researched, defaults={
                "similarity_score": member.get("similarity_score", 0), "is_centroid": member.get("is_centroid", False)})
        if merge_evidence:
            from django.db.models import Sum, Avg, Count
            measured = ResearchedKeyword.objects.filter(island_memberships__island=island)
            metrics = measured.aggregate(keyword_count=Count("id"), total_volume=Sum("volume"),
                avg_difficulty=Avg("difficulty"), opportunity_score=Sum("opportunity_index"), ai_search_volume=Sum("ai_search_volume"))
            for field, value in metrics.items():
                setattr(island, field, value or 0)
            if not preserve_positioning:
                island.name = defaults["name"]
                island.description = defaults["description"]
            island.save()
        ContentIslandSnapshot.objects.update_or_create(island=island, captured_on=now.date(),
            defaults={**{key: getattr(island, key) for key in proposal["metrics"]}, "status": island.status})
        rebuild_island_edges(org)
    return island, created
