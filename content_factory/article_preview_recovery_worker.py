"""Durable dispatch and readback for one saved-preview operation."""

from datetime import timedelta
from urllib.parse import quote, urlsplit
from uuid import uuid4

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from integrations import http_client
from workflow_runs.models import ContentFactoryRun

from . import article_preview_recovery as recovery
from .article_preview_recovery_state import REASONS, instant, mapping
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnectionOperation


def _claim(identifier, now):
    with transaction.atomic():
        op = (
            WebsiteConnectionOperation.objects.select_for_update(of=("self",))
            .filter(pk=identifier, action=recovery.ACTION, state="pending")
            .first()
        )
        if not op or op.next_attempt_at and op.next_attempt_at > now:
            return None
        if instant(op.payload["wire"]["expires_at"]) <= now:
            recovery.settle(op, "lease_expired")
            return None
        op.attempts += 1
        op.next_attempt_at = min(now + timedelta(seconds=70), instant(op.payload["wire"]["expires_at"]))
        op.save(update_fields=["attempts", "next_attempt_at", "updated_at"])
        return op


def _matching_proof(run_id, wire, snapshot):
    raw = mapping(snapshot.get("previewRecovery"))
    if (
        raw.get("state") != "ready"
        or snapshot.get("revision") != wire["revision"]
        or raw.get("runId") != run_id
        or raw.get("revision") != wire["revision"]
        or raw.get("sourceSha") != wire["source_sha"]
        or raw.get("operationId") != wire["operation_id"]
        or type(raw.get("attempt")) is not int
        or raw["attempt"] != wire["attempt"]
        or type(raw.get("generation")) is not int
        or raw["generation"] != wire["generation"]
    ):
        return None
    deadline = instant(raw.get("verifiedUntil"))
    try:
        url = urlsplit(raw.get("previewUrl") or "")
        good_url = (
            url.scheme == "https"
            and url.hostname
            and not url.username
            and not url.password
            and url.port in (None, 443)
        )
    except (ValueError, TypeError):
        return None
    if (
        not deadline
        or deadline <= timezone.now()
        or not good_url
        or not url.path.startswith(wire["directory"].rstrip("/") + "/")
    ):
        return None
    # The browser receives only the owner-scoped historical run's proxy.
    return {
        "revision": wire["revision"],
        "previewRecovery": {
            k: raw[k]
            for k in (
                "runId",
                "state",
                "revision",
                "operationId",
                "sourceSha",
                "attempt",
                "generation",
                "verifiedUntil",
            )
        },
    }


def advance(identifier, *, now=None):
    from .article_review_views import remote_review
    from .vibe_marketing_views import _content_factory_headers, _content_factory_remote_config

    now = now or timezone.now()
    op = _claim(identifier, now)
    if not op:
        return None
    wire, run_id = op.payload["wire"], op.payload["source_run_id"]
    result = None
    try:
        with recovery.authority(run_id, wire):
            pass
        run = ContentFactoryRun.objects.get(run_id=run_id)
        snapshot = remote_review(run, preview_recovery=True)
        if not isinstance(snapshot, dict):
            raise recovery.Refused("provider_unavailable", 503)
        if snapshot.get("revision") != wire["revision"] or snapshot.get("approved") is True:
            raise recovery.Refused("revision_changed")
        proof = _matching_proof(run_id, wire, snapshot)
        if proof:
            # The first provider call precedes CF readback. Reauthorize after it,
            # since the principal, deployment source or review may have changed.
            with recovery.authority(run_id, wire) as locked:
                if locked.attempts != op.attempts:
                    return locked.state
                locked.receipt = {"status": "ready", "proof": proof}
                locked.state, locked.next_attempt_at = "completed", None
                locked.save(update_fields=["receipt", "state", "next_attempt_at", "updated_at"])
                return locked.state
        else:
            raw = mapping(snapshot.get("previewRecovery"))
            exact = (
                raw.get("operationId") == wire["operation_id"]
                and raw.get("revision") == wire["revision"]
                and raw.get("generation") == wire["generation"]
                and raw.get("attempt") == wire["attempt"]
            )
            if exact and raw.get("state") in {"unavailable", "settled"}:
                raise recovery.Refused(raw.get("reason") if raw.get("reason") in REASONS else "build_failed")
            if not exact or raw.get("state") not in {"building", "validation_pending"}:
                cfg = _content_factory_remote_config()
                if not cfg["enabled"] or not cfg["api_key_configured"]:
                    raise recovery.Refused("provider_unavailable", 503)
                response = http_client.post(
                    f"{cfg['base_url']}/api/runs/{quote(run_id, safe='')}/article-review/preview-recovery",
                    headers=_content_factory_headers(),
                    json=wire,
                    timeout=(3, 20),
                )
                if response.status_code not in (200, 202):
                    raise recovery.Refused(
                        "build_failed"
                        if 400 <= response.status_code < 500 and response.status_code != 429
                        else "provider_unavailable",
                        409 if response.status_code in (400, 403, 409, 422) else 503,
                    )
    except (recovery.Refused, WebsiteAuthorityError) as exc:
        if getattr(exc, "status", 409) < 500 and not getattr(exc, "retryable", False):
            result = {"status": "settled", "reason": getattr(exc, "reason", "access_changed")}
    except (http_client.RequestException, ValueError):
        pass
    with transaction.atomic():
        updated = WebsiteConnectionOperation.objects.select_for_update(of=("self",)).get(pk=identifier)
        if updated.state != "pending" or updated.attempts != op.attempts:
            return updated.state
        if result:
            updated.state, updated.receipt, updated.next_attempt_at = "completed", result, None
        else:
            updated.receipt = {"status": "building"}
            updated.next_attempt_at = min(timezone.now() + timedelta(seconds=30), instant(wire["expires_at"]))
        updated.save(update_fields=["state", "receipt", "next_attempt_at", "updated_at"])
        return updated.state


