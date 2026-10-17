"""Durable private native-artifact collection, without CMS publication approval."""

from copy import deepcopy
from datetime import timedelta
import json
import re
from urllib.parse import quote, urlsplit
from uuid import uuid4

from bs4 import BeautifulSoup
from bs4.element import Comment, Tag
from django.db import transaction
from django.db.models import Exists, F, OuterRef
from django.utils import timezone
import requests

from . import channel_artifacts
from .channel_artifact_contract import digest
from .cms_artifact_contract import validate_handoff, CmsArtifactError, MAX_HANDOFF_BYTES
from .cms_native_html import native_fragment, NativeHtmlError
from .website_contract import WebsiteAuthorityError

ACTION = "cms-review"
LEASE_SECONDS = 90
REVIEW_VERSION = "2026-10-10.1"
IDENTITY = {"schema_version", "binding", "credential_operation_id", "source_run_id", "source_generation",
    "envelope_digest", "request_sha256", "expected_revision"}


class ArtifactReadError(ValueError):
    """A fixed local category, without worker bodies, keys or article source."""

    def __init__(self, category):
        self.category = category
        super().__init__("Native CMS artifact collection could not finish.")


def _deny():
    return WebsiteAuthorityError("website_connection_changed", "The original CMS article, connection or review changed.")


def _source(run):
    if (not channel_artifacts.enabled() or run is None or run.github_repo or
            run.workflow not in {"article_generation", "direct_generate", "confirmed_topic"} or
            run.status not in {"completed", "awaiting_approval", "approval_required"} or
            not isinstance(run.result, dict) or type(run.result.get("generation")) is not int or
            not 0 <= run.result["generation"] < 10**12):
        raise _deny()
    admitted = channel_artifacts.authorize_payload(run.run_request)
    if str(run.organization_id) != admitted.envelope["organization_id"]:
        raise _deny()
    return admitted


def _identity(run, admitted, revision):
    return {"schema_version": 1, "binding": admitted.scope.binding,
        "credential_operation_id": admitted.operation.payload["credential_operation_id"],
        "source_run_id": run.run_id, "source_generation": run.result["generation"],
        "envelope_digest": admitted.envelope["digest"],
        "request_sha256": channel_artifacts._request_identity(run.run_request), "expected_revision": revision}


def start_review(run, *, expected_revision=None):
    """Reserve collection from original backend intent; this is never approval."""
    if expected_revision is not None and (not isinstance(expected_revision, str) or re.fullmatch(r"[a-f0-9]{64}", expected_revision) is None):
        raise _deny()
    from organizations.models import Organization
    from workflow_runs.models import ContentFactoryRun
    from .website_models import WebsiteConnectionOperation
    admitted = _source(run)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=run.organization_id)
        fresh = ContentFactoryRun.objects.select_for_update().filter(pk=run.pk).first()
        admitted = _source(fresh)
        identity = _identity(fresh, admitted, expected_revision)
        operation, created = WebsiteConnectionOperation.objects.get_or_create(
            idempotency_key="cms-review:" + digest(identity), defaults={"action": ACTION,
                "connection": admitted.scope.connection, "generation": admitted.scope.binding["generation"],
                "state": "pending", "next_attempt_at": timezone.now(),
                "payload": {**identity, "failures": 0},
                "receipt": {"status": "artifact_pending", "channel": "cms", "provider_modified": False,
                    "publishing_verified": False, "approval_recorded": False}})
        if (operation.action != ACTION or {key: operation.payload.get(key) for key in IDENTITY} != identity or
                operation.generation != admitted.scope.binding["generation"] or
                str(operation.connection_id) != admitted.scope.binding["connection_id"]):
            raise _deny()
        return operation


def _signature(body):
    text = lambda node: " ".join(node.get_text(" ", strip=True).split())
    return {"text": text(body), "headings": [(node.name, text(node)) for node in body.find_all(re.compile(r"^h[1-6]$"))],
        "links": [(text(node), node.get("href")) for node in body.find_all("a")],
        "images": [(node.get("src"), node.get("alt", "")) for node in body.find_all("img")],
        "preformatted_text": [node.get_text("", strip=False) for node in body.find_all(["pre", "code"])],
        "table": [[text(cell) for cell in row.find_all(["th", "td"], recursive=False)] for row in body.select("tr")],
        "list": [text(node) for node in body.find_all("li")]}


