"""One free first article per startup, kept as zero-point ledger rows (P0.2).

In the 30 days to 29 Oct 2026, 0 of 80 new users reached an article and 45 of
them had 0 points. Each startup now gets one free first article.

Storage needs no schema change. Every state is a roo ``Ledger`` row (kind
ADJUST, source CONTENT_FACTORY, delta 0, reference_type FIRST_ARTICLE_FREE)
whose globally unique idempotency key names the organisation and the step::

    content_factory:first_article:org:{org}:attempt:{n}           reserved, n is 1 or 2
    content_factory:first_article:org:{org}:attempt:{n}:released  released (terminal)
    content_factory:first_article:org:{org}:consumed              consumed (terminal, one per org)
    content_factory:first_article:org:{org}:topics                the free first topic batch
    content_factory:first_article:org:{org}:topics:released       that batch never started
    content_factory:first_article:org:{org}:topics:retry          its one free re-run

An attempt's reference_id is the ledger reference of its original request id,
so a replay or transport retry finds the same row, and a consumed or released
row carries the reference of the attempt it settles. The state is derived from
these rows only: each attempt is reserved, then exactly one of consumed or
released, and both are terminal. Only the original attempt run settles it.
Revisions, restarts, re-pins, resumes and comments run on its stamp and never
consume or release it.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

REFERENCE_TYPE = "FIRST_ARTICLE_FREE"
ENTITLEMENT = "first_article"
BILLING_STATUS = "entitled"
DESCRIPTION = "Free first article"
ATTEMPTS_ALLOWED = 2
# Per account email, across startups: one live (reserved or consumed) free
# article and three reservations in any outcome per rolling 30 days.
ACCOUNT_WINDOW = timedelta(days=30)
ACCOUNT_ACTIVE_LIMIT = 1
ACCOUNT_RESERVATION_LIMIT = 3
FREE_TOPIC_BATCH_MAX = 4
TOPIC_ACTIONS = frozenset({"content_island_topic_generation", "topic_discovery"})

# Content Factory article steps from planning onwards (content-factory 91d4efa,
# backend/harness/runner.py BASE_PUBLISH_STEPS, REVIEW_DRAFT_STEPS,
# CONTENT_ONLY_STEPS and the *_TAIL_STEPS lists; the "write" phase starts at
# plan_article in backend/harness/artifacts.py _STEP_DURATION_PHASE_RULES).
# A stop after one of these used model spend on writing, so an owner cancel
# consumes the free article instead of releasing it.
WRITING_STEPS = frozenset({
    "plan_article", "assemble_article", "generate_directory_sources", "integrate_directory_sources",
    "prepare_seed_article_sources", "plan_resource_asset", "generate_resource_pdf", "generate_content_images",
    "package_content_delivery", "render_article", "validate_render_dependencies", "prepare_bootstrap_publish_target",
    "prove_publish_surface", "verify_static", "verify_build", "package_publish_bundle", "publish_preview_draft",
    "acquire_preview_artifact", "verify_browser", "review_visual_style", "await_publish_approval", "await_review",
    "finalize",
})
WRITING_STEP_PREFIXES = ("draft_section:", "ground_section:")
NOT_STARTED_STATUSES = frozenset({"queued", "pending"})

_KEY_PREFIX = "content_factory:first_article:org:"
_KEY = re.compile(r"^content_factory:first_article:org:(\d+):(attempt:(\d+)(:released)?|consumed|topics(:released|:retry)?)$")
_goal_conflict_logged = False


def _key(organization_id, suffix: str) -> str:
    return f"{_KEY_PREFIX}{int(organization_id)}:{suffix}"


def _reference(client_request_id) -> str:
    from integrations.services.article_generation import _ledger_reference_id
    return _ledger_reference_id(str(client_request_id or "").strip())


def _organization_id(organization, run=None) -> Optional[int]:
    value = getattr(organization, "pk", organization) if organization is not None else getattr(run, "organization_id", None)
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def free_first_article_enabled() -> bool:
    """True when free first articles are on and the goal-scoped billing path is off.

    The first-article goal (website_first_article_billing) charges its own
    spend. P0 refuses to combine the two, so the goal flag disables this one.
    """
    global _goal_conflict_logged
    if not getattr(settings, "ARTICLES_FREE_FIRST_ARTICLE_ENABLED", False):
        return False
    if getattr(settings, "ARTICLES_FIRST_ARTICLE_GOAL_ENABLED", False):
        if not _goal_conflict_logged:
            logger.warning("first_article_entitlement_disabled reason=first_article_goal_enabled")
            _goal_conflict_logged = True
        return False
    return True


@dataclass
class _Rows:
    attempts: dict = field(default_factory=dict)
    released: set = field(default_factory=set)
    consumed: object = None
    topics: object = None
    topics_released: bool = False
    topics_retry: object = None

    def attempt_state(self, number: int) -> str:
        if number in self.released:
            return "released"
        if self.consumed is not None and self.consumed.reference_id == self.attempts[number].reference_id:
            return "consumed"
        return "reserved"

    def attempt_for_reference(self, reference: str):
        return next(((number, row) for number, row in sorted(self.attempts.items()) if row.reference_id == reference), (None, None))


def _rows(organization_id) -> _Rows:
    from roo.models import Ledger
    rows = _Rows()
    for row in Ledger.objects.filter(reference_type=REFERENCE_TYPE, idempotency_key__startswith=_key(organization_id, "")):
        match = _KEY.match(row.idempotency_key or "")
        if not match or int(match.group(1)) != int(organization_id):
            continue
        step = match.group(2)
        if match.group(3):
            number = int(match.group(3))
            if match.group(4):
                rows.released.add(number)
            else:
                rows.attempts[number] = row
        elif step == "consumed":
            rows.consumed = row
        elif step == "topics":
            rows.topics = row
        elif step == "topics:released":
            rows.topics_released = True
        elif step == "topics:retry":
            rows.topics_retry = row
    return rows


def _write(*, organization_id, suffix, user, reference_id):
    """Create one marker row idempotently; a concurrent writer keeps its own row."""
    from roo.models import Ledger
    row, _created = Ledger.objects.get_or_create(idempotency_key=_key(organization_id, suffix), defaults={
        "user": user, "kind": "ADJUST", "source": "CONTENT_FACTORY", "delta": 0, "delta_microroo": 0,
        "digital_delta_microroo": 0, "reference_type": REFERENCE_TYPE, "reference_id": reference_id,
        "description": DESCRIPTION})
    return row


def _lock(organization_id, user=None) -> None:
    from django.contrib.auth import get_user_model
    from organizations.models import Organization
    Organization.objects.select_for_update().filter(pk=organization_id).first()
    if user is not None and getattr(user, "pk", None):
        # Account caps span startups; one account's presses queue behind each other.
        get_user_model().objects.select_for_update().filter(pk=user.pk).first()


def _lock_attempt(organization_id, request):
    """Lock the original attempt row and re-read the rows under that lock.

    Consume and release serialise on the attempt row itself, a leaf lock taken
    last. Callers may already hold a run lock (a worker's cancellation receipt
    locks the run first), so taking the organisation lock here could invert
    the organisation-then-run order the reconciliation sweeps use.
    """
    from roo.models import Ledger
    number, attempt = _matching_attempt(_rows(organization_id), request)
    if attempt is None:
        return _Rows(), None, None
    Ledger.objects.select_for_update().filter(pk=attempt.pk).first()
    rows = _rows(organization_id)
    number, attempt = _matching_attempt(rows, request)
    return rows, number, attempt


def _account_rows(user, *, now):
    """Entitlement rows of every account sharing this account's email in the window."""
    from django.db.models import Q
    from roo.models import Ledger
    email = str(getattr(user, "email", "") or "").strip()
    owner = Q(user__email__iexact=email) if email else Q(user_id=getattr(user, "pk", None))
    return list(Ledger.objects.filter(owner, reference_type=REFERENCE_TYPE, created_at__gte=now - ACCOUNT_WINDOW))


