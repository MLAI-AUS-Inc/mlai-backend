"""Short-lived workflow presentation cache; live authority is never cached here."""
from copy import deepcopy
import hashlib
import json
import logging

from django.core.cache import cache
from django.db.models import Count, Max

from .models import VibeMarketingComponentComment
from workflow_runs.models import ContentFactoryRun

logger = logging.getLogger(__name__)


def _versions(organization):
    # Source revisions and feedback outside the displayed window also affect
    # progress. Match the domain scope of the existing bootstrap run reader.
    return {
        "runs": ContentFactoryRun.objects.filter(domain=organization.domain).aggregate(
            count=Count("id"), updated=Max("updated_at")),
        "comments": VibeMarketingComponentComment.objects.filter(run__domain=organization.domain).aggregate(
            count=Count("id"), updated=Max("updated_at")),
    }


def _cache_key(context, config, runs, checks, candidates, serialized_runs, capabilities):
    from .vibe_marketing_views import _repository_observation_scope
    connection = getattr(config, "website_connection", None)
    identity = {
        "organization": context.organization.pk, "domain": context.organization.domain,
        "actor": context.profile.user.pk, "configUpdated": getattr(config, "updated_at", None),
        "connection": _repository_observation_scope(connection) if connection else None,
        "connectionUpdated": getattr(connection, "updated_at", None),
        "versions": _versions(context.organization),
        "currentRuns": [(run.run_id, run.updated_at) for run in runs],
        "displayedRuns": [run.get("runId") for run in serialized_runs],
        "checks": checks, "capabilities": capabilities, "topicCount": len(candidates),
    }
    # Hash only public control state and versions, never raw run artifacts.
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return "articles-bootstrap-progress-v1:" + digest


def project_workflow_progress(*, context, config, runs, checks, candidates, serialized_runs, capabilities):
    """Reuse workflow presentation only while its scoped input versions match."""
    from .vibe_marketing_views import _bootstrap_cache_seconds, _workflow_progress
    ttl = min(_bootstrap_cache_seconds(), 20)
    key = None
    if ttl:
        try:
            key = _cache_key(context, config, runs, checks, candidates, serialized_runs, capabilities)
            existing = cache.get(key)
            if (isinstance(existing, dict) and isinstance(existing.get("root"), dict)
                    and isinstance(existing.get("runs"), dict)):
                return deepcopy(existing)
        except Exception:
            logger.warning("bootstrap_progress_cache_unavailable org_id=%s operation=read", context.organization.pk)
    by_id = {run.run_id: run for run in runs}
    result = {
        "root": _workflow_progress(context=context, latest_runs=runs, checks=checks,
            topic_candidates=candidates, config=config),
        "runs": {serialized["runId"]: _workflow_progress(context=context, run=by_id[serialized["runId"]],
            latest_runs=runs, checks=checks, topic_candidates=candidates, config=config)
            for serialized in serialized_runs if serialized.get("runId") in by_id},
    }
    if key:
        try:
            cache.set(key, deepcopy(result), ttl)
        except Exception:
            logger.warning("bootstrap_progress_cache_unavailable org_id=%s operation=write", context.organization.pk)
    return result
