"""Exact hosted setup-build references; publication still needs live deployment proof."""
import hashlib
import json
import re
from datetime import timedelta
from django.conf import settings
from django.utils import timezone
from . import website_goal as goals
from .website_doctor import safe_identifier


def enabled(website):
    """Use only server-enrolled websites for the hosted proof rollout."""
    return getattr(settings,"ARTICLES_HOSTED_VERIFICATION_ENABLED",False) and goals.enrolled(getattr(website,"organization_id",None))


def current_scan_source(website):
    """The current scan/observed source takes precedence over a last-successful SHA."""
    from .website_connections import observed_source_sha
    from .website_discovery import discovery_snapshot
    observed=observed_source_sha(website)
    rows=website.scan_snapshots.filter(generation=website.generation).exclude(detector_version="github_head")
    if observed:rows=rows.filter(source_sha=observed)
    scan=rows.order_by("-created_at","-pk").first()
    if not scan or not isinstance(scan.source_sha,str) or not goals.SHA.fullmatch(scan.source_sha):return None
    evidence=goals.mapping(scan.evidence)
    discovery=discovery_snapshot({**goals.mapping(evidence.get("scan_summary")),**evidence})
    return scan.source_sha if discovery.get("complete") is True and (not observed or observed==scan.source_sha) else None


