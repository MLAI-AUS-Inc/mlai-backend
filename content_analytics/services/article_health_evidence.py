"""Bounded, read-only article health evidence from existing research snapshots.

This module does not fetch competitor pages or estimate their private analytics.
Only explicit dated SERP observations can supply positions; legacy URL lists
remain useful references with unknown rank, age and search intent.
"""
from __future__ import annotations

from datetime import date, datetime
import math
from urllib.parse import urlsplit, urlunsplit


MAX_COMPETITORS = 20


def _record(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _value(mapping, *keys):
    return next((mapping[key] for key in keys if mapping.get(key) is not None), None)


def _date(value, as_of):
    if isinstance(value, (date, datetime)):
        value = value.isoformat()
    if not isinstance(value, str):
        return None
    try:
        observed = date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        return observed.isoformat() if observed <= as_of else None
    except ValueError:
        return None


def _position(value):
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
        return int(number) if math.isfinite(number) and number.is_integer() and 1 <= number <= 1000 else None
    except (TypeError, ValueError):
        return None


def _observed(value, fallback, as_of):
    return _date(value, as_of) if value is not None else fallback


def _locale(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return str(value).strip().casefold() or None


def _same_locale(row, bundle):
    return all(_locale(row.get(key, bundle.get(key))) == _locale(bundle.get(key))
               for key in ("locationCode", "languageCode", "device"))


def _content_date(row, key, observed_at, as_of):
    value = _date(row.get(key), as_of)
    checked = _observed(row.get("dateObservedAt"), observed_at, as_of)
    return value if value and observed_at and checked and value <= observed_at and value <= checked else None


def _url(value):
    raw = _text(value)
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        # Accessing the parsed port validates malformed netlocs without I/O.
        _ = parsed.port
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
    except ValueError:
        return None


def _host(url):
    return (urlsplit(url).hostname or "").removeprefix("www.") if url else ""


def _query(value):
    return " ".join(str(value or "").split()).casefold()


def _url_key(value):
    url = _url(value)
    if not url:
        return None
    parts = urlsplit(url)
    return (_host(url), parts.port or (443 if parts.scheme == "https" else 80), parts.path.rstrip("/"), parts.query)


def research_evidence_bundles(result):
    """Read explicit health observations without traversing arbitrary run JSON."""
    result = _record(result)
    raw = _value(result, "articleHealthEvidence", "article_health_evidence")
    if isinstance(raw, list):
        return [_record(row) for row in raw[:200] if isinstance(row, dict)]
    if isinstance(raw, dict):
        if "competitors" in raw:
            return [raw]
        return [_record(row) for row in list(raw.values())[:200] if isinstance(row, dict)]
    return []


def build_article_health_evidence(article, *, keywords=(), observations=(), as_of):
    """Attach same-topic, tenant-scoped research supplied by the report loader.

    ``keywords`` and ``observations`` must already belong to the article's
    organization. An explicit article URL/ID takes precedence over a query match.
    """
    primary = _query(article.primary_keyword)
    own_url = _url(article.canonical_url or article.live_url)
    own_host = _host(own_url)
    evidence = {
        # The legacy model's published_at is also assigned during draft/PR
        # creation. Only explicit own-page metadata proves publication age.
        "publishedAt": None,
        # This is set once on the first verified LIVE transition. It establishes
        # a minimum age, never that a recently discovered article is new.
        "firstKnownLiveAt": _date(getattr(article, "live_verified_at", None), as_of),
        # Metadata write times and publish checks do not prove a content update.
        "updatedAt": None,
        "primaryQuery": _text(article.primary_keyword),
        "searchLocationCode": None,
        "searchLanguageCode": None,
        "searchDevice": None,
        "competitors": [],
    }
    candidates = []
    locale_observed_at = ""
    for bundle in observations:
        bundle = _record(bundle)
        query = _text(_value(bundle, "query", "keyword"))
        article_id = _text(_value(bundle, "articleId", "article_id"))
        article_url = _url(_value(bundle, "articleUrl", "article_url"))
        if article_id and article_id != str(article.id):
            continue
        if article_url and _url_key(article_url) != _url_key(own_url):
            continue
        if not article_id and not article_url and (not query or _query(query) != primary):
            continue
        observed_at = _date(_value(bundle, "observedAt", "observed_at"), as_of)
        locale = {key: bundle.get(key) for key in ("locationCode", "languageCode", "device")}
        if observed_at and observed_at > locale_observed_at and all(_locale(value) for value in locale.values()):
            evidence.update(
                searchLocationCode=locale["locationCode"],
                searchLanguageCode=locale["languageCode"], searchDevice=locale["device"],
            )
            locale_observed_at = observed_at
        if article_url == own_url and own_url:
            updated = _content_date({**bundle, "updatedAt": _value(bundle, "updatedAt", "updated_at")}, "updatedAt", observed_at, as_of)
            if observed_at and updated and updated <= observed_at:
                evidence["updatedAt"] = max(evidence["updatedAt"] or updated, updated)
        own_rankings = _value(bundle, "ownRankings", "own_rankings")
        own_position = None
        if isinstance(own_rankings, list) and own_url and observed_at:
            for ranking in own_rankings[:100]:
                ranking = _record(ranking)
                ranking_date = _observed(_value(ranking, "observedAt", "observed_at"), observed_at, as_of)
                ranking_query = _value(ranking, "query", "keyword") or query
                if (_url_key(ranking.get("url")) == _url_key(own_url)
                        and ranking_date == observed_at and _query(ranking_query) == _query(query)
                        and _same_locale(ranking, locale)):
                    own_position = _position(ranking.get("position"))
                    for key in ("publishedAt", "updatedAt"):
                        value = _content_date(ranking, key, observed_at, as_of)
                        if value:
                            if key == "publishedAt":
                                evidence[key] = evidence[key] or value
                            else:
                                evidence[key] = max(evidence[key] or value, value)
                    break
        rows = bundle.get("competitors")
        if isinstance(rows, list):
            candidates.extend((row, query, observed_at, own_position, locale) for row in rows[:MAX_COMPETITORS])
    for keyword in keywords:
        rows = keyword.get("competitor_urls")
        if isinstance(rows, list):
            candidates.extend((row, _text(keyword.get("keyword")), None, None, {}) for row in rows[:MAX_COMPETITORS])

    by_source = {}
    for raw, query, observed_at, own_position, locale in candidates:
        row = {"url": raw} if isinstance(raw, str) else _record(raw)
        url = _url(row.get("url"))
        if not url or _host(url) == own_host:
            continue
        row_query = _text(_value(row, "query", "keyword")) or query
        row_date = _observed(_value(row, "observedAt", "observed_at"), observed_at, as_of)
        if (_query(row_query) != _query(query) or row_date != observed_at
                or not _same_locale(row, locale)):
            own_position = None
        query, observed_at = row_query, row_date
        clean = {
            "url": url,
            "title": _text(row.get("title")),
            "query": query,
            "position": _position(_value(row, "position", "rank")) if observed_at else None,
            "ownPosition": (_position(_value(row, "ownPosition", "own_position")) or own_position) if observed_at else None,
            "publishedAt": _content_date({**row, "publishedAt": _value(row, "publishedAt", "published_at")}, "publishedAt", observed_at, as_of),
            "updatedAt": _content_date({**row, "updatedAt": _value(row, "updatedAt", "updated_at")}, "updatedAt", observed_at, as_of),
            "observedAt": observed_at,
            "dateObservedAt": _observed(row.get("dateObservedAt"), observed_at, as_of),
            "intentMatch": _value(row, "intentMatch", "intent_match") is True,
            **{key: row.get(key, locale.get(key)) for key in ("locationCode", "languageCode", "device")},
        }
        key = (url, _query(query), *(_locale(clean[field]) for field in ("locationCode", "languageCode", "device")))
        if not observed_at and any(source[:2] == key[:2] for source in by_source):
            continue
        prior = by_source.get(key)
        if prior is None or (clean["observedAt"] or "") > (prior["observedAt"] or ""):
            by_source[key] = clean
    evidence["competitors"] = sorted(
        by_source.values(), key=lambda row: (row["observedAt"] or "", row["url"]), reverse=True,
    )[:MAX_COMPETITORS]
    return evidence
