"""Read-only look at the Meeting Notes database for setting up Notion agenda items.

``db`` prints the database's data sources and their templates (the IDs for
NOTION_AGENDA_DATA_SOURCE_ID / NOTION_AGENDA_TEMPLATE_ID). ``page`` prints a
meeting page's block tree so the agenda page logic can be checked against it.
Output contains real meeting content: read it in the terminal, don't commit it.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from integrations import http_client
from integrations.services.notion_agenda_client import NOTION_BASE_URL, NotionAgendaClient, NotionAgendaError

MAX_DEPTH = 3
TEXT_PREVIEW = 80


class ReadOnlyNotionClient(NotionAgendaClient):
    """Agenda client that needs only the token and refuses anything but GET."""

    def __init__(self):
        self.token = str(getattr(settings, "NOTION_AGENDA_API_TOKEN", "") or "").strip()
        if not self.token:
            raise CommandError("NOTION_AGENDA_API_TOKEN is not set.")
        self.data_source_id = ""
        self.template_id = ""
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Notion-Version": str(getattr(settings, "NOTION_API_VERSION", "2026-03-11")),
        }
        self._title_property = None

    def request(self, method: str, path: str, *, params=None, body=None) -> dict[str, Any]:
        if method.upper() != "GET":
            raise CommandError(f"Refusing {method} {path}: this command is read-only.")
        # Setup tool: show Notion's own error, which the endpoint's client hides on purpose.
        try:
            response = http_client.request(
                "GET", f"{NOTION_BASE_URL}{path}", headers=self.headers, params=params, timeout=(3, 20)
            )
        except http_client.RequestException as exc:
            raise CommandError(f"Couldn't reach Notion: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            try:
                error = response.json()
            except ValueError:
                error = {}
            raise CommandError(
                f"Notion {response.status_code} {error.get('code', '')}: {error.get('message', '')} (GET {path})"
            )
        return response.json()


def block_text(block: dict[str, Any]) -> str:
    content = block.get(block.get("type") or "")
    rich_text = content.get("rich_text") if isinstance(content, dict) else None
    if not isinstance(rich_text, list):
        return ""
    return "".join(str(part.get("plain_text") or "") for part in rich_text if isinstance(part, dict))


def describe_block(block: dict[str, Any]) -> str:
    block_type = str(block.get("type") or "?")
    content = block.get(block_type) if isinstance(block.get(block_type), dict) else {}
    flags = []
    if content.get("is_toggleable"):
        flags.append("TOGGLE")
    if block.get("has_children"):
        flags.append("has_children")
    text = block_text(block)
    if len(text) > TEXT_PREVIEW:
        text = text[: TEXT_PREVIEW - 1] + "…"
    shown = repr(text) if text else "(empty)"
    suffix = f" [{', '.join(flags)}]" if flags else ""
    return f"{block_type}{suffix} {shown}  id={block.get('id')}"


class Command(BaseCommand):
    help = "Read-only: print Meeting Notes data sources/templates, or a meeting page's block tree."
    # System checks import the URL routes, which fail on Windows (os.register_at_fork).
    requires_system_checks = []

    def add_arguments(self, parser):
        subcommands = parser.add_subparsers(dest="target", required=True)
        db = subcommands.add_parser("db", help="Data sources and templates of a database.")
        db.add_argument("database_id", help="Database ID from the Meeting Notes URL.")
        page = subcommands.add_parser("page", help="Block tree of one page.")
        page.add_argument("page_id", help="Page ID from the meeting page URL.")

    def handle(self, *args, **options):
        client = ReadOnlyNotionClient()
        try:
            if options["target"] == "db":
                self._print_database(client, options["database_id"])
            else:
                self._print_page(client, options["page_id"])
        except NotionAgendaError as exc:
            raise CommandError(f"{exc.code}: {exc}") from exc

    def _print_database(self, client: NotionAgendaClient, database_id: str) -> None:
        database = client.request("GET", f"/databases/{database_id}")
        data_sources = [ds for ds in database.get("data_sources") or [] if isinstance(ds, dict)]
        if not data_sources:
            raise CommandError("The database lists no data sources.")
        for data_source in data_sources:
            self.stdout.write(f"data source {data_source.get('name')!r}  id={data_source.get('id')}")
            payload = client.request("GET", f"/data_sources/{data_source.get('id')}/templates")
            templates = [t for t in payload.get("templates") or [] if isinstance(t, dict)]
            if not templates:
                self.stdout.write("  (no templates)")
            for template in templates:
                default = "  DEFAULT" if template.get("is_default") else ""
                self.stdout.write(f"  template {template.get('name')!r}  id={template.get('id')}{default}")

    def _print_page(self, client: NotionAgendaClient, page_id: str) -> None:
        page = client.request("GET", f"/pages/{page_id}")
        self.stdout.write(f"page {page.get('url')}")
        self._print_children(client, page_id, depth=0)

    def _print_children(self, client: NotionAgendaClient, block_id: str, *, depth: int) -> None:
        for block in client.list_children(block_id):
            self.stdout.write("  " * depth + describe_block(block))
            if block.get("has_children"):
                if depth + 1 >= MAX_DEPTH:
                    self.stdout.write("  " * (depth + 1) + "…")
                else:
                    self._print_children(client, str(block.get("id")), depth=depth + 1)
