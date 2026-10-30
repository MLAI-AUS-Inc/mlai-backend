"""P1.10: the draft tools a new-journey founder uses after "Copy article" (IF-P1-12, IF-P1-13).

- :func:`founder_draft_review` decides, for the article review view, whether a
  content_only draft may be copied while its preview rebuilds, and builds the
  ``founderDraft`` object. Legacy organisations get ``({}, None)``: today's
  export and no ``founderDraft``.
- ``POST runs/<run_id>/copied/`` records a copy receipt. It never approves or
  charges the run.
- ``POST runs/<run_id>/posted-link/`` stores where the founder posted the
  article, checking it over the SSRF-pinned fetcher when it is on their own
  website.
- ``GET runs/<run_id>/article/download/`` returns ``article.md``,
  ``article.html`` and the article's images as one zip.

The three endpoints sit behind ``ARTICLES_DRAFT_TOOLS_ENABLED`` and
``founder_journey_cohort.is_new_journey`` (404 otherwise), are scoped to the
selected company's runs, and are for the startup owner only. Receipts and
links live in ``pillar_strategy["founder_posted_articles"]`` (no migration):
``{run_id: {"url", "check", "checkedAt", "postedAt", "copiedAt", "copiedRevision"}}``,
newest first, at most 50 runs.

Error bodies are ``{"detail": <founder sentence>, "reason": <closed enum>}``
with ``reason`` in ``not_ready``, ``revision_changed``, ``invalid_url`` and
``not_owner``.
"""
from __future__ import annotations

import html as html_lib
import http.client
import ipaddress
import logging
import re
import socket
import ssl
import time
import unicodedata
import zipfile
from copy import deepcopy
from io import BytesIO
from urllib.parse import urlsplit, urlunsplit

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from . import founder_journey_cohort as cohort
from . import vibe_marketing_views as views
from .article_export import article_export, public_export_url

logger = logging.getLogger(__name__)

TOOLS_FLAG = "ARTICLES_DRAFT_TOOLS_ENABLED"
COPY_FLAG = "ARTICLES_COPY_WITHOUT_PREVIEW_ENABLED"
POSTED_KEY = cohort.POSTED_ARTICLES_KEY
MAX_POSTED_RUNS = 50
MAX_URL_LENGTH = 2048
MAX_REVISION_LENGTH = 200
IMAGE_HOSTS = frozenset({"firebasestorage.googleapis.com", "storage.googleapis.com"})
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGES_TOTAL_BYTES = 25 * 1024 * 1024
IMAGE_DEADLINE_SECONDS = 20
IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif", "image/avif": "avif"}
# Hostnames that are never a public place to post an article.
_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".invalid", ".test", ".example", ".lan", ".home.arpa", ".corp")
# Live-route outcomes that mean the page answered and is not this article.
_NOT_MATCHED_FETCH = frozenset({"deployment_origin_invalid", "deployment_body_oversized", "deployment_route_contract_invalid",
                                "deployment_route_unverified", "deployment_tls_invalid"})
MATCH_SHARE = 0.6

# Founder copy (contract section 4, IF-P1-13). No codes, no dashes.
NOT_FOUND = "Not found."
RUN_NOT_FOUND = "Run not found."
NOT_READY_COPY = "Your article isn't ready to copy yet."
NOT_READY_DOWNLOAD = "Your article isn't ready to download yet."
REVISION_CHANGED = "Your draft just changed. Copy it again."
INVALID_URL = "That doesn't look like a web address."
NOT_OWNER = "Only the startup owner can do this."


def tools_enabled() -> bool:
    """True when ``ARTICLES_DRAFT_TOOLS_ENABLED`` is on."""
    return bool(getattr(settings, TOOLS_FLAG, False))


def copy_without_preview_enabled() -> bool:
    """True when ``ARTICLES_COPY_WITHOUT_PREVIEW_ENABLED`` is on."""
    return bool(getattr(settings, COPY_FLAG, False))


