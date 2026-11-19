"""E11: the read-only re-engagement report for stalled, never-connected startups.

Plan section 6.7 and decision D14. Ops sends one personal email to each
founder whose startup never connected its website, one follow-up a week later,
then stops; opt-outs are honoured. This module only *reads* and *drafts*:

* :func:`build_report` buckets every never-connected organisation by its
  server-owned next step and its website address, finds the owner through
  ``founder_tools.services.organization_owner_user_id`` (the catalogue
  script's ``get_full_name`` lookup failed on the custom user model), and
  gives each one a deep link into the setup page that organisation's owner
  can open, whatever its cohort.
* :func:`render_drafts` writes the first email and the one follow-up per
  bucket, personalised per recipient, for a person to review and send.

Nothing here sends, writes a row or calls the network. The management command
``founder_reengagement_report`` refuses any send unless the owner's go-ahead
setting (``FOUNDER_REENGAGEMENT_SEND_APPROVED``) is on, and even then it is a
dry run: no sender is built for this one-off email.

The deep link. Chat sends a legacy account that opens ``/my-startup/start``
without a startup to the old add-startup form (Chat
``founder/routes/start.tsx``), so the link never uses that page. It opens
``/my-startup/onboarding?step=<page>&company_id=<id>``: the company-scoped
setup page every cohort can open (Chat ``legacy/routes/startup-onboarding.tsx``
has no cohort gate once a company id is given), which shows the server-owned
setup state (E6, decision D15). :func:`link_visible_to` proves the company in
the link is one the recipient can open (``founder_tools.membership``).

The next step. When E6's ``content_factory.founder_setup_state`` is present it
is used; otherwise :func:`local_setup_next` computes the GitHub step from the
same facts. :data:`SETUP_NEXT` is the one place the integrator swaps.
"""
from __future__ import annotations

import importlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import DEFAULT_DB_ALIAS
from django.utils import timezone

logger = logging.getLogger(__name__)

REPORT_VERSION = 1
FOLLOW_UP_AFTER = timedelta(days=7)
SETUP_PATH = "/my-startup/onboarding"
# Setup pages a re-engagement link may open. Both are company scoped and have
# no cohort gate in Chat once ``company_id`` is set.
LINK_STEPS = frozenset({"startup", "repository"})

# Buckets, in report order. ``hold`` buckets get no email (plan 6.7, D9).
CONNECT_GITHUB = "connect_github"
DOMAIN_TYPO = "domain_typo"
AI_BUILDER = "ai_builder"
NO_EXPORT_BUILDER = "no_export_builder"
PLATFORM_URL = "platform_url"
NO_DOMAIN = "no_domain"
SUBDOMAIN_OTHER_OWNER = "subdomain_other_owner"
TEST = "test"
NO_OWNER = "no_owner"
BUCKETS = (CONNECT_GITHUB, DOMAIN_TYPO, AI_BUILDER, NO_EXPORT_BUILDER, PLATFORM_URL, NO_DOMAIN,
           SUBDOMAIN_OTHER_OWNER, TEST, NO_OWNER)
OVERRIDABLE_BUCKETS = frozenset({CONNECT_GITHUB, DOMAIN_TYPO, AI_BUILDER, NO_EXPORT_BUILDER, PLATFORM_URL,
                                 NO_DOMAIN, SUBDOMAIN_OTHER_OWNER, TEST})
HOLD_REASONS = {
    NO_EXPORT_BUILDER: "Decision D9: sites kept inside this builder can't be connected yet. No email.",
    TEST: "Test organisation. No email.",
    NO_OWNER: "No active owner can open this startup. Fix the owner first; no email.",
}
# Steps whose page is the startup details page, where the website is edited.
STARTUP_PAGE_BUCKETS = frozenset({DOMAIN_TYPO, NO_DOMAIN, SUBDOMAIN_OTHER_OWNER})
GITHUB_ASKS = frozenset({"connect_github", "choose_repository", "authorize_github"})

