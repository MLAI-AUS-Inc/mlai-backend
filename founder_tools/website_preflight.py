"""Plan E1: the website preflight, and the E5 own-profile duplicate check.

``POST /api/v1/founder-tools/website-preflight/`` (Chat alias
``/api/v1/my-startup/founder-tools/website-preflight/``) with ``{domain}``
tells a founder, before any startup exists, whether the address resolves,
what kind of host it is, what the startup will be called, and whether they
already added it::

    {"normalised": "lifestitch.com.au", "reachable": "yes" | "no" | "unknown",
     "check": {"outcome": "ok", "httpStatus": 200, "redirectHost": None},
     "hostClass": "git_host" | "ai_builder" | "no_export" | "unknown",
     "platform": "cloudflare_pages" | ... | None, "guidanceId": ... | None,
     "builderName": "Cloudflare" | None, "deploymentUrl": bool,
     "suggestion": "reidandcoconsulting.com.au" | None,
     "suggestedName": "Lifestitch", "displayName": "Lifestitch",
     "existing": {"state": "none"} | {"state": "yours", "companyId", "name", "match"},
     "notes": [{"code", "message"}], "cached": bool}

Rules (plan 6.1):

* ``reachable`` is ``no`` only for NXDOMAIN or a refused connection. A 403,
  429 or 5xx homepage, a timeout or a TLS fault is ``unknown`` and never
  blocks: bot protection is common on real sites. A homepage fetch error is
  always classified (``check.outcome``), never swallowed.
* A typo suggestion is offered only when the address does not resolve and
  the suggestion does.
* A hosting platform's own address (``workers.dev``, ``up.railway.app``...)
  is accepted for setup (decision D10), with a note to add the public address
  before the first article goes live.
* ``existing`` only ever looks inside the caller's own profile (E5). Another
  account's startup is never named here: the create's exact-domain claim
  check stays the only cross-account answer, and look-alikes across accounts
  go to staff only (``manage.py founder_duplicate_lookalikes``).

Guards: authenticated (the project default), a per-account throttle
(``founder_website_preflight``), a per-domain result cache, the SSRF-pinned
probe (``website_live_fetch.probe_homepage``) with a wall-clock budget, and a
bounded number of typo lookups. The whole endpoint rides
``MY_STARTUP_WEBSITE_PREFLIGHT_ENABLED`` (code default off): off, it answers
404 ``not_enabled`` and a client keeps today's screen.
"""
from __future__ import annotations

import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from concurrent.futures import wait as wait_futures

from django.conf import settings
from django.core.cache import cache
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import website_hosts

logger = logging.getLogger(__name__)

CACHE_VERSION = 1
CACHE_SECONDS = 15 * 60
# An answer that may change soon (a site mid-deploy, a resolver hiccup) is
# kept for less time.
CACHE_UNKNOWN_SECONDS = 2 * 60
PROBE_SECONDS = 8
TYPO_SECONDS = 3
TYPO_MAX_CANDIDATES = 40
TYPO_MIN_LABEL = 5
# How much of the homepage the fingerprints read.
PROBE_HTML_SCAN = 200_000
NOT_ENABLED = {"detail": "Not available.", "reason": "not_enabled"}
INVALID = {"detail": "Enter your website's address, like yourstartup.com.", "reason": "invalid_website"}

REACHABLE_YES = frozenset({"ok", "redirect"})
REACHABLE_NO = frozenset({"nxdomain", "refused"})

# Founder copy for each note. No jargon, no dashes.
NOTE_COPY = {
    "not_found": "We couldn't find a website at this address. Check the spelling.",
    "not_answering": "This address exists but no website answered. If it isn't live yet, you can still use it.",
    "check_blocked": "Your website didn't let us look at it, which is common and fine. You can carry on.",
    "check_unavailable": "We couldn't check your website just now. You can carry on.",
    "not_public": "This address doesn't point to a public website.",
    "deployment_url": ("This looks like a hosting address rather than your public website. You can use it to set up; "
                       "add your public address before your first article goes live."),
    "redirects": "This address sends visitors to {host}.",
    "no_export": "Websites built in {builder} keep their code inside {builder}, so they can't be connected yet.",
    "ai_builder": "Websites built in {builder} can usually send their code to GitHub first.",
}

