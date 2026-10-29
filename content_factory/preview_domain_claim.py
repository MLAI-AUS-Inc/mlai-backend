"""Which ownership record lets an article preview copy a live site's design.

Content Factory's snapshot preview captures the live site at a domain and splices
the draft into it on MLAI's public preview host. It asks here first, so only a
site the run's own organisation can show it controls is copied; any other domain
gets the plain brand-template preview. Nothing is stored.

There is no public suffix list in this repository, so domain matching is
deliberately narrow. A record covers a domain only when it is that domain or a
parent of it (``pawshare.com.au`` covers ``blog.pawshare.com.au``; siblings and
children never cover their parent), and never when the record is itself a public
suffix, a shared hosting root or a government or education namespace. A missed
match costs only preview fidelity; a false match would copy someone else's site.
"""

import ipaddress
import logging
import re
from urllib.parse import urlsplit

import idna
from django.contrib.auth import get_user_model
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRooApiKey
from founder_tools.models import VibeRaisingCompany
from integrations.utils import normalize_domain
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun

from .hosted_article_models import HostedArticleSite
from .website_models import WebsiteConnection

logger = logging.getLogger(__name__)

BASES = ("hosted_site", "website_connection", "email_domain", "org_website")
NONE = "none"
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}")
USER_ID = re.compile(r"[1-9][0-9]{0,17}")
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
TLD = re.compile(r"[a-z]{2,63}|xn--[a-z0-9-]{1,59}")
# Names that never resolve to a public website.
RESERVED_TLDS = frozenset({"arpa", "internal", "invalid", "local", "localhost", "onion"})
# Second-level labels ccTLD registries keep for public registration (com.au,
# co.uk, co.nz, com.sg, ...). Listing too many only narrows a claim.
CC_SECOND_LEVEL = frozenset({
    "ac", "act", "asn", "co", "com", "conf", "csiro", "edu", "go", "gob", "gouv", "gov", "govt", "gv", "id", "info", "ltd",
    "me", "mil", "ne", "net", "nom", "nsw", "nt", "or", "org", "plc", "qld", "sa", "sch", "tas", "vic", "wa"})
# Government and education namespaces have deeper public suffixes (nsw.gov.au,
# k12.ca.us) that cannot be told apart without the full list, so they are never claimed.
UNCLAIMABLE_SECOND_LEVEL = frozenset({"ac", "edu", "go", "gob", "gouv", "gov", "govt", "gv", "mil", "sch"})
UNCLAIMABLE_TLDS = frozenset({"edu", "gov", "int", "mil"})
# Hosts where unrelated customers each get a subdomain.
SHARED_HOST_SUFFIXES = frozenset({
    "amazonaws.com", "appspot.com", "azurestaticapps.net", "azurewebsites.net", "blogspot.com", "bubbleapps.io",
    "carrd.co", "cloudfront.net", "firebaseapp.com", "fly.dev", "framer.app", "framer.website", "ghost.io",
    "github.io", "gitlab.io", "glitch.me", "herokuapp.com", "hs-sites.com", "lovable.app", "myshopify.com",
    "netlify.app", "notion.site", "onrender.com", "pages.dev", "railway.app", "replit.app", "square.site",
    "squarespace.com", "substack.com", "surge.sh", "vercel.app", "web.app", "webflow.io", "weebly.com",
    "wixsite.com", "wordpress.com", "workers.dev"})
# Mailboxes anyone can open prove nothing about the domain.
FREE_MAIL_DOMAINS = frozenset({
    "126.com", "163.com", "aol.com", "bigpond.com", "bigpond.net.au", "dodo.com.au", "fastmail.com", "fastmail.fm",
    "gmail.com", "gmx.com", "gmx.net", "googlemail.com", "hey.com", "hotmail.co.uk", "hotmail.com", "hotmail.com.au",
    "icloud.com", "iinet.net.au", "internode.on.net", "live.com", "live.com.au", "mac.com", "mail.com", "mail.ru",
    "me.com", "msn.com", "optusnet.com.au", "outlook.com", "outlook.com.au", "ozemail.com.au", "pm.me",
    "proton.me", "protonmail.com", "qq.com", "rocketmail.com", "telstra.com", "tpg.com.au", "tuta.io",
    "tutanota.com", "westnet.com.au", "y7mail.com", "yahoo.co.uk", "yahoo.com", "yahoo.com.au", "yandex.com",
    "ymail.com", "zoho.com", "zohomail.com"})


def public_host(value) -> str:
    """The lower-case ASCII host of a bare domain or site URL, or "" when it is not a public DNS name.

    Scheme, port, path, a trailing dot and a leading ``www.`` are dropped and
    international names become their IDNA A-labels, so every spelling of one
    site compares equal. User info, IP addresses and single labels are refused.
    """
    text = str(value or "").strip()
    if not text or len(text) > 2048 or "\\" in text or any(ord(char) < 33 or ord(char) == 127 for char in text):
        return ""
    try:
        parts = urlsplit(text if "://" in text or text.startswith("//") else "//" + text)
        parts.port  # an out-of-range or non-numeric port raises ValueError
    except ValueError:
        return ""
    if parts.scheme not in {"", "http", "https"} or parts.username is not None or parts.password is not None:
        return ""
    host = (parts.hostname or "").rstrip(".")
    try:
        ipaddress.ip_address(host)
        return ""
    except ValueError:
        pass
    try:
        host = idna.encode(host, uts46=True).decode("ascii") if host else ""
    except (idna.IDNAError, UnicodeError):
        return ""
    labels = host.split(".")
    if (len(host) > 253 or len(labels) < 2 or not all(LABEL.fullmatch(label) for label in labels)
            or not TLD.fullmatch(labels[-1]) or labels[-1] in RESERVED_TLDS):
        return ""
    if labels[0] == "www" and len(labels) > 2:
        labels = labels[1:]
    return ".".join(labels)


