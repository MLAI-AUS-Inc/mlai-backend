"""One explicit test-organisation marker (plan F1, decision D8).

The nightly, the fixture fleet and the UI canary run against production with
marked test organisations. A marked organisation is MLAI's own test traffic,
so it must never look like a founder: it is left out of every founder report
(the funnel, the daily founder-health report, the re-engagement report),
never raises a stuck or journey alert, never reaches staff Slack, is never
swept by self-heal and never counts in revenue numbers.

The source of truth is one private key in the organisation's content config,
``OrganizationContentConfig.pillar_strategy["test_organization"]``::

    {"version": 1, "marked": true, "markedAt": "...", "reason": "...", "byUserId": 1}

``editorial_catalog.merge_strategy`` keeps it across scans and
``public_strategy`` hides it from workers (it is in
``p2_foundation.P2_PRIVATE_STRATEGY_KEYS``). It is written only by
``manage.py mark_test_organization``, a dry run unless ``--apply``. The
``FOUNDER_TEST_ORGANIZATION_IDS`` setting (organisation ids, comma separated)
is an operator override on top of the marker, for the host's ``.env`` only.

Reads are one scalar query (``organization_id`` only) and never load a whole
strategy document. Callers read :func:`test_organization_ids` once per sweep
or report and filter with it; they never query per organisation.

This marker does not loosen any guard. Spend ceilings, consent, the fleet's
``sandbox_repo_owners`` and refused-domain list, Content Factory's
``max_article_points`` cap and the no-delivery refunds all still apply to a
marked organisation.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

MARKER_KEY = "test_organization"
MARKER_VERSION = 1
MAX_REASON = 200


def _mapping(value):
    return value if isinstance(value, dict) else {}


def marker_from_strategy(strategy) -> dict | None:
    """The stored marker when it is a valid, set version-1 marker, else None."""
    marker = _mapping(_mapping(strategy).get(MARKER_KEY))
    if type(marker.get("version")) is int and marker["version"] == MARKER_VERSION and marker.get("marked") is True:
        return marker
    return None


def setting_ids() -> frozenset[int]:
    """The operator override: organisation ids from ``FOUNDER_TEST_ORGANIZATION_IDS``.

    Anything that is not a positive whole number is ignored (and logged once
    per read), so a typo can never mark every organisation.
    """
    raw = str(getattr(settings, "FOUNDER_TEST_ORGANIZATION_IDS", "") or "")
    ids, ignored = set(), 0
    for part in raw.replace(";", ",").split(","):
        value = part.strip()
        if not value:
            continue
        if value.isdecimal() and 0 < len(value) <= 12 and int(value) > 0:
            ids.add(int(value))
        else:
            ignored += 1
    if ignored:
        logger.warning("test_organization_setting_ignored entries=%s", ignored)
    return frozenset(ids)


def marked_ids(*, using=DEFAULT_DB_ALIAS) -> frozenset[int]:
    """Organisations whose content config carries the marker. One scalar query."""
    from .models import OrganizationContentConfig
    rows = (OrganizationContentConfig.objects.using(using)
            .filter(**{f"pillar_strategy__{MARKER_KEY}__marked": True,
                       f"pillar_strategy__{MARKER_KEY}__version": MARKER_VERSION})
            .values_list("organization_id", flat=True))
    return frozenset(int(value) for value in rows if value is not None)


def test_organization_ids(*, using=DEFAULT_DB_ALIAS) -> frozenset[int]:
    """Every test organisation: the stored marker plus the settings override."""
    return marked_ids(using=using) | setting_ids()


def _organization_id(organization_or_id):
    if organization_or_id is None:
        return None
    if isinstance(organization_or_id, int) and not isinstance(organization_or_id, bool):
        return organization_or_id
    if isinstance(organization_or_id, str) and organization_or_id.isdecimal():
        return int(organization_or_id)
    value = getattr(organization_or_id, "pk", None)
    return value if isinstance(value, int) else None


def is_test_org(organization_or_id, *, ids=None, using=DEFAULT_DB_ALIAS) -> bool:
    """Whether this organisation (an Organization, or its id) is a marked test organisation.

    Pass ``ids`` (from :func:`test_organization_ids`) inside a loop so the
    check never queries per organisation.
    """
    organization_id = _organization_id(organization_or_id)
    if organization_id is None:
        return False
    if ids is not None:
        return organization_id in ids
    if organization_id in setting_ids():
        return True
    from .models import OrganizationContentConfig
    strategy = (OrganizationContentConfig.objects.using(using).filter(organization_id=organization_id)
                .values_list(f"pillar_strategy__{MARKER_KEY}", flat=True).first())
    return marker_from_strategy({MARKER_KEY: strategy}) is not None


def exclude_test_orgs(queryset, *, field="organization_id", ids=None, using=DEFAULT_DB_ALIAS):
    """``queryset`` without rows that belong to a test organisation (``field`` names the org id)."""
    ids = test_organization_ids(using=using) if ids is None else ids
    return queryset.exclude(**{f"{field}__in": sorted(ids)}) if ids else queryset


def set_marker(organization, *, marked: bool, reason: str = "", by_user_id=None, now=None,
               using=DEFAULT_DB_ALIAS) -> dict:
    """Write or remove the marker under the config row lock. Returns ``{"changed", "marker"}``.

    Uses a queryset update, so marking never looks like founder activity.
    """
    from .models import OrganizationContentConfig
    now = now or timezone.now()
    with transaction.atomic(using=using):
        config = (OrganizationContentConfig.objects.using(using).select_for_update()
                  .filter(organization_id=organization.pk).first())
        if config is None:
            if not marked:
                return {"changed": False, "marker": None}
            config, _ = OrganizationContentConfig.objects.using(using).get_or_create(organization_id=organization.pk)
            config = OrganizationContentConfig.objects.using(using).select_for_update().get(pk=config.pk)
        strategy = dict(_mapping(config.pillar_strategy))
        current = marker_from_strategy(strategy)
        if marked:
            if current is not None:
                return {"changed": False, "marker": current}
            marker = {"version": MARKER_VERSION, "marked": True, "markedAt": now.isoformat(),
                      "reason": str(reason or "")[:MAX_REASON], "byUserId": by_user_id}
            strategy[MARKER_KEY] = marker
        else:
            if MARKER_KEY not in strategy:
                return {"changed": False, "marker": None}
            strategy.pop(MARKER_KEY)
            marker = None
        OrganizationContentConfig.objects.using(using).filter(pk=config.pk).update(pillar_strategy=strategy)
    logger.info("test_organization_marker org_id=%s marked=%s", organization.pk, marked)
    return {"changed": True, "marker": marker}


def test_only_user_ids(user_ids, *, ids=None, using=DEFAULT_DB_ALIAS) -> frozenset[int]:
    """Accounts whose every startup belongs to a test organisation: test accounts, not founders.

    An account with a real startup as well stays in founder reports; only its
    test organisation's facts are left out. One scalar query.
    """
    ids = test_organization_ids(using=using) if ids is None else ids
    user_ids = list(user_ids)
    if not ids or not user_ids:
        return frozenset()
    from founder_tools.models import VibeRaisingCompany
    organizations = {}
    for user_id, organization_id in (VibeRaisingCompany.objects.using(using)
                                     .filter(profile__user_id__in=user_ids, organization__isnull=False)
                                     .values_list("profile__user_id", "organization_id")):
        organizations.setdefault(user_id, set()).add(organization_id)
    return frozenset(user_id for user_id, owned in organizations.items() if owned and owned <= ids)
