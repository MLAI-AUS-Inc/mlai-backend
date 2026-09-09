"""Typed editorial inputs shared by API and durable run state.

Catalog entries are drafts until explicitly approved. Validation is structural;
an approval record is not proof of source checking or commercial fulfilment.
"""
from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def destination(value: str) -> str:
    value = value.strip()
    if not value or any(ord(c) < 33 for c in value) or "\\" in value:
        raise ValueError("CTA destination must be a nonempty safe URL")
    parts = urlsplit(value)
    if value.startswith("/") and not value.startswith("//"):
        return value
    if parts.scheme in {"http", "https"} and parts.hostname and not parts.username and not parts.password:
        return value
    raise ValueError("CTA destination must be a site-relative path or HTTP(S) URL, not a fragment or executable URL")


class CTAOptionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    button_text: str = Field(min_length=1)
    button_href: str
    audience: str = "general"  # Retained legacy display label; not an approved ICP.
    use_when: str = ""
    cta_component: str = "ArticleCompanyCTA"
    image_url: str | None = None
    secondary_button_text: str | None = None
    secondary_button_href: str | None = None
    status: Literal["draft", "approved", "retired"] = "draft"
    audience_ids: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    version: int = Field(default=1, ge=1)
    approved_by: str | None = None
    approved_at: str | None = None

    _destination = field_validator("button_href")(destination)

    @field_validator("secondary_button_href")
    @classmethod
    def secondary_destination(cls, value: str | None) -> str | None:
        return destination(value) if value is not None else None


def normalize_cta_options(value: Any) -> list[CTAOptionModel]:
    # Empty legacy run snapshots remain loadable. Nonempty dicts must not be
    # silently reinterpreted as one chosen offer or discarded.
    if value is None or value == {}:
        return []
    if not isinstance(value, list):
        raise ValueError("cta_options must be a list of CTA records; migrate legacy nonempty dictionary configuration")
    result = [CTAOptionModel.model_validate(item) for item in value]
    ids = [item.id for item in result]
    if len(ids) != len(set(ids)):
        raise ValueError("CTA ids must be unique")
    return result


class AudienceOption(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1)
    reader_task: str = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    status: Literal["draft", "approved", "retired"] = "draft"
    version: int = Field(default=1, ge=1)
    approved_by: str | None = None
    approved_at: str | None = None
    allow_no_offer: bool = False


class ArticleEditorialBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    audience_id: str = Field(min_length=1)
    audience_version: int = Field(ge=1)
    conversion_intent: Literal["offer", "none"] = "offer"
    offer_id: str | None = Field(default=None, min_length=1)
    offer_version: int | None = Field(default=None, ge=1)
    no_offer_reason: str | None = Field(default=None, min_length=1)
    country: str = Field(pattern=r"^[A-Z]{2}$")
    reader_task: str = Field(min_length=1)
    distinct_contribution: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def coherent_offer_decision(self):
        if self.conversion_intent == "none":
            if self.offer_id is not None or self.offer_version is not None:
                raise ValueError("No-offer briefs must not select an offer or version")
            if not self.no_offer_reason:
                raise ValueError("No-offer briefs require an explicit editorial reason")
        elif self.offer_id is None or self.offer_version is None or self.no_offer_reason is not None:
            raise ValueError("Offer briefs require an offer/version and no no-offer reason")
        if self.audience_id == "OUTSIDE" and self.conversion_intent != "none":
            raise ValueError("OUTSIDE articles require an explicit no-offer decision")
        return self

    @field_validator("acceptance_criteria")
    @classmethod
    def nonempty_criteria(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("Acceptance criteria cannot be blank")
        return [item.strip() for item in value]


def resolve_editorial_brief(
    brief: ArticleEditorialBrief,
    audiences: list[AudienceOption],
    offers: list[CTAOptionModel],
) -> tuple[AudienceOption, CTAOptionModel | None]:
    """Fail closed on missing, ambiguous, stale or incompatible decisions."""
    # Revalidate snapshots/copies too; model_copy(update=...) bypasses validation.
    brief = ArticleEditorialBrief.model_validate(brief.model_dump())
    matched_audiences = [a for a in audiences if a.id == brief.audience_id]
    if len(matched_audiences) != 1:
        raise ValueError("Editorial brief needs one known audience")
    audience = matched_audiences[0]
    if audience.status != "approved" or not audience.approved_by or not audience.approved_at:
        raise ValueError("Audience requires explicit approval provenance")
    if audience.version != brief.audience_version:
        raise ValueError("Editorial brief refers to stale catalog versions")
    if brief.conversion_intent == "none":
        if not audience.allow_no_offer:
            raise ValueError("Audience is not approved for no-offer articles")
        return audience, None
    matched_offers = [o for o in offers if o.id == brief.offer_id]
    if len(matched_offers) != 1:
        raise ValueError("Editorial brief needs one known offer")
    offer = matched_offers[0]
    if offer.status != "approved" or not offer.approved_by or not offer.approved_at:
        raise ValueError("Audience and offer require explicit approval provenance")
    if (audience.version, offer.version) != (brief.audience_version, brief.offer_version):
        raise ValueError("Editorial brief refers to stale catalog versions")
    if audience.id not in offer.audience_ids:
        raise ValueError("Offer is not approved for the selected audience")
    if brief.country not in offer.countries:
        raise ValueError("Offer is not approved for the selected country")
    return audience, offer


def approved_offer_payload(
    brief: ArticleEditorialBrief | None,
    audiences: list[AudienceOption],
    offers: list[CTAOptionModel],
) -> dict[str, Any] | None:
    """Serialize a selected offer without inventing one for explicit no-offer briefs."""
    if brief is None:
        if audiences or offers:
            raise ValueError("Configured catalog requires an explicit approved editorial brief")
        return None
    _, offer = resolve_editorial_brief(brief, audiences, offers)
    return offer.model_dump(mode="json") if offer is not None else None


def editorial_delivery_metadata(
    brief: ArticleEditorialBrief | None,
    audiences: list[AudienceOption],
    offers: list[CTAOptionModel],
    cta: Any,
) -> dict[str, Any]:
    """Preserve the decision and reject CTA drift at an export boundary."""
    offer = approved_offer_payload(brief, audiences, offers)
    if brief is None:
        return {}
    actual = cta.model_dump(mode="json") if hasattr(cta, "model_dump") else cta
    if offer is None:
        if actual is not None:
            raise ValueError("No-offer delivery must not contain a CTA")
    elif not isinstance(actual, dict) or any(
        actual.get(key) != offer[key] for key in ("title", "body", "button_text", "button_href")
    ):
        raise ValueError("Delivery CTA does not match the approved offer")
    return {"editorial_brief": brief.model_dump(mode="json")}
