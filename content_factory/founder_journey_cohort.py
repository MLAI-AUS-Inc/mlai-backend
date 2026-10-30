"""P1.12: the founder-journey cohort, a sticky per-organisation marker.

Every founder-journey change in P1 applies only to the new-journey cohort.
The legacy cohort (La Vie, TalaThrive, Vyavos, PawShare, mlai.au, and every
organisation with publishing history when the report is applied) keeps
today's path until its owner opts in.

The cohort lives in ``OrganizationContentConfig.pillar_strategy`` under
``founder_journey`` (no migration). It is decided once, from the marker, the
launch time and the kill switches, and is never recomputed from verification,
connection, publishing or billing state, so a flapping proof cannot move an
organisation between cohorts.

Settings (all read with ``getattr`` and a default, so a deploy alone changes
nothing):

- ``MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED``: master switch and global kill
  switch for :func:`is_new_journey`.
- ``MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT``: ISO 8601 time with an offset.
  Organisations created at or after it are new-journey.
- ``MY_STARTUP_FOUNDER_JOURNEY_V2_DISABLED_ORGANIZATIONS``: per-organisation
  kill switch, comma-separated organisation ids or domains.
- ``MY_STARTUP_FOUNDER_JOURNEY_OPT_IN_ENABLED``: offers the opt-in to owners.
- ``ARTICLES_COPY_READY_BEFORE_VERIFICATION_ENABLED``: reported to clients
  through :func:`public_payload`.
"""
from __future__ import annotations

import logging
from copy import deepcopy
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

MARKER_KEY = "founder_journey"
FIRST_PROFILE_KEY = "founder_first_profile"
POSTED_ARTICLES_KEY = "founder_posted_articles"
# pillar_strategy keys owned by the founder journey. A Content Factory
# strategy sync must never drop them and workers must never see them.
PRIVATE_STRATEGY_KEYS = (MARKER_KEY, FIRST_PROFILE_KEY, POSTED_ARTICLES_KEY)



def without_private_keys(strategy):
    """A copy of ``strategy`` without the founder-journey keys, for anything sent to a worker.

    Every other key is kept as it is, so a strategy that never held a
    founder-journey key comes back unchanged.
    """
    if not isinstance(strategy, dict):
        return strategy
    return {key: value for key, value in strategy.items() if key not in PRIVATE_STRATEGY_KEYS}


MARKER_VERSION = 1
LEGACY = "legacy"
NEW_JOURNEY = "new_journey"
COHORTS = frozenset({LEGACY, NEW_JOURNEY})
ASSIGNED_BY = frozenset({"create", "report_apply", "opt_in", "launch_window"})

NAMED_LEGACY_DOMAINS = frozenset({"talathrive.com", "vyavos.com", "pawshareclub.com", "la-vie.com.au", "mlai.au"})
SETUP_WORKFLOWS = ("article_system_setup", "repo_scan", "content_factory_scan")
SETUP_RECENT_WINDOW = timedelta(days=90)
CREATE_KEY_WINDOW_HOURS = 24
# Article runs in these states are finished and waiting on nobody.
_FINISHED_ARTICLE_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "denied", "stopped"})
_REPOSITORY_KEYS = frozenset({"github_repo", "githubRepo", "repository", "repo", "repo_full_name", "repoFullName"})

_UNSET = object()
_warned_launch_values: set[str] = set()


class FounderJourneyOptInUnavailable(Exception):
    """The organisation cannot opt in: V2 is off, it is disabled, or it already has the new journey."""


# ---------------------------------------------------------------------------
# Settings


def v2_enabled() -> bool:
    """True when ``MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED`` is on."""
    return bool(getattr(settings, "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED", False))