def valid_hosted_build(run, preview, head, website, *, now=None):
    """Require CF's saved nonce/run/builder/commit reference, exact render and sealed digest."""
    now=now or timezone.now()
    proof=goals.mapping(goals.mapping(preview).get("proof"));receipt=goals.mapping(proof.get("hostedBuildReceipt"))
    fields={"version","runId","commitSha","branch","builderRepo","builderRunId","dispatchNonce","status","exactRender","completedAt","digest"}
    at=goals.stamp(receipt.get("completedAt"))
    expected_branch=preview_branch(run, preview, website)
    if (set(receipt)!=fields or type(receipt.get("version")) is not int or receipt["version"]!=1
        or receipt.get("runId")!=run.run_id or receipt.get("commitSha")!=head
        or not expected_branch or receipt.get("branch")!=expected_branch or preview.get("branchName")!=expected_branch
        or receipt.get("builderRepo")!=proof.get("builderRepo")
        or not isinstance(receipt.get("builderRepo"),str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",receipt["builderRepo"])
        or not all(safe_identifier(part) for part in receipt["builderRepo"].split("/"))
        or not isinstance(receipt.get("builderRunId"),str) or not re.fullmatch(r"[1-9][0-9]{0,19}",receipt["builderRunId"])
        or not isinstance(receipt.get("dispatchNonce"),str) or not re.fullmatch(r"[a-f0-9]{12,64}",receipt["dispatchNonce"])
        or receipt["dispatchNonce"]!=proof.get("builderDispatchNonce")
        or receipt.get("status")!="passed" or receipt.get("exactRender") is not True
        or preview.get("previewMode")!="platform_deployment" or preview.get("platformStatus")!="ready"
        or preview.get("exactRender") is not True or preview.get("commitSha")!=head or preview.get("renderConfidence")!="hosted"
        or not at or not now-timedelta(days=7)<=at<=now+timedelta(minutes=5)):
        return False
    try:return receipt["digest"]==goals.digest({key:value for key,value in receipt.items() if key!="digest"})
    except (ValueError,TypeError,OverflowError):return False


def preview_branch(run, preview, website):
    """Bind temporary preview refs without changing the original source branch."""
    proof = goals.mapping(goals.mapping(preview).get("proof"))
    if "branch" not in proof:
        return goals.mapping(run.run_request).get("branch") or getattr(website, "branch", None)
    branch, repository = proof.get("branch"), proof.get("sourceRepo")
    if (not isinstance(branch, str) or not 0 < len(branch) <= 255 or any(ord(char) < 32 or ord(char) == 127 for char in branch)
            or not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or not all(safe_identifier(part) for part in repository.split("/"))
            or repository.casefold() != str(getattr(website, "github_repo", "")).casefold()
            or repository.casefold() != str(getattr(run, "github_repo", "")).casefold()):
        return None
    return branch


def partial_environment(run, preview, head, website, *, now=None):
    """Recognize only a current CF-bound negative observation, never approval proof."""
    if not getattr(settings, "ARTICLES_HOSTED_ENVIRONMENT_RECOVERY_ENABLED", False) or not enabled(website):
        return False
    try:
        proof = goals.mapping(goals.mapping(preview).get("proof"))
        value = proof.get("hostedEnvironmentReceipt")
        fields = {"schema_version", "status", "run_id", "commit_sha", "branch", "dispatch_nonce", "builder_repo",
            "builder_run_id", "builder_revision", "request_identity", "broker_record", "identity_complete", "reproducible", "digest"}
        saved = goals.mapping(run.run_request)
        branch = preview_branch(run, preview, website)
        now = now or timezone.now()
        checked = goals.stamp(goals.mapping(goals.mapping(run.result).get("directory_quality_verification")).get("completed_at"))
        if (not isinstance(value, dict) or set(value) != fields or type(value["schema_version"]) is not int
                or value["schema_version"] != 1 or value["status"] != "bound_to_attempt"
                or value["identity_complete"] is not False or value["reproducible"] is not False
                or proof.get("environmentRequired") is not True or not branch or "branch" not in proof
                or not checked or not now-timedelta(days=7) <= checked <= now+timedelta(minutes=5)
                or preview.get("previewMode") != "platform_deployment" or preview.get("platformStatus") != "ready"
                or preview.get("renderConfidence") != "hosted" or preview.get("exactRender") is not True
                or preview.get("commitSha") != head or value["commit_sha"] != head or not goals.SHA.fullmatch(head)
                or value["run_id"] != run.run_id or value["branch"] != branch or preview.get("branchName") != branch
                or saved.get("website_connection_id") != str(website.pk)
                or type(saved.get("connection_generation")) is not int or saved["connection_generation"] != website.generation
                or type(saved.get("repository_id")) is not int or saved["repository_id"] != website.repository_id
                or saved.get("branch") != website.branch
                or not isinstance(value["dispatch_nonce"], str) or not re.fullmatch(r"[a-f0-9]{12,64}", value["dispatch_nonce"])
                or value["dispatch_nonce"] != proof.get("builderDispatchNonce")
                or not isinstance(value["builder_repo"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["builder_repo"])
                or not all(safe_identifier(part) for part in value["builder_repo"].split("/"))
                or value["builder_repo"] != proof.get("builderRepo")
                or not isinstance(value["builder_run_id"], str) or not re.fullmatch(r"[1-9][0-9]{0,19}", value["builder_run_id"])
                or preview.get("builderRunUrl") != f'https://github.com/{value["builder_repo"]}/actions/runs/{value["builder_run_id"]}'
                or not isinstance(value["builder_revision"], str) or not goals.SHA.fullmatch(value["builder_revision"])):
            return False
        request, record = value["request_identity"], value["broker_record"]
        if (not isinstance(request, dict) or set(request) != {"schema_version", "request_digest", "build_request_digest",
                    "fingerprint_digest", "source_sha", "source_ref", "app_root"}
                or type(request["schema_version"]) is not int or request["schema_version"] != 1
                or request != preview.get("requestIdentity")
                or request["source_sha"] != saved.get("expected_source_sha") or not goals.SHA.fullmatch(request["source_sha"])
                or request["source_ref"] != saved["branch"] or request["app_root"] != website.app_root
                or any(not isinstance(request[key], str) or not goals.DIGEST.fullmatch(request[key])
                    for key in ("request_digest", "build_request_digest", "fingerprint_digest"))
                or not isinstance(record, dict) or record.get("source_sha") != head
                or record.get("source_plan_digest") != proof.get("sourceBuildPlanDigest")
                or record.get("public_inputs_digest") != proof.get("sourceBuildPublicInputsDigest")
                or any(not isinstance(record.get(key), str) or not goals.DIGEST.fullmatch(record[key])
                    for key in ("source_plan_digest", "public_inputs_digest"))):
            return False
        # CF environment.digest deliberately uses ASCII escaping, unlike C3 v1.
        raw = json.dumps({key: child for key, child in value.items() if key != "digest"},
            sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        return len(raw) <= 96 * 1024 and value["digest"] == hashlib.sha256(raw).hexdigest()
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, UnicodeError):
        return False
