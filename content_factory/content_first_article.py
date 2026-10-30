"""P1.6 and P1.7: the "Your first article" card and its one-press start (IF-P1-09).

For a new-journey startup the card always has a topic and, once the founder
has given a one-line description, an audience. One press then saves the
founder's edits, approves the audience (decision 1: the press is the owner's
approval) and, when the website has a call to action on its own domain, an
offer for it, builds the editorial brief, and starts a copy-ready
(``content_only``) article on the free first-article entitlement.

Nothing here dispatches work on a read: the card only reads the catalogue,
the profile, the free topic batch already researched and the entitlement.

Behind ``ARTICLES_FIRST_ARTICLE_CARD_ENABLED`` and ``is_new_journey`` (else the
views return 404). Error bodies use ``detail`` and ``reason`` only.
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction

from . import founder_journey_cohort as cohorts

logger = logging.getLogger(__name__)

VERSION = 1
TOPIC_COUNT = 3
CUSTOM_TOPIC_MIN, CUSTOM_TOPIC_MAX = 3, 160
AUDIENCE_MIN, AUDIENCE_MAX = 3, 280
INFERRED_AUDIENCE_MAX = 80
NAME_MAX = 255
ONE_LINER_MAX = 280
EXISTING_POSTS_IN_BRIEF = 5
FREE_LABEL = "Write my first article · free"
PAID_LABEL = "Write an article · {price} points"
DEFAULT_COUNTRY = "AU"
REQUEST_ID_PREFIX = "first-article:"
SKIPPED_TOPIC_STATUSES = frozenset({"written", "in_progress", "skipped", "declined"})
_FIRST_PERSON = re.compile(r"^(we|i|our|my)\b", re.IGNORECASE)

DETAIL_IN_PROGRESS = "Your article is already being written."
DETAIL_DAILY_CAP = "Free first articles are busy today. Please try again tomorrow."
DETAIL_NOT_AVAILABLE = "This isn't available for your startup yet."
DETAIL_INVALID_TOPIC = "Choose a topic or write your own in 3 to 160 characters."
DETAIL_INVALID_AUDIENCE = "Describe your customer in a short sentence."
DETAIL_INVALID_PROFILE = "Check your startup's name and one-line description."
DETAIL_SWITCH_UNAVAILABLE = "You can't switch topics right now."
DETAIL_OWNER_ONLY = "Only the startup owner can do this."


class FirstArticleRefusal(Exception):
    """A founder-safe refusal: ``detail`` (a sentence), ``reason`` (closed enum) and the HTTP status."""

    def __init__(self, status, detail, reason):
        super().__init__(detail)
        self.status, self.detail, self.reason = status, detail, reason

    def body(self) -> dict:
        """The response body (no registry keys)."""
        return {"detail": self.detail, "reason": self.reason}


def card_enabled() -> bool:
    """True when ``ARTICLES_FIRST_ARTICLE_CARD_ENABLED`` is on (code default off)."""
    return bool(getattr(settings, "ARTICLES_FIRST_ARTICLE_CARD_ENABLED", False))


def applies(organization, config=None) -> bool:
    """The card, start and switch-topic apply: the flag is on and the org is new-journey."""
    return organization is not None and card_enabled() and cohorts.is_new_journey(organization, config)


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _clean(value, limit) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def _https(value):
    text = str(value or "").strip()
    return text if text.startswith("https://") and len(text) <= 2048 else None


# ---------------------------------------------------------------------------
# Profile


def _strategy(config) -> dict:
    strategy = getattr(config, "pillar_strategy", None)
    return strategy if isinstance(strategy, dict) else {}


def first_profile_state(config) -> dict:
    """``pillar_strategy["founder_first_profile"]`` read as JSON (written by be-profile)."""
    return _mapping(_strategy(config).get(cohorts.FIRST_PROFILE_KEY))


def site_profile(config) -> dict:
    """``founder_first_profile.siteProfile``, or {}."""
    return _mapping(first_profile_state(config).get("siteProfile"))


def _startup_profile(organization):
    from startup_updates.models import StartupProfile
    if organization is None or not getattr(organization, "pk", None):
        return None
    return StartupProfile.objects.filter(organization_id=organization.pk).first()


def _author_name(config) -> str | None:
    from .authors import normalize_authors, resolve_default_author
    authors = normalize_authors(getattr(config, "authors", None))
    author = resolve_default_author(authors, getattr(config, "default_author_id", "") or "")
    name = _clean(_mapping(author).get("name"), 160)
    return name or None


def profile_facts(company, organization, config) -> dict:
    """Name, one-liner, target audience, logo and author for the card and the journey."""
    profile = _startup_profile(organization)
    return {
        "name": _clean(getattr(company, "name", "") or getattr(organization, "name", ""), NAME_MAX),
        "oneLiner": _clean(getattr(profile, "short_description", ""), ONE_LINER_MAX) or None,
        "targetAudience": _clean(getattr(profile, "target_audience", ""), AUDIENCE_MAX) or None,
        "logoUrl": _https(getattr(company, "avatar_url", None)),
        "authorName": _author_name(config) if config is not None else None,
        "location": _clean(getattr(company, "location", ""), 255),
    }


# ---------------------------------------------------------------------------
# Audience


def _cut_at_word(text, limit) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit + 1].rsplit(" ", 1)[0].strip()
    return cut or text[:limit].strip()


def infer_audience(one_liner) -> str | None:
    """The audience a one-line description implies.

    The text after its last " for " (trimmed, at most 80 characters, cut at a
    word boundary), else "People who need {one-liner}" with the first letter
    lowercased. None without a one-liner.
    """
    text = _clean(one_liner, ONE_LINER_MAX).rstrip(" .!?")
    if not text:
        return None
    lowered = text.lower()
    index = lowered.rfind(" for ")
    if index >= 0:
        tail = _cut_at_word(text[index + len(" for "):].strip(" .,;:!?"), INFERRED_AUDIENCE_MAX)
        if len(tail) >= AUDIENCE_MIN:
            return tail
    lead = text[:1].lower() + text[1:] if text[:1].isupper() and text[1:2].islower() else text
    return _cut_at_word(f"People who need {lead}", AUDIENCE_MAX)


def _catalog(config):
    from .editorial_catalog import review_payload
    try:
        return review_payload(_strategy(config))
    except ValueError:
        return None


def _audience_payload(item, status):
    return {"id": item.get("id"), "name": _clean(item.get("name"), 160) or _clean(item.get("description"), 80),
            "description": _clean(item.get("description"), 2000), "status": status}


def audience_for(organization, config, facts) -> dict | None:
    """The card's audience: approved, then draft, then ``targetAudience``, then inferred.

    Never None when the profile has a one-line description. None only with no
    audience at all and no description (the journey's ``describe_customer``).
    """
    from .editorial_first_audience import _default_name
    catalog = _catalog(config)
    options = (catalog or {}).get("audience_options") or []
    approved = [item for item in options if item.get("status") == "approved"]
    if approved:
        chosen = next((item for item in approved if item.get("allow_no_offer")), approved[0])
        return _audience_payload(chosen, "approved")
    drafts = [item for item in options if item.get("status") == "draft"]
    if drafts:
        preferred = first_profile_state(config).get("draftAudienceId")
        chosen = next((item for item in drafts if item.get("id") == preferred), drafts[0])
        return _audience_payload(chosen, "draft")
    facts = facts or {}
    if facts.get("targetAudience") and len(facts["targetAudience"]) >= AUDIENCE_MIN:
        description = facts["targetAudience"]
        return {"id": None, "name": _default_name(description), "description": description, "status": "draft"}
    inferred = infer_audience(facts.get("oneLiner"))
    if inferred:
        return {"id": None, "name": _default_name(inferred), "description": inferred, "status": "inferred"}
    return None


# ---------------------------------------------------------------------------
# Call to action


def _bare_domain(domain) -> str:
    domain = str(domain or "").strip().lower().rstrip(".")
    return domain[4:] if domain.startswith("www.") else domain


def call_to_action(organization, config) -> dict | None:
    """The site's primary call to action when its URL is https on the org's domain or a subdomain."""
    if not cohorts.organization_has_website(organization):
        return None
    cta = _mapping(site_profile(config).get("primaryCallToAction"))
    label, url = _clean(cta.get("label"), 120), _https(cta.get("url"))
    if not label or not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    domain = _bare_domain(organization.domain)
    if parts.username or parts.password or not domain or not (host == domain or host.endswith("." + domain)):
        return None
    return {"label": label, "url": url}


# ---------------------------------------------------------------------------
# Topics


def normalise_title(value) -> str:
    """Lowercase with every non-alphanumeric removed (the existing-post match)."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def slugify(value) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")


