"""P1.9: "Your draft is ready", once per article run, for new-journey founders (IF-P1-11).

The website goal outbox (``website_notification_outbox``) needs a website
connection and goal enrolment. A new-journey founder usually has neither, so
this outbox is keyed on the run alone and stores its once-only rows as
``ContentFactoryCallbackEvent`` leases (no migration):

- ``event_id`` ``draft-ready:{org_id}:{run_id}:email`` (and ``...:dm``),
- ``event_type`` ``founder_draft_ready``, ``job_id`` the run id,
- ``claimed_at`` the drain lease, ``processed_at`` once the channel is done.

:func:`enqueue_ready_drafts` and :func:`drain` are the two minute sweeps
(``founder_journey_sweeps.SWEEPS``). Both do nothing unless
``ARTICLES_DRAFT_READY_NOTIFICATIONS_ENABLED`` and V2 are on, and only
new-journey organisations (``founder_journey_cohort.is_new_journey``) ever get
a row, so the legacy cohort never receives anything new.

Email goes through Resend with ``Idempotency-Key: articles-draft-ready/{run_id}``
to the founder who started the run (falling back to the startup owner). The
Chat DM reuses the goal notifier's transport (``open_dm`` and ``post_event``)
with a deterministic message event, so a replay is the same message. A replay
or a second sweep therefore sends nothing new.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta
from email.utils import formataddr, parseaddr
from urllib.parse import quote, urlencode
from uuid import UUID

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from . import founder_journey_cohort as cohort

logger = logging.getLogger(__name__)

FLAG = "ARTICLES_DRAFT_READY_NOTIFICATIONS_ENABLED"
EVENT_TYPE = "founder_draft_ready"
CHANNELS = ("email", "dm")
# A draft that became ready more than this long ago is never announced.
READY_WINDOW = timedelta(hours=48)
# Resend keeps an idempotency key for 24 hours; stop retrying before then.
RETRY_WINDOW = timedelta(hours=23)
CLAIM_LEASE = timedelta(minutes=10)
ENQUEUE_LIMIT = 500
DRAIN_LIMIT = 25
MAX_DM_RECIPIENTS = 8
# Article runs that announce a draft. A revision child is a change the
# founder asked for on the open page, so it is not announced again.
ANNOUNCED_WORKFLOWS = ("article_generation", "confirmed_topic", "content_factory_article", "direct_generate")
READY_STATUSES = ("completed", "awaiting_approval", "approval_required")

# Founder copy (contract section 11.1). No codes, no dashes.
SUBJECT = "Your draft is ready"
BODY = "Tap to review it and copy it to your website."
SENDER_NAME = "MLAI"
DEFAULT_SENDER_ADDRESS = "notifications@mlai.au"


def enabled() -> bool:
    """True when ``ARTICLES_DRAFT_READY_NOTIFICATIONS_ENABLED`` and V2 are both on."""
    return bool(getattr(settings, FLAG, False)) and cohort.v2_enabled()


def can_notify(organization, user) -> bool:
    """Whether a draft-ready email can reach ``user`` for this organisation.

    The flag is on, the organisation is new-journey, Resend is configured and
    the user has an email. be-journey reports it as ``draftReadyNotifications``.
    """
    return bool(enabled() and organization is not None and cohort.is_new_journey(organization)
                and str(getattr(settings, "RESEND_API_KEY", "") or "")
                and str(getattr(user, "email", "") or "").strip())


def event_id(organization_id, run_id, channel) -> str:
    """The once-only row id for one run and channel, always under 100 characters."""
    value = f"draft-ready:{organization_id}:{run_id}:{channel}"
    if len(value) < 100:
        return value
    digest = hashlib.sha256(f"{organization_id}:{run_id}".encode()).hexdigest()[:48]
    return f"draft-ready:{digest}:{channel}"


def _channel_of(row) -> str:
    return str(row.event_id).rsplit(":", 1)[-1]


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _joined_at(marker):
    """When the organisation joined the new journey: drafts ready before it are never announced."""
    from django.utils.dateparse import parse_datetime
    for key in ("optedInAt", "assignedAt"):
        raw = _mapping(marker).get(key)
        if isinstance(raw, str) and raw:
            try:
                value = parse_datetime(raw)
            except ValueError:
                value = None
            if value is not None and not timezone.is_naive(value):
                return value
    return None


def handled_by_founder(config, run_id) -> bool:
    """True once the founder copied the draft or posted its link (IF-P1-13)."""
    strategy = getattr(config, "pillar_strategy", None)
    posted = _mapping(_mapping(strategy).get(cohort.POSTED_ARTICLES_KEY))
    record = _mapping(posted.get(str(run_id)))
    return bool(record.get("copiedAt") or record.get("url"))


def _draft_deleted(run) -> bool:
    from .run_cancellation import DRAFT_DELETED_AT_KEY
    return bool(_mapping(run.result).get(DRAFT_DELETED_AT_KEY))


def _announceable(run, now) -> bool:
    return (run.workflow in ANNOUNCED_WORKFLOWS and run.status in READY_STATUSES
            and run.created_at is not None and run.created_at >= now - READY_WINDOW and not _draft_deleted(run))


def _cohort_and_config(organization, cache):
    """``(is_new_journey, config)`` for the organisation, memoised per sweep."""
    from .models import OrganizationContentConfig
    if organization.pk not in cache:
        config = OrganizationContentConfig.objects.filter(organization_id=organization.pk).first()
        cache[organization.pk] = (cohort.is_new_journey(organization, config), config)
    return cache[organization.pk]


def enqueue_ready_drafts(now=None) -> dict:
    """Create one row per channel for every new-journey draft that became ready in the last 48 hours.

    Uses ``get_or_create`` on the unique ``event_id``, so a second sweep adds
    nothing. Legacy organisations, runs from before the organisation joined the
    new journey, drafts the founder already copied or posted, discarded drafts
    and revision children are skipped.
    """
    from workflow_runs.models import ContentFactoryRun
    from .models import ContentFactoryCallbackEvent
    if not enabled():
        return {"status": "disabled"}
    now = now or timezone.now()
    runs = list(ContentFactoryRun.objects.select_related("organization")
                .filter(workflow__in=ANNOUNCED_WORKFLOWS, status__in=READY_STATUSES,
                        created_at__gte=now - READY_WINDOW, organization__isnull=False)
                .order_by("-created_at")[:ENQUEUE_LIMIT])
    configs, wanted = {}, []
    for run in runs:
        organization = run.organization
        new_journey, config = _cohort_and_config(organization, configs)
        if not new_journey:
            continue
        joined = _joined_at(cohort.read_marker(organization, config))
        if joined is not None and run.created_at < joined:
            continue
        if not _announceable(run, now) or handled_by_founder(config, run.run_id):
            continue
        wanted.extend((event_id(organization.pk, run.run_id, channel), run) for channel in CHANNELS)
    existing = set(ContentFactoryCallbackEvent.objects.filter(
        event_id__in=[identifier for identifier, _ in wanted]).values_list("event_id", flat=True))
    created = 0
    for identifier, run in wanted:
        if identifier in existing:
            continue
        _, was_created = ContentFactoryCallbackEvent.objects.get_or_create(
            event_id=identifier,
            defaults={"job_id": str(run.run_id)[:100], "event_type": EVENT_TYPE, "emitted_at": now})
        created += int(was_created)
    return {"status": "completed", "created": created}


# ---------------------------------------------------------------------------
# Recipients and copy


def _founder_company(user, organization):
    from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
    return (VibeRaisingCompany.objects.filter(organization_id=organization.pk, profile__user=user,
                                              profile__role=VibeRaisingProfile.ROLE_FOUNDER)
            .order_by("created_at").first())


def recipient(run, organization):
    """``(user, company)`` for the founder who started the run, else the startup owner, else ``(None, None)``.

    Only an active founder with a company on this organisation qualifies.
    """
    from django.contrib.auth import get_user_model
    from founder_tools.services import organization_owner_user_id
    requested = _mapping(run.run_request).get("roo_points_requested_by_user_id")
    candidates = []
    if isinstance(requested, int) and not isinstance(requested, bool) or str(requested or "").isdigit():
        candidates.append(int(requested))
    owner_id = organization_owner_user_id(organization)
    if owner_id is not None:
        candidates.append(owner_id)
    for user_id in dict.fromkeys(candidates):
        user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
        company = _founder_company(user, organization) if user else None
        if company is not None:
            return user, company
    return None, None


def draft_link(run_id, company_id) -> str:
    """``{COMMUNITY_CHAT_FRONTEND_URL}/my-startup/runs/<run_id>/draft?company_id=<uuid>``: ids only."""
    origin = str(getattr(settings, "COMMUNITY_CHAT_FRONTEND_URL", "") or "https://chat.mlai.au").strip().rstrip("/")
    return (f"{origin}/my-startup/runs/{quote(str(run_id), safe='')}/draft?"
            + urlencode({"company_id": str(UUID(str(company_id)))}))


def _sender() -> str:
    _, address = parseaddr(str(getattr(settings, "RESEND_FROM_EMAIL", "") or ""))
    return formataddr((SENDER_NAME, address if "@" in address else DEFAULT_SENDER_ADDRESS))


def email_payload(email, link) -> dict:
    """The whole Resend request body, frozen per send."""
    return {"from": _sender(), "to": [email], "subject": SUBJECT, "text": f"{BODY}\n\n{link}"}


def post_email(payload, run_id) -> str:
    """Send through Resend with ``Idempotency-Key: articles-draft-ready/{run_id}`` and return the message id.

    It mirrors ``website_notification_transport.post_email``, whose key prefix
    is fixed to the goal notices, and raises its ``NotificationTransportError``.
    """
    from . import website_notification_transport as transport
    from .website_connections import require_unlocked_remote_call
    from agent_runtime import native_crypto
    from agent_runtime.webhook_transport import CallbackEndpointError
    api_key = str(getattr(settings, "RESEND_API_KEY", "") or "")
    if not api_key:
        raise transport.NotificationTransportError("email_unconfigured", retryable=False)
    require_unlocked_remote_call()
    try:
        response = transport.post_webhook(
            "https://api.resend.com/emails", native_crypto.canonical_json(payload).encode(),
            {"Authorization": "Bearer " + api_key, "Idempotency-Key": f"articles-draft-ready/{run_id}"},
            timeout=10, max_response_bytes=16384)
        value = json.loads(response.body)
    except (CallbackEndpointError, ValueError) as exc:
        raise transport.NotificationTransportError("email_response_unavailable") from exc
    if (response.status in {408, 429} or response.status >= 500
            or response.status == 409 and isinstance(value, dict) and value.get("name") == "concurrent_idempotent_requests"):
        raise transport.NotificationTransportError("email_temporarily_unavailable")
    if response.status not in {200, 201} or not isinstance(value, dict):
        raise transport.NotificationTransportError("email_rejected", retryable=False)
    try:
        return str(UUID(value["id"]))
    except (ValueError, TypeError, KeyError) as exc:
        raise transport.NotificationTransportError("email_response_unavailable") from exc


def _devices(user):
    from community_chat.models import CommunityChatDevice, DeviceBindingStatus
    return list(CommunityChatDevice.objects.filter(
        user=user, status=DeviceBindingStatus.VERIFIED, revoked_at__isnull=True, verified_at__isnull=False,
    ).order_by("public_key").values_list("public_key", flat=True).distinct()[:MAX_DM_RECIPIENTS + 1])


def dm_event(channel_id, pubkeys, notice_id, link, created_at) -> dict:
    """One signed Chat message; the same inputs always give the same event id."""
    from agent_runtime import native_crypto
    from . import website_notification_transport as transport
    return native_crypto.sign_event(
        transport.notifier_key(), kind=9, created_at=created_at,
        tags=[["h", str(UUID(str(channel_id)))], ["mlai-draft-ready", notice_id]] + [["p", key] for key in sorted(pubkeys)],
        content=f"{SUBJECT}\n\n{BODY}\n\n{link}")


def _send(row, run, organization, user, company):
    """Send one channel. Returns ``"sent"`` or ``"skipped"``; raises ``NotificationTransportError``."""
    from . import website_notification_transport as transport
    link = draft_link(run.run_id, company.pk)
    if _channel_of(row) == "email":
        email = str(getattr(user, "email", "") or "").strip()
        if not email:
            return "skipped"
        post_email(email_payload(email, link), run.run_id)
        return "sent"
    devices = _devices(user)
    if not devices or len(devices) > MAX_DM_RECIPIENTS:
        return "skipped"
    notice_id = hashlib.sha256(row.event_id.encode()).hexdigest()
    channel_id = transport.open_dm(devices, notice_id)
    transport.post_event(dm_event(channel_id, devices, notice_id, link, int(row.created_at.timestamp())))
    return "sent"


def _claim(row, now) -> bool:
    from .models import ContentFactoryCallbackEvent
    return bool(ContentFactoryCallbackEvent.objects.filter(pk=row.pk, processed_at__isnull=True).filter(
        Q(claimed_at__isnull=True) | Q(claimed_at__lt=now - CLAIM_LEASE)).update(claimed_at=now))


def _finish(row, now):
    from .models import ContentFactoryCallbackEvent
    ContentFactoryCallbackEvent.objects.filter(pk=row.pk).update(processed_at=now)


def _release(row):
    from .models import ContentFactoryCallbackEvent
    ContentFactoryCallbackEvent.objects.filter(pk=row.pk, processed_at__isnull=True).update(claimed_at=None)


def _deliverable(run, now):
    """``(organization, config)`` when this run should still be announced, else ``(None, None)``."""
    from .models import OrganizationContentConfig
    organization = getattr(run, "organization", None) if run is not None else None
    if organization is None or not _announceable(run, now):
        return None, None
    config = OrganizationContentConfig.objects.filter(organization_id=organization.pk).first()
    if not cohort.is_new_journey(organization, config) or handled_by_founder(config, run.run_id):
        return None, None
    return organization, config


def drain(now=None) -> dict:
    """Claim unprocessed rows, send each channel once and mark it processed.

    A transient provider failure releases the claim for the next sweep, until
    23 hours after the row was created (the Resend idempotency window). A row
    whose run is gone, no longer new-journey, already copied or posted, or
    whose founder has no email or verified device is marked processed without
    sending.
    """
    from workflow_runs.models import ContentFactoryRun
    from . import website_notification_transport as transport
    from .models import ContentFactoryCallbackEvent
    if not enabled():
        return {"status": "disabled"}
    now = now or timezone.now()
    rows = list(ContentFactoryCallbackEvent.objects.filter(event_type=EVENT_TYPE, processed_at__isnull=True)
                .filter(Q(claimed_at__isnull=True) | Q(claimed_at__lt=now - CLAIM_LEASE))
                .order_by("created_at")[:DRAIN_LIMIT])
    counts = {"sent": 0, "skipped": 0, "retry": 0, "failed": 0}
    for row in rows:
        if not _claim(row, now):
            continue
        run = ContentFactoryRun.objects.select_related("organization").filter(run_id=row.job_id).first()
        organization, _config = _deliverable(run, now)
        user, company = recipient(run, organization) if organization is not None else (None, None)
        channel = _channel_of(row)
        if user is None:
            _finish(row, now)
            counts["skipped"] += 1
            continue
        try:
            outcome = _send(row, run, organization, user, company)
        except transport.NotificationTransportError as exc:
            if exc.retryable and row.created_at is not None and now - row.created_at < RETRY_WINDOW:
                _release(row)
                counts["retry"] += 1
            else:
                _finish(row, now)
                counts["failed"] += 1
                logger.warning("founder_draft_ready_failed org=%s run=%s channel=%s outcome=%s",
                               organization.pk, run.run_id, channel, exc.outcome)
            continue
        except Exception as exc:  # noqa: BLE001 - one row must never stop the sweep
            _release(row)
            counts["retry"] += 1
            logger.warning("founder_draft_ready_error org=%s run=%s channel=%s error=%s",
                           organization.pk, run.run_id, channel, type(exc).__name__)
            continue
        _finish(row, now)
        counts[outcome] += 1
        if outcome == "sent":
            logger.info("founder_draft_ready_sent org=%s run=%s channel=%s", organization.pk, run.run_id, channel)
    return {"status": "completed", **counts}
