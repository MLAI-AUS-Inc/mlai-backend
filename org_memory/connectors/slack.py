from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from startup_updates.models import SlackChannelSelection, SlackThreadArtifact

from .artifact_utils import (
    bounded_text,
    changed_after,
    content_hash,
    current_positions,
    cursor_position,
    decode_cursor,
    encoded_positions,
    estimate_tokens,
    parse_source_datetime,
    source_acl,
    source_removals,
    version_key,
)
from .base import (
    ConnectorHealth,
    DryRunResult,
    ScopeDescriptor,
    ScopePage,
    SourcePreview,
    SourceVersionPayload,
    SyncPage,
    TombstoneResult,
)


SLACK_KINDS = ("thread",)


def _page_size() -> int:
    return max(min(int(getattr(settings, "ORG_MEMORY_ARTIFACT_PAGE_SIZE", 100)), 500), 1)


def _quiet_seconds() -> int:
    return max(int(getattr(settings, "ORG_MEMORY_SLACK_THREAD_QUIET_SECONDS", 900)), 0)


def _chunk_target_chars() -> int:
    return max(
        min(int(getattr(settings, "ORG_MEMORY_SLACK_CHUNK_TARGET_CHARS", 6000)), 50000),
        500,
    )


def is_slack_dm_scope(external_id: str) -> bool:
    return str(external_id or "").upper().startswith("D")


def _metadata_is_dm(metadata) -> bool:
    if not isinstance(metadata, dict):
        return False
    channel_type = str(
        metadata.get("channel_type") or metadata.get("type") or ""
    ).lower()
    return bool(
        metadata.get("is_dm")
        or metadata.get("is_im")
        or metadata.get("is_mpim")
        or channel_type in {"im", "mpim", "direct_message"}
    )


def _selection_is_dm(selection) -> bool:
    return bool(
        is_slack_dm_scope(selection.channel_id)
        or _metadata_is_dm(selection.raw_payload)
    )


def _selected_scope_map(configuration, selected_scopes=None):
    scopes = list(
        selected_scopes
        if selected_scopes is not None
        else configuration.source_scopes.filter(selected=True, status="selected")
    )
    selections = {
        row.channel_id: row
        for row in SlackChannelSelection.objects.filter(
            connection=configuration.connection,
            channel_id__in=[str(scope.external_id or "") for scope in scopes],
        )
    }
    result = {}
    for scope in scopes:
        channel_id = str(scope.external_id or "").strip()
        if scope.scope_type != "channel" or not channel_id:
            raise ValueError("Slack memory supports selected channel scopes only.")
        selection = selections.get(channel_id)
        if (
            is_slack_dm_scope(channel_id)
            or _metadata_is_dm(scope.metadata)
            or (selection is not None and _selection_is_dm(selection))
            or (channel_id.upper().startswith("G") and selection is None)
        ):
            raise ValueError("Slack direct-message scopes cannot be selected for memory.")
        result[channel_id] = scope
    if not result:
        raise ValueError("Slack memory requires at least one selected channel scope.")
    return result


def _all_threads(configuration, scopes):
    queryset = SlackThreadArtifact.objects.filter(
        organization=configuration.organization,
        connection=configuration.connection,
        channel_id__in=tuple(scopes),
    )
    if configuration.historical_cutoff:
        queryset = queryset.filter(
            Q(latest_message_at__gte=configuration.historical_cutoff)
            | Q(
                latest_message_at__isnull=True,
                updated_at__gte=configuration.historical_cutoff,
            )
        )
    return queryset


def _ready_threads(configuration, scopes, *, include_active=False):
    queryset = _all_threads(configuration, scopes)
    if include_active:
        return queryset
    quiet_before = timezone.now() - timedelta(seconds=_quiet_seconds())
    return queryset.filter(
        Q(latest_message_at__lte=quiet_before)
        | Q(latest_message_at__isnull=True, updated_at__lte=quiet_before)
    )


def _slack_url(channel_id: str, thread_ts: str) -> str:
    compact_ts = str(thread_ts or "").replace(".", "")
    if not channel_id or not compact_ts:
        return ""
    return f"https://slack.com/archives/{channel_id}/p{compact_ts}"


def _payload_text(payload) -> str:
    if not isinstance(payload, dict):
        return ""
    return bounded_text(payload.get("cleaned_text") or payload.get("text"), 60000)


def _message_id(payload, ordinal: int) -> str:
    if not isinstance(payload, dict):
        return str(ordinal)
    return str(
        payload.get("message_id")
        or payload.get("slack_message_ts")
        or payload.get("message_ts")
        or payload.get("ts")
        or ordinal
    )[:512]