def existing_posts(config) -> list[dict]:
    """``siteProfile.existingPosts`` as ``[{url, title}]``."""
    posts = site_profile(config).get("existingPosts")
    return [item for item in posts if isinstance(item, dict)] if isinstance(posts, list) else []


def _post_keys(config):
    titles, slugs = set(), set()
    for post in existing_posts(config):
        if normalise_title(post.get("title")):
            titles.add(normalise_title(post.get("title")))
        try:
            path = urlsplit(str(post.get("url") or "")).path
        except ValueError:
            path = ""
        segment = slugify(path.rstrip("/").rsplit("/", 1)[-1])
        if segment:
            slugs.add(segment)
    return titles, slugs


def repeats_existing_post(title, config) -> bool:
    """True when a topic's normalised title or slug matches a post the site already has."""
    titles, slugs = _post_keys(config)
    return normalise_title(title) in titles or slugify(title) in slugs


def _subject(facts, audience) -> str:
    one_liner = _clean(facts.get("oneLiner"), ONE_LINER_MAX).rstrip(" .!?")
    if one_liner and not _FIRST_PERSON.match(one_liner):
        return one_liner[:1].lower() + one_liner[1:] if one_liner[:1].isupper() and one_liner[1:2].islower() else one_liner
    if audience and audience.get("description"):
        return f"{_clean(facts.get('name'), 80) or 'your startup'} for {_cut_at_word(audience['description'], 80).rstrip(' .')}"
    return _clean(facts.get("name"), 80) or "getting started"


