"""Certify a first static article from its merged Git tree and exact public route bytes."""
import base64
import hashlib
from urllib.parse import quote
from uuid import uuid4

from django.db import transaction

from integrations.services.github_connections import read_bound_repository
from . import reviewed_article_pr as pr
from .models import OrganizationContentConfig, WrittenArticle, ArticlePublishStatus
from .website_contract import WebsiteAuthorityError
from .website_live_fetch import fetch_live_route
from .website_live_verification import marker_present
from .website_models import WebsiteConnection, WebsiteConnectionOperation
from .website_reconciliation import _setup_pull_merged_as, commit_tree_sha
from .website_verification import static_listing_links_to

ORIGIN = "reviewed_static_article_http"


def _provider(site, relative):
    response = read_bound_repository(site, f"https://api.github.com/repos/{site.github_repo}/{relative}")
    response.raise_for_status()
    return response.json()


def _repo_bytes(site, sha, path):
    body = _provider(site, f"contents/{quote(path, safe='/')}?ref={sha}")
    if (body.get("type") != "file" or body.get("path") != path or body.get("encoding") != "base64"
            or type(body.get("size")) is not int or not 0 < body["size"] <= 1_000_000
            or not isinstance(body.get("content"), str) or len(body["content"]) > 1_450_000):
        raise pr.Unavailable("repository_file_invalid")
    raw = base64.b64decode("".join(body["content"].split()), validate=True)
    git_blob = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
    if len(raw) != body["size"] or body.get("sha") != git_blob:
        raise pr.Unavailable("repository_file_invalid")
    return raw


def _live(op, site, merged, target):
    receipt = op.receipt
    directory, route = receipt["directory_path"], receipt["route_path"]
    root = target.contract["registration_strategy"]["recipe"]["static_root"]
    domain = site.organization.domain
    hashes = {}
    for kind, path in (("directory", directory), ("detail", route)):
        repo_path = "/".join(x for x in (root, path.strip("/"), "index.html") if x)
        expected = _repo_bytes(site, merged, repo_path)
        digest = pr.sha256(expected)
        if kind == "detail" and digest != receipt["rendered_article_sha256"]:
            raise pr.Unavailable("reviewed_content_changed")
        url = f"https://{domain}{path}"
        observed, headers = fetch_live_route(url, domain)
        if (expected != observed or not marker_present(receipt["artifact_digest"], observed, headers)
                or kind == "directory" and not static_listing_links_to(url, observed, route)):
            raise pr.Unavailable("deployment_pending", 503)
        hashes[kind] = digest
    missing = f"{directory}mlai-missing-{uuid4().hex}/"
    fetch_live_route(f"https://{domain}{missing}", domain, expected_status=404)
    return {**hashes, "missing_route": missing}


