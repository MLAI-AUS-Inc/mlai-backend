"""Canonical founder article presentation from backend-verified local facts.

This is a read projection, not command authority. Every mutation continues to
recheck its own current actor, source, review, budget and idempotent intent.
No worker-supplied error text or provider request enters this contract.
"""
from copy import deepcopy
from functools import lru_cache
import json
import math
from pathlib import Path
import re

STEPS = ("website", "first_article", "writing", "review", "setup", "publishing", "done")
STEP_COPY = {
    "website": ("Your website", "You can connect your website later.", None),
    "first_article": ("Your first article", "Choose the article you want to write.", None),
    "writing": ("Writing your article", "Your saved run keeps its progress.", None),
    "review": ("Review your draft", "Check the draft before copying or publishing it.", None),
    "setup": ("Connect your website", "Adding your blog to a website is optional.", None),
    "publishing": ("Publish your article", "You choose when and where to publish.", None),
    "done": ("Your article", "Your delivered draft stays available in the app.", None),
}


def _mapping(value):
    return value if isinstance(value, dict) else {}


def effective_approval_state(run):
    """Read exact current parent consent without inventing lifecycle writes."""
    from .website_run_authority import event_owned
    if event_owned(run):
        from .article_publish_approval import effective_approval_state as current_approval
        return current_approval(run)
    return getattr(run, "approval_state", "")


def _copy(headline, body=None, button=None):
    return {"headline": headline, "body": body, "button": button}


@lru_cache(maxsize=1)
def _schema():
    return json.loads((Path(__file__).resolve().parent.parent / "contracts/founder-article-v2.schema.json").read_text())


class FounderContractMismatch(ValueError):
    """A declared public field drifted; the diagnostic never includes its value."""


def _validate(value, rule, definitions, path="$", depth=0):
    # The backend intentionally supports the closed schema vocabulary consumed
    # by this boundary's generator, with no optional runtime dependency.
    supported = {"$schema", "$id", "$defs", "$ref", "title", "version", "type", "const", "enum",
        "anyOf", "additionalProperties", "properties", "required", "minimum", "maximum", "minLength",
        "maxLength", "pattern", "minItems", "maxItems", "items", "prefixItems", "x-wire-name", "x-wire-project"}
    def fail():
        raise FounderContractMismatch(f"Founder contract mismatch at {path}")
    if rule is True:
        return
    if rule is False or not isinstance(rule, dict) or depth > 40 or set(rule) - supported:
        fail()
    if "$ref" in rule:
        reference = rule["$ref"]
        if not reference.startswith("#/$defs/") or reference[8:] not in definitions:
            fail()
        _validate(value, definitions[reference[8:]], definitions, path, depth + 1)
    if "anyOf" in rule:
        for option in rule["anyOf"]:
            try:
                _validate(value, option, definitions, path, depth + 1)
                break
            except FounderContractMismatch:
                continue
        else:
            fail()
    matches = lambda candidate: type(value) is type(candidate) and value == candidate
    if "const" in rule and not matches(rule["const"]):
        fail()
    if "enum" in rule and not any(matches(candidate) for candidate in rule["enum"]):
        fail()
    if "type" in rule:
        expected = {"object": (dict,), "array": (list,), "string": (str,), "integer": (int,),
            "number": (int, float), "boolean": (bool,), "null": (type(None),)}
        if rule["type"] not in expected or type(value) not in expected[rule["type"]]:
            fail()
    if type(value) is dict:
        properties = rule.get("properties", {})
        if any(type(key) is not str for key in value) or not set(rule.get("required", ())) <= value.keys() or (rule.get("additionalProperties") is False and value.keys() - properties.keys()):
            fail()
        for key in value.keys() & properties.keys():
            _validate(value[key], properties[key], definitions, f"{path}.{key}", depth + 1)
        if isinstance(rule.get("additionalProperties"), dict):
            for key in value.keys() - properties.keys():
                _validate(value[key], rule["additionalProperties"], definitions, f"{path}.*", depth + 1)
    if type(value) is list:
        if not rule.get("minItems", 0) <= len(value) <= rule.get("maxItems", len(value)):
            fail()
        prefixes = rule.get("prefixItems", ())
        for index, item in enumerate(value):
            subrule = prefixes[index] if index < len(prefixes) else rule.get("items")
            if subrule is not None:
                _validate(item, subrule, definitions, f"{path}[{index}]", depth + 1)
    if type(value) is str:
        if not rule.get("minLength", 0) <= len(value) <= rule.get("maxLength", len(value)) or ("pattern" in rule and re.search(rule["pattern"], value) is None):
            fail()
    if type(value) in (int, float):
        if not math.isfinite(value) or not rule.get("minimum", value) <= value <= rule.get("maximum", value):
            fail()


def validate_journey(payload):
    """Fail loudly if a new projection drifts from its generated client contract."""
    schema = _schema()
    _validate(payload, schema, schema["$defs"])
    return payload


def _rights(organization, company, config, user):
    from founder_tools import startup_team
    from founder_tools.services import organization_owner_user_id
    if not startup_team._valid_user(user):
        return False, False, False, False
    owner = organization_owner_user_id(organization)
    strategy = _mapping(getattr(config, "pillar_strategy", None))
    roster = startup_team._roster(config)
    invalid = startup_team.KEY in strategy and roster is None
    frozen = invalid or bool(roster and roster.get("frozen"))
    if owner == user.pk:
        return True, not frozen, not frozen, not frozen
    rights = startup_team._rights(user, organization, roster) if startup_team.enabled() else frozenset()
    return "read" in rights, "write_articles" in rights, "publish" in rights, False