def description_topics(facts, audience) -> list[dict]:
    """Three deterministic titles from the name, one-liner and audience (``source: description``).

    Used when no researched topic exists, also when discovery is off. Ids are
    stable for the same inputs.
    """
    subject = _subject(facts or {}, audience)
    templates = ("A beginner's guide to {subject}", "Common mistakes to avoid with {subject}",
                 "Questions to ask before choosing {subject}")
    topics = []
    for index, template in enumerate(templates):
        title = _cut_at_word(template.format(subject=subject), CUSTOM_TOPIC_MAX)
        digest = hashlib.sha256(f"{index}|{title}".encode()).hexdigest()[:16]
        topics.append({"id": f"description:{digest}", "title": title, "source": "description"})
    return topics


def research_topics(organization, config) -> list[dict]:
    """Uncovered researched candidates (the free topic batch), best first. Never dispatches anything."""
    if not cohorts.organization_has_website(organization):
        return []
    from .website_first_article_topics import _number
    try:
        from .vibe_marketing_views import _topic_selection_candidate_pool
        pool = _topic_selection_candidate_pool(organization, config)
    except Exception:
        logger.warning("first_article_card_topics_unavailable org=%s", organization.pk, exc_info=True)
        return []
    eligible = []
    for candidate in pool if isinstance(pool, list) else []:
        if not isinstance(candidate, dict):
            continue
        identifier = str(candidate.get("id") or "")
        if not identifier or identifier.startswith("hidden:") or len(identifier) > 512:
            continue
        if (candidate.get("alreadyWritten") or candidate.get("writtenArticle") or candidate.get("coveredTopic")
                or str(candidate.get("status") or "").lower() in SKIPPED_TOPIC_STATUSES):
            continue
        title = _clean(candidate.get("title") or candidate.get("keyword"), 300)
        if not title:
            continue
        eligible.append(({"id": identifier, "title": title, "source": "research",
                          "keyword": _clean(candidate.get("keyword") or title, 300),
                          "sourceRunId": str(candidate.get("sourceRunId") or "")}, candidate))
    eligible.sort(key=lambda pair: (-_number(pair[1].get("opportunityScore")), -_number(pair[1].get("volume")),
                                    pair[0]["title"], pair[0]["id"]))
    return [item for item, _candidate in eligible]


def topics_for(*, organization, config, facts, audience=None) -> list[dict]:
    """Up to 3 topics (the first is the card's topic), never repeating an existing post."""
    seen, chosen = set(), []
    for topic in [*research_topics(organization, config), *description_topics(facts, audience)]:
        key = normalise_title(topic["title"])
        if key in seen or repeats_existing_post(topic["title"], config):
            continue
        seen.add(key)
        chosen.append(topic)
        if len(chosen) >= TOPIC_COUNT:
            break
    return chosen


def first_topic(*, organization, company, config, facts=None) -> dict | None:
    """The card's topic, for the journey headline."""
    facts = facts or profile_facts(company, organization, config)
    topics = topics_for(organization=organization, config=config, facts=facts,
                        audience=audience_for(organization, config, facts))
    return topics[0] if topics else None


