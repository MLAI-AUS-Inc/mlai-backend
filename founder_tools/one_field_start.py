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
  omitted (no website: ``shortDescription`` is then required);
- ``timezone`` is stored when it is a valid IANA zone and ignored otherwise;
- website and LinkedIn input is normalised, never rejected.
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
_IGNORED_KEYS = ("firstProfile", "first_profile", "timezone")


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


def create_key_sha256(request) -> str | None:
    """SHA-256 of the request's ``Idempotency-Key`` header, or None without one."""
    key = str(request.headers.get("Idempotency-Key") or "").strip()
    return hashlib.sha256(key.encode()).hexdigest() if key else None


def prepare_body(data):
    """The serializer body for a one-field create.

    Returns ``(body, error, derived_name)``: ``error`` is a 400 body when no
    website and no one-sentence description were given; ``derived_name`` is
    the name made from the website when the founder gave none.
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
    if not domain and not DESCRIPTION_MIN <= len(description) <= DESCRIPTION_MAX:
        return None, DESCRIPTION_REQUIRED, None
    if description:
        body["shortDescription"] = description
    else:
        body.pop("shortDescription", None)
    name = " ".join(str(body.get("name") or "").split())
    derived = None
    if not name and domain:
        from content_factory.startup_first_profile import derive_company_name
        derived = derive_company_name(domain) or None
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


def wait_for_replay(profile, user_id, key_sha):
    """Wait for the in-flight create with this key.

    Returns ``("replay", company)`` once it is saved, ``("acquired", None)``
    when the other request finished without one (the caller now holds the
    lock and creates), or ``("busy", None)`` after the wait.
    """
    from content_factory.founder_journey_cohort import find_company_for_create_key

    deadline = time.monotonic() + REPLAY_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            company = find_company_for_create_key(profile, key_sha)
        except OperationalError:
            company = None
        if company is not None:
            return "replay", company
        if acquire_create_lock(user_id, key_sha):
            return "acquired", None
        time.sleep(REPLAY_POLL_SECONDS)
    return "busy", None
