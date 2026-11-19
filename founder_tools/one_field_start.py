"""P1.5 and P1.6: the one-field company create (IF-P1-06).

``POST /api/v1/founder-tools/companies/`` with ``createNew`` and
``firstProfile`` takes this path only while ``MY_STARTUP_ONE_FIELD_START_ENABLED``
is on and a startup added now would join the new journey
(``founder_journey_cohort.new_startup_cohort()``). Otherwise every new key is
ignored and today's create runs unchanged.

On this path:

- ``Idempotency-Key`` makes a double press create one company: a replay within
  24 h returns 200 with the same company, a first create returns 201;
- the name may be omitted (derived from the website) and the website may be
  omitted (no website: ``shortDescription`` is then required while
  ``MY_STARTUP_NO_WEBSITE_ARTICLES_ENABLED`` is on);
- ``timezone`` is stored when it is a valid IANA zone and ignored otherwise;
- website and LinkedIn input is normalised, never rejected;
- plan E4: while the GitHub-first gate
  (``ARTICLES_FIRST_ARTICLE_REQUIRES_CONNECTED_WEBSITE``) is on, a website is
  required here, and only here (``ensure_company_organization`` and every
  fundraising and connector create still make or bind domainless companies).
  No website, or a placeholder ``.invalid`` one, is 400 ``website_required``.
  While the website preflight (``MY_STARTUP_WEBSITE_PREFLIGHT_ENABLED``) is
  also on, the create re-runs it (cached per domain): an address that does
  not exist is 400 ``website_not_found`` with any typo suggestion, unless the
  founder pressed "Use it anyway" (``useAnyway: true``). That override is
  never treated as a verified website: nothing here verifies anything;
- plan E2: the name, when none is given, is the preflight's
  ``suggestedName`` (the homepage's own name, else the one name rule in
  ``website_hosts``).
"""
from __future__ import annotations

import hashlib
import logging
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.cache import cache
from django.db import OperationalError

logger = logging.getLogger(__name__)

DESCRIPTION_MIN, DESCRIPTION_MAX = 3, 280
DESCRIPTION_REQUIRED = {"detail": "Tell us what your startup does in one sentence.", "reason": "description_required"}
CREATE_IN_PROGRESS = {"detail": "We're still saving your startup. Try again in a moment.", "reason": "in_progress"}
LOCK_SECONDS = 30
REPLAY_WAIT_SECONDS = 10.0
REPLAY_POLL_SECONDS = 0.1
_IGNORED_KEYS = ("firstProfile", "first_profile", "timezone", "useAnyway", "use_anyway", "notDuplicate",
                 "not_duplicate")
WEBSITE_REQUIRED = {"detail": "Enter your website's address, like yourstartup.com.", "reason": "website_required"}
WEBSITE_NOT_FOUND = {"detail": "We couldn't find a website at this address. Check the spelling, or use it anyway.",
                     "reason": "website_not_found"}


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def one_field_create_requested(data) -> bool:
    """True when this company POST takes the one-field path (flag on, new-journey launch, new company)."""
    from content_factory.founder_journey_cohort import NEW_JOURNEY, new_startup_cohort

    if not getattr(settings, "MY_STARTUP_ONE_FIELD_START_ENABLED", False):
        return False
    if not hasattr(data, "get"):
        return False
    return bool(_truthy(data.get("firstProfile", data.get("first_profile")))
                and _truthy(data.get("createNew", data.get("create_new")))
                and not (data.get("companyId") or data.get("company_id"))
                and new_startup_cohort() == NEW_JOURNEY)


def normalize_website(value) -> str:
    """A bare host from whatever was typed: ``HTTPS://www.Banksia.studio/about?x=1`` gives ``banksia.studio``."""
    from .services import normalize_company_domain

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        host = urlsplit(raw if "://" in raw else f"//{raw}").hostname or ""
    except ValueError:
        host = ""
    host = host.strip().strip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or normalize_company_domain(raw)


def website_required() -> bool:
    """E4: the one-field create needs a website while the GitHub-first gate is on."""
    return bool(getattr(settings, "ARTICLES_FIRST_ARTICLE_REQUIRES_CONNECTED_WEBSITE", False))


def check_website(data):
    """E4 and E1 at create: ``(error, preflight)``.

    ``error`` is a 400 body, or None. ``preflight`` is the domain's preflight
    answer while ``MY_STARTUP_WEBSITE_PREFLIGHT_ENABLED`` is on, else None.
    A preflight that fails on our side never blocks the create.
    """
    from .website_hosts import host_of
    from . import website_preflight

    data = data if hasattr(data, "get") else {}
    domain = normalize_website(data.get("domain"))
    preflight = None
    if domain and website_preflight.enabled():
        host = website_preflight.normalise(domain)
        if host:
            try:
                preflight = website_preflight.cached_check(host)
            except Exception:  # noqa: BLE001 - our fault is never the founder's
                logger.warning("founder_company_create_preflight_failed", exc_info=True)
    if not website_required():
        return None, preflight
    host = host_of(domain)
    if not host or host.endswith(".invalid"):
        return dict(WEBSITE_REQUIRED), None
    if website_preflight.enabled() and not website_preflight.normalise(host):
        return dict(WEBSITE_REQUIRED), None
    if (preflight and preflight.get("reachable") == "no" and (preflight.get("check") or {}).get("outcome") == "nxdomain"
            and not _truthy(data.get("useAnyway", data.get("use_anyway")))):
        return {**WEBSITE_NOT_FOUND, "suggestion": preflight.get("suggestion")}, preflight
    return None, preflight


