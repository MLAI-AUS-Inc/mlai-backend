"""One-sentence approved customer profile for a founder's first article.

Pressing "Write my first article" is the owner's approval of the audience the
founder typed (decision 1 of the founder journey overhaul, 29 Oct 2026). This
module is a thin wrapper over ``editorial_views.mutate_catalog_response``: it
appends one schema v2 audience and activates it in the same owner-locked save,
so the organisation/owner locks, the single catalogue version bump and the
owner approval receipt (``approved_by``/``approved_at``/``approval_history``)
all come from the existing audited path. The approval method is recorded in a
structured log line and in the response, never by rewriting approval_history.

A new audience is always "helpful information only" (``allow_no_offer``): it
cannot yet be in any offer's ``audience_ids``. Binding an approved call to
action to it stays with the catalogue editor.
"""
from __future__ import annotations

import logging
import re

from rest_framework.response import Response

from .editorial_catalog import OFFER_PAGE_CHECKS_KEY, article_brief_for_catalog, catalog_payload, review_payload
from .models import OrganizationContentConfig
from .offer_page_checks import offer_page_check_payload

logger = logging.getLogger(__name__)

APPROVAL_METHOD = "first_article_press"
NO_OFFER_REASON = "No approved call to action yet."
ACCEPTANCE_CRITERIA = ("Answers the reader's question in plain English",)
DEFAULT_COUNTRY = "AU"
DESCRIPTION_MIN, DESCRIPTION_MAX = 3, 280
NAME_MAX = 80
NAME_WORDS = 6

# Free-text startup locations ("Melbourne, Australia", "Auckland NZ") mapped to
# ISO 3166 alpha-2 markets. Ambiguous short words ("us", "act") count only as
# the whole location. Unknown text keeps the Australian default.
_COUNTRY_TERMS = (
    ("AU", ("australia", "new south wales", "victoria", "queensland", "tasmania", "nsw", "vic",
            "qld", "tas", "sydney", "melbourne", "brisbane", "perth", "adelaide", "hobart",
            "canberra", "darwin", "gold coast")),
    ("NZ", ("new zealand", "aotearoa", "nz", "auckland", "wellington", "christchurch")),
    ("GB", ("united kingdom", "uk", "england", "scotland", "wales", "northern ireland", "london", "britain")),
    ("US", ("united states", "usa", "new york", "san francisco", "california")),
    ("CA", ("canada", "toronto", "vancouver", "montreal")),
    ("SG", ("singapore",)),
    ("IN", ("india", "bengaluru", "bangalore", "mumbai", "delhi")),
    ("IE", ("ireland", "dublin")),
)
_WHOLE_LOCATION = {
    "au": "AU", "sa": "AU", "wa": "AU", "nt": "AU", "act": "AU", "nz": "NZ", "uk": "GB", "gb": "GB",
    "us": "US", "ca": "CA", "sg": "SG", "in": "IN", "ie": "IE",
}


def country_from_location(location) -> str:
    """Two upper-case letters for the startup's market, defaulting to AU."""
    words = re.sub(r"[^a-z]+", " ", str(location or "").casefold()).strip()
    if words in _WHOLE_LOCATION:
        return _WHOLE_LOCATION[words]
    text = f" {words} "
    for code, terms in _COUNTRY_TERMS:
        if any(f" {term} " in text for term in terms):
            return code
    return DEFAULT_COUNTRY


def _reader_task(description: str) -> str:
    """Deterministic, non-empty reader task: 'Understand <description>'."""
    text = description.rstrip(" .")
    if text[:1].isupper() and text[1:2].islower():
        text = text[:1].lower() + text[1:]
    return f"Understand {text}"


def _default_name(description: str) -> str:
    return " ".join(description.split()[:NAME_WORDS])[:NAME_MAX].strip()


def _audience_id(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:60].strip("-") or "customer"
    candidate, suffix = base, 2
    while candidate in taken:
        candidate, suffix = f"{base}-{suffix}", suffix + 1
    return candidate


def _parse(data):
    """Return (description, name, expected_version) or an error message."""
    description = data.get("description")
    if not isinstance(description, str) or not DESCRIPTION_MIN <= len(description.strip()) <= DESCRIPTION_MAX:
        return None, "Describe your typical customer in a short sentence."
    description = " ".join(description.split())
    name = data.get("name")
    if name is not None and (not isinstance(name, str) or len(name.strip()) > NAME_MAX):
        return None, "Use a customer profile name of up to 80 characters."
    name = " ".join(name.split()) if isinstance(name, str) and name.strip() else _default_name(description)
    version = data.get("expected_editorial_catalog_version")
    if type(version) is not int or version < 0:
        return None, "Reload your customer profiles and try again."
    return (description, name, version), None


def _strategy(organization):
    config = OrganizationContentConfig.objects.filter(organization=organization).first()
    strategy = config.pillar_strategy if config else {}
    return strategy if isinstance(strategy, dict) else {}


