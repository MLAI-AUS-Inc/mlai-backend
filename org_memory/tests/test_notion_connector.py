from datetime import timedelta
from unittest.mock import patch
from urllib.parse import urlparse

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from integrations.models import ExternalServiceConnection
from organizations.models import Organization
from org_memory.connectors.notion import NotionMemoryConnector
from org_memory.connectors.registry import MetadataOnlyMemoryConnector, connector_registry
from org_memory.models import (
    MemoryConnectionConfiguration,
    MemoryConnectionState,
    MemoryScopeStatus,
    MemorySource,
    MemorySourceLifecycle,
    MemorySourceScope,
    NotionArtifactState,
    NotionBlockArtifact,
    NotionPageArtifact,
)
from org_memory.runtime import _apply_removal, _capture_record


class _Response:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._payload


@override_settings(
    NOTION_API_VERSION="2026-03-11",
    ORG_MEMORY_NOTION_SCAN_PAGE_BUDGET=10,
    ORG_MEMORY_NOTION_SCAN_MAX_PAGES=100,
    ORG_MEMORY_NOTION_MAX_BLOCKS_PER_PAGE=100,
    ORG_MEMORY_NOTION_MAX_DEPTH=8,
    ORG_MEMORY_NOTION_CHUNK_TARGET_CHARS=100,
)
class NotionMemoryConnectorTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Notion Adapter Org",
            domain="notion-adapter.mlai.test",
        )
        self.user = get_user_model().objects.create_user(email="notion@mlai.test")
        self.connection = ExternalServiceConnection.objects.create(
            provider="notion",
            user=self.user,
            organization=self.organization,
            access_token="secret-notion-token",
            external_account_id="workspace-1",
            account_label="Notion workspace",
        )
        self.configuration = MemoryConnectionConfiguration.objects.create(
            organization=self.organization,
            provider="notion",
            external_connection=self.connection,
            lifecycle_state=MemoryConnectionState.ACTIVE,
            created_by=self.user,
        )
        self.scope = MemorySourceScope.objects.create(
            configuration=self.configuration,
            scope_type="page_root",
            external_id="root-page",
            name="Company brain",
            selected=True,
            status=MemoryScopeStatus.SELECTED,
            default_classification="executive",
        )
        self.child_text = "The launch decision is approved."
        self.include_child = True
        self.calls = []

    @staticmethod
    def _page(page_id, title, edited="2026-07-22T01:00:00.000Z", parent=None):
        return {
            "object": "page",
            "id": page_id,
            "created_time": "2026-07-20T01:00:00.000Z",
            "last_edited_time": edited,
            "archived": False,
            "in_trash": False,
            "parent": parent or {"type": "workspace", "workspace": True},
            "url": f"https://www.notion.so/{page_id}",
            "properties": {
                "Name": {"type": "title", "title": [{"plain_text": title}]},
                "Status": {"type": "status", "status": {"name": "Published"}},
            },
        }

    @staticmethod
    def _block(block_id, kind, text="", has_children=False):
        return {
            "object": "block",
            "id": block_id,
            "type": kind,
            "created_time": "2026-07-20T01:00:00.000Z",
            "last_edited_time": "2026-07-22T01:00:00.000Z",
            "archived": False,
            "in_trash": False,
            "has_children": has_children,
            kind: {
                "rich_text": ([{"plain_text": text}] if text else []),
                **({"title": text} if kind in {"child_page", "child_database"} else {}),
            },
        }

    def _request(self, method, url, **kwargs):
        path = urlparse(url).path
        self.calls.append((method, path, kwargs))
        if method == "POST" and path == "/v1/search":
            return _Response(
                {
                    "results": [
                        self._page("root-page", "Company brain"),
                        {
                            "object": "data_source",
                            "id": "data-source-1",
                            "title": [{"plain_text": "Projects"}],
                            "url": "https://www.notion.so/data-source-1",
                        },
                    ],
                    "has_more": False,
                    "next_cursor": None,
                }
            )
        if method == "GET" and path == "/v1/pages/root-page":
            return _Response(self._page("root-page", "Company brain"))
        if method == "GET" and path == "/v1/pages/child-page":
            return _Response(
                self._page(
                    "child-page",
                    "Launch decision",
                    edited="2026-07-22T02:00:00.000Z",
                    parent={"type": "page_id", "page_id": "root-page"},
                )
            )
        if method == "GET" and path == "/v1/blocks/root-page/children":
            blocks = [
                self._block("heading-1", "heading_1", "Strategy"),
                self._block("toggle-1", "toggle", "Launch context", has_children=True),
            ]
            if self.include_child:
                blocks.append(self._block("child-page", "child_page", "Launch decision"))
            return _Response({"results": blocks, "has_more": False, "next_cursor": None})
        if method == "GET" and path == "/v1/blocks/toggle-1/children":
            return _Response(
                {
                    "results": [self._block("paragraph-1", "paragraph", "A nested strategy note.")],
                    "has_more": False,
                    "next_cursor": None,
                }
            )
        if method == "GET" and path == "/v1/blocks/child-page/children":
            return _Response(
                {
                    "results": [self._block("child-paragraph", "paragraph", self.child_text)],
                    "has_more": False,
                    "next_cursor": None,
                }
            )
        if method == "POST" and path == "/v1/data_sources/data-source-1/query":
            return _Response(
                {
                    "results": [self._page("child-page", "Launch decision")],
                    "has_more": False,
                    "next_cursor": None,
                }
            )
        raise AssertionError(f"Unexpected Notion request: {method} {path}")

    def _run_backfill(self, connector):
        records = []
        removals = []
        checkpoint = {}
        while True:
            page = connector.backfill(self.configuration, [self.scope], checkpoint)
            records.extend(page.records)
            removals.extend(page.removals)
            if not page.has_more:
                return page, records, removals
            checkpoint = page.checkpoint

    def _run_incremental(self, connector, cursor):
        records = []
        removals = []
        while True:
            page = connector.incremental_sync(self.configuration, cursor)
            records.extend(page.records)
            removals.extend(page.removals)
            cursor = page.next_cursor
            if not page.has_more:
                return page, records, removals

    @patch("org_memory.connectors.notion.http_client.request")
    def test_discovery_is_metadata_only_and_registry_uses_real_adapter(self, request):
        request.side_effect = self._request
        connector = connector_registry.get("notion")
        self.assertIsInstance(connector, NotionMemoryConnector)
        self.assertNotIsInstance(connector, MetadataOnlyMemoryConnector)
        self.assertEqual(connector_registry.validate_conformance("notion"), [])

        discovery = connector.discover_scopes(self.configuration)

        self.assertEqual(
            [(row.scope_type, row.external_id) for row in discovery.scopes],
            [("page_root", "root-page"), ("data_source", "data-source-1")],
        )
        self.assertEqual([path for _method, path, _kwargs in self.calls], ["/v1/search"])
        self.assertFalse(NotionPageArtifact.objects.exists())
        self.assertFalse(MemorySource.objects.exists())

    @patch("org_memory.connectors.notion.http_client.request")
    def test_recursive_durable_backfill_locators_versions_and_access_reconciliation(self, request):
        request.side_effect = self._request
        connector = connector_registry.get("notion")
        page, records, removals = self._run_backfill(connector)
        self.assertEqual(removals, [])
        self.assertEqual(len(records), 2)
        self.assertEqual(NotionPageArtifact.objects.count(), 2)
        self.assertEqual(NotionBlockArtifact.objects.count(), 5)

        child = NotionPageArtifact.objects.get(notion_page_id="child-page")
        self.assertEqual(child.ancestor_page_ids, ["root-page"])
        self.assertEqual(child.selected_root_ids, ["root-page"])
        nested = NotionBlockArtifact.objects.get(notion_block_id="paragraph-1")
        self.assertEqual(nested.depth, 1)
        self.assertEqual(nested.heading_path, ["Strategy"])
        child_record = next(row for row in records if row["external_id"] == "notion_page:child-page")
        block_chunk = next(row for row in child_record["chunks"] if row["chunk_kind"] == "notion_blocks")
        self.assertEqual(block_chunk["source_locator"]["start_block_id"], "child-paragraph")
        self.assertNotIn("secret-notion-token", repr(records))
        self.assertEqual(self.connection.sync_cursor, {})

        for record in records:
            _capture_record(self.configuration, record)
        child_source = MemorySource.objects.get(external_id="notion_page:child-page")
        self.assertEqual(child_source.versions.count(), 1)
        self.assertTrue(child_source.current_version.acl_snapshot.is_accessible)

        self.child_text = "The launch decision is approved and funded."
        changed_page, changed_records, _removals = self._run_incremental(connector, page.next_cursor)
        for record in changed_records:
            _capture_record(self.configuration, record)
        child_source.refresh_from_db()
        self.assertEqual(child_source.versions.count(), 2)
        self.assertIn("funded", child_source.current_version.bounded_excerpt)

        self.include_child = False
        _final, final_records, final_removals = self._run_incremental(
            connector, changed_page.next_cursor
        )
        for record in final_records:
            _capture_record(self.configuration, record)
        for removal in final_removals:
            _apply_removal(self.configuration, removal)
        child.refresh_from_db()
        child_source.refresh_from_db()
        self.assertEqual(child.lifecycle_state, NotionArtifactState.ACCESS_LOST)
        self.assertEqual(child_source.lifecycle_state, MemorySourceLifecycle.ACCESS_REVOKED)
        self.assertFalse(child_source.current_version.chunks.filter(active_for_retrieval=True).exists())

    @patch("org_memory.connectors.notion.http_client.request")
    def test_selected_data_source_root_queries_rows_before_fetching_page_content(self, request):
        request.side_effect = self._request
        self.scope.scope_type = "data_source"
        self.scope.external_id = "data-source-1"
        self.scope.save(update_fields=("scope_type", "external_id", "updated_at"))
        connector = connector_registry.get("notion")

        _page, records, removals = self._run_backfill(connector)

        self.assertEqual(removals, [])
        self.assertEqual([row["external_id"] for row in records], ["notion_page:child-page"])
        paths = [path for _method, path, _kwargs in self.calls]
        self.assertLess(paths.index("/v1/data_sources/data-source-1/query"), paths.index("/v1/pages/child-page"))
        artifact = NotionPageArtifact.objects.get(notion_page_id="child-page")
        self.assertEqual(artifact.selected_root_ids, ["data-source-1"])

    @patch("org_memory.connectors.notion.http_client.request")
    def test_trash_is_an_access_revocation_not_a_tombstone(self, request):
        def trashed_request(method, url, **kwargs):
            path = urlparse(url).path
            if path == "/v1/pages/root-page":
                payload = self._page("root-page", "Company brain")
                payload["in_trash"] = True
                return _Response(payload)
            return self._request(method, url, **kwargs)

        request.side_effect = trashed_request
        connector = connector_registry.get("notion")
        _page, records, _removals = self._run_backfill(connector)
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["acl"]["is_accessible"])
        _capture_record(self.configuration, records[0])
        source = MemorySource.objects.get(external_id="notion_page:root-page")
        self.assertEqual(source.lifecycle_state, MemorySourceLifecycle.ACCESS_REVOKED)
        self.assertNotEqual(source.lifecycle_state, MemorySourceLifecycle.TOMBSTONED)
        self.assertEqual(
            NotionPageArtifact.objects.get().lifecycle_state,
            NotionArtifactState.TRASHED,
        )
