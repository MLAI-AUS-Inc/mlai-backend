"""Allowlisted My startup APIs for revocable Chat account sessions."""
from uuid import UUID

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated

from founder_tools.my_startup.api import MyStartupAuthentication, MyStartupViewMixin
from content_factory.website_metrics import WebsiteMetricsView as ContentWebsiteMetricsView
from content_factory.company_billing import CompanyBillingView as ContentCompanyBillingView
from content_factory.article_review_views import VibeMarketingArticleReviewView
from content_factory.article_preview_lease import ArticlePreviewLeaseView
from content_factory import vibe_marketing_views as marketing
from content_factory import notification_channel_views as notifications
from content_factory import website_views as websites
from content_factory.github_repository_views import VibeMarketingGitHubRepositoryView
from core.views import CurrentUserView
from founder_tools import views as founder
from founder_tools.models import VibeRaisingCompany
from founder_tools.services import user_may_use_organization
from integrations.api_views_connectors import ConnectorSourcesStatusView
from roo.views import CurrentUserBalanceView


# Exact facade identities, never inherited attributes or the legacy adapters.
MEMBER_ACCESS = {
    ('community_chat.my_startup_views', 'CompaniesView'): {'GET': 'read', 'HEAD': 'read'},
    ('community_chat.my_startup_journey_views', 'FounderJourneyView'): {'GET': 'read', 'HEAD': 'read'},
    ('community_chat.my_startup_views', 'RunView'): {'GET': 'read', 'HEAD': 'read'},
    ('community_chat.my_startup_views', 'ArticleReviewView'): {'GET': 'read', 'HEAD': 'read'},
    ('community_chat.my_startup_views', 'ArticlePreviewView'): {'GET': 'read', 'HEAD': 'read', 'POST': 'read'},
    ('community_chat.my_startup_views', 'ArticleCommentsView'): {'GET': 'read', 'HEAD': 'read', 'POST': 'write_articles'},
    ('community_chat.my_startup_views', 'ArticleView'): {'GET': 'read', 'HEAD': 'read', 'POST': 'write_articles'},
    ('community_chat.my_startup_views', 'ArticleRunActionView'): {'POST': 'publish'},
    ('content_factory.publish_reviewed_draft_views', 'PublishReviewedDraftView'): {'POST': 'publish'},
    ('founder_tools.startup_team_views', 'TeamView'): {'GET': 'read', 'HEAD': 'read'},
    ('founder_tools.startup_team_views', 'TeamMemberView'): {'PATCH': 'read', 'DELETE': 'read'},
}


class MyStartupAccess(MyStartupViewMixin):
    """Reject ambiguous scope before calling the existing domain API."""
    authentication_classes = (MyStartupAuthentication,)
    permission_classes = (IsAuthenticated,)
    requires_company = True

    def dispatch(self, request, *args, **kwargs):
        self._team_context_token = None
        try:
            return super().dispatch(request, *args, **kwargs)
        finally:
            if self._team_context_token is not None:
                from founder_tools.startup_team import reset_member_context
                reset_member_context(self._team_context_token)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        values = [source.get(key) for source in (request.query_params, request.data)
                  for key in ("company_id", "companyId") if source.get(key)]
        if kwargs.get("company_id"):
            values.append(kwargs["company_id"])
        try:
            ids = {str(UUID(str(value))) for value in values}
        except (ValueError, TypeError, AttributeError):
            raise ValidationError({"companyId": "Choose a valid startup."})
        if len(ids) > 1:
            raise ValidationError({"companyId": "Request startup selections do not match."})
        create_new = request.data.get("createNew", request.data.get("create_new")) in (True, "true", "1")
        if create_new and ids:
            raise ValidationError({"companyId": "New startup requests must not include an existing startup."})
        self.company = None
        if not ids:
            if self.requires_company and not (getattr(self, "allows_creation", False) and create_new):
                raise ValidationError({"companyId": "Choose a startup."})
            return
        try:
            self.company = get_object_or_404(VibeRaisingCompany.objects.select_related("organization"),
                pk=next(iter(ids)), profile__user=request.user)
        except (ValueError, TypeError, DjangoValidationError, Http404):
            from founder_tools import startup_team as teams
            identity = (type(self).__module__, type(self).__name__)
            need = MEMBER_ACCESS.get(identity, {}).get(request.method)
            if not teams.enabled() or need is None:
                raise NotFound('Startup not found.') from None
            if identity == ('community_chat.my_startup_views', 'ArticleRunActionView') and kwargs.get('action') not in {'approve', 'publish-pr'}:
                raise NotFound('Startup not found.') from None
            context = teams.member_context(request.user, next(iter(ids)), need='read')
            if context is None:
                raise NotFound('Startup not found.') from None
            if kwargs.get('run_id'):
                from workflow_runs.models import ContentFactoryRun
                run = ContentFactoryRun.objects.filter(run_id=kwargs['run_id'], organization=context.organization).first()
                if run is None:
                    raise NotFound('Startup not found.') from None
                kind = teams.mapping(teams.mapping(run.result).get('publish_pr_v1')).get('kind')
                if run.workflow not in teams.ARTICLE_WORKFLOWS or kind in {'setup', 'scaffold', 'proxy'}:
                    if identity[1] == 'ArticleRunActionView':
                        raise PermissionDenied({'detail': 'Only the startup owner can do this.', 'reason': 'owner_only'})
                    raise NotFound('Startup not found.') from None
            context = teams.member_context(request.user, next(iter(ids)), need=need)
            if context is None:
                raise NotFound('Startup not found.') from None
            self.company, self.startup_context = context.company, context
            self._team_context_token = teams.bind_member_context(context, need=need)
            return
        if self.company.organization_id and not user_may_use_organization(request.user, self.company.organization):
            raise NotFound("Startup not found.")

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response

    def handle_exception(self, exc):
        from founder_tools.services import CompanyDomainClaimedError
        if isinstance(exc, CompanyDomainClaimedError):
            from founder_tools.startup_team import issue_join_reference
            nonce = issue_join_reference(user=self.request.user, organization=exc.organization)
            response = super().handle_exception(exc)
            if nonce:
                response.data = {**response.data, 'joinReference': nonce}
            return response
        return super().handle_exception(exc)


