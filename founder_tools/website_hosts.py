"""Where a website address is hosted, and what to call the startup (plan E1, E2).

One data table maps a hosting platform's own subdomain suffix to a host class
and a guidance id. The ids and classes are the ones Chat's builder guidance
uses (Chat ``desktop/src/features/my-startup/founderBuilderGuidance.ts`` and
``mobile/lib/features/my_startup/startup_builder_guidance.dart``); change all
three together.

Host classes:

* ``git_host``: hosts that usually build from code on GitHub already (Netlify,
  Vercel, Cloudflare, Render, GitHub Pages, Railway).
* ``ai_builder``: AI builders that can send their code to GitHub (Lovable,
  Bolt, Replit, Emergent, Base44).
* ``no_export``: builders that keep the code inside the builder (Framer,
  Webflow, Wix, Squarespace, Shopify, ChatGPT sites). Decision D9 keeps these
  "can't be connected yet".
* ``unknown``: anything else, including the founder's own domain when no
  response fingerprint names a host. Never a guess.

A platform subdomain (``auditmind-prod.up.railway.app``) is a deployment
address, not the startup's public address. Decision D10: it is accepted for
setup, and the founder is asked for the public address before the first
publish.

The name rule (E2) is the one server-owned rule; clients show the server's
``suggestedName`` and never derive their own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

HOST_CLASSES = ("git_host", "ai_builder", "no_export", "unknown")


@dataclass(frozen=True)
class Platform:
    """One hosting platform's own subdomain suffix."""

    suffix: str
    guidance_id: str
    host_class: str
    # True when the label just before the suffix is the account, not the site
    # (``<site>.<account>.workers.dev``).
    account_label: bool = False
    # True when the platform makes up random site names (``spring-surf-fc2e``).
    generated_names: bool = False


PLATFORMS: tuple[Platform, ...] = (
    Platform("workers.dev", "cloudflare_pages", "git_host", account_label=True, generated_names=True),
    Platform("pages.dev", "cloudflare_pages", "git_host", generated_names=True),
    Platform("vercel.app", "vercel", "git_host"),
    Platform("netlify.app", "netlify", "git_host"),
    Platform("netlify.com", "netlify", "git_host"),
    Platform("onrender.com", "render", "git_host"),
    Platform("github.io", "github_pages", "git_host"),
    Platform("up.railway.app", "railway", "git_host"),
    Platform("railway.app", "railway", "git_host"),
    Platform("lovable.app", "lovable", "ai_builder"),
    Platform("lovableproject.com", "lovable", "ai_builder"),
    Platform("bolt.host", "bolt", "ai_builder"),
    Platform("replit.app", "replit", "ai_builder"),
    Platform("repl.co", "replit", "ai_builder"),
    Platform("emergent.host", "emergent", "ai_builder"),
    Platform("base44.app", "base44", "ai_builder"),
    Platform("framer.website", "framer", "no_export"),
    Platform("framer.app", "framer", "no_export"),
    Platform("framer.ai", "framer", "no_export"),
    Platform("webflow.io", "webflow", "no_export"),
    Platform("wixsite.com", "wix", "no_export", account_label=True),
    Platform("wixstudio.io", "wix", "no_export"),
    Platform("wixstudio.com", "wix", "no_export"),
    Platform("squarespace.com", "squarespace", "no_export"),
    Platform("myshopify.com", "shopify", "no_export"),
    Platform("chatgpt.site", "chatgpt_sites", "no_export", account_label=True),
)

GUIDANCE = {
    "cloudflare_pages": ("Cloudflare", "git_host"), "vercel": ("Vercel", "git_host"),
    "netlify": ("Netlify", "git_host"), "render": ("Render", "git_host"),
    "github_pages": ("GitHub Pages", "git_host"), "railway": ("Railway", "git_host"),
    "lovable": ("Lovable", "ai_builder"), "bolt": ("Bolt", "ai_builder"), "replit": ("Replit", "ai_builder"),
    "v0": ("v0", "ai_builder"), "emergent": ("Emergent", "ai_builder"), "base44": ("Base44", "ai_builder"),
    "framer": ("Framer", "no_export"), "webflow": ("Webflow", "no_export"), "wix": ("Wix", "no_export"),
    "squarespace": ("Squarespace", "no_export"), "shopify": ("Shopify", "no_export"),
    "chatgpt_sites": ("ChatGPT sites", "no_export"),
}

