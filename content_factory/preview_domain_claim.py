"""Which ownership record lets an article preview copy a live site's design.

Content Factory's snapshot preview captures the live site at a domain and splices
the draft into it on MLAI's public preview host. It asks here first, so only a
site the run's own organisation can show it controls is copied; any other domain
gets the plain brand-template preview. Nothing is stored.

The bases, strongest first:

- ``hosted_site``: an active or draft MLAI-hosted article site of the
  organisation (operators create these).
- ``website_connection``: a connected or paused website connection of the
  organisation holds a passed live-deployment receipt for the domain. Only
  ``verify_live_deployment`` writes one, after fetching the domain's public
  pages over pinned HTTPS and finding the repository CI's sealed artifact
  digest there, so it proves the connection deploys that domain. A connection's
  typed ``site_url`` proves nothing and is not read.
- ``email_domain``: the run's requester received an email code at their current
  address, the address is on the domain, the domain is not a mail provider, and
  no other organisation with a website of its own has verified members on it.
  A code is mailed to the address the account holds at delivery, not the one
  typed when it was requested, so it counts only when the account has not been
  saved since it was requested (see ``_code_received_at``). Proof does not
  expire: someone who leaves a company keeps it until their address changes.
  That is accepted because the claim is only ever asked for the run's own
  organisation's domain, and a domain another tenant saved is refused.
- ``org_website``: the organisation's saved website. It is typed and
  first-claim-wins, so it proves nothing; Content Factory excludes it by default.

What refuses a claim depends on the basis. A listed mail provider, and an
MLAI-hosted site of another organisation at the domain, a parent or a child,
refuse every basis. A website another organisation merely saved (typed,
first-claim), and a mail brand on any country domain (``hotmail.fr``), refuse
only ``email_domain`` and ``org_website``: neither can take an operator-created
hosted site or a passed live-deployment receipt away from the organisation
that holds it.

There is no public suffix list in this repository, so domain matching is
deliberately narrow (see ``preview_domain_lists``). A record covers a domain
only when it is that domain or a parent of it (``pawshare.com.au`` covers
``blog.pawshare.com.au``; siblings and children never cover their parent), and
never when the record is itself a public suffix, a shared hosting root or a
government or education namespace. A missed match costs only preview fidelity;
a false match would copy someone else's site.
"""

import ipaddress
import logging
import re
from urllib.parse import urlsplit

import idna
from django.contrib.auth import get_user_model
from rest_framework.response import Response
from rest_framework.views import APIView

from community_chat.email_codes import email_digest
from community_chat.models import CommunityChatEmailCodeChallenge
from core.permissions import HasRooApiKey
from founder_tools.models import VibeRaisingCompany
from integrations.utils import normalize_domain
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun

from .hosted_article_models import HostedArticleSite
from .preview_domain_lists import (CC_SECOND_LEVEL, FREE_MAIL_BRANDS, FREE_MAIL_DOMAINS, REGIONAL_SECOND_LEVEL,
                                   RESERVED_TLDS, SHARED_HOST_BRANDS, SHARED_HOST_SUFFIXES,
                                   UNCLAIMABLE_ROOTS, UNCLAIMABLE_SECOND_LEVEL, UNCLAIMABLE_TLDS,
                                   VANITY_CC_SECOND_LEVEL)
from .website_models import WebsiteConnection, WebsiteConnectionOperation

logger = logging.getLogger(__name__)

BASES = ("hosted_site", "website_connection", "email_domain", "org_website")
NONE = "none"
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}")
USER_ID = re.compile(r"[1-9][0-9]{0,17}")
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
TLD = re.compile(r"[a-z]{2,63}|xn--[a-z0-9-]{1,59}")
# The host part of a stored domain that a strict parse refuses (a stray port word, user info, ...).
STORED_HOST = re.compile(r"(?:[a-z][a-z0-9+.-]*://)?(?:[^@/?#]*@)?([^/:?#\s]+)")
# The newest live-deployment receipts read per claim; every one comes from verify_live_deployment.
RECEIPTS_READ = 100
# The most other-organisation members read per mail domain; reaching it marks a mail provider.
MEMBERS_READ = 200
USABLE_CONNECTION_STATES = (WebsiteConnection.State.CONNECTED, WebsiteConnection.State.PAUSED)


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


def _under(host: str, roots) -> bool:
    labels = host.split(".")
    return any(".".join(labels[index:]) in roots for index in range(len(labels)))


