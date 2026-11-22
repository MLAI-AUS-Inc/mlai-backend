"""Plan E5: the staff-only list of look-alike startups across accounts.

A founder is only ever told about their own startups (the create's
own-profile check, ``website_preflight.own_duplicate``) and, across accounts,
only the exact-domain claim that already exists: the claim response never
names the owner, and an endpoint that answered "similar" for any typed name
would let anyone enumerate who is on MLAI. So look-alikes across accounts
(the same site after stripping subdomains, or the same name) are listed for
staff only, by ``manage.py founder_duplicate_lookalikes``. Read-only: nothing
here merges, archives or messages anyone. There is no general merge tool;
staff archive the empty duplicate by hand after asking the founders.

Two startups in one profile are never listed here (the founder already sees
them), and two startups of one founder with different names and sites (orgs
194 and 195) match nothing.
"""
from __future__ import annotations

from collections import defaultdict

from . import website_hosts
from .website_preflight import name_key


def _row(company):
    return {"companyId": str(company.pk), "organizationId": company.organization_id, "profileId": company.profile_id,
            "name": company.name, "domain": company.domain or None,
            "createdAt": company.created_at.isoformat() if company.created_at else None}


def cross_account_lookalikes(companies=None) -> list[dict]:
    """Groups of startups in two or more profiles that share a site key or a name.

    Each group is ``{match: "domain" | "name", key, companies: [...]}``,
    ordered by match then key, so the report is deterministic.
    """
    from .models import VibeRaisingCompany
    from .profile_fields import visible_companies

    if companies is None:
        companies = visible_companies(VibeRaisingCompany.objects.all()).order_by("created_at", "pk")
    buckets = defaultdict(list)
    for company in companies:
        site = website_hosts.site_key(company.domain)
        if site:
            buckets[("domain", site)].append(company)
        name = name_key(company.name)
        if len(name) >= 3:
            buckets[("name", name)].append(company)
    groups = []
    for (match, key), members in sorted(buckets.items()):
        if len({member.profile_id for member in members}) < 2:
            continue
        groups.append({"match": match, "key": key, "companies": [_row(member) for member in members]})
    return groups