# Two-label public suffixes the name rule and the site key must not treat as
# the startup's own label.
TWO_LEVEL_SUFFIXES = frozenset({
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "asn.au", "id.au",
    "co.nz", "net.nz", "org.nz", "co.uk", "org.uk", "ac.uk", "me.uk",
    "com.sg", "co.in", "com.br", "co.za", "com.my", "co.jp",
})
# Leading labels that name a part of a site, not the startup:
# ``platform.tailoredagents.ai`` is Tailoredagents, ``app.banksia.io`` Banksia.
GENERIC_LABELS = frozenset({
    "www", "app", "apps", "platform", "beta", "dashboard", "my", "portal", "web", "site", "home", "en",
    "staging", "stage", "dev", "prod", "api", "admin", "login", "account", "accounts", "go", "get", "try",
    "use", "hq", "blog", "shop", "store", "secure", "m", "mobile", "landing", "info",
})
# Trailing words a deployment adds to a project name (``auditmind-prod``).
DEPLOYMENT_WORDS = frozenset({"prod", "production", "staging", "stage", "dev", "preview", "app", "web", "site",
                              "live", "main", "frontend", "www", "demo", "test"})
_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)")
_GENERATED = re.compile(r"[a-z]+-[a-z]+-[0-9a-f]{4}")


def host_of(value) -> str:
    """A bare lowercase hostname from what was typed, ``''`` when it is not one."""
    raw = str(value or "").strip().lower()
    if not raw or any(char.isspace() for char in raw):
        return ""
    try:
        host = urlsplit(raw if "://" in raw else f"//{raw}").hostname or ""
    except ValueError:
        return ""
    host = host.strip(".")
    host = re.sub(r"^www\d*\.", "", host)
    labels = host.split(".")
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels):
        return ""
    if not re.fullmatch(r"[a-z]{2,63}|xn--[a-z0-9-]{1,59}", labels[-1]):
        return ""
    return host


def platform_for(host) -> Platform | None:
    """The platform whose own subdomain ``host`` is; never the platform's own site."""
    host = str(host or "").lower().rstrip(".")
    best = None
    for platform in PLATFORMS:
        if host.endswith("." + platform.suffix) and (best is None or len(platform.suffix) > len(best.suffix)):
            best = platform
    return best


def shared_host_suffix(host) -> str | None:
    """The shared hosting suffix ``host`` sits under (a known platform first, then
    Content Factory's hand-kept list, ``preview_domain_lists.SHARED_HOST_SUFFIXES``)."""
    platform = platform_for(host)
    if platform is not None:
        return platform.suffix
    from content_factory.preview_domain_lists import SHARED_HOST_SUFFIXES

    host = str(host or "").lower().rstrip(".")
    found = [suffix for suffix in SHARED_HOST_SUFFIXES if host.endswith("." + suffix)]
    return max(found, key=len) if found else None


def guidance_for(guidance_id) -> dict | None:
    """``{guidanceId, builderName, hostClass}`` for a known guidance id."""
    entry = GUIDANCE.get(str(guidance_id or ""))
    if entry is None:
        return None
    return {"guidanceId": guidance_id, "builderName": entry[0], "hostClass": entry[1]}


def public_suffix_length(labels) -> int:
    """How many trailing labels are the public suffix.

    The vendored public suffix list decides (``startup_team_domains``); a
    name it cannot read falls back to 2 for ``com.au`` and friends, else 1.
    """
    from .startup_team_domains import registrable_domain

    try:
        registrable = registrable_domain(".".join(labels))
    except Exception:  # noqa: BLE001 - the list is a refinement, never a gate
        registrable = None
    if registrable:
        return max(1, len(registrable.split(".")) - 1)
    return 2 if len(labels) >= 3 and ".".join(labels[-2:]) in TWO_LEVEL_SUFFIXES else 1


def site_key(domain) -> str:
    """What two addresses of one site share (plan E5).

    On a hosting platform the whole host is the site (two ``workers.dev``
    sites are two sites). Elsewhere a leading generic subdomain is stripped
    (``platform.tailoredagents.ai`` and ``tailoredagents.ai`` are one site),
    but a named one is kept: ``prognosole.eliascorp.org`` and
    ``viterbi.eliascorp.org`` are two startups.
    """
    host = host_of(domain)
    if not host or host.endswith(".invalid"):
        return ""
    if shared_host_suffix(host):
        return host
    labels = host.split(".")
    keep = public_suffix_length(labels) + 1
    while len(labels) > keep and labels[0] in GENERIC_LABELS:
        labels = labels[1:]
    return ".".join(labels)


