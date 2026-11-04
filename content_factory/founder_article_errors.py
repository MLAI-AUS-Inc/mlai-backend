"""Server-owned article error copy without changing legacy response envelopes."""
from uuid import UUID

EXPLICIT_SURFACES = {
    "community_chat.my_startup_journey_views": frozenset({"FounderJourneyView", "FirstArticleCardView",
        "FirstArticleStartView", "FirstArticleSwitchTopicView"}),
    "community_chat.my_startup_views": frozenset({"RunView", "CancelRunView", "ArticleReviewView",
        "ArticlePreviewView", "ArticleCommentsView", "ArticleCommentView", "ArticleView", "ArticleRunActionView",
        "WebsiteConnectionView", "WebsiteConnectionActionView", "WebsiteConnectionOperationView", "GitHubConnectView"}),
    "content_factory.publish_reviewed_draft_views": frozenset({"PublishReviewedDraftView"}),
}
ADAPTED_SURFACES = frozenset({"FounderDraftCopiedView", "FounderDraftPostedLinkView", "FounderDraftDownloadView",
    "VibeMarketingArticleReviewView", "VibeMarketingArticleView", "VibeMarketingRunView", "VibeMarketingRunControlView",
    "VibeMarketingRunCommentsView", "VibeMarketingRunCommentDetailView", "VibeMarketingRunCommentsSubmitView",
    "VibeMarketingRunCommentsAcceptRevisionView", "VibeMarketingRunLivePreviewView", "ArticlePreviewLeaseView"})
REASON_COPY = {
    "owner_only": ("Check your startup account", "Only the startup owner can complete this action.", None),
    "not_owner": ("Check your startup account", "Only the startup owner can complete this action.", None),
    "invalid_topic": ("Choose an article topic", "Choose a suggested topic or describe the article you want.", "Choose a topic"),
    "invalid_audience": ("Check your audience", "Describe who this article is for, then try again.", "Review audience"),
    "revision_changed": ("Your draft changed", "Review the current draft before trying this action again.", "Review draft"),
    "not_ready": ("Your article is still being prepared", "Your saved progress will update when the draft is ready.", "View progress"),
    "invalid_key": ("This request needs a new save attempt", "Your saved work is still here. Try the action again.", "Try again"),
    "in_progress": ("Working on it", "Your original request is still being confirmed.", "View progress"),
    "not_available": ("This step isn't available right now", "Refresh your startup details, then try again.", "Try again"),
    "invalid_profile": ("Check your startup details", "Add your startup name and a short description, then try again.", "Edit details"),
    "operation_conflict": ("Check the saved request", "This request already records different details. Review the current details before continuing.", "Review details"),
    "daily_cap": ("Articles are busy today", "Please try starting this article again tomorrow.", None),
}
COMMAND_COPY = {
    "roo_points_quote_changed": ("Check the updated price", "The price changed before your article started. Refresh the details before continuing.", "Refresh details"),
    "run_execution_closed": ("This article request is closed", "Start a new article to continue.", "Write an article"),
    "run_stop_requested": ("Confirming your stop request", "Your saved progress is still available while the run stops.", "View progress"),
    "run_execution_owned": ("Follow your saved run", "Review the saved run's current progress before continuing.", "View progress"),
    "review_source_changed": ("The saved review changed", "Review the current draft before publishing.", "Review draft"),
    "review_source_unavailable": ("The review could not be checked", "Your saved progress is still available. Try again shortly.", "Try again"),
}
CLASS_COPY = {
    "transient": ("Please try again shortly", "We couldn't connect. Your saved progress is still available.", "Try again"),
    "waiting": ("Working on it", "The saved run is waiting for its next update.", "View progress"),
    "needs_user": ("Your turn", "Review the saved article before continuing.", "Review article"),
    "unsupported": ("Publishing needs a check", "Your saved progress is still available. You can continue with a draft.", "View progress"),
}
GENERIC_COPY = ("Check your saved progress", "We couldn't complete this request. Review your saved progress before trying again.", "View progress")


def article_error_envelope(body):
    """Use registered codes and a closed server catalog, never remote detail text."""
    from .website_failure_contract import load_registry
    registry = load_registry()
    known = {**registry.get("by_code", {}), **registry.get("by_code_v2", {})}
    failure = body.get("failure") if isinstance(body.get("failure"), dict) else {}
    candidate = body.get("code") or body.get("error_code") or failure.get("code")
    code = candidate if isinstance(candidate, str) and (candidate in known or candidate in COMMAND_COPY) else "UNREGISTERED_FAILURE_CODE"
    entry = known.get(code, {})
    reason = body.get("reason")
    selected = COMMAND_COPY.get(code) or (REASON_COPY.get(reason) if isinstance(reason, str) else None)
    if code == "INSUFFICIENT_ROO_POINTS":
        selected = ("Add points to continue", "Your article hasn't started yet.", "Get points")
    headline, text, button = selected or CLASS_COPY.get(entry.get("failure_class"), GENERIC_COPY)
    return {"contractVersion": 2, "failure": {"code": code,
        "copy": {"headline": headline, "body": text, "button": button}}}


def decorate_article_error(view, request, response):
    """Add a scoped v2 error beside frozen old fields on reviewed article APIs."""
    identity = type(view)
    explicit = identity.__name__ in EXPLICIT_SURFACES.get(identity.__module__, ())
    adapted = (identity.__module__ == "founder_tools.my_startup.api" and identity.__name__.startswith("MyStartup")
        and identity.__name__[9:] in ADAPTED_SURFACES)
    body = getattr(response, "data", None)
    if (not explicit and not adapted or not 400 <= response.status_code < 600 or not isinstance(body, dict)
            or not getattr(request.user, "is_authenticated", False) or not getattr(request.user, "is_active", False)):
        return response
    values = [source.get(key) for source in (request.query_params, request.data)
        for key in ("companyId", "company_id") if source.get(key)]
    try:
        identifiers = {str(UUID(str(value))) for value in values}
    except (ValueError, TypeError, AttributeError):
        return response
    company = getattr(view, "company", None)
    if company is not None:
        if identifiers and identifiers != {str(company.pk)}:
            return response
    elif len(identifiers) == 1:
        from founder_tools.models import VibeRaisingCompany
        company = VibeRaisingCompany.objects.select_related("organization").filter(
            pk=next(iter(identifiers)), profile__user=request.user).first()
    if company is None or company.organization_id is None:
        return response
    from .founder_journey_cohort import is_new_journey
    from .p2_foundation import p2_applies
    if is_new_journey(company.organization) or p2_applies(company.organization):
        response.data = {**body, "articleError": article_error_envelope(body)}
    return response