def validate_review_surface(value):
    """Independently compare inert owner preview with exact native source."""
    value = validate_handoff(value)
    try:
        article = value["article"]
        opening = re.match(r'\A<header data-cf-cms-preview-title(?:="")?>', value["preview_html"])
        if not opening or value["preview_html"].count("</header>") != 1:
            raise ValueError()
        raw_header, raw_body = value["preview_html"][opening.end():].split("</header>", 1)
        native_fragment(raw_body, title=article["title"])
        if native_fragment("<article>" + raw_header + "</article>", title=article["title"]) != native_fragment("<article></article>", title=article["title"]):
            raise ValueError()
        source = BeautifulSoup(article["html"], "html.parser")
        preview = BeautifulSoup(value["preview_html"], "html.parser")
        roots = [node for node in preview.contents if not isinstance(node, Comment) and (isinstance(node, Tag) or str(node).strip())]
        if len(roots) != 2 or any(not isinstance(node, Tag) for node in roots):
            raise ValueError()
        header, body = roots
        if (header.name != "header" or set(header.attrs) != {"data-cf-cms-preview-title"} or
                len(header.find_all("h1")) != 1 or header.get_text(" ", strip=True) != article["title"] or
                body.name != "article" or not body.has_attr("data-cf-article-body") or body.select("h1") or
                any(node.has_attr("style") or node.has_attr("hidden") or node.has_attr("inert") or
                    str(node.get("aria-hidden", "")).lower() == "true" for node in [*source.find_all(True), *preview.find_all(True)])):
            raise ValueError()
        if native_fragment("<article>" + header.decode_contents() + "</article>", title=article["title"]) != native_fragment("<article></article>", title=article["title"]):
            raise ValueError()
        native_fragment(str(body), title=article["title"])
        if (_signature(source.article) != _signature(body) or
                [(node.get("src"), node.get("alt", "")) for node in source.find_all("img")] !=
                [(image["src"], image["alt"]) for image in value["images"]] or
                value["editorial_acceptance"]["review_version"] != REVIEW_VERSION):
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError, KeyError, NativeHtmlError):
        raise CmsArtifactError("The native CMS review surface changed.") from None


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError()
        value[key] = item
    return value


def fetch_handoff(run, *, expected_revision=None):
    """Read bounded service evidence without redirects, provider keys or retries."""
    from .vibe_marketing_views import _content_factory_remote_config, _content_factory_headers
    from .website_connections import require_unlocked_remote_call
    require_unlocked_remote_call()
    config = _content_factory_remote_config()
    base = config["base_url"]
    parsed = urlsplit(base)
    if (not config["enabled"] or not config["api_key_configured"] or parsed.scheme != "https" or
            not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or
            not isinstance(run.run_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run.run_id) is None):
        raise ArtifactReadError("unavailable")
    try:
        with requests.get(base + "/api/runs/" + quote(run.run_id, safe="") + "/channel-artifact",
                params={"expected_revision": expected_revision} if expected_revision else {},
                headers={**_content_factory_headers(), "Accept-Encoding": "identity"},
                timeout=(5, 30), stream=True, allow_redirects=False) as response:
            if response.status_code == 429 or response.status_code >= 500:
                raise ArtifactReadError("unavailable")
            if response.status_code != 200:
                raise ArtifactReadError("changed")
            if (response.headers.get("Content-Type", "").split(";", 1)[0].lower().strip() != "application/json" or
                    response.headers.get("Content-Encoding", "identity").lower() != "identity"):
                raise ArtifactReadError("invalid")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isascii() or not length.isdigit() or len(length) > 10 or int(length) > MAX_HANDOFF_BYTES):
                raise ArtifactReadError("invalid")
            data, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_HANDOFF_BYTES:
                    raise ArtifactReadError("invalid")
                data.append(chunk)
            value = json.loads(b"".join(data).decode("utf-8"), object_pairs_hook=_unique_object)
        return validate_review_surface(value)
    except ArtifactReadError:
        raise
    except requests.RequestException:
        raise ArtifactReadError("unavailable") from None
    except (CmsArtifactError, ValueError, TypeError, KeyError, UnicodeError):
        raise ArtifactReadError("invalid") from None


def _current(operation, *, lease_id=None):
    from workflow_runs.models import ContentFactoryRun
    payload = operation.payload
    if (not isinstance(payload, dict) or operation.action != ACTION or operation.state not in {"pending", "running"} or
            lease_id is not None and (operation.state != "running" or payload.get("lease_id") != lease_id) or
            not IDENTITY <= set(payload) or set(payload) - IDENTITY - {"failures", "lease_id", "handoff"} or
            type(payload.get("schema_version")) is not int or payload["schema_version"] != 1):
        raise _deny()
    run = ContentFactoryRun.objects.filter(run_id=payload["source_run_id"]).first()
    admitted = _source(run)
    if ({key: payload[key] for key in IDENTITY} != _identity(run, admitted, payload["expected_revision"]) or
            operation.generation != admitted.scope.binding["generation"] or
            str(operation.connection_id) != admitted.scope.binding["connection_id"]):
        raise _deny()
    return run, admitted