def _mapping(value):
    return value if isinstance(value, dict) else {}


def is_content_only(run) -> bool:
    """True when the run delivers a portable (copy-ready) draft."""
    return views._run_delivery_mode(run) == "content_only"


def is_delivered_content_only(run) -> bool:
    """A content_only article run whose draft has been delivered and not discarded."""
    from .article_publication_evidence import useful_delivery
    from .run_cancellation import DRAFT_DELETED_AT_KEY
    from .run_state import ARTICLE_WORKFLOWS
    if run.workflow not in ARTICLE_WORKFLOWS or not is_content_only(run):
        return False
    if run.status in {"cancelled", "denied"} or _mapping(run.result).get(DRAFT_DELETED_AT_KEY):
        return False
    return run.status == "completed" or useful_delivery(run)


def _config(organization):
    from .models import OrganizationContentConfig
    if organization is None or not getattr(organization, "pk", None):
        return None
    return OrganizationContentConfig.objects.filter(organization_id=organization.pk).first()


def posted_record(config, run_id) -> dict:
    """The stored receipt and link of one run, or ``{}``."""
    posted = _mapping(_mapping(getattr(config, "pillar_strategy", None)).get(POSTED_KEY))
    return dict(_mapping(posted.get(str(run_id))))


def _site_profile(config):
    first = _mapping(_mapping(getattr(config, "pillar_strategy", None)).get(cohort.FIRST_PROFILE_KEY))
    return _mapping(first.get("siteProfile"))


def _platform(profile):
    value = profile.get("platform")
    return value.strip().lower() if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", value.strip()) else None


def _posted_link(record):
    url = record.get("url")
    if not isinstance(url, str) or not url:
        return None
    check = record.get("check") if record.get("check") in {"pending", "matched", "not_matched", "skipped"} else "pending"
    checked = record.get("checkedAt")
    return {"url": url, "check": check, "checkedAt": checked if isinstance(checked, str) else None}


def _copied(record):
    at, revision = record.get("copiedAt"), record.get("copiedRevision")
    if not isinstance(at, str) or not at or not isinstance(revision, str):
        return None
    return {"at": at, "revision": revision}


def _is_owner(user, organization) -> bool:
    from founder_tools.services import user_may_use_organization
    return bool(user is not None and getattr(user, "is_authenticated", False)
                and user_may_use_organization(user, organization))


def founder_draft_review(*, context, run, user):
    """``(article_export keyword arguments, founderDraft or None)`` for the article review view.

    For a legacy organisation it returns ``({}, None)``, so the review stays
    exactly as it is today. For a new-journey organisation:

    - ``allow_preview_pending`` when ``ARTICLES_COPY_WITHOUT_PREVIEW_ENABLED``
      is on and the run is content_only (``founderDraft.copyWhilePreviewPending``);
    - ``copy_formats`` (``html`` and ``metaTitle``) with the same flag;
    - ``founderDraft`` with the receipt, the posted link and the site profile's
      ``platform`` and ``blogUrl``; ``tools`` is true for the owner while
      ``ARTICLES_DRAFT_TOOLS_ENABLED`` is on.
    """
    organization = getattr(context, "organization", None)
    if organization is None or not cohort.is_new_journey(organization):
        return {}, None
    config = _config(organization)
    copy_while_pending = copy_without_preview_enabled() and is_content_only(run)
    record = posted_record(config, run.run_id)
    profile = _site_profile(config)
    blog_url = public_export_url(profile.get("blogUrl")) or None
    founder_draft = {
        "version": 1,
        "copyWhilePreviewPending": copy_while_pending,
        "tools": tools_enabled() and _is_owner(user, organization),
        "postedLink": _posted_link(record),
        "copied": _copied(record),
        "platform": _platform(profile),
        "blogUrl": blog_url,
    }
    return {"allow_preview_pending": copy_while_pending, "copy_formats": copy_without_preview_enabled()}, founder_draft