def _public_topic(topic) -> dict:
    return {"id": topic["id"], "title": topic["title"], "source": topic["source"]}


# ---------------------------------------------------------------------------
# The card


def _in_flight_article_run(organization):
    from .founder_journey import ACTIVE_STATUSES, latest_article_run
    run = latest_article_run(organization)
    return run if run is not None and run.status in ACTIVE_STATUSES else None


def card_payload(context, user, config=None) -> dict:
    """The GET body of IF-P1-09. Reads only; never creates a run or calls Content Factory."""
    from .activation import first_article_capability
    from .first_article_entitlement import entitlement_state
    from .founder_journey import article_price_and_balance
    organization, company = context.organization, context.company
    config = config if config is not None else (cohorts._config_for(organization) or cohorts._EmptyConfig)
    facts = profile_facts(company, organization, config)
    audience = audience_for(organization, config, facts)
    topics = topics_for(organization=organization, config=config, facts=facts, audience=audience)
    entitlement = entitlement_state(organization, user, config=config)
    first_article = first_article_capability(entitlement)
    price, balance = article_price_and_balance(organization, user)
    free = bool(first_article["free"])
    available = _in_flight_article_run(organization) is None and first_article["status"] != "in_progress"
    return {
        "version": VERSION,
        "available": available,
        "topic": _public_topic(topics[0]) if topics else None,
        "alternatives": [_public_topic(topic) for topic in topics[1:TOPIC_COUNT]],
        "audience": audience,
        "callToAction": call_to_action(organization, config),
        "label": FREE_LABEL if free else PAID_LABEL.format(price=price),
        "free": free,
        "pricePoints": 0 if free else price,
        "balancePoints": balance,
        "profile": {"name": facts["name"], "oneLiner": facts["oneLiner"], "logoUrl": facts["logoUrl"],
                    "authorName": facts["authorName"]},
        "firstArticle": first_article,
    }


def _config(organization):
    from .models import OrganizationContentConfig
    config, _created = OrganizationContentConfig.objects.get_or_create(organization=organization)
    return config


# ---------------------------------------------------------------------------
# The press


def _owner_id(organization):
    from founder_tools.services import organization_owner_user_id
    return organization_owner_user_id(organization)


def require_owner(user, organization) -> None:
    """Raise a 403 refusal unless ``user`` owns the organisation."""
    if organization is None or _owner_id(organization) != getattr(user, "pk", None):
        raise FirstArticleRefusal(403, DETAIL_OWNER_ONLY, "owner_only")


def _text_field(data, key, low, high, *, reason, detail):
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not low <= len(" ".join(value.split())) <= high:
        raise FirstArticleRefusal(400, detail, reason)
    return " ".join(value.split())


def parse_press(data) -> dict:
    """Validate a start or switch-topic body: exactly one of ``topicId`` and ``customTopic``."""
    data = data if isinstance(data, dict) else {}
    topic_id = data.get("topicId")
    custom = _text_field(data, "customTopic", CUSTOM_TOPIC_MIN, CUSTOM_TOPIC_MAX, reason="invalid_topic",
                         detail=DETAIL_INVALID_TOPIC)
    if topic_id is not None and (not isinstance(topic_id, str) or not topic_id.strip() or len(topic_id) > 512):
        raise FirstArticleRefusal(400, DETAIL_INVALID_TOPIC, "invalid_topic")
    if (topic_id is None) == (custom is None):
        raise FirstArticleRefusal(400, DETAIL_INVALID_TOPIC, "invalid_topic")
    audience = data.get("audience")
    if audience is not None and not isinstance(audience, dict):
        raise FirstArticleRefusal(400, DETAIL_INVALID_AUDIENCE, "invalid_audience")
    audience = audience or {}
    audience_id = audience.get("id")
    if audience_id is not None and (not isinstance(audience_id, str) or not audience_id.strip()):
        raise FirstArticleRefusal(400, DETAIL_INVALID_AUDIENCE, "invalid_audience")
    description = _text_field(audience, "description", AUDIENCE_MIN, AUDIENCE_MAX, reason="invalid_audience",
                              detail=DETAIL_INVALID_AUDIENCE)
    profile = data.get("profile")
    if profile is not None and not isinstance(profile, dict):
        raise FirstArticleRefusal(400, DETAIL_INVALID_PROFILE, "invalid_profile")
    profile = profile or {}
    name = _text_field(profile, "name", 1, NAME_MAX, reason="invalid_profile", detail=DETAIL_INVALID_PROFILE)
    one_liner = _text_field(profile, "oneLiner", 3, ONE_LINER_MAX, reason="invalid_profile",
                            detail=DETAIL_INVALID_PROFILE)
    expected = data.get("expectedCostPoints", data.get("expected_cost_points"))
    return {"topicId": topic_id.strip() if isinstance(topic_id, str) else None, "customTopic": custom,
            "audienceId": audience_id.strip() if isinstance(audience_id, str) else None,
            "audienceDescription": description, "name": name, "oneLiner": one_liner, "expectedCostPoints": expected}