# Contact statuses that stop an email. Unsubscribe or a spam complaint is an
# opt-out; a bounce makes the address undeliverable.
_OPTED_OUT_STATUSES = frozenset({"unsubscribed", "complained"})
_UNDELIVERABLE_STATUSES = frozenset({"bounced"})

# Hosting platforms, when E1's ``founder_tools.website_hosts`` is absent.
# Kept to E1's table (suffix, guidance id, host class); E1 wins when present.
_LOCAL_PLATFORMS = (
    ("workers.dev", "cloudflare_pages", "git_host"), ("pages.dev", "cloudflare_pages", "git_host"),
    ("vercel.app", "vercel", "git_host"), ("netlify.app", "netlify", "git_host"),
    ("netlify.com", "netlify", "git_host"), ("onrender.com", "render", "git_host"),
    ("github.io", "github_pages", "git_host"), ("up.railway.app", "railway", "git_host"),
    ("railway.app", "railway", "git_host"), ("lovable.app", "lovable", "ai_builder"),
    ("lovableproject.com", "lovable", "ai_builder"), ("bolt.host", "bolt", "ai_builder"),
    ("replit.app", "replit", "ai_builder"), ("repl.co", "replit", "ai_builder"),
    ("emergent.host", "emergent", "ai_builder"), ("base44.app", "base44", "ai_builder"),
    ("framer.website", "framer", "no_export"), ("framer.app", "framer", "no_export"),
    ("framer.ai", "framer", "no_export"), ("webflow.io", "webflow", "no_export"),
    ("wixsite.com", "wix", "no_export"), ("wixstudio.io", "wix", "no_export"),
    ("wixstudio.com", "wix", "no_export"), ("squarespace.com", "squarespace", "no_export"),
    ("myshopify.com", "shopify", "no_export"), ("chatgpt.site", "chatgpt_sites", "no_export"),
)
PLATFORM_NAMES = {
    "cloudflare_pages": "Cloudflare", "vercel": "Vercel", "netlify": "Netlify", "render": "Render",
    "github_pages": "GitHub Pages", "railway": "Railway", "lovable": "Lovable", "bolt": "Bolt",
    "replit": "Replit", "emergent": "Emergent", "base44": "Base44", "framer": "Framer", "webflow": "Webflow",
    "wix": "Wix", "squarespace": "Squarespace", "shopify": "Shopify", "chatgpt_sites": "ChatGPT",
}
_SECOND_LEVEL = frozenset({"com", "net", "org", "edu", "gov", "co", "ac", "id", "asn", "csiro", "nsw", "vic", "qld",
                           "wa", "sa", "tas", "act", "nt", "govt", "nz", "ltd", "plc", "sch"})
_UNCLAIMABLE_PARENTS = frozenset({"edu", "gov", "ac", "govt", "mil", "sch"})


# ---------------------------------------------------------------------------
# Drafts (D14: one email, one follow-up, then stop)

