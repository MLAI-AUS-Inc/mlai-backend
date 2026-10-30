"""P1 founder journey endpoints on the My startup facade (IF-P1-03, IF-P1-05, IF-P1-09).

Every view is scoped by :class:`MyStartupAccess` (an owned company only, else
404) and answers 404 while its flag is off, so a flags-off deploy changes
nothing. A legacy organisation gets the legacy journey body and 404 from the
first-article card. Error bodies carry ``detail`` and ``reason`` only.
"""
from django.conf import settings
from django.http import HttpResponseNotModified
from rest_framework.response import Response
from rest_framework.views import APIView

from content_factory import content_first_article as first_articles
from content_factory import founder_journey as journeys
from content_factory import founder_journey_cohort as cohorts

from .my_startup_views import MyStartupAccess

NOT_FOUND = {"detail": "Not found."}
OWNER_ONLY = {"detail": "Only the startup owner can do this.", "reason": "owner_only"}
NOT_AVAILABLE = {"detail": "This isn't available for your startup yet.", "reason": "not_available"}


def _not_found():
    return Response(NOT_FOUND, status=404)


def _is_owner(user, organization) -> bool:
    from founder_tools.services import organization_owner_user_id
    return organization is not None and organization_owner_user_id(organization) == getattr(user, "pk", None)


class FounderJourneyView(MyStartupAccess, APIView):
    """GET the server-owned founder journey, with an ETag and 304 on a match."""

    startup_read_bucket = "poll"

    def get(self, request):
        if not journeys.api_enabled():
            return _not_found()
        company = self.company
        payload = journeys.project_founder_journey(company.organization, user=request.user, company=company)
        etag = payload["etag"]
        if journeys.matches_if_none_match(request.headers.get("If-None-Match"), etag):
            response = HttpResponseNotModified()
        else:
            response = Response(payload)
        response["ETag"] = f'"{etag}"'
        return response


class FounderJourneyOptInView(MyStartupAccess, APIView):
    """POST: an owner moves a legacy startup to the new journey (IF-P1-05)."""

    def post(self, request):
        if not getattr(settings, "MY_STARTUP_FOUNDER_JOURNEY_OPT_IN_ENABLED", False):
            return _not_found()
        organization = self.company.organization
        # MyStartupAccess already hides startups the caller does not own; this
        # keeps the owner rule explicit should that scoping ever widen.
        if not _is_owner(request.user, organization):
            return Response(OWNER_ONLY, status=403)
        try:
            payload = cohorts.opt_in(organization, request.user)
        except cohorts.FounderJourneyOptInUnavailable:
            return Response(NOT_AVAILABLE, status=409)
        return Response({"founderJourney": payload})


class _FirstArticleAccess(MyStartupAccess, APIView):
    def _context(self):
        from founder_tools.services import FounderCompanyContext
        company = self.company
        return FounderCompanyContext(profile=company.profile, company=company, organization=company.organization)

    def _applies(self):
        return first_articles.applies(self.company.organization)

    @staticmethod
    def _refused(refusal):
        return Response(refusal.body(), status=refusal.status)


class FirstArticleCardView(_FirstArticleAccess):
    """GET the "Your first article" card. Never dispatches work."""

    startup_read_bucket = "read"

    def get(self, request):
        if not self._applies():
            return _not_found()
        return Response(first_articles.card_payload(self._context(), request.user))


class FirstArticleStartView(_FirstArticleAccess):
    """POST the one press: save edits, approve the audience, start a copy-ready article."""

    def post(self, request):
        if not self._applies():
            return _not_found()
        try:
            status, body = first_articles.start_first_article(self._context(), request.user, request.data)
        except first_articles.FirstArticleRefusal as refusal:
            return self._refused(refusal)
        return Response(body, status=status)


class FirstArticleSwitchTopicView(_FirstArticleAccess):
    """POST: replace a slow free first-article run with a new topic (attempt 2)."""

    def post(self, request):
        if not self._applies():
            return _not_found()
        try:
            status, body = first_articles.switch_topic(self._context(), request.user, request.data)
        except first_articles.FirstArticleRefusal as refusal:
            return self._refused(refusal)
        return Response(body, status=status)
