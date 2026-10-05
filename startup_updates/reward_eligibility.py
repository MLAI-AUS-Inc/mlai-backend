"""Forgiving, server-owned identity checks for the full Startup Pulse reward."""

from copy import copy
from difflib import SequenceMatcher
import hashlib
import json
import re
import unicodedata

from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist

from startup_updates.reward_website import website_evidence
from vibe_raising.registration import verify_and_persist_company_registration


_LEGAL_WORDS = {"pty", "proprietary", "ltd", "limited", "inc", "incorporated", "the"}
_GENERAL_WORDS = set("a an and are as at be by for from has have in into is it its of on or our that the their this to using we with your you startup company business platform solution solutions help helps provide provides service services technology innovative australian australia build builds building create creates creating product products make makes made welcome home about contact".split())


def _words(value):
    plain = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z0-9]+", plain)


def _name(value):
    return " ".join(word for word in _words(value) if word not in _LEGAL_WORDS)


def _names_match(left, right):
    a, b = _name(left), _name(right)
    if not a or not b:
        return False
    if a.replace(" ", "") == b.replace(" ", ""):
        return True
    # Permit a small spelling difference, without accepting a shared generic word.
    return min(len(a), len(b)) >= 6 and SequenceMatcher(None, a, b).ratio() >= 0.85


def _mentions_name(text, name):
    haystack, needle = " " + _name(text) + " ", _name(name)
    return bool(needle and f" {needle} " in haystack)


def _activity_terms(value):
    # Light suffix handling keeps "payments" / "payment" from becoming a mismatch.
    return {word[:-1] if word.endswith("s") else word for word in _words(value)
            if len(word) >= 4 and word not in _GENERAL_WORDS}


def _description(company):
    try:
        profile = company.organization.startup_profile
    except (AttributeError, ObjectDoesNotExist):
        return ""
    return " ".join(str(getattr(profile, key, "") or "") for key in (
        "short_description", "problem_solved", "target_audience",
    ))


def startup_reward_eligibility(company):
    """Check active ABR registration, company identity and public website activity.

    Missing optional profile details never block approval. Missing or conflicting
    evidence only selects the standard reward; raw register/website data stays out
    of the client response. Cache keys include every input used in the decision.
    """
    if company is None:
        return {"eligible": False, "reason": "registration_missing"}
    description = _description(company)
    inputs = {key: str(getattr(company, key, "") or "") for key in ("name", "domain", "abn", "acn")}
    inputs["description"] = description
    key = "startup-pulse-eligibility:v1:" + hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    try:
        cached = cache.get(key)
    except Exception:
        cached = None
    if isinstance(cached, dict) and isinstance(cached.get("eligible"), bool):
        return cached
    result = _check(company, inputs, description)
    try:
        cache.set(key, result, timeout=6 * 60 * 60 if result["eligible"] else 60)
    except Exception:
        pass  # A cache outage must not prevent an approved update earning points.
    return result


def _check(company, inputs, description):
    if not inputs["abn"].strip() and not inputs["acn"].strip():
        return {"eligible": False, "reason": "registration_missing"}
    try:
        registration = verify_and_persist_company_registration(
            copy(company), abn=inputs["abn"], acn=inputs["acn"], save=False,
        )
    except Exception:
        return {"eligible": False, "reason": "registration_unconfirmed"}
    names = registration.get("names") or []
    if not inputs["domain"].strip():
        return {"eligible": False, "reason": "website_missing"}
    try:
        text = website_evidence(inputs["domain"])
    except Exception:
        return {"eligible": False, "reason": "website_unconfirmed"}
    registry_matches = any(_names_match(inputs["name"], name) for name in names)
    website_has_legal_name = any(_mentions_name(text, name) for name in names)
    website_has_brand = _mentions_name(text, inputs["name"]) or (registry_matches and website_has_legal_name)
    # A trading brand can differ from its legal entity. A legal name or ABN on
    # that same branded website provides the link without demanding exact names.
    website_has_registration = website_has_legal_name or bool(
        re.search(r"(?<!\d)" + r"[\s-]*".join(registration["abn"]) + r"(?!\d)", text)
    )
    if not website_has_brand or not (registry_matches or website_has_registration):
        return {"eligible": False, "reason": "identity_unconfirmed"}
    # The same brand appears in both texts even when their activities conflict.
    # It is identity evidence, never evidence that the claimed work matches.
    identity_terms = _activity_terms(" ".join([inputs["name"], *names]))
    expected = _activity_terms(description) - identity_terms
    observed = _activity_terms(text) - identity_terms
    if expected and not expected.intersection(observed):
        return {"eligible": False, "reason": "activity_unconfirmed"}
    return {"eligible": True, "reason": "verified", "abn": registration["abn"]}
