"""Identity-bound, non-terminal article-start observations; no billing decisions."""
import hashlib
import json
import re
from datetime import datetime

from .editorial_contract import ArticleEditorialAdmission, ArticleEditorialBrief
from .run_state import ARTICLE_WORKFLOWS


NOTICE_CODES = frozenset({
    "editorial_catalog_identity_missing", "editorial_catalog_unavailable",
    "editorial_catalog_identity_mismatch", "editorial_catalog_contract_unavailable",
    "editorial_brief_required", "editorial_catalog_selection_not_current",
    "editorial_admission_missing", "editorial_admission_conflict",
    "saved_editorial_policy_invalid", "editorial_catalog_policy_changed",
    "article_task_identity_invalid", "article_task_source_conflict",
    "article_task_history_conflict", "article_task_request_conflict",
    "article_task_dispatch_uncertain", "article_task_state_changed",
    "article_task_policy_unrecognized",
    "article_task_request_invalid",
})


def notice_for_run(existing, data, emitted_at):
    """Validate against existing history, never create or repair that history.

Return only allowlisted observation fields. Error prose, refund/retry flags and
replacement run requests from the callback are deliberately not copied.
"""
    notice = data.get("admission_notice")
    if (not isinstance(notice, dict) or notice.get("schema_version") != "2026-09-11.1"
            or not isinstance(notice.get("error_code"), str) or notice["error_code"] not in NOTICE_CODES
            or not isinstance(notice.get("task_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", notice["task_id"])
            or emitted_at is None or emitted_at.utcoffset() is None):
        raise ValueError("Invalid article admission observation")
    if (not existing.get("run_id") or data.get("run_id") != existing["run_id"]
            or data.get("job_id") != existing["run_id"]
            or data.get("workflow") not in {"direct_generate", "confirmed_topic"}
            or existing.get("workflow") not in ARTICLE_WORKFLOWS):
        raise ValueError("Article admission observation identity conflicts with its run")
    try:
        domain = ArticleEditorialAdmission.normalized_domain(existing.get("domain"))
        if domain != ArticleEditorialAdmission.normalized_domain(data.get("domain")):
            raise ValueError("Domain conflict")
        request = existing.get("run_request") or {}
        repo = str(existing.get("github_repo") or request.get("github_repo") or "").strip().casefold()
        if repo and repo != str(data.get("github_repo") or "").strip().casefold():
            raise ValueError("Repository conflict")
        if request.get("domain") and domain != ArticleEditorialAdmission.normalized_domain(request["domain"]):
            raise ValueError("Stored domain conflict")
        if request.get("github_repo") and repo != str(request["github_repo"]).strip().casefold():
            raise ValueError("Stored repository conflict")
        if "editorial_brief" in request and "editorialBrief" in request and request["editorial_brief"] != request["editorialBrief"]:
            raise ValueError("Stored brief aliases conflict")
        brief = request.get("editorial_brief", request.get("editorialBrief"))
        expected_hash = None
        if brief is not None:
            canonical = ArticleEditorialBrief.model_validate(brief).model_dump(mode="json")
            expected_hash = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        if "brief_sha256" not in notice or notice["brief_sha256"] != expected_hash:
            raise ValueError("Original brief conflict")
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Article admission observation does not match the stored organisation, repository or reader decision") from None
    return {"schema_version": "2026-09-11.1", "error_code": notice["error_code"],
            "task_id": notice["task_id"], "brief_sha256": expected_hash, "observed_at": emitted_at.isoformat()}


def is_older_notice(previous, current):
    if not isinstance(previous, dict):
        return False
    try:
        return datetime.fromisoformat(previous["observed_at"]) >= datetime.fromisoformat(current["observed_at"])
    except (ValueError, TypeError, KeyError):
        return False