def _resolve_topic(press, topics) -> dict:
    if press["customTopic"]:
        return {"id": None, "title": press["customTopic"], "source": "custom", "keyword": press["customTopic"]}
    topic = next((item for item in topics if item["id"] == press["topicId"]), None)
    if topic is None:
        raise FirstArticleRefusal(400, DETAIL_INVALID_TOPIC, "invalid_topic")
    return topic


def _resolve_audience(press, card_audience, config) -> dict:
    """The audience to approve: a catalogue entry by id, a new description, or the card's audience."""
    if press["audienceId"]:
        catalog = _catalog(config) or {}
        item = next((entry for entry in catalog.get("audience_options") or []
                     if entry.get("id") == press["audienceId"] and entry.get("status") in {"approved", "draft"}), None)
        if item is None:
            raise FirstArticleRefusal(400, DETAIL_INVALID_AUDIENCE, "invalid_audience")
        return {**_audience_payload(item, item["status"]), "version": item.get("version")}
    if press["audienceDescription"]:
        from .editorial_first_audience import _default_name
        return {"id": None, "name": _default_name(press["audienceDescription"]),
                "description": press["audienceDescription"], "status": "inferred"}
    if card_audience is None or len(card_audience.get("description") or "") < AUDIENCE_MIN:
        raise FirstArticleRefusal(400, DETAIL_INVALID_AUDIENCE, "invalid_audience")
    if card_audience.get("id"):
        catalog = _catalog(config) or {}
        item = next((entry for entry in catalog.get("audience_options") or []
                     if entry.get("id") == card_audience["id"]), None)
        return {**card_audience, "version": (item or {}).get("version")}
    return dict(card_audience)


def save_profile_edits(company, organization, press) -> list[str]:
    """Save the press's name and one-liner edits (step 1). Returns the fields changed."""
    from startup_updates.models import StartupProfile
    changed = []
    if press["name"] and press["name"] != company.name:
        company.name = press["name"]
        company.save(update_fields=["name", "updated_at"])
        changed.append("name")
    if press["oneLiner"]:
        profile, _created = StartupProfile.objects.get_or_create(organization=organization)
        if profile.short_description != press["oneLiner"]:
            profile.short_description = press["oneLiner"]
            profile.save(update_fields=["short_description", "updated_at"])
            changed.append("oneLiner")
    return changed


def _offer_id(label, taken) -> str:
    base = slugify(label)[:60].strip("-") or "next-step"
    candidate, suffix = base, 2
    while candidate in taken:
        candidate, suffix = f"{base}-{suffix}", suffix + 1
    return candidate


def _new_audience_option(audience, taken) -> dict:
    from .editorial_first_audience import _audience_id, _reader_task
    description = _clean(audience["description"], AUDIENCE_MAX)
    name = _clean(audience.get("name"), 80) or description[:80]
    return {"catalog_schema_version": 2, "id": _audience_id(name, taken), "name": name, "description": description,
            "reader_task": _reader_task(description), "allow_no_offer": True, "status": "draft", "version": 1,
            "approved_by": None, "approved_at": None}