class AccountView(MyStartupAccess, CurrentUserView):
    startup_read_bucket = "bootstrap"
    requires_company = False


class BalanceView(MyStartupAccess, CurrentUserBalanceView):
    startup_read_bucket = "bootstrap"
    requires_company = False


class FounderBootstrapView(MyStartupAccess, founder.FounderToolsBootstrapView):
    startup_read_bucket = "bootstrap"
    requires_company = False


class ProfileView(MyStartupAccess, founder.FounderToolsProfileView):
    startup_read_bucket = "bootstrap"
    requires_company = False

    def get(self, request):
        response = super().get(request)
        from founder_tools.startup_team import enabled
        if enabled() and response.status_code == 200 and isinstance(response.data, dict):
            response.data = {**response.data, 'teamAvailable': True}
        return response


class CompaniesView(MyStartupAccess, founder.FounderToolsCompanyView):
    requires_company = False

    def get(self, request):
        response = super().get(request)
        from founder_tools.startup_team import member_companies
        if response.status_code == 200:
            members = member_companies(request.user)
            if members and isinstance(response.data, dict):
                response.data = {**response.data, 'companies': [*response.data.get('companies', []), *members]}
            elif members and isinstance(response.data, list):
                response.data = [*response.data, *members]
        return response

    def post(self, request):
        if self.company is not None and not (request.data.get("companyId") or request.data.get("company_id")):
            raise ValidationError({"companyId": "Include the selected startup in the save request."})
        if any(key in request.data for key in ("githubRepo", "github_repo", "articleDeliveryMode",
                "article_delivery_mode", "dailyDiscoveryEnabled", "daily_discovery_enabled")):
            raise ValidationError("Manage GitHub in Connections and article preferences in Articles.")
        response = super().post(request)
        if response.status_code == 409 and isinstance(response.data, dict) and response.data.get('code') == 'company_domain_claimed':
            from founder_tools.services import claimed_organization
            from founder_tools.startup_team import issue_join_reference
            from founder_tools.one_field_start import one_field_create_requested, prepare_body
            body = prepare_body(request.data)[0] if one_field_create_requested(request.data) else request.data
            organization = claimed_organization((body or {}).get('domain'))
            nonce = issue_join_reference(user=request.user, organization=organization)
            if nonce:
                response.data = {**response.data, 'joinReference': nonce}
        return response


class ActiveCompanyView(MyStartupAccess, founder.FounderToolsActiveCompanyView):
    def post(self, request):
        from founder_tools.profile_fields import visible_companies
        if not visible_companies(VibeRaisingCompany.objects.filter(pk=self.company.pk)).exists():
            raise ValidationError("Save this startup before selecting it.")
        return super().post(request)


class BootstrapView(MyStartupAccess, marketing.VibeMarketingBootstrapView):
    startup_read_bucket = "bootstrap"


class SettingsView(MyStartupAccess, marketing.VibeMarketingSettingsView):
    def put(self, request):
        if "githubRepo" in request.data or "github_repo" in request.data:
            raise ValidationError({"githubRepo": "Select the repository in Connections."})
        return super().put(request)


class ResearchView(MyStartupAccess, marketing.VibeMarketingAutofillView):
    allows_creation = True

    def post(self, request):
        if not marketing._request_flag(request, "draft_only", "draftOnly"):
            raise ValidationError({"draftOnly": "Research must return suggestions for review."})
        return super().post(request)


class AvatarView(MyStartupAccess, marketing.VibeMarketingCompanyAvatarView):
    pass


class AuthorPhotoPreviewView(MyStartupAccess, marketing.VibeMarketingAuthorPhotoPreviewView):
    pass


class RepositoryView(MyStartupAccess, VibeMarketingGitHubRepositoryView):
    pass


