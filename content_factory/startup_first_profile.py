"""P1.5 and P1.6: the one-field start's first profile (IF-P1-06, IF-P1-07).

A new-journey founder types one website. The company is created first, then a
free, budgeted ("first_profile") autofill runs on that real organisation, and
this module turns its result into a profile that is never empty:

- :func:`homepage_meta` reads the homepage title, description and images with
  the SSRF-pinned fetcher before dispatch, so a failed research run still has
  a name and a one-liner to show.
- :func:`apply_first_profile` runs exactly once per organisation when the
  autofill run is terminal (on the client's poll and from :func:`sweep`). It
  fills empty fields only, stores the site profile, seeds one draft audience
  (never an offer) and starts the free first topic batch.
- One automatic retry for a transient failure, never more, and never counted
  against the free-setup daily cap.

Everything here is gated by ``MY_STARTUP_ONE_FIELD_START_ENABLED`` and
``founder_journey_cohort.is_new_journey(org)``: a legacy organisation is never
touched, whatever the flags say.

State lives in ``OrganizationContentConfig.pillar_strategy["founder_first_profile"]``
(no migration)::

    {"version": 1, "derivedName": str | None, "homepageMeta": {...} | None,
     "siteProfile": {...} | None, "appliedRunId": str | None, "appliedAt": ISO | None,
     "retryCount": 0 | 1, "topicBatchRunId": str | None,
     # bookkeeping, additive:
     "runId": str | None, "startedAt": ISO | None, "fallbackValues": {...},
     "draftAudienceId": str | None, "topicBatchAttemptedAt": ISO | None}
"""
from __future__ import annotations

import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from copy import deepcopy
from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .founder_journey_cohort import FIRST_PROFILE_KEY, is_new_journey, organization_has_website

logger = logging.getLogger(__name__)

FIRST_PROFILE_DEPTH = "first_profile"
BUDGET_SECONDS = 90
HOMEPAGE_FETCH_SECONDS = 6
# A first-profile run still running after this is treated as finished with no
# result, so the founder still gets a name and a one-liner.
STALE_RUN_AFTER = timedelta(minutes=15)
SWEEP_WINDOW = timedelta(hours=24)
SWEEP_LIMIT = 50
# Configs read per tick, whatever their state, so the scan never grows with the cohort.
SWEEP_SCAN_LIMIT = 500
STATE_VERSION = 1
MAX_EXISTING_POSTS = 50
SITE_PLATFORMS = frozenset({"wix", "squarespace", "webflow", "wordpress", "ghost", "framer", "shopify", "nextjs", "unknown"})
_COMPLETED = "completed"
_RUNNING = frozenset({"queued", "running"})
_TERMINAL = frozenset({"completed", "failed", "blocked", "cancelled", "denied"})
_TWO_LEVEL_SUFFIXES = frozenset({
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "asn.au", "id.au",
    "co.nz", "net.nz", "org.nz", "co.uk", "org.uk", "ac.uk", "me.uk",
    "com.sg", "co.in", "com.br", "co.za", "com.my", "co.jp",
})
_STOPWORDS = frozenset("""
a an and are as at be by for from has have helps help in into is it its of on or our that the their them they this to
we with you your who what which while will can all any more most new best get make made just only also over under via
""".split())


def one_field_start_enabled() -> bool:
    """True when ``MY_STARTUP_ONE_FIELD_START_ENABLED`` is on (code default off)."""
    return bool(getattr(settings, "MY_STARTUP_ONE_FIELD_START_ENABLED", False))


def no_website_articles_enabled() -> bool:
    """True when ``MY_STARTUP_NO_WEBSITE_ARTICLES_ENABLED`` is on (code default off, IF-P1-08)."""
    return bool(getattr(settings, "MY_STARTUP_NO_WEBSITE_ARTICLES_ENABLED", False))


def first_profile_applies(organization, config=None) -> bool:
    """The one-field start applies: its flag is on and the organisation is new-journey."""
    return organization is not None and one_field_start_enabled() and is_new_journey(organization, config)


# ---------------------------------------------------------------------------
# Names and websites