TEMPLATES = {
    CONNECT_GITHUB: {
        "subject": "One step left for {startup}",
        "body": ("Hi {first_name},\n\n"
                 "You added {startup} to MLAI, and there's one step left: connect your website's GitHub so we can "
                 "set up your articles. Your first article is free.\n\n"
                 "Carry on here: {link}\n"),
        "follow_up_subject": "Still want articles for {startup}?",
        "follow_up_body": ("Hi {first_name},\n\n"
                           "A quick follow-up: connecting your website's GitHub is the last step before your first "
                           "article, which is free. It takes a couple of minutes: {link}\n\n"
                           "This is the last time we'll email about it.\n"),
    },
    DOMAIN_TYPO: {
        "subject": "Is your website {suggestion}?",
        "body": ("Hi {first_name},\n\n"
                 "{startup}'s website is saved as {domain}. Did you mean {suggestion}? You can fix it in one step, "
                 "then connect your website's GitHub: {link}\n"),
        "follow_up_subject": "Quick check on {startup}'s website address",
        "follow_up_body": ("Hi {first_name},\n\n"
                           "Following up: if your website is {suggestion}, fix the address here and carry on with "
                           "setup: {link}\n\n"
                           "This is the last time we'll email about it.\n"),
    },
    AI_BUILDER: {
        "subject": "Getting {startup}'s {platform} site ready for articles",
        "body": ("Hi {first_name},\n\n"
                 "{startup}'s website is built with {platform}. To add articles, MLAI needs the site's code on "
                 "GitHub. {platform} can send it there; once it is, connect it here: {link}\n"),
        "follow_up_subject": "Your {platform} site and MLAI articles",
        "follow_up_body": ("Hi {first_name},\n\n"
                           "Following up: once {platform} has sent your site's code to GitHub, connect it here and "
                           "we'll set up your articles: {link}\n\n"
                           "This is the last time we'll email about it.\n"),
        "review": "Send only once this builder's export path is verified (plan 6.7, E3, C12).",
    },
    PLATFORM_URL: {
        "subject": "One step left for {startup}",
        "body": ("Hi {first_name},\n\n"
                 "You added {startup} with the address {domain}, which looks like a {platform} deployment address. "
                 "That's fine for setup: connect your website's GitHub here: {link}\n\n"
                 "If {startup} has a public web address, reply with it. We'll ask for it before your first article "
                 "is published.\n"),
        "follow_up_subject": "Still want articles for {startup}?",
        "follow_up_body": ("Hi {first_name},\n\n"
                           "A quick follow-up: connecting your website's GitHub is the last step before your first "
                           "article: {link}\n\n"
                           "This is the last time we'll email about it.\n"),
    },
    NO_DOMAIN: {
        "subject": "Add {startup}'s website",
        "body": ("Hi {first_name},\n\n"
                 "{startup} doesn't have a website saved yet. Add it here and we'll take you through connecting "
                 "it: {link}\n"),
        "follow_up_subject": "Add {startup}'s website",
        "follow_up_body": ("Hi {first_name},\n\n"
                           "Following up: add your website's address to carry on with setup: {link}\n\n"
                           "This is the last time we'll email about it.\n"),
    },
    SUBDOMAIN_OTHER_OWNER: {
        "subject": "Which website is {startup}'s?",
        "body": ("Hi {first_name},\n\n"
                 "{startup}'s website is saved as {domain}, which sits under someone else's domain. If {startup} "
                 "has its own address, update it here and carry on with setup: {link}\n"),
        "follow_up_subject": "Which website is {startup}'s?",
        "follow_up_body": ("Hi {first_name},\n\n"
                           "Following up: confirm {startup}'s own web address here: {link}\n\n"
                           "This is the last time we'll email about it.\n"),
    },
}
SESSION_OFFER = ("\nYou joined us at a workshop, so if it helps, reply and we'll book a short call to connect it "
                 "together.\n")
OPT_OUT_LINE = "\nIf you'd rather not hear from us about this, reply \"stop\" and we won't email again.\n"
SIGN_OFF = "\nThe MLAI team\n"


# ---------------------------------------------------------------------------
# The next step: E6 when present, otherwise the same GitHub facts locally


def local_setup_next(organization) -> dict:
    """The setup step a never-connected organisation is on, from its own facts.

    Returns ``{"current", "ask", "source"}`` in E6's vocabulary: ``github``
    with ``add_website``, ``connect_github``, ``choose_repository`` or
    ``authorize_github``. Read only.
    """
    from . import founder_journey_cohort as cohorts
    from .website_models import WebsiteConnection
    if not cohorts.organization_has_website(organization):
        return {"current": "github", "ask": "add_website", "source": "local"}
    connection = (WebsiteConnection.objects.filter(organization_id=organization.pk)
                  .order_by("-updated_at", "-created_at").first())
    if connection is None or connection.state == WebsiteConnection.State.DISCONNECTED:
        ask = "connect_github"
    elif connection.state == WebsiteConnection.State.REVOKED:
        ask = "authorize_github"
    elif not str(connection.github_repo or "").strip():
        ask = "choose_repository"
    else:
        return {"current": "articles_page", "ask": None, "source": "local"}
    return {"current": "github", "ask": ask, "source": "local"}


