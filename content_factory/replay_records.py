"""Replay records of founder failures, and their redacted export (plan F5).

A replay record (``replay-record-v1``) is the recorded input of one failed run,
fed in CI to the function that failed, with the outcome it must reach today:
``pass``, ``ask:<action>`` or ``code:<CODE>``. Content Factory keeps its
records and runner in ``backend/evals/replay``; this module keeps the backend
records (``content_factory/fixtures/replay``), the same format checks, and
``export_record``, which builds a record from a saved run.

The export is an allowlist. It copies only named fields, and only values that
are codes, booleans, small integers or public paths. Free text (error
messages, prompts, article bodies), tokens, e-mail addresses, names, the
founder's domain, the repository name and any repository content are never
copied, because they are never selected. A final scan fails closed if a
forbidden shape still appears. An exported record is a draft: a reviewer
checks the redaction, sets ``redaction_reviewed_by``, chooses the handler and
the expected outcome, and only then is it committed (owner decision D11).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from django.utils import timezone

from .website_failure_redaction import redact_secret_text

SCHEMA = "replay-record-v1"
RECORDS_DIR = Path(__file__).resolve().parent / "fixtures" / "replay"
DRAFT_HANDLER = "unassigned"
EXPORT_SITE = "site.example"

TOP_LEVEL = {"schema", "id", "source", "workflow", "step", "code", "framework_fingerprint", "http", "inputs",
             "replay", "expected", "notes"}
REQUIRED = TOP_LEVEL - {"notes", "framework_fingerprint"}
SOURCE_FIELDS = {"run_pk", "org_id", "catalogue", "synthetic", "exported_at", "redaction_reviewed_by"}
HTTP_FIELDS = {"method", "url", "status", "headers", "body"}
HTTP_HEADERS = {"location", "content-type"}
FINGERPRINT_FIELDS = {"framework", "adapter", "router", "host", "language", "listing", "package_manager"}

ID = re.compile(r"[a-z0-9][a-z0-9-]{2,79}")
CODE = re.compile(r"[A-Za-z][A-Za-z0-9_:.\-]{0,127}")
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_:.\-]{0,79}")
OUTCOME = re.compile(r"pass|ask:[a-z][a-z0-9_]{0,63}|code:[A-Za-z][A-Za-z0-9_]{0,127}")
PUBLIC_PATH = re.compile(r"/[A-Za-z0-9_.\-/]{0,200}")
RESERVED_HOST = re.compile(r"(?:[a-z0-9-]+\.)*(?:example|test|invalid|localhost)|(?:[a-z0-9-]+\.)*example\.(?:com|org|net)")

FORBIDDEN = {
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "provider_key": re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{16,}|xox[abprs]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16})"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    "bearer": re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9+/=_\-.]{12,}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "url_credentials": re.compile(r"[a-z][a-z0-9+.\-]*://[^/\s\"@]+:[^/\s\"@]+@"),
    "email": re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}"),
}
SECRET_FIELD = re.compile(r"(?i)(?:^|_)(?:token|secret|password|passwd|authorization|cookie|api_key|apikey|private_key)$")


class RecordError(ValueError):
    """A record breaks the format or carries a forbidden value."""


# --- checks -------------------------------------------------------------------------------------


def _check_keys(where, value, allowed):
    if not isinstance(value, dict):
        raise RecordError(f"{where} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise RecordError(f"{where} has unknown fields: {sorted(unknown)}")


def _walk(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{path}.{key}" if path else str(key)
            yield here, key, item
            yield from _walk(item, here)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield f"{path}[{index}]", None, item
            yield from _walk(item, f"{path}[{index}]")


def forbidden_values(value: Any) -> list[str]:
    """Names of the forbidden shapes found anywhere in ``value``."""
    text = json.dumps(value, sort_keys=True, default=str)
    found = [name for name, pattern in FORBIDDEN.items() if pattern.search(text)]
    for path, key, item in _walk(value):
        if isinstance(key, str) and SECRET_FIELD.search(key) and item not in (None, "", False):
            found.append(f"secret_field:{path}")
    return found


def validate(record: Any, *, draft: bool = False) -> dict:
    """Return ``record`` if it is a well-formed, redacted record.

    ``draft=True`` accepts a fresh export: no reviewer, no handler and no
    expected outcome yet. A committed record is checked with ``draft=False``.
    """
    _check_keys("record", record, TOP_LEVEL)
    missing = REQUIRED - set(record)
    if missing:
        raise RecordError(f"record is missing {sorted(missing)}")
    if record["schema"] != SCHEMA:
        raise RecordError(f"schema must be {SCHEMA}")
    if not isinstance(record["id"], str) or not ID.fullmatch(record["id"]):
        raise RecordError("id must be a lower-case slug")
    source = record["source"]
    _check_keys("source", source, SOURCE_FIELDS)
    for key in ("run_pk", "org_id"):
        if source.get(key) is not None and type(source.get(key)) is not int:
            raise RecordError(f"source.{key} must be an integer or null")
    if not isinstance(source.get("catalogue"), str) or not source["catalogue"].strip():
        raise RecordError("source.catalogue names where the case is described")
    if not draft and source.get("synthetic") is not True and not source.get("redaction_reviewed_by"):
        raise RecordError("an exported record needs redaction_reviewed_by before it is committed (D11)")
    for key in ("workflow", "step", "code"):
        if not isinstance(record[key], str) or not CODE.fullmatch(record[key]):
            raise RecordError(f"{key} must be a code")
    fingerprint = record.get("framework_fingerprint")
    if fingerprint is not None:
        _check_keys("framework_fingerprint", fingerprint, FINGERPRINT_FIELDS)
        if any(not isinstance(item, str) or not CODE.fullmatch(item) for item in fingerprint.values()):
            raise RecordError("framework_fingerprint values must be codes")
    if not isinstance(record["http"], list):
        raise RecordError("http must be a list")
    for index, response in enumerate(record["http"]):
        where = f"http[{index}]"
        _check_keys(where, response, HTTP_FIELDS)
        url = response.get("url")
        if not isinstance(url, str) or type(response.get("status")) is not int:
            raise RecordError(f"{where} needs a url and an integer status")
        host = urlsplit(url).hostname
        if urlsplit(url).scheme not in {"https", "http"} or not host or not RESERVED_HOST.fullmatch(host):
            raise RecordError(f"{where} must use a reserved example host, not {host!r}")
        _check_keys(f"{where}.headers", response.get("headers") or {}, HTTP_HEADERS)
    if not isinstance(record["inputs"], dict):
        raise RecordError("inputs must be an object")
    replay = record["replay"]
    _check_keys("replay", replay, {"repo", "handler"})
    if replay.get("repo") not in {"cf", "be"} or not isinstance(replay.get("handler"), str) or not replay["handler"]:
        raise RecordError("replay names a repo (cf or be) and a handler")
    if draft and record["expected"] is None:
        pass
    elif not isinstance(record["expected"], str) or not OUTCOME.fullmatch(record["expected"]):
        raise RecordError("expected must be pass, ask:<action> or code:<CODE>")
    if not draft and replay["handler"] == DRAFT_HANDLER:
        raise RecordError("a committed record names the handler it is replayed through")
    if "notes" in record and (not isinstance(record["notes"], str) or len(record["notes"]) > 1200):
        raise RecordError("notes must be short text")
    found = forbidden_values(record)
    if found:
        raise RecordError(f"record carries forbidden values: {found}")
    return record


def load_records(directory: Path = RECORDS_DIR, *, repo: str = "be") -> list[dict]:
    """Every committed record in ``directory`` for ``repo``, validated, sorted by id."""
    records = []
    for path in sorted(directory.glob("*.json")):
        record = validate(json.loads(path.read_text(encoding="utf-8")))
        if record["id"] != path.stem:
            raise RecordError(f"{path.name}: id {record['id']!r} must match the file name")
        if record["replay"]["repo"] == repo:
            records.append(record)
    return records


# --- export -------------------------------------------------------------------------------------


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _token(value):
    """A code-like token (``style:2_mismatches``, ``plan_article``), never prose or a credential."""
    if not isinstance(value, str) or not TOKEN.fullmatch(value) or redact_secret_text(value) != value:
        return None
    return None if forbidden_values(value) else value


def _flag(value):
    return value if isinstance(value, bool) else None


def _small_int(value):
    return value if type(value) is int and 0 <= value <= 10**9 else None


def _pick(source, spec):
    """``spec`` lists (output field, input key, converter); unset and invalid values are dropped."""
    picked = {}
    for name, key, convert in spec:
        value = convert(_mapping(source).get(key))
        if value is not None:
            picked[name] = value
    return picked


def _codes(values, limit=32):
    return [code for code in (_token(item) for item in (values if isinstance(values, list) else [])[:limit]) if code]


def _public_path(url, domain):
    """The path of a URL on the founder's own site, rewritten to the export host; else None."""
    if not isinstance(url, str):
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    own = str(domain or "").strip().lower().removeprefix("www.")
    if parts.scheme not in {"http", "https"} or not own or host.removeprefix("www.") != own or parts.username:
        return None
    path = parts.path or "/"
    return f"https://{EXPORT_SITE}{path}" if PUBLIC_PATH.fullmatch(path) else None