def derive_company_name(domain) -> str:
    """A readable startup name from a website: ``banksia-studio.com.au`` gives "Banksia Studio"."""
    host = str(domain or "").strip().lower()
    if "://" in host or "/" in host:
        try:
            host = urlsplit(host if "://" in host else f"//{host}").hostname or ""
        except ValueError:
            host = ""
    labels = [label for label in host.strip(".").split(".") if label]
    if labels and labels[0] == "www":
        labels = labels[1:]
    if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_LEVEL_SUFFIXES:
        labels = labels[:-2]
    elif len(labels) >= 2:
        labels = labels[:-1]
    if not labels:
        return ""
    words = [word for word in re.split(r"[-_]+", labels[-1]) if word]
    return " ".join(word[:1].upper() + word[1:] for word in words)[:255]


def _clean(value, limit) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].strip() if text else ""


def _https(value, base=None):
    raw = str(value or "").strip()
    if not raw:
        return None
    if base:
        raw = urljoin(base, raw)
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return raw[:2048]


def _title_name(title) -> str:
    """The brand part of a page title: "Banksia | Garden design" gives "Banksia"."""
    text = _clean(title, 200)
    for separator in (" | ", " - ", " \u2013 ", " \u2014 ", " :: ", " \u00b7 ", " \u2022 "):
        if separator in text:
            text = text.split(separator)[0].strip()
    return text[:80]


# ---------------------------------------------------------------------------
# Homepage meta (backend side, pinned fetch)


class _HomepageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.icon = None
        self.title = ""
        self._in_title = False
        self._title_done = False

    def handle_starttag(self, tag, attrs):
        values = {str(key).lower(): value or "" for key, value in attrs}
        if tag == "title" and not self._title_done:
            self._in_title = True
        elif tag == "meta":
            key = str(values.get("property") or values.get("name") or "").strip().lower()
            if key and key not in self.meta and values.get("content"):
                self.meta[key] = values["content"]
        elif tag == "link" and self.icon is None:
            rel = str(values.get("rel") or "").lower().split()
            if values.get("href") and ("icon" in rel or "apple-touch-icon" in rel):
                self.icon = values["href"]

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self._in_title = False
            self._title_done = True

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def parse_homepage_meta(body, domain) -> dict | None:
    """``{title, description, ogSiteName, ogImage, icon}`` from homepage HTML, or None when it has none."""
    text = body.decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else str(body or "")
    parser = _HomepageParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed HTML is just no metadata
        logger.info("founder_first_profile_homepage_unparsable")
    base = f"https://{domain}/"
    meta = parser.meta
    result = {
        "title": _clean(parser.title, 200) or _clean(meta.get("og:title"), 200) or None,
        "description": _clean(meta.get("description"), 500) or _clean(meta.get("og:description"), 500) or None,
        "ogSiteName": _clean(meta.get("og:site_name"), 120) or None,
        "ogImage": _https(meta.get("og:image") or meta.get("og:image:url") or meta.get("twitter:image"), base),
        "icon": _https(parser.icon, base),
    }
    return result if any(result.values()) else None


def homepage_meta(domain) -> dict | None:
    """Fetch the homepage through the pinned fetcher within 6 s and read its metadata.

    Every fetch error (DNS, TLS, redirects, size, time) is swallowed: the
    caller simply has no homepage metadata.
    """
    from .website_live_fetch import fetch_live_route

    domain = str(domain or "").strip().lower().rstrip(".")
    if not domain or domain.endswith(".invalid"):
        return None
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(fetch_live_route, f"https://{domain}/", domain)
        body, _headers = future.result(timeout=HOMEPAGE_FETCH_SECONDS)
    except FutureTimeout:
        logger.info("founder_first_profile_homepage_unavailable reason=timeout")
        return None
    except Exception as exc:  # noqa: BLE001 - never surfaced to the founder
        logger.info("founder_first_profile_homepage_unavailable reason=%s", type(exc).__name__)
        return None
    finally:
        executor.shutdown(wait=False)
    return parse_homepage_meta(body, domain)


# ---------------------------------------------------------------------------
# State


def _empty_state() -> dict:
    return {
        "version": STATE_VERSION, "derivedName": None, "homepageMeta": None, "siteProfile": None,
        "appliedRunId": None, "appliedAt": None, "retryCount": 0, "topicBatchRunId": None,
    }


def read_state(organization, config=None) -> dict:
    """The organisation's ``founder_first_profile`` state (a copy), or ``{}``."""
    from .models import OrganizationContentConfig
    if config is None:
        if organization is None or not getattr(organization, "pk", None):
            return {}
        config = OrganizationContentConfig.objects.filter(organization_id=organization.pk).first()
    strategy = getattr(config, "pillar_strategy", None)
    state = strategy.get(FIRST_PROFILE_KEY) if isinstance(strategy, dict) else None
    return deepcopy(state) if isinstance(state, dict) else {}