def observe(op, now):
    """No publication admission exists until the owner merges and the real site serves both pages."""
    ctx, _, cfg = pr.authorized_context(op, require_pending=False)
    site = op.connection
    receipt = op.receipt
    pull = _provider(site, f"pulls/{receipt['pr_number']}")
    head = pull.get("head") or {}
    if head.get("ref") != receipt["branch_name"] or head.get("sha") != receipt["head_sha"]:
        raise pr.Unavailable("reviewed_branch_changed")
    if not pull.get("merged"):
        if pull.get("state") != "open":
            raise pr.Unavailable("pull_request_closed")
        return receipt, "pending"
    merged = pull.get("merge_commit_sha")
    if (not pr.SHA.fullmatch(str(merged or "")) or not _setup_pull_merged_as(site, pull, number=receipt["pr_number"],
            head_sha=receipt["head_sha"], source_sha=merged)):
        raise pr.Unavailable("reviewed_merge_changed")
    tree = commit_tree_sha(site, receipt["head_sha"])
    if tree != commit_tree_sha(site, merged):
        raise pr.Unavailable("reviewed_tree_changed")
    waiting = {**receipt, "status": "merged_awaiting_verification", "merge_sha": merged}
    pr.verify_repository_native_target(site)
    if pr.read_head(site) != merged:
        return waiting, "pending"
    target = site.targets.get(generation=op.generation, target_key=receipt["target_id"])
    from .website_sealed_workflow import required
    if required(site, target):
        # A configured stronger execution proof stays mandatory.
        return waiting, "pending"
    try:
        hashes = _live(op, site, merged, target)
    except WebsiteAuthorityError:
        return waiting, "pending"
    except pr.Unavailable as exc:
        if exc.status >= 500:
            return waiting, "pending"
        raise
    if pr.read_head(site) != merged:
        return waiting, "pending"
    with transaction.atomic():
        site = WebsiteConnection.objects.select_for_update().get(pk=site.pk)
        op = WebsiteConnectionOperation.objects.select_for_update().get(pk=op.pk)
        if op.state != "pending" or op.receipt != receipt:
            raise pr.Unavailable()
        ctx, source, cfg = pr.authorized_context(op, require_pending=False)
        cfg = OrganizationContentConfig.objects.select_for_update().get(pk=cfg.pk)
        current = site.targets.select_for_update().get(generation=op.generation, target_key=receipt["target_id"])
        if cfg.default_publish_target_id != current.target_key or current.contract != target.contract:
            raise pr.Unavailable()
        wire = op.payload["wire"]
        evidence = {"method": ORIGIN, "attestation_origin": ORIGIN, "provider_verified": False,
                    "static_directory_link_verified": True, "source_sha": merged, "target_id": current.target_key,
                    "artifact_digest": receipt["artifact_digest"], "contract_digest": receipt["contract_digest"],
                    "connection_generation": site.generation, "status": "passed", "checked_at": now.isoformat(),
                    "public_url": f"https://{site.organization.domain}{receipt['route_path']}", "operation_id": str(op.pk),
                    "reviewed_head_sha": receipt["head_sha"], "tree_sha": tree, "route_hashes": hashes,
                    "reviewed_revision": wire["reviewed_revision"], "article_sha256": wire["article_sha256"]}
        cert = {k: wire[k] for k in ("website_connection_id", "connection_generation", "repository_id", "github_repo",
                                     "operation_id", "operation_attempt", "deletion_epoch")}
        cert.update(status="verified", source_sha=merged, adapter_id=current.adapter, target_id=current.target_key,
                    contract_digest=receipt["contract_digest"], preview_capable=True, confidence="high",
                    evidence_digest=pr.sha256(pr.canonical(evidence).encode()), attestation_origin=ORIGIN)
        targets = [item for item in cfg.publish_targets or [] if isinstance(item, dict) and item.get("target_id") == current.target_key]
        if len(targets) != 1 or targets[0] != current.contract:
            raise pr.Unavailable("target_changed")
        new_contract = {**current.contract, "verification": cert}
        new_contract.pop("provisional", None)
        current.source_sha, current.verified_at, current.contract = merged, now, new_contract
        current.capabilities = {**current.capabilities, "generationReady": True, "publishingReady": True, "previewSupported": True}
        current.save(update_fields=["contract", "capabilities", "source_sha", "verified_at", "updated_at"])
        cfg.publish_targets = [new_contract if i == targets[0] else i for i in cfg.publish_targets]
        cfg.save(update_fields=["publish_targets", "updated_at"])
        WebsiteConnectionOperation.objects.get_or_create(idempotency_key=f"reviewed-static-deployed:{op.pk}:{merged}",
            defaults={"action": "deployment-verify", "connection": site, "generation": site.generation, "state": "completed",
                      "payload": {"source_sha": merged, "target_id": current.target_key}, "receipt": evidence})
        site.verified_sha, site.last_verified_at = merged, now
        site.capabilities = {**site.capabilities, "generationReady": True, "publishingReady": True, "previewSupported": True}
        site.blockers = [item for item in site.blockers if not isinstance(item, dict) or item.get("code") != "repository_source_changed"]
        site.save(update_fields=["capabilities", "verified_sha", "last_verified_at", "blockers", "updated_at"])
        article, _ = WrittenArticle.objects.select_for_update().get_or_create(organization_id=site.organization_id,
            slug=op.payload["slug"], defaults={"title": op.payload["title"], "primary_keyword": "", "category": "articles",
                                               "source_run_id": source.run_id, "run": source})
        if article.source_run_id != source.run_id:
            raise pr.Unavailable("article_identity_changed")
        article.publish_status, article.pr_number, article.pr_url = ArticlePublishStatus.LIVE, receipt["pr_number"], receipt["pr_url"]
        article.live_url = article.article_url = evidence["public_url"]
        article.canonical_path = receipt["route_path"]
        article.content_path = "/".join(x for x in (target.contract["registration_strategy"]["recipe"]["static_root"],
                                                    receipt["route_path"].strip("/"), "index.html") if x)
        article.merge_commit_sha = article.on_main_commit_sha = merged
        article.on_main_verified_at = article.live_checked_at = article.live_verified_at = now
        article.published_at = article.published_at or now
        article.save()
        from .first_article_entitlement import settle_entitlement_for_run
        settle_entitlement_for_run(source)
        finished = {**waiting, "status": "verified_live", "live_url": evidence["public_url"]}
        op.state, op.receipt, op.next_attempt_at = "completed", finished, None
        op.save(update_fields=["state", "receipt", "next_attempt_at", "updated_at"])
        return finished, "completed"
