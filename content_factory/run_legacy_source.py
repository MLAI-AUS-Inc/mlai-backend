"""Authenticated, immutable source descriptors for new children of legacy runs.

A descriptor names retained Content Factory source bytes. It never enrolls the
historical parent, reconstructs its worker request from mutable backend fields,
or grants current startup/repository rights; the caller must check those gates.
"""
from copy import deepcopy
import hashlib
import json
import re
from urllib.parse import quote

from .run_ownership import RunOwnershipConflict, owner_receipt

_FIELDS = frozenset({"schema_version", "source_run_id", "workflow", "domain", "github_repo",
    "request_sha256", "content_sha256", "descriptor_sha256"})
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_ALIASES = {
    "scan": {"repo_scan"}, "content_factory_scan": {"repo_scan"},
    "scaffold_articles": {"article_system_setup"},
    "article_generation": {"direct_generate", "confirmed_topic"},
    "confirmed_topic": {"direct_generate"},
}


def validate_legacy_source(run, value):
    """Check the exact closed CF descriptor without inventing a parent receipt."""
    if (owner_receipt(run) is not None or not isinstance(value, dict) or set(value) != _FIELDS
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or any(type(value.get(field)) is not str for field in _FIELDS - {"schema_version"})
            or value["source_run_id"] != run.run_id or value["domain"] != run.domain
            or value["github_repo"] != run.github_repo
            or value["workflow"] not in {run.workflow} | _ALIASES.get(run.workflow, set())
            or any(not _SHA.fullmatch(value[field]) for field in ("request_sha256", "content_sha256", "descriptor_sha256"))):
        raise RunOwnershipConflict("The original legacy source could not be verified.")
    digest = hashlib.sha256(json.dumps({key: item for key, item in value.items() if key != "descriptor_sha256"},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()
    if digest != value["descriptor_sha256"]:
        raise RunOwnershipConflict("The original legacy source descriptor changed.")
    return deepcopy(value)


def fetch_legacy_source(run):
    """Read the existing authenticated descriptor endpoint outside SQL locks."""
    from . import vibe_marketing_views as views
    from workflow_runs.models import ContentFactoryRun
    views.require_unlocked_remote_call()
    current = ContentFactoryRun.objects.filter(pk=run.pk).first()
    if (current is None or owner_receipt(current) is not None or current.organization_id != run.organization_id
            or current.run_id != run.run_id or current.domain != run.domain or current.github_repo != run.github_repo
            or current.workflow != run.workflow):
        raise RunOwnershipConflict("The original legacy source identity changed.")
    config = views._content_factory_remote_config()
    if not config["enabled"]:
        raise RunOwnershipConflict("The original legacy source is temporarily unavailable.")
    try:
        response = views.http_client.get(
            f"{config['base_url']}/api/runs/{quote(run.run_id, safe='')}/continuation-source",
            headers=views._content_factory_headers(), timeout=(5, 15))
        if response.status_code != 200:
            raise RunOwnershipConflict("The original legacy source could not be verified.")
        value = response.json()
    except (views.http_client.RequestException, ValueError) as exc:
        raise RunOwnershipConflict("The original legacy source could not be verified.") from exc
    return validate_legacy_source(current, value)