def _message_time(payload, fallback):
    if not isinstance(payload, dict):
        return fallback
    return (
        parse_source_datetime(payload.get("posted_at"))
        or parse_source_datetime(payload.get("message_ts"))
        or parse_source_datetime(payload.get("ts"))
        or fallback
    )


def _thread_chunks(thread):
    messages = []
    for ordinal, payload in enumerate(thread.message_payloads or []):
        if not isinstance(payload, dict):
            continue
        text = _payload_text(payload)
        if not text:
            continue
        author_name = bounded_text(
            payload.get("author_name") or payload.get("author_id") or "Slack user",
            255,
        )
        posted_at = _message_time(payload, thread.latest_message_at)
        timestamp = posted_at.isoformat() if posted_at else str(
            payload.get("message_ts") or payload.get("ts") or ""
        )
        messages.append(
            {
                "id": _message_id(payload, ordinal),
                "author_id": str(payload.get("author_id") or "")[:512],
                "author_name": author_name,
                "occurred_at": posted_at,
                "line": f"[{timestamp}] {author_name}: {text}" if timestamp else f"{author_name}: {text}",
            }
        )

    if not messages:
        text = bounded_text(thread.cleaned_text)
        if not text:
            text = "Slack thread contained no extractable text."
        messages = [
            {
                "id": str((thread.source_message_ids or [thread.thread_ts])[0])[:512],
                "author_id": "",
                "author_name": "",
                "occurred_at": thread.latest_message_at,
                "line": text,
            }
        ]

    chunks = []
    batch = []
    batch_chars = 0
    target = _chunk_target_chars()

    def flush():
        nonlocal batch, batch_chars
        if not batch:
            return
        chunk_text = "\n".join(item["line"] for item in batch).strip()
        participants = []
        for item in batch:
            participant = item["author_id"] or item["author_name"]
            if participant and participant not in participants:
                participants.append(participant)
        chunks.append(
            {
                "ordinal": len(chunks),
                "chunk_kind": "slack_thread_messages",
                "text": chunk_text,
                "token_count": estimate_tokens(chunk_text),
                "source_locator": {
                    "channel_id": thread.channel_id,
                    "thread_ts": thread.thread_ts,
                    "first_message_id": batch[0]["id"],
                    "last_message_id": batch[-1]["id"],
                    "start_occurred_at": (
                        batch[0]["occurred_at"].isoformat()
                        if batch[0]["occurred_at"]
                        else ""
                    ),
                    "end_occurred_at": (
                        batch[-1]["occurred_at"].isoformat()
                        if batch[-1]["occurred_at"]
                        else ""
                    ),
                    "participant_refs": participants[:100],
                },
                "occurred_at": batch[-1]["occurred_at"] or thread.latest_message_at,
            }
        )
        batch = []
        batch_chars = 0

    for message in messages:
        size = len(message["line"])
        if batch and batch_chars + size + 1 > target:
            flush()
        batch.append(message)
        batch_chars += size + 1
    flush()
    return tuple(chunks)


def _record_for(configuration, thread, scopes):
    scope = scopes.get(str(thread.channel_id))
    if scope is None or is_slack_dm_scope(thread.channel_id):
        return None
    chunks = _thread_chunks(thread)
    full_text = "\n\n".join(chunk["text"] for chunk in chunks)
    acl = source_acl(
        configuration,
        scope,
        revision_payload={
            "kind": "thread",
            "channel_id": thread.channel_id,
            "thread_ts": thread.thread_ts,
        },
    )
    payload = {
        "content": full_text,
        "message_ids": list(thread.source_message_ids or []),
        "acl": acl,
        "updated_at": thread.updated_at,
        "adapter": "slack-thread-v1",
    }
    participant_summary = (
        thread.participant_summary if isinstance(thread.participant_summary, dict) else {}
    )
    participant_count = len(participant_summary.get("participants") or [])
    title = bounded_text(
        f"#{thread.channel_name or thread.channel_id} thread {thread.thread_ts}",
        512,
    )
    return {
        "source_scope_id": scope.pk,
        "source_type": "slack_thread",
        "external_id": f"slack_thread:{thread.channel_id}:{thread.thread_ts}",
        "version_key": version_key(payload),
        "content_hash": content_hash(full_text),
        "classification": scope.default_classification,
        "acl": acl,
        "chunks": chunks,
        "canonical_url": _slack_url(thread.channel_id, thread.thread_ts),
        "title": title,
        "author_external_id": "",
        "source_created_at": (
            parse_source_datetime(chunks[0]["source_locator"]["start_occurred_at"])
            if chunks
            else None
        )
        or thread.created_at,
        "source_updated_at": thread.latest_message_at or thread.updated_at,
        "occurred_at": thread.latest_message_at or thread.updated_at,
        "bounded_excerpt": full_text[:4096],
        "metadata": {
            "record_type": "slack_thread",
            "channel_id": thread.channel_id,
            "thread_ts": thread.thread_ts,
            "source_message_count": int(thread.source_message_count or len(thread.source_message_ids or [])),
            "participant_count": participant_count,
            "authority_fields": ["informal_context", "discussion", "open_loops"],
        },
        "restore_access": bool(acl["is_accessible"]),
    }


