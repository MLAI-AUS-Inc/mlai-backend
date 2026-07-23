from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone as datetime_timezone
from typing import Any
from urllib.parse import unquote, urlsplit

from django.conf import settings
from django.core.cache import cache

from integrations import http_client


class UmamiConfigurationError(RuntimeError):
    pass


class UmamiAPIError(RuntimeError):
    pass


def normalize_domain(value: str) -> str:
    text = str(value or "").strip().lower()
    parsed = urlsplit(text if "://" in text else f"//{text}")
    host = parsed.hostname or parsed.path.split("/", 1)[0]
    return host.removeprefix("www.").rstrip(".")


def normalize_path(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text if "://" in text else f"//placeholder{text if text.startswith('/') else '/' + text}")
    path = parsed.path or "/"
    # Umami v3.2 applies JavaScript decodeURI before storing url_path. Match it
    # for international slugs while retaining percent-encoded URI delimiters
    # such as %2F and %3F, which decodeURI intentionally preserves.
    protected = re.sub(
        r"%(?=(?:3B|2C|2F|3F|3A|40|26|3D|2B|24|23))",
        "%25",
        path,
        flags=re.IGNORECASE,
    )
    path = unquote(protected)
    if not path.startswith("/"):
        path = f"/{path}"
    return path.rstrip("/") or "/"


def _day_window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=datetime_timezone.utc)
    end = datetime.combine(day, time.max, tzinfo=datetime_timezone.utc)
    return start, end


def _day_window_ms(day: date) -> tuple[int, int]:
    start, end = _day_window(day)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _integer(value: Any) -> int:
    try:
        return max(int(float(value or 0)), 0)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class UmamiArticleDay:
    stats: dict[str, int]
    milestones: dict[str, int]
    referrers: list[dict[str, Any]]
    source_attribution_complete: bool