def _locked_config(organization):
    """The config row, locked after the organisation row (the lock order of every strategy writer)."""
    from organizations.models import Organization
    from .models import OrganizationContentConfig
    Organization.objects.select_for_update().only("pk").get(pk=organization.pk)
    OrganizationContentConfig.objects.get_or_create(organization_id=organization.pk)
    return OrganizationContentConfig.objects.select_for_update().get(organization_id=organization.pk)


def _write_state(config, state) -> None:
    strategy = deepcopy(config.pillar_strategy) if isinstance(config.pillar_strategy, dict) else {}
    strategy[FIRST_PROFILE_KEY] = state
    config.pillar_strategy = strategy
    config.save(update_fields=["pillar_strategy", "updated_at"])


def _update_state(organization, mutate):
    """Lock, apply ``mutate(state) -> state | None`` and save; None leaves it unchanged. Returns the state."""
    with transaction.atomic():
        config = _locked_config(organization)
        current = read_state(organization, config) or _empty_state()
        updated = mutate(deepcopy(current))
        if updated is None:
            return current
        _write_state(config, updated)
        return updated


def record_create(organization, *, derived_name=None) -> dict | None:
    """Start the first-profile state for a company the one-field start just created."""
    if organization is None or not getattr(organization, "pk", None):
        return None

    def mutate(state):
        if state.get("derivedName") or state.get("appliedRunId"):
            return None
        state["derivedName"] = derived_name or None
        return state
    return _update_state(organization, mutate)


def record_dispatch(organization, run, *, meta=None) -> dict | None:
    """Remember the first-profile autofill run (and the homepage meta read before it)."""
    if organization is None or run is None:
        return None

    def mutate(state):
        if meta and not state.get("homepageMeta"):
            state["homepageMeta"] = meta
        if not state.get("appliedRunId"):
            state["runId"] = str(run.run_id)
            state["startedAt"] = timezone.now().isoformat()
        return state
    return _update_state(organization, mutate)


# ---------------------------------------------------------------------------
# Autofill request (IF-P1-07, client to backend to CF)


def first_profile_gate(profile, data, company_id):
    """Decide how the autofill view treats this request.

    Returns ``(company, refuse_no_website)``. ``company`` is the founder's own
    new-journey company when ``researchDepth: "first_profile"`` is honoured
    (flag on, new-journey org), else None. ``refuse_no_website`` is True for a
    new-journey company without a website whose request brings no usable
    website of its own, while ``MY_STARTUP_NO_WEBSITE_ARTICLES_ENABLED`` is
    also on (IF-P1-08): nothing is dispatched for it. A request that brings a
    real domain for such a company runs today's attach-and-research path. A
    legacy company always gets ``(None, False)``, so its request runs exactly
    as today.
    """
    if not one_field_start_enabled() or not company_id or profile is None:
        return None, False
    try:
        company = profile.companies.select_related("organization").filter(pk=company_id).first()
    except Exception:  # noqa: BLE001 - a malformed id is today's 404 later on
        return None, False
    organization = getattr(company, "organization", None) if company is not None else None
    if organization is None or not is_new_journey(organization):
        return None, False
    if not organization_has_website(organization):
        return None, no_website_articles_enabled() and not _request_has_website(data)
    depth = str(data.get("researchDepth") or data.get("research_depth") or "").strip()
    return (company if depth == FIRST_PROFILE_DEPTH else None), False


def _request_has_website(data) -> bool:
    """True when the autofill body carries a real website (not empty, not a ``.invalid`` placeholder)."""
    from founder_tools.services import normalize_company_domain
    domain = normalize_company_domain(data.get("domain"))
    return bool(domain) and not str(domain).lower().rstrip(".").endswith(".invalid")


def _normalised_linkedin(value) -> str:
    from founder_tools.services import normalize_company_linkedin_url
    try:
        return normalize_company_linkedin_url(value)
    except ValueError:
        return ""


