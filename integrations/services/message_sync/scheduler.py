"""Database-owned leases and budgets shared by every worker process.

A claim is one page in one conversation. Import priority never outranks owner
fairness; ordinary slots retain least-recently-served conversation rotation.
Checkpoints contain cursors and boundaries only.
"""

import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import connection, transaction
from django.db.models import BigIntegerField, Case, Exists, F, Max, Min, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from integrations.models import BridgeSyncJob, BridgeSyncState, BridgeWorkerHeartbeat

CHECKPOINT_KEYS = frozenset({
    "cursor", "latest", "oldest", "thread_ts", "next_latest", "scan_id",
    "upper_bound", "lower_bound", "authority_generation", "has_more", "phase",
    "observed_messages", "source_limited",
})


class LeaseLost(RuntimeError):
    """A superseded worker must discard its result instead of advancing state."""


class BudgetDeferred(RuntimeError):
    """The caller must reschedule without consuming a provider-failure attempt."""

    def __init__(self, seconds, *, before_request_method=""):
        self.retry_after = max(1, math.ceil(seconds))
        # Only durable admission sets this: a provider 429 already used a turn.
        self.before_request_method = before_request_method
        super().__init__("provider_budget_deferred")


@dataclass(frozen=True)
class JobLease:
    job_id: int
    state_id: int
    token: uuid.UUID
    authority_generation: int
    kind: str
    source_object_key: str
    checkpoint: dict
    previous_job_turn: datetime | None


def validate_checkpoint(checkpoint):
    if not isinstance(checkpoint, dict) or set(checkpoint) - CHECKPOINT_KEYS:
        raise ValueError("Checkpoint may contain only approved cursor/range fields")
    for value in checkpoint.values():
        if not isinstance(value, (str, int, bool, type(None))) or len(str(value)) > 1000:
            raise ValueError("Checkpoint contains an invalid cursor value")


def schedule_job(state, kind, *, source_object_key="", due_at=None):
    """Create recurring work without erasing a durable in-progress checkpoint."""
    if kind not in {"head", "archive", "thread", "authority"}:
        raise ValueError("Invalid sync job kind")
    job, _ = BridgeSyncJob.objects.get_or_create(
        state=state, kind=kind, source_object_key=source_object_key,
        defaults={"due_at": due_at or timezone.now()},
    )
    return job


def defer_quiet_history_jobs(conversation, *, delay_seconds=3600):
    """Defer a source-confirmed quiet mirror after its current authority check.

    Call inside discovery's grant/conversation transaction. Keep active leases,
    durable cursors, callback ingestion and delivery scheduling unchanged.
    """
    due_at = timezone.now() + timedelta(seconds=max(1, delay_seconds))
    state, _ = BridgeSyncState.objects.get_or_create(
        private_conversation=conversation,
        defaults={"workspace_id": conversation.slack_workspace_id,
                  "source_channel_id": conversation.slack_conversation_id},
    )
    for kind in ("head", "archive"):
        schedule_job(state, kind, due_at=due_at)
    return state.jobs.filter(
        kind__in=("head", "archive", "thread"), due_at__lt=due_at,
    ).filter(
        Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=timezone.now()),
    ).update(due_at=due_at)


def eligible_states():
    return BridgeSyncState.objects.filter(
        Q(public_channel__enabled=True)
        | Q(private_conversation__status="live",
            private_conversation__grant__status="active",
            private_conversation__grant__revoked_at__isnull=True),
    ).exclude(status__in=["paused", "revoked"])


