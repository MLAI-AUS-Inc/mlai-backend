"""User-authored island briefs; persisted in the existing island description column."""
import hashlib
import json


def validate_custom_island(data):
    if not isinstance(data, dict):
        raise ValueError("Describe your subject and audience to create an island.")
    data = dict(data)
    # Accept earlier launch-oriented clients, while keeping the brief general.
    if "subject" not in data:
        data["subject"] = data.get("productName")
    elif "productName" in data and data["productName"] != data["subject"]:
        raise ValueError("Use one subject for this island.")
    limits = {"subject": (1, 120), "description": (20, 2000),
              "audience": (3, 300), "focus": (3, 500),
              "name": (1, 160), "keyword": (1, 200)}
    clean = {}
    labels = {"subject": "Island subject", "description": "Content description",
              "audience": "Audience", "focus": "Content focus", "name": "Island name", "keyword": "Search theme"}
    for key, (minimum, maximum) in limits.items():
        value = data.get(key)
        if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
            raise ValueError(f"{labels[key]} must be {minimum}–{maximum} characters.")
        clean[key] = value.strip()
    return clean


def custom_island_description(brief):
    return (f"Subject: {brief['subject']}\n\n{brief['description']}\n\n"
            f"Audience: {brief['audience']}\n\nContent focus: {brief['focus']}")


def custom_island_slug(brief):
    # Content-addressed identity makes a repeated save safe even after a lost response.
    from django.utils.text import slugify
    # Retain the earlier identity format so older clients can safely retry a save.
    identity = {"productName" if key == "subject" else key: value for key, value in brief.items()}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    return f"{slugify(brief['name'])[:55] or 'custom-island'}-{fingerprint}"


def custom_island_pillar(island):
    return {"id": f"island:{island.slug}", "slug": island.slug, "name": island.name,
            "description": island.description, "pillarKeyword": island.pillar_keyword,
            "iconKey": island.icon_key, "colorKey": island.color_key,
            "source": "content_island", "ideaCount": 0, "topicCandidates": []}


def resolve_island_discovery_scope(organization, config, slug):
    """Resolve a stored brief in the requesting company, with legacy pillar support."""
    from .models import ContentIsland, ContentIslandStatus
    island = ContentIsland.objects.filter(
        organization=organization, slug=slug, status=ContentIslandStatus.VISIBLE,
    ).first()
    if island:
        return {"name": island.name, "keyword": island.pillar_keyword, "context": island.description,
                "icon_key": island.icon_key, "color_key": island.color_key}
    from .vibe_marketing_views import _topic_pillars_for_bootstrap
    pillar = next((pillar for pillar in _topic_pillars_for_bootstrap(organization, config, compact=True)
                   if pillar["slug"] == slug), None)
    if not pillar:
        raise ValueError("Choose an island belonging to this company.")
    return {"name": pillar["name"], "keyword": pillar.get("pillarKeyword") or pillar["name"],
            "context": "", "icon_key": pillar.get("iconKey") or "default",
            "color_key": pillar.get("colorKey") or "purple"}