def _account_limited(user, *, topics: bool, now) -> bool:
    """Apply the email-keyed cap: one live grant and three grants in any outcome."""
    if user is None:
        return True
    rows = _account_rows(user, now=now)
    keys = {row.idempotency_key for row in rows}
    if topics:
        grants = [row for row in rows if (row.idempotency_key or "").endswith((":topics", ":topics:retry"))]
    else:
        grants = [row for row in rows if re.search(r":attempt:\d+$", row.idempotency_key or "")]
    from roo.models import Ledger
    released = keys | set(Ledger.objects.filter(
        idempotency_key__in=[f"{row.idempotency_key}:released" for row in grants]).values_list("idempotency_key", flat=True))
    live = [row for row in grants if f"{row.idempotency_key}:released" not in released]
    return len(live) >= ACCOUNT_ACTIVE_LIMIT or len(grants) >= ACCOUNT_RESERVATION_LIMIT


def _recent_reservations(since) -> int:
    from roo.models import Ledger
    return Ledger.objects.filter(reference_type=REFERENCE_TYPE, created_at__gte=since,
        idempotency_key__contains=":attempt:").exclude(idempotency_key__endswith=":released").count()


def _daily_cap_reached(now) -> bool:
    cap = int(getattr(settings, "ARTICLES_FREE_FIRST_ARTICLE_DAILY_CAP", 0) or 0)
    return cap > 0 and _recent_reservations(now - timedelta(hours=24)) >= cap


