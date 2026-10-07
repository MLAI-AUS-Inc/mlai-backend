"""Bind provisional dispatch-token runs to their real Content Factory run ids.

When a Content Factory dispatch response is lost (read timeout, dropped
connection) and the by-key lookup cannot immediately resolve it, mlai creates a
provisional ContentFactoryRun whose ``run_id`` IS the dispatch idempotency key
(``client_request_id``) instead of inventing an untraceable ``vibe-marketing-*``
id. The first signal that carries the real remote run_id — a callback stamped
with ``client_request_id`` or a poll-time key lookup — binds the provisional
record to the real run so the two never diverge into a ghost pair.

``ContentFactoryRun.run_id`` is a unique CharField, not the primary key
(``ContentFactoryRunStep`` FKs the integer pk), so the bind is a single in-place
rename that keeps steps, pk, and history intact.
"""
import logging
from copy import deepcopy

from django.db import transaction

from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus
from content_factory.editorial_run_state import BRIEF_KEYS, merge_editorial_run_snapshot

logger = logging.getLogger(__name__)


def run_is_dispatch_token_keyed(run) -> bool:
    """True for a provisional run still keyed by its dispatch token: the local
    run_id equals the client_request_id the dispatch was sent with."""
    if run is None:
        return False
    run_request = run.run_request if isinstance(run.run_request, dict) else {}
    token = str(run_request.get("client_request_id") or "").strip()
    return bool(token and token == str(run.run_id or "").strip())


def bind_dispatch_token_run(*, client_request_id, remote_run_id):
    """Bind the provisional run keyed by ``client_request_id`` to ``remote_run_id``.

    Returns the bound run, or None when there is nothing to bind. When a run
    row for the real id already exists (a callback materialized it before the
    bind), the provisional placeholder's request payload (billing lineage,
    client_request_id) is merged into it and the placeholder is deleted —
    keeping exactly one local record per remote run. Conflicting editorial
    identity leaves both records intact and returns None; callers retain their
    existing best-effort callback/poll behavior, not new publication authority.
    """
    token = str(client_request_id or "").strip()
    real = str(remote_run_id or "").strip()
    if not token or not real or token == real:
        return None
    try:
        with transaction.atomic():
            token_run = (
                ContentFactoryRun.objects.select_for_update().filter(run_id=token).first()
            )
            if token_run is None or not run_is_dispatch_token_keyed(token_run):
                return None
            existing_real = ContentFactoryRun.objects.select_for_update().filter(run_id=real).first()
            if existing_real is not None:
                real_org = getattr(existing_real, "organization_id", None)
                token_org = getattr(token_run, "organization_id", None)
                if ((real_org is not None and token_org is not None and real_org != token_org)
                        or (existing_real.domain and token_run.domain
                            and existing_real.domain.lower().strip() != token_run.domain.lower().strip())):
                    return None
                _merge_provisional_into_real(token_run, existing_real)
                token_run.delete()
                logger.warning(
                    "content_factory_dispatch_token_merged token=%s run_id=%s",
                    token,
                    real,
                )
                _rebind_billing_job(token, real)
                return existing_real
            token_run.run_id = real
            # The provisional record carries a blocked "dispatch unconfirmed"
            # verdict; reset to queued so the next remote sync/callback is
            # authoritative (local FAILED/BLOCKED otherwise refuses remote
            # RUNNING states).
            token_run.status = ContentFactoryRunStatus.QUEUED
            token_run.current_step = "queued"
            token_run.error = ""
            token_run.result = {}
            token_run.save(update_fields=["run_id", "status", "current_step", "error", "result", "updated_at"])
        logger.warning(
            "content_factory_dispatch_token_bound token=%s run_id=%s workflow=%s",
            token,
            real,
            token_run.workflow,
        )
        _rebind_billing_job(token, real)
        return token_run
    except Exception:  # pragma: no cover - binding must never break callback/poll ingestion
        logger.warning(
            "content_factory_dispatch_token_bind_failed token=%s run_id=%s",
            token,
            real,
            exc_info=True,
        )
        return None