def launched_at():
    """The parsed ``MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT``, or None when unset.

    An unparsable value or one without a UTC offset counts as unset and logs
    one warning per distinct value.
    """
    raw = getattr(settings, "MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT", "")
    if not raw:
        return None
    if hasattr(raw, "tzinfo"):
        value = raw
    else:
        text = str(raw).strip()
        if not text:
            return None
        try:
            value = parse_datetime(text)
        except ValueError:
            value = None
    if value is None or timezone.is_naive(value):
        key = str(raw)
        if key not in _warned_launch_values:
            _warned_launch_values.add(key)
            logger.warning("founder_journey_launched_at_ignored reason=%s",
                "unparsable" if value is None else "naive")
        return None
    return value


def _disabled_entries() -> set[str]:
    raw = getattr(settings, "MY_STARTUP_FOUNDER_JOURNEY_V2_DISABLED_ORGANIZATIONS", "") or ""
    items = raw if isinstance(raw, (list, tuple, set, frozenset)) else str(raw).split(",")
    return {str(item).strip().lower() for item in items if str(item).strip()}


def _domain(organization) -> str:
    return str(getattr(organization, "domain", "") or "").strip().lower()


def is_disabled(organization) -> bool:
    """True when the organisation's id or domain is in the per-org kill switch (case-insensitive)."""
    entries = _disabled_entries()
    if not entries or organization is None:
        return False
    domain = _domain(organization)
    bare = domain[4:] if domain.startswith("www.") else domain
    return str(getattr(organization, "pk", "")) in entries or bool(domain and (domain in entries or bare in entries))


# ---------------------------------------------------------------------------
# Marker storage


def _config_for(organization, config=None):
    if config is not None:
        return config
    from .models import OrganizationContentConfig
    if organization is None or not getattr(organization, "pk", None):
        return None
    return OrganizationContentConfig.objects.filter(organization_id=organization.pk).first()


def _raw_marker(config):
    strategy = getattr(config, "pillar_strategy", None)
    if not isinstance(strategy, dict):
        return None
    marker = strategy.get(MARKER_KEY)
    return marker if isinstance(marker, dict) else None


def _valid(marker) -> bool:
    return (isinstance(marker, dict) and type(marker.get("version")) is int
            and marker.get("version") == MARKER_VERSION and marker.get("cohort") in COHORTS)


def read_marker(organization, config=None):
    """The raw ``founder_journey`` marker dict, or None.

    It ignores the V2 flag and the kill switch, so a sticky key such as
    ``ownerClickRequired`` survives either. It does not validate the marker;
    :func:`cohort` ignores a malformed one.
    """
    marker = _raw_marker(_config_for(organization, config))
    return deepcopy(marker) if marker is not None else None


def _now_iso(now=None) -> str:
    return (now or timezone.now()).isoformat()


def _base_marker(cohort_value, assigned_by, reasons, *, now=None) -> dict:
    return {
        "version": MARKER_VERSION,
        "cohort": cohort_value,
        "assignedAt": _now_iso(now),
        "assignedBy": assigned_by,
        "reasons": list(reasons),
        "optedInAt": None,
        "optedInBy": None,
        "createKeySha256": None,
        "createdCompanyId": None,
        "githubReturn": None,
        "publishAutomatically": None,
        "setupApproval": None,
    }


class _EmptyConfigType:
    """A stand-in config with no strategy, for predicates on an org without a config row."""
    pillar_strategy: dict = {}
    article_system: dict = {}


_EmptyConfig = _EmptyConfigType()


def _locked_config(organization, *, create: bool):
    """The organisation's config row, locked for update (inside an atomic block).

    It locks the ``Organization`` row first, as every other ``pillar_strategy``
    writer does (the Content Factory strategy sync, catalogue edits, the
    article voice), so a marker write and a strategy merge never interleave
    and neither loses the other's keys.
    """
    from organizations.models import Organization
    from .models import OrganizationContentConfig
    Organization.objects.select_for_update().only("pk").get(pk=organization.pk)
    config = OrganizationContentConfig.objects.select_for_update().filter(organization_id=organization.pk).first()
    if config is None and create:
        OrganizationContentConfig.objects.get_or_create(organization_id=organization.pk)
        config = OrganizationContentConfig.objects.select_for_update().get(organization_id=organization.pk)
    return config