def _mapping(value):
    return value if isinstance(value, dict) else {}


def entitled_request(request) -> bool:
    """A run request whose work the first-article entitlement covers."""
    request = _mapping(request)
    return request.get("roo_points_billing_status") == BILLING_STATUS and request.get("roo_points_entitlement") == ENTITLEMENT


def original_entitled_request(request) -> bool:
    """The original attempt's request: entitled, and not a child of another run."""
    request = _mapping(request)
    return entitled_request(request) and not request.get("original_billing_source_run_id")


def _matching_attempt(rows: _Rows, request):
    """Return (number, row) for the original attempt this request started, else (None, None).

    A request without a stamp (a refund path's article_request) is matched by
    its original request id alone. A stamped request must also name that row.
    """
    request = _mapping(request)
    if request.get("original_billing_source_run_id"):
        return None, None
    key = str(request.get("client_request_id") or "").strip()
    if not key:
        return None, None
    number, row = rows.attempt_for_reference(_reference(key))
    stamp = str(request.get("roo_points_ledger_id") or "").strip()
    if row is None or (stamp and stamp != str(row.pk)):
        return None, None
    if stamp and not entitled_request(request):
        return None, None
    return number, row


def _delivered(run) -> bool:
    from .article_publication_evidence import useful_delivery
    return getattr(run, "status", None) == "completed" or useful_delivery(run)


def _original_runs(organization_id):
    from workflow_runs.models import ContentFactoryRun
    from .run_state import ARTICLE_WORKFLOWS
    runs = ContentFactoryRun.objects.filter(organization_id=organization_id, workflow__in=ARTICLE_WORKFLOWS,
        run_request__roo_points_entitlement=ENTITLEMENT).order_by("created_at", "id")
    return [run for run in runs if original_entitled_request(run.run_request)]


def _run_for_attempt(runs, row):
    return next((run for run in runs if str(_mapping(run.run_request).get("roo_points_ledger_id") or "") == str(row.pk)), None)


def is_established_publisher(organization, config=None) -> bool:
    """True for a startup that is past its first article (and so is not a new founder).

    It has a verified (connected or paused) website, a written or completed
    article that was not the free one, or a paid article charge. The same rule
    defines the cohort that keeps today's flow.
    """
    from content_factory.models import ContentFactoryJob, OrganizationContentConfig, WrittenArticle
    from workflow_runs.models import ContentFactoryRun
    from .portable_drafts import has_connected_website
    from .run_state import ARTICLE_WORKFLOWS
    if config is None:
        config = OrganizationContentConfig.objects.filter(organization=organization).select_related("website_connection").first()
    if has_connected_website(config):
        return True
    if WrittenArticle.objects.filter(organization=organization).exists():
        return True
    runs = ContentFactoryRun.objects.filter(organization=organization, workflow__in=ARTICLE_WORKFLOWS)
    if runs.filter(run_request__roo_points_billing_status__in=["charged", "reused"]).exists():
        return True
    if any(not entitled_request(request) for request in runs.filter(status="completed").values_list("run_request", flat=True)[:50]):
        return True
    paid_jobs = ContentFactoryJob.objects.filter(domain__iexact=organization.domain, billing_status__in=["charged", "reused"])
    return any(_mapping(meta).get("roo_points_action") not in TOPIC_ACTIONS
        for meta in paid_jobs.values_list("request_meta", flat=True)[:50])


def _topic_batch_free(organization, user, rows: _Rows, *, now, established: bool) -> bool:
    if established or (rows.topics is not None and (not rows.topics_released or rows.topics_retry is not None)):
        return False
    return not _account_limited(user, topics=True, now=now)