def first_profile_payload(payload, *, company) -> dict:
    """The CF payload for a first-profile autofill: budgeted, draft mode, a fresh request id.

    A typed LinkedIn URL is normalised, or dropped when it cannot be.
    """
    result = dict(payload)
    fields = dict(_mapping(result.get("existing_fields")))
    if "companyLinkedInUrl" in fields:
        fields["companyLinkedInUrl"] = _normalised_linkedin(fields["companyLinkedInUrl"])
        result["existing_fields"] = fields
    if "company_linkedin_url" in result:
        result["company_linkedin_url"] = _normalised_linkedin(result["company_linkedin_url"])
    result.update({
        "research_depth": FIRST_PROFILE_DEPTH,
        "budget_seconds": BUDGET_SECONDS,
        "draft_mode": True,
        "client_request_id": f"first-profile:{company.pk}:{uuid.uuid4().hex}",
    })
    return result


def dispatch_homepage_meta(organization) -> dict | None:
    """The homepage meta to store before dispatch: the saved one, or a fresh pinned read."""
    saved = read_state(organization).get("homepageMeta")
    if isinstance(saved, dict) and saved:
        return None
    return homepage_meta(getattr(organization, "domain", ""))


# ---------------------------------------------------------------------------
# Reading a run


def _mapping(value):
    return value if isinstance(value, dict) else {}


def autofill_from_run(run) -> dict:
    """The autofill object of a run result, wherever the worker put it."""
    result = _mapping(getattr(run, "result", None))
    for source in (result.get("autofill"), _mapping(result.get("result")).get("autofill"), result):
        source = _mapping(source)
        if any(source.get(key) for key in ("profileFields", "brandName", "seedKeywords", "siteProfile", "researchQuality")):
            return source
    return {}


def _transient_failure(run) -> bool:
    result = _mapping(getattr(run, "result", None))
    failure = _mapping(result.get("failure"))
    return bool(result.get("retryable") is True or failure.get("retryable") is True
                or "transient" in (result.get("failure_class"), failure.get("failure_class")))


def site_profile_from_autofill(autofill) -> dict | None:
    """A bounded copy of CF's ``siteProfile`` (platform, blogUrl, existingPosts, logoUrl, primaryCallToAction, homepage)."""
    raw = _mapping(_mapping(autofill).get("siteProfile"))
    if not raw:
        return None
    platform = str(raw.get("platform") or "").strip().lower()
    posts = []
    for item in raw.get("existingPosts") or []:
        if not isinstance(item, dict):
            continue
        url = _https(item.get("url"))
        if url:
            posts.append({"url": url, "title": _clean(item.get("title"), 300)})
        if len(posts) >= MAX_EXISTING_POSTS:
            break
    cta = _mapping(raw.get("primaryCallToAction"))
    cta_label, cta_url = _clean(cta.get("label"), 120), _https(cta.get("url"))
    homepage = _mapping(raw.get("homepage"))
    return {
        "platform": platform if platform in SITE_PLATFORMS else "unknown",
        "blogUrl": _https(raw.get("blogUrl")),
        "existingPosts": posts,
        "logoUrl": _https(raw.get("logoUrl")),
        "primaryCallToAction": {"label": cta_label, "url": cta_url} if cta_label and cta_url else None,
        "homepage": {"title": _clean(homepage.get("title"), 200) or None,
                     "description": _clean(homepage.get("description"), 500) or None},
    }


def _seed_list(value) -> list[str]:
    seeds = []
    for item in value if isinstance(value, list) else []:
        text = item.get("keyword") if isinstance(item, dict) else item
        text = _clean(text, 120).lower()
        if text and text not in seeds:
            seeds.append(text)
    return seeds[:24]


def fallback_seed_keywords(*, name="", description="", audience="", domain="") -> list[str]:
    """Deterministic seed keywords when research gave none, so every new org has some."""
    seeds = []

    def add(value):
        value = _clean(value, 80).lower()
        if value and value not in seeds:
            seeds.append(value)
    add(name)
    for text in (description, audience):
        words = [word for word in re.findall(r"[a-z0-9][a-z0-9'&-]*", str(text or "").lower())
                 if word not in _STOPWORDS and len(word) > 2]
        for first, second in zip(words, words[1:]):
            add(f"{first} {second}")
            if len(seeds) >= 6:
                break
        if len(words) == 1:
            add(words[0])
    if not seeds:
        add(derive_company_name(domain))
    return seeds[:8]


# ---------------------------------------------------------------------------
# Applying a profile