def e6_setup_next(organization) -> dict | None:
    """E6's server-owned setup state reduced to the current step and ask, or None when E6 is absent."""
    try:
        module = importlib.import_module("content_factory.founder_setup_state")
    except ImportError:
        return None
    setup_state = getattr(module, "setup_state", None)
    if setup_state is None:
        return None
    try:
        state = setup_state(organization)
    except Exception:
        logger.warning("founder_reengagement_setup_state_failed org=%s", organization.pk, exc_info=True)
        return None
    if not isinstance(state, dict):
        return None
    ask = state.get("ask")
    ask = ask.get("id") if isinstance(ask, dict) else None
    return {"current": state.get("current"), "ask": ask, "source": "e6"}


def setup_next(organization) -> dict:
    """The current step for the report (swap point for the integrator)."""
    return e6_setup_next(organization) or local_setup_next(organization)


SETUP_NEXT = setup_next


# ---------------------------------------------------------------------------
# Website address classes (offline: no DNS or HTTP)


@dataclass(frozen=True)
class HostClass:
    """Where an address is hosted, from its text alone."""

    platform: str | None
    host_class: str
    deployment_address: bool


def _host(domain) -> str:
    text = str(domain or "").strip().lower()
    if "://" in text:
        text = urlsplit(text).hostname or ""
    return text.split("/", 1)[0].split(":", 1)[0].rstrip(".")


def _platform(host):
    try:
        hosts = importlib.import_module("founder_tools.website_hosts")
    except ImportError:
        hosts = None
    platform_for = getattr(hosts, "platform_for", None)
    if platform_for is not None:
        match = platform_for(host)
        if match is not None:
            return match.guidance_id, match.host_class
        return None
    for suffix, guidance_id, host_class in _LOCAL_PLATFORMS:
        if host == suffix or host.endswith("." + suffix):
            return guidance_id, host_class
    return None


def _shared_host(host) -> bool:
    from .preview_domain_lists import SHARED_HOST_SUFFIXES
    return any(host == suffix or host.endswith("." + suffix) for suffix in SHARED_HOST_SUFFIXES)


def registrable_domain(host) -> str:
    """The owner's domain: the last two labels, or three under a country second level (``example.com.au``)."""
    labels = [label for label in host.split(".") if label]
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def classify_host(domain) -> HostClass:
    """Classify an address from its text: a builder or deployment host, or the founder's own."""
    host = _host(domain)
    match = _platform(host)
    if match is not None:
        guidance_id, host_class = match
        return HostClass(guidance_id, host_class, True)
    if _shared_host(host):
        return HostClass(None, "git_host", True)
    return HostClass(None, "unknown", False)


def _subdomain_of_other_owner(organization, host, *, using) -> bool:
    """A non-www subdomain under an unclaimable parent or another organisation's domain."""
    from organizations.models import Organization
    labels = host.split(".")
    parent = registrable_domain(host)
    extra = labels[: len(labels) - len(parent.split("."))]
    if not extra or extra == ["www"]:
        return False
    if len(parent.split(".")) == 3 and parent.split(".")[1] in _UNCLAIMABLE_PARENTS:
        return True
    return (Organization.objects.using(using).filter(domain__in=[parent, f"www.{parent}"])
            .exclude(pk=organization.pk).exists())


# ---------------------------------------------------------------------------
# Cohort, owner and link


def never_connected_organizations(*, using=DEFAULT_DB_ALIAS, organization_ids=None):
    """Organisations with a founder startup that never connected a website repository, by id."""
    from founder_tools.models import VibeRaisingCompany
    from organizations.models import Organization

    from .models import OrganizationContentConfig
    from .website_models import WebsiteConnection
    with_startup = VibeRaisingCompany.objects.using(using).filter(organization__isnull=False,
                                                                   closed_at__isnull=True)
    orgs = Organization.objects.using(using).filter(pk__in=with_startup.values("organization_id"))
    if organization_ids:
        orgs = orgs.filter(pk__in=organization_ids)
    connected = (WebsiteConnection.objects.using(using).exclude(github_repo="")
                 .values("organization_id"))
    configured = (OrganizationContentConfig.objects.using(using).exclude(github_repo__isnull=True)
                  .exclude(github_repo="").values("organization_id"))
    return orgs.exclude(pk__in=connected).exclude(pk__in=configured).order_by("pk")