def entitlement_state(organization, user, *, config=None, now=None) -> dict:
    """Describe this startup's free first article for the requesting account.

    Returns the snake_case source of ``articleCapabilities.firstArticle``:
    status available|in_progress|used|unavailable, and ``limit`` naming why it
    is unavailable (disabled, established, attempts, account or daily_cap).
    """
    state = {"version": 1, "status": "unavailable", "free": False, "attempts_used": 0,
        "attempts_allowed": ATTEMPTS_ALLOWED, "topic_batch_free": False, "run_id": None, "limit": None}
    if not free_first_article_enabled() or organization is None or not getattr(organization, "pk", None):
        return {**state, "limit": "disabled"}
    now = now or timezone.now()
    rows = _rows(organization.pk)
    state["attempts_used"] = len(rows.attempts)
    runs = _original_runs(organization.pk)
    delivered = next((run for run in runs if _delivered(run)), None)
    if rows.consumed is not None or delivered is not None:
        # A late delivery of a released attempt still counts as the free article.
        run = delivered or _run_for_attempt(runs, next((row for row in rows.attempts.values()
            if row.reference_id == rows.consumed.reference_id), rows.consumed))
        return {**state, "status": "used", "run_id": getattr(run, "run_id", None)}
    established = is_established_publisher(organization, config)
    state["topic_batch_free"] = _topic_batch_free(organization, user, rows, now=now, established=established)
    live = [row for number, row in sorted(rows.attempts.items()) if rows.attempt_state(number) == "reserved"]
    if live:
        run = _run_for_attempt(runs, live[-1])
        return {**state, "status": "in_progress", "run_id": getattr(run, "run_id", None)}
    if established:
        return {**state, "limit": "established"}
    if len(rows.attempts) >= ATTEMPTS_ALLOWED:
        return {**state, "limit": "attempts"}
    if _account_limited(user, topics=False, now=now):
        return {**state, "limit": "account"}
    if _daily_cap_reached(now):
        return {**state, "limit": "daily_cap"}
    return {**state, "status": "available", "free": True}


def reserve_allowed(data, config) -> bool:
    """Reserve only for a 0 quote on a confirmed copy-ready draft (or a verified site).

    Older desktops default to review_draft; their 0 quote must not spend an attempt.
    """
    from .portable_drafts import has_connected_website
    data = _mapping(data)
    expected = data.get("expected_cost_points", data.get("expectedCostPoints"))
    if isinstance(expected, bool) or expected != 0:
        return False
    content_only = data.get("delivery_mode") == "content_only" and data.get("delivery_mode_confirmed") is True
    return content_only or has_connected_website(config)


def quote_is_free(*, organization, user, client_request_id="", data=None, config=None) -> bool:
    """Whether this article start is quoted at 0: a replay of a live attempt, or a fresh free one."""
    if not free_first_article_enabled() or not reserve_allowed({**_mapping(data), "expected_cost_points": 0}, config):
        return False
    if client_request_id:
        rows = _rows(organization.pk)
        number, _row = rows.attempt_for_reference(_reference(client_request_id))
        if number is not None:
            return rows.attempt_state(number) != "released"
    return entitlement_state(organization, user, config=config)["free"]


def reserve_entitlement(*, organization, user, client_request_id, data, config):
    """Reserve the next free attempt for this request, or return None to quote the price.

    Runs under the organisation (and account) lock inside the caller's charge
    transaction, so the row rolls back with the dispatch reservation. A replay
    or transport retry of the same request id returns its existing row.
    """
    if not free_first_article_enabled() or not reserve_allowed(data, config):
        return None
    reference = _reference(client_request_id)
    with transaction.atomic():
        _lock(organization.pk, user)
        rows = _rows(organization.pk)
        number, existing = rows.attempt_for_reference(reference)
        if existing is not None:
            return None if rows.attempt_state(number) == "released" else existing
        if not entitlement_state(organization, user, config=config)["free"]:
            return None
        number = len(rows.attempts) + 1
        row = _write(organization_id=organization.pk, suffix=f"attempt:{number}", user=user, reference_id=reference)
    logger.info("first_article_entitlement_reserved org_id=%s attempt=%s ledger_id=%s", organization.pk, number, row.pk)
    transaction.on_commit(lambda: _check_spike(organization), robust=True)
    return row