FAILURE_FIELDS = (
    ("code", "code", _token),
    ("failure_class", "failure_class", _token),
    ("retryable", "retryable", _flag),
    ("requires_user_action", "requires_user_action", _flag),
    ("stage", "stage", _token),
    ("callback_denial_code", "callback_denial_code", _token),
    ("generation", "generation", _small_int),
)
REFUSAL_FIELDS = (
    ("code", "code", _token),
    ("raise_site", "raise_site", _token),
)
CANCEL_FIELDS = (
    ("cancellation_reason", "cancellation_reason", _token),
    ("cancelled_by", "cancelled_by", _token),
    ("cancellation_transition", "cancellation_transition", _token),
    ("cancellation_generation", "cancellation_generation", _small_int),
)
QUALITY_FIELDS = (
    ("status", "status", _token),
    ("self_heal_exhausted", "self_heal_exhausted", _flag),
    ("self_heal_attempt", "self_heal_attempt", _small_int),
    ("escalation", "escalation", _token),
    ("hold_owner", "hold_owner", _token),
    ("blocker_code", "blocker_code", _token),
)
REQUEST_FIELDS = (
    ("request_source", "request_source", _token),
    ("delivery_mode", "delivery_mode", _token),
    ("workflow_mode", "workflow_mode", _token),
    ("connection_generation", "connection_generation", _small_int),
    ("roo_points_billing_status", "roo_points_billing_status", _token),
    ("roo_points_cost", "roo_points_cost", _small_int),
    ("automation_trigger", "automation_trigger", _token),
)
FINGERPRINT_SOURCES = {"framework": ("framework", "detected_framework"), "adapter": ("adapter", "article_adapter"),
                       "router": ("router",), "host": ("hosting_provider", "host"), "language": ("language",),
                       "package_manager": ("package_manager",)}