def registrable_domain(host: str) -> str:
    """The name a registrant holds for ``host`` (``blog.pawshare.com.au`` -> ``pawshare.com.au``), or "" for none.

    ``host`` must come from :func:`public_host`. Public suffixes, shared hosting
    roots and government or education namespaces have no registrable domain.
    """
    labels = host.split(".")
    for index in range(len(labels)):
        if ".".join(labels[index:]) in SHARED_HOST_SUFFIXES:
            return ".".join(labels[index - 1:]) if index else ""
    if labels[-1] in UNCLAIMABLE_TLDS:
        return ""
    if len(labels[-1]) == 2 and labels[-2] in CC_SECOND_LEVEL:
        if labels[-2] in UNCLAIMABLE_SECOND_LEVEL or len(labels) < 3:
            return ""
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def covers(evidence, host: str) -> bool:
    """True when an ownership record's domain is ``host`` or a registrable parent of it."""
    evidence = public_host(evidence)
    return bool(evidence and registrable_domain(evidence) and (host == evidence or host.endswith("." + evidence)))


def free_mail(domain: str) -> bool:
    return any(domain == item or domain.endswith("." + item) for item in FREE_MAIL_DOMAINS)


def _run_organization(run):
    # The same tenant resolution as ContentFactoryRun.save: the exact FK, else the unique domain.
    if run.organization_id is not None:
        return run.organization
    domain = normalize_domain(run.domain or "")
    return Organization.objects.filter(domain=domain).first() if domain else None


def _spellings(names):
    # Stored domains are normalized but may hold the Unicode form of an international name.
    result = set(names)
    for name in names:
        try:
            result.add(idna.decode(name))
        except (idna.IDNAError, UnicodeError):
            pass
    return sorted(result)


def _claimed_by_another_organization(host: str, organization) -> bool:
    """Another MLAI organisation has this site, or a parent of it up to its registrable domain, as its own."""
    registrable, names, name = registrable_domain(host), [], host
    while True:
        names.append(name)
        if name == registrable or "." not in name:
            break
        name = name.split(".", 1)[1]
    names = _spellings(names)
    return (Organization.objects.filter(domain__in=names).exclude(pk=organization.pk).exists()
            or HostedArticleSite.objects.filter(domain__in=names).exclude(organization=organization).exists())


def _run_owner(run, organization):
    """The active founder who requested the run, while they still belong to its organisation."""
    requester = (run.run_request or {}).get("roo_points_requested_by_user_id")
    if isinstance(requester, bool) or not isinstance(requester, (int, str)) or not USER_ID.fullmatch(str(requester)):
        return None
    user = get_user_model().objects.filter(pk=int(requester), is_active=True).first()
    if user is None or not VibeRaisingCompany.objects.filter(organization=organization, profile__user=user).exists():
        return None
    return user


def claim_basis(run_id: str, host: str) -> str:
    """The strongest record showing the run's organisation controls ``host``, or "none".

    Only the run's own organisation's records count, and a site another
    organisation holds is never claimed, so a run cannot reach another tenant's site.
    """
    run = ContentFactoryRun.objects.filter(run_id=run_id).first()
    organization = _run_organization(run) if run is not None else None
    # A mail provider's own site is never a tenant's, whatever was typed as its website.
    if (organization is None or not registrable_domain(host) or free_mail(host)
            or _claimed_by_another_organization(host, organization)):
        return NONE
    sites = HostedArticleSite.objects.filter(
        organization=organization, state__in=[HostedArticleSite.State.ACTIVE, HostedArticleSite.State.DRAFT])
    if any(covers(domain, host) for domain in sites.values_list("domain", flat=True)):
        return "hosted_site"
    connections = WebsiteConnection.objects.filter(
        organization=organization, state__in=[WebsiteConnection.State.CONNECTED, WebsiteConnection.State.PAUSED])
    if any(covers(site_url, host) for site_url in connections.values_list("site_url", flat=True)):
        return "website_connection"
    owner = _run_owner(run, organization)
    if owner is not None and owner.email_verified_at is not None and "@" in owner.email:
        email_domain = public_host(owner.email.rsplit("@", 1)[1])
        if email_domain and not free_mail(email_domain) and covers(email_domain, host):
            return "email_domain"
    websites = [organization.domain, *VibeRaisingCompany.objects.filter(organization=organization).values_list("domain", flat=True)]
    if any(covers(website, host) for website in websites):
        return "org_website"
    return NONE


class PreviewDomainClaimServiceView(APIView):
    """Content Factory asks whether a run's preview may copy the live design of a domain."""

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        run_id = str(request.query_params.get("run_id") or "").strip()
        host = public_host(request.query_params.get("domain"))
        if not RUN_ID.fullmatch(run_id) or not host:
            return Response({"basis": NONE, "reason": "invalid_request",
                             "detail": "A run id and a public domain name are required."}, status=400)
        basis = claim_basis(run_id, host)
        logger.info("content_factory_preview_domain_claim run_id=%s domain=%s basis=%s", run_id, host, basis)
        return Response({"basis": basis})