def consume_entitlement(*, organization, run) -> bool:
    """Consume the original attempt this run started; idempotent and never after a release."""
    organization_id = _organization_id(organization, run)
    request = _mapping(getattr(run, "run_request", None))
    if organization_id is None or not original_entitled_request(request):
        return False
    with transaction.atomic():
        rows, number, attempt = _lock_attempt(organization_id, request)
        if attempt is None:
            return False
        state = rows.attempt_state(number)
        if state != "reserved":
            # A delivery after release writes nothing: the state still reports
            # "used" from the delivered run, so no further free attempt is offered.
            return state == "consumed"
        if rows.consumed is not None:
            return False
        _write(organization_id=organization_id, suffix="consumed", user=attempt.user, reference_id=attempt.reference_id)
    logger.info("first_article_entitlement_consumed org_id=%s attempt=%s run_id=%s", organization_id, number,
        getattr(run, "run_id", ""))
    return True


def release_entitlement(*, organization, run, reason) -> bool:
    """Release the original attempt after a stop without delivery; True only the first time."""
    return release_entitlement_for_request(organization=_organization_id(organization, run),
        request=getattr(run, "run_request", None), reason=reason, run_id=getattr(run, "run_id", ""))


def release_entitlement_for_request(*, organization, request, reason, run_id="") -> bool:
    """Release by the original request (refund paths that hold no run)."""
    organization_id = _organization_id(organization)
    request = _mapping(request)
    if organization_id is None or request.get("original_billing_source_run_id"):
        return False
    if request.get("roo_points_ledger_id") and not original_entitled_request(request):
        return False
    with transaction.atomic():
        rows, number, attempt = _lock_attempt(organization_id, request)
        if attempt is None or rows.attempt_state(number) != "reserved":
            return False
        _write(organization_id=organization_id, suffix=f"attempt:{number}:released", user=attempt.user,
            reference_id=attempt.reference_id)
    logger.info("first_article_entitlement_released org_id=%s attempt=%s run_id=%s reason=%s", organization_id,
        number, run_id, str(reason or "")[:120])
    return True


def settle_entitlement_for_run(run) -> None:
    """Consume the free article once its original run has delivered a draft (idempotent)."""
    if run is None or not original_entitled_request(getattr(run, "run_request", None)) or not _delivered(run):
        return
    consume_entitlement(organization=None, run=run)


def writing_started(run) -> bool:
    """True once the run has left queueing and research for a writing step."""
    step = str(getattr(run, "current_step", "") or "")
    return (str(getattr(run, "status", "") or "") not in NOT_STARTED_STATUSES
        and (step in WRITING_STEPS or step.startswith(WRITING_STEP_PREFIXES)))


def settle_owner_cancel(run) -> None:
    """An owner stop releases the attempt before writing, and consumes it once writing started.

    Call before the local cancel overwrites the run's status and current step.
    """
    if run is None or not original_entitled_request(getattr(run, "run_request", None)):
        return
    if _delivered(run) or writing_started(run):
        consume_entitlement(organization=None, run=run)
    else:
        release_entitlement(organization=None, run=run, reason="owner_cancelled")


def entitlement_payload(run) -> dict:
    """Client fields for a run: the state of the entitlement covering it, if any."""
    request = _mapping(getattr(run, "run_request", None))
    organization_id = getattr(run, "organization_id", None)
    stamp = str(request.get("roo_points_ledger_id") or "").strip()
    if not entitled_request(request) or not organization_id or not stamp.isdecimal():
        return {"entitlementStatus": None, "entitlementRetryAvailable": False}
    rows = _rows(organization_id)
    attempt = next(((number, row) for number, row in rows.attempts.items() if str(row.pk) == stamp), None)
    if attempt is None:
        return {"entitlementStatus": None, "entitlementRetryAvailable": False}
    status = rows.attempt_state(attempt[0])
    retry = False
    if status == "released":
        from organizations.models import Organization
        organization = Organization.objects.filter(pk=organization_id).first()
        retry = bool(organization and entitlement_state(organization, attempt[1].user)["free"])
    return {"entitlementStatus": status, "entitlementRetryAvailable": retry}


def topic_batch_free(organization, user) -> bool:
    """Whether this startup's next topic batch is the free first one."""
    if not free_first_article_enabled() or organization is None:
        return False
    return entitlement_state(organization, user)["topic_batch_free"]


