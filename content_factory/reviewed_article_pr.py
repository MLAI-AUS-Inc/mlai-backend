"""A founder's one-time authority to stage exact reviewed article bytes in a draft PR."""
import hashlib
import json
import re
from contextlib import contextmanager
from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from founder_tools.services import get_founder_company_context
from workflow_runs.models import ContentFactoryRun
from .models import OrganizationContentConfig
from .publish_reviewed_draft import may_publish
from .website_connections import contract_for, verify_repository_native_target
from .website_content_files import enabled, setup_delivered_not_live
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnection, WebsiteConnectionOperation
from .website_operations import deletion_epoch
from .website_repository_identity import has_pausing_blocker
from .website_rollout import require_repository_write_policy
from .website_source_sync import read_head

ACTION = "reviewed-article-pr"
HASH = re.compile(r"[0-9a-f]{64}")
SHA = re.compile(r"[0-9a-f]{40}")
OPEN = {"pending", "running"}


class Unavailable(ValueError):
    """A stale selection, withdrawn approval or unavailable dependency."""

    def __init__(self, reason="review_changed", status=409):
        self.status = status
        super().__init__(reason)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def _selected(organization):
    cfg = OrganizationContentConfig.objects.select_related("website_connection").filter(organization=organization).first()
    site = cfg.website_connection if cfg else None
    if (not site or site.organization_id != organization.pk or site.state != "connected" or not site.repository_id
            or not site.installation_id or not site.branch or site.app_root or str(cfg.github_repo or "").casefold() != site.github_repo.casefold()
            or has_pausing_blocker(site)):
        raise Unavailable("website_unavailable")
    require_repository_write_policy(action="setup", domain=organization.domain)
    return cfg, site


def destination(cfg, site):
    """Select only a merged static candidate or the current approved files-only setup."""
    target = site.targets.filter(generation=site.generation, target_key=cfg.default_publish_target_id).first()
    if target:
        v = target.contract.get("verification") or {}
        recipe = (target.contract.get("registration_strategy") or {}).get("recipe") or {}
        if (target.adapter == "static_dropin_v1" and target.contract.get("provisional") is True
                and recipe.get("kind") == "static_dropin_v1" and v.get("status") == "awaiting_live_proof"
                and SHA.fullmatch(target.source_sha) and v.get("source_sha") == target.source_sha
                and (v.get("static_setup_merge") or {}).get("merge_sha") == target.source_sha):
            return {"kind": "static_dropin", "target": target.target_key, "base": target.source_sha,
                    "setup": v["static_setup_merge"].get("setup_run_id"), "digest": target.contract.get("contract_digest"),
                    "route": recipe.get("route_base")}, target
        if target.verified_at or target.capabilities.get("publishingReady"):
            raise Unavailable("website_already_integrated")
    setup = ContentFactoryRun.objects.filter(organization_id=site.organization_id, workflow="article_system_setup",
            run_request__website_connection_id=str(site.pk), run_request__connection_generation=site.generation,
            run_request__repository_id=site.repository_id, github_repo__iexact=site.github_repo).order_by("-created_at", "-pk").first()
    ladder = setup_delivered_not_live(setup.result) if setup and setup.approval_state == "approved" else None
    if not ladder or setup.status in {"cancelled", "denied", "failed"}:
        raise Unavailable("website_unavailable")
    return {"kind": "content_files", "target": "", "base": "", "setup": setup.run_id, "route": ""}, None


def check_source(context, run, user, cfg):
    from . import vibe_marketing_views as views
    from .editorial_catalog import article_brief_for_catalog
    from .run_ownership import user_stop_intent
    from .publish_reviewed_draft import claim_count
    if (not enabled() or not may_publish(user, context.organization) or run.organization_id != context.organization.pk
            or run.domain.casefold() != context.organization.domain.casefold()
            or run.workflow not in views.ARTICLE_WORKFLOWS or run.status in {"denied", "cancelled", "failed"}
            or run.approval_state == "denied" or user_stop_intent(run) or not views._run_belongs_to_context(run, context)):
        raise Unavailable("article_unavailable")
    original = run.run_request or {}
    if original.get("website_connection_id") and any(original.get(k) != v for k, v in
            (("website_connection_id", str(cfg.website_connection_id)), ("connection_generation", cfg.website_connection.generation),
             ("repository_id", cfg.website_connection.repository_id))):
        raise Unavailable("website_changed")
    if claim_count(run, {}, attested_clear=True) != 0:
        raise Unavailable("article_unavailable")
    latest = views._latest_review_ready_component_revision(run, context)
    if latest and latest.run_id != run.run_id:
        raise Unavailable()
    try:
        article_brief_for_catalog(cfg.pillar_strategy, run.run_request)
    except (TypeError, ValueError):
        raise Unavailable("editorial_approval_changed") from None