def _approve_with_offer(context, user, config, audience, cta, *, country, topic_title):
    """One catalogue mutation: activate the audience, then a new offer for the call to action.

    Returns the brief with ``conversion_intent: "offer"``, or None when the
    mutation or the brief is refused (the caller falls back to IF-08).
    """
    from .editorial_catalog import review_payload
    from .editorial_views import mutate_catalog_response
    from .website_first_article_topics import _brief as topic_brief
    current = _catalog(config)
    if current is None:
        return None
    audiences = list(current["audience_options"])
    activate = []
    if audience.get("id"):
        item = next((entry for entry in audiences if entry["id"] == audience["id"]), None)
        if item is None:
            return None
        audience_id = item["id"]
        if item["status"] != "approved":
            activate.append({"kind": "audience", "id": audience_id, "version": item["version"]})
    else:
        option = _new_audience_option(audience, {entry["id"] for entry in audiences})
        audiences.append(option)
        audience_id = option["id"]
        activate.append({"kind": "audience", "id": audience_id, "version": 1})
    offers = list(current["cta_options"])
    covered = any(entry["status"] == "approved" and audience_id in entry["audience_ids"]
                  and country in entry["countries"] and entry.get("button_href") == cta["url"] for entry in offers)
    if not covered:
        startup = _clean(getattr(context.company, "name", ""), 80) or "us"
        offer = {"id": _offer_id(cta["label"], {entry["id"] for entry in offers}), "title": cta["label"],
                 "body": f"{cta['label']} with {startup}.", "button_text": cta["label"], "button_href": cta["url"],
                 "status": "draft", "audience_ids": [audience_id], "countries": [country], "version": 1,
                 "approved_by": None, "approved_at": None}
        offers.append(offer)
        activate.append({"kind": "offer", "id": offer["id"], "version": 1})
    if activate:
        response = mutate_catalog_response(
            {"pk": context.organization.pk},
            {"audience_options": audiences, "cta_options": offers,
             "expected_editorial_catalog_version": current["editorial_catalog_version"],
             "activate_entries": activate},
            owner_context=context, owner_user_id=user.pk)
        if response.status_code != 200:
            logger.info("first_article_offer_refused org=%s status=%s", context.organization.pk,
                        response.status_code)
            return None
    config.refresh_from_db()
    try:
        review_payload(_strategy(config))
        brief = topic_brief(_strategy(config), {"title": topic_title},
                            {"preferred_audience_id": audience_id, "editorial_brief": {"country": country}})
    except ValueError:
        logger.info("first_article_offer_brief_refused org=%s", context.organization.pk)
        return None
    if not brief or brief.get("conversion_intent") != "offer":
        return None
    return brief


def _approve_without_offer(context, user, config, audience):
    """P0 IF-08: approve the audience at press time and return its brief, or raise."""
    from .editorial_first_audience import first_audience_response
    from .editorial_views import mutate_catalog_response
    current = _catalog(config)
    if current is None:
        raise FirstArticleRefusal(409, DETAIL_NOT_AVAILABLE, "not_available")
    if audience.get("id"):
        item = next((entry for entry in current["audience_options"] if entry["id"] == audience["id"]), None)
        if item is not None and item["status"] == "draft":
            response = mutate_catalog_response(
                {"pk": context.organization.pk},
                {"audience_options": current["audience_options"],
                 "expected_editorial_catalog_version": current["editorial_catalog_version"],
                 "activate_entries": [{"kind": "audience", "id": item["id"], "version": item["version"]}]},
                owner_context=context, owner_user_id=user.pk)
            if response.status_code != 200:
                raise FirstArticleRefusal(400, DETAIL_INVALID_AUDIENCE, "invalid_audience")
            config.refresh_from_db()
            current = _catalog(config) or current
    response = first_audience_response(context, user.pk, {
        "description": _clean(audience["description"], AUDIENCE_MAX),
        "name": _clean(audience.get("name"), 80) or None,
        "expected_editorial_catalog_version": current["editorial_catalog_version"]})
    if response.status_code not in (200, 201):
        raise FirstArticleRefusal(400, DETAIL_INVALID_AUDIENCE, "invalid_audience")
    brief = _mapping(response.data).get("editorial_brief")
    if not brief:
        raise FirstArticleRefusal(400, DETAIL_INVALID_AUDIENCE, "invalid_audience")
    config.refresh_from_db()
    return brief


def _with_existing_posts(brief, config) -> dict:
    """Add one acceptance line naming up to 5 existing posts, when the brief builder accepts it."""
    from .editorial_catalog import article_brief_for_catalog
    titles = [_clean(post.get("title"), 120) for post in existing_posts(config) if _clean(post.get("title"), 120)]
    if not titles:
        return brief
    line = "Does not repeat these existing posts: " + "; ".join(titles[:EXISTING_POSTS_IN_BRIEF])
    candidate = {**brief, "acceptance_criteria": [*list(brief.get("acceptance_criteria") or []), line]}
    try:
        checked = article_brief_for_catalog(_strategy(config), {"editorial_brief": candidate})
    except ValueError:
        return brief
    return checked or brief


