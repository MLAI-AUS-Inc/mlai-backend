"""Sending domains: which domains CRM email may come from, and whether their DNS agrees.

``CRM_SENDING_DOMAINS`` names each domain and the route that sends for it
("mlai.au:resend"). A mailbox's address must be on a listed domain, and a
mailbox can only be switched on while its domain's required records check
out. Lookups use DNS over HTTPS (Cloudflare, then Google) and are cached for
``CACHE_SECONDS``; nothing here holds credentials or reaches the provider.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from integrations import http_client

from .models import SenderMailbox

logger = logging.getLogger(__name__)

CACHE_SECONDS = 300
TIMEOUT = (3, 5)
MAX_VALUES = 10
MAX_VALUE_LENGTH = 1024
RESOLVERS = (
    ("https://cloudflare-dns.com/dns-query", {"accept": "application/dns-json"}),
    ("https://dns.google/resolve", {"accept": "application/dns-json"}),
)
_TYPE_CODES = {"TXT": 16, "MX": 15, "CNAME": 5}
_NXDOMAIN = 3
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
_DOMAIN = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
ROUTES = (SenderMailbox.Route.RESEND, SenderMailbox.Route.GOOGLE_RELAY)

PASS, FAIL, ERROR = "pass", "fail", "error"
VERIFIED, FAILING, UNKNOWN = "verified", "failing", "unknown"


def configured_domains() -> dict[str, str]:
    """Each listed domain and its route, in setting order; bad entries are logged and skipped."""

    domains = {}
    for entry in getattr(settings, "CRM_SENDING_DOMAINS", ()):
        domain, _, route = entry.strip().lower().partition(":")
        if not _DOMAIN.match(domain) or route not in ROUTES:
            logger.warning("crm_sending_domain_ignored", extra={"entry": entry[:300]})
            continue
        domains.setdefault(domain, route)
    return domains


def domain_of(email: str) -> str:
    return email.rpartition("@")[2].lower()


# DNS over HTTPS


def _answer_value(rtype: str, data: str) -> str:
    if rtype == "TXT":
        parts = _QUOTED.findall(data)
        return "".join(part.replace('\\"', '"') for part in parts) if parts else data
    return data.rstrip(".").lower()


def lookup(name: str, rtype: str) -> Optional[list[str]]:
    """Records of ``rtype`` at ``name`` with CNAMEs followed; ``[]`` for none, ``None`` if unanswered."""

    code = _TYPE_CODES[rtype]
    for url, headers in RESOLVERS:
        try:
            response = http_client.get(
                url, params={"name": name, "type": rtype}, headers=headers, timeout=TIMEOUT
            )
            body = response.json() if response.status_code == 200 else None
        except (http_client.RequestException, ValueError):
            body = None
        if not isinstance(body, dict):
            continue
        if body.get("Status") == _NXDOMAIN:
            return []
        if body.get("Status") != 0:
            continue
        return [
            _answer_value(rtype, str(answer.get("data") or ""))
            for answer in body.get("Answer") or []
            if isinstance(answer, dict) and answer.get("type") == code
        ]
    return None


# Checks


@dataclass(frozen=True)
class Check:
    key: str
    label: str
    rtype: str
    host: str
    expected: str
    required: bool
    judge: Callable[[list[str]], tuple[bool, str]]
    # Which looked-up values to show; a domain's root TXT holds unrelated
    # verification strings, so its SPF rows show only the SPF record.
    shown: Callable[[list[str]], list[str]] = list


def _spf_records(values):
    return [value for value in values if value.lower().startswith("v=spf1")]


def _spf_or_all(values):
    return _spf_records(values) or values


def _judge_root_spf(include: Optional[str]):
    def judge(values):
        records = _spf_records(values)
        if not records:
            return False, "No SPF record."
        if len(records) > 1:
            return False, "More than one SPF record; receivers treat that as an SPF error."
        if include and f"include:{include}" not in records[0].lower().split():
            return False, f"The SPF record doesn't include {include}."
        return True, ""

    return judge


def _judge_spf(values):
    records = _spf_records(values)
    if len(records) == 1:
        return True, ""
    return False, "No SPF record." if not records else "More than one SPF record."


def _judge_dkim(values):
    for value in values:
        tags = dict(
            part.strip().partition("=")[::2] for part in value.split(";") if "=" in part
        )
        if tags.get("p", "").strip():
            return True, ""
    return False, "No DKIM public key."


def _judge_mx(values):
    return (True, "") if values else (False, "No MX record.")


def _judge_dmarc(values):
    records = [value for value in values if value.lower().startswith("v=dmarc1")]
    if len(records) != 1:
        return False, "No DMARC record." if not records else "More than one DMARC record."
    tags = {
        name.strip().lower(): setting.strip()
        for name, _, setting in (part.partition("=") for part in records[0].split(";"))
    }
    notes = []
    if tags.get("p", "").lower() == "none":
        notes.append("Monitoring only (p=none); move to quarantine once reports look clean.")
    if not tags.get("rua"):
        notes.append("No rua= address, so no aggregate reports arrive.")
    return True, " ".join(notes)


def checks_for(domain: str, route: str) -> list[Check]:
    """The records a domain needs for its route, required ones first."""

    dmarc = Check(
        "dmarc",
        "DMARC policy",
        "TXT",
        f"_dmarc.{domain}",
        f"v=DMARC1; p=none; rua=mailto:team@{domain}",
        True,
        _judge_dmarc,
    )
    inbound = Check(
        "mx", "Inbound mail (replies)", "MX", domain, "Your mail host's MX records", False, _judge_mx
    )
    if route == SenderMailbox.Route.GOOGLE_RELAY:
        return [
            Check(
                "dkim",
                "DKIM signature",
                "TXT",
                f"google._domainkey.{domain}",
                "v=DKIM1; k=rsa; p=… from Google Admin → Gmail → Authenticate email",
                True,
                _judge_dkim,
            ),
            Check(
                "spf",
                "SPF",
                "TXT",
                domain,
                "v=spf1 include:_spf.google.com ~all",
                True,
                _judge_root_spf("_spf.google.com"),
                _spf_or_all,
            ),
            dmarc,
            inbound,
        ]
    return [
        Check(
            "dkim",
            "DKIM signature",
            "TXT",
            f"resend._domainkey.{domain}",
            "p=… from Resend → Domains",
            True,
            _judge_dkim,
        ),
        Check(
            "return_path_spf",
            "Return path SPF",
            "TXT",
            f"send.{domain}",
            "CNAME send → send.forge.rmta.net (or Resend's TXT v=spf1 record)",
            True,
            _judge_spf,
        ),
        Check(
            "return_path_mx",
            "Return path MX",
            "MX",
            f"send.{domain}",
            "CNAME send → send.forge.rmta.net (or Resend's feedback MX record)",
            True,
            _judge_mx,
        ),
        dmarc,
        Check(
            "spf",
            "SPF on the domain",
            "TXT",
            domain,
            "One v=spf1 record (Resend aligns through DKIM, so this is advisory)",
            False,
            _judge_root_spf(None),
            _spf_or_all,
        ),
        inbound,
    ]


def _clip(values: list[str]) -> list[str]:
    return [value[:MAX_VALUE_LENGTH] for value in values[:MAX_VALUES]]


def run_check(check: Check) -> dict:
    values = lookup(check.host, check.rtype)
    if values is None:
        passed, status, detail, found = False, ERROR, "DNS lookup failed; try again.", []
    else:
        passed, detail = check.judge(values)
        status, found = (PASS if passed else FAIL), _clip(check.shown(values))
    return {
        "key": check.key,
        "label": check.label,
        "type": check.rtype,
        "host": check.host,
        "expected": check.expected,
        "required": check.required,
        "status": status,
        "detail": detail,
        "found": found,
    }


def overall_status(records: list[dict]) -> str:
    required = [record["status"] for record in records if record["required"]]
    if FAIL in required:
        return FAILING
    if ERROR in required:
        return UNKNOWN
    return VERIFIED


def _cache_key(domain: str, route: str) -> str:
    return f"crm:sending-domain:v1:{route}:{domain}"


def domain_health(domain: str, route: str, *, refresh: bool = False, now: Optional[datetime] = None) -> dict:
    """Checked records and overall status for one domain; cached unless ``refresh``.

    A result whose lookups failed is not cached, so the next read tries again.
    """

    key = _cache_key(domain, route)
    if not refresh:
        cached = cache.get(key)
        if cached is not None:
            return cached
    checks = checks_for(domain, route)
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        records = list(pool.map(run_check, checks))
    result = {
        "status": overall_status(records),
        "checked_at": now or timezone.now(),
        "records": records,
    }
    if result["status"] != UNKNOWN:
        cache.set(key, result, CACHE_SECONDS)
    return result


def is_verified(domain: str) -> bool:
    """Whether a listed domain's required records pass now (cached reads count)."""

    route = configured_domains().get(domain)
    return route is not None and domain_health(domain, route)["status"] == VERIFIED