def grant_topic_batch(*, organization, user, client_request_id, requested_topic_count):
    """Grant the free first topic batch (up to four topics, once, plus one free re-run).

    Call inside the topic charge transaction so the row rolls back with it.
    Returns the grant row, or None when the batch is not free.
    """
    try:
        count = int(requested_topic_count)
    except (TypeError, ValueError):
        count = FREE_TOPIC_BATCH_MAX
    if not free_first_article_enabled() or count > FREE_TOPIC_BATCH_MAX:
        return None
    reference = _reference(client_request_id)
    with transaction.atomic():
        _lock(organization.pk, user)
        rows = _rows(organization.pk)
        for row in (rows.topics, rows.topics_retry):
            if row is not None and row.reference_id == reference:
                return row
        if not entitlement_state(organization, user)["topic_batch_free"]:
            return None
        suffix = "topics" if rows.topics is None else "topics:retry"
        row = _write(organization_id=organization.pk, suffix=suffix, user=user, reference_id=reference)
    logger.info("first_article_topic_batch_granted org_id=%s grant=%s ledger_id=%s", organization.pk, suffix, row.pk)
    return row


def release_topic_batch(*, client_request_id, organization=None) -> bool:
    """Record that the free first topic batch never ran, allowing one free re-run."""
    from roo.models import Ledger
    reference = _reference(client_request_id)
    grants = Ledger.objects.filter(reference_type=REFERENCE_TYPE, reference_id=reference,
        idempotency_key__endswith=":topics")
    organization_id = _organization_id(organization)
    if organization_id is not None:
        grants = grants.filter(idempotency_key=_key(organization_id, "topics"))
    grant = grants.first()
    match = _KEY.match(getattr(grant, "idempotency_key", "") or "")
    if grant is None or match is None:
        return False
    organization_id = int(match.group(1))
    with transaction.atomic():
        Ledger.objects.select_for_update().filter(pk=grant.pk).first()
        rows = _rows(organization_id)
        if rows.topics_released or rows.topics is None or rows.topics.pk != grant.pk:
            return False
        _write(organization_id=organization_id, suffix="topics:released", user=grant.user, reference_id=reference)
    logger.info("first_article_topic_batch_released org_id=%s", organization_id)
    return True


def entitled_authorization(*, domain, action, ledger) -> dict:
    """The run_request billing stamp for work the entitlement covers (cost 0)."""
    from .billing import build_roo_points_authorization_payload
    return build_roo_points_authorization_payload(domain=domain, action=action, cost_points=0, required_points=0,
        billing_status=BILLING_STATUS, ledger_id=getattr(ledger, "pk", ledger), entitlement=ENTITLEMENT)


def lineage_entitlement(run):
    """The live (reserved or consumed) attempt that covers this run's article, else None.

    Child runs carry their parent's stamp, so the stamped row identifies the
    lineage. A released attempt covers nothing more.
    """
    request = _mapping(getattr(run, "run_request", None))
    organization_id = getattr(run, "organization_id", None)
    stamp = str(request.get("roo_points_ledger_id") or "").strip()
    if not entitled_request(request) or not organization_id or not stamp.isdecimal():
        return None
    rows = _rows(organization_id)
    attempt = next(((number, row) for number, row in rows.attempts.items() if str(row.pk) == stamp), None)
    if attempt is None or rows.attempt_state(attempt[0]) == "released":
        return None
    return attempt[1]


def _queue_alert(*, organization, alert_reason, run=None) -> Optional[str]:
    """Hand an ops alert to the founder-journey sweep, or log it while that is absent."""
    try:
        from .founder_journey_alerts import queue_founder_journey_alert
    except ImportError:
        # The alert sweep ships separately; until it merges the signal is a log line.
        logger.warning("founder_journey_alert alert_reason=%s org_id=%s run_id=%s state=unconfigured", alert_reason,
            getattr(organization, "pk", organization), getattr(run, "run_id", ""))
        return None
    return queue_founder_journey_alert(organization=organization, alert_reason=alert_reason, run=run)


def note_attempts_exhausted(organization) -> None:
    """A start after both free attempts were used: quote the price and tell ops once."""
    _queue_alert(organization=organization, alert_reason="first_article_attempts_exhausted")


def _check_spike(organization) -> None:
    threshold = int(getattr(settings, "ARTICLES_FREE_FIRST_ARTICLE_SPIKE_THRESHOLD", 0) or 0)
    if threshold <= 0:
        return
    count = _recent_reservations(timezone.now() - timedelta(hours=1))
    if count and count % threshold == 0:
        logger.warning("first_article_entitlement_spike reservations_last_hour=%s threshold=%s", count, threshold)
        _queue_alert(organization=organization, alert_reason="free_cap_spike")


def request_namespace(request, *, organization_id=None, run_id=""):
    """A run-shaped view of a saved request, for refund paths that hold no run."""
    return SimpleNamespace(run_request=_mapping(request), organization_id=organization_id, run_id=run_id)