def request_id(company, attempt) -> str:
    """``first-article:{company_id}:{attempt}``, derived server-side."""
    return f"{REQUEST_ID_PREFIX}{company.pk}:{attempt}"


def _is_first_article_request(run, company) -> bool:
    key = str(_mapping(getattr(run, "run_request", None)).get("client_request_id") or "")
    return key.startswith(f"{REQUEST_ID_PREFIX}{company.pk}:")


def started_body(run_id, status, company) -> dict:
    """The 201 (or replay 200) body."""
    return {"runId": run_id, "status": status,
            "redirect": f"/my-startup/runs/{run_id}/writing?company_id={company.pk}"}


def _replay_or_refuse(context, entitlement):
    """A press while an article is in flight: replay our attempt (200), else 409 in_progress."""
    from workflow_runs.models import ContentFactoryRun
    run = _in_flight_article_run(context.organization)
    if run is None and entitlement.get("status") == "in_progress" and entitlement.get("run_id"):
        run = ContentFactoryRun.objects.filter(organization_id=context.organization.pk,
                                               run_id=entitlement["run_id"]).first()
    if run is not None and _is_first_article_request(run, context.company) and run.status not in {"failed", "cancelled", "denied"}:
        return started_body(run.run_id, run.status, context.company)
    if run is not None or entitlement.get("status") == "in_progress":
        raise FirstArticleRefusal(409, DETAIL_IN_PROGRESS, "in_progress")
    return None


def _start_data(topic, brief, *, client_request_id, expected):
    data = {"client_request_id": client_request_id, "expectedCostPoints": expected,
            "delivery_mode": "content_only", "delivery_mode_explicit": True, "delivery_mode_confirmed": True,
            "editorial_brief": brief}
    if topic["source"] == "research":
        data.update(topic_candidate_id=topic["id"], selected_title=topic["title"],
                    target_keyword=topic.get("keyword") or topic["title"], source_run_id=topic.get("sourceRunId") or "")
    else:
        data.update(custom_title=topic["title"], topic=topic["title"], target_keyword=topic["title"])
    return data


def _passthrough(response) -> bool:
    """The charge path's own 402 and 409 bodies (P0 IF-05) pass through unchanged."""
    if response.status_code == 402:
        return True
    marker = str(_mapping(getattr(response, "data", None)).get("code") or "")
    return response.status_code == 409 and marker in {"roo_points_quote_changed", "roo_points_billing_required",
                                                       "INSUFFICIENT_ROO_POINTS"}


def start_first_article(context, user, data, *, attempt_source="start"):
    """Run the one-press start (IF-P1-09). Returns ``(status, body)``; refusals raise FirstArticleRefusal.

    Order: save profile edits, approve the audience (and the call to action's
    offer, falling back to IF-08), build the brief, derive the request id
    server-side, then start a ``content_only`` run, where the free first
    article is reserved in the same transaction as the dispatch.
    """
    from .first_article_entitlement import entitlement_state
    from . import vibe_marketing_views as views
    organization, company = context.organization, context.company
    require_owner(user, organization)
    press = parse_press(data)
    config = _config(organization)
    entitlement = entitlement_state(organization, user, config=config)
    replay = _replay_or_refuse(context, entitlement)
    if replay is not None:
        return 200, replay
    if entitlement.get("limit") == "daily_cap":
        raise FirstArticleRefusal(409, DETAIL_DAILY_CAP, "daily_cap")
    facts = profile_facts(company, organization, config)
    card_audience = audience_for(organization, config, facts)
    topic = _resolve_topic(press, topics_for(organization=organization, config=config, facts=facts,
                                             audience=card_audience))
    audience = _resolve_audience(press, card_audience, config)
    expected = press["expectedCostPoints"]
    if isinstance(expected, bool) or not isinstance(expected, int):
        expected = None

    save_profile_edits(company, organization, press)
    from .editorial_first_audience import country_from_location
    country = country_from_location(getattr(company, "location", "")) or DEFAULT_COUNTRY
    brief = None
    cta = call_to_action(organization, config)
    if cta is not None:
        brief = _approve_with_offer(context, user, config, audience, cta, country=country, topic_title=topic["title"])
        config.refresh_from_db()
    if brief is None:
        brief = _approve_without_offer(context, user, config, audience)
    brief = _with_existing_posts(brief, config)

    free = bool(entitlement.get("free"))
    attempt = int(entitlement.get("attempts_used") or 0) + 1
    client_request_id = request_id(company, attempt) if free else f"{REQUEST_ID_PREFIX}{company.pk}:paid:{uuid.uuid4().hex[:12]}"
    response = views.start_article_generation(user=user, context=context, data=_start_data(
        topic, brief, client_request_id=client_request_id, expected=expected))
    if response.status_code == 202:
        body = _mapping(response.data)
        run_id = str(body.get("runId") or body.get("run_id") or "")
        logger.info("founder_first_article_started org=%s run=%s attempt=%s free=%s source=%s", organization.pk,
                    run_id, attempt if free else 0, free, attempt_source)
        return 201, started_body(run_id, body.get("status"), company)
    if _passthrough(response):
        return response.status_code, response.data
    if response.status_code == 400:
        raise FirstArticleRefusal(400, DETAIL_INVALID_TOPIC, "invalid_topic")
    logger.info("first_article_start_refused org=%s status=%s", organization.pk, response.status_code)
    raise FirstArticleRefusal(409, DETAIL_NOT_AVAILABLE, "not_available")


