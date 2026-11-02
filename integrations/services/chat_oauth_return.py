"""Narrow native Chat OAuth return URI, containing routing data only."""
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import UUID
from django.http import HttpResponseRedirect
from django.shortcuts import redirect

CHAT_CONNECTION_PROVIDERS = frozenset({
    "gmail", "stripe", "xero", "bank_feed", "notion", "google_drive",
    "slack", "linear", "google_analytics", "google", "google_search_console", "github",
})
STARTUP_PATHS = frozenset({'/my-startup/articles', '/my-startup/settings', '/my-startup/create',
                          '/my-startup/onboarding', '/my-startup/growth'})
STARTUP_STEPS = frozenset({'baseline', 'websiteCheck', 'website', 'articles', 'startup', 'voice'})


def safe_chat_startup_return_path(value, *, company_id=None):
    """Canonical navigation-only startup return; optionally bind the selected company."""
    try:
        if (not isinstance(value, str) or not 0 < len(value) <= 1024 or value != value.strip()
                or '\\' in value or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            return None
        parsed = urlsplit(value)
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        params = dict(pairs)
        if (parsed.scheme or parsed.netloc or parsed.fragment or parsed.path not in STARTUP_PATHS
                or len(pairs) != len(params) or set(params)-{'company_id','articlePlan','googleBaseline','step'}):
            return None
        company = str(UUID(params['company_id']))
        if company_id is not None and company != str(UUID(str(company_id))):
            return None
        query = {'company_id': company}
        if 'articlePlan' in params:
            if parsed.path != '/my-startup/articles':
                return None
            query['articlePlan'] = str(UUID(params['articlePlan']))
        if 'googleBaseline' in params:
            if params['googleBaseline'] != 'refresh':
                return None
            query['googleBaseline'] = 'refresh'
        if 'step' in params:
            if parsed.path not in {'/my-startup/create','/my-startup/onboarding'} or params['step'] not in STARTUP_STEPS:
                return None
            query['step'] = params['step']
        return parsed.path+'?'+urlencode(query)
    except (ValueError, TypeError, KeyError):
        return None


def native_chat_startup_return_url(value):
    """Accept only the same bounded My startup grammar understood by the native app."""
    try:
        parsed = urlsplit(str(value or ''))
        if (parsed.scheme not in {'mlaichat','mlaichat-dev'} or parsed.netloc != 'my-startup'
                or parsed.username or parsed.password or parsed.fragment):
            return None
        safe = safe_chat_startup_return_path('/my-startup'+parsed.path+('?' + parsed.query if parsed.query else ''))
        return parsed.scheme+':/'+safe if safe else None
    except (ValueError, TypeError):
        return None


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
    native_url = native_chat_connection_return_url(url) or native_chat_startup_return_url(url)
    if native_url:
        class NativeChatRedirect(HttpResponseRedirect):
            allowed_schemes = ["mlaichat", "mlaichat-dev"]
        response = NativeChatRedirect(native_url)
    else:
        response = redirect(url)
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response
