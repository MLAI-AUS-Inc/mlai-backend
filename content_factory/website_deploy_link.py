"""The deploy link: merging reaches the founder's live site (plan C14).

Two checks share this module, both reading what the existing live checks
already record and neither writing anything new to the founder's repository:

* **At setup.** After the setup pull request merges, the ready-on-merge watch
  (``website_live_status``) polls the public articles page for the reviewed
  marker. The watch records the outcome on its own receipt as the site's
  deploy link (``deploy_link``: ``proven`` once the marker shows, ``not_live``
  once :data:`ASK_AFTER` passes without it). Past that bound the founder gets
  one named ask, ``deploy_site``, whose copy is chosen by host class
  (:func:`deploy_ask`): "Press Publish in Lovable", "Link this repository in
  Netlify", and so on.
* **After each article merge.** ``article_publish_status._observe_live_article``
  compares the saved article with the public page. While the article is merged
  but not served, it is not counted as published (``article_bucket``); after
  :data:`ARTICLE_ALERT_AFTER` staff get one ``founder_stuck`` alert for that
  article and merge, and the founder sees the same host's deploy step as advice.

The live site is optional input (plan rule): nothing here blocks a draft,
the writing of an article, or a review. Every fetch still goes through the
SSRF-pinned fetchers and the exact merged-commit and saved-content identities
of the existing checks; nothing here loosens them.

Everything here applies only while ``ARTICLES_LIVE_URL_VERIFICATION_ENABLED``
is on (the plan's flag for C14; code default off). With it off the founder
journey keeps retiring ``deploy_site`` and an article on the default branch
still counts as published.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

# How long a merged change may take to reach the live site before the founder
# is asked to deploy it (setup) or staff are alerted (an article). Hosts that
# deploy on push finish in minutes; the host help already says "an hour".
ASK_AFTER = timedelta(hours=1)
ARTICLE_ALERT_AFTER = timedelta(hours=1)
# Merged articles older than this are history: the sweep stops checking them.
ARTICLE_WATCH_WINDOW = timedelta(days=14)
ACTION_ID = "deploy_site"
ARTICLE_ALERT_KIND = "article_not_live"
ARTICLE_ALERT_STEP = "article-live"
NOT_LIVE_CODE = "live_route_unavailable"

# Host classes the deploy ask has its own copy for. ``unknown`` is the honest
# fallback, never a guess.
HOST_CLASSES = ("lovable", "vercel", "netlify", "cloudflare", "github_pages", "unknown")
HOST_NAMES = {"lovable": "Lovable", "vercel": "Vercel", "netlify": "Netlify", "cloudflare": "Cloudflare Pages",
              "github_pages": "GitHub Pages"}
# Founder copy for the deploy ask (headline, body, button). No em or en dashes.
DEPLOY_ASK_COPY = {
    "lovable": ("Press Publish in Lovable",
                "Your change is merged on GitHub, but Lovable only updates your live site when you publish. "
                "Open your project in Lovable and press Publish.",
                "How to publish"),
    "vercel": ("Link this repository in Vercel",
               "Your change is merged on GitHub, but your live site hasn't updated. "
               "Check your Vercel project is connected to this repository and deploys its main branch.",
               "How to link it"),
    "netlify": ("Link this repository in Netlify",
                "Your change is merged on GitHub, but your live site hasn't updated. "
                "Check your Netlify site is linked to this repository and deploys its main branch.",
                "How to link it"),
    "cloudflare": ("Link this repository in Cloudflare Pages",
                   "Your change is merged on GitHub, but your live site hasn't updated. "
                   "Check your Cloudflare Pages project is connected to this repository.",
                   "How to link it"),
    "github_pages": ("Turn on GitHub Pages for this repository",
                     "Your change is merged on GitHub, but your live site hasn't updated. "
                     "Check GitHub Pages is turned on and builds from this repository's main branch.",
                     "How to turn it on"),
    "unknown": ("Your live site hasn't updated yet",
                "Your change is merged on GitHub, but your website hasn't picked it up. "
                "Check your hosting deploys from this GitHub repository.",
                "How to fix this"),
}
# Where each host's own deploy settings live: fixed MLAI-chosen links, never a
# value read from the founder's site or repository.
HOST_HELP_URLS = {"lovable": "https://lovable.dev/projects", "vercel": "https://vercel.com/dashboard",
                  "netlify": "https://app.netlify.com/", "cloudflare": "https://dash.cloudflare.com/"}
# A second sentence for the founder once staff have been told.
STAFF_TOLD = " Our team has been told and will help."
# Lovable serves projects from these suffixes; a domain on one is Lovable's.
LOVABLE_DOMAINS = ("lovable.app", "lovableproject.com")


def enabled() -> bool:
    """True while ``ARTICLES_LIVE_URL_VERIFICATION_ENABLED`` is on (code default off)."""
    from .website_live_verification import flag_enabled
    return flag_enabled()


def _stamp(value):
    if isinstance(value, datetime):
        return value if not timezone.is_naive(value) else None
    try:
        stamp = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return stamp if not timezone.is_naive(stamp) else None


def _domain_is_lovable(domain) -> bool:
    text = str(domain or "").strip().lower().split("/", 1)[0].split(":", 1)[0].rstrip(".")
    return any(text == suffix or text.endswith("." + suffix) for suffix in LOVABLE_DOMAINS)


def builder_hint(connection) -> str | None:
    """The no-code builder a current site fingerprint names (``lovable``), else ``None``.

    Content Factory records ``fields.hosting.value.builder`` when the
    repository's manifest carries the builder's own packages. Only a validated,
    current-source fingerprint is read (``website_site_fingerprint``).
    """
    from .website_site_fingerprint import for_connection
    source = getattr(connection, "verified_sha", None)
    if connection is None or not source:
        return None
    try:
        site = for_connection(connection, source)
    except Exception:  # noqa: BLE001 - a hint, never a gate
        logger.info("website_deploy_link_fingerprint_unreadable connection=%s", getattr(connection, "pk", None))
        return None
    value = ((site or {}).get("fields") or {}).get("hosting", {}).get("value")
    builder = value.get("builder") if isinstance(value, dict) else None
    return builder if builder == "lovable" else None


def host_class(connection, *, provider=None) -> str:
    """The host class the deploy ask speaks to: a builder first, then the host.

    A builder that publishes by hand (Lovable) is named from the site
    fingerprint or the public domain; otherwise the host the watch observed
    (``provider``) or the current deployment fingerprint names. Never a guess:
    anything else is ``unknown``.
    """
    organization = getattr(connection, "organization", None)
    if builder_hint(connection) == "lovable" or _domain_is_lovable(getattr(organization, "domain", "")):
        return "lovable"
    if provider in HOST_CLASSES and provider != "unknown":
        return provider
    from .website_live_status import saved_host
    saved = saved_host(connection) if connection is not None else "unknown"
    return saved if saved in HOST_CLASSES else "unknown"


def deploy_ask(host, *, staff_alerted=False) -> dict:
    """The ``deploy_site`` ask for ``host``: id, host class and its exact copy."""
    host = host if host in DEPLOY_ASK_COPY else "unknown"
    headline, body, label = DEPLOY_ASK_COPY[host]
    return {"id": ACTION_ID, "hostClass": host, "hostName": HOST_NAMES.get(host), "headline": headline,
            "body": body + (STAFF_TOLD if staff_alerted else ""), "label": label, "url": HOST_HELP_URLS.get(host)}


def setup_ask_due(watch, *, now=None) -> bool:
    """Whether a setup live watch has waited past :data:`ASK_AFTER` without the marker."""
    if not enabled() or not watch or watch.get("state") not in {"pending", "needs_attention"}:
        return False
    started = _stamp(watch.get("startedAt"))
    return bool(started and (now or timezone.now()) - started >= ASK_AFTER)


def merged_at(article):
    """When ``article`` reached the default branch, from the authoritative facts only."""
    return article.on_main_verified_at or article.pr_merged_at


def article_live_state(article, observation=None, *, now=None) -> dict | None:
    """The ``article_live`` state of one written article, or ``None`` before it merges.

    ``live`` once the saved content was observed on the public page (or the
    row is already ``live``); ``checking`` while merged within
    :data:`ARTICLE_ALERT_AFTER`; ``not_live`` after that. Only a merge into the
    default branch starts the clock, so an open pull request or a draft never
    has a live state and is never blocked by one.
    """
    from .models import ArticlePublishStatus
    if article.publish_status == ArticlePublishStatus.LIVE:
        return {"state": "live", "since": article.live_verified_at.isoformat() if article.live_verified_at else None,
                "url": article.live_url or None}
    merged = merged_at(article)
    if not merged or article.publish_status not in {ArticlePublishStatus.MERGED, ArticlePublishStatus.PR_OPEN,
                                                     ArticlePublishStatus.WRITTEN}:
        return None
    now = now or timezone.now()
    observation = observation if isinstance(observation, dict) else {}
    waited = now - merged
    state = "checking" if waited < ARTICLE_ALERT_AFTER else "not_live"
    value = {"state": state, "since": merged.isoformat(), "checkedAt": observation.get("checked_at")}
    reason = str(observation.get("reason") or "")
    if state == "not_live" and (reason or observation.get("state") == "content_mismatch"):
        # A coarse, founder-safe reason; the raw observation never leaves the server.
        value["reason"] = ("content_mismatch" if observation.get("state") == "content_mismatch"
                           else "not_found" if "HTTP 404" in reason else "unavailable")
    return value


def observe_article(article, observation, *, now=None) -> int:
    """Alert staff once when a merged article is still not served past the bound.

    One ``founder_stuck`` alert per article and merge commit (the alert slot),
    so a re-check never pages again for the same merge. Returns how many
    alerts were queued. Never raises for the founder's site.
    """
    if not enabled():
        return 0
    state = article_live_state(article, observation, now=now)
    if not state or state["state"] != "not_live":
        return 0
    try:
        from .founder_stuck_alerts import queue_article_not_live_alert
        return queue_article_not_live_alert(article, observation=observation, since=merged_at(article), now=now)
    except Exception:  # noqa: BLE001 - an alert failure never stops the refresh
        logger.warning("website_deploy_link_article_alert_failed article=%s", article.pk, exc_info=True)
        return 0


SWEEP_INTERVAL_SECONDS = 600
SWEEP_ORGANIZATIONS = 10
THROTTLE_KEY = "website_deploy_link:last_article_sweep"


def run_article_live_sweep(now=None, limit=None) -> dict:
    """Re-check merged articles that are not live yet, then alert staff past the bound.

    At most one pass every ten minutes and :data:`SWEEP_ORGANIZATIONS`
    organisations a pass, each through the bounded, throttled
    ``refresh_publish_statuses`` (one PR read and one pinned page fetch per
    article). Only articles merged within :data:`ARTICLE_WATCH_WINDOW` are read.
    """
    if not enabled():
        return {"status": "disabled"}
    from django.core.cache import cache
    now = now or timezone.now()
    if not cache.add(THROTTLE_KEY, now.isoformat(), SWEEP_INTERVAL_SECONDS):
        return {"status": "throttled"}
    from django.db.models import F, Q
    from .models import ArticlePublishStatus, OrganizationContentConfig, WrittenArticle
    from .article_publish_status import REFRESH_INTERVAL, refresh_publish_statuses
    since = now - ARTICLE_WATCH_WINDOW
    # The least recently checked first, and only those due a re-check, so a
    # handful of sites that never deploy cannot starve every other website.
    rows = (WrittenArticle.objects.exclude(publish_status=ArticlePublishStatus.LIVE)
            .filter(Q(on_main_verified_at__gte=since) | Q(on_main_verified_at__isnull=True, pr_merged_at__gte=since))
            .filter(Q(live_checked_at__isnull=True) | Q(live_checked_at__lte=now - REFRESH_INTERVAL))
            .order_by(F("live_checked_at").asc(nulls_first=True), "pk")
            .values_list("organization_id", "source_run_id"))
    by_org = {}
    for organization_id, run_id in rows[:500]:
        by_org.setdefault(organization_id, set()).add(run_id)
    counts = {"organizations": 0, "checked": 0, "live": 0, "alerted": 0, "errors": 0}
    for organization_id in list(by_org)[:limit or SWEEP_ORGANIZATIONS]:
        config = OrganizationContentConfig.objects.select_related("organization").filter(
            organization_id=organization_id).first()
        if config is None:
            continue
        counts["organizations"] += 1
        try:
            refreshed = refresh_publish_statuses(config.organization, config, limit=6,
                                                 source_run_ids=[run for run in by_org[organization_id] if run])
        except Exception:  # noqa: BLE001 - one website never stops the next
            logger.warning("website_deploy_link_sweep_failed org=%s", organization_id, exc_info=True)
            counts["errors"] += 1
            continue
        for article in refreshed:
            counts["checked"] += 1
            counts["live"] += article.publish_status == ArticlePublishStatus.LIVE
            counts["alerted"] += getattr(article, "_live_alerts_queued", 0)
    return {"status": "completed", **counts}