def reserve_portable_dispatch_intent(*, organization, workflow, actor_id, payload):
    """Persist admitted draft intent before a worker can mirror its first status."""
    from .portable_drafts import PORTABLE_DISPATCH_RESERVATION, original_portable_run
    from .website_contract import WebsiteAuthorityError
    key = str(payload.get("client_request_id") or "").strip()
    if not key or workflow not in {"article_generation", "direct_generate", "confirmed_topic"}:
        raise WebsiteAuthorityError("portable_dispatch_intent_changed", "This draft request is no longer current. Start a new reviewed attempt.")
    intent = {**deepcopy(payload), PORTABLE_DISPATCH_RESERVATION: True, "dispatch_pending_resolution": True}
    with transaction.atomic():
        run = ContentFactoryRun.objects.select_for_update().filter(
            organization=organization, domain=organization.domain,
            run_request__client_request_id=key,
        ).exclude(run_id=key).first()
        created = False
        if run is None:
            run, created = ContentFactoryRun.objects.select_for_update().get_or_create(run_id=key, defaults={
                "organization": organization, "domain": organization.domain, "workflow": workflow,
                "slack_user_id": actor_id, "github_repo": "", "status": ContentFactoryRunStatus.QUEUED,
                "current_step": "queued", "run_request": intent,
            })
        saved = run.run_request or {}
        identity_fields = ("client_request_id", "topic", "target_keyword", "source_run_id", "author_id", "editorial_brief")
        if (not key or not original_portable_run(run) or run.organization_id != organization.pk
                or run.workflow not in {"article_generation", "direct_generate", "confirmed_topic"} or run.slack_user_id != actor_id
                or (not created and run.run_id == key and (run.status not in {ContentFactoryRunStatus.QUEUED, ContentFactoryRunStatus.BLOCKED}
                    or not saved.get(PORTABLE_DISPATCH_RESERVATION) or not saved.get("dispatch_pending_resolution")))
                or any(saved.get(field) != payload.get(field) for field in identity_fields)):
            raise WebsiteAuthorityError("portable_dispatch_intent_changed", "This draft request is no longer current. Start a new reviewed attempt.")
    return run


def bind_portable_dispatch_snapshot(*, remote_run_id, payload):
    """Adopt a worker identity only after validating its reserved portable scope."""
    from .portable_drafts import PORTABLE_DISPATCH_RESERVATION, portable_dispatch_snapshot_allowed, portable_run_update_allowed
    request = payload.get("run_request") if isinstance(payload, dict) else None
    key = str(request.get("client_request_id") or "").strip() if isinstance(request, dict) else ""
    if not key:
        return None
    with transaction.atomic():
        original = ContentFactoryRun.objects.select_for_update().filter(run_id=key).first()
        if not portable_dispatch_snapshot_allowed(original, remote_run_id, payload):
            return None
        existing = ContentFactoryRun.objects.select_for_update().filter(run_id=remote_run_id).first()
        if existing is not None and (existing.organization_id != original.organization_id
                or not portable_run_update_allowed(existing, payload)):
            return None
        bound = bind_dispatch_token_run(client_request_id=key, remote_run_id=remote_run_id)
        if bound is not None:
            bound.run_request = dict(bound.run_request or {})
            bound.run_request.pop(PORTABLE_DISPATCH_RESERVATION, None)
            bound.run_request.pop("dispatch_pending_resolution", None)
            bound.save(update_fields=["run_request", "updated_at"])
        return bound


def _merge_provisional_into_real(token_run, real_run) -> None:
    """Copy what only the provisional record knows (the dispatch payload with
    its billing lineage) onto the callback-materialized run, without touching
    remote-authoritative fields."""
    update_fields = []
    token_request = token_run.run_request if isinstance(token_run.run_request, dict) else {}
    real_request = real_run.run_request if isinstance(real_run.run_request, dict) else {}
    has_editorial_decision = any(
        key in request and request[key] is not None
        for request in (token_request, real_request) for key in BRIEF_KEYS
    )
    if has_editorial_decision:
        snapshot = merge_editorial_run_snapshot(
            {"workflow": token_run.workflow, "domain": token_run.domain, "run_request": token_request},
            {"workflow": real_run.workflow or token_run.workflow,
             "domain": real_run.domain or token_run.domain, "run_request": real_request},
        )
        # Preserve token-only dispatch/billing context as well as remote-only
        # fields, after checking the immutable brief and key for conflicts.
        merged_request = {**deepcopy(token_request), **snapshot["run_request"]}
        merged_request.pop("editorialBrief", None)
        if merged_request != real_run.run_request:
            real_run.run_request = merged_request
            update_fields.append("run_request")
    elif not real_run.run_request and token_run.run_request:
        real_run.run_request = deepcopy(token_run.run_request)
        update_fields.append("run_request")
    for field in ("workflow", "domain", "github_repo", "slack_user_id"):
        if not getattr(real_run, field) and getattr(token_run, field):
            setattr(real_run, field, getattr(token_run, field))
            update_fields.append(field)
    if update_fields:
        update_fields.append("updated_at")
        real_run.save(update_fields=update_fields)


def _rebind_billing_job(token: str, real: str) -> None:
    """The web billing carrier (ContentFactoryJob) may have been stamped under
    the token id at dispatch time; move it to the real run id so the charge
    stays discoverable (revision flow reads it by run-id lineage)."""
    try:
        from content_factory.models import ContentFactoryJob

        if ContentFactoryJob.objects.filter(job_id=real).exists():
            return
        ContentFactoryJob.objects.filter(job_id=token).update(job_id=real)
    except Exception:  # pragma: no cover - best-effort billing continuity
        logger.warning(
            "content_factory_dispatch_token_job_rebind_failed token=%s run_id=%s",
            token,
            real,
            exc_info=True,
        )