class UmamiClient:
    """Small client for the Umami v3 management and aggregate statistics APIs."""

    def __init__(self):
        self.base_url = str(getattr(settings, "UMAMI_BASE_URL", "") or "").strip().rstrip("/")
        self.api_token = str(getattr(settings, "UMAMI_API_TOKEN", "") or "").strip()
        self.username = str(getattr(settings, "UMAMI_USERNAME", "") or "").strip()
        self.password = str(getattr(settings, "UMAMI_PASSWORD", "") or "").strip()
        self._token = self.api_token
        if not self.base_url:
            raise UmamiConfigurationError("UMAMI_BASE_URL is not configured.")
        if not self._token and not (self.username and self.password):
            raise UmamiConfigurationError("Configure UMAMI_API_TOKEN or UMAMI_USERNAME and UMAMI_PASSWORD.")

    def _token_cache_key(self) -> str:
        return f"content-analytics:umami-token:{hash((self.base_url, self.username))}"

    def _authenticate(self, *, force: bool = False) -> str:
        if self.api_token:
            self._token = self.api_token
            return self._token
        if not force:
            cached = cache.get(self._token_cache_key())
            if cached:
                self._token = str(cached)
                return self._token
        response = http_client.post(
            f"{self.base_url}/api/auth/login",
            json={"username": self.username, "password": self.password},
            timeout=(3, 15),
        )
        if response.status_code >= 400:
            raise UmamiAPIError(f"Umami authentication failed with HTTP {response.status_code}.")
        payload = response.json() if response.content else {}
        token = str(payload.get("token") or "").strip()
        if not token:
            raise UmamiAPIError("Umami authentication did not return a token.")
        self._token = token
        cache.set(self._token_cache_key(), token, 20 * 60)
        return token

    def _request(self, method: str, path: str, *, retry_auth: bool = True, **kwargs):
        token = self._token or self._authenticate()
        headers = {**dict(kwargs.pop("headers", {}) or {}), "Authorization": f"Bearer {token}", "Accept": "application/json"}
        response = http_client.request(
            method,
            f"{self.base_url}{path}",
            headers=headers,
            timeout=kwargs.pop("timeout", (3, 20)),
            **kwargs,
        )
        if response.status_code == 401 and retry_auth and not self.api_token:
            self._authenticate(force=True)
            return self._request(method, path, retry_auth=False, **kwargs)
        if response.status_code >= 400:
            detail = ""
            try:
                detail = str((response.json() or {}).get("message") or (response.json() or {}).get("error") or "")
            except Exception:
                detail = ""
            raise UmamiAPIError(
                f"Umami {method.upper()} {path} failed with HTTP {response.status_code}"
                + (f": {detail}" if detail else ".")
            )
        return response.json() if response.content else {}

    def find_website(self, domain: str) -> dict[str, Any] | None:
        normalized = normalize_domain(domain)
        payload = self._request(
            "GET",
            "/api/websites",
            params={"includeTeams": "true", "search": normalized, "page": 1, "pageSize": 100},
        )
        rows = payload.get("data") if isinstance(payload, dict) else payload
        for row in rows or []:
            if normalize_domain(row.get("domain")) == normalized:
                return row
        return None

    def ensure_website(self, *, name: str, domain: str, team_id: str = "") -> dict[str, Any]:
        existing = self.find_website(domain)
        if not existing:
            body = {"name": str(name or domain).strip(), "domain": normalize_domain(domain)}
            if team_id:
                body["teamId"] = team_id
            existing = self._request("POST", "/api/websites", json=body)
        website_id = str(existing.get("id") or "").strip()
        if not website_id:
            raise UmamiAPIError("Umami website response did not contain an id.")
        # Replay/heatmaps are explicitly disabled even for an existing site. Site
        # creation does not accept this configuration in Umami v3.
        updated = self._request(
            "POST",
            f"/api/websites/{website_id}",
            json={
                "name": str(name or domain).strip(),
                "domain": normalize_domain(domain),
                "replayEnabled": False,
                "replayConfig": {
                    "replayEnabled": False,
                    "heatmapEnabled": False,
                    "sampleRate": 0,
                    "heatmapSampleRate": 0,
                    "maskLevel": "strict",
                    "blockSelector": "",
                },
            },
        )
        return updated or existing

    def _stats(
        self,
        website_id: str,
        *,
        start_at: int,
        end_at: int,
        path: str,
        referrer: str = "",
        utm_source: str = "",
    ) -> dict[str, Any]:
        params = {"startAt": start_at, "endAt": end_at, "path": path}
        if referrer:
            params["referrer"] = referrer
        if utm_source:
            params["utmSource"] = utm_source
        return self._request(
            "GET",
            f"/api/websites/{website_id}/stats",
            params=params,
        )

    def _event_unique_visits(
        self,
        website_id: str,
        *,
        start_at: int,
        end_at: int,
        path: str,
        event: str,
        referrer: str = "",
        utm_source: str = "",
    ) -> int:
        params = {"startAt": start_at, "endAt": end_at, "path": path, "event": event}
        if referrer:
            params["referrer"] = referrer
        if utm_source:
            params["utmSource"] = utm_source
        payload = self._request(
            "GET",
            f"/api/websites/{website_id}/events/stats",
            params=params,
        )
        data = payload.get("data") if isinstance(payload, dict) else {}
        return _integer((data or {}).get("visits"))

    def _event_visits_by_name(
        self,
        website_id: str,
        *,
        start_at: int,
        end_at: int,
        path: str,
    ) -> dict[str, int]:
        """Fetch every custom-event unique-visit count in one Umami query."""

        payload = self._request(
            "GET",
            f"/api/websites/{website_id}/metrics/expanded",
            params={
                "startAt": start_at,
                "endAt": end_at,
                "path": path,
                "type": "event",
                "limit": 100,
                "offset": 0,
            },
        )
        rows = payload if isinstance(payload, list) else list(payload.get("data") or [])
        return {
            str(row.get("name") or "").strip(): _integer(row.get("visits"))
            for row in rows
            if isinstance(row, dict) and str(row.get("name") or "").strip()
        }

    def _referrers(self, website_id: str, *, start_at: int, end_at: int, path: str) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            f"/api/websites/{website_id}/metrics/expanded",
            params={
                "startAt": start_at,
                "endAt": end_at,
                "path": path,
                "type": "referrer",
                "limit": 500,
                "offset": 0,
            },
        )
        return payload if isinstance(payload, list) else list(payload.get("data") or [])

    def _utm_sources(self, website_id: str, *, day: date, path: str) -> list[dict[str, Any]]:
        start, end = _day_window(day)
        payload = self._request(
            "POST",
            "/api/reports/utm",
            json={
                "websiteId": website_id,
                "type": "utm",
                "filters": {"path": path},
                "parameters": {
                    "startDate": start.isoformat().replace("+00:00", "Z"),
                    "endDate": end.isoformat().replace("+00:00", "Z"),
                },
            },
        )
        rows = payload.get("utm_source") if isinstance(payload, dict) else []
        return [row for row in rows or [] if isinstance(row, dict)]

    def fetch_article_day(self, website_id: str, *, path: str, day: date) -> UmamiArticleDay:
        normalized_path = normalize_path(path)
        if not normalized_path:
            raise ValueError("A canonical article path is required for Umami synchronization.")
        start_at, end_at = _day_window_ms(day)
        raw_stats = self._stats(website_id, start_at=start_at, end_at=end_at, path=normalized_path)
        event_names = {
            "engaged_30_count": "cf_engaged_30s",
            "scroll_50_count": "cf_scroll_50",
            "scroll_90_count": "cf_scroll_90",
            "cta_impression_count": "cf_cta_impression",
            "cta_click_count": "cf_cta_click",
        }
        event_visits = self._event_visits_by_name(
            website_id,
            start_at=start_at,
            end_at=end_at,
            path=normalized_path,
        )
        milestones = {
            field: _integer(event_visits.get(event))
            for field, event in event_names.items()
        }
        milestones["outbound_click_count"] = 0
        stats = {
            "pageviews": _integer(raw_stats.get("pageviews")),
            "visitors": _integer(raw_stats.get("visitors")),
            "visits": _integer(raw_stats.get("visits")),
            "bounces": _integer(raw_stats.get("bounces")),
            "umami_total_time": _integer(raw_stats.get("totaltime")),
        }
        referrers = self._referrers(website_id, start_at=start_at, end_at=end_at, path=normalized_path)
        attribution_limit = max(
            0,
            int(getattr(settings, "CONTENT_ANALYTICS_UMAMI_SOURCE_ATTRIBUTION_LIMIT", 3)),
        )
        utm_sources = self._utm_sources(website_id, day=day, path=normalized_path)
        attributed_referrers = []
        for raw_row in referrers:
            row = dict(raw_row)
            row["evidence_kind"] = "referrer"
            # Source-level CTA queries multiply API work by every referrer and
            # are deliberately decoupled from the daily headline sync. Overall
            # CTA milestones above remain exact unique-visit counts.
            row["conversion_attribution_available"] = False
            attributed_referrers.append(row)
        for index, raw_row in enumerate(utm_sources):
            row = dict(raw_row)
            utm_source = str(row.get("utm") or "").strip()
            available = bool(utm_source and index < attribution_limit)
            source_stats = (
                self._stats(
                    website_id,
                    start_at=start_at,
                    end_at=end_at,
                    path=normalized_path,
                    utm_source=utm_source,
                )
                if available
                else {}
            )
            row.update(
                {
                    "name": utm_source,
                    "evidence_kind": "utm",
                    "pageviews": _integer(source_stats.get("pageviews") or row.get("views")),
                    "visitors": _integer(source_stats.get("visitors")),
                    "visits": _integer(source_stats.get("visits")),
                    "conversion_attribution_available": False,
                }
            )
            attributed_referrers.append(row)
        return UmamiArticleDay(
            stats=stats,
            milestones=milestones,
            referrers=attributed_referrers,
            source_attribution_complete=False,
        )


