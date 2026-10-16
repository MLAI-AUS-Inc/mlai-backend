"""Read-only, company-scoped Articles status; this projection never schedules."""

from django.conf import settings
from django.utils import timezone

from . import website_goal as goals
from .website_goal_evidence import source_for

OWNER_ACTIONS = {"authorize_github", "review_github_permissions", "review_setup", "merge_pr", "deploy_site",
                 "choose_articles_page", "remove_setup_example", "choose_article_channel", "review_draft"}


def _action(raw, operation_id, *, owner=False, run_ids=(), repository=""):
    from urllib.parse import urlsplit
    value = goals.mapping(raw)
    identifier = value.get("id")
    if not isinstance(identifier, str) or owner and identifier not in OWNER_ACTIONS:
        return {"id": "inspect_goal", "label": "View details", "operationId": operation_id}
    labels = {"authorize_github": "Authorize GitHub", "review_github_permissions": "Review GitHub permissions",
        "review_setup": "Review setup", "review_draft":"Review first article", "merge_pr": "Merge pull request", "deploy_site": "Deploy your site",
        "choose_article_channel": "Choose articles channel", "choose_articles_page": "Choose articles page", "remove_setup_example": "Review example cleanup"}
    result = {"id": identifier if identifier in OWNER_ACTIONS else "inspect_goal", "label": labels.get(identifier, "View details")}
    run_id = value.get("run_id")
    if not run_id and identifier in {"review_setup", "review_draft", "merge_pr", "deploy_site", "remove_setup_example"}:
        run_id = next((item for item in run_ids if isinstance(item, str) and item), None)
    if run_id in run_ids and isinstance(run_id, str) and 0 < len(run_id) <= 100 and all(c.isalnum() or c in "-_.:" for c in run_id):
        result["runId"] = run_id
    if type(value.get("pr_number")) is int and value["pr_number"] > 0:
        result["prNumber"] = value["pr_number"]
    url = value.get("url")
    if isinstance(url, str) and len(url) <= 2048:
        try:
            parsed = urlsplit(url)
            if (identifier in {"authorize_github", "review_github_permissions"} and parsed.scheme == "https"
                    and parsed.hostname == "github.com" and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment):
                result["url"] = url
        except ValueError:
            pass
    if identifier == "choose_articles_page":
        result["repository"] = repository
        raw_candidates = value.get("candidates")
        if isinstance(raw_candidates, list):
            result["candidates"] = [route for route in raw_candidates[:8] if isinstance(route, str) and len(route) <= 255
                and route.startswith("/") and not route.startswith("//") and not any(c in route for c in "?#\\\r\n")]
    result["operationId"] = operation_id
    return result