def _is_random(word) -> bool:
    return bool(re.fullmatch(r"\d+", word) or (re.fullmatch(r"[0-9a-f]{4,12}", word) and re.search(r"\d", word)))


def _title(words) -> str:
    return " ".join(word[:1].upper() + word[1:] for word in words)[:255]


def derive_company_name(domain) -> str:
    """The one server rule for a startup name from its address (plan E2).

    On a hosting platform: the site label, after stripping the platform
    suffix, the account label and random or deployment suffixes
    (``auditmind-prod.up.railway.app`` is "Auditmind",
    ``lazarus-care.chalkypeach513.chatgpt.site`` is "Lazarus Care"). A name the
    platform made up (``spring-surf-fc2e.<account>.workers.dev``) gives ``''``:
    nothing in it is the startup's.

    On the founder's own domain: the first label that is not a generic part
    of a site (``www``, ``app``, ``platform``...) and not the public suffix
    (``examon.jtutes.com`` is "Examon", ``platform.tailoredagents.ai`` is
    "Tailoredagents", ``banksia-studio.com.au`` is "Banksia Studio").
    """
    raw = str(domain or "").strip().lower()
    # ``ledger_mates.co.nz``: an underscore is not a hostname character, but
    # it still separates the words of a name.
    host = host_of(raw) or host_of(raw.replace("_", "-"))
    if not host or host.endswith(".invalid"):
        return ""
    labels = host.split(".")
    platform = platform_for(host)
    shared = shared_host_suffix(host)
    if shared is not None:
        labels = labels[:-len(shared.split("."))]
        if platform is not None and platform.account_label and len(labels) >= 2:
            labels = labels[:-1]
        label = labels[0] if labels else ""
        if platform is not None and platform.generated_names and _GENERATED.fullmatch(label):
            return ""
        words = [word for word in label.split("-") if word]
        while len(words) > 1 and (_is_random(words[-1]) or words[-1] in DEPLOYMENT_WORDS):
            words.pop()
        if words and _is_random(words[0]) and len(words) == 1:
            return ""
        return _title(words)
    labels = labels[:-public_suffix_length(labels)]
    while len(labels) > 1 and labels[0] in GENERIC_LABELS:
        labels = labels[1:]
    if not labels:
        return ""
    return _title(word for word in labels[0].split("-") if word)


# Titles a template or framework leaves in place: never a startup's name.
GENERIC_TITLES = frozenset({
    "home", "homepage", "home page", "welcome", "index", "untitled", "document", "loading", "loading...",
    "react app", "vite app", "vite + react", "vite + react + ts", "next.js", "create next app", "my site",
    "my website", "coming soon", "under construction", "site", "website", "app", "lovable app", "lovable",
    "bolt", "replit", "emergent", "403 forbidden", "404 not found", "not found", "access denied",
    "just a moment...", "attention required!", "error",
})


def _clean(value, limit) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].strip()


def _title_name(title) -> str:
    """The brand part of a page title: "Banksia | Garden design" gives "Banksia"."""
    text = _clean(title, 200)
    for separator in (" | ", " - ", " – ", " — ", " :: ", " · ", " • "):
        if separator in text:
            text = text.split(separator)[0].strip()
    return text[:80]


def _usable(name, host) -> str:
    name = _clean(name, 120)
    lowered = name.lower()
    if not name or lowered in GENERIC_TITLES or lowered.rstrip(".") == host or lowered.startswith("www."):
        return ""
    return name


def homepage_name(homepage_meta, domain="") -> str:
    """The startup name a homepage gives (``og:site_name``, else the title's brand part), or ``''``.

    A template's leftover title ("Vite + React", "Home", a bot-check page) is
    never a name.
    """
    host = host_of(domain)
    meta = homepage_meta if isinstance(homepage_meta, dict) else {}
    return _usable(meta.get("ogSiteName"), host) or _usable(_title_name(meta.get("title")), host)


def suggested_company_name(domain, homepage_meta=None) -> str:
    """The name to show and save: :func:`homepage_name`, else :func:`derive_company_name`."""
    return homepage_name(homepage_meta, domain) or derive_company_name(domain)