def _public_suffix_depth(labels) -> int:
    """How many trailing labels form the public suffix the registrant's name sits under, or 0 for none."""
    tld = labels[-1]
    if len(tld) != 2 or len(labels) < 2:
        return 1
    second_levels = VANITY_CC_SECOND_LEVEL.get(tld, CC_SECOND_LEVEL)
    if labels[-2] in second_levels or labels[-2] in REGIONAL_SECOND_LEVEL.get(tld, ()):
        return 0 if labels[-2] in UNCLAIMABLE_SECOND_LEVEL else 2
    # Geographic .us names (ca.us, k12.ca.us) belong to governments and schools.
    if tld == "us" and len(labels[-2]) == 2:
        return 0
    return 1


def registrable_domain(host: str) -> str:
    """The name a registrant holds for ``host`` (``blog.pawshare.com.au`` -> ``pawshare.com.au``), or "" for none.

    ``host`` must come from :func:`public_host`. Public suffixes, shared hosting
    roots and government or education namespaces have no registrable domain.
    """
    if _under(host, UNCLAIMABLE_ROOTS):
        return ""
    labels = host.split(".")
    for index in range(len(labels)):
        if ".".join(labels[index:]) in SHARED_HOST_SUFFIXES:
            return ".".join(labels[index - 1:]) if index else ""
    if labels[-1] in UNCLAIMABLE_TLDS:
        return ""
    depth = _public_suffix_depth(labels)
    if not depth or len(labels) <= depth:
        return ""
    # A platform brand on a country domain (blogspot.com.au) is itself a shared root.
    if labels[-depth - 1] in SHARED_HOST_BRANDS:
        depth += 1
        if len(labels) <= depth:
            return ""
    return ".".join(labels[-depth - 1:])


def covers(evidence, host: str) -> bool:
    """True when an ownership record's domain is ``host`` or a registrable parent of it."""
    evidence = public_host(evidence)
    return bool(evidence and registrable_domain(evidence) and (host == evidence or host.endswith("." + evidence)))


def free_mail(domain: str) -> bool:
    """True when ``domain`` (from :func:`public_host`) belongs to a mail provider anyone can open a box at."""
    if _under(domain, FREE_MAIL_DOMAINS):
        return True
    registrable = registrable_domain(domain)
    return bool(registrable) and registrable.split(".", 1)[0] in FREE_MAIL_BRANDS


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


def _stored_host(value) -> str:
    """The public host of a stored domain, also when it kept a port, a trailing dot or other debris."""
    host = public_host(value)
    if host:
        return host
    match = STORED_HOST.match(str(value or "").strip().lower())
    return public_host(match.group(1)) if match else ""


def _held(host: str, stored_domains) -> bool:
    """True when a stored domain is ``host``, a parent of it up to its registrable domain, or a child of it."""
    registrable, names, name = registrable_domain(host), set(), host
    while True:
        names.add(name)
        if name == registrable or "." not in name:
            break
        name = name.split(".", 1)[1]
    for stored in stored_domains:
        stored = _stored_host(stored)
        if stored and (stored in names or stored.endswith("." + host)):
            return True
    return False


def _held_by_rival(host: str, records, **others) -> bool:
    # Every stored spelling counts (ports, trailing dots, schemes, Unicode), so the
    # candidates are read by the registrable name and compared once normalized.
    return any(_held(host, records.filter(domain__icontains=spelling).exclude(**others).values_list("domain", flat=True))
               for spelling in _spellings([registrable_domain(host)]))


def _rival_hosted_site(host: str, organization) -> bool:
    """Another organisation has an MLAI-hosted article site (operators create these) at this site, a parent or a child."""
    return _held_by_rival(host, HostedArticleSite.objects, organization=organization)


def _rival_website(host: str, organization) -> bool:
    """Another organisation saved this site, a parent or a child as its own typed, first-claim website."""
    return _held_by_rival(host, Organization.objects, pk=organization.pk)


def _run_owner(run, organization):
    """The active founder who requested the run, while they still belong to its organisation."""
    requester = (run.run_request or {}).get("roo_points_requested_by_user_id")
    if isinstance(requester, bool) or not isinstance(requester, (int, str)) or not USER_ID.fullmatch(str(requester)):
        return None
    user = get_user_model().objects.filter(pk=int(requester), is_active=True).first()
    if user is None or not VibeRaisingCompany.objects.filter(organization=organization, profile__user=user).exists():
        return None
    return user