def _offer(context, run, user):
    from .founder_draft_views import current_export
    cfg, site = _selected(context.organization)
    check_source(context, run, user, cfg)
    dest, target = destination(cfg, site)
    export, err = current_export(context, run, content_files=True, clear_claims=True)
    handoff = (export or {}).get("reviewedContentFilesV1")
    if (err is not None or not handoff or not HASH.fullmatch(str(handoff.get("revision") or ""))
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run.run_id)):
        raise Unavailable("article_unavailable")
    manifest = sha256(canonical(handoff).encode())
    binding = {**contract_for(site), "source_run_id": run.run_id, "reviewed_revision": handoff["revision"],
               "article_sha256": handoff["markdownSha256"], "manifest_sha256": manifest,
               "delivery_kind": dest["kind"], "connection_target_id": dest["target"], "source_branch": site.branch,
               "deletion_epoch": deletion_epoch(site)}
    offer_id = salted_hmac("founder-reviewed-article-pr", canonical([str(context.company.pk), str(user.pk), site.installation_id,
                           binding, dest]), algorithm="sha256").hexdigest()
    public = {"available": True, "deliveryKind": dest["kind"], "runId": run.run_id, "reviewedRevision": handoff["revision"],
              "articleSha256": handoff["markdownSha256"], "status": "available", "offerId": offer_id, "repository": site.github_repo}
    if dest["route"]:
        public["directoryRoute"] = dest["route"]
    return site, dest, binding, public, export


def _current_op(site, run):
    return site.operations.filter(action=ACTION, generation=site.generation,
            payload__wire__source_run_id=run.run_id).order_by("-created_at").first()


def summary(op):
    wire = op.payload["wire"]
    view = {"available": True, "operationId": str(op.pk), "deliveryKind": wire["delivery_kind"],
            "status": op.receipt.get("status", "pending"), "runId": wire["source_run_id"],
            "articleSha256": wire["article_sha256"], "reviewedRevision": wire["reviewed_revision"], "repository": wire["github_repo"]}
    if op.receipt.get("pr_url"):
        view["prUrl"] = op.receipt["pr_url"]
    return view


def offer(context, run, user):
    _, site = _selected(context.organization)
    op = _current_op(site, run)
    if op and may_publish(user, context.organization):
        return summary(op)
    return _offer(context, run, user)[3]


def reserve(context, run, user, data):
    if not isinstance(data, dict) or set(data) != {"deliveryKind", "reviewedRevision", "articleSha256", "offerId"}:
        raise Unavailable("invalid_request", 400)
    # A lost founder response must not wait on CF's article lock while the
    # first dispatch is still assembling files. This returns only an existing receipt.
    _, selected = _selected(context.organization)
    if may_publish(user, context.organization) and all(isinstance(v, str) for v in data.values()):
        saved = selected.operations.filter(action=ACTION, generation=selected.generation,
            payload__wire__source_run_id=run.run_id, payload__wire__reviewed_revision=data["reviewedRevision"],
            payload__wire__delivery_kind=data["deliveryKind"], payload__wire__article_sha256=data["articleSha256"],
            payload__offer_id=data["offerId"], payload__requested_by=user.pk).first()
        if saved:
            return saved
    site, dest, binding, public, export = _offer(context, run, user)
    if any(not isinstance(data.get(k), str) or not constant_time_compare(data[k], public[k]) for k in data):
        raise Unavailable()
    sha = read_head(site)
    if not sha:
        raise Unavailable("source_temporarily_unavailable", 503)
    if dest["base"] and sha != dest["base"]:
        raise Unavailable("source_changed")
    with transaction.atomic():
        site = WebsiteConnection.objects.select_for_update().get(pk=site.pk)
        cfg, current = _selected(context.organization)
        check_source(context, run, user, cfg)
        if (site.pk != current.pk or dest != destination(cfg, current)[0] or contract_for(current) !=
                {k: binding[k] for k in contract_for(site)} or binding["deletion_epoch"] != deletion_epoch(current)):
            raise Unavailable()
        identifier = uuid4()
        handoff = export["reviewedContentFilesV1"]
        wire = {**binding, "expected_source_sha": sha, "operation_id": str(identifier), "operation_attempt": 1,
                "dispatch_nonce": uuid4().hex}
        key = sha256(canonical([str(site.pk), site.generation, run.run_id, binding["reviewed_revision"]]).encode())
        op, created = WebsiteConnectionOperation.objects.get_or_create(idempotency_key=f"{ACTION}:{key}", defaults={
            "pk": identifier, "action": ACTION, "connection": site, "generation": site.generation, "receipt": {"status": "pending"},
            "next_attempt_at": timezone.now(),
            "payload": {"wire": wire, "offer_id": public["offerId"], "requested_by": user.pk, "company_id": str(context.company.pk), "destination": dest,
                        "installation_id": site.installation_id, "slug": handoff["slug"],
                        "title": str(export["metadata"].get("title") or "")[:500], "images": [i["name"] for i in handoff["images"]]}})
        if not created and any(op.payload["wire"].get(k) != v for k, v in binding.items()):
            raise Unavailable()
        if created:
            from .reviewed_article_pr_worker import advance
            from .website_wakeups import submit_operation
            transaction.on_commit(lambda: submit_operation(advance, op.pk), robust=True)
    return op


