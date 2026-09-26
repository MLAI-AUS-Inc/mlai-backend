"""Client-specific links without changing stored content or provider callbacks."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings

API_PREFIX = "/api/v1/my-startup/"
PAGE_ROUTES = {
    "/founder-tools": "/my-startup",
    "/founder-tools/start": "/my-startup/onboarding",
    "/founder-tools/company-setup": "/my-startup/company",
    "/founder-tools/companies": "/my-startup/companies",
    "/founder-tools/switch-company": "/my-startup/switch-company",
    "/founder-tools/data-sources": "/my-startup/connections",
    "/founder-tools/link-roo": "/my-startup/link-roo",
    "/founder-tools/upgrade": "/my-startup/credits",
    "/founder-tools/upgrades": "/my-startup/credits",
    "/founder-tools/marketing": "/my-startup",
    "/founder-tools/marketing/editorial": "/my-startup/customer-profiles",
    "/founder-tools/marketing/articles": "/my-startup/articles",
    "/founder-tools/marketing/create": "/my-startup/create",
    "/founder-tools/marketing/settings": "/my-startup/settings",
    "/founder-tools/marketing/github-connect": "/my-startup/github-connect",
    "/founder-tools/marketing/island-research": "/my-startup/island-research",
}
URL_FIELDS = frozenset(
    {
        "href",
        "url",
        "redirect",
        "redirectUrl",
        "returnUrl",
        "connectUrl",
        "authUrl",
        "auth_url",
        "return_url",
        "previewUrl",
        "preview_url",
        "proxyPath",
        "proxy_path",
        "fallbackPreviewUrl",
        "fallback_preview_url",
        "statusUrl",
    }
)


def frontend_origin():
    """Return the configured Chat origin, never a client-supplied redirect host."""
    return str(
        getattr(settings, "COMMUNITY_CHAT_FRONTEND_URL", "https://chat.mlai.au")
    ).rstrip("/")


def page_path(path):
    if path in PAGE_ROUTES:
        return PAGE_ROUTES[path]
    for source, destination in (
        ("/founder-tools/marketing/runs/", "/my-startup/runs/"),
        ("/founder-tools/marketing/autofill-runs/", "/my-startup/autofill-runs/"),
    ):
        if path.startswith(source):
            return destination + path[len(source) :]
    return None


def is_startup_request(request):
    return str(getattr(request, "path", "")).startswith(API_PREFIX)


def rewrite_url(value, *, company_id=""):
    """Rewrite only known product URLs on configured MLAI origins."""
    if not isinstance(value, str) or not value or "\\" in value:
        return value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    known_origins = {
        "https://mlai.au",
        "https://www.mlai.au",
        "https://api.mlai.au",
        frontend_origin(),
    }
    known_origins.update(
        str(getattr(settings, key, "") or "").rstrip("/")
        for key in (
            "DEFAULT_BACKEND_URL",
            "DEFAULT_FRONTEND_URL",
            "FOUNDER_TOOLS_URL",
            "VIBE_RAISING_URL",
        )
    )
    if parsed.netloc and (
        parsed.username
        or parsed.password
        or f"{parsed.scheme}://{parsed.netloc}" not in known_origins
    ):
        return value
    path = page_path(parsed.path)
    scheme, netloc = parsed.scheme, parsed.netloc
    if path:
        if netloc:
            target = urlsplit(frontend_origin())
            scheme, netloc = target.scheme, target.netloc
    elif parsed.path.startswith("/api/v1/vibe-marketing/"):
        tail = parsed.path[len("/api/v1/") :]
        if company_id and "/live-preview/" in tail:
            path = f"{API_PREFIX}companies/{company_id}/{tail}"
        else:
            path = API_PREFIX + tail
    else:
        path = parsed.path
    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        query.append(
            (
                key,
                rewrite_url(item, company_id=company_id)
                if key in {"next", "return_url"}
                else item,
            )
        )
    # Carry company identity even in server-generated workflow links.
    if (
        company_id
        and path.startswith("/my-startup")
        and not any(k in {"company_id", "companyId"} for k, _ in query)
    ):
        query.append(("company_id", str(company_id)))
    return urlunsplit((scheme, netloc, path, urlencode(query), parsed.fragment))


def rewrite_payload(value, *, company_id=""):
    """Adapt navigation fields only; never rewrite user prose, code, or policies."""
    if isinstance(value, list):
        return [rewrite_payload(item, company_id=company_id) for item in value]
    if isinstance(value, dict):
        return {
            key: rewrite_url(item, company_id=company_id)
            if key in URL_FIELDS and isinstance(item, str)
            else rewrite_payload(item, company_id=company_id)
            for key, item in value.items()
        }
    return value


def marketing_delivery_url(url, *, company_id=""):
    """Opt-in cutover for durable email/Slack/action links; legacy URLs stay valid."""
    if not getattr(settings, "MY_STARTUP_DELIVERY_LINKS_ENABLED", False):
        return url
    return rewrite_url(url, company_id=company_id)