def owner_of(organization, *, using=DEFAULT_DB_ALIAS):
    """The active owner user, through the one owner rule (``organization_owner_user_id``)."""
    from founder_tools.services import organization_owner_user_id
    owner_id = organization_owner_user_id(organization)
    if owner_id is None:
        return None
    return get_user_model().objects.using(using).filter(pk=owner_id, is_active=True).first()


def owner_company(organization, owner, *, using=DEFAULT_DB_ALIAS):
    """The startup in this organisation that the owner can open, oldest first."""
    from founder_tools.membership import companies_for_user
    if owner is None:
        return None
    return (companies_for_user(owner).using(using).filter(organization_id=organization.pk)
            .order_by("created_at", "id").first())


def frontend_origin() -> str:
    from founder_tools.my_startup.links import frontend_origin as origin
    return origin()


def setup_link(company_id, step) -> str:
    """The company-scoped setup page for ``step`` (never the cohort-gated start page)."""
    if step not in LINK_STEPS:
        raise ValueError(f"{step!r} is not a re-engagement setup page")
    query = urlencode({"step": step, "company_id": str(company_id)})
    return f"{frontend_origin()}{SETUP_PATH}?{query}"


def link_visible_to(link, user, organization, *, using=DEFAULT_DB_ALIAS) -> bool:
    """Whether ``link`` opens a setup page of ``organization`` that ``user`` can open.

    The origin is the configured Chat origin, the path is the company-scoped
    setup page (no cohort gate), the step is a setup page, and the company is
    one of the user's own startups in that organisation.
    """
    from founder_tools.membership import companies_for_user
    if user is None or not link:
        return False
    parts = urlsplit(link)
    if f"{parts.scheme}://{parts.netloc}" != frontend_origin() or parts.path != SETUP_PATH:
        return False
    query = parse_qs(parts.query)
    steps, companies = query.get("step") or [], query.get("company_id") or []
    if len(steps) != 1 or steps[0] not in LINK_STEPS or len(companies) != 1:
        return False
    return companies_for_user(user).using(using).filter(pk=companies[0], organization_id=organization.pk).exists()


def contact_block(email, *, using=DEFAULT_DB_ALIAS) -> str | None:
    """``opted_out`` or ``undeliverable``, or None when an email may go.

    Reads the CRM contact (unsubscribe, spam complaint, bounce, withdrawn
    consent) and the system email suppression list (hard bounce or complaint
    recorded by the email provider's webhook, which stores the address as
    the provider sent it). Addresses match case-blind.
    """
    from crm.models import Contact
    from system_email.models import SystemEmailSuppression
    address = str(email or "").strip()
    if not address:
        return None
    blocks = set()
    for contact in Contact.objects.using(using).filter(email__iexact=address):
        if contact.email_status in _OPTED_OUT_STATUSES:
            blocks.add("opted_out")
        elif contact.email_status in _UNDELIVERABLE_STATUSES:
            blocks.add("undeliverable")
        if contact.marketing_consent == Contact.MarketingConsent.WITHDRAWN:
            blocks.add("opted_out")
    suppressions = SystemEmailSuppression.objects.using(using).filter(email__iexact=address,
                                                                      cleared_at__isnull=True)
    for reason in suppressions.values_list("reason", flat=True):
        blocks.add("opted_out" if reason == "complaint" else "undeliverable")
    if "opted_out" in blocks:
        return "opted_out"
    return "undeliverable" if blocks else None


# ---------------------------------------------------------------------------
# Report


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _parse_time(value):
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if timezone.is_aware(moment) else timezone.make_aware(moment, timezone.get_default_timezone())