def switch_topic(context, user, data):
    """Cancel a slow free first-article run as ``superseded`` and start attempt 2. Returns ``(status, body)``.

    Allowed only while the journey says ``switchTopicAvailable`` for that run.
    The entitlement of the cancelled attempt is released, not consumed, so the
    new attempt is free; a third attempt is never offered.
    """
    from workflow_runs.models import ContentFactoryRun
    from .article_publication_evidence import cancellation_receipt
    from .first_article_entitlement import release_entitlement
    from .founder_journey import project_founder_journey
    from .run_cancellation import cancellation_request_fields
    from . import vibe_marketing_views as views
    organization, company = context.organization, context.company
    require_owner(user, organization)
    data = data if isinstance(data, dict) else {}
    run_id = data.get("runId")
    if not isinstance(run_id, str) or not run_id.strip():
        raise FirstArticleRefusal(409, DETAIL_SWITCH_UNAVAILABLE, "not_available")
    press = parse_press({key: data.get(key) for key in ("topicId", "customTopic") if key in data})
    journey = project_founder_journey(organization, user=user, company=company)
    if not journey.get("switchTopicAvailable") or journey.get("runId") != run_id:
        raise FirstArticleRefusal(409, DETAIL_SWITCH_UNAVAILABLE, "not_available")
    run = ContentFactoryRun.objects.filter(organization_id=organization.pk, run_id=run_id).first()
    if run is None:
        raise FirstArticleRefusal(409, DETAIL_SWITCH_UNAVAILABLE, "not_available")
    config = _config(organization)
    facts = profile_facts(company, organization, config)
    card_audience = audience_for(organization, config, facts)
    topic = _resolve_topic(press, topics_for(organization=organization, config=config, facts=facts,
                                             audience=card_audience))
    remote = views._call_content_factory_run_action(run_id=run.run_id, action="cancel",
                                                    payload=cancellation_request_fields("superseded", "system"),
                                                    workflow=run.workflow)
    remote = remote if isinstance(remote, dict) else {}
    # Without a clean stop the worker may still be writing: never start a
    # second free article on top of it.
    if (remote.get("error") or int(remote.get("content_factory_status_code") or 0) == 409
            or views._article_cancel_receipt_conflict(run, remote)):
        raise FirstArticleRefusal(409, DETAIL_SWITCH_UNAVAILABLE, "not_available")
    try:
        receipt = cancellation_receipt(run, remote)
    except (ValueError, TypeError):
        receipt = None
    delivered = bool(receipt and receipt.get("delivered_value") is True)
    with transaction.atomic():
        if not delivered:
            release_entitlement(organization=organization, run=run, reason="superseded")
        try:
            views._cancel_local_article_run(run=run, organization=organization, remote_data=remote,
                                            cancellation_reason="superseded", cancelled_by="system")
        except (ValueError, TypeError):
            raise FirstArticleRefusal(409, DETAIL_SWITCH_UNAVAILABLE, "not_available")
    if delivered:
        raise FirstArticleRefusal(409, DETAIL_SWITCH_UNAVAILABLE, "not_available")
    start_body = {"topicId": topic["id"]} if topic["id"] else {"customTopic": topic["title"]}
    start_body["expectedCostPoints"] = 0
    if card_audience and card_audience.get("id"):
        start_body["audience"] = {"id": card_audience["id"]}
    return start_first_article(context, user, start_body, attempt_source="switch_topic")
