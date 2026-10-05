"""Build reviewable research requests without committing the user's profile draft."""
from copy import deepcopy
import hashlib
import json

from founder_tools.profile_fields import validate_profile_fields
from rest_framework.exceptions import ValidationError


_DRAFT_FIELDS = {
    "companyName": "company_name", "brandName": "brand_name",
    "shortDescription": "short_description", "problemSolved": "problem_solved",
    "targetAudience": "target_audience", "founderNames": "founder_names",
    "founderProfiles": "founder_profiles", "stage": "stage",
    "organizationKind": "organization_kind", "hasRevenue": "has_revenue",
    "notes": "notes", "companyContext": "company_context",
    "companyLinkedInUrl": "company_linkedin_url", "competitors": "competitors",
    "seedKeywords": "seed_keywords", "location": "location", "abn": "abn",
    "defaultTimezone": "default_timezone",
}


def research_draft_values(data):
    """Normalize native and desktop draft envelopes without accepting control fields."""
    existing = data.get("existingFields", data.get("existing_fields", {}))
    if not isinstance(existing, dict):
        raise ValidationError({"existingFields": "Use an object of draft profile fields."})
    profile = existing.get("profileFields", existing.get("profile_fields", {}))
    if not isinstance(profile, dict):
        raise ValidationError({"profileFields": "Use an object of draft profile fields."})
    result = {}
    # Flat native draft values and nested desktop values use the same allowlist.
    # Explicit top-level values win, including their snake_case aliases and blanks.
    for layer in (existing, profile, data):
        for key, alias in _DRAFT_FIELDS.items():
            if key in layer or alias in layer:
                result[key] = deepcopy(layer[key] if key in layer else layer[alias])
    return validate_profile_fields(result)


def research_draft_payload(payload, data):
    """Overlay explicitly submitted draft values onto a saved research snapshot."""
    data = research_draft_values(data)
    result = deepcopy(payload)
    fields = dict(result.get("existing_fields") or {})
    profile_fields = dict(fields.get("profileFields") or {})
    profile = dict(result.get("startup_profile") or {})
    pairs = {
        "shortDescription": "short_description", "problemSolved": "problem_solved",
        "targetAudience": "target_audience", "founderNames": "founder_names",
        "stage": "stage", "organizationKind": "organization_kind",
        "hasRevenue": "has_revenue", "notes": "notes",
        "founderProfiles": "founder_profiles",
    }
    for key, alias in pairs.items():
        if key in data or alias in data:
            value = deepcopy(data[key] if key in data else data[alias])
            profile_fields[key] = value
            profile[alias] = value
    for key, alias in {
        "companyContext": "company_context", "companyLinkedInUrl": "company_linkedin_url",
        "competitors": "competitors", "seedKeywords": "seed_keywords",
        "brandName": "brand_name", "defaultTimezone": "default_timezone",
    }.items():
        if key in data or alias in data:
            fields[key] = deepcopy(data[key] if key in data else data[alias])
    for key in ("location", "abn"):
        if key in data:
            profile_fields[key] = data[key]
            result[key] = data[key]
    name = data.get("companyName", data.get("company_name"))
    if name is not None:
        result["company_name"] = name
    if "brandName" in data:
        result["brand_name"] = data["brandName"]
    result["company_linkedin_url"] = fields.get("companyLinkedInUrl", "")
    fields["profileFields"] = profile_fields
    result.update({"existing_fields": fields, "startup_profile": profile, "draft_mode": True,
                   "draft_only": True, "persist": False})
    result["draft_fingerprint"] = hashlib.sha256(json.dumps({key: result.get(key) for key in
        ("domain", "company_name", "existing_fields", "startup_profile")},
        sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return result
