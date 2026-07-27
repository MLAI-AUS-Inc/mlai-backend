from __future__ import annotations

import base64
import json
import uuid
from dataclasses import replace
from typing import Mapping, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from integrations import http_client
from org_memory.models import (
    MemorySource,
    NotionArtifactState,
    NotionBlockArtifact,
    NotionPageArtifact,
)

from .artifact_utils import (
    bounded_text,
    canonical_hash,
    content_hash,
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


NOTION_BASE_URL = "https://api.notion.com/v1"
NOTION_SCOPE_TYPES = frozenset({"page_root", "data_source"})


class NotionProviderError(RuntimeError):
    pass


def _setting(name: str, default: int, *, maximum: Optional[int] = None) -> int:
    value = max(int(getattr(settings, name, default)), 1)
    return min(value, maximum) if maximum else value


def _selected_scope_map(configuration, selected_scopes=None):
    scopes = list(
        selected_scopes
        if selected_scopes is not None
        else configuration.source_scopes.filter(selected=True, status="selected")
    )
    result = {}
    for scope in scopes:
        if scope.scope_type not in NOTION_SCOPE_TYPES:
            raise ValueError("Notion memory supports page_root and data_source scopes only.")
        external_id = str(scope.external_id or "").strip()
        if not external_id:
            raise ValueError("Notion selected scopes require a provider object ID.")
        result[f"{scope.scope_type}:{external_id}"] = scope
    if not result:
        raise ValueError("Notion memory requires at least one selected root scope.")
    return result


def _rich_text(values) -> str:
    if not isinstance(values, list):
        return ""
    return "".join(
        str(item.get("plain_text") or item.get("text", {}).get("content") or "")
        for item in values
        if isinstance(item, dict)
    ).strip()


def _page_title(page: Mapping) -> str:
    properties = page.get("properties") if isinstance(page.get("properties"), dict) else {}
    for value in properties.values():
        if isinstance(value, dict) and value.get("type") == "title":
            title = _rich_text(value.get("title"))
            if title:
                return bounded_text(title, 512)
    title = _rich_text(page.get("title"))
    return bounded_text(title or "Untitled Notion page", 512)


def _data_source_title(value: Mapping) -> str:
    return bounded_text(_rich_text(value.get("title")) or "Untitled data source", 512)


def _property_value(value: Mapping) -> str:
    kind = str(value.get("type") or "")
    raw = value.get(kind)
    if kind in {"title", "rich_text"}:
        return _rich_text(raw)
    if kind in {"select", "status"} and isinstance(raw, dict):
        return str(raw.get("name") or "")
    if kind == "multi_select" and isinstance(raw, list):
        return ", ".join(str(item.get("name") or "") for item in raw if isinstance(item, dict))
    if kind == "people" and isinstance(raw, list):
        return ", ".join(
            str(item.get("name") or item.get("person", {}).get("email") or item.get("id") or "")
            for item in raw
            if isinstance(item, dict)
        )
    if kind == "relation" and isinstance(raw, list):
        return ", ".join(str(item.get("id") or "") for item in raw if isinstance(item, dict))
    if kind == "date" and isinstance(raw, dict):
        return " to ".join(str(raw.get(key) or "") for key in ("start", "end") if raw.get(key))
    if kind in {"url", "email", "phone_number", "number", "checkbox"}:
        return "" if raw is None else str(raw)
    if kind in {"formula", "rollup"} and isinstance(raw, dict):
        nested_kind = str(raw.get("type") or "")
        nested = raw.get(nested_kind)
        if isinstance(nested, (str, int, float, bool)):
            return str(nested)
        if nested_kind == "date" and isinstance(nested, dict):
            return str(nested.get("start") or "")
    if kind in {"created_time", "last_edited_time"}:
        return str(raw or "")
    return ""


def _properties_text(page: Mapping) -> str:
    properties = page.get("properties") if isinstance(page.get("properties"), dict) else {}
    lines = []
    for name, value in properties.items():
        if not isinstance(value, dict) or value.get("type") == "title":
            continue
        rendered = bounded_text(_property_value(value), 4000)
        if rendered:
            lines.append(f"{bounded_text(name, 256)}: {rendered}")
    return bounded_text("\n".join(lines), 30000)


def _block_text(block: Mapping) -> str:
    kind = str(block.get("type") or "")
    value = block.get(kind) if isinstance(block.get(kind), dict) else {}
    text = _rich_text(value.get("rich_text"))
    if not text and kind in {"child_page", "child_database"}:
        text = str(value.get("title") or "")
    if not text and kind == "equation":
        text = str(value.get("expression") or "")
    if kind == "code" and value.get("language") and text:
        text = f"[{value.get('language')}]\n{text}"
    caption = _rich_text(value.get("caption"))
    if caption:
        text = f"{text}\n{caption}".strip()
    return bounded_text(text, 30000)


def _encode_state(value: Mapping) -> str:
    raw = json.dumps(dict(value), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_state(value: Optional[str]) -> dict:
    if not value:
        return {"version": 1, "mode": "idle"}
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise ValueError("Notion sync cursor is invalid.") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("Notion sync cursor is invalid.")
    return payload


def _initial_state(scopes) -> dict:
    queue = []
    for scope in scopes.values():
        queue.append(
            {
                "kind": "page" if scope.scope_type == "page_root" else "data_source",
                "id": str(scope.external_id),
                "root": str(scope.external_id),
                "scope_id": scope.pk,
                "ancestors": [],
            }
        )
    return {
        "version": 1,
        "mode": "scan",
        "scan_id": str(uuid.uuid4()),
        "queue": queue,
        "visited": [],
    }


class _NotionClient:
    def __init__(self, configuration):
        connection = configuration.connection
        token = str(getattr(connection, "access_token", "") or "").strip()
        if not token:
            raise NotionProviderError("Notion connection is missing its access token.")
        if str(getattr(connection, "status", "connected") or "connected") == "disconnected":
            raise NotionProviderError("Notion connection is disconnected.")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Notion-Version": str(getattr(settings, "NOTION_API_VERSION", "2026-03-11")),
        }
        self.timeout = (3, _setting("ORG_MEMORY_NOTION_HTTP_READ_SECONDS", 20, maximum=90))

    def request(self, method: str, path: str, *, params=None, body=None, allow_missing=False):
        response = http_client.request(
            method,
            f"{NOTION_BASE_URL}{path}",
            headers=self.headers,
            params=params,
            json=body,
            timeout=self.timeout,
        )
        if allow_missing and response.status_code in {403, 404}:
            return None
        if response.status_code == 429:
            retry_after = str(response.headers.get("Retry-After") or "1")
            raise NotionProviderError(f"Notion rate limit reached; retry after {retry_after} seconds.")
        if response.status_code >= 400:
            raise NotionProviderError(f"Notion API request failed with HTTP {response.status_code}.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise NotionProviderError("Notion API returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise NotionProviderError("Notion API returned an unexpected response.")
        return payload

    def search(self, cursor=None):
        body = {
            "page_size": _setting("ORG_MEMORY_NOTION_DISCOVERY_PAGE_SIZE", 100, maximum=100),
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
        }
        if cursor:
            body["start_cursor"] = cursor
        return self.request("POST", "/search", body=body)

    def page(self, page_id):
        return self.request("GET", f"/pages/{page_id}", allow_missing=True)

    def database(self, database_id):
        return self.request("GET", f"/databases/{database_id}", allow_missing=True)

    def query_data_source(self, data_source_id, cursor=None):
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        return self.request(
            "POST",
            f"/data_sources/{data_source_id}/query",
            body=body,
            allow_missing=True,
        )

    def children(self, block_id, cursor=None):
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        return self.request(
            "GET",
            f"/blocks/{block_id}/children",
            params=params,
            allow_missing=True,
        )


def _fetch_blocks(client: _NotionClient, page_id: str):
    maximum = _setting("ORG_MEMORY_NOTION_MAX_BLOCKS_PER_PAGE", 2000, maximum=10000)
    max_depth = _setting("ORG_MEMORY_NOTION_MAX_DEPTH", 16, maximum=64)
    blocks = []
    children = []

    def visit(parent_id: str, depth: int, heading_path):
        if depth > max_depth or len(blocks) >= maximum:
            return
        cursor = None
        current_headings = list(heading_path)
        while True:
            payload = client.children(parent_id, cursor)
            if payload is None:
                return
            for block in payload.get("results") or []:
                if not isinstance(block, dict) or len(blocks) >= maximum:
                    break
                kind = str(block.get("type") or "unsupported")
                text = _block_text(block)
                if kind in {"heading_1", "heading_2", "heading_3"} and text:
                    level = int(kind[-1])
                    current_headings = current_headings[: level - 1]
                    current_headings.append(text[:512])
                block_id = str(block.get("id") or "")
                normalized = {
                    "notion_block_id": block_id,
                    "parent_block_id": "" if parent_id == page_id else parent_id,
                    "block_type": kind,
                    "ordinal": len(blocks),
                    "depth": depth,
                    "heading_path": list(current_headings),
                    "plain_text": text,
                    "has_children": bool(block.get("has_children")),
                    "in_trash": bool(block.get("in_trash")),
                    "is_archived": bool(block.get("archived")),
                    "source_created_at": parse_source_datetime(block.get("created_time")),
                    "source_updated_at": parse_source_datetime(block.get("last_edited_time")),
                    "content_hash": content_hash(text),
                }
                blocks.append(normalized)
                if kind == "child_page" and block_id:
                    children.append(("page", block_id))
                elif kind == "child_database" and block_id:
                    children.append(("database", block_id))
                elif normalized["has_children"] and block_id and depth < max_depth:
                    visit(block_id, depth + 1, current_headings)
            if len(blocks) >= maximum or not payload.get("has_more"):
                break
            cursor = payload.get("next_cursor")
            if not cursor:
                break
    visit(page_id, 0, [])
    return blocks, children


def _cleaned_text(title: str, property_text: str, blocks) -> str:
    body = "\n".join(str(block.get("plain_text") or "") for block in blocks if block.get("plain_text"))
    return bounded_text("\n\n".join(value for value in (title, property_text, body) if value), 500000)


@transaction.atomic
def _persist_page(configuration, scope, root_id, ancestors, page, blocks, scan_id):
    page_id = str(page.get("id") or "").strip()
    if not page_id:
        raise NotionProviderError("Notion page response is missing its ID.")
    title = _page_title(page)
    property_text = _properties_text(page)
    cleaned = _cleaned_text(title, property_text, blocks)
    parent = page.get("parent") if isinstance(page.get("parent"), dict) else {}
    parent_type = str(parent.get("type") or "")
    state = (
        NotionArtifactState.TRASHED
        if bool(page.get("in_trash") or page.get("archived"))
        else NotionArtifactState.ACTIVE
    )
    artifact = NotionPageArtifact.objects.filter(
        configuration=configuration,
        notion_page_id=page_id,
    ).first()
    roots = [str(root_id)]
    if artifact and str(artifact.scan_generation or "") == str(scan_id):
        roots = list(dict.fromkeys([*(artifact.selected_root_ids or []), str(root_id)]))
    revision = canonical_hash(
        {
            "last_edited_time": page.get("last_edited_time"),
            "content_hash": content_hash(cleaned),
            "lifecycle_state": state,
            "roots": roots,
        }
    )
    durable_scope = (
        artifact.source_scope
        if artifact and str(artifact.scan_generation or "") == str(scan_id)
        else scope
    )
    defaults = {
        "organization": configuration.organization,
        "source_scope": durable_scope,
        "selected_root_ids": roots,
        "ancestor_page_ids": [str(value) for value in ancestors],
        "parent_type": parent_type,
        "parent_external_id": str(parent.get(parent_type) or "")[:128],
        "title": title,
        "canonical_url": str(page.get("url") or "")[:2048],
        "property_text": property_text,
        "cleaned_text": cleaned,
        "source_created_at": parse_source_datetime(page.get("created_time")),
        "source_updated_at": parse_source_datetime(page.get("last_edited_time")),
        "lifecycle_state": state,
        "in_trash": bool(page.get("in_trash")),
        "is_archived": bool(page.get("archived")),
        "provider_revision": revision,
        "content_hash": content_hash(cleaned),
        "scan_generation": scan_id,
        "last_seen_at": timezone.now(),
    }
    artifact, _created = NotionPageArtifact.objects.update_or_create(
        configuration=configuration,
        notion_page_id=page_id,
        defaults=defaults,
    )
    seen_ids = []
    for block in blocks:
        block_id = str(block["notion_block_id"])
        if not block_id:
            continue
        seen_ids.append(block_id)
        NotionBlockArtifact.objects.update_or_create(
            page=artifact,
            notion_block_id=block_id,
            defaults={key: value for key, value in block.items() if key != "notion_block_id"},
        )
    artifact.blocks.exclude(notion_block_id__in=seen_ids).delete()
    return artifact


def _mark_inaccessible(configuration, page_id, scan_id):
    artifact = NotionPageArtifact.objects.filter(
        configuration=configuration,
        notion_page_id=page_id,
    ).first()
    if artifact is None:
        return None
    artifact.lifecycle_state = NotionArtifactState.ACCESS_LOST
    artifact.scan_generation = scan_id
    artifact.last_seen_at = timezone.now()
    artifact.provider_revision = canonical_hash(
        {"previous": artifact.provider_revision, "lifecycle_state": artifact.lifecycle_state}
    )
    artifact.save(
        update_fields=(
            "lifecycle_state",
            "scan_generation",
            "last_seen_at",
            "provider_revision",
            "updated_at",
        )
    )
    return artifact


def _chunks(artifact):
    target = _setting("ORG_MEMORY_NOTION_CHUNK_TARGET_CHARS", 6000, maximum=20000)
    chunks = []
    prefix = "\n".join(value for value in (artifact.title, artifact.property_text) if value).strip()
    if prefix:
        chunks.append(
            {
                "ordinal": 0,
                "chunk_kind": "notion_page_properties",
                "text": prefix,
                "token_count": estimate_tokens(prefix),
                "source_locator": {"page_id": artifact.notion_page_id, "section": "properties"},
                "occurred_at": artifact.source_updated_at,
            }
        )
    group = []
    length = 0
    for block in artifact.blocks.order_by("ordinal"):
        text = str(block.plain_text or "").strip()
        if not text:
            continue
        if group and length + len(text) + 1 > target:
            chunks.append(_block_chunk(artifact, group, len(chunks)))
            group = []
            length = 0
        group.append(block)
        length += len(text) + 1
    if group:
        chunks.append(_block_chunk(artifact, group, len(chunks)))
    if not chunks:
        empty = artifact.title or "Untitled Notion page"
        chunks.append(
            {
                "ordinal": 0,
                "chunk_kind": "notion_page",
                "text": empty,
                "token_count": estimate_tokens(empty),
                "source_locator": {"page_id": artifact.notion_page_id},
                "occurred_at": artifact.source_updated_at,
            }
        )
    return tuple(chunks)


def _block_chunk(artifact, blocks, ordinal):
    text = "\n".join(block.plain_text for block in blocks if block.plain_text).strip()
    first, last = blocks[0], blocks[-1]
    return {
        "ordinal": ordinal,
        "chunk_kind": "notion_blocks",
        "text": text,
        "token_count": estimate_tokens(text),
        "source_locator": {
            "page_id": artifact.notion_page_id,
            "start_block_id": first.notion_block_id,
            "end_block_id": last.notion_block_id,
            "start_ordinal": first.ordinal,
            "end_ordinal": last.ordinal,
            "heading_path": first.heading_path,
            "depth": first.depth,
        },
        "occurred_at": artifact.source_updated_at,
    }


def _record_for(configuration, artifact, scope):
    acl = source_acl(
        configuration,
        scope,
        revision_payload={
            "page_id": artifact.notion_page_id,
            "provider_revision": artifact.provider_revision,
            "lifecycle_state": artifact.lifecycle_state,
        },
    )
    acl["is_accessible"] = bool(
        acl["is_accessible"] and artifact.lifecycle_state == NotionArtifactState.ACTIVE
    )
    payload = {
        "content_hash": artifact.content_hash,
        "provider_revision": artifact.provider_revision,
        "acl": acl,
        "adapter": "notion-page-v1",
    }
    return {
        "source_scope_id": scope.pk,
        "source_type": "notion_page",
        "external_id": f"notion_page:{artifact.notion_page_id}",
        "version_key": version_key(payload),
        "content_hash": artifact.content_hash,
        "classification": scope.default_classification,
        "acl": acl,
        "chunks": _chunks(artifact),
        "canonical_url": artifact.canonical_url,
        "title": artifact.title,
        "author_external_id": "",
        "source_created_at": artifact.source_created_at,
        "source_updated_at": artifact.source_updated_at,
        "occurred_at": artifact.source_updated_at,
        "bounded_excerpt": artifact.cleaned_text[:4096],
        "metadata": {
            "record_type": "notion_page",
            "page_id": artifact.notion_page_id,
            "parent_type": artifact.parent_type,
            "parent_external_id": artifact.parent_external_id,
            "ancestor_page_ids": artifact.ancestor_page_ids,
            "selected_root_ids": artifact.selected_root_ids,
            "in_trash": artifact.in_trash,
            "is_archived": artifact.is_archived,
            "authority_fields": ["published_workspace_document"],
        },
        "restore_access": bool(acl["is_accessible"]),
    }


class NotionMemoryConnector:
    provider = "notion"

    def discover_scopes(self, configuration, cursor=None) -> ScopePage:
        payload = _NotionClient(configuration).search(cursor)
        descriptors = []
        for value in payload.get("results") or []:
            if not isinstance(value, dict):
                continue
            object_type = str(value.get("object") or "")
            if object_type == "page":
                descriptors.append(
                    ScopeDescriptor(
                        scope_type="page_root",
                        external_id=str(value.get("id") or ""),
                        name=_page_title(value),
                        canonical_url=str(value.get("url") or ""),
                        metadata={"object_type": "page", "metadata_only": True},
                    )
                )
            elif object_type == "data_source":
                descriptors.append(
                    ScopeDescriptor(
                        scope_type="data_source",
                        external_id=str(value.get("id") or ""),
                        name=_data_source_title(value),
                        canonical_url=str(value.get("url") or ""),
                        metadata={"object_type": "data_source", "metadata_only": True},
                    )
                )
        return ScopePage(
            scopes=tuple(row for row in descriptors if row.external_id),
            next_cursor=str(payload.get("next_cursor") or "") or None,
            warnings=("Discovery returns metadata only; content is fetched after root selection.",),
        )

    def preview(self, configuration, selected_scopes, policy) -> SourcePreview:
        scopes = _selected_scope_map(configuration, selected_scopes)
        artifacts = NotionPageArtifact.objects.filter(configuration=configuration)
        return SourcePreview(
            summary={
                "scope_count": len(scopes),
                "durable_page_count": artifacts.count(),
                "accessible_page_count": artifacts.filter(lifecycle_state=NotionArtifactState.ACTIVE).count(),
                "record_count": None,
                "content_activated": False,
            },
            warnings=("Preview is metadata-only; the recursive page count is established by backfill.",),
        )

    def dry_run(self, configuration, selected_scopes, policy) -> DryRunResult:
        scopes = _selected_scope_map(configuration, selected_scopes)
        samples = list(
            NotionPageArtifact.objects.filter(configuration=configuration)
            .order_by("title")
            .values("notion_page_id", "title", "lifecycle_state")[:10]
        )
        return DryRunResult(
            summary={
                "scope_count": len(scopes),
                "sample_artifacts": len(samples),
                "samples": samples,
                "active_memory_created": False,
            },
            warnings=("Dry-run reads durable Notion metadata and creates no memory sources.",),
        )

    def _execute(self, configuration, scopes, state) -> SyncPage:
        client = _NotionClient(configuration)
        scan_id = str(state.get("scan_id") or uuid.uuid4())
        if state.get("mode") != "scan":
            state = _initial_state(scopes)
            scan_id = state["scan_id"]
        queue = list(state.get("queue") or [])
        visited = set(str(value) for value in (state.get("visited") or []))
        records = []
        budget = _setting("ORG_MEMORY_NOTION_SCAN_PAGE_BUDGET", 10, maximum=100)
        maximum = _setting("ORG_MEMORY_NOTION_SCAN_MAX_PAGES", 1000, maximum=10000)
        processed = 0
        while queue and processed < budget:
            entry = queue.pop(0)
            kind = str(entry.get("kind") or "")
            object_id = str(entry.get("id") or "")
            root_id = str(entry.get("root") or object_id)
            visit_key = f"{kind}:{object_id}:{root_id}:{entry.get('cursor') or ''}"
            if not object_id or visit_key in visited:
                continue
            if len(visited) >= maximum:
                raise NotionProviderError("Notion selected-root scan exceeded its configured object limit.")
            visited.add(visit_key)
            processed += 1
            scope = scopes.get(f"page_root:{root_id}") or scopes.get(f"data_source:{root_id}")
            if scope is None:
                continue
            if kind == "page":
                page = client.page(object_id)
                if page is None:
                    artifact = _mark_inaccessible(configuration, object_id, scan_id)
                    if artifact:
                        records.append(_record_for(configuration, artifact, scope))
                    continue
                blocks = []
                children = []
                if not bool(page.get("in_trash") or page.get("archived")):
                    blocks, children = _fetch_blocks(client, object_id)
                artifact = _persist_page(
                    configuration,
                    scope,
                    root_id,
                    entry.get("ancestors") or [],
                    page,
                    blocks,
                    scan_id,
                )
                records.append(
                    _record_for(
                        configuration,
                        artifact,
                        artifact.source_scope or scope,
                    )
                )
                child_ancestors = [*(entry.get("ancestors") or []), object_id]
                for child_kind, child_id in children:
                    queue.append(
                        {
                            "kind": child_kind,
                            "id": child_id,
                            "root": root_id,
                            "scope_id": scope.pk,
                            "ancestors": child_ancestors,
                        }
                    )
            elif kind == "database":
                database = client.database(object_id)
                for data_source in (database or {}).get("data_sources") or []:
                    if isinstance(data_source, dict) and data_source.get("id"):
                        queue.append(
                            {
                                "kind": "data_source",
                                "id": str(data_source["id"]),
                                "root": root_id,
                                "scope_id": scope.pk,
                                "ancestors": entry.get("ancestors") or [],
                            }
                        )
            elif kind == "data_source":
                result = client.query_data_source(object_id, entry.get("cursor"))
                if result:
                    for page in result.get("results") or []:
                        if isinstance(page, dict) and page.get("id"):
                            queue.append(
                                {
                                    "kind": "page",
                                    "id": str(page["id"]),
                                    "root": root_id,
                                    "scope_id": scope.pk,
                                    "ancestors": entry.get("ancestors") or [],
                                }
                            )
                    if result.get("has_more") and result.get("next_cursor"):
                        queue.append({**entry, "cursor": str(result["next_cursor"])})
        if queue:
            next_state = {
                "version": 1,
                "mode": "scan",
                "scan_id": scan_id,
                "queue": queue,
                "visited": sorted(visited),
            }
            return SyncPage(
                records=tuple(records),
                next_cursor=_encode_state(next_state),
                checkpoint=next_state,
                has_more=True,
            )
        stale = list(
            NotionPageArtifact.objects.filter(
                configuration=configuration,
                lifecycle_state=NotionArtifactState.ACTIVE,
            )
            .exclude(scan_generation=scan_id)
            .order_by("created_at")[:budget]
        )
        removals = []
        for artifact in stale:
            artifact.lifecycle_state = NotionArtifactState.ACCESS_LOST
            artifact.provider_revision = canonical_hash(
                {"previous": artifact.provider_revision, "lifecycle_state": artifact.lifecycle_state}
            )
            artifact.save(update_fields=("lifecycle_state", "provider_revision", "updated_at"))
            removals.append(
                {
                    "source_type": "notion_page",
                    "external_id": f"notion_page:{artifact.notion_page_id}",
                    "reason": "notion_page_missing_or_outside_selected_roots",
                    "revoke_access": True,
                }
            )
        if len(stale) >= budget:
            next_state = {
                "version": 1,
                "mode": "scan",
                "scan_id": scan_id,
                "queue": [],
                "visited": sorted(visited),
            }
            return SyncPage(
                records=tuple(records),
                removals=tuple(removals),
                next_cursor=_encode_state(next_state),
                checkpoint=next_state,
                has_more=True,
            )
        idle = {"version": 1, "mode": "idle", "last_scan_at": timezone.now().isoformat()}
        return SyncPage(
            records=tuple(records),
            removals=tuple(removals),
            next_cursor=_encode_state(idle),
            checkpoint={"mode": "completed", "scan_id": scan_id},
            has_more=False,
        )

    def backfill(self, configuration, selected_scopes, checkpoint) -> SyncPage:
        scopes = _selected_scope_map(configuration, selected_scopes)
        state = checkpoint if (checkpoint or {}).get("mode") == "scan" else _initial_state(scopes)
        return self._execute(configuration, scopes, state)

    def incremental_sync(self, configuration, cursor) -> SyncPage:
        scopes = _selected_scope_map(configuration)
        state = _decode_state(cursor)
        if state.get("mode") == "idle":
            state = _initial_state(scopes)
        return self._execute(configuration, scopes, state)

    def refresh_permissions(self, configuration, checkpoint) -> SyncPage:
        scopes = _selected_scope_map(configuration)
        continuation = checkpoint if (checkpoint or {}).get("mode") == "scan" else _initial_state(scopes)
        page = self._execute(configuration, scopes, continuation)
        return replace(page, next_cursor=None)

    def fetch_version(self, configuration, external_id) -> SourceVersionPayload:
        raw = str(external_id or "")
        page_id = raw[len("notion_page:") :] if raw.startswith("notion_page:") else raw
        artifact = NotionPageArtifact.objects.filter(
            configuration=configuration,
            notion_page_id=page_id,
        ).select_related("source_scope").first()
        if artifact is None or artifact.source_scope is None:
            raise ValueError("Notion page is outside the durable selected-root inventory.")
        record = _record_for(configuration, artifact, artifact.source_scope)
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

    def tombstone_missing(self, configuration, sync_run) -> TombstoneResult:
        expected = {
            ("notion_page", f"notion_page:{page_id}")
            for page_id in NotionPageArtifact.objects.filter(configuration=configuration).values_list(
                "notion_page_id", flat=True
            )
        }
        removals = source_removals(configuration, expected=expected)
        return TombstoneResult(
            tombstoned_external_ids=tuple(row["external_id"] for row in removals)
        )

    def health(self, configuration) -> ConnectorHealth:
        latest = (
            NotionPageArtifact.objects.filter(configuration=configuration)
            .order_by("-last_seen_at")
            .first()
        )
        last_sync = configuration.last_successful_sync_at
        lag = max(int((timezone.now() - last_sync).total_seconds()), 0) if last_sync else None
        connection_status = str(getattr(configuration.connection, "status", "connected") or "connected")
        return ConnectorHealth(
            status=configuration.lifecycle_state,
            credential_status=connection_status,
            last_successful_sync_at=last_sync.isoformat() if last_sync else None,
            source_lag_seconds=lag,
            details={
                "connection_status": connection_status,
                "durable_pages": NotionPageArtifact.objects.filter(configuration=configuration).count(),
                "latest_page_seen_at": latest.last_seen_at.isoformat() if latest else None,
                "daily_fallback_seconds": int(getattr(settings, "ORG_MEMORY_SYNC_INTERVAL_SECONDS", 86400)),
            },
        )