def _company_for_run(run):
    from founder_tools.models import VibeRaisingCompany
    company_id = _mapping(getattr(run, "run_request", None)).get("company_id")
    if not company_id:
        return None
    try:
        return (VibeRaisingCompany.objects.select_related("organization", "profile", "profile__user")
                .filter(pk=company_id, organization__isnull=False).first())
    except Exception:  # noqa: BLE001 - a malformed id is no company
        return None


def _candidates(autofill, state):
    """(name, oneLiner, audience, problem) from research, then from the homepage."""
    fields = _mapping(autofill.get("profileFields"))
    meta = _mapping(state.get("homepageMeta"))
    homepage = _mapping(_mapping(state.get("siteProfile")).get("homepage"))
    research_name = _clean(autofill.get("brandName"), 120)
    research_line = _clean(fields.get("shortDescription"), 280)
    name = research_name or _clean(meta.get("ogSiteName"), 120) or _title_name(meta.get("title")) or _title_name(homepage.get("title"))
    line = research_line or _clean(meta.get("description"), 280) or _clean(homepage.get("description"), 280)
    return {
        "name": name, "shortDescription": line,
        "targetAudience": _clean(fields.get("targetAudience"), 500),
        "problemSolved": _clean(fields.get("problemSolved"), 1000),
        "fromHomepage": {"name": bool(name and not research_name), "shortDescription": bool(line and not research_line)},
    }


def _is_open(current, key, state) -> bool:
    current = _clean(current, 10_000)
    fallback = _clean(_mapping(state.get("fallbackValues")).get(key), 10_000)
    return not current or bool(fallback and current == fallback)


def _fill_fields(company, organization, config, candidates, state, *, record_fallback=False) -> list[str]:
    """Fill empty fields only (inside the caller's lock). Returns the filled field names."""
    from startup_updates.services import resolve_or_create_profile

    filled = []
    fallback_values = dict(_mapping(state.get("fallbackValues")))
    derived = _clean(state.get("derivedName"), 255)
    name = candidates["name"]
    current_name = _clean(company.name, 255)
    if name and name != current_name and (not current_name or (derived and current_name == derived)
                                          or _is_open(current_name, "name", state)):
        previous = current_name
        company.name = name[:255]
        company.save(update_fields=["name", "updated_at"])
        organization.refresh_from_db(fields=["name"])
        if _clean(organization.name, 255) in {"", previous, derived}:
            organization.name = company.name
            organization.save(update_fields=["name"])
        if _clean(config.brand_name, 255) in {"", previous, derived}:
            config.brand_name = company.name
            config.save(update_fields=["brand_name", "updated_at"])
        filled.append("name")
        if record_fallback and candidates["fromHomepage"]["name"]:
            fallback_values["name"] = company.name
    _org, startup_profile = resolve_or_create_profile(domain=organization.domain)
    profile_updates = []
    for key, column in (("shortDescription", "short_description"), ("targetAudience", "target_audience"),
                        ("problemSolved", "problem_solved")):
        value = candidates[key]
        if value and value != getattr(startup_profile, column) and _is_open(getattr(startup_profile, column), key, state):
            setattr(startup_profile, column, value)
            profile_updates.append(column)
            filled.append(key)
            if record_fallback and candidates["fromHomepage"].get(key):
                fallback_values[key] = value
    if profile_updates:
        startup_profile.save(update_fields=[*profile_updates, "updated_at"])
    if record_fallback:
        state["fallbackValues"] = fallback_values
    return filled


def _ensure_seed_keywords(organization, autofill, candidates) -> bool:
    from startup_updates.services import resolve_or_create_profile

    organization.refresh_from_db(fields=["seed_keywords"])
    if _seed_list(organization.seed_keywords):
        return False
    seeds = _seed_list(autofill.get("seedKeywords")) or fallback_seed_keywords(
        name=candidates["name"], description=candidates["shortDescription"],
        audience=candidates["targetAudience"], domain=organization.domain)
    if not seeds:
        return False
    organization.seed_keywords = seeds
    organization.save(update_fields=["seed_keywords"])
    _org, startup_profile = resolve_or_create_profile(domain=organization.domain)
    if not startup_profile.positive_keywords:
        startup_profile.positive_keywords = seeds
        startup_profile.save(update_fields=["positive_keywords", "updated_at"])
    return True