def enrich_journey(payload, *, organization, company, config, user, run, connection, setup_run):
    """Add the closed v2 article object to an already scoped new journey.

    Historical cohorts return before this function. Repository verification
    never gates a portable first draft. Capability booleans are conservative
    presentation hints; an accepted command revalidates its current authority.
    """
    from . import founder_journey as journey
    from .founder_draft_views import is_delivered_article
    from .hosted_articles import hosted_capabilities
    from .article_publish_approval import article_review_identity_is_complete
    from .website_failure_contract import load_registry

    payload = deepcopy(payload)
    from integrations.services.github_callback_reasons import REASONS
    returned = _mapping(payload.get("githubReturn"))
    if returned:
        reason = "connected" if returned.get("reason") == "installed" else returned.get("reason")
        payload["githubReturn"] = {**returned, "reason": reason} if reason in REASONS else None
    readable, writable, publishable, may_upgrade = _rights(organization, company, config, user)
    result = _mapping(getattr(run, "result", None))
    ready = bool(run is not None and is_delivered_article(run))
    approved = bool(run is not None and effective_approval_state(run) == "approved")
    copied = bool(run is not None and journey.copied_or_posted(config, run.run_id))
    hosted = hosted_capabilities(config)
    mode = journey.delivery_mode(run) if run is not None else "content_only"
    delivery = ("hosted" if mode == "hosted" or hosted.get("canPublishHostedArticle") is True
        else "contentOnly" if mode in {"", "content_only"} else "website")
    stage, state = payload["stage"], payload["state"]
    button = _mapping(payload.get("action") or payload.get("next")).get("label")
    current_copy = _copy(payload["headline"], payload.get("body"), button)
    code = result.get("error_code") or _mapping(result.get("failure")).get("code")
    failed = bool(run is not None and run.status in {"failed", "blocked", "denied", "cancelled"}
        and not journey.is_owner_cancel(run) and not ready)
    failure = None
    if failed:
        registry = load_registry()
        known = {**registry.get("by_code", {}), **registry.get("by_code_v2", {})}
        failure = {"code": code if isinstance(code, str) and code in known else "UNREGISTERED_FAILURE_CODE",
            "copy": deepcopy(current_copy)}
    website = _mapping(payload.get("website"))
    live = bool(stage == "done" and _mapping(payload.get("next")).get("kind") == "view_article")
    setup_done = website.get("publishAutomatically") == "on"
    statuses = {
        "website": "done" if website.get("hasWebsite") else "optional",
        "first_article": "done" if run is not None else "pending",
        "writing": "done" if ready else "pending",
        "review": "done" if copied or approved else "pending",
        "setup": "done" if setup_done else "pending" if delivery == "website" else "optional",
        "publishing": "done" if live else "pending" if delivery != "contentOnly" else "optional",
        "done": "done" if live or copied else "pending",
    }
    if stage in statuses and statuses[stage] != "done":
        statuses[stage] = "blocked" if failed or state == "your_turn" else "working" if state == "working" else "pending"
    steps = [{"id": key, "state": statuses[key],
        "copy": deepcopy(current_copy) if key == stage else _copy(*STEP_COPY[key])} for key in STEPS]
    offer = _mapping(payload.get("publishOffer"))
    quality_ready = bool(run is not None and article_review_identity_is_complete(run))
    from .website_run_authority import event_owned
    if run is not None and event_owned(run):
        from .run_review_quality import verified_review_quality
        quality_ready = verified_review_quality(run)
    payload.update(contractVersion=2, capabilities={
        "canWriteArticle": bool(writable and _mapping(payload.get("next")).get("kind") in {"write_first_article", "write_article"}),
        "canCopyDraft": bool(readable and ready),
        "canPublish": bool(publishable and ready and quality_ready and offer.get("runId") == getattr(run, "run_id", None)),
        "canUpgradeWebsite": bool(may_upgrade and website.get("hasWebsite") and not setup_done),
    }, deliveryMode=delivery, steps=steps, failure=failure)
    payload["article"] = None
    if run is not None:
        progress = []
        for index, step in enumerate(run.steps.all().order_by("display_order", "pk")[:100]):
            identifier = step.step_key if re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", step.step_key) else f"step-{index + 1}"
            progress.append({"id": identifier, "status": step.status,
                "copy": _copy("Article step complete" if step.status == "completed" else
                    "Article step needs attention" if step.status == "failed" else "Preparing your article")})
        payload["article"] = {"runId": run.run_id, "status": run.status,
            "approvalState": effective_approval_state(run), "currentStep": run.current_step or None,
            "deliveryMode": delivery, "resumeAvailable": _resume_available(run, user),
            "pollAfterMs": payload["pollAfterMs"], "progress": progress,
            "failure": deepcopy(failure), "capabilities": deepcopy(payload["capabilities"])}
    payload.setdefault("holdingReason", None)
    return payload


def _resume_available(run, user):
    from .website_run_authority import event_owned
    if event_owned(run):
        from .run_user_resume import can_request_user_resume
        return can_request_user_resume(run, user=user)
    return bool(run.resume_available)
