"""P1.6 (IF-P1-08): a startup with no website still writes copy-ready articles.

A company saved without a domain has the placeholder organisation
``startup-{pk}.invalid``. For the new-journey cohort, while
``MY_STARTUP_NO_WEBSITE_ARTICLES_ENABLED`` is on, that startup:

- gets the real bootstrap (capabilities, ``firstArticle``, ``founderJourney``)
  instead of "Company domain is required.";
- may write portable (copy-ready) articles from its one-line description, and
  is never sent to the repository step;
- is told to add its website, without any GitHub state being minted or any
  topic research being dispatched, when it asks for GitHub or topics.

Every other organisation (legacy, a real website, or any switch off) keeps
today's behaviour: :func:`applies` is the only gate and is false for them.
"""
from __future__ import annotations

from django.conf import settings

FLAG = "MY_STARTUP_NO_WEBSITE_ARTICLES_ENABLED"

# Founder copy (contract section 11.7 and IF-P1-07). P1 refusals carry a
# closed ``reason`` enum, never a failure code.
NO_WEBSITE_GITHUB_REFUSAL = {"detail": "Add your website before connecting GitHub.", "reason": "no_website"}
NO_WEBSITE_TOPICS_REFUSAL = {"detail": "Add your website first.", "reason": "no_website"}


def no_website_articles_enabled() -> bool:
    """True when ``MY_STARTUP_NO_WEBSITE_ARTICLES_ENABLED`` is on (code default off)."""
    return bool(getattr(settings, FLAG, False))


def applies(organization, config=None) -> bool:
    """True for a new-journey startup with no website while the switch is on.

    The switch is read first, so with it off no cohort or database read happens.
    """
    if organization is None or not no_website_articles_enabled():
        return False
    from .founder_journey_cohort import is_new_journey, organization_has_website
    return not organization_has_website(organization) and is_new_journey(organization, config)


def one_line_description(organization) -> str:
    """The startup's saved one-sentence description, whitespace-collapsed ("" when none)."""
    if organization is None or not getattr(organization, "pk", None):
        return ""
    from startup_updates.models import StartupProfile
    value = (StartupProfile.objects.filter(organization_id=organization.pk)
             .values_list("short_description", flat=True).first())
    return " ".join(str(value or "").split())


def apply_no_website_capabilities(capabilities, *, organization, config=None):
    """Return the article capabilities a no-website startup may act on.

    For an organisation where :func:`applies` is false the input is returned
    unchanged (the same object). Otherwise a copy where research and portable
    drafts depend on the one-line description, repository articles are off and
    the next step is never ``"repository"``.
    """
    if not isinstance(capabilities, dict) or not applies(organization, config):
        return capabilities
    described = bool(one_line_description(organization))
    updated = {**capabilities, "canResearch": described, "canGeneratePortableDraft": described,
               "canGenerateArticle": False}
    if updated.get("nextStep") == "repository":
        updated["nextStep"] = "articles"
    return updated


def resolve_no_website_context(request):
    """The founder context when the selected startup is a new-journey one with no website, else None.

    Returns None (the caller keeps its own, unchanged resolution) whenever the
    switch is off, the context cannot be resolved, or the startup has a
    website or is legacy.
    """
    if not no_website_articles_enabled():
        return None
    from .vibe_marketing_views import _resolve_context_or_response
    context, error = _resolve_context_or_response(request, require_domain=False)
    if error is not None or context is None or not applies(context.organization):
        return None
    return context


def _portable_run_of(organization, run_id) -> bool:
    from workflow_runs.models import ContentFactoryRun
    from .portable_drafts import original_portable_run
    run = ContentFactoryRun.objects.filter(run_id=str(run_id)).only("run_id", "organization_id", "run_request").first()
    return bool(run is not None and run.organization_id == organization.pk and original_portable_run(run))


def portable_no_website_context(request, *, run_id=None):
    """The founder context when the domain requirement is waived for this request, else None.

    It is waived only for a new-journey startup with no website
    (:func:`applies`), and only for an explicit, confirmed portable request
    or for an operation on one of its original portable runs. With the switch
    off this returns None before any read, so the caller's own resolution runs
    exactly as today.
    """
    if not no_website_articles_enabled():
        return None
    from .portable_drafts import explicit_portable_request
    data = getattr(request, "data", None)
    portable = explicit_portable_request(data) if hasattr(data, "get") else False
    if not (portable or run_id):
        return None
    context = resolve_no_website_context(request)
    if context is not None and (portable or _portable_run_of(context.organization, run_id)):
        return context
    return None


def resolve_portable_context_or_response(request, *, run_id=None):
    """``(context, error)`` for an owner operation that may run without a website.

    :func:`portable_no_website_context` decides the waiver. Everything else
    resolves exactly as ``_resolve_context_or_response`` does today, with the
    same error responses.
    """
    context = portable_no_website_context(request, run_id=run_id)
    if context is not None:
        return context, None
    from .vibe_marketing_views import _resolve_context_or_response
    return _resolve_context_or_response(request)