def seed_no_website_keywords(organization, *, name="", description="") -> bool:
    """Seed keywords for a new no-website startup, which never gets an autofill run.

    The one-field create calls this for a ``.invalid`` org so island refresh
    still has keywords (P1.5). Fills empty keywords only; True when it wrote.
    """
    if organization is None or organization_has_website(organization):
        return False
    return _ensure_seed_keywords(organization, {}, {
        "name": _clean(name, 255), "shortDescription": _clean(description, 500), "targetAudience": ""})


def _apply_logo(company, site_profile, state) -> bool:
    from founder_tools.profile_fields import company_avatar_url, save_company_branding
    from founder_tools.services import DomainOwnershipError

    if company_avatar_url(company):
        return False
    url = _mapping(site_profile).get("logoUrl") or _mapping(state.get("homepageMeta")).get("ogImage")
    url = _https(url)
    if not url:
        return False
    try:
        save_company_branding(company, company.profile.user, url)
    except DomainOwnershipError:
        return False
    return True


def seed_draft_audience(organization, audience) -> str | None:
    """Save one draft audience from ``targetAudience`` (never approved, never an offer).

    Only when the catalogue has no audience yet. The founder's press approves
    it later (decision 1). Returns the audience id, or None.
    """
    from .editorial_catalog import review_payload
    from .editorial_first_audience import _audience_id, _default_name, _reader_task
    from .editorial_views import mutate_catalog_response
    from .models import OrganizationContentConfig

    description = _clean(audience, 280)
    if len(description) < 3:
        return None
    config = OrganizationContentConfig.objects.filter(organization_id=organization.pk).first()
    strategy = config.pillar_strategy if config is not None and isinstance(config.pillar_strategy, dict) else {}
    try:
        current = review_payload(strategy)
    except ValueError:
        return None
    if current["audience_options"]:
        return None
    name = _default_name(description)
    option = {
        "catalog_schema_version": 2, "id": _audience_id(name, set()), "name": name, "description": description,
        "reader_task": _reader_task(description), "allow_no_offer": True,
        "status": "draft", "version": 1, "approved_by": None, "approved_at": None,
    }
    response = mutate_catalog_response({"pk": organization.pk}, {
        "audience_options": [option],
        "expected_editorial_catalog_version": current["editorial_catalog_version"],
    })
    if response.status_code != 200:
        logger.info("founder_first_profile_audience_skipped org=%s status=%s", organization.pk, response.status_code)
        return None
    return option["id"]


def start_topic_batch(company) -> str | None:
    """Start the free first topic batch (P0 IF-01) once. Never charges points.

    Skipped unless ``ARTICLES_FREE_SETUP_RESEARCH_ENABLED``. The request
    quotes 0 points, so a batch that is not free is refused, not charged.
    """
    if not getattr(settings, "ARTICLES_FREE_SETUP_RESEARCH_ENABLED", False):
        return None
    from founder_tools.services import get_founder_company_context
    from . import vibe_marketing_views as views

    user = company.profile.user
    context = get_founder_company_context(user, company_id=company.pk)
    response = views.start_article_research(user=user, context=context, data={
        "requestedTopicCount": 4, "expectedCostPoints": 0, "clientRequestId": f"first-profile-topics:{company.pk}"})
    data = getattr(response, "data", None)
    if getattr(response, "status_code", 500) >= 300 or not isinstance(data, dict):
        logger.info("founder_first_profile_topics_skipped org=%s status=%s", company.organization_id,
                    getattr(response, "status_code", None))
        return None
    return str(data.get("runId") or data.get("run_id") or "") or None


def _dispatch_retry(run, company):
    from founder_tools.services import get_founder_company_context
    from . import vibe_marketing_views as views

    payload = deepcopy(_mapping(run.run_request))
    payload["client_request_id"] = f"first-profile:{company.pk}:{uuid.uuid4().hex}"
    payload["first_profile_retry"] = True
    context = get_founder_company_context(company.profile.user, company_id=company.pk)
    config = views._get_config(context.organization)
    # Not recorded with _record_free_setup_research: the retry is not the
    # founder's use, so it never counts against the free-setup daily cap.
    return views._queue_content_factory_run(endpoint="autofill", workflow="startup_autofill",
                                            context=context, config=config, payload=payload)


def _started_at(run, state):
    started = parse_datetime(str(state.get("startedAt") or "")) if state.get("startedAt") else None
    return started or getattr(run, "created_at", None)


