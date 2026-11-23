"""Track an approved draft PR command until GitHub or the founder's host supplies evidence."""
import logging
import re
from contextlib import contextmanager
from datetime import timedelta
from urllib.parse import quote
from uuid import uuid4

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from integrations import http_client
from . import reviewed_article_pr as pr
from .reviewed_article_pr import ACTION
from .website_contract import WebsiteAuthorityError, safe_repository_path
from .website_models import WebsiteConnectionOperation

logger = logging.getLogger(__name__)


def credential(data):
    """Issue a revocable, one-repository GitHub App token only to the trusted writer."""
    from integrations.services.github_app import create_installation_access_token, GitHubAppTokenError
    from .website_tokens import token_index_key
    with pr.authority(data) as op:
        site = op.connection
    try:
        issued = create_installation_access_token(installation_id=site.installation_id, repository=site.github_repo,
                        repository_id=site.repository_id, permission_mode="write", use_cache=False)
    except (GitHubAppTokenError, http_client.RequestException):
        raise pr.Unavailable("credential_temporarily_unavailable", 503) from None
    try:
        with pr.authority(data) as op:
            ref = uuid4().hex
            cache.set(f"website-issued-token:{ref}", issued.token, timeout=3600)
            for scope in (site.generation, f"operation:{op.pk}"):
                key = token_index_key(site.pk, scope)
                cache.set(key, [*(cache.get(key) or []), ref], timeout=3600)
            return {**issued.as_content_factory_payload(domain=site.organization.domain), "authorized": True,
                    **op.payload["wire"], "credential_reference": ref}
    except Exception:
        try:
            http_client.delete("https://api.github.com/installation/token",
                    headers={"Authorization": f"Bearer {issued.token}"}, timeout=(3, 8))
        except http_client.RequestException:
            pass
        raise


def receipt_paths(op, paths):
    if not isinstance(paths, list) or not 1 <= len(paths) <= 24 or not all(isinstance(x, str) for x in paths):
        raise pr.Unavailable()
    target = op.connection.targets.filter(generation=op.generation, target_key=op.payload["wire"]["connection_target_id"]).first()
    allowed = pr.package_paths(op, target)
    if len(set(paths)) != len(paths) or any(safe_repository_path(x) != x or x not in allowed for x in paths):
        raise pr.Unavailable()
    return set(paths)


@contextmanager
def mutation_guard(binding, data):
    """Reuse the ownership ledger, without granting this operation general configuration rights."""
    from .website_connections import authority_guard
    from uuid import UUID
    try:
        identifier = UUID(str(data.get("operation_id")))
    except ValueError:
        identifier = None
    op = WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION).first() if identifier else None
    if not op:
        with authority_guard(binding, action="config_write") as site:
            yield site
        return
    try:
        wire = op.payload["wire"]
        scope = ("website_connection_id", "connection_generation", "repository_id", "github_repo", "domain",
                 "expected_source_sha", "operation_id", "operation_attempt", "deletion_epoch")
        if (any(data.get(k) != wire[k] for k in scope)
                or data.get("run_id") != str(op.pk)
                or data.get("branch") != pr.branch_name(op) or data.get("status") not in {"applied", "proposed"}
                or any(k in data for k in ("article_publication_intent_v1", "native_ownership", "static_preview_reference"))
                or not isinstance(data.get("files"), list)):
            raise pr.Unavailable()
        with pr.authority(wire) as locked:
            base = data.get("base_sha")
            if (base != wire["expected_source_sha"] and not locked.connection.repository_mutations.filter(
                    generation=locked.generation, run_id=str(locked.pk), branch=pr.branch_name(locked),
                    status="applied", head_sha=base).exists()):
                raise pr.Unavailable()
            if data["files"]:
                if any(not isinstance(r, dict) or r.get("kind") != "user_content" for r in data["files"]):
                    raise pr.Unavailable()
                receipt_paths(locked, [r.get("path") for r in data["files"]])
            if data.get("pr_url") and not re.fullmatch(
                    rf"https://github\.com/{re.escape(wire['github_repo'])}/pull/[1-9][0-9]*", data["pr_url"]):
                raise pr.Unavailable()
            yield locked.connection
    except pr.Unavailable:
        raise WebsiteAuthorityError("website_operation_changed", "The reviewed pull request approval changed.") from None