def contact_stage(ledger_entry, *, now) -> dict:
    """Where this recipient is in the one-email, one-follow-up cap (D14).

    ``ledger_entry`` is the sender's own record of what went out, keyed
    ``first`` and ``follow_up`` (ISO times). Nothing writes it here.
    """
    entry = ledger_entry if isinstance(ledger_entry, dict) else {}
    # Any recorded follow-up, stop or opt-out ends it, readable or not.
    if entry.get("follow_up") or entry.get("stopped") or entry.get("opted_out"):
        return {"stage": "stop", "after": None}
    if not entry.get("first"):
        return {"stage": "first", "after": None}
    first = _parse_time(entry.get("first"))
    if first is None:
        # A first email went out but its time is unreadable: never resend it,
        # and never guess when the follow-up is due.
        return {"stage": "stop", "after": None}
    due = first + FOLLOW_UP_AFTER
    if now < due:
        return {"stage": "wait", "after": _iso(due)}
    return {"stage": "follow_up", "after": _iso(due)}


def _bucket(organization, step, override, *, using):
    """The bucket and its facts for one organisation."""
    domain = str(organization.domain or "")
    host = _host(domain)
    host_class = classify_host(host)
    facts = {"host": host, "hostClass": host_class.host_class, "platform": host_class.platform}
    if override.get("bucket") in OVERRIDABLE_BUCKETS:
        facts["override"] = True
        if override.get("suggestion"):
            facts["suggestion"] = str(override["suggestion"]).strip().lower()
        return override["bucket"], facts
    if step.get("ask") == "add_website":
        return NO_DOMAIN, facts
    if host_class.host_class == "ai_builder":
        return AI_BUILDER, facts
    if host_class.host_class == "no_export":
        return NO_EXPORT_BUILDER, facts
    if host_class.deployment_address:
        return PLATFORM_URL, facts
    if _subdomain_of_other_owner(organization, host, using=using):
        return SUBDOMAIN_OTHER_OWNER, facts
    return CONNECT_GITHUB, facts


def validate_overrides(overrides) -> dict:
    """Ops' overrides by organisation id, refusing anything that would silently be ignored.

    An unknown bucket (a typo such as ``"tset"``) would otherwise fall through
    to the computed bucket and email a test organisation; a typo bucket with no
    corrected address has nothing to suggest.
    """
    checked = {}
    for key, value in (overrides or {}).items():
        if not isinstance(value, dict):
            raise ValueError(f"Override for organisation {key!r} must be an object.")
        bucket = value.get("bucket")
        if bucket not in OVERRIDABLE_BUCKETS:
            raise ValueError(f"Override for organisation {key!r} has unknown bucket {bucket!r}; "
                             f"use one of {', '.join(sorted(OVERRIDABLE_BUCKETS))}.")
        if bucket == DOMAIN_TYPO and not str(value.get("suggestion") or "").strip():
            raise ValueError(f"Override for organisation {key!r} is domain_typo without a suggestion.")
        checked[str(key)] = value
    return checked


def _created_on(moment, days) -> bool:
    if moment is None or not days:
        return False
    return timezone.localtime(moment).date().isoformat() in days