def current_export(context, run):
    """``(export, error_response)``: the run's current export, read the way the review view reads it."""
    from .article_review_views import remote_review
    review = remote_review(run)
    if isinstance(review, Response):
        return None, review
    latest = views._latest_review_ready_component_revision(run, context)
    allow_pending = copy_without_preview_enabled() and is_content_only(run)
    export = article_export(review, run_id=run.run_id, latest_run_id=latest.run_id if latest else None,
                            allow_preview_pending=allow_pending, copy_formats=True)
    return export, None


def _store(organization, run_id, changes) -> dict:
    """Merge ``changes`` into the run's record under the organisation lock; newest first, at most 50 runs."""
    from organizations.models import Organization
    from .models import OrganizationContentConfig
    run_id = str(run_id)
    with transaction.atomic():
        # Lock order matches every other pillar_strategy writer (strategy sync, marker writes).
        Organization.objects.select_for_update().only("pk").get(pk=organization.pk)
        OrganizationContentConfig.objects.get_or_create(organization_id=organization.pk)
        config = OrganizationContentConfig.objects.select_for_update().get(organization_id=organization.pk)
        strategy = deepcopy(config.pillar_strategy) if isinstance(config.pillar_strategy, dict) else {}
        posted = _mapping(strategy.get(POSTED_KEY))
        record = {**_mapping(posted.get(run_id)), **changes}
        ordered = {run_id: record}
        for key, value in posted.items():
            if key != run_id and isinstance(value, dict) and len(ordered) < MAX_POSTED_RUNS:
                ordered[key] = value
        strategy[POSTED_KEY] = ordered
        config.pillar_strategy = strategy
        config.save(update_fields=["pillar_strategy", "updated_at"])
    return record


def _refusal(detail, reason, status):
    return Response({"detail": detail, "reason": reason}, status=status, headers={"Cache-Control": "private, no-store"})


class _FounderDraftToolView(APIView):
    """Shared gate: flag, company scope, run ownership, cohort, then the owner check."""

    def _resolve(self, request, run_id):
        from workflow_runs.models import ContentFactoryRun
        if not tools_enabled():
            return None, None, Response({"detail": NOT_FOUND}, status=404)
        context, error = views._resolve_context_or_response(request, require_domain=False)
        if error is not None:
            return None, None, error
        run = ContentFactoryRun.objects.filter(run_id=run_id).first()
        if run is None or not views._run_belongs_to_context(run, context):
            return None, None, Response({"detail": RUN_NOT_FOUND}, status=404)
        if not cohort.is_new_journey(context.organization):
            return None, None, Response({"detail": NOT_FOUND}, status=404)
        if not _is_owner(request.user, context.organization):
            return None, None, _refusal(NOT_OWNER, "not_owner", 403)
        return context, run, None


class FounderDraftCopiedView(_FounderDraftToolView):
    """POST: record that the founder copied this exact revision. Never approves or charges the run."""

    def post(self, request, run_id):
        context, run, error = self._resolve(request, run_id)
        if error is not None:
            return error
        data = request.data if isinstance(request.data, dict) else {}
        revision = data.get("revision")
        if not is_delivered_content_only(run):
            return _refusal(NOT_READY_COPY, "not_ready", 409)
        if not isinstance(revision, str) or not revision or len(revision) > MAX_REVISION_LENGTH:
            return _refusal(REVISION_CHANGED, "revision_changed", 409)
        stored = _copied(posted_record(_config(context.organization), run.run_id))
        if stored is not None and stored["revision"] == revision:
            return Response({"copied": stored}, headers={"Cache-Control": "private, no-store"})
        export, error = current_export(context, run)
        if error is not None or export.get("status") != "ready":
            return _refusal(NOT_READY_COPY, "not_ready", 409)
        if export.get("revision") != revision:
            return _refusal(REVISION_CHANGED, "revision_changed", 409)
        at = timezone.now().isoformat()
        record = _store(context.organization, run.run_id, {"copiedAt": at, "copiedRevision": revision})
        logger.info("founder_draft_copied org=%s run=%s", context.organization.pk, run.run_id)
        return Response({"copied": _copied(record)}, headers={"Cache-Control": "private, no-store"})


