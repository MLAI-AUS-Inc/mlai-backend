"""Read-only outcome telemetry for the one-click readiness and generation flow."""
from math import ceil

REVIEW_STATUSES = frozenset({"completed", "needs_review", "awaiting_approval", "approval_required", "ready_for_review", "review_ready", "pr_opened", "publish_bundle_ready"})


def _percentile(values, fraction):
    ordered = sorted(values)
    return round(ordered[max(0, ceil(len(ordered) * fraction) - 1)], 3) if ordered else None


def _article_roots(rows):
    """Group revisions/restarts by their recorded spend or original request lineage."""
    by_id = {row.get("run_id"): row for row in rows if row.get("run_id")}
    groups = {}
    for index, row in enumerate(rows):
        request = row.get("run_request") if isinstance(row.get("run_request"), dict) else {}
        spend = str(request.get("roo_points_ledger_id") or "")
        root = row.get("run_id") or f"sample:{index}"
        visited = set()
        for _ in range(20):
            current = by_id.get(root)
            source = (current or {}).get("run_request") or {}
            parent = source.get("original_billing_source_run_id") or source.get("restart_source_run_id") or (
                source.get("source_run_id") if (current or {}).get("workflow") == "article_revision" else None)
            if not parent or parent in visited:
                break
            visited.add(root)
            root = parent
        key = (row.get("organization_id"), "spend", spend) if len(spend) <= 19 and spend.isdecimal() and int(spend) > 0 else (row.get("organization_id"), "root", root)
        groups.setdefault(key, []).append(row)
    roots = []
    for group in groups.values():
        reviews = [row for row in group if row.get("status") in REVIEW_STATUSES]
        selected = max(reviews or group, key=lambda row: row.get("updated_at").isoformat() if row.get("updated_at") else "")
        results = [row.get("result") if isinstance(row.get("result"), dict) else {} for row in group]
        actions = [value["user_action_count"] for value in results if type(value.get("user_action_count")) is int and value["user_action_count"] >= 0]
        calls = [value["content_acceptance_calls"] for value in results if type(value.get("content_acceptance_calls")) is int and value["content_acceptance_calls"] >= 0]
        starts = [row["created_at"] for row in group if row.get("created_at")]
        result = {**(selected.get("result") or {}), "user_action_count": max(actions) if actions else None,
            "authority_refusals": [event for value in results for event in (value.get("authority_refusals") if isinstance(value.get("authority_refusals"), list) else [])],
            "reconciliation_recovery": any(value.get("reconciliation_recovery") or (value.get("reconciliation") or {}).get("outcome") in {
                "adopted_remote_terminal", "worker_recovery_exhausted", "missing_on_remote", "placeholder_never_dispatched"} for value in results)}
        if calls:
            result["content_acceptance_calls"] = sum(calls)
        roots.append({**selected, "created_at": min(starts) if starts else None, "result": result})
    return roots


def summarize_one_click(*, article_rows, prepare_rows):
    """Keep missing instrumentation visible instead of reporting invented success."""
    attempts = len(article_rows)
    article_rows = _article_roots(article_rows)
    durations, review_calls = [], []
    ready, zero_actions, unknown_actions, refusals, repairs = 0, 0, 0, 0, 0
    for row in article_rows:
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        if row.get("status") in REVIEW_STATUSES:
            ready += 1
            actions = result.get("user_action_count")
            zero_actions += int(actions == 0 and not isinstance(actions, bool))
            unknown_actions += int(actions is None)
            start, end = row.get("created_at"), row.get("updated_at")
            if start and end and end >= start:
                durations.append((end - start).total_seconds())
        calls = result.get("content_acceptance_calls")
        if isinstance(calls, int) and not isinstance(calls, bool) and calls >= 0:
            review_calls.append(calls)
        events = result.get("authority_refusals")
        refusals += len(events) if isinstance(events, list) else 0
        repair = result.get("reconciliation") or {}
        repairs += int(bool(result.get("reconciliation_recovery")) or repair.get("outcome") in {
            "adopted_remote_terminal", "worker_recovery_exhausted", "missing_on_remote", "placeholder_never_dispatched"})
    setups = [row for row in prepare_rows if row.get("state") == "completed" and row.get("receipt", {}).get("status") == "ready"]
    return {"articleRuns": len(article_rows), "articleAttempts": attempts, "articlesReachingReview": ready,
        "articlesWithZeroUserActions": zero_actions, "articlesWithoutActionTelemetry": unknown_actions,
        "prepareOperations": len(prepare_rows), "setupsReady": len(setups),
        "setupsReadyOnFirstClick": sum(row.get("payload", {}).get("user_clicks") == 1 for row in setups),
        "refusals": refusals, "sweepRepairs": repairs,
        "durationSeconds": {"p50": _percentile(durations, .5), "p90": _percentile(durations, .9),
            "sample": len(durations), "method": "creation_to_terminal_observation"},
        "reviewCalls": {"mean": round(sum(review_calls) / len(review_calls), 3) if review_calls else None,
            "p50": _percentile(review_calls, .5), "p90": _percentile(review_calls, .9), "sample": len(review_calls)}}


def one_click_outcomes(*, since, domain=None, row_limit=2000):
    """Bound the dashboard read by time, organization and row count."""
    from workflow_runs.models import ContentFactoryRun
    from .website_models import WebsiteConnectionOperation
    articles = ContentFactoryRun.objects.filter(created_at__gte=since,
        workflow__in=["article_generation", "direct_generate", "confirmed_topic", "article_revision"])
    prepares = WebsiteConnectionOperation.objects.filter(action="prepare", created_at__gte=since)
    if domain:
        articles = articles.filter(organization__domain=domain)
        prepares = prepares.filter(connection__organization__domain=domain)
    article_rows = list(articles.order_by("-created_at").values("run_id", "organization_id", "workflow", "run_request", "status", "result", "created_at", "updated_at")[:row_limit + 1])
    prepare_rows = list(prepares.order_by("-created_at").values("state", "payload", "receipt")[:row_limit + 1])
    return {**summarize_one_click(article_rows=article_rows[:row_limit], prepare_rows=prepare_rows[:row_limit]),
        "sampleLimit": row_limit, "truncated": len(article_rows) > row_limit or len(prepare_rows) > row_limit}