# Response fingerprints for a founder's own domain: (guidance id, header
# test, HTML test). Only a strong, product-specific signal counts.
_FINGERPRINTS = (
    ("wix", lambda h: "x-wix-request-id" in h, re.compile(r'content="Wix\.com Website Builder"', re.I)),
    ("squarespace", lambda h: h.get("server", "").lower() == "squarespace",
     re.compile(r"static1\.squarespace\.com", re.I)),
    ("webflow", lambda h: False, re.compile(r'<meta[^>]+content="Webflow"|data-wf-site=', re.I)),
    ("framer", lambda h: h.get("server", "").lower() == "framer", re.compile(r'content="Framer[ "]|framerusercontent\.com', re.I)),
    ("shopify", lambda h: "x-shopid" in h or "x-shopify-stage" in h, re.compile(r"cdn\.shopify\.com", re.I)),
    ("vercel", lambda h: "x-vercel-id" in h or h.get("server", "").lower() == "vercel", None),
    ("netlify", lambda h: "x-nf-request-id" in h or h.get("server", "").lower() == "netlify", None),
    ("github_pages", lambda h: h.get("server", "").lower() == "github.com", None),
)
# A content system is advice for later steps (plan C15), never a host class.
_CONTENT_SYSTEMS = (("wordpress", re.compile(r'content="WordPress|/wp-content/', re.I)),
                    ("ghost", re.compile(r'content="Ghost \d', re.I)))
_TLD_FIXES = {"con": "com", "cmo": "com", "ocm": "com", "comm": "com", "om": "com", "cm": "com", "vom": "com",
              "xom": "com", "nte": "net", "ogr": "org", "orgg": "org", "com.aus": "com.au", "comau": "com.au",
              "con.au": "com.au", "cmo.au": "com.au", "ocm.au": "com.au", "co.nzz": "co.nz"}


def enabled() -> bool:
    """True while ``MY_STARTUP_WEBSITE_PREFLIGHT_ENABLED`` is on (code default off)."""
    return getattr(settings, "MY_STARTUP_WEBSITE_PREFLIGHT_ENABLED", False) is True


def normalise(value) -> str:
    """A checkable public hostname from what was typed, or ``''``.

    Never an IP literal, ``localhost``, a placeholder ``.invalid`` address or
    anything that is not a hostname.
    """
    host = website_hosts.host_of(value)
    if not host or host.endswith((".invalid", ".local", ".localhost", ".internal", ".test", ".example")):
        return ""
    if re.fullmatch(r"[\d.]+", host):
        return ""
    return host


def _cache_key(host) -> str:
    return f"founder-website-preflight:v{CACHE_VERSION}:{hashlib.sha256(host.encode()).hexdigest()}"


def _registrable(host):
    labels = host.split(".")
    platform = website_hosts.platform_for(host)
    if platform is not None:
        size = len(platform.suffix.split(".")) + (1 if platform.account_label else 0)
        if len(labels) <= size:
            return None
        return ".".join(labels[:-size]), ".".join(labels[-size:])
    size = website_hosts.public_suffix_length(labels)
    return ".".join(labels[:-size]), ".".join(labels[-size:])


def typo_candidates(host) -> list[str]:
    """Close spellings of ``host`` to try, most plausible first (bounded).

    A fixed top-level-domain slip (``.con``), then one dropped letter
    (``consuliting`` to ``consulting``), then two swapped letters. Only the
    startup's own label is edited, never a platform's suffix.
    """
    parts = _registrable(host)
    if parts is None:
        return []
    front, suffix = parts
    label = front.split(".")[-1]
    prefix = front[: len(front) - len(label)]
    seen, out = {host}, []

    def add(value):
        if value not in seen and normalise(value) == value:
            seen.add(value)
            out.append(value)

    if suffix in _TLD_FIXES:
        add(f"{front}.{_TLD_FIXES[suffix]}")
    if len(label) >= TYPO_MIN_LABEL:
        # A doubled or stray letter first, left to right.
        for index in range(len(label)):
            add(f"{prefix}{label[:index]}{label[index + 1:]}.{suffix}")
        for index in range(len(label) - 1):
            if label[index] != label[index + 1]:
                swapped = label[:index] + label[index + 1] + label[index] + label[index + 2:]
                add(f"{prefix}{swapped}.{suffix}")
    return out[:TYPO_MAX_CANDIDATES]