def clean_posted_url(value):
    """``(url, host)`` for an https link to a public host of at most 2048 characters, else None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_URL_LENGTH or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or port not in {None, 443}:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if not address.is_global:
            return None
    elif host == "localhost" or "." not in host or host.endswith(_PRIVATE_SUFFIXES):
        return None
    return value, host


def own_site_host(organization, host) -> bool:
    """True when ``host`` is the organisation's domain or one of its subdomains."""
    if not cohort.organization_has_website(organization):
        return False
    apex = str(organization.domain or "").strip().lower().rstrip(".").removeprefix("www.")
    return bool(apex) and (host == apex or host.endswith("." + apex))


def _normal_text(value):
    text = unicodedata.normalize("NFC", value or "")
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(text.split())


def _page_text(raw):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(raw, "html.parser")
    for ignored in soup.select("script, style, template, noscript"):
        ignored.decompose()
    return _normal_text(soup.get_text(" ", strip=True))


def _contains_article(expected_html, observed) -> bool:
    """A lenient match for a pasted article: most of its longer sentences appear on the page."""
    expected = _page_text(expected_html)
    sentences = [part for part in re.split(r"(?<=[.!?])\s+", expected) if len(part.split()) >= 8]
    if not sentences:
        return False
    page = _page_text(observed)
    return sum(1 for sentence in sentences if sentence in page) / len(sentences) >= MATCH_SHARE


def check_posted_link(organization, url, host, export) -> str:
    """``matched``, ``not_matched`` or ``pending`` for a link on the founder's own site.

    The page is read with ``website_live_fetch.fetch_live_route`` (DNS pinned,
    public addresses only, bounded), then compared with
    ``article_live_evidence.compare_live_body``. A pasted article rarely
    reproduces the exact body hash, so a page that holds most of the article's
    sentences also counts as matched. A transient fetch is ``pending``.
    """
    from .article_live_evidence import compare_live_body
    from .website_contract import WebsiteAuthorityError
    from .website_live_fetch import fetch_live_route
    if not isinstance(export, dict) or export.get("status") != "ready":
        return "pending"
    parsed = urlsplit(url)
    target = urlunsplit(("https", host, parsed.path or "/", "", ""))
    try:
        body, _headers = fetch_live_route(target, host)
    except WebsiteAuthorityError as exc:
        return "not_matched" if getattr(exc, "authority_code", None) in _NOT_MATCHED_FETCH else "pending"
    except Exception:  # noqa: BLE001 - a fetch problem never fails the founder's save
        return "pending"
    expected = expected_article_html(export)
    try:
        if compare_live_body(expected, body, canonical_url=target).get("state") == "verified":
            return "matched"
    except ValueError:
        pass
    return "matched" if _contains_article(expected, body) else "not_matched"


class FounderDraftPostedLinkView(_FounderDraftToolView):
    """POST: store the link where the founder posted this article."""

    def post(self, request, run_id):
        from .run_state import ARTICLE_WORKFLOWS
        context, run, error = self._resolve(request, run_id)
        if error is not None:
            return error
        if run.workflow not in ARTICLE_WORKFLOWS:
            return _refusal(NOT_READY_COPY, "not_ready", 409)
        data = request.data if isinstance(request.data, dict) else {}
        cleaned = clean_posted_url(data.get("url"))
        if cleaned is None:
            return _refusal(INVALID_URL, "invalid_url", 400)
        url, host = cleaned
        now = timezone.now()
        if own_site_host(context.organization, host):
            export, error = current_export(context, run)
            check = check_posted_link(context.organization, url, host, export) if error is None else "pending"
        else:
            check = "skipped"
        checked_at = now.isoformat() if check in {"matched", "not_matched"} else None
        record = _store(context.organization, run.run_id,
                        {"url": url, "check": check, "checkedAt": checked_at, "postedAt": now.isoformat()})
        logger.info("founder_draft_posted org=%s run=%s check=%s", context.organization.pk, run.run_id, check)
        return Response({"postedLink": _posted_link(record)}, headers={"Cache-Control": "private, no-store"})