def add_anyway(data) -> bool:
    """E5: the founder said a name-only match is a different startup."""
    return hasattr(data, "get") and _truthy(data.get("notDuplicate", data.get("not_duplicate")))


def create_key_sha256(request) -> str | None:
    """SHA-256 of the request's ``Idempotency-Key`` header, or None without one."""
    key = str(request.headers.get("Idempotency-Key") or "").strip()
    return hashlib.sha256(key.encode()).hexdigest() if key else None


def prepare_body(data, preflight=None):
    """The serializer body for a one-field create.

    Returns ``(body, error, derived_name)``: ``error`` is a 400 body when no
    website and no one-sentence description were given while
    ``MY_STARTUP_NO_WEBSITE_ARTICLES_ENABLED`` is on; ``derived_name`` is
    the name made from the website when the founder gave none: the
    preflight's ``suggestedName`` for this domain when there is one (E2),
    else the one name rule.
    """
    from .services import normalize_company_linkedin_url

    body = {key: value for key, value in data.items() if key not in _IGNORED_KEYS}
    domain = normalize_website(body.get("domain"))
    if domain:
        body["domain"] = domain
    else:
        body.pop("domain", None)
    alias = body.pop("short_description", None)
    description = body.get("shortDescription") if body.get("shortDescription") is not None else alias
    description = " ".join(str(description or "").split())
    if (not domain and getattr(settings, "MY_STARTUP_NO_WEBSITE_ARTICLES_ENABLED", False)
            and not DESCRIPTION_MIN <= len(description) <= DESCRIPTION_MAX):
        # IF-P1-08: a no-website startup needs its one sentence. With that
        # flag off this is today's name-only create.
        return None, DESCRIPTION_REQUIRED, None
    if description:
        body["shortDescription"] = description
    else:
        body.pop("shortDescription", None)
    name = " ".join(str(body.get("name") or "").split())
    derived = None
    if not name and domain:
        from content_factory.startup_first_profile import derive_company_name
        suggested = preflight.get("suggestedName") if isinstance(preflight, dict) and preflight.get("normalised") == domain else None
        derived = (" ".join(str(suggested).split())[:255] if suggested else "") or derive_company_name(domain) or None
        name = derived or domain
    if name:
        body["name"] = name
    zone = data.get("timezone")
    for key in ("defaultTimezone", "default_timezone"):
        if key in body and not _valid_zone(body[key]):
            body.pop(key)
    if _valid_zone(zone) and "defaultTimezone" not in body and "default_timezone" not in body:
        body["defaultTimezone"] = str(zone).strip()
    for key in ("companyLinkedInUrl", "company_linkedin_url"):
        if key in body:
            try:
                body[key] = normalize_company_linkedin_url(body[key])
            except ValueError:
                body.pop(key)
    return body, None, derived


def _valid_zone(value) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        ZoneInfo(value.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _lock_key(user_id, key_sha) -> str:
    return f"founder-company-create:{user_id}:{key_sha}"


def acquire_create_lock(user_id, key_sha) -> bool:
    """Hold a short cross-worker lock for this key; True when held (or when the cache is down)."""
    try:
        return bool(cache.add(_lock_key(user_id, key_sha), 1, LOCK_SECONDS))
    except Exception:  # noqa: BLE001 - the profile row lock still serialises creates
        logger.warning("founder_company_create_lock_unavailable", exc_info=True)
        return True


def release_create_lock(user_id, key_sha) -> None:
    """Release the lock taken by :func:`acquire_create_lock`."""
    try:
        cache.delete(_lock_key(user_id, key_sha))
    except Exception:  # noqa: BLE001
        logger.warning("founder_company_create_unlock_failed", exc_info=True)


def _done_key(user_id, key_sha) -> str:
    return f"founder-company-created:{user_id}:{key_sha}"


def remember_create(user_id, key_sha, company_id) -> None:
    """Remember which company this key created, for 24 h.

    The org marker holds the key only for a new-journey org; a create that
    binds to an organisation made before launch stays legacy and has no
    marker, so this cache entry is what lets its retry replay. The view
    writes it only when no marker carries the key.
    """
    from content_factory.founder_journey_cohort import CREATE_KEY_WINDOW_HOURS
    try:
        cache.set(_done_key(user_id, key_sha), str(company_id), CREATE_KEY_WINDOW_HOURS * 3600)
    except Exception:  # noqa: BLE001 - the marker still replays new-journey creates
        logger.warning("founder_company_create_remember_failed", exc_info=True)


def find_created_company(profile, user_id, key_sha):
    """The company this key already created for this profile within 24 h, or None."""
    from content_factory.founder_journey_cohort import find_company_for_create_key
    from .models import VibeRaisingCompany

    company = find_company_for_create_key(profile, key_sha)
    if company is not None:
        return company
    try:
        company_id = cache.get(_done_key(user_id, key_sha))
    except Exception:  # noqa: BLE001
        logger.warning("founder_company_create_recall_failed", exc_info=True)
        return None
    if not company_id:
        return None
    return VibeRaisingCompany.objects.filter(pk=company_id, profile=profile).select_related("organization").first()


def wait_for_replay(profile, user_id, key_sha):
    """Wait for the in-flight create with this key.

    Returns ``("replay", company)`` once it is saved, ``("acquired", None)``
    when the other request finished without one (the caller now holds the
    lock and creates), or ``("busy", None)`` after the wait.
    """
    deadline = time.monotonic() + REPLAY_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            company = find_created_company(profile, user_id, key_sha)
        except OperationalError:
            company = None
        if company is not None:
            return "replay", company
        if acquire_create_lock(user_id, key_sha):
            return "acquired", None
        time.sleep(REPLAY_POLL_SECONDS)
    return "busy", None