def suggest_spelling(host, *, resolve) -> str | None:
    """The first close spelling that resolves to a public address, within :data:`TYPO_SECONDS`."""
    candidates = typo_candidates(host)
    if not candidates:
        return None
    executor = ThreadPoolExecutor(max_workers=8)
    try:
        futures = {executor.submit(resolve, candidate): candidate for candidate in candidates}
        done, _pending = wait_futures(futures, timeout=TYPO_SECONDS)
        resolved = set()
        for future in done:
            try:
                state, _addresses = future.result()
            except Exception:  # noqa: BLE001 - one failed lookup is just no suggestion
                continue
            if state == "ok":
                resolved.add(futures[future])
        return next((candidate for candidate in candidates if candidate in resolved), None)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _fingerprint(headers, body):
    text = body[:PROBE_HTML_SCAN].decode("utf-8", errors="replace") if body else ""
    for guidance_id, header_test, html_test in _FINGERPRINTS:
        try:
            if header_test(headers) or (html_test is not None and text and html_test.search(text)):
                return guidance_id
        except Exception:  # noqa: BLE001 - a fingerprint is a hint, never a gate
            continue
    return None


def _content_system(body):
    text = body[:PROBE_HTML_SCAN].decode("utf-8", errors="replace") if body else ""
    return next((name for name, pattern in _CONTENT_SYSTEMS if text and pattern.search(text)), None)


def _probe(host, *, probe, resolve):
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        return executor.submit(probe, host, resolve=resolve).result(timeout=PROBE_SECONDS)
    except FutureTimeout:
        return {"outcome": "timeout", "status": None, "headers": {}, "body": b"", "redirectHost": None}
    except Exception as exc:  # noqa: BLE001 - classified, never swallowed into "no answer"
        logger.info("founder_website_preflight_probe_failed reason=%s", type(exc).__name__)
        return {"outcome": "connect_error", "status": None, "headers": {}, "body": b"", "redirectHost": None}
    finally:
        executor.shutdown(wait=False)


def _note(code, **values):
    return {"code": code, "message": NOTE_COPY[code].format(**values)}


def check_domain(host, *, probe=None, resolve=None) -> dict:
    """The domain-level preflight answer for a normalised ``host`` (no account data)."""
    from content_factory.startup_first_profile import parse_homepage_meta
    from content_factory.website_live_fetch import probe_homepage, resolve_public

    probe = probe or probe_homepage
    resolve = resolve or resolve_public
    observed = _probe(host, probe=probe, resolve=resolve)
    outcome = str(observed.get("outcome") or "connect_error")
    http_status = observed.get("status")
    headers = observed.get("headers") if isinstance(observed.get("headers"), dict) else {}
    body = observed.get("body") if isinstance(observed.get("body"), (bytes, bytearray)) else b""
    redirect_host = website_hosts.host_of(observed.get("redirectHost")) or None
    reachable = "yes" if outcome in REACHABLE_YES else "no" if outcome in REACHABLE_NO else "unknown"

    platform = website_hosts.platform_for(host)
    guidance_id = platform.guidance_id if platform else None
    if guidance_id is None and redirect_host and website_hosts.platform_for(redirect_host):
        guidance_id = website_hosts.platform_for(redirect_host).guidance_id
    if guidance_id is None and outcome == "ok":
        guidance_id = _fingerprint(headers, body)
    guidance = website_hosts.guidance_for(guidance_id) or {}
    host_class = guidance.get("hostClass") or "unknown"

    meta = parse_homepage_meta(body, observed.get("finalHost") or host) if outcome == "ok" and body else None
    notes = []
    suggestion = None
    if outcome == "nxdomain":
        suggestion = suggest_spelling(host, resolve=resolve)
        notes.append(_note("not_found"))
    elif outcome == "refused":
        notes.append(_note("not_answering"))
    elif outcome == "not_public":
        notes.append(_note("not_public"))
    elif outcome == "http_status" and http_status in {401, 403, 429}:
        notes.append(_note("check_blocked"))
    elif outcome not in REACHABLE_YES:
        notes.append(_note("check_unavailable"))
    if outcome == "redirect" and redirect_host and website_hosts.site_key(redirect_host) != website_hosts.site_key(host):
        notes.append(_note("redirects", host=redirect_host))
    if platform is not None:
        notes.append(_note("deployment_url"))
    if host_class in {"no_export", "ai_builder"}:
        notes.append(_note(host_class, builder=guidance.get("builderName")))
    name = website_hosts.suggested_company_name(host, meta)
    return {
        "version": 1, "normalised": host, "reachable": reachable,
        "check": {"outcome": outcome, "httpStatus": http_status if isinstance(http_status, int) else None,
                  "redirectHost": redirect_host},
        "hostClass": host_class, "platform": guidance_id, "guidanceId": guidance_id,
        "builderName": guidance.get("builderName"), "deploymentUrl": platform is not None,
        "contentSystem": _content_system(body) if outcome == "ok" else None,
        "suggestion": suggestion, "suggestedName": name or None, "displayName": name or None,
        "notes": notes,
    }