def apply_first_profile(run, *, now=None) -> dict:
    """Turn a terminal first-profile autofill run into the startup's profile, once per organisation.

    Fills empty fields only: the name (only while it still equals the name
    derived from the domain), the one-liner, who it helps, the problem, the
    logo and ``Organization.seed_keywords``. Stores ``siteProfile``. Never
    writes ``article_system`` or ``tech_stack``. A failed or fallback run
    still gets a name and one-liner from the homepage. A transient failure
    is retried once. Returns ``{"status": ...}``.
    """
    now = now or timezone.now()
    if not one_field_start_enabled():
        return {"status": "disabled"}
    request = _mapping(getattr(run, "run_request", None))
    if getattr(run, "workflow", "") != "startup_autofill" or request.get("research_depth") != FIRST_PROFILE_DEPTH:
        return {"status": "skipped"}
    company = _company_for_run(run)
    organization = getattr(company, "organization", None) if company is not None else None
    if organization is None or not is_new_journey(organization):
        return {"status": "skipped"}
    state = read_state(organization)
    if state.get("appliedRunId"):
        return {"status": "already_applied"}
    if state.get("runId") and state["runId"] != run.run_id:
        return {"status": "superseded"}
    started = _started_at(run, state)
    stale = run.status in _RUNNING and started is not None and started <= now - STALE_RUN_AFTER
    if run.status not in _TERMINAL and not stale:
        return {"status": "pending"}
    autofill = autofill_from_run(run)
    usable = run.status == _COMPLETED or bool(_mapping(autofill.get("profileFields")) or autofill.get("brandName"))
    if not usable and not stale and _transient_failure(run) and int(state.get("retryCount") or 0) < 1:
        return _retry(run, company, organization)
    return _final_apply(run, company, organization, autofill, stale=stale)


def _retry(run, company, organization) -> dict:
    claimed = {}

    def claim(state):
        if (state.get("appliedRunId") or int(state.get("retryCount") or 0) >= 1
                or (state.get("runId") or run.run_id) != run.run_id):
            return None
        from .models import OrganizationContentConfig
        config = OrganizationContentConfig.objects.get(organization_id=organization.pk)
        # The founder sees a name and a one-liner while the retry runs.
        _fill_fields(company, organization, config, _candidates({}, state), state, record_fallback=True)
        state["retryCount"] = 1
        claimed["ok"] = True
        return state
    _update_state(organization, claim)
    if not claimed:
        return {"status": "already_applied"}
    try:
        retry_run = _dispatch_retry(run, company)
    except Exception:  # noqa: BLE001 - the sweep finishes the profile from the failed run
        logger.warning("founder_first_profile_retry_failed org=%s run=%s", organization.pk, run.run_id, exc_info=True)
        return _final_apply(run, company, organization, autofill_from_run(run), stale=False)
    retry_id = str(getattr(retry_run, "run_id", "") or "")

    def record(state):
        if state.get("appliedRunId"):
            return None
        state["runId"] = retry_id or state.get("runId")
        state["startedAt"] = timezone.now().isoformat()
        return state
    _update_state(organization, record)
    logger.info("founder_first_profile_retried org=%s run=%s retry_run=%s", organization.pk, run.run_id, retry_id)
    return {"status": "retried", "runId": retry_id}