def _expected_sources(configuration, scopes):
    return {
        ("slack_thread", f"slack_thread:{row.channel_id}:{row.thread_ts}")
        for row in _all_threads(configuration, scopes).only("channel_id", "thread_ts")
        if not is_slack_dm_scope(row.channel_id)
    }


class SlackArtifactMemoryConnector:
    provider = "slack"

    def discover_scopes(self, configuration, cursor=None) -> ScopePage:
        descriptors = [
            ScopeDescriptor(
                scope_type="channel",
                external_id=row.channel_id,
                name=row.channel_name or row.channel_id,
                canonical_url=f"https://slack.com/archives/{row.channel_id}",
                metadata={
                    "is_private": bool(row.is_private),
                    "legacy_selected": bool(row.selected),
                },
            )
            for row in SlackChannelSelection.objects.filter(
                connection=configuration.connection,
            ).order_by("channel_name", "channel_id")
            if not _selection_is_dm(row)
        ]
        if not descriptors:
            seen = set()
            for row in SlackThreadArtifact.objects.filter(
                organization=configuration.organization,
                connection=configuration.connection,
            ).order_by("channel_name", "channel_id"):
                if (
                    row.channel_id in seen
                    or is_slack_dm_scope(row.channel_id)
                    or str(row.channel_id).upper().startswith("G")
                ):
                    continue
                seen.add(row.channel_id)
                descriptors.append(
                    ScopeDescriptor(
                        scope_type="channel",
                        external_id=row.channel_id,
                        name=row.channel_name or row.channel_id,
                        canonical_url=f"https://slack.com/archives/{row.channel_id}",
                        metadata={"discovered_from": "slack_thread_artifact"},
                    )
                )
        return ScopePage(
            scopes=tuple(descriptors),
            warnings=("Slack direct messages are excluded from scope discovery.",),
        )

    def preview(self, configuration, selected_scopes, policy) -> SourcePreview:
        scopes = _selected_scope_map(configuration, selected_scopes)
        all_threads = _all_threads(configuration, scopes)
        ready_threads = _ready_threads(configuration, scopes)
        all_count = all_threads.count()
        ready_count = ready_threads.count()
        return SourcePreview(
            summary={
                "scope_count": len(scopes),
                "record_count": ready_count,
                "threads_waiting_for_quiet_period": max(all_count - ready_count, 0),
                "quiet_period_seconds": _quiet_seconds(),
                "direct_messages_included": False,
                "content_activated": False,
            },
            warnings=(
                "Only selected channels are included; Slack direct messages are always excluded.",
            ),
        )

    def dry_run(self, configuration, selected_scopes, policy) -> DryRunResult:
        scopes = _selected_scope_map(configuration, selected_scopes)
        samples = []
        for thread in _ready_threads(configuration, scopes).order_by("pk")[:10]:
            record = _record_for(configuration, thread, scopes)
            if record:
                samples.append(
                    {
                        "source_type": record["source_type"],
                        "external_id": record["external_id"],
                        "title": record["title"],
                        "chunk_count": len(record["chunks"]),
                        "version_key": record["version_key"],
                    }
                )
        return DryRunResult(
            summary={
                "sample_artifacts": len(samples),
                "samples": samples,
                "active_memory_created": False,
            },
            warnings=("Dry-run reads existing Slack artifacts and creates no memory sources.",),
        )

    def _backfill(self, configuration, selected_scopes, checkpoint, *, include_active=False):
        scopes = _selected_scope_map(configuration, selected_scopes)
        threads = _ready_threads(configuration, scopes, include_active=include_active)
        last_pk = max(int((checkpoint or {}).get("last_pk") or 0), 0)
        rows = list(threads.filter(pk__gt=last_pk).order_by("pk")[: _page_size()])
        records = tuple(
            record
            for record in (_record_for(configuration, row, scopes) for row in rows)
            if record
        )
        has_more = len(rows) >= _page_size()
        checkpoint_out = {"last_pk": rows[-1].pk if has_more else 0}
        return SyncPage(
            records=records,
            removals=(
                ()
                if has_more
                else source_removals(
                    configuration,
                    expected=_expected_sources(configuration, scopes),
                )
            ),
            next_cursor=(
                None
                if has_more
                else encoded_positions(current_positions({"thread": threads}))
            ),
            checkpoint=checkpoint_out,
            has_more=has_more,
        )

    def backfill(self, configuration, selected_scopes, checkpoint) -> SyncPage:
        return self._backfill(configuration, selected_scopes, checkpoint)

    def incremental_sync(self, configuration, cursor) -> SyncPage:
        scopes = _selected_scope_map(configuration)
        threads = _ready_threads(configuration, scopes)
        positions = decode_cursor(cursor, kinds=SLACK_KINDS)
        rows = list(
            changed_after(threads, positions["thread"])
            .order_by("updated_at", "pk")[: _page_size() + 1]
        )
        has_more = len(rows) > _page_size()
        if has_more:
            rows = rows[: _page_size()]
        records = []
        for row in rows:
            record = _record_for(configuration, row, scopes)
            if record:
                records.append(record)
            positions["thread"] = cursor_position(row)
        return SyncPage(
            records=tuple(records),
            removals=(
                ()
                if has_more
                else source_removals(
                    configuration,
                    expected=_expected_sources(configuration, scopes),
                )
            ),
            next_cursor=encoded_positions(positions),
            checkpoint={"mode": "incremental", "records": len(records)},
            has_more=has_more,
        )

    def refresh_permissions(self, configuration, checkpoint) -> SyncPage:
        continuation = (
            checkpoint
            if (
                (checkpoint or {}).get("mode") == "permission_refresh"
                and not (checkpoint or {}).get("completed")
            )
            else {}
        )
        page = self._backfill(
            configuration,
            _selected_scope_map(configuration).values(),
            continuation,
            include_active=True,
        )
        return replace(
            page,
            next_cursor=None,
            checkpoint={
                **dict(page.checkpoint or {}),
                "mode": "permission_refresh",
                "completed": not page.has_more,
            },
        )

    def fetch_version(self, configuration, external_id) -> SourceVersionPayload:
        scopes = _selected_scope_map(configuration)
        raw = str(external_id or "")
        prefix = "slack_thread:"
        if raw.startswith(prefix):
            remainder = raw[len(prefix):]
            channel_id, separator, thread_ts = remainder.partition(":")
            if separator:
                thread = _all_threads(configuration, scopes).filter(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                ).first()
                if thread is not None:
                    record = _record_for(configuration, thread, scopes)
                    if record:
                        return SourceVersionPayload(
                            external_id=record["external_id"],
                            canonical_url=record["canonical_url"],
                            version_key=record["version_key"],
                            source_times={
                                "created_at": record["source_created_at"],
                                "modified_at": record["source_updated_at"],
                            },
                            metadata=record["metadata"],
                            acl=record["acl"],
                            content=record["bounded_excerpt"],
                        )
        raise ValueError("Slack thread is outside the selected channels or no longer exists.")

    def tombstone_missing(self, configuration, sync_run) -> TombstoneResult:
        scopes = _selected_scope_map(configuration)
        removals = source_removals(
            configuration,
            expected=_expected_sources(configuration, scopes),
        )
        return TombstoneResult(
            tombstoned_external_ids=tuple(row["external_id"] for row in removals)
        )

    def health(self, configuration) -> ConnectorHealth:
        scopes = _selected_scope_map(configuration)
        all_count = _all_threads(configuration, scopes).count()
        ready_count = _ready_threads(configuration, scopes).count()
        last_sync = configuration.last_successful_sync_at
        return ConnectorHealth(
            status=configuration.lifecycle_state,
            credential_status=str(getattr(configuration.connection, "status", "connected")),
            last_successful_sync_at=last_sync.isoformat() if last_sync else None,
            source_lag_seconds=(
                max(int((timezone.now() - last_sync).total_seconds()), 0)
                if last_sync
                else None
            ),
            details={
                "selected_channels": len(scopes),
                "ready_threads": ready_count,
                "threads_waiting_for_quiet_period": max(all_count - ready_count, 0),
                "quiet_period_seconds": _quiet_seconds(),
                "direct_messages_included": False,
            },
        )
