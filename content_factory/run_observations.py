"""Progress observations from proven dispatches carry no repository authority."""
from copy import deepcopy
import re
from types import SimpleNamespace

from .portable_drafts import REPOSITORY_CONFIG_FIELDS
from .website_contract import CONNECTION_FIELDS, SECRET_KEYS, WebsiteAuthorityError, connection_contract

AUTHORITY_FIELDS = REPOSITORY_CONFIG_FIELDS | set(CONNECTION_FIELDS) | SECRET_KEYS | {
    "configuration_revision", "configuration_version", "github_installation_id", "installation_id",
    "capabilities", "article_system_setup", "website_connection", "connection_id", "connection_target_id",
    "expected_source_sha", "source_sha", "repo_head_sha", "commit_sha", "head_sha", "verified_sha",
    "pr_url", "pr_number", "pull_request_url", "publish_url", "published_url", "merge_status", "merge_sha",
    "publish_status", "publish_stage", "approval_state", "publishing_ready", "generation_ready",
    "verification_summary", "approval_recorded", "configuration_revision",
    "build_verified", "browser_verified", "route_is_live", "preview_content_verified", "publish_child_run_id",
    "live_preview", "live_preview_url", "preview_url", "preview_commit_sha", "branch", "branch_name",
    "operation_id", "operation_attempt", "deletion_epoch", "connection_contract",
    "approval_receipt", "publication_receipt", "publish_approval", "publish_approval_receipt",
    "publication_approved", "publish_approved", "approved_by", "approved_at",
    "client_request_id",
    "github_repo",
}


def _normal(key):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", str(key)).lower()


def _observations(value):
    if isinstance(value, dict):
        return {key: _observations(item) for key, item in value.items()
                if _normal(key) not in AUTHORITY_FIELDS
                and not (_normal(key) in {"status", "state"} and item in ("published", "merged", "approved", "auto_approved", "pr_created", "draft_pr_created", "setup_pr_created"))}
    if isinstance(value, list):
        return [_observations(item) for item in value]
    return deepcopy(value)


def observation_payload(payload, run):
    """Retain the original identity while discarding claimed promotions and Git effects."""
    safe = _observations(payload)
    incoming_request = safe.get("run_request", {})
    raw_request = payload.get("run_request")
    if isinstance(raw_request, dict) and "editorial_admission" in raw_request:
        # This is immutable reader-selection history, not website approval.
        # The normal snapshot merge validates it against the saved receipt;
        # stripping its approved catalogue records would corrupt that proof.
        incoming_request["editorial_admission"] = deepcopy(raw_request["editorial_admission"])
    safe["run_request"] = {**(run.run_request or {}), **incoming_request}
    safe["domain"] = run.domain
    safe["github_repo"] = run.github_repo
    if getattr(run, "approval_state", None):
        safe["approval_state"] = run.approval_state
    if getattr(run, "verification_summary", None):
        safe["verification_summary"] = deepcopy(run.verification_summary)
    # Previously authorised state remains historical; this update cannot grant it.
    safe["result"] = {**(run.result or {}), **safe.get("result", {})}
    return safe


def valid_existing_provenance(run, payload, website=None):
    """An existing run is proof only within its original organisation and domain."""
    if (not run or not getattr(run, "organization_id", None)
            or getattr(run, "status", None) in {"denied", "cancelled"}):
        return False
    domain = str(payload.get("domain") or (payload.get("run_request") or {}).get("domain") or "").casefold().strip()
    if not domain or domain != str(run.domain).casefold().strip():
        return False
    request = payload.get("run_request") or {}
    if request.get("domain") and str(request["domain"]).casefold().strip() != domain:
        return False
    return website is None or website.organization_id == run.organization_id


def valid_dispatch_provenance(operation, payload):
    """A new worker ID must refer to a dispatch reserved by this backend."""
    if operation is None or operation.state not in {"pending", "running"}:
        return False
    request = payload.get("run_request") or {}
    key = request.get("client_request_id")
    domain = str(payload.get("domain") or request.get("domain") or "").casefold().strip()
    expected = str(operation.connection.organization.domain).casefold().strip()
    return bool(key and key == operation.payload.get("client_request_id") and domain == expected)


def proven_run(run_id, payload):
    """Load provenance without using a worker assertion to select a company."""
    from workflow_runs.models import ContentFactoryRun
    from .website_models import WebsiteConnection, WebsiteConnectionOperation
    original = ContentFactoryRun.objects.filter(run_id=run_id).first()
    try:
        from .website_connections import _payload_with_context
        contract = connection_contract(_payload_with_context(payload))
    except WebsiteAuthorityError:
        return None
    website = WebsiteConnection.objects.filter(pk=contract["website_connection_id"]).first() if contract else None
    if contract and website is None:
        return None
    if original:
        return original if valid_existing_provenance(original, payload, website) else None
    key = (payload.get("run_request") or {}).get("client_request_id")
    if not key:
        return None
    candidates = WebsiteConnectionOperation.objects.select_related("connection__organization").filter(
        action="workflow", payload__client_request_id=key)
    for operation in candidates:
        if (valid_dispatch_provenance(operation, payload)
                and operation.payload.get("run_id") in (None, "", key, run_id)
                and (website is None or website.organization_id == operation.connection.organization_id)):
            saved = {"client_request_id": key, "operation_id": str(operation.pk),
                     "github_repo": operation.connection.github_repo,
                     "operation_attempt": operation.payload.get("attempt", 1),
                     "website_connection_id": str(operation.connection_id), "connection_generation": operation.generation,
                     "repository_id": operation.payload.get("repository_id") or operation.connection.repository_id,
                     "github_installation_id": operation.payload.get("installation_id"),
                     "deletion_epoch": operation.payload.get("deletion_epoch", 0)}
            return SimpleNamespace(domain=operation.connection.organization.domain, github_repo=operation.connection.github_repo,
                                   workflow=operation.payload.get("workflow") or payload.get("workflow"),
                                   run_request=saved, result={})
    return None
