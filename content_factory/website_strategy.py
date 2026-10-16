"""Scoped, monotonic Articles strategy selection; no dispatch or owner writes."""
from copy import deepcopy
from django.conf import settings
from . import website_goal as goals

RUNGS = ("adopt", "native_collection", "deterministic", "model_adapter", "agent", "hosted")
KNOWN_FRAMEWORKS = {"nextjs", "next", "astro", "gatsby", "vite", "react_router", "react-router", "hugo", "jekyll", "eleventy"}


def enabled():
    """Strategy rollout is separate from goal execution and status display."""
    return getattr(settings, "ARTICLES_WEBSITE_STRATEGY_ENABLED", False)


def binding(goal, source):
    """Bind a pin to exact saved authority and selected source."""
    return goals.digest({"scope": goal["scope"], "source_sha": source})


def initial_rung(journey):
    """Use explicit inventory facts; absence of evidence cannot certify adoption."""
    discovery = goals.mapping(journey.get("discovery"))
    resolution = goals.mapping(discovery.get("resolution"))
    if resolution.get("existence_state") == "confirmed":
        return 0
    if goals.mapping(resolution.get("content_source")) or discovery.get("contentRoot"):
        return 1
    framework = goals.mapping(journey.get("repository")).get("framework")
    return 2 if isinstance(framework, str) and framework.casefold() in KNOWN_FRAMEWORKS else 3


def valid_pin(pin):
    """Reject future versions and malformed pin identifiers rather than guessing."""
    return (isinstance(pin, dict) and type(pin.get("version")) is int and pin["version"] == 1
        and pin.get("rung") in RUNGS and type(pin.get("revision")) is int and pin["revision"] > 0
        and isinstance(pin.get("binding"), str) and goals.DIGEST.fullmatch(pin["binding"])
        and isinstance(pin.get("fingerprint_digest"), str) and goals.DIGEST.fullmatch(pin["fingerprint_digest"]))


def set_pin(goal, index, *, source, fingerprint, now, reason):
    """Record every strategy change and never silently reduce its rung."""
    result = deepcopy(goal)
    previous = result.get("strategy_pin")
    if previous is not None and not valid_pin(previous):
        raise ValueError("Saved strategy pin is malformed")
    if previous and index < RUNGS.index(previous["rung"]):
        raise ValueError("A strategy cannot silently move backwards")
    revision = previous["revision"] + 1 if previous else 1
    pin = {"version": 1, "rung": RUNGS[index], "revision": revision, "binding": binding(goal, source), "fingerprint_digest": fingerprint}
    if not valid_pin(pin):
        raise ValueError("Strategy needs current inventory proof")
    result["strategy_pin"] = pin
    history = list(result.get("strategy_history") or [])[-15:]
    history.append({"from": previous["rung"] if previous else None, "to": pin["rung"], "revision": revision,
        "at": now.isoformat(), "reason": reason, "binding": pin["binding"]})
    result["strategy_history"] = history
    result["revision"] += 1
    return result


def ensure_pin(goal, journey, *, source, now):
    """Reuse an exact pin or explicitly rebind it at the same or higher rung."""
    if not enabled():
        return goal
    understood = next(item for item in goal["milestones"] if item["name"] == "understood")
    if understood["state"] != "passed":
        return goal
    fingerprint = goals.mapping(understood["proof"]).get("evidence_digest")
    previous = goal.get("strategy_pin")
    if previous and not valid_pin(previous):
        raise ValueError("Saved strategy pin is malformed")
    if previous and previous["binding"] == binding(goal, source) and previous["fingerprint_digest"] == fingerprint:
        return goal
    index = max(initial_rung(journey), RUNGS.index(previous["rung"]) if previous else 0)
    return set_pin(goal, index, source=source, fingerprint=fingerprint, now=now, reason="source_changed" if previous else "inventory_selected")


def advance(goal, *, source, now, reason):
    """One fresh causal strategy outcome moves one rung, up to hosted fallback."""
    pin = goal.get("strategy_pin")
    if not valid_pin(pin) or pin["binding"] != binding(goal, source):
        raise ValueError("Strategy failure does not match the current source pin")
    index = RUNGS.index(pin["rung"])
    if index == len(RUNGS) - 1:
        return goal
    return set_pin(goal, index + 1, source=source, fingerprint=pin["fingerprint_digest"], now=now, reason=reason)


def request_pin(goal):
    """Only a server-reserved goal supplies a setup strategy request."""
    pin = goals.mapping(goal).get("strategy_pin")
    if not pin:
        return {}
    if not valid_pin(pin) or pin["rung"] == "hosted":
        raise ValueError("This strategy is not a repository setup rung")
    return {"strategy_pin": pin["rung"], "strategy_goal_id": goal["goal_id"], "strategy_revision": pin["revision"], "strategy_binding": pin["binding"]}


def valid_exhaustion(goal, observation):
    """Accept exhausted-rung evidence only for the current source-bound pin."""
    pin = goal.get("strategy_pin")
    return (valid_pin(pin) and isinstance(observation, dict)
        and type(observation.get("version")) is int and observation["version"] == 1
        and observation.get("rung") == pin["rung"] and observation.get("goal_id") == goal["goal_id"]
        and type(observation.get("revision")) is int and observation["revision"] == pin["revision"]
        and observation.get("binding") == pin["binding"]
        and isinstance(observation.get("evidence_digest"), str) and goals.DIGEST.fullmatch(observation["evidence_digest"]))