def _save_marker(config, marker) -> None:
    strategy = deepcopy(config.pillar_strategy) if isinstance(config.pillar_strategy, dict) else {}
    strategy[MARKER_KEY] = marker
    config.pillar_strategy = strategy
    config.save(update_fields=["pillar_strategy", "updated_at"])


# ---------------------------------------------------------------------------
# Cohort predicate


def _created_after_launch(organization, launch=None) -> bool:
    launch = launch if launch is not None else launched_at()
    created = getattr(organization, "created_at", None)
    return launch is not None and created is not None and created >= launch


def cohort(organization, config=None) -> str:
    """The raw cohort, ``"legacy"`` or ``"new_journey"``, independent of the V2 flag.

    Order: (1) the per-org kill switch makes it legacy; (2) a valid marker
    decides; (3) an organisation created at or after LAUNCHED_AT is
    new-journey; (4) otherwise legacy. It never reads verification,
    connection, publishing or billing state.
    """
    if organization is None or is_disabled(organization):
        return LEGACY
    marker = _raw_marker(_config_for(organization, config))
    if _valid(marker):
        return marker["cohort"]
    return NEW_JOURNEY if _created_after_launch(organization) else LEGACY


def is_new_journey(organization, config=None) -> bool:
    """True when V2 is on and the organisation's cohort is new_journey.

    Every new-journey gate in P1 calls this and nothing else.
    """
    return v2_enabled() and cohort(organization, config) == NEW_JOURNEY


def new_startup_cohort(now=None) -> str:
    """The cohort a startup added now would join: new_journey once V2 is on and LAUNCHED_AT has passed."""
    launch = launched_at()
    if v2_enabled() and launch is not None and (now or timezone.now()) >= launch:
        return NEW_JOURNEY
    return LEGACY


# ---------------------------------------------------------------------------
# Writers


def update_marker(organization, *, github_return=_UNSET, publish_automatically=_UNSET,
                  owner_click_required=_UNSET, setup_approval=_UNSET):
    """Merge the allowlisted keys into a new-journey organisation's marker.

    Only ``githubReturn``, ``publishAutomatically``, ``ownerClickRequired``
    and ``setupApproval`` change; ``cohort`` and every other
    ``pillar_strategy`` key stay as they are. It writes when
    :func:`is_new_journey` is true or the raw marker's cohort is new_journey
    (so a sticky key is recorded even while V2 is off), and otherwise does
    nothing and returns None. A new-journey organisation without a marker
    (new through LAUNCHED_AT alone) first gets a ``launch_window`` marker.
    Creates the config row when missing. Returns the marker.
    """
    updates = {key: value for key, value in (
        ("githubReturn", github_return), ("publishAutomatically", publish_automatically),
        ("ownerClickRequired", owner_click_required), ("setupApproval", setup_approval),
    ) if value is not _UNSET}
    if organization is None or not getattr(organization, "pk", None):
        return None
    with transaction.atomic():
        config = _locked_config(organization, create=False)
        marker = _raw_marker(config)
        raw_new = _valid(marker) and marker["cohort"] == NEW_JOURNEY
        if not raw_new and not is_new_journey(organization, config if config is not None else _EmptyConfig):
            return None
        if config is None:
            config = _locked_config(organization, create=True)
            marker = _raw_marker(config)
        if not _valid(marker):
            marker = {**(marker or {}), **_base_marker(NEW_JOURNEY, "launch_window", ["created_after_launch"])}
        else:
            marker = deepcopy(marker)
        marker.update(deepcopy(updates))
        _save_marker(config, marker)
        return deepcopy(marker)