def _final_apply(run, company, organization, autofill, *, stale) -> dict:
    site_profile = site_profile_from_autofill(autofill)
    outcome = {}

    def claim(state):
        if state.get("appliedRunId"):
            return None
        from .models import OrganizationContentConfig
        config = OrganizationContentConfig.objects.get(organization_id=organization.pk)
        if site_profile is not None:
            state["siteProfile"] = site_profile
        candidates = _candidates(autofill, state)
        outcome["filled"] = _fill_fields(company, organization, config, candidates, state)
        outcome["candidates"] = candidates
        outcome["seeded"] = _ensure_seed_keywords(organization, autofill, candidates)
        state["appliedRunId"] = str(run.run_id)
        state["appliedAt"] = timezone.now().isoformat()
        outcome["state"] = state
        return state
    _update_state(organization, claim)
    if "state" not in outcome:
        return {"status": "already_applied"}
    state = outcome["state"]
    quality = str(_mapping(autofill.get("researchQuality")).get("status") or "")
    fallback = bool(stale or run.status != _COMPLETED or quality == "fallback" or not autofill)
    company.refresh_from_db()
    logo = False
    try:
        logo = _apply_logo(company, site_profile, state)
    except Exception:  # noqa: BLE001 - a logo never blocks the profile
        logger.warning("founder_first_profile_logo_failed org=%s", organization.pk, exc_info=True)
    audience_id = None
    try:
        audience_id = seed_draft_audience(organization, outcome["candidates"]["targetAudience"])
    except Exception:  # noqa: BLE001
        logger.warning("founder_first_profile_audience_failed org=%s", organization.pk, exc_info=True)
    topic_claim = {}

    def claim_topics(current):
        if current.get("topicBatchAttemptedAt") or current.get("topicBatchRunId"):
            return None
        current["topicBatchAttemptedAt"] = timezone.now().isoformat()
        if audience_id:
            current["draftAudienceId"] = audience_id
        topic_claim["ok"] = True
        return current
    _update_state(organization, claim_topics)
    topic_run_id = None
    if topic_claim:
        try:
            topic_run_id = start_topic_batch(company)
        except Exception:  # noqa: BLE001
            logger.warning("founder_first_profile_topics_failed org=%s", organization.pk, exc_info=True)
        if topic_run_id:
            def record_topics(current):
                current["topicBatchRunId"] = topic_run_id
                return current
            _update_state(organization, record_topics)
    logger.info("founder_first_profile_applied org=%s run=%s fallback=%s", organization.pk, run.run_id, fallback)
    return {"status": "applied", "fallback": fallback, "filled": outcome["filled"], "logo": logo,
            "seedKeywords": outcome["seeded"], "audienceId": audience_id, "topicBatchRunId": topic_run_id}


def apply_on_poll(run) -> None:
    """The run read path's hook: apply a terminal first-profile run, never failing the poll."""
    if not one_field_start_enabled() or getattr(run, "workflow", "") != "startup_autofill":
        return
    if _mapping(getattr(run, "run_request", None)).get("research_depth") != FIRST_PROFILE_DEPTH:
        return
    try:
        apply_first_profile(run)
    except Exception:  # noqa: BLE001 - the founder's poll must still answer
        logger.warning("founder_first_profile_poll_apply_failed run=%s", getattr(run, "run_id", ""), exc_info=True)


# ---------------------------------------------------------------------------
# Minute sweep (IF-P1-21)


def _refresh_from_worker(run):
    from . import vibe_marketing_views as views
    try:
        remote = views._call_content_factory_run_status(run.run_id, workflow=run.workflow)
        if isinstance(remote, dict) and remote and not views._is_status_poll_unavailable_payload(remote):
            return views._sync_local_run_from_remote(run, remote)
    except Exception:  # noqa: BLE001 - a later tick tries again
        logger.info("founder_first_profile_sweep_refresh_failed run=%s", run.run_id)
    return run


def sweep(now=None) -> dict:
    """Finish first profiles whose founder stopped polling. Called once a minute by the scheduler."""
    from workflow_runs.models import ContentFactoryRun
    from .models import OrganizationContentConfig

    now = now or timezone.now()
    if not one_field_start_enabled():
        return {"status": "disabled"}
    applied = retried = checked = 0
    # A run started inside the window wrote its state (and bumped updated_at)
    # inside it too, so older configs are never read.
    configs = (OrganizationContentConfig.objects
               .filter(pillar_strategy__has_key=FIRST_PROFILE_KEY, updated_at__gte=now - SWEEP_WINDOW)
               .select_related("organization").order_by("-updated_at")[:SWEEP_SCAN_LIMIT])
    for config in configs.iterator():
        if checked >= SWEEP_LIMIT:
            break
        state = read_state(config.organization, config)
        run_id = state.get("runId")
        if not run_id or state.get("appliedRunId"):
            continue
        started = parse_datetime(str(state.get("startedAt") or "")) if state.get("startedAt") else None
        if started is not None and started < now - SWEEP_WINDOW:
            continue
        if not is_new_journey(config.organization, config):
            continue
        run = ContentFactoryRun.objects.filter(run_id=run_id, workflow="startup_autofill").first()
        if run is None:
            continue
        checked += 1
        if run.status in _RUNNING:
            run = _refresh_from_worker(run)
        result = apply_first_profile(run, now=now)
        applied += result.get("status") == "applied"
        retried += result.get("status") == "retried"
    return {"status": "completed", "checked": checked, "applied": applied, "retried": retried}