def _receipt(op, result):
    wire = op.payload["wire"]
    if (not isinstance(result, dict) or any(pr.canonical(result.get(k)) != pr.canonical(v) for k, v in wire.items())
            or result.get("draft") is not True or result.get("counts_as_published") is not False
            or result.get("consumes_free_article") is not False or result.get("branch_name") != pr.branch_name(op)
            or not pr.SHA.fullmatch(str(result.get("head_sha") or "")) or result["head_sha"] == wire["expected_source_sha"]
            or type(result.get("pr_number")) is not int or result["pr_number"] <= 0
            or result.get("pr_url") != f"https://github.com/{wire['github_repo']}/pull/{result['pr_number']}"):
        raise pr.Unavailable("receipt_invalid")
    paths = receipt_paths(op, result.get("paths"))
    required = {"branch_name", "head_sha", "pr_number", "pr_url", "paths"}
    slug = op.payload["slug"]
    if wire["delivery_kind"] == "static_dropin":
        target = op.connection.targets.get(generation=op.generation, target_key=wire["connection_target_id"])
        recipe = target.contract["registration_strategy"]["recipe"]
        base = recipe["route_base"]
        prefix = "/".join(x for x in (recipe["static_root"], base.strip("/")) if x)
        if (result.get("target_id") != target.target_key or result.get("contract_digest") != target.contract["contract_digest"]
                or result.get("artifact_digest") != (target.contract.get("live_marker") or {}).get("value")
                or result.get("source_sha") != wire["expected_source_sha"] or not pr.HASH.fullmatch(str(result.get("rendered_article_sha256") or ""))
                or result.get("route_path") != f"{base}{slug}/" or result.get("directory_path") != base
                or not {f"{prefix}/index.html", f"{prefix}/index.json", f"{prefix}/{slug}/index.html"}.issubset(paths)):
            raise pr.Unavailable("receipt_invalid")
        required.update(("target_id", "contract_digest", "artifact_digest", "rendered_article_sha256", "route_path", "directory_path"))
    elif (paths - {"mlai-content-files/WIRING.md", f"mlai-content-files/{slug}/WIRING.md"} !=
          pr.package_paths(op, None) - {"mlai-content-files/WIRING.md", f"mlai-content-files/{slug}/WIRING.md"}):
        raise pr.Unavailable("receipt_invalid")
    return {"status": "draft_pr_open", **{k: result[k] for k in required}}


def dispatch(op):
    from .vibe_marketing_views import _content_factory_remote_config, _content_factory_headers
    cfg = _content_factory_remote_config()
    if not cfg["enabled"] or not cfg["api_key_configured"]:
        raise pr.Unavailable("unavailable", 503)
    wire = op.payload["wire"]
    with pr.authority(wire):
        pass
    url = f"{cfg['base_url']}/api/runs/{quote(wire['source_run_id'], safe='')}/reviewed-article-pr"
    response = http_client.post(url, headers=_content_factory_headers(), json=wire, timeout=(3, 75))
    if response.status_code not in (200, 201):
        raise pr.Unavailable("worker_unavailable", 503 if response.status_code >= 500 or response.status_code == 429 else 409)
    with pr.authority(wire) as locked:
        return _receipt(locked, response.json())


def advance(identifier, *, now=None):
    """Lease one idempotent delivery; revisit open static PRs independently of the client."""
    from .reviewed_article_pr_verify import observe
    now = now or timezone.now()
    with transaction.atomic():
        op = WebsiteConnectionOperation.objects.select_for_update().select_related("connection__organization").get(pk=identifier)
        if op.action != ACTION or op.state != "pending" or op.next_attempt_at and op.next_attempt_at > now:
            return None
        op.next_attempt_at, op.attempts = now + timedelta(minutes=3), op.attempts + 1
        op.save(update_fields=["next_attempt_at", "attempts", "updated_at"])
    receipt, state, delay = op.receipt, "pending", timedelta(minutes=2)
    try:
        if not pr.enabled():
            raise pr.Unavailable("delivery_disabled", 503)
        if receipt.get("status") == "pending":
            if now - op.created_at > timedelta(hours=6):
                raise pr.Unavailable("confirmation_delayed")
            receipt = dispatch(op)
            if op.payload["wire"]["delivery_kind"] == "content_files":
                state = "completed"
        else:
            receipt, state = observe(op, now)
            delay = timedelta(hours=12) if receipt.get("status") == "draft_pr_open" and now - op.created_at > timedelta(days=1) else timedelta(minutes=10)
    except pr.Unavailable as exc:
        if exc.status < 500:
            state, receipt = "needs_attention", {**receipt, "status": "needs_attention", "reason": str(exc)}
    except Exception as exc:
        logger.warning("reviewed_article_pr_followup operation=%s error_type=%s", op.pk, type(exc).__name__)
    if state == "pending" and receipt.get("status") != "draft_pr_open" and now - op.created_at > timedelta(days=7):
        state, receipt = "needs_attention", {**receipt, "status": "needs_attention", "reason": "confirmation_delayed"}
    updated = WebsiteConnectionOperation.objects.filter(pk=op.pk, state="pending", attempts=op.attempts).update(
        state=state, receipt=receipt, next_attempt_at=now + delay if state == "pending" else None, updated_at=now)
    if updated and state == "needs_attention" and receipt.get("reason") in {"confirmation_delayed", "receipt_invalid"}:
        from .founder_journey_alerts import queue_founder_journey_alert
        queue_founder_journey_alert(organization=op.connection.organization, alert_reason="self_heal_capped", slot=str(op.pk),
            details={"website_connection_id": str(op.connection_id)})
    return state