def assign_on_create(organization, *, company_id, create_key_sha256=None) -> str:
    """Mark a freshly created startup new-journey and return its raw cohort.

    Writes ``{cohort: new_journey, assignedBy: create}`` only when no valid
    marker exists, V2 is on, LAUNCHED_AT is set and the organisation was
    created at or after it, so an existing older organisation is never moved.
    """
    if organization is None or not getattr(organization, "pk", None):
        return LEGACY
    launch = launched_at()
    if not (v2_enabled() and launch is not None and _created_after_launch(organization, launch)):
        return cohort(organization)
    with transaction.atomic():
        config = _locked_config(organization, create=True)
        marker = _raw_marker(config)
        if not _valid(marker):
            fresh = _base_marker(NEW_JOURNEY, "create", ["created_after_launch"])
            fresh["createKeySha256"] = create_key_sha256 or None
            fresh["createdCompanyId"] = str(company_id) if company_id else None
            _save_marker(config, {**(marker or {}), **fresh})
        return cohort(organization, config)


def find_company_for_create_key(profile, key_sha256, *, within_hours=CREATE_KEY_WINDOW_HOURS):
    """The founder profile's company whose create marker carries ``key_sha256`` within the window, else None."""
    from founder_tools.models import VibeRaisingCompany
    from .models import OrganizationContentConfig
    if profile is None or not key_sha256:
        return None
    companies = list(VibeRaisingCompany.objects.filter(profile=profile, organization__isnull=False)
                     .select_related("organization"))
    if not companies:
        return None
    configs = {config.organization_id: config for config in
               OrganizationContentConfig.objects.filter(organization_id__in=[c.organization_id for c in companies])}
    cutoff = timezone.now() - timedelta(hours=within_hours)
    matches = []
    for company in companies:
        marker = _raw_marker(configs.get(company.organization_id))
        if not _valid(marker) or marker.get("createKeySha256") != key_sha256:
            continue
        created_for = marker.get("createdCompanyId")
        if created_for and str(created_for) != str(company.pk):
            continue
        assigned = parse_datetime(str(marker.get("assignedAt") or ""))
        if assigned is None or timezone.is_naive(assigned) or assigned < cutoff:
            continue
        matches.append((assigned, company))
    return max(matches, key=lambda item: item[0])[1] if matches else None


def opt_in(organization, user) -> dict:
    """Move a legacy organisation to the new journey at its owner's request.

    The caller checks that ``user`` is the owner. Raises
    :class:`FounderJourneyOptInUnavailable` when V2 is off, the organisation
    is disabled, or it already has the new journey. An organisation that
    already publishes to a connected website also gets ``publishAutomatically``
    (origin ``opt_in_existing_publisher``), so it keeps its review drafts.
    Returns :func:`public_payload`.
    """
    if not v2_enabled():
        raise FounderJourneyOptInUnavailable("The new journey is not available.")
    if organization is None or not getattr(organization, "pk", None) or is_disabled(organization):
        raise FounderJourneyOptInUnavailable("The new journey is not available for this startup.")
    publishes = has_connected_website(organization)
    with transaction.atomic():
        config = _locked_config(organization, create=True)
        if cohort(organization, config) == NEW_JOURNEY:
            raise FounderJourneyOptInUnavailable("This startup already has the new journey.")
        now = timezone.now()
        marker = _raw_marker(config)
        merged = deepcopy(marker) if _valid(marker) else {
            **(deepcopy(marker) or {}), **_base_marker(NEW_JOURNEY, "opt_in", [], now=now)}
        user_id = getattr(user, "pk", None)
        merged.update({"cohort": NEW_JOURNEY, "assignedBy": "opt_in", "optedInAt": now.isoformat(), "optedInBy": user_id})
        if publishes:
            merged["publishAutomatically"] = {"enabledAt": now.isoformat(), "enabledBy": user_id,
                                              "origin": "opt_in_existing_publisher"}
        _save_marker(config, merged)
    logger.info("founder_journey_opt_in org=%s", organization.pk)
    return public_payload(organization, user=user, config=config)