def build_report(*, now, using=DEFAULT_DB_ALIAS, organization_ids=None, overrides=None, ledger=None,
                 workshop_days=(), setup_next_fn=None) -> dict:
    """The deterministic re-engagement report. Read only; the caller owns the transaction.

    ``overrides`` maps an organisation id (string) to ``{"bucket", "suggestion"}``
    from ops' own checks (a typo's corrected address, a test organisation).
    ``ledger`` maps an organisation id to the sender's record of what went out.
    ``workshop_days`` are ISO dates whose sign-ups also get the session offer.
    """
    overrides = validate_overrides(overrides)
    ledger = {str(key): value for key, value in (ledger or {}).items()}
    workshop_days = frozenset(str(day) for day in workshop_days)
    next_step = setup_next_fn or SETUP_NEXT
    from . import founder_journey_cohort as cohorts
    rows = []
    for organization in never_connected_organizations(using=using, organization_ids=organization_ids):
        override = overrides.get(str(organization.pk), {})
        step = next_step(organization)
        bucket, facts = _bucket(organization, step, override, using=using)
        owner = owner_of(organization, using=using)
        company = owner_company(organization, owner, using=using)
        row = {
            "organizationId": organization.pk,
            "name": organization.name,
            "domain": organization.domain,
            "createdAt": _iso(organization.created_at),
            "cohort": cohorts.cohort(organization),
            "setup": {"current": step.get("current"), "ask": step.get("ask"), "source": step.get("source")},
            **facts,
            "workshopSession": _created_on(organization.created_at, workshop_days),
            "ownerUserId": getattr(owner, "pk", None),
            "ownerEmail": getattr(owner, "email", None),
            "ownerFirstName": (getattr(owner, "first_name", "") or "").strip() or None,
            "companyId": str(company.pk) if company else None,
            "companyName": getattr(company, "name", None),
        }
        if bucket != TEST and company is None:
            bucket = NO_OWNER
        link = None
        if company is not None:
            link = setup_link(company.pk, "startup" if bucket in STARTUP_PAGE_BUCKETS else "repository")
        row["link"] = link
        row["linkVisibleToOwner"] = bool(link) and link_visible_to(link, owner, organization, using=using)
        if link and not row["linkVisibleToOwner"] and bucket != TEST:
            bucket = NO_OWNER
        row["bucket"] = bucket
        block = contact_block(owner.email, using=using) if owner is not None else None
        row["contactBlock"] = block
        row["hold"] = HOLD_REASONS.get(bucket)
        rows.append(row)
    unmatched = _apply_contact_stages(rows, ledger, now=now)
    _mark_duplicates(rows)
    counts = {bucket: sum(1 for row in rows if row["bucket"] == bucket) for bucket in BUCKETS}
    return {"version": REPORT_VERSION, "asOf": _iso(now), "organizations": rows, "buckets": counts,
            "total": len(rows), "unmatchedLedgerKeys": unmatched}


def _merge_ledger_entries(entries) -> dict:
    """One person's sends across every ledger entry that names them.

    The earliest first email starts the follow-up wait; any follow-up, stop or
    opt-out ends it; an unreadable first time stays unreadable (and stops).
    """
    merged = {}
    firsts = [entry.get("first") for entry in entries if entry.get("first")]
    if firsts:
        times = [_parse_time(value) for value in firsts]
        merged["first"] = min(times).isoformat() if all(times) else "unreadable"
    for key in ("follow_up", "stopped", "opted_out"):
        if any(entry.get(key) for entry in entries):
            merged[key] = True
    return merged


def _apply_contact_stages(rows, ledger, *, now) -> list:
    """Set each row's place in the cap. The cap is per person, not per organisation.

    A ledger entry may be keyed by organisation id or by the recipient's email.
    Every entry for any of the owner's stalled organisations counts, so the
    email that covered a second startup (``alsoCovers``) is never sent again
    under the other one. Returns the ledger keys that matched no row, sorted,
    so ops can see a sent record that no longer lines up.
    """
    entries = {str(key): value for key, value in ledger.items() if isinstance(value, dict)}
    by_email = {}
    for key, value in entries.items():
        if "@" in key:
            by_email.setdefault(key.strip().lower(), []).append(value)
    org_keys_for = {}
    for row in rows:
        email = (row.get("ownerEmail") or "").strip().lower()
        if email:
            org_keys_for.setdefault(email, []).append(str(row["organizationId"]))
    matched = set()
    for row in rows:
        email = (row.get("ownerEmail") or "").strip().lower()
        keys = org_keys_for.get(email, []) if email else [str(row["organizationId"])]
        person = [entries[key] for key in keys if key in entries] + by_email.get(email, [])
        matched.update(key for key in keys if key in entries)
        if email and email in by_email:
            matched.update(key for key in entries if "@" in key and key.strip().lower() == email)
        if row["hold"] or row["contactBlock"]:
            row["contact"] = {"stage": "skip", "after": None}
        else:
            row["contact"] = contact_stage(_merge_ledger_entries(person), now=now)
    return sorted(key for key in ledger if str(key) not in matched)