def project_status(op, *, scope, source_sha, journey, now=None):
    """Project only persisted schedules and current proof, with terminal precedence."""
    from .website_failure_redaction import redact_secret_text
    from .article_publication_policy import journey_can_publish
    now = now or timezone.now()
    receipt = goals.mapping(op.receipt)
    goal = receipt.get("goal")
    legacy = "goal" not in receipt
    valid = goals.valid_goal(goal, scope)
    milestones = []
    if valid:
        for item in goal["milestones"]:
            passed = goals.valid_proof(item["name"], item.get("proof"), scope=scope, source_sha=source_sha, now=now)
            milestones.append({"name": item["name"], "state": "passed" if passed else "not_required" if not item["required"] else "pending",
                "required": item["required"], "proof": ({key: item["proof"].get(key) for key in ("kind", "sha", "run_id", "evidence_digest", "at")} if passed else None)})
    terminal = op.state in {"cancelled", "denied", "deleted", "stopped", "failed", "needs_attention"}
    current_access = all(goals.mapping(goals.mapping(journey.get("prerequisites")).get(key)).get("status") == "complete"
                         for key in ("account", "repository"))
    disconnected = op.action == "disconnect" and op.state == "completed"
    done = disconnected or (op.state == "completed" and current_access and (valid
        and all(not item["required"] or item["state"] == "passed" for item in milestones)
        or legacy and journey_can_publish(journey)))
    action = _action(receipt.get("user_action") or {**goals.mapping(receipt.get("prompt")), "id": goals.mapping(receipt.get("prompt")).get("action")}, str(op.pk), owner=True,
        run_ids=(receipt.get("setup_run_id"), receipt.get("child_run_id"),receipt.get("first_article_run_id"),receipt.get("first_article_publish_run_id")), repository=op.connection.github_repo) if op.state == "needs_user" else None
    schedule = op.next_attempt_at if op.state in {"pending", "running"} else None
    if terminal:
        state = "stopped"
    elif done:
        state = "done"
    elif op.state == "needs_user" and action and action["id"] in OWNER_ACTIONS:
        state = "needs_you"
    elif (legacy or valid and goals.enrolled(op.connection.organization_id)) and schedule:
        state = "working"
    else:
        state = "stopped"
    if state == "stopped":
        action = _action(None, str(op.pk))
        message = "Website preparation has stopped. Review its recorded details."
        if not valid and not legacy:
            message = "The saved goal does not match the current website. Refresh the website details."
        elif op.state in {"pending", "running"} and not schedule:
            message = "The goal has no recorded recovery schedule. Review its recorded details."
        elif receipt.get("budget_stop_reason"):
            message = "The articles goal reached its execution budget. Review its recorded usage."
        elif op.state == "completed":
            message = "Current website proof has changed or expired. Refresh website verification."
        elif not terminal and not goals.enrolled(op.connection.organization_id):
            message = "Goal execution is disabled. Its recorded work is retained."
    elif state == "done":
        message = "Your website is disconnected." if disconnected else "Your first article is live." if valid and goal["target"] == "first_article_live" else "Your website is ready for articles."
    else:
        message = redact_secret_text(str(receipt.get("message") or "Preparing your website for articles."))[:500]
    due = schedule.isoformat() if schedule and state == "working" else None
    step = next((item["name"] for item in milestones if item["required"] and item["state"] != "passed"), None)
    evidence = [{"id": f"milestone:{item['name']}", "result": "passed" if item["state"] in {"passed", "not_required"} else "pending",
        "at": goals.mapping(item.get("proof")).get("at"), "message": item["name"].replace("_", " ")} for item in milestones]
    return {"version": 1, "state": state, "step": step, "title": {"working": "Setting up articles", "needs_you": "Your action is needed",
        "done": "Website disconnected" if disconnected else "First article live" if valid and goal["target"] == "first_article_live" else "Articles ready", "stopped": "Setup stopped"}[state], "message": message, "nextAttemptAt": due,
        "action": action, "evidence": evidence, "operationId": str(op.pk), "updatedAt": op.updated_at.isoformat(),
        "companyId": scope["company_id"], "connectionId": scope["connection_id"], "connectionGeneration": scope["connection_generation"],
        "configurationRevision": scope["configuration_revision"],
        "goalId": goal.get("goal_id") if valid else None, "target": goal.get("target") if valid else None,
        "milestones": milestones, "expectedTime": None, "mode": "goal" if not legacy else "legacy"}


def status_for_context(context, config, *, journey, now=None):
    """Select one current owned operation; feature-off responses remain unchanged."""
    if not getattr(settings, "ARTICLES_WEBSITE_STATUS_V1_ENABLED", False) or config.website_connection is None:
        return None
    connection = config.website_connection
    from django.db.models import Q
    op = connection.operations.filter(generation=connection.generation).filter(
        Q(action="disconnect") | Q(action="prepare", payload__company_id=str(context.company.pk)) |
        Q(action="source-reverify", payload__site_recipe_reconnect=True)).order_by("-created_at", "-pk").first()
    if op is None:
        return {"version": 1, "state": "stopped", "step": None, "title": "Articles setup is available",
            "message": "Set up articles to create a website goal.", "nextAttemptAt": None,
            "action": {"id": "prepare", "label": "Set up articles"}, "evidence": [], "operationId": None,
            "updatedAt": None, "companyId": str(context.company.pk), "connectionId": str(connection.pk),
            "connectionGeneration": connection.generation, "goalId": None, "target": None,
            "configurationRevision": connection.configuration_version,
            "milestones": [], "expectedTime": None, "mode": "goal"}
    scope, source = goals.scope_for(connection, context.company.pk), source_for(connection, journey)
    result = project_status(op, scope=scope, source_sha=source, journey=journey, now=now)
    from .website_native_inventory import add_display
    result = add_display(result, op)
    if getattr(settings, "ARTICLES_WEBSITE_DOCTOR_ENABLED", False):
        from .website_doctor import read_doctor
        result["doctor"] = read_doctor(op, scope=scope, source_sha=source, milestones=result["milestones"])
    return result


def add_status(payload, context, config, *, journey):
    """Add the shared server contract only when the status rollout is enabled."""
    from .website_publishing_channels import projection
    from .publishing_channels import enabled as channels_enabled, inspect as inspect_channel
    if channels_enabled():
        from .publishing_channel_contract import ChannelRequest
        channel = inspect_channel(ChannelRequest(context=context, config=config))["planning"]
    else:
        channel = projection(context, config)
    if channel is not None:
        payload["publishingChannel"] = channel
    result = status_for_context(context, config, journey=journey)
    if result is not None:
        from .website_setup_consent import setup_plan
        plan = setup_plan(context, config, journey)
        if plan is not None:
            result["setupPlan"] = plan
        payload["websiteStatus"] = result
    return payload