def claim_job(*, kinds=None, lease_seconds=120, prefer_import=False):
    """Claim one bounded page, rotating workspaces, owners, then conversations.

    Lock the state as well as the job: two processes cannot run different job
    kinds concurrently against the same conversation or overwrite its cursor.
    """
    now = timezone.now()
    due = BridgeSyncJob.objects.filter(due_at__lte=now).filter(
        Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now),
    )
    if kinds is not None:
        due = due.filter(kind__in=kinds)
    active = BridgeSyncJob.objects.filter(state_id=OuterRef("pk"), lease_expires_at__gt=now)
    candidates = eligible_states().filter(
        Exists(due.filter(state_id=OuterRef("pk"))), ~Exists(active),
    )
    # Max across ALL states records the workspace's latest turn, including a
    # state whose next page is no longer due. Delivery has an independent turn. A busy workspace gets one turn
    # before the least-recently-served eligible workspace is considered again.
    workspaces = list(BridgeSyncJob.objects.filter(
        state__workspace_id__in=candidates.values("workspace_id"),
    ).values("state__workspace_id").annotate(served=Max("last_served_at")).order_by(
        F("served").asc(nulls_first=True), "state__workspace_id",
    ).values_list("state__workspace_id", flat=True))
    # Compute each owner's service turn once, rather than running the same
    # all-owner job scan for every candidate conversation. Conversation turns
    # remain small, state-indexed lookups inside the chosen owner's queue.
    candidates = candidates.annotate(
        owner_key=Coalesce("private_conversation__grant_id", Value(-1), output_field=BigIntegerField()),
        history_turn=Subquery(BridgeSyncJob.objects.filter(state_id=OuterRef("pk")).order_by(
            F("last_served_at").desc(nulls_last=True),
        ).values("last_served_at")[:1]),
    )
    state_order = []
    if prefer_import:
        from integrations.models import SlackDmMirrorConversation
        from .private_coverage import recent_conversations
        pending_private = recent_conversations(SlackDmMirrorConversation.objects.filter(
            pk=OuterRef("private_conversation_id"), history_backfilled_at__isnull=True,
        ))
        pending_public = due.filter(state_id=OuterRef("pk"), kind="archive", completed_at__isnull=True,
                                    state__public_channel__isnull=False)
        candidates = candidates.annotate(import_priority=Case(
            When(Exists(pending_private), then=Value(0)),
            When(Exists(pending_public), then=Value(0)), default=Value(1),
        ))
        state_order.append("import_priority")
    state_order.extend([F("history_turn").asc(nulls_first=True), "id"])
    for workspace_id in workspaces:
        with transaction.atomic():
            if connection.vendor == "postgresql":
                # Serialize the short claim decision, not source I/O. Without
                # this, simultaneous workers can all observe the same owner's
                # previous turn and claim different rooms from that owner.
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))", [f"message-sync-history:{workspace_id}"])
                    if not cursor.fetchone()[0]:
                        continue
            state = _claim_owner_state(candidates, workspace_id, state_order)
            if state is None:
                continue
            # Rotate head/archive/thread lanes before individual thread roots.
            # Thousands of old threads must not delay the next recent-head scan.
            lane = BridgeSyncJob.objects.filter(state=state, kind__in=due.filter(state=state).values("kind")).values("kind").annotate(
                served=Max("last_served_at"),
            ).order_by(F("served").asc(nulls_first=True), Case(When(kind="head", then=Value(0)), When(kind="archive", then=Value(1)), default=Value(2)), "kind").first()
            if prefer_import and state.import_priority == 0 and due.filter(state=state, kind="archive").exists():
                lane = {"kind": "archive"}
            if lane is None:
                continue
            job = due.filter(state=state, kind=lane["kind"]).select_for_update(skip_locked=True).order_by(
                F("last_served_at").asc(nulls_first=True), "due_at", "id",
            ).first()
            if job is None:
                continue
            previous_job_turn = job.last_served_at
            token = uuid.uuid4()
            # A caller may start before another worker but acquire the claim
            # lock afterwards. Record service order inside the serialized
            # decision so owner turns and lease lifetimes cannot run backwards.
            claimed_at = timezone.now()
            job.lease_token = token
            job.lease_expires_at = claimed_at + timedelta(seconds=max(1, lease_seconds))
            job.last_served_at = claimed_at
            job.attempts += 1
            job.save(update_fields=["lease_token", "lease_expires_at", "last_served_at", "attempts"])
            return JobLease(job.pk, state.pk, token, state.authority_generation,
                            job.kind, job.source_object_key, dict(job.checkpoint), previous_job_turn)
    return None