def cached_check(host, *, probe=None, resolve=None, refresh=False) -> dict:
    """:func:`check_domain` through the per-domain cache. A cache fault only costs a re-check."""
    key = _cache_key(host)
    if not refresh:
        try:
            hit = cache.get(key)
        except Exception:  # noqa: BLE001
            hit = None
        if isinstance(hit, dict) and hit.get("version") == 1 and hit.get("normalised") == host:
            return {**hit, "cached": True}
    result = check_domain(host, probe=probe, resolve=resolve)
    seconds = CACHE_SECONDS if result["reachable"] != "unknown" else CACHE_UNKNOWN_SECONDS
    try:
        cache.set(key, result, seconds)
    except Exception:  # noqa: BLE001
        logger.warning("founder_website_preflight_cache_unavailable", exc_info=True)
    return {**result, "cached": False}


# ---------------------------------------------------------------------------
# E5: the own-profile duplicate check


def name_key(value) -> str:
    """A name compared without case, spacing or punctuation ("Safe Trase" is "safetrase")."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def own_duplicate(profile, *, name=None, domain=None, exclude_company_id=None):
    """``(company, match)`` for a startup this profile already added, else ``(None, None)``.

    Only the caller's own profile is read. ``match`` is ``domain`` (the same
    site, after stripping subdomains of the founder's own domain; two sites on
    one hosting platform stay two sites) or ``name`` (the same name). A
    provisional research workspace is not a startup yet (``visible_companies``
    leaves it out), so the create may still reuse it.
    """
    from .profile_fields import visible_companies

    if profile is None or not getattr(profile, "pk", None):
        return None, None
    key = website_hosts.site_key(domain)
    wanted_name = name_key(name)
    by_name = None
    for company in visible_companies(profile.companies.all().order_by("created_at", "pk")):
        if exclude_company_id is not None and company.pk == exclude_company_id:
            continue
        if key and website_hosts.site_key(company.domain) == key:
            return company, "domain"
        if by_name is None and wanted_name and name_key(company.name) == wanted_name:
            by_name = company
    return (by_name, "name") if by_name is not None else (None, None)


ALREADY_ADDED_DETAIL = "You already added this. Open it to carry on."


def already_added_body(company, match) -> dict:
    """The 409 body of a create that matches one of the caller's own startups."""
    return {"detail": ALREADY_ADDED_DETAIL, "code": "company_already_added", "reason": "already_added",
            "companyId": str(company.pk), "name": company.name, "match": match,
            # A name-only match may be a different startup: the founder may say so.
            "canAddAnyway": match == "name"}


def existing_for(user, host, name) -> dict:
    from .services import get_or_create_founder_profile

    try:
        profile = get_or_create_founder_profile(user)
        company, match = own_duplicate(profile, name=name, domain=host)
    except Exception:  # noqa: BLE001 - the create repeats this check
        logger.warning("founder_website_preflight_existing_failed", exc_info=True)
        return {"state": "none"}
    if company is None:
        return {"state": "none"}
    return {"state": "yours", "companyId": str(company.pk), "name": company.name, "match": match,
            "message": ALREADY_ADDED_DETAIL}


class FounderWebsitePreflightView(APIView):
    """``POST {domain}``: what MLAI can tell about a website before a startup is created."""

    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "founder_website_preflight"

    def post(self, request):
        if not enabled():
            return Response(dict(NOT_ENABLED), status=status.HTTP_404_NOT_FOUND)
        data = request.data if hasattr(request.data, "get") else {}
        raw = data.get("domain", data.get("website"))
        if not isinstance(raw, str) or len(raw) > 300:
            return Response(dict(INVALID), status=status.HTTP_400_BAD_REQUEST)
        host = normalise(raw)
        if not host:
            return Response(dict(INVALID), status=status.HTTP_400_BAD_REQUEST)
        result = cached_check(host)
        result["existing"] = existing_for(request.user, host, result.get("suggestedName"))
        return Response(result, status=status.HTTP_200_OK)
