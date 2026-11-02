"""P2 journey lines from durable outcomes, without provider calls or mutations."""
from __future__ import annotations

import re
from urllib.parse import quote, urlencode, urlsplit

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import p2_foundation, registry_policy


def mapping(value):
    return value if isinstance(value, dict) else {}


def optional(module, name, *args, default=None, **kwargs):
    """Call an optional package seam; absence or failure grants no authority."""
    function = p2_foundation._optional(module, name)
    if function is None:
        return default
    try:
        return function(*args, **kwargs)
    except Exception:
        return default


def verified_email(user):
    """An active account with a verified email can receive an email notice."""
    return bool(user and getattr(user, "is_active", False) and getattr(user, "email_verified_at", None)
                and str(getattr(user, "email", "") or "").count("@") == 1)


def emailable(organization, user):
    """Project the email promise only for a reachable, verified recipient."""
    return bool(verified_email(user) and str(getattr(settings, "RESEND_API_KEY", "") or "") and (getattr(settings, "MY_STARTUP_TRANSITION_NOTIFICATIONS_ENABLED", False)
        or getattr(settings, "ARTICLES_DRAFT_READY_NOTIFICATIONS_ENABLED", False)))


def recipient_checks_enabled(organization):
    """Enforce team recipient fences even when the recovery-policy switch is off."""
    return bool(p2_foundation.p2_applies(organization) and any(getattr(settings, name, False) for name in (
        "MY_STARTUP_TEAM_ENABLED", "MY_STARTUP_TRANSITION_NOTIFICATIONS_ENABLED", "ARTICLES_FAILURE_POLICY_V2_ENABLED")))


def claims_count(run):
    """Actual current findings, falling back to the durable strict-pass count."""
    result = mapping(getattr(run, "result", None))
    findings = result.get("reviewFindings")
    if isinstance(findings, list):
        return sum(isinstance(row, dict) and row.get("kind") == "claim"
                   and row.get("state") not in {"resolved", "dismissed"} for row in findings)
    value = mapping(result.get("strict_pass_v1")).get("claims")
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, list):
        return len(value)
    return None


def ready_before_preview(run, organization=None):
    """Accept a CF-authored readiness receipt only for an enrolled live article."""
    if run is None or run.status in {"cancelled", "denied"}:
        return False
    org = organization or getattr(run, "organization", None)
    receipt = mapping(mapping(run.result).get("draft_ready_v1"))
    try:
        at = parse_datetime(receipt.get("at")) if isinstance(receipt.get("at"), str) else None
    except ValueError:
        at = None
    revision = receipt.get("revision")
    return bool(type(receipt.get("version")) is int
                and receipt["version"] == 1 and at and not timezone.is_naive(at)
                and isinstance(revision, str) and 0 < len(revision) <= 256 and p2_foundation.p2_applies(org))


def destination(organization, company_id, *, run_id=None, ask=None, item=None):
    """Links carry only company/run identifiers, never recipient or cohort data."""
    origin = str(getattr(settings, "COMMUNITY_CHAT_FRONTEND_URL", "") or "https://chat.mlai.au").rstrip("/")
    try:
        parts = urlsplit(origin)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path not in {"", "/"}:
            return None
    except ValueError:
        return None
    if ask == "review_draft" and run_id:
        path = "runs/" + quote(str(run_id), safe="") + "/draft"
    elif ask == "approve_join_request" or str(item or "").startswith("team:"):
        path = "team"
    elif ask in {"approve_setup", "authorize_github", "approve_on_github"} or str(item or "").startswith("website:"):
        path = "website"
    else:
        path = ""
    return origin + "/my-startup/" + path + "?" + urlencode({"company_id": str(company_id)})


def _safe_name(value, limit=100):
    text = str(value or "").strip()
    if re.search(r"(?:gh[pousr]_|sk[-_]|re_|xox[baprs]-|pypi-|eyJ)[A-Za-z0-9_-]{8,}", text) or "bearer" in text.lower():
        return None
    return text[:limit] if text and re.fullmatch(r"[\w .:/()#-]+", text) and "@" not in text and "://" not in text else None


def developer_note(organization, run):
    """A public support handoff with bounded names, never raw errors or secrets."""
    result = mapping(getattr(run, "result", None))
    site = str(organization.domain or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9.-]+", site):
        site = "your website"
    failure = mapping(result.get("failure"))
    fingerprint = result.get("failure_fingerprint") or failure.get("fingerprint")
    support = _safe_name(fingerprint, 128)
    check = _safe_name(result.get("failing_check") or result.get("failing_test") or mapping(result.get("site_test_v1")).get("test_name"))
    pr = mapping(result.get("site_test_v1")).get("pr_url") or mapping(result.get("publish_pr_v1")).get("pr_url") or result.get("pr_url")
    try:
        parts = urlsplit(str(pr or ""))
        if parts.scheme != "https" or parts.hostname != "github.com" or parts.query or parts.fragment or parts.username or parts.password or not re.fullmatch(r"/[^/]+/[^/]+/pull/[1-9][0-9]*", parts.path):
            pr = None
    except ValueError:
        pr = None
    text = f"MLAI tried to add the blog page to {site} and checked the generated changes."
    if check:
        text += f" The check that needs attention is {check}."
    if pr:
        text += " Pull request: " + pr + "."
    if support:
        text += " Support code: " + support + "."
    return {"subject": "Blog page checks for " + site, "body": text}