class RepositoriesView(MyStartupAccess, marketing.VibeMarketingGitHubReposView):
    pass


class WebsiteConnectionView(MyStartupAccess, websites.WebsiteConnectionView):
    """Expose the selected company's canonical website binding to Chat sessions."""


class WebsiteCostUsageView(MyStartupAccess, websites.WebsiteCostUsageView):
    """Reuse explicit-company authorization for lazy Articles accounting reads."""


class WebsiteConnectionActionView(MyStartupAccess, websites.WebsiteConnectionActionView):
    """Reuse website lifecycle consent and durable cancellation for Chat owners."""


class GitHubConnectView(MyStartupAccess, marketing.VibeMarketingGitHubConnectView):
    def post(self, request):
        if "githubRepo" in request.data or "github_repo" in request.data:
            raise ValidationError({"githubRepo": "Select the repository after connecting GitHub."})
        response = super().post(request)
        if response.status_code == 200 and response.data.get("status") == "auth_required":
            # Native clients open a one-use handoff, never raw OAuth without
            # the Chat session/company binding and safe return destination.
            from .startups.connections import ConnectView
            handoff = ConnectView()
            handoff.company = self.company
            authorization = handoff.post(request, "github")
            response.data = {**response.data, "auth_url": authorization.data["authorizationUrl"],
                             "authorizationUrl": authorization.data["authorizationUrl"]}
        return response


class LocationsView(MyStartupAccess, marketing.VibeMarketingLocationLookupView):
    requires_company = False


class AbnsView(MyStartupAccess, marketing.VibeMarketingAbnLookupView):
    requires_company = False


class RunView(MyStartupAccess, marketing.VibeMarketingRunView):
    startup_read_bucket = "poll"


class CancelRunView(MyStartupAccess, marketing.VibeMarketingRunControlView):
    def post(self, request, run_id):
        return super().post(request, run_id=run_id, action="cancel")


class NotificationChannelsView(MyStartupAccess, notifications.VibeMarketingNotificationChannelsView):
    pass


class NotificationChannelView(MyStartupAccess, notifications.VibeMarketingNotificationChannelDetailView):
    pass


class VerifyNotificationChannelView(MyStartupAccess, notifications.VibeMarketingNotificationChannelVerifyView):
    pass


class ResendNotificationChannelView(MyStartupAccess, notifications.VibeMarketingNotificationChannelResendView):
    pass


class NotificationDeliveryView(MyStartupAccess, notifications.VibeMarketingNotificationChannelDeliveryView):
    pass


class AutomationStatusView(MyStartupAccess, notifications.VibeMarketingResearchAutomationView):
    # Changes use the settings API's prerequisite and price checks.
    http_method_names = ("get", "head", "options")


class LearnedRulesView(MyStartupAccess, marketing.VibeMarketingLearnedRulesView):
    pass


class LearnedRuleView(MyStartupAccess, marketing.VibeMarketingLearnedRuleDetailView):
    pass


class SourcesStatusView(MyStartupAccess, ConnectorSourcesStatusView):
    def get(self, request):
        # Reuse the Connections projection, including website account status and
        # per-startup Pulse defaults. This is one allowlisted read operation.
        from .startups.views import SourcesView
        delegate = SourcesView()
        delegate.company = self.company
        return delegate.get(request)


class ArticleReviewView(MyStartupAccess, VibeMarketingArticleReviewView):
    """Use the same explicit startup selection for preview review updates."""



class ArticlePreviewView(MyStartupAccess, ArticlePreviewLeaseView):
    """Acquire a short-lived preview grant for the explicitly selected startup."""



class ArticleCommentsView(MyStartupAccess, marketing.VibeMarketingRunCommentsView):
    """Create and read persisted comments through the existing review controller."""



class ArticleCommentView(MyStartupAccess, marketing.VibeMarketingRunCommentDetailView):
    """Update or delete a draft comment within the selected startup."""


class CompanyBillingView(MyStartupAccess, ContentCompanyBillingView):
    """Permit a selected founder to opt in their own billing account."""


class ArticleView(MyStartupAccess, marketing.VibeMarketingArticleView):
    """Start a paid draft within the selected startup."""


class ArticleRunActionView(MyStartupAccess, marketing.VibeMarketingRunControlView):
    """Only drafting and reviewed publication controls are exposed to Chat."""
    def post(self, request, run_id, action):
        if action not in {"approve", "publish-pr", "resume", "restart", "deny"}:
            from rest_framework.response import Response
            return Response({"detail": "Unknown article action."}, status=404)
        return super().post(request, run_id, action)


class WebsiteMetricsView(MyStartupAccess, ContentWebsiteMetricsView):
    """Read bounded article outcomes only for the selected startup."""
    startup_read_bucket = "read"


class WebsiteConnectionOperationView(MyStartupAccess, websites.WebsiteConnectionOperationView):
    """Read a single persisted operation under the selected company's consent."""
    startup_read_bucket = "poll"
