"""Resolve the client address that our own proxies vouch for.

Production traffic reaches Django through Cloudflare and nginx. nginx trusts
Cloudflare's ranges for ``real_ip_header CF-Connecting-IP`` and then overwrites
``X-Forwarded-For`` with ``$remote_addr`` (ops/backend-api/nginx-api.conf.template),
so the entry our nearest proxy wrote is the rightmost one. Anything to its left
was supplied by the caller and must never select a rate-limit bucket: reading
the leftmost entry let any client mint a fresh per-IP budget per request.

``TRUSTED_PROXY_HOPS`` is the number of proxies in front of Django that append
to (or overwrite) ``X-Forwarded-For``. The trusted entry is the one that many
places from the right. ``0`` ignores the header and uses ``REMOTE_ADDR``.
"""

import ipaddress

from django.conf import settings


UNKNOWN_CLIENT_IP = "unknown"


def _normalised_ip(value):
    """Return ``value`` as a canonical IP string, or ``None`` when it is not one."""
    candidate = str(value or "").strip()
    if not candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def trusted_proxy_hops():
    """Return the configured proxy hop count, never negative."""
    try:
        hops = int(getattr(settings, "TRUSTED_PROXY_HOPS", 1))
    except (TypeError, ValueError):
        hops = 1
    return max(hops, 0)


def trusted_client_ip(request):
    """Return the client IP chosen by the trusted proxy hop.

    Takes the entry ``TRUSTED_PROXY_HOPS`` from the right of
    ``X-Forwarded-For``. When the header holds fewer entries than the hop
    count, the leftmost entry is the closest one any trusted proxy wrote, so it
    is used (matching DRF's ``NUM_PROXIES`` rule). A missing or malformed entry
    falls back to ``REMOTE_ADDR``, then to ``"unknown"``.
    """
    meta = getattr(request, "META", None) or {}
    remote_addr = _normalised_ip(meta.get("REMOTE_ADDR"))
    hops = trusted_proxy_hops()
    forwarded = str(meta.get("HTTP_X_FORWARDED_FOR") or "")
    if hops and forwarded:
        entries = [entry.strip() for entry in forwarded.split(",") if entry.strip()]
        if entries:
            trusted = _normalised_ip(entries[-min(hops, len(entries))])
            if trusted:
                return trusted
    return remote_addr or UNKNOWN_CLIENT_IP
