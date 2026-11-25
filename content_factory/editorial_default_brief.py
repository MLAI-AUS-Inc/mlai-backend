"""One server function that gives every article start a valid brief (plan B19).

An article start either names an approved editorial brief, or the organisation
is catalogue-free (legacy, no editorial policy at all). Before this module a
start with no brief for an organisation with a catalogue failed with a bare
"Configured catalog requires an explicit approved editorial brief" 400. The
founder saw a code string, not a question.

``default_start_brief`` resolves that one way for every start path:

* **legacy** - no ``editorial_catalog`` key: no brief, the legacy path. It never
  creates a catalogue for the organisation: that would flip it to "configured"
  and every other brief-less start would then fail with
  ``editorial_brief_required``.
* **brief** - the catalogue has an approved customer profile that can write
  without an offer, or one approved offer for that profile and market: a brief
  built from that approved entry only.
* **ask** - otherwise exactly one founder question with a registered code and a
  canonical action, never a raw code string.

It only ever *selects* an entry that is already approved; it never approves a
draft. That keeps the owner-only activation guard (``editorial_views``) intact
on paths with no owner present (delayed dispatch, staff console, scheduler and
service starts).

Call sites of ``vibe_marketing_views._refresh_article_editorial_payload``
(classified for B19; the brief either comes from the founder's request or from
a stored run, never invented there):

1. ``_revision_editorial_payload_from_run`` - owner present; the source run's
   brief; accepted unchanged when only superseded (B9).
2. ``_charge_roo_points_for_article`` - owner present; the request's brief, which
   ``start_article_generation`` defaults through this module first.
3. ``_restart_article_run`` (owner Restart) - owner present; the stored brief,
   upgraded to the newer approved version by A8.
4. ``_queue_content_factory_run_authorized`` - owner present or not; the payload
   is already resolved by the caller, so the exact rule applies.
5. ``_call_content_factory_component_revision`` - owner present; the source
   run's brief; accepted unchanged when only superseded (B9).
6. ``VibeMarketingRunCommentsSubmitView.post`` (revision billing) - owner
   present; the source run's brief; accepted unchanged when only superseded.
7. ``dispatch_outbox._deliver`` (delayed dispatch) - no owner; the escrowed
   request; upgraded to the newer approved version only when the request is
   not bound to a backend admission (approved entries only, never a draft).
8. ``staff_console._start_copy_ready`` (two calls) - no owner; the stored brief
   through the Restart rules; staff never approve an entry.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from .editorial_catalog import CATALOG_KEY, article_brief_for_catalog, catalog_payload

BRIEF_KEYS = ("editorial_brief", "editorialBrief")
ASK_CODE = "editorial_brief_required"
ASK_ACTION = "describe_customer"
NO_APPROVED_PROFILE = ("Describe your customer and approve the profile, then we can write this article "
                       "for them.")
CHOOSE_NEXT_STEP = ("Choose the next step readers should take for this customer, or allow helpful "
                    "information only, then start the article again.")
CATALOG_UNREADABLE = ("We couldn't read your customer profiles. Open them and save them again, then start "
                      "the article.")


@dataclass(frozen=True)
class StartBrief:
    """The outcome for one start: ``legacy``, ``brief`` or ``ask``."""

    state: str
    brief: dict | None = None
    detail: str = ""
    audience_id: str | None = None
    extra: dict = field(default_factory=dict)

    def ask_payload(self) -> dict:
        """One founder question: a registered code, plain copy and a canonical action."""
        return {"code": ASK_CODE, "detail": self.detail, "field": "editorialBrief",
                "action": {"id": ASK_ACTION}, "needsUser": True, **self.extra}


def _candidates(approved, preferred_audience_id):
    preferred = [item for item in approved if item["id"] == preferred_audience_id]
    rest = [item for item in approved if item["id"] != preferred_audience_id]
    # Prefer a profile that can already write without an offer (as the first article does).
    return preferred + sorted(rest, key=lambda item: not item.get("allow_no_offer"))


def sparse_proposal(start, *, config, context):
    """Offer a draft audience for a configured, pristine catalogue without admitting it."""
    from .content_first_article import infer_audience, profile_facts
    from .editorial_first_audience import _reader_task
    strategy = getattr(config, "pillar_strategy", None)
    if start.state != "ask" or start.detail != NO_APPROVED_PROFILE or not isinstance(strategy, dict):
        return start
    catalog = strategy.get(CATALOG_KEY)
    if (not isinstance(catalog, dict) or catalog.get("approval_history")
            or any((catalog.get("approval_receipts") or {}).values())
            or catalog.get("audience_options") or catalog.get("cta_options")):
        return start
    facts = profile_facts(context.company, context.organization, config)
    audience = facts["targetAudience"] or infer_audience(facts["oneLiner"])
    if not audience:
        return start
    return replace(start, extra={"suggestedAudience": audience,
        "proposedDraft": {"readerTask": _reader_task(audience), "conversionIntent": "none",
                          "requiresAudienceApproval": True}})


def default_start_brief(strategy, *, country, startup_name, preferred_audience_id=None) -> StartBrief:
    """Return the brief a start without one should use, or the single question to ask.

    Pure: reads only ``strategy`` and never writes, approves or creates entries.
    """
    if strategy is not None and not isinstance(strategy, dict):
        return StartBrief("ask", detail=CATALOG_UNREADABLE)
    if CATALOG_KEY not in (strategy or {}):
        return StartBrief("legacy")
    try:
        current = catalog_payload(strategy)
    except ValueError:
        return StartBrief("ask", detail=CATALOG_UNREADABLE)
    approved = [item for item in current["audience_options"] if item["status"] == "approved"]
    if not approved:
        return StartBrief("ask", detail=NO_APPROVED_PROFILE)
    from .editorial_first_audience import _brief
    for audience in _candidates(approved, preferred_audience_id):
        brief = _brief(strategy, audience, country=country, startup_name=startup_name)
        if brief is not None:
            return StartBrief("brief", brief=brief, audience_id=audience["id"])
    return StartBrief("ask", detail=CHOOSE_NEXT_STEP, audience_id=approved[0]["id"])


def startup_country_and_name(context) -> tuple[str, str]:
    """The startup's market and display name for a default brief, as the first article uses."""
    from .editorial_first_audience import country_from_location
    company = getattr(context, "company", None)
    organization = getattr(context, "organization", None)
    name = (str(getattr(company, "name", "") or "").strip() or str(getattr(organization, "name", "") or "").strip()
            or "this startup")
    return country_from_location(getattr(company, "location", "")), name


def has_brief(data) -> bool:
    """Whether the request names a brief (an explicit null still counts: it is refused, not defaulted)."""
    return hasattr(data, "keys") and any(key in data for key in BRIEF_KEYS)


def start_brief(strategy, data, *, country, startup_name) -> StartBrief:
    """The brief for one start: the request's own (validated) or the server default.

    A brief the request names is validated exactly as before; a ValueError keeps
    the caller's existing refusal. Only a request with no brief at all is
    defaulted, so a founder's explicit choice is never replaced.
    """
    if has_brief(data):
        return StartBrief("brief", brief=article_brief_for_catalog(strategy, data))
    preferred = data.get("audience_id") or data.get("audienceId") if hasattr(data, "get") else None
    return default_start_brief(strategy, country=country, startup_name=startup_name,
                               preferred_audience_id=preferred if isinstance(preferred, str) else None)
