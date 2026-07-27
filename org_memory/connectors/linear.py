from __future__ import annotations

from dataclasses import replace

from django.conf import settings
from django.utils import timezone

from org_memory.models import MemorySource
from startup_updates.models import (
    LinearIssueArtifact,
    LinearProjectArtifact,
    LinearProjectSelection,
    LinearProjectUpdateArtifact,
)

from .artifact_utils import (
    bounded_text,
    changed_after,
    content_hash,
    current_positions,
    cursor_position,
    decode_cursor,
    encoded_positions,
    estimate_tokens,
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


LINEAR_KINDS = ("project", "issue", "project_update")


def _page_size() -> int:
    return max(min(int(getattr(settings, "ORG_MEMORY_ARTIFACT_PAGE_SIZE", 100)), 500), 1)


def _selected_scope_map(configuration, selected_scopes=None):
    scopes = list(
        selected_scopes
        if selected_scopes is not None
        else configuration.source_scopes.filter(selected=True, status="selected")
    )
    result = {}
    for scope in scopes:
        if scope.scope_type != "project" or not str(scope.external_id or "").strip():
            raise ValueError("Linear memory supports selected project scopes only.")
        result[str(scope.external_id)] = scope
    if not result:
        raise ValueError("Linear memory requires at least one selected project scope.")
    return result


def _is_private_project(project) -> bool:
    payload = project.raw_payload if isinstance(project.raw_payload, dict) else {}
    return bool(payload.get("private") or payload.get("isPrivate"))


def _querysets(configuration, scopes):
    project_ids = tuple(scopes)
    projects = LinearProjectArtifact.objects.filter(
        organization=configuration.organization,
        connection=configuration.connection,
        linear_project_id__in=project_ids,
    )
    issues = LinearIssueArtifact.objects.filter(
        organization=configuration.organization,
        connection=configuration.connection,
        project__linear_project_id__in=project_ids,
    ).select_related("project")
    updates = LinearProjectUpdateArtifact.objects.filter(
        organization=configuration.organization,
        connection=configuration.connection,
        project__linear_project_id__in=project_ids,
    ).select_related("project")
    cutoff = configuration.historical_cutoff
    if cutoff:
        projects = projects.filter(updated_at__gte=cutoff)
        issues = issues.filter(updated_at__gte=cutoff)
        updates = updates.filter(updated_at__gte=cutoff)
    return {
        "project": projects,
        "issue": issues,
        "project_update": updates,
    }


def _lines(*values):
    return "\n".join(value for value in values if value).strip()


def _project_record(configuration, project, scope):
    text = bounded_text(
        _lines(
            f"Linear project: {project.name}",
            f"Status: {project.status_name or project.status_type}" if (project.status_name or project.status_type) else "",
            f"Health: {project.health}" if project.health else "",
            f"Progress: {project.progress}" if project.progress is not None else "",
            f"Priority: {project.priority}" if project.priority else "",
            f"Lead: {project.lead_name}" if project.lead_name else "",
            f"Teams: {', '.join(project.team_names or [])}" if project.team_names else "",
            f"Start date: {project.start_date}" if project.start_date else "",
            f"Target date: {project.target_date}" if project.target_date else "",
            project.description,
        )
    )
    acl = source_acl(
        configuration,
        scope,
        revision_payload={"kind": "project", "project_id": project.linear_project_id},
    )
    payload = {
        "text": text,
        "acl": acl,
        "updated_at": project.updated_at,
        "adapter": "linear-project-v1",
    }
    return {
        "source_scope_id": scope.pk,
        "source_type": "linear_project",
        "external_id": f"linear_project:{project.linear_project_id}",
        "version_key": version_key(payload),
        "content_hash": content_hash(text),
        "classification": scope.default_classification,
        "acl": acl,
        "chunks": ({
            "ordinal": 0,
            "chunk_kind": "linear_project",
            "text": text,
            "token_count": estimate_tokens(text),
            "source_locator": {"project_id": project.linear_project_id},
            "occurred_at": project.updated_at,
        },),
        "canonical_url": project.url,
        "title": project.name,
        "author_external_id": "",
        "source_created_at": project.started_at or project.created_at,
        "source_updated_at": project.updated_at,
        "occurred_at": project.updated_at,
        "bounded_excerpt": text[:4096],
        "metadata": {
            "record_type": "linear_project",
            "project_id": project.linear_project_id,
            "status_type": project.status_type,
            "authority_fields": ["project_state"],
        },
        "restore_access": bool(acl["is_accessible"]),
    }


def _issue_record(configuration, issue, scope):
    project_name = issue.project.name if issue.project else ""
    text = bounded_text(
        _lines(
            f"Linear issue: {issue.identifier or issue.linear_issue_id} — {issue.title}",
            f"Project: {project_name}" if project_name else "",
            f"State: {issue.state_name or issue.state_type}" if (issue.state_name or issue.state_type) else "",
            f"Assignee: {issue.assignee_name}" if issue.assignee_name else "",
            f"Priority: {issue.priority_label or issue.priority}" if (issue.priority_label or issue.priority is not None) else "",
            f"Due date: {issue.due_date}" if issue.due_date else "",
            f"Labels: {', '.join(issue.label_names or [])}" if issue.label_names else "",
            issue.description,
        )
    )
    acl = source_acl(
        configuration,
        scope,
        revision_payload={"kind": "issue", "issue_id": issue.linear_issue_id},
    )
    payload = {
        "text": text,
        "acl": acl,
        "updated_at": issue.updated_at_linear or issue.updated_at,
        "project_revision": issue.project.updated_at if issue.project else None,
        "adapter": "linear-issue-v1",
    }
    occurred = issue.updated_at_linear or issue.created_at_linear or issue.updated_at
    return {
        "source_scope_id": scope.pk,
        "source_type": "linear_issue",
        "external_id": f"linear_issue:{issue.linear_issue_id}",
        "version_key": version_key(payload),
        "content_hash": content_hash(text),
        "classification": scope.default_classification,
        "acl": acl,
        "chunks": ({
            "ordinal": 0,
            "chunk_kind": "linear_issue",
            "text": text,
            "token_count": estimate_tokens(text),
            "source_locator": {
                "issue_id": issue.linear_issue_id,
                "identifier": issue.identifier,
                "project_id": issue.project.linear_project_id if issue.project else "",
            },
            "occurred_at": occurred,
        },),
        "canonical_url": issue.url,
        "title": bounded_text(issue.title or issue.identifier, 512),
        "author_external_id": "",
        "source_created_at": issue.created_at_linear or issue.created_at,
        "source_updated_at": issue.updated_at_linear or issue.updated_at,
        "occurred_at": occurred,
        "bounded_excerpt": text[:4096],
        "metadata": {
            "record_type": "linear_issue",
            "issue_id": issue.linear_issue_id,
            "identifier": issue.identifier,
            "project_id": issue.project.linear_project_id if issue.project else "",
            "authority_fields": ["issue_status", "issue_assignee", "issue_priority"],
        },
        "restore_access": bool(acl["is_accessible"]),
    }


def _update_record(configuration, update, scope):
    project_name = update.project.name if update.project else ""
    text = bounded_text(
        _lines(
            f"Linear project update for {project_name}" if project_name else "Linear project update",
            f"Health: {update.health}" if update.health else "",
            f"Author: {update.author_name}" if update.author_name else "",
            update.body,
        )
    )
    acl = source_acl(
        configuration,
        scope,
        revision_payload={"kind": "project_update", "update_id": update.linear_project_update_id},
    )
    payload = {
        "text": text,
        "acl": acl,
        "updated_at": update.updated_at_linear or update.updated_at,
        "project_revision": update.project.updated_at if update.project else None,
        "adapter": "linear-project-update-v1",
    }
    occurred = update.updated_at_linear or update.created_at_linear or update.updated_at
    return {
        "source_scope_id": scope.pk,
        "source_type": "linear_project_update",
        "external_id": f"linear_project_update:{update.linear_project_update_id}",
        "version_key": version_key(payload),
        "content_hash": content_hash(text),
        "classification": scope.default_classification,
        "acl": acl,
        "chunks": ({
            "ordinal": 0,
            "chunk_kind": "linear_project_update",
            "text": text,
            "token_count": estimate_tokens(text),
            "source_locator": {
                "project_update_id": update.linear_project_update_id,
                "project_id": update.project.linear_project_id if update.project else "",
            },
            "occurred_at": occurred,
        },),
        "canonical_url": update.url,
        "title": bounded_text(f"{project_name or 'Linear'} project update", 512),
        "author_external_id": "",
        "source_created_at": update.created_at_linear or update.created_at,
        "source_updated_at": update.updated_at_linear or update.updated_at,
        "occurred_at": occurred,
        "bounded_excerpt": text[:4096],
        "metadata": {
            "record_type": "linear_project_update",
            "project_update_id": update.linear_project_update_id,
            "project_id": update.project.linear_project_id if update.project else "",
        },
        "restore_access": bool(acl["is_accessible"]),
    }


def _record_for(configuration, kind, instance, scopes):
    project = instance if kind == "project" else instance.project
    project_id = (
        instance.linear_project_id
        if kind == "project"
        else project.linear_project_id
        if project
        else ""
    )
    scope = scopes.get(str(project_id))
    if scope is None or project is None or _is_private_project(project):
        return None
    if kind == "project":
        return _project_record(configuration, instance, scope)
    if kind == "issue":
        return _issue_record(configuration, instance, scope)
    return _update_record(configuration, instance, scope)


def _expected_sources(configuration, querysets, scopes):
    expected = set()
    for project in querysets["project"]:
        expected.add(("linear_project", f"linear_project:{project.linear_project_id}"))
    expected.update(
        ("linear_issue", f"linear_issue:{issue.linear_issue_id}")
        for issue in querysets["issue"]
        if issue.project and issue.project.linear_project_id in scopes
    )
    expected.update(
        (
            "linear_project_update",
            f"linear_project_update:{update.linear_project_update_id}",
        )
        for update in querysets["project_update"]
        if update.project and update.project.linear_project_id in scopes
    )
    return expected


def _private_revocations(querysets):
    project_ids = {
        project.linear_project_id
        for project in querysets["project"]
        if _is_private_project(project)
    }
    removals = [
        {
            "source_type": "linear_project",
            "external_id": f"linear_project:{project_id}",
            "reason": "linear_project_private",
            "revoke_access": True,
        }
        for project_id in project_ids
    ]
    removals.extend(
        {
            "source_type": "linear_issue",
            "external_id": f"linear_issue:{issue.linear_issue_id}",
            "reason": "linear_project_private",
            "revoke_access": True,
        }
        for issue in querysets["issue"]
        if issue.project and issue.project.linear_project_id in project_ids
    )
    removals.extend(
        {
            "source_type": "linear_project_update",
            "external_id": f"linear_project_update:{update.linear_project_update_id}",
            "reason": "linear_project_private",
            "revoke_access": True,
        }
        for update in querysets["project_update"]
        if update.project and update.project.linear_project_id in project_ids
    )
    return tuple(removals)


def _reconciliation_removals(configuration, querysets, scopes):
    return (
        *_private_revocations(querysets),
        *source_removals(
            configuration,
            expected=_expected_sources(configuration, querysets, scopes),
        ),
    )


class LinearArtifactMemoryConnector:
    provider = "linear"

    def discover_scopes(self, configuration, cursor=None) -> ScopePage:
        selections = LinearProjectSelection.objects.filter(
            connection=configuration.connection,
        ).order_by("project_name", "linear_project_id")
        descriptors = [
            ScopeDescriptor(
                scope_type="project",
                external_id=row.linear_project_id,
                name=row.project_name or row.linear_project_id,
                metadata={
                    "legacy_selected": bool(row.selected),
                    "project_status": row.project_status,
                    "project_health": row.project_health,
                },
            )
            for row in selections
        ]
        if not descriptors:
            descriptors = [
                ScopeDescriptor(
                    scope_type="project",
                    external_id=row.linear_project_id,
                    name=row.name or row.linear_project_id,
                    canonical_url=row.url,
                    metadata={"discovered_from": "linear_project_artifact"},
                )
                for row in LinearProjectArtifact.objects.filter(
                    organization=configuration.organization,
                    connection=configuration.connection,
                ).order_by("name", "linear_project_id")
            ]
        return ScopePage(scopes=tuple(descriptors))

    def preview(self, configuration, selected_scopes, policy) -> SourcePreview:
        scopes = _selected_scope_map(configuration, selected_scopes)
        querysets = _querysets(configuration, scopes)
        counts = {kind: queryset.count() for kind, queryset in querysets.items()}
        eligible_count = sum(
            1
            for kind in LINEAR_KINDS
            for row in querysets[kind]
            if _record_for(configuration, kind, row, scopes)
        )
        private_count = max(sum(counts.values()) - eligible_count, 0)
        return SourcePreview(
            summary={
                "scope_count": len(scopes),
                "counts": counts,
                "record_count": eligible_count,
                "private_artifacts_excluded": private_count,
                "content_activated": False,
            },
            warnings=(
                ("Private Linear projects are excluded." if private_count else "")
            ,) if private_count else (),
        )

    def dry_run(self, configuration, selected_scopes, policy) -> DryRunResult:
        scopes = _selected_scope_map(configuration, selected_scopes)
        querysets = _querysets(configuration, scopes)
        samples = []
        for kind in LINEAR_KINDS:
            for instance in querysets[kind].order_by("pk")[:3]:
                record = _record_for(configuration, kind, instance, scopes)
                if record:
                    samples.append(
                        {
                            "source_type": record["source_type"],
                            "external_id": record["external_id"],
                            "title": record["title"],
                            "version_key": record["version_key"],
                        }
                    )
        return DryRunResult(
            summary={
                "sample_artifacts": len(samples),
                "samples": samples[:10],
                "active_memory_created": False,
            },
            warnings=("Dry-run reads existing Linear artifacts and creates no memory sources.",),
        )

    def backfill(self, configuration, selected_scopes, checkpoint) -> SyncPage:
        scopes = _selected_scope_map(configuration, selected_scopes)
        querysets = _querysets(configuration, scopes)
        kind_index = max(int((checkpoint or {}).get("kind_index") or 0), 0)
        last_pk = max(int((checkpoint or {}).get("last_pk") or 0), 0)
        while kind_index < len(LINEAR_KINDS):
            kind = LINEAR_KINDS[kind_index]
            rows = list(
                querysets[kind].filter(pk__gt=last_pk).order_by("pk")[: _page_size()]
            )
            records = tuple(
                record
                for record in (
                    _record_for(configuration, kind, row, scopes) for row in rows
                )
                if record
            )
            if len(rows) >= _page_size():
                return SyncPage(
                    records=records,
                    checkpoint={"kind_index": kind_index, "last_pk": rows[-1].pk},
                    has_more=True,
                )
            kind_index += 1
            last_pk = 0
            if records:
                return SyncPage(
                    records=records,
                    checkpoint={"kind_index": kind_index, "last_pk": 0},
                    has_more=kind_index < len(LINEAR_KINDS),
                    next_cursor=(
                        encoded_positions(current_positions(querysets))
                        if kind_index >= len(LINEAR_KINDS)
                        else None
                    ),
                    removals=(
                        _reconciliation_removals(configuration, querysets, scopes)
                        if kind_index >= len(LINEAR_KINDS)
                        else ()
                    ),
                )
        return SyncPage(
            records=(),
            removals=_reconciliation_removals(configuration, querysets, scopes),
            next_cursor=encoded_positions(current_positions(querysets)),
            checkpoint={"kind_index": len(LINEAR_KINDS), "last_pk": 0},
            has_more=False,
        )

    def incremental_sync(self, configuration, cursor) -> SyncPage:
        scopes = _selected_scope_map(configuration)
        querysets = _querysets(configuration, scopes)
        positions = decode_cursor(cursor, kinds=LINEAR_KINDS)
        records = []
        has_more = False
        reset_children = False
        for kind in LINEAR_KINDS:
            if reset_children and kind in {"issue", "project_update"}:
                positions[kind] = {"updated_at": "", "pk": 0}
            changed = changed_after(querysets[kind], positions[kind]).order_by(
                "updated_at", "pk"
            )
            rows = list(changed[: _page_size() + 1])
            if len(rows) > _page_size():
                has_more = True
                rows = rows[: _page_size()]
            for row in rows:
                record = _record_for(configuration, kind, row, scopes)
                if record:
                    records.append(record)
                    if kind == "project" and not MemorySource.objects.filter(
                        configuration=configuration,
                        source_type="linear_project",
                        external_id=record["external_id"],
                        lifecycle_state="active",
                    ).exists():
                        reset_children = True
                positions[kind] = cursor_position(row)
        return SyncPage(
            records=tuple(records),
            removals=(
                ()
                if has_more
                else _reconciliation_removals(configuration, querysets, scopes)
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
        page = self.backfill(configuration, _selected_scope_map(configuration).values(), continuation)
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
        querysets = _querysets(configuration, scopes)
        raw = str(external_id or "")
        matches = (
            ("project", "linear_project:", "linear_project_id"),
            ("issue", "linear_issue:", "linear_issue_id"),
            ("project_update", "linear_project_update:", "linear_project_update_id"),
        )
        for kind, prefix, field in matches:
            if raw.startswith(prefix):
                instance = querysets[kind].filter(**{field: raw[len(prefix):]}).first()
                if instance is None:
                    break
                record = _record_for(configuration, kind, instance, scopes)
                if record is None:
                    break
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
        raise ValueError("Linear artifact is outside the selected projects or no longer exists.")

    def tombstone_missing(self, configuration, sync_run) -> TombstoneResult:
        scopes = _selected_scope_map(configuration)
        querysets = _querysets(configuration, scopes)
        removals = source_removals(
            configuration,
            expected=_expected_sources(configuration, querysets, scopes),
        )
        return TombstoneResult(
            tombstoned_external_ids=tuple(row["external_id"] for row in removals)
        )

    def health(self, configuration) -> ConnectorHealth:
        scopes = _selected_scope_map(configuration)
        querysets = _querysets(configuration, scopes)
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
                "selected_projects": len(scopes),
                "projects": querysets["project"].count(),
                "issues": querysets["issue"].count(),
                "project_updates": querysets["project_update"].count(),
            },
        )