_AI_SOURCES = {
    "chatgpt.com": "ChatGPT",
    "chat.openai.com": "ChatGPT",
    "perplexity.ai": "Perplexity",
    "claude.ai": "Claude",
    "copilot.microsoft.com": "Microsoft Copilot",
    "gemini.google.com": "Google Gemini",
    "grok.com": "Grok",
}
_SEARCH_SOURCES = {
    "google.com": "Google",
    "google.com.au": "Google",
    "bing.com": "Bing",
    "search.yahoo.com": "Yahoo",
    "duckduckgo.com": "DuckDuckGo",
    "ecosia.org": "Ecosia",
}
_SOCIAL_SOURCES = {
    "linkedin.com": "LinkedIn",
    "facebook.com": "Facebook",
    "instagram.com": "Instagram",
    "x.com": "X",
    "twitter.com": "X",
    "youtube.com": "YouTube",
    "reddit.com": "Reddit",
}
_SOURCE_LABEL_ALIASES = {
    "google": ("search", "Google"),
    "bing": ("search", "Bing"),
    "duckduckgo": ("search", "DuckDuckGo"),
    "chatgpt": ("ai", "ChatGPT"),
    "openai": ("ai", "ChatGPT"),
    "perplexity": ("ai", "Perplexity"),
    "claude": ("ai", "Claude"),
    "copilot": ("ai", "Microsoft Copilot"),
    "gemini": ("ai", "Google Gemini"),
    "linkedin": ("social", "LinkedIn"),
    "facebook": ("social", "Facebook"),
    "instagram": ("social", "Instagram"),
    "twitter": ("social", "X"),
    "x": ("social", "X"),
    "reddit": ("social", "Reddit"),
    "email": ("email", "Email"),
    "newsletter": ("email", "Email"),
    "google_ads": ("paid", "Google Ads"),
    "facebook_ads": ("paid", "Meta Ads"),
}


def classify_referrer(value: str) -> tuple[str, str]:
    source_label = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if source_label in _SOURCE_LABEL_ALIASES:
        return _SOURCE_LABEL_ALIASES[source_label]
    domain = normalize_domain(value)
    if not domain or domain in {"(direct)", "direct"}:
        return "direct", "Direct or unknown"
    if re.fullmatch(r"google\.(?:[a-z]{2,3}|(?:com|co)\.[a-z]{2})", domain):
        return "search", "Google"
    for mapping, category in ((_AI_SOURCES, "ai"), (_SEARCH_SOURCES, "search"), (_SOCIAL_SOURCES, "social")):
        for source_domain, label in mapping.items():
            if domain == source_domain or domain.endswith(f".{source_domain}"):
                return category, label
    return "referral", domain[:255]