def _deployment_proven(organization, host: str) -> bool:
    """A usable connection of the organisation passed a live-deployment check on ``host`` or a parent, at any age."""
    receipts = WebsiteConnectionOperation.objects.filter(
        connection__organization=organization, connection__state__in=USABLE_CONNECTION_STATES,
        action="deployment-verify", state="completed", receipt__status="passed",
    ).order_by("-created_at").values_list("receipt", flat=True)[:RECEIPTS_READ]
    return any(isinstance(receipt, dict) and receipt.get("status") == "passed" and receipt.get("provider_verified") is True
               and covers(receipt.get("public_url"), host) for receipt in receipts)


def _code_received_at(owner, email: str) -> bool:
    """The owner received a consumed email code at ``email``, their current address.

    A challenge records the address typed when the code was requested, but the
    outbox mails the code to whatever address the account holds at delivery, and
    email_verified_at is stamped once and survives an address change. So a code
    counts only when the account has not been saved since it was requested:
    every profile or admin change of address saves the account, which stamps
    ``updated_at``, so the address was the same at request, delivery and now. A
    sign-up code also counts: it was requested before the account existed and
    mailed to the typed address itself.
    """
    codes = CommunityChatEmailCodeChallenge.objects.filter(user=owner, email_digest=email_digest(email),
                                                           consumed_at__isnull=False)
    if owner.updated_at is not None and codes.filter(created_at__gte=owner.updated_at).exists():
        return True
    return owner.date_joined is not None and codes.filter(created_at__lt=owner.date_joined).exists()


def _shared_mail_domain(owner, organization, spellings) -> bool:
    """Verified founders of other organisations that have websites of their own hold addresses on this domain.

    That marks a mail provider, not one company. A founder whose other company
    has no website (a placeholder ``startup-<pk>.invalid`` organisation, as a
    co-founder's usually is) says nothing about the domain.
    """
    for spelling in spellings:
        domains = list(VibeRaisingCompany.objects.filter(
            profile__user__email__iendswith="@" + spelling, profile__user__email_verified_at__isnull=False,
            organization__isnull=False).exclude(organization=organization).exclude(profile__user=owner)
            .values_list("organization__domain", flat=True)[:MEMBERS_READ])
        # So many members on one domain is a provider whatever their organisations are.
        if len(domains) >= MEMBERS_READ or any(_stored_host(domain) for domain in domains):
            return True
    return False


def _mailbox_proven(owner, organization, host: str) -> bool:
    """The owner received a code at their current address, on ``host`` or a parent, at a domain one organisation uses."""
    email = str(owner.email or "")
    if owner.email_verified_at is None or email.count("@") != 1:
        return False
    stored_domain = email.rsplit("@", 1)[1]
    domain = public_host(stored_domain)
    if not domain or free_mail(domain) or not covers(domain, host):
        return False
    return _code_received_at(owner, email) and not _shared_mail_domain(owner, organization, sorted({stored_domain, domain}))


def claim_basis(run_id: str, host: str) -> str:
    """The strongest record showing the run's organisation controls ``host``, or "none".

    Only the run's own organisation's records count, and a site another
    organisation holds by an equal or stronger record is never claimed, so a run
    cannot reach another tenant's site.
    """
    run = ContentFactoryRun.objects.filter(run_id=run_id).first()
    organization = _run_organization(run) if run is not None else None
    # A listed mail provider's own site is never a tenant's, whatever was saved as its website.
    if (organization is None or not registrable_domain(host) or _under(host, FREE_MAIL_DOMAINS)
            or _rival_hosted_site(host, organization)):
        return NONE
    # Operator-created sites and live-deployment receipts prove control by themselves.
    sites = HostedArticleSite.objects.filter(
        organization=organization, state__in=[HostedArticleSite.State.ACTIVE, HostedArticleSite.State.DRAFT])
    if any(covers(domain, host) for domain in sites.values_list("domain", flat=True)):
        return "hosted_site"
    if _deployment_proven(organization, host):
        return "website_connection"
    # The weak bases never claim a mail brand's site or one another organisation saved.
    if free_mail(host) or _rival_website(host, organization):
        return NONE
    owner = _run_owner(run, organization)
    if owner is not None and _mailbox_proven(owner, organization, host):
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