def _startup_name(context) -> str:
    return (str(getattr(context.company, "name", "") or "").strip()
            or str(getattr(context.organization, "name", "") or "").strip()
            or "this startup")


def _brief(strategy, audience, *, country, startup_name):
    """Build an article brief that passes article_brief_for_catalog, or None.

    A no-offer audience gets a 'helpful information only' brief. An audience
    that needs an offer uses the single approved offer covering it in this
    country; with zero or several such offers the owner must choose, so the
    client keeps its existing confirmation flow.
    """
    brief = {
        "audience_id": audience["id"], "audience_version": audience["version"],
        "conversion_intent": "none", "offer_id": None, "offer_version": None,
        "no_offer_reason": NO_OFFER_REASON, "country": country,
        "reader_task": audience["reader_task"],
        "distinct_contribution": f"Practical guidance from {startup_name}",
        "acceptance_criteria": list(ACCEPTANCE_CRITERIA),
    }
    if not audience.get("allow_no_offer"):
        offers = [offer for offer in catalog_payload(strategy)["cta_options"]
                  if offer["status"] == "approved" and audience["id"] in offer["audience_ids"]
                  and country in offer["countries"]]
        if len(offers) != 1:
            return None
        brief.update(conversion_intent="offer", offer_id=offers[0]["id"],
                     offer_version=offers[0]["version"], no_offer_reason=None)
    try:
        return article_brief_for_catalog(strategy, {"editorial_brief": brief})
    except ValueError as exc:
        logger.warning("editorial_first_audience_brief_rejected audience_id=%s error=%s", audience["id"], exc)
        return None


def _payload(strategy, audience, *, country, startup_name, catalog):
    return {
        "catalog": catalog,
        "audience_id": audience["id"],
        "audience_version": audience["version"],
        "approval_method": APPROVAL_METHOD,
        "editorial_brief": _brief(strategy, audience, country=country, startup_name=startup_name),
    }


def _existing_approved(current):
    approved = [item for item in current["audience_options"] if item["status"] == "approved"]
    # Prefer a profile that can already write without an offer.
    return next((item for item in approved if item.get("allow_no_offer")), approved[0] if approved else None)


def first_audience_response(context, owner_user_id, data) -> Response:
    """Save and approve the founder's one-sentence customer profile.

    ``context`` is the caller's ``FounderCompanyContext`` (already scoped to a
    company the user owns). Returns 201 for a newly approved audience, 200 when
    an approved audience already exists (including a replay), or the error
    response of ``mutate_catalog_response`` unchanged.
    """
    from .editorial_views import mutate_catalog_response

    if not isinstance(data, dict):
        return Response({"error": "Expected a JSON object"}, status=400)
    parsed, message = _parse(data)
    if message:
        return Response({"error": message}, status=400)
    description, name, expected_version = parsed
    organization = context.organization
    country = country_from_location(getattr(context.company, "location", ""))
    startup_name = _startup_name(context)
    strategy = _strategy(organization)
    try:
        current = review_payload(strategy)
    except ValueError:
        return Response({"error": "Stored catalog requires administrator repair"}, status=409)

    existing = _existing_approved(current)
    if existing is not None:
        catalog = {**current, OFFER_PAGE_CHECKS_KEY: offer_page_check_payload(strategy)}
        return Response(_payload(strategy, existing, country=country, startup_name=startup_name,
                                 catalog=catalog), status=200)

    audience_id = _audience_id(name, {item["id"] for item in current["audience_options"]})
    option = {
        "catalog_schema_version": 2, "id": audience_id, "name": name, "description": description,
        "reader_task": _reader_task(description), "allow_no_offer": True,
        "status": "draft", "version": 1, "approved_by": None, "approved_at": None,
    }
    response = mutate_catalog_response(
        {"pk": organization.pk},
        {
            "audience_options": [*current["audience_options"], option],
            "expected_editorial_catalog_version": expected_version,
            "activate_entries": [{"kind": "audience", "id": audience_id, "version": 1}],
        },
        owner_context=context, owner_user_id=owner_user_id,
    )
    if response.status_code != 200:
        return response
    audience = next((item for item in response.data["audience_options"] if item["id"] == audience_id), None)
    if audience is None or audience["status"] != "approved":
        logger.error("editorial_first_audience_not_approved org_id=%s audience_id=%s", organization.pk, audience_id)
        return Response({"error": "Your customer profile could not be saved. Please try again."}, status=409)
    actor = f"user:{owner_user_id}"
    logger.info(
        "editorial_first_audience_approved org_id=%s audience_id=%s actor=%s approval_method=%s",
        organization.pk, audience_id, actor, APPROVAL_METHOD,
        extra={"org_id": organization.pk, "audience_id": audience_id, "actor": actor,
               "approval_method": APPROVAL_METHOD},
    )
    return Response(_payload(_strategy(organization), audience, country=country, startup_name=startup_name,
                             catalog=response.data), status=201)