def callback(run_id, payload):
    if not isinstance(payload, dict):
        raise recovery.Refused()
    state, reason = payload.get("state"), payload.get("reason")
    if state not in {"ready", "unavailable", "settled"} or not isinstance(reason, str) or len(reason) > 150:
        raise recovery.Refused()
    wire = {k: v for k, v in payload.items() if k not in {"state", "reason"}}
    with transaction.atomic():
        op = (
            WebsiteConnectionOperation.objects.select_for_update(of=("self",))
            .filter(
                action=recovery.ACTION,
                payload__source_run_id=run_id,
                payload__wire__operation_id=wire.get("operation_id"),
            )
            .first()
        )
        if not op or not recovery.exact_wire(wire, op.payload["wire"]):
            raise recovery.Refused()
        if (
            op.state == "completed"
            and op.receipt.get("status") == "ready"
            and state == "unavailable"
            and reason in recovery.MISSING_DEPLOYMENT
        ):
            op.receipt = {**op.receipt, "status": "expired", "reason": reason}
            op.save(update_fields=["receipt", "updated_at"])
        elif op.state == "pending":
            if state == "ready":
                # Do not accept readiness from a callback alone. An immediate due
                # sweep reads the independently scoped CF artifact after its lock releases.
                op.next_attempt_at = timezone.now()
                op.save(update_fields=["next_attempt_at", "updated_at"])
            else:
                recovery.settle(op, reason if reason in REASONS else "build_failed")
    return {"accepted": True}


def read_token(run_id, wire):
    from integrations.services.github_app import create_installation_access_token, GitHubAppTokenError
    from .website_tokens import token_index_key

    with recovery.authority(run_id, wire) as op:
        site = op.connection
    try:
        issued = create_installation_access_token(
            installation_id=site.installation_id,
            repository=site.github_repo,
            repository_id=site.repository_id,
            permission_mode="read",
            use_cache=False,
        )
    except (GitHubAppTokenError, http_client.RequestException) as exc:
        raise recovery.Refused("provider_unavailable", 503) from exc
    try:
        with recovery.authority(run_id, wire) as op:
            ref = uuid4().hex
            cache.set(f"website-issued-token:{ref}", issued.token, timeout=3600)
            for scope in (site.generation, f"operation:{op.pk}"):
                key = token_index_key(site.pk, scope)
                cache.set(key, [*(cache.get(key) or []), ref], timeout=3600)
            from .models import OrganizationContentConfig
            from .website_contract import sanitized_evidence

            cfg = OrganizationContentConfig.objects.get(
                website_connection=op.connection, organization_id=site.organization_id
            )
            selected = op.connection.targets.get(
                generation=op.generation, target_key=wire["target_key"], source_sha=wire["source_sha"]
            )
            return {
                "authorized": True,
                **wire,
                "github_token": issued.token,
                "permission_mode": "read",
                "publish_target": sanitized_evidence(selected.contract),
                "repo_execution_contract": sanitized_evidence(cfg.repo_execution_contract),
            }
    except Exception:
        try:
            http_client.delete(
                "https://api.github.com/installation/token",
                headers={"Authorization": f"Bearer {issued.token}"},
                timeout=(3, 8),
            )
        except http_client.RequestException:
            pass
        raise