# ---------------------------------------------------------------------------
# Client payloads (IF-P1-02)


def _is_owner(user, organization) -> bool:
    from founder_tools.services import user_may_use_organization
    return bool(user is not None and getattr(user, "is_authenticated", False)
                and user_may_use_organization(user, organization))


def public_payload(organization, *, user, config=None):
    """The company bootstrap's ``founderJourney`` object, or None when V2 is off."""
    if not v2_enabled():
        return None
    config = _config_for(organization, config)
    raw = cohort(organization, config if config is not None else _EmptyConfig)
    new = raw == NEW_JOURNEY
    opt_in_available = bool(getattr(settings, "MY_STARTUP_FOUNDER_JOURNEY_OPT_IN_ENABLED", False)
                            and raw == LEGACY and organization is not None and not is_disabled(organization)
                            and _is_owner(user, organization))
    return {
        "version": 1,
        "cohort": NEW_JOURNEY if new else LEGACY,
        "optInAvailable": opt_in_available,
        "staffView": bool(getattr(user, "is_staff", False)),
        "copyReadyBeforeVerification": bool(
            new and getattr(settings, "ARTICLES_COPY_READY_BEFORE_VERIFICATION_ENABLED", False)),
    }


def _profile_companies(user):
    from founder_tools.models import VibeRaisingCompany
    if user is None or not getattr(user, "pk", None):
        return []
    return list(VibeRaisingCompany.objects.filter(profile__user_id=user.pk, organization__isnull=False)
                .select_related("organization"))


def _configs_by_org(companies):
    from .models import OrganizationContentConfig
    return {config.organization_id: config for config in OrganizationContentConfig.objects.filter(
        organization_id__in={company.organization_id for company in companies})}


def account_payload(user):
    """The founder profile's ``founderJourney`` object, or None when V2 is off.

    ``companies`` lists every new-journey company of this account; a company
    absent from it is legacy.
    """
    if not v2_enabled():
        return None
    companies = _profile_companies(user)
    configs = _configs_by_org(companies)
    return {
        "version": 1,
        "newStartupCohort": new_startup_cohort(),
        "companies": {str(company.pk): NEW_JOURNEY for company in companies
                      if cohort(company.organization, configs.get(company.organization_id, _EmptyConfig)) == NEW_JOURNEY},
    }


def user_has_legacy_org(user) -> bool:
    """True when any company of this user's founder profile has a raw cohort of legacy."""
    companies = _profile_companies(user)
    configs = _configs_by_org(companies)
    return any(cohort(company.organization, configs.get(company.organization_id, _EmptyConfig)) == LEGACY
               for company in companies)


# ---------------------------------------------------------------------------
# Website predicates


def organization_has_website(organization) -> bool:
    """False for an empty domain or the ``.invalid`` no-website placeholder, otherwise True."""
    domain = _domain(organization).rstrip(".")
    return bool(domain) and not domain.endswith(".invalid")


def has_connected_website(organization) -> bool:
    """True when a connected (not paused, revoked or disconnected) website has a verified target."""
    from .website_models import WebsiteConnection
    if organization is None or not getattr(organization, "pk", None):
        return False
    # Only a target of the connection's current generation counts: a reconnect
    # bumps the generation and leaves the old generation's verified targets.
    return WebsiteConnection.objects.filter(organization_id=organization.pk, state=WebsiteConnection.State.CONNECTED,
                                            targets__generation=F("generation"),
                                            targets__verified_at__isnull=False).exists()


# ---------------------------------------------------------------------------
# Report inputs


def _runs(organization):
    from workflow_runs.models import ContentFactoryRun
    scope = Q(organization_id=organization.pk)
    domain = _domain(organization)
    if domain:
        scope |= Q(organization__isnull=True, domain__iexact=domain)
    return ContentFactoryRun.objects.filter(scope)