def _claim_owner_state(candidates, workspace_id, state_order):
    """Select an owner once under the workspace claim lock, then one room."""
    candidates = candidates.filter(workspace_id=workspace_id)
    owners = BridgeSyncJob.objects.filter(state__workspace_id=workspace_id).annotate(
        owner_key=Coalesce("state__private_conversation__grant_id", Value(-1), output_field=BigIntegerField()),
    ).filter(owner_key__in=candidates.values("owner_key")).values("owner_key").annotate(
        served=Max("last_served_at"), first_state=Min("state_id"),
    ).order_by(F("served").asc(nulls_first=True), "first_state", "owner_key")
    # Include ALL jobs belonging to eligible owners, including future-due jobs:
    # completing a page or exhausting a conversation must not refund its turn.
    for owner in list(owners):
        state = candidates.filter(owner_key=owner["owner_key"]).select_for_update(
            skip_locked=True, of=("self",),
        ).order_by(*state_order).first()
        if state is not None:
            return state
    return None


def locked_job(lease):
    """Validate a claim inside the transaction that persists its page effects."""
    state = eligible_states().select_for_update(of=("self",)).filter(
        pk=lease.state_id, authority_generation=lease.authority_generation,
    ).first()
    if state is None:
        raise LeaseLost("sync_authority_changed")
    job = BridgeSyncJob.objects.select_for_update().filter(
        pk=lease.job_id, state=state, lease_token=lease.token,
        lease_expires_at__gt=timezone.now(),
    ).first()
    if job is None:
        raise LeaseLost("sync_lease_lost")
    return state, job


def finish_job(lease, *, checkpoint, delay_seconds=0, complete=False):
    """Commit this page's cursor; completed thread work remains scheduled."""
    validate_checkpoint(checkpoint)
    with transaction.atomic():
        state, job = locked_job(lease)
        now = timezone.now()
        job.checkpoint = checkpoint
        job.due_at = now + timedelta(seconds=max(0, delay_seconds))
        job.lease_token = None
        job.lease_expires_at = None
        job.attempts = 0
        job.backoff_seconds = 0
        job.last_error_code = ""
        if complete:
            job.completed_at = now
        job.save()
        state.last_successful_scan_at = now
        state.last_error_code = ""
        state.save(update_fields=["last_successful_scan_at", "last_error_code"])


def fail_job(lease, *, error_code, retry_after=None):
    with transaction.atomic():
        state, job = locked_job(lease)
        delay = retry_after if retry_after is not None else min(3600, 2 ** min(job.attempts, 12))
        job.due_at = timezone.now() + timedelta(seconds=max(1, delay))
        job.backoff_seconds = max(1, math.ceil(delay))
        job.last_error_code = safe_error_code(error_code)
        if error_code == "invalid_cursor":
            # Provider cursors expire. Restart the bounded scan; idempotent
            # ingestion preserves already imported rows.
            job.checkpoint = {}
        if retry_after is None:
            from .coverage import failure_classification
            state.last_error_code = job.last_error_code
            state.status = "retrying"
            ranges = dict(state.verified_ranges or {})
            ranges[lease.kind] = {**ranges.get(lease.kind, {}), "classification": failure_classification(error_code), "absence": "unknown"}
            state.verified_ranges = ranges
            state.save(update_fields=["status", "last_error_code", "verified_ranges"])
        job.lease_token = None
        job.lease_expires_at = None
        if retry_after is not None:
            job.attempts = max(0, job.attempts - 1)
            # Admission denied is not a served turn. Otherwise a fixed quota
            # cadence can repeatedly favour the same subset of conversations.
            job.last_served_at = lease.previous_job_turn
        job.save()


def safe_error_code(value):
    """Store machine codes only, never provider exceptions containing bodies."""
    value = str(value)
    return value if re.fullmatch(r"[A-Za-z0-9_]{1,100}", value) else "sync_error"


def heartbeat(worker_id, lane, *, completed=0, failed=0, error_code=""):
    row, _ = BridgeWorkerHeartbeat.objects.get_or_create(worker_id=worker_id, lane=lane)
    values = dict(heartbeat_at=timezone.now(), completed_count=F("completed_count") + completed,
                  failed_count=F("failed_count") + failed, last_error_code=safe_error_code(error_code) if error_code else "")
    if completed:
        values["progressed_at"] = timezone.now()
    BridgeWorkerHeartbeat.objects.filter(pk=row.pk).update(**values)