def line(copy_id, *, run=None, count=None, notify=False, queue=None, outcome=None, expected=None):
    """Exact contract copy with one canonical ask or one next action."""
    run_id = getattr(run, "run_id", None)
    headline, body, label, ask, next_kind = "Working on it", "Still working.", None, None, "view_progress"
    if copy_id == "working_slow":
        body = "Taking a little longer than usual."
    elif copy_id == "working_queue":
        from .founder_journey import ordinal, minutes
        queue = queue or {}
        body = f"You're {ordinal(queue.get('queuePosition') or 1)} in line"
        eta = queue.get("etaSeconds")
        body += f" · about {minutes(eta)} minutes." if eta is not None else "."
    elif copy_id == "working_website_update":body = "Waiting for your website to update."
    elif copy_id == "working_publishing":body = "Publishing…"
    elif copy_id.startswith("ask_"):
        ask = copy_id.removeprefix("ask_")
        ask = {"check_highlighted": "review_draft", "authorize_github_revoked": "authorize_github",
            "approve_on_github_protected": "approve_on_github", "approve_on_github_site_test": "approve_on_github"}.get(ask, ask)
        rows = {
            "ask_review_draft": ("Your draft is ready", "Tap anything to suggest a change, then copy it to your website.", "Read your article"),
            "ask_check_highlighted": ("Your draft is ready", "Please check the highlighted sentence before you use it." if count == 1 else f"Please check the {count} highlighted sentences before you use it.", "Check them"),
            "ask_describe_customer": ("Who is your typical customer?", "One short answer and we'll start your article.", "Describe your customer"),
            "ask_agree_terms": ("Our terms were just updated", "Please tick to agree again.", "Agree to the terms"),
            "ask_authorize_github": ("We need permission to read your website on GitHub", None, "Give access on GitHub"),
            "ask_authorize_github_revoked": ("MLAI can't read your website on GitHub any more", None, "Give access on GitHub"),
            "ask_approve_setup": ("Your blog page is ready", "Take a look, then add it to your website.", "Add to my website"),
            "ask_approve_on_github": ("Your change is waiting on GitHub", "Approve it on GitHub when you're ready.", "Approve on GitHub"),
            "ask_approve_on_github_protected": ("GitHub needs you to approve one change for your blog", None, "Approve on GitHub"),
            "ask_approve_on_github_site_test": (f"Your site has a test that expects exactly {expected if type(expected) is int else 'a fixed number of'} pages", "Approve a one-line change on GitHub.", "Approve on GitHub"),
            "ask_approve_join_request": ("Someone asked to join your team", None, "Review request"),
        }
        headline, body, label = rows.get(copy_id, ("Your articles", None, "Continue"))
        next_kind = None
    elif copy_id in {"done_copy_ready_strategy", "done_copy_ready_ladder_exhausted"}:
        exhausted = copy_id.endswith("ladder_exhausted")
        headline = "Your articles are still ready to copy" if exhausted else "Your article is ready to copy"
        body = "Your website's own checks didn't accept the new blog page." if exhausted else "We'll offer to add it to your website once your blog page works."
        label, next_kind = ("Send details to my developer" if exhausted else "Read your article"), "view_draft"
    elif copy_id == "stopped":
        outcome = outcome or {}
        from .first_article_entitlement import entitled_request
        free = bool(run and entitled_request(mapping(run.run_request)) and outcome.get("state") == "no_charge")
        headline = "We couldn't write this one"
        body = "Your free article is still waiting for you." if free else f"Refunded {outcome.get('points', 0)} points." if outcome.get("state") == "refunded" else "No charge."
        if outcome.get("state") in {None, "pending"}:body = "We're checking the points for this article."
        elif outcome.get("state") in {"charged", "kept"}:body = f"This article used {outcome.get('points') or 0} points."
        label, next_kind = ("Write my first article · free", "write_first_article") if free else ("Try another topic", "write_article")
    if notify and copy_id in {"working", "working_slow", "working_queue", "working_website_update"}:
        body += " " + {"working": "We'll email you when it's done.", "working_website_update": "We'll email you when it's live."}.get(copy_id, "We'll email you when it's ready.")
    return {"stage": "review" if ask == "review_draft" else "setup" if ask in {"approve_setup", "authorize_github", "approve_on_github"} else "done" if copy_id.startswith("done_") or copy_id == "stopped" else "writing",
        "state": "your_turn" if ask else "done" if copy_id.startswith("done_") or copy_id == "stopped" else "working",
        "headline": headline, "body": body, "copyId": copy_id,
        "action": {"id": ask, "label": label, "url": None} if ask else None,
        "next": {"kind": next_kind, "label": label, "runId": run_id, "url": None} if next_kind and label else None,
        "runId": run_id, "supportCode": None}


