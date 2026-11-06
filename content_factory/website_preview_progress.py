"""Best-effort build observation for the current preparation, with no run control."""
import hashlib
import re
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .website_contract import connection_contract, WebsiteAuthorityError

PHASES = frozenset({"waiting_for_runner", "building", "awaiting_callback", "failed"})


def prepare_build_progress(op, connection):
    """Read one original current child via CF's observation-only endpoint.

    Older CF releases return404 here rather than interpreting a new query flag
    as an ordinary preview GET, which may start recovery. This never calls that
    preview GET or a status/control endpoint.
    """
    if (op is None or op.action != "prepare" or op.state not in {"pending", "running"}
            or connection.state != "connected" or op.generation != connection.generation):
        return None
    identifier = (op.receipt or {}).get("child_run_id")
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier):
        return None
    from workflow_runs.models import ContentFactoryRun
    run = ContentFactoryRun.objects.filter(run_id=identifier, organization_id=connection.organization_id,
        workflow="article_system_setup", run_request__website_connection_id=str(connection.pk),
        run_request__connection_generation__in=[connection.generation, str(connection.generation)]).first()
    if run is None or run.status in {"completed", "failed", "blocked", "cancelled", "denied"}:
        return None
    saved = run.run_request or {}
    try:
        original = connection_contract(saved)
    except WebsiteAuthorityError:
        return None
    if (original.get("website_connection_id") != str(connection.pk)
            or original.get("connection_generation") != connection.generation
            or original.get("repository_id") != connection.repository_id
            or any(saved.get(key) != getattr(connection, key) for key in ("github_repo", "branch", "app_root"))):
        return None
    key = "website-preview-progress:" + hashlib.sha256(
        f"{connection.pk}:{connection.generation}:{connection.configuration_version}:{identifier}".encode()).hexdigest()
    try:
        prior = cache.get(key)
        if prior is not None:
            return prior if isinstance(prior, dict) else None
        if not cache.add(key + ":lease", True, timeout=15):
            return None
        cache.set(key, False, timeout=15)
    except Exception:
        return None
    try:
        from integrations import http_client
        from .vibe_marketing_views import _content_factory_headers, _content_factory_remote_config
        remote = _content_factory_remote_config()
        if not remote["enabled"]:
            return None
        response = http_client.get(remote["base_url"].rstrip("/") + f"/api/runs/{identifier}/preview-build-progress",
            headers=_content_factory_headers(), timeout=(3, 6))
        if response.status_code != 200:
            return None
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("runId") != identifier or payload.get("phase") not in PHASES:
            return None
        run_id = payload.get("builderRunId")
        url = payload.get("url")
        if (not isinstance(run_id, str) or not re.fullmatch(r"[0-9]{1,20}", run_id)
                or not isinstance(url, str) or not re.fullmatch(
                    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/" + run_id, url)):
            return None
        result = {"runId": identifier, "phase": payload["phase"], "url": url}
        for field in ("observedAt", "lastProgressAt"):
            value = payload.get(field)
            stamp = parse_datetime(value) if isinstance(value, str) and len(value) <= 40 else None
            if stamp is not None and timezone.is_aware(stamp) and stamp <= timezone.now() + timedelta(minutes=1):
                result[field] = stamp.isoformat()
        cache.set(key, result, timeout=30)
        return result
    except Exception:
        return None
    finally:
        try:
            cache.delete(key + ":lease")
        except Exception:
            pass
