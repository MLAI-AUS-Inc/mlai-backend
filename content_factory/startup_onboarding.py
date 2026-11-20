"""New startup onboarding (v3): "Check your details" and the Dashboard's next step.

Everything here is additive and gated by ``MY_STARTUP_ONBOARDING_V3_ENABLED``.
The journey view passes ``onboarding=True`` only when the client sends
``?onboarding=3``, so older clients keep today's journey body byte for byte.

The confirmed marker lives in the first-profile state
(``pillar_strategy["founder_first_profile"]["confirmedAt"]``), written with the
same locked update as the website read. No migration.

A startup is *unconfirmed* (the journey asks ``confirm_profile``) when:

- the setting is on and the organisation is in the new journey;
- it has no ``confirmedAt``;
- it has no article run, no setup run and no website connection; and
- it was created at or after ``MY_STARTUP_ONBOARDING_V3_LAUNCHED_AT``, or it
  has no one-liner yet.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

CONFIRMED_KEY = "confirmedAt"
SEED_SOURCE_KEY = "seedKeywordsSource"
SEARCH_TERM_LIMIT = 4
IDEA_LIMIT = 3
# Site builders whose code is not on GitHub (BE #1367's host-class table replaces this when it lands).
PLATFORM_LABELS = {"wix": "Wix", "squarespace": "Squarespace", "webflow": "Webflow", "wordpress": "WordPress",
                   "ghost": "Ghost", "framer": "Framer", "shopify": "Shopify"}
GITHUB_LIKELY_PLATFORMS = frozenset({"nextjs"})


def enabled() -> bool:
    """True when ``MY_STARTUP_ONBOARDING_V3_ENABLED`` is on (default off)."""
    return bool(getattr(settings, "MY_STARTUP_ONBOARDING_V3_ENABLED", False))


def launched_at():
    """The parsed ``MY_STARTUP_ONBOARDING_V3_LAUNCHED_AT``, or None when unset, unparsable or naive."""
    raw = str(getattr(settings, "MY_STARTUP_ONBOARDING_V3_LAUNCHED_AT", "") or "").strip()
    if not raw:
        return None
    try:
        value = parse_datetime(raw)
    except ValueError:
        value = None
    if value is None or timezone.is_naive(value):
        logger.warning("startup_onboarding_launched_at_invalid")
        return None
    return value


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _first_profile(config) -> dict:
    from .founder_journey_cohort import FIRST_PROFILE_KEY
    strategy = getattr(config, "pillar_strategy", None)
    return _mapping(strategy.get(FIRST_PROFILE_KEY)) if isinstance(strategy, dict) else {}


def is_confirmed(config) -> bool:
    return bool(_first_profile(config).get(CONFIRMED_KEY))


def is_unconfirmed(organization, *, config, run, setup_run, connection, one_liner: str) -> bool:
    """Whether this startup should be asked to check its details (see the module docstring)."""
    from . import founder_journey_cohort as cohorts
    if not enabled() or organization is None or not cohorts.is_new_journey(organization):
        return False
    if is_confirmed(config) or run is not None or setup_run is not None or connection is not None:
        return False
    if not one_liner:
        return True
    launch = launched_at()
    created = getattr(organization, "created_at", None)
    return bool(launch is not None and created is not None and created >= launch)


def confirm(organization) -> dict | None:
    """Mark the profile confirmed and stop a late website read changing the name or one-liner.

    Clearing ``derivedName`` and ``fallbackValues`` makes the read's fill-empty
    rule treat the confirmed values as the founder's own.
    """
    from .startup_first_profile import _update_state
    if organization is None or not getattr(organization, "pk", None):
        return None

    def mutate(state):
        state[CONFIRMED_KEY] = timezone.now().isoformat()
        state["derivedName"] = None
        state["fallbackValues"] = {}
        return state
    return _update_state(organization, mutate)


def read_status(config, has_website: bool) -> str:
    """``none`` | ``running`` | ``done`` for the first website read."""
    state = _first_profile(config)
    if state.get("appliedRunId") or state.get("appliedAt"):
        return "done"
    if state.get("runId"):
        return "running"
    return "none" if has_website else "done"


def website_facts(config, has_website: bool) -> dict:
    platform = str(_mapping(_first_profile(config).get("siteProfile")).get("platform") or "").lower()
    if not has_website:
        likely = "unknown"
    elif platform in PLATFORM_LABELS:
        likely = "unlikely"
    elif platform in GITHUB_LIKELY_PLATFORMS:
        likely = "likely"
    else:
        likely = "unknown"
    return {"githubLikely": likely, "platformLabel": PLATFORM_LABELS.get(platform)}


def search_terms(organization, config) -> list[str]:
    """Seed keywords, only when the website read's research found them (never name-derived guesses)."""
    if _first_profile(config).get(SEED_SOURCE_KEY) != "research":
        return []
    terms = []
    for value in getattr(organization, "seed_keywords", None) or []:
        text = " ".join(str(value or "").split())[:80]
        if text and text.lower() not in {term.lower() for term in terms}:
            terms.append(text)
        if len(terms) >= SEARCH_TERM_LIMIT:
            break
    return terms


def ideas(organization, config) -> list[str]:
    """Up to three researched article titles from the free topic batch, when it ran."""
    if not _first_profile(config).get("topicBatchRunId"):
        return []
    from .content_first_article import research_topics
    try:
        topics = research_topics(organization, config)
    except Exception:  # noqa: BLE001 - ideas are a nicety, never a failure
        logger.warning("startup_onboarding_ideas_unavailable org=%s", organization.pk, exc_info=True)
        return []
    return [str(topic.get("title") or "")[:300] for topic in topics[:IDEA_LIMIT] if topic.get("title")]


def block(organization, *, config, user, has_website: bool, unconfirmed: bool) -> dict:
    """The journey's ``onboarding`` object (B3)."""
    from .founder_journey import article_price_and_balance
    price, balance = article_price_and_balance(organization, user)
    return {
        "profile": {"confirmed": not unconfirmed, "read": read_status(config, has_website)},
        "website": website_facts(config, has_website),
        "searchTerms": search_terms(organization, config),
        "ideas": ideas(organization, config),
        "articleCost": price if price > 0 else None,
        "balance": balance if getattr(user, "pk", None) else None,
    }