def _published_articles(organization):
    from .models import ArticlePublishStatus, WrittenArticle
    return WrittenArticle.objects.filter(organization_id=organization.pk).filter(
        Q(publish_status__in=[ArticlePublishStatus.MERGED, ArticlePublishStatus.LIVE])
        | Q(published_at__isnull=False) | Q(on_main_verified_at__isnull=False) | Q(live_verified_at__isnull=False))


def legacy_reasons(organization, config=None) -> list[str]:
    """Why the report keeps an organisation on today's path (empty when nothing does)."""
    from .first_article_entitlement import is_established_publisher
    from .hosted_article_models import HostedArticleSite
    from .website_models import WebsiteConnection
    config = _config_for(organization, config)
    reasons = []
    if WebsiteConnection.objects.filter(organization_id=organization.pk).exists():
        reasons.append("website_connection")
    if HostedArticleSite.objects.filter(organization_id=organization.pk).exists():
        reasons.append("hosted_site")
    if _published_articles(organization).exists():
        reasons.append("published_article")
    if _runs(organization).filter(workflow__in=SETUP_WORKFLOWS,
                                  created_at__gte=timezone.now() - SETUP_RECENT_WINDOW).exists():
        reasons.append("setup_run_90d")
    domain = _domain(organization)
    if domain in NAMED_LEGACY_DOMAINS or (domain.startswith("www.") and domain[4:] in NAMED_LEGACY_DOMAINS):
        reasons.append("named_org")
    if is_established_publisher(organization, config):
        reasons.append("established_publisher")
    return reasons


def _has_saved_repository(value, depth=0) -> bool:
    if depth > 3:
        return False
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _REPOSITORY_KEYS and isinstance(item, str) and item.strip():
                return True
            if isinstance(item, (dict, list)) and _has_saved_repository(item, depth + 1):
                return True
    elif isinstance(value, list):
        return any(_has_saved_repository(item, depth + 1) for item in value[:50])
    return False


def founder_state_inventory(organization, config=None) -> list[str]:
    """State a new path could strand, checked independently of :func:`legacy_reasons`.

    Signals: ``saved_repo``, ``written_article``, ``open_article_run``,
    ``open_website_operation``, ``notification_channel``, ``publish_settings``
    and ``setup_run``. An empty list means nothing could be stranded.
    """
    from .models import NotificationChannel, WrittenArticle
    from .run_state import ARTICLE_WORKFLOWS
    from .website_models import WebsiteConnectionOperation
    from .website_operations import TERMINAL_WORKFLOW_STATES
    config = _config_for(organization, config)
    article_system = getattr(config, "article_system", None)
    signals = []
    if (config is not None and str(getattr(config, "github_repo", "") or "").strip()) or _has_saved_repository(article_system):
        signals.append("saved_repo")
    if WrittenArticle.objects.filter(organization_id=organization.pk).exists():
        signals.append("written_article")
    article_runs = _runs(organization).filter(workflow__in=ARTICLE_WORKFLOWS)
    if (article_runs.exclude(status__in=_FINISHED_ARTICLE_RUN_STATUSES).exists()
            or article_runs.filter(approval_state="approval_required").exists()):
        signals.append("open_article_run")
    if WebsiteConnectionOperation.objects.filter(connection__organization_id=organization.pk).exclude(
            state__in=TERMINAL_WORKFLOW_STATES).exists():
        signals.append("open_website_operation")
    if NotificationChannel.objects.filter(organization_id=organization.pk).exists():
        signals.append("notification_channel")
    if config is not None and (getattr(config, "auto_publish", False) or getattr(config, "requires_review", False)):
        signals.append("publish_settings")
    pending_setup = isinstance(article_system, dict) and bool(article_system.get("pending_article_system_setup"))
    if pending_setup or _runs(organization).filter(workflow__in=SETUP_WORKFLOWS).exists():
        signals.append("setup_run")
    return signals