def advance_review(identifier, *, now=None):
    """Collect once per durable lease, fencing current source after the HTTP read."""
    if not channel_artifacts.enabled():
        return None
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    now = now or timezone.now()
    candidate = WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION).first()
    if candidate is None or candidate.state not in {"pending", "running"}:
        return candidate
    lease = str(uuid4())
    claimed = False
    try:
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
            operation = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=ACTION).first()
            if (operation is None or operation.state not in {"pending", "running"} or
                    operation.next_attempt_at and operation.next_attempt_at > now):
                return operation
            run, admitted = _current(operation)
            operation.payload = {**operation.payload, "lease_id": lease}
            operation.state, operation.next_attempt_at = "running", now + timedelta(seconds=LEASE_SECONDS)
            operation.attempts += 1
            operation.save(update_fields=["payload", "state", "next_attempt_at", "attempts", "updated_at"])
            claimed = True
        # Private authority is refreshed immediately before HTTP, outside locks.
        operation.refresh_from_db()
        run, admitted = _current(operation, lease_id=lease)
        failure, value = None, None
        try:
            value = fetch_handoff(run, expected_revision=operation.payload["expected_revision"])
        except ArtifactReadError as exc:
            failure = exc.category
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=admitted.scope.connection.organization_id)
            saved = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=ACTION).first()
            if saved is None or saved.state != "running" or saved.payload.get("lease_id") != lease:
                return saved
            run, admitted = _current(saved)
            saved.payload = deepcopy(saved.payload)
            if failure is None:
                value = validate_review_surface(value)
                if (value["run_id"] != run.run_id or value["run_generation"] != run.result["generation"] or
                        value["envelope"] != admitted.envelope or
                        saved.payload["expected_revision"] is not None and value["review_revision"] != saved.payload["expected_revision"]):
                    raise _deny()
                saved.payload["handoff"] = value
                saved.state, saved.next_attempt_at = "completed", None
                saved.receipt = {"status": "artifact_received", "channel": "cms", "sourceRunId": run.run_id,
                    "artifactDigest": value["digest"], "reviewRevision": value["review_revision"],
                    "provider_modified": False, "publishing_verified": False, "approval_recorded": False}
            else:
                from .website_failure_contract import classify_failure
                failures = saved.payload.get("failures", 0) + 1
                saved.payload["failures"] = failures
                transient = failure == "unavailable" and failures < 5
                saved.state, saved.next_attempt_at = ("pending", now + timedelta(seconds=15 * 2 ** (failures - 1))) if transient else ("blocked", None)
                classified = classify_failure("CONNECTION_SERVICE_TRANSIENT" if failure == "unavailable" else "PORTABLE_PACKAGE_DRIFT", "cms_artifact_review")
                saved.receipt = {"status": "artifact_pending" if transient else "artifact_blocked", "failure": classified,
                    "failure_class": classified["failure_class"], "provider_modified": False, "approval_recorded": False}
            saved.payload.pop("lease_id", None)
            saved.save(update_fields=["payload", "receipt", "state", "next_attempt_at", "updated_at"])
            return saved
    except (WebsiteAuthorityError, CmsArtifactError, ValueError, TypeError, KeyError):
        # CAS only this lease. Never overwrite a disconnect or newer worker.
        owned = {"state": "running", "payload__lease_id": lease} if claimed else {
            "state": candidate.state, "updated_at": candidate.updated_at}
        WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION, **owned).update(
            state="cancelled", next_attempt_at=None, payload={key: candidate.payload.get(key) for key in IDENTITY},
            receipt={"status": "artifact_source_changed", "provider_modified": False,
                "approval_recorded": False}, updated_at=timezone.now())
        return WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION).first()


def ensure_reviews(*, limit=20, connection_id=None):
    """Wake artifact collection for completed original CMS runs, without billing."""
    if not channel_artifacts.enabled():
        return 0
    from workflow_runs.models import ContentFactoryRun
    from .website_models import WebsiteConnectionOperation
    runs = ContentFactoryRun.objects.filter(status__in=["completed", "awaiting_approval", "approval_required"],
        run_request__publishing_channel_artifact__channel="cms",
        organization__content_config__website_connection__state="connected",
        organization__content_config__website_connection__authorized_by__is_active=True,
        run_request__publishing_channel_artifact__connection_generation=F("organization__content_config__website_connection__generation"))
    if connection_id:
        runs = runs.filter(run_request__publishing_channel_artifact__connection_id=str(connection_id))
    existing = WebsiteConnectionOperation.objects.filter(action=ACTION, payload__source_run_id=OuterRef("run_id"),
        payload__source_generation=OuterRef("result__generation"))
    runs = runs.annotate(_cms_review_exists=Exists(existing)).filter(_cms_review_exists=False)
    count = 0
    for run in runs.order_by("-updated_at")[:limit]:
        try:
            start_review(run)
            count += 1
        except WebsiteAuthorityError:
            continue
    return count
