"""Narrow native Chat OAuth return URI, containing routing data only."""
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import UUID
from django.http import HttpResponseRedirect
from django.shortcuts import redirect

CHAT_CONNECTION_PROVIDERS = frozenset({
    "gmail", "stripe", "xero", "bank_feed", "notion", "google_drive",
    "slack", "linear", "google_analytics", "google", "google_search_console", "github",
})


def native_chat_connection_return_url(value):
    """Canonicalize only the registered Connections callback, never arbitrary URLs."""
    try:
        parsed = urlsplit(str(value or ""))
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        params = dict(pairs)
        if (parsed.scheme not in {"mlaichat", "mlaichat-dev"} or parsed.netloc != "connections"
                or parsed.path or parsed.fragment
                or len(pairs) != len(params)
                or set(params) != {"company_id", "provider"}
                or params["provider"] not in CHAT_CONNECTION_PROVIDERS):
            return None
        company_id = str(UUID(params["company_id"]))
    except (ValueError, TypeError, KeyError):
        return None
    return parsed.scheme + "://connections?" + urlencode({"company_id": company_id, "provider": params["provider"]})


def oauth_return_redirect(url):
    """Permit the native scheme only after validating the entire return URI."""
    native_url = native_chat_connection_return_url(url)
    if native_url:
        class NativeChatRedirect(HttpResponseRedirect):
            allowed_schemes = ["mlaichat", "mlaichat-dev"]
        response = NativeChatRedirect(native_url)
    else:
        response = redirect(url)
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response
