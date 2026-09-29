"""Versioned presentation of baseline measurements, without rewriting history."""

import math


AUTHORITY_METHOD_VERSION = "ahrefs-dr-v1"
AI_VISIBILITY_METHOD_VERSION = "ai-mentions-v2"


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _unavailable(metric, *, legacy=False):
    return {
        **metric,
        "status": "unavailable",
        "score": None,
        "verified": False,
        "reasonCode": "legacy_method" if legacy else "invalid_measurement",
        "message": "Rerun the baseline to collect the updated measurement." if legacy else "This measurement is incomplete. Rerun the baseline.",
    }


def _ai_metric(metric):
    metric = dict(metric)
    if metric.get("methodVersion") != AI_VISIBILITY_METHOD_VERSION:
        if metric.get("score") is None and metric.get("status") != "measured":
            metric["score"] = None
            if isinstance(metric.get("providers"), list):
                metric["providers"] = [_ai_metric(provider) for provider in metric["providers"] if isinstance(provider, dict)]
            return metric
        metric = _unavailable(metric, legacy=True)
        metric["methodVersion"] = "legacy"
        metric["providers"] = [
            {**_unavailable(provider, legacy=True), "methodVersion": "legacy"}
            for provider in metric.get("providers", []) if isinstance(provider, dict)
        ]
        return metric
    if metric.get("status") != "measured":
        return {**metric, "score": None}
    responses, mentions, citations = (metric.get(key) for key in ("responseCount", "mentionCount", "citationCount"))
    requested = metric.get("requestedCount")
    if (
        not all(_count(value) for value in (responses, mentions, citations, requested))
        or not responses or mentions > responses or citations > responses or responses > requested
    ):
        return _unavailable(metric)
    # Integer arithmetic avoids Python's ties-to-even behavior at e.g. 12.5%.
    metric["score"] = (200 * mentions + responses) // (2 * responses)
    if isinstance(metric.get("providers"), list):
        metric["providers"] = [_ai_metric(provider) for provider in metric["providers"] if isinstance(provider, dict)]
    return metric


def baseline_display_metrics(metrics):
    """Hide incompatible historical scores and expose evidence-backed values only."""
    result = dict(metrics or {})
    authority = result.get("authority")
    if isinstance(authority, dict):
        authority = dict(authority)
        authority.pop("authorityScore", None)
        if (
            not authority.get("methodVersion") and authority.get("source") == "Ahrefs"
            and authority.get("status") == "measured" and _number(authority.get("domainRating"))
            and 0 <= authority["domainRating"] <= 100
        ):
            # Historical full snapshots retain the original vendor provenance.
            authority["methodVersion"] = AUTHORITY_METHOD_VERSION
        if authority.get("methodVersion") != AUTHORITY_METHOD_VERSION or authority.get("source") != "Ahrefs":
            if authority.get("score") is None and authority.get("status") != "measured":
                authority = {**authority, "score": None, "domainRating": None}
            else:
                authority = {**_unavailable(authority, legacy=True), "methodVersion": "legacy", "domainRating": None}
        elif authority.get("status") != "measured":
            authority = {**authority, "score": None, "domainRating": None}
        elif not _number(authority.get("domainRating")) or not 0 <= authority["domainRating"] <= 100:
            authority = {**_unavailable(authority), "domainRating": None}
        else:
            authority["score"] = authority["domainRating"]
        result["authority"] = authority
    if isinstance(result.get("aiVisibility"), dict):
        result["aiVisibility"] = _ai_metric(result["aiVisibility"])
    return result