def run_failure_code(run) -> str:
    """The stored failure code of a run, as staff alerts read it."""
    result = _mapping(run.result)
    return (_token(result.get("error_code")) or _token(_mapping(result.get("failure")).get("code"))
            or _token(_mapping(result.get("article_preview_quality")).get("blocker_code")) or "none")


def _failed_step(run, steps):
    for step in steps:
        if step.status in {"failed", "blocked"} and _token(step.step_key):
            return step.step_key
    return _token(run.current_step) or "unknown"


def _fingerprint(run):
    request, result = _mapping(run.run_request), _mapping(run.result)
    picked = {}
    for name, keys in FINGERPRINT_SOURCES.items():
        for key in keys:
            value = _token(request.get(key)) or _token(result.get(key))
            if value:
                picked[name] = value
                break
    return picked or None


def export_record(run, *, steps=(), now=None) -> dict:
    """A draft replay record for ``run``, built only from allowlisted fields."""
    result, request = _mapping(run.result), _mapping(run.run_request)
    failure = _mapping(result.get("failure"))
    details = _mapping(failure.get("details"))
    http = []
    url = _public_path(details.get("url") or details.get("failing_url"), run.domain)
    status = _small_int(details.get("http_status") or details.get("status"))
    if url and status is not None and 100 <= status <= 599:
        http.append({"method": "GET", "url": url, "status": status, "headers": {}, "body": ""})
    rejection = _pick(result.get("callback_rejection"), REFUSAL_FIELDS)
    inputs = {
        "status": _token(run.status), "current_step": _token(run.current_step),
        "approval_state": _token(run.approval_state),
        "steps": [{"step_key": key, "status": _token(step.status), "attempts": _small_int(step.attempts)}
                  for step in list(steps)[:64] if (key := _token(step.step_key))],
        "failure": _pick(failure, FAILURE_FIELDS),
        "callback_rejection": rejection,
        "authority_refusals": [item for item in (_pick(entry, REFUSAL_FIELDS)
                               for entry in (result.get("authority_refusals") or [])[:8] if isinstance(entry, dict)) if item],
        "cancellation": _pick(result, CANCEL_FIELDS),
        "article_preview_quality": {**_pick(result.get("article_preview_quality"), QUALITY_FIELDS),
                                    "findings": _codes(_mapping(result.get("article_preview_quality")).get("findings"))},
        "request": {**_pick(request, REQUEST_FIELDS),
                    "has_website_connection": bool(request.get("website_connection_id")),
                    "has_editorial_brief": bool(request.get("editorial_brief"))},
        "error_present": bool(str(run.error or "").strip()),
    }
    record = {
        "schema": SCHEMA, "id": f"export-run-{int(run.pk)}",
        "source": {"run_pk": int(run.pk), "org_id": _small_int(run.organization_id),
                   "catalogue": f"exported from run pk {int(run.pk)}", "synthetic": False,
                   "exported_at": (now or timezone.now()).isoformat()},
        "workflow": _token(run.workflow) or "unknown", "step": _failed_step(run, steps),
        "code": run_failure_code(run), "framework_fingerprint": _fingerprint(run), "http": http,
        "inputs": {key: value for key, value in inputs.items() if value not in (None, {}, [])},
        "replay": {"repo": "be", "handler": DRAFT_HANDLER}, "expected": None,
        "notes": "Draft export. Review the redaction, set redaction_reviewed_by, choose the repo, handler "
                 "and expected outcome before committing.",
    }
    found = forbidden_values(record) + _identifying_values(record, run)
    if found:  # Unreachable through the allowlist; fail closed rather than write it.
        raise RecordError(f"export refused: forbidden values {found}")
    return validate(record, draft=True)


def _identifying_values(record, run) -> list[str]:
    """The founder's domain and repository must not survive, even inside a code-shaped value."""
    text = json.dumps(record, sort_keys=True).lower()
    domain = str(run.domain or "").strip().lower().removeprefix("www.")
    repo = str(run.github_repo or "").strip().lower()
    names = [domain, repo, *repo.split("/"), domain.split(".")[0] if domain.count(".") else ""]
    return [f"identifying:{kind}" for kind, name in zip(("domain", "repository", "owner", "name", "site"), names)
            if len(name) >= 4 and re.search(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])", text)]