def _operation(data):
    if not isinstance(data, dict):
        raise Unavailable("invalid_request", 400)
    try:
        identifier = UUID(str(data.get("operation_id", "")))
    except (TypeError, ValueError):
        raise Unavailable()
    op = WebsiteConnectionOperation.objects.select_related("connection__organization").filter(pk=identifier, action=ACTION).first()
    if not op or canonical(data) != canonical(op.payload["wire"]):
        raise Unavailable()
    return op


def authorized_context(op, *, require_pending=True):
    site = op.connection
    data = op.payload
    wire = data["wire"]
    if require_pending and (op.state not in OPEN or op.receipt.get("status") != "pending"):
        raise Unavailable()
    user = get_user_model().objects.filter(pk=data["requested_by"], is_active=True).first()
    if not user:
        raise Unavailable()
    try:
        ctx = get_founder_company_context(user, company_id=data["company_id"])
    except (PermissionError, ObjectDoesNotExist):
        raise Unavailable() from None
    cfg, current = _selected(ctx.organization)
    if (site.pk != current.pk or current.generation != op.generation or data["installation_id"] != current.installation_id
            or any(wire.get(k) != v for k, v in contract_for(current).items()) or deletion_epoch(current) != wire["deletion_epoch"]):
        raise Unavailable()
    run = ContentFactoryRun.objects.filter(organization=ctx.organization, run_id=wire["source_run_id"]).first()
    if not run:
        raise Unavailable()
    check_source(ctx, run, user, cfg)
    if destination(cfg, current)[0] != data["destination"]:
        raise Unavailable()
    return ctx, run, cfg


@contextmanager
def authority(data):
    """Check the provider branch outside locks, then repeat local approval and policy checks."""
    op = _operation(data)
    authorized_context(op)
    verify_repository_native_target(op.connection)
    head = read_head(op.connection)
    if not head:
        raise Unavailable("source_temporarily_unavailable", 503)
    if head != data["expected_source_sha"]:
        raise Unavailable("source_changed")
    with saved_approval(op, data) as locked:
        yield locked


@contextmanager
def saved_approval(op, wire):
    """Fence the DB receipt against withdrawals, independently of a remote ref advancing."""
    with transaction.atomic():
        WebsiteConnection.objects.select_for_update().get(pk=op.connection_id)
        locked = WebsiteConnectionOperation.objects.select_for_update().select_related("connection__organization").get(pk=op.pk)
        if canonical(wire) != canonical(locked.payload["wire"]):
            raise Unavailable()
        authorized_context(locked)
        yield locked


def package_paths(op, target):
    slug, images = op.payload["slug"], op.payload["images"]
    if op.payload["wire"]["delivery_kind"] == "content_files":
        folder = f"mlai-content-files/{slug}"
        return {f"{folder}/index.md", f"{folder}/article.jsonld", f"{folder}/WIRING.md", "mlai-content-files/WIRING.md", *(f"{folder}/{i}" for i in images)}
    recipe = target.contract["registration_strategy"]["recipe"]
    prefix = "/".join(x for x in (recipe["static_root"], recipe["route_base"].strip("/")) if x)
    paths = {f"{prefix}/index.html", f"{prefix}/index.json", f"{prefix}/{slug}/index.html"}
    if recipe.get("origin"):
        paths.add(f"{prefix}/sitemap.xml")
    if recipe.get("sitemap_source"):
        paths.add(recipe["sitemap_source"])
    paths.update(f"{prefix}/{slug}/{i}" for i in images)
    return paths


def branch_name(op):
    return f"content-factory/reviewed-article-{op.pk}"