# ---------------------------------------------------------------------------
# Download


_LIST_MARKER = re.compile(r"^\s*([-*+]|\d+\.)\s+")


def _inline(text):
    text = html_lib.escape(text, quote=True)
    text = re.sub(r"!\[([^\]]*)\]\((https://[^)\s]+)\)", r'<img src="\2" alt="\1">', text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])", r"<em>\1</em>", text)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", text)


def markdown_html(markdown) -> str:
    """A small Markdown to HTML conversion for exports that carry no ``html``.

    Headings, paragraphs, lists, links, images, bold, italics and code. Text
    is escaped first, so nothing in the article becomes markup by accident.
    """
    blocks = []
    for block in re.split(r"\n\s*\n", str(markdown or "").strip()):
        lines = [line.rstrip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        heading = re.fullmatch(r"(#{1,6})\s+(.*)", lines[0].strip())
        if heading and len(lines) == 1:
            level = len(heading.group(1))
            blocks.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            continue
        if all(re.match(r"\s*([-*+]|\d+\.)\s+", line) for line in lines):
            tag = "ol" if re.match(r"\s*\d+\.", lines[0]) else "ul"
            items = "".join("<li>" + _inline(_LIST_MARKER.sub("", line)) + "</li>" for line in lines)
            blocks.append(f"<{tag}>{items}</{tag}>")
            continue
        if heading:
            level = len(heading.group(1))
            blocks.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            lines = lines[1:]
        blocks.append(f"<p>{_inline(' '.join(line.strip() for line in lines))}</p>")
    return "\n".join(blocks)


def _title(export):
    metadata = _mapping(export.get("metadata"))
    for value in (metadata.get("title"), export.get("metaTitle")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Article"


def expected_article_html(export) -> str:
    """The article as one ``<article>`` with a single ``h1``, from the export's html or Markdown."""
    body = export.get("html") if isinstance(export.get("html"), str) and export.get("html") else markdown_html(export.get("markdown"))
    if re.search(r"<h1[\s>]", body, re.IGNORECASE):
        return f"<article>{body}</article>"
    return f"<article><h1>{html_lib.escape(_title(export))}</h1>{body}</article>"


def article_document(export) -> str:
    """``article.html``: a complete page around the article."""
    title = html_lib.escape(_title(export))
    description = _mapping(export.get("metadata")).get("description")
    meta = f'<meta name="description" content="{html_lib.escape(description, quote=True)}">' if isinstance(description, str) and description else ""
    return (f'<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<title>{title}</title>\n{meta}\n</head>\n'
            f"<body>\n{expected_article_html(export)}\n</body>\n</html>\n")


class _ImageSkipped(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def fetch_image(url, *, limit, deadline):
    """``(bytes, extension)`` for an MLAI storage image, over a DNS-pinned HTTPS connection with no redirects."""
    from agent_runtime.webhook_transport import CallbackEndpointError, resolve_callback
    host = (urlsplit(url).hostname or "").lower()
    if host not in IMAGE_HOSTS:
        raise _ImageSkipped("not stored by MLAI")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _ImageSkipped("the download took too long")
    try:
        hostname, address, target = resolve_callback(url, dns_timeout=min(3, remaining))
    except CallbackEndpointError:
        raise _ImageSkipped("could not be downloaded") from None
    connection = http.client.HTTPSConnection(hostname, 443, timeout=max(0.001, deadline - time.monotonic()),
                                             context=ssl.create_default_context())

    def pinned_connection(_destination, socket_timeout, source_address=None):
        return socket.create_connection((address, 443), min(socket_timeout, max(0.001, deadline - time.monotonic())), source_address)

    connection._create_connection = pinned_connection
    try:
        connection.request("GET", target, headers={"Accept": "image/*", "User-Agent": "MLAI-ArticleDownload/1.0 (+https://mlai.au)"})
        response = connection.getresponse()
        if response.status != 200:
            raise _ImageSkipped("could not be downloaded")
        kind = (response.getheader("Content-Type") or "").split(";")[0].strip().lower()
        extension = IMAGE_TYPES.get(kind)
        if extension is None:
            raise _ImageSkipped("not a supported image type")
        declared = response.getheader("Content-Length") or ""
        if declared.isdigit() and int(declared) > limit:
            raise _ImageSkipped("larger than 8 MB" if limit >= MAX_IMAGE_BYTES else "over the 25 MB download limit")
        data = response.read(limit + 1)
        if len(data) > limit:
            raise _ImageSkipped("larger than 8 MB" if limit >= MAX_IMAGE_BYTES else "over the 25 MB download limit")
        return data, extension
    except (OSError, http.client.HTTPException):
        raise _ImageSkipped("could not be downloaded") from None
    finally:
        connection.close()


def article_zip(export, *, fetch=None) -> bytes:
    """The zip: ``article.md``, ``article.html``, ``images/<n>.<ext>`` and ``images/README.txt``.

    Only MLAI storage images, at most 10, 8 MB each and 25 MB in total. Every
    image left out is listed in the README with the reason.
    """
    fetch = fetch or fetch_image
    buffer = BytesIO()
    included, skipped, total = [], [], 0
    deadline = time.monotonic() + IMAGE_DEADLINE_SECONDS
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("article.md", export.get("markdown") or "")
        archive.writestr("article.html", article_document(export))
        for item in export.get("media") or []:
            url = item.get("url") if isinstance(item, dict) else None
            if not isinstance(url, str) or not url:
                continue
            if (urlsplit(url).hostname or "").lower() not in IMAGE_HOSTS:
                skipped.append((url, "not stored by MLAI"))
                continue
            if len(included) >= MAX_IMAGES:
                skipped.append((url, "more than 10 images"))
                continue
            limit = min(MAX_IMAGE_BYTES, MAX_IMAGES_TOTAL_BYTES - total)
            if limit <= 0:
                skipped.append((url, "over the 25 MB download limit"))
                continue
            try:
                data, extension = fetch(url, limit=limit, deadline=deadline)
            except _ImageSkipped as exc:
                skipped.append((url, exc.reason))
                continue
            name = f"images/{len(included) + 1}.{extension}"
            archive.writestr(name, data)
            total += len(data)
            included.append((name, item.get("alt") or "", url))
        if included or skipped:
            lines = ["Images for your article.", ""]
            for name, alt, _url in included:
                short = name.removeprefix("images/")
                lines.append(f"{short}: {alt}" if alt else short)
            if skipped:
                lines += ["", "Not included:"] + [f"{url} ({reason})" for url, reason in skipped]
            archive.writestr("images/README.txt", "\n".join(lines) + "\n")
    return buffer.getvalue()


def _filename(export):
    slug = _mapping(export.get("metadata")).get("slug")
    clean = re.sub(r"[^a-z0-9-]+", "-", slug.lower()).strip("-")[:80] if isinstance(slug, str) else ""
    return f"{clean or 'article'}.zip"


class FounderDraftDownloadView(_FounderDraftToolView):
    """GET: the article and its images as one zip."""

    def get(self, request, run_id):
        context, run, error = self._resolve(request, run_id)
        if error is not None:
            return error
        export, error = current_export(context, run)
        if error is not None or export.get("status") != "ready":
            return _refusal(NOT_READY_DOWNLOAD, "not_ready", 409)
        response = HttpResponse(article_zip(export), content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="{_filename(export)}"'
        response["Cache-Control"] = "private, no-store"
        logger.info("founder_draft_downloaded org=%s run=%s", context.organization.pk, run.run_id)
        return response