def run_decision(organization, run, *, config, user, now, queue=None, follow_publication=True):
    """Apply P2 precedence to one run; None leaves the P1 rule in charge."""
    if run is None or not registry_policy._policy_v2_for(organization):
        return None
    result = mapping(run.result)
    publication = optional("content_factory.publish_reviewed_draft", "published_child_for_source", run) if follow_publication else None
    if publication is not None:
        published = mapping(publication.result)
        if mapping(published.get("deploy_v1")).get("url"):
            return {"stage": "done", "state": "done", "headline": "Your article is live!", "body": None,
                "action": None, "next": {"kind": "view_article", "label": "View article", "runId": run.run_id,
                    "url": published["deploy_v1"]["url"]}, "runId": run.run_id, "supportCode": None}
        child = run_decision(organization, publication, config=config, user=user, now=now, follow_publication=False)
        if child is not None:
            child["runId"] = run.run_id
            return child
        return line("working_publishing", run=run)
    code = result.get("error_code") or mapping(result.get("failure")).get("code")
    entry = registry_policy.effective_entry(code, organization)
    channel = mapping(result.get("channel_v1"))
    count = claims_count(run)
    notify = emailable(organization, user)
    outcome = optional("content_factory.no_delivery_refunds", "points_outcome", run)
    copy_id = None
    if run.status == "queued" and queue and queue.get("queuePosition"):
        copy_id = "working_queue"
    elif mapping(result.get("deploy_v1")).get("url"):
        url = mapping(result["deploy_v1"]).get("url")
        return {"stage": "done", "state": "done", "headline": "Your article is live!", "body": None,
            "action": None, "next": {"kind": "view_article", "label": "View article", "runId": run.run_id, "url": url},
            "runId": run.run_id, "supportCode": None}
    elif channel.get("channel") == "copy_ready" and channel.get("waiting_to_publish") and channel.get("dispatch_state") == "accepted":
        # Intent/applied conversion cannot claim a delivered draft.
        from .founder_draft_views import is_delivered_content_only
        if is_delivered_content_only(run):
            exhausted = mapping(mapping(getattr(config, "pillar_strategy", None)).get("self_heal")).get("ladder")
            copy_id = "done_copy_ready_ladder_exhausted" if mapping(exhausted).get("exhausted_at") else "done_copy_ready_strategy"
    if copy_id is None and count:
        copy_id = "ask_check_highlighted"
    if copy_id is None and ready_before_preview(run, organization) and getattr(run, "approval_state", None) != "approved":
        copy_id = "ask_review_draft"
    if copy_id is None and code in {"SITE_TEST_CHANGE_REQUIRED", "BRANCH_PROTECTION_APPROVAL_REQUIRED"}:
        copy_id = "ask_approve_on_github_site_test" if code == "SITE_TEST_CHANGE_REQUIRED" else "ask_approve_on_github_protected"
    if copy_id is None and entry and entry.get("founder_copy_id") == "ask_authorize_github_revoked":
        copy_id = "ask_authorize_github_revoked"
    worker = mapping(mapping(result.get("self_heal_v1")).get("merge_worker"))
    if copy_id is None and (code == "DEPLOYMENT_NOT_DETECTED" or result.get("merge_v1") or worker.get("watch_next_at")):
        copy_id = "working_website_update"
    if copy_id is None and result.get("publish_pr_v1"):
        copy_id = "working_publishing"
    if copy_id is None and (run.status in {"failed", "blocked", "cancelled", "denied"} or code):
        copy_id = registry_policy.copy_id_for(code, organization=organization, terminal=run.status in {"failed", "blocked", "cancelled", "denied"}, self_heal=mapping(result.get("self_heal_v1")))
        if entry is None:
            copy_id = "working"
    if copy_id is None:
        return None
    decision = line(copy_id, run=run, count=count, notify=notify, queue=queue, outcome=outcome,
        expected=mapping(result.get("site_test_v1")).get("expected"))
    if decision["action"] and decision["action"]["id"] == "approve_on_github":
        decision["action"]["url"] = mapping(result.get("site_test_v1")).get("pr_url") or mapping(result.get("publish_pr_v1")).get("pr_url") or result.get("pr_url")
    if copy_id == "working_website_update":
        from .website_deployment import PLAIN_HOST_LINES
        host = worker.get("deploy_provider")
        if worker.get("deploy_alerted") and host in PLAIN_HOST_LINES:
            decision["body"] += "\n" + PLAIN_HOST_LINES[host]
    return decision
