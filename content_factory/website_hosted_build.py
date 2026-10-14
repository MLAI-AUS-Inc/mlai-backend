"""Exact hosted setup-build references; publication still needs live deployment proof."""
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
    saved=goals.mapping(run.run_request)
    expected_branch=saved.get("branch") or getattr(website,"branch",None)
    if (set(receipt)!=fields or type(receipt.get("version")) is not int or receipt["version"]!=1
        or receipt.get("runId")!=run.run_id or receipt.get("commitSha")!=head
        or receipt.get("branch")!=expected_branch or preview.get("branchName")!=expected_branch
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