def _mark_duplicates(rows) -> None:
    """One email per person: a second stalled startup of the same owner rides the first one's email."""
    first_for = {}
    for row in rows:
        email = (row.get("ownerEmail") or "").lower()
        row["sendWith"] = None
        if not email or row["contact"]["stage"] == "skip":
            continue
        if email in first_for:
            row["sendWith"] = first_for[email]
            row["contact"] = {"stage": "skip", "after": None}
        else:
            first_for[email] = row["organizationId"]


# ---------------------------------------------------------------------------
# Drafts


class _Blank(dict):
    def __missing__(self, key):
        return ""


def _values(row) -> dict:
    platform = PLATFORM_NAMES.get(row.get("platform") or "", "")
    if not platform and row.get("bucket") == PLATFORM_URL:
        platform = "hosting"
    return _Blank(first_name=row.get("ownerFirstName") or "there",
                  startup=row.get("companyName") or row.get("name") or "your startup",
                  domain=row.get("domain") or "", suggestion=row.get("suggestion") or "",
                  platform=platform or "your builder", link=row.get("link") or "")


def render_drafts(report) -> dict:
    """The templates per bucket and one draft per recipient at their current stage. Never sent."""
    templates = {bucket: dict(TEMPLATES[bucket]) for bucket in BUCKETS if bucket in TEMPLATES}
    drafts = []
    for row in report["organizations"]:
        stage = row["contact"]["stage"]
        template = TEMPLATES.get(row["bucket"])
        if stage not in {"first", "follow_up"} or template is None:
            continue
        if row["bucket"] == DOMAIN_TYPO and not row.get("suggestion"):
            continue
        values = _values(row)
        prefix = "follow_up_" if stage == "follow_up" else ""
        body = template[f"{prefix}body"].format_map(values)
        if row.get("workshopSession") and stage == "first":
            body += SESSION_OFFER
        body += OPT_OUT_LINE + SIGN_OFF
        draft = {"organizationId": row["organizationId"], "bucket": row["bucket"], "stage": stage,
                 "to": row["ownerEmail"], "subject": template[f"{prefix}subject"].format_map(values),
                 "body": body, "link": row["link"]}
        also = sorted(other["organizationId"] for other in report["organizations"]
                      if other.get("sendWith") == row["organizationId"])
        if also:
            draft["alsoCovers"] = also
        if template.get("review"):
            draft["review"] = template["review"]
        drafts.append(draft)
    return {"version": REPORT_VERSION, "asOf": report["asOf"], "templates": templates, "drafts": drafts,
            "cap": {"emails": 1, "followUps": 1, "followUpAfterDays": FOLLOW_UP_AFTER.days, "then": "stop"}}


def drafts_text(drafts) -> str:
    """Plain text for a person to read and send by hand."""
    lines = [f"Re-engagement drafts as of {drafts['asOf']} (not sent; one email, one follow-up after "
             f"{drafts['cap']['followUpAfterDays']} days, then stop)", ""]
    for draft in drafts["drafts"]:
        lines += [f"--- org {draft['organizationId']} ({draft['bucket']}, {draft['stage']})",
                  f"To: {draft['to']}", f"Subject: {draft['subject']}"]
        if draft.get("alsoCovers"):
            lines.append(f"Also covers orgs: {', '.join(str(item) for item in draft['alsoCovers'])}")
        if draft.get("review"):
            lines.append(f"Review: {draft['review']}")
        lines += ["", draft["body"].rstrip("\n"), ""]
    return "\n".join(lines) + "\n"


def send_approved() -> bool:
    """The owner's go-ahead for this one-off email (decisions D13, D14). Off unless set."""
    return bool(getattr(settings, "FOUNDER_REENGAGEMENT_SEND_APPROVED", False))


def dumps(value) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False)
