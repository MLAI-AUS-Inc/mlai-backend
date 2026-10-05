"""Allowlisted My startup APIs for revocable Chat account sessions."""
from uuid import UUID

from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated

from founder_tools.my_startup.api import MyStartupAuthentication, MyStartupViewMixin
from community_chat.throttles import CommunityChatScopedThrottle
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


class MyStartupAccess(MyStartupViewMixin):
    """Reject ambiguous scope before calling the existing domain API."""
    authentication_classes = (MyStartupAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_home"
    requires_company = True

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
        except (ValueError, TypeError, DjangoValidationError):
            raise NotFound("Startup not found.")
        if self.company.organization_id and not user_may_use_organization(request.user, self.company.organization):
            raise NotFound("Startup not found.")

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response


class AccountView(MyStartupAccess, CurrentUserView):
    requires_company = False


class BalanceView(MyStartupAccess, CurrentUserBalanceView):
    requires_company = False


class FounderBootstrapView(MyStartupAccess, founder.FounderToolsBootstrapView):
    requires_company = False


class ProfileView(MyStartupAccess, founder.FounderToolsProfileView):
    requires_company = False


class CompaniesView(MyStartupAccess, founder.FounderToolsCompanyView):
    requires_company = False

    def post(self, request):
        if self.company is not None and not (request.data.get("companyId") or request.data.get("company_id")):
            raise ValidationError({"companyId": "Include the selected startup in the save request."})
        if any(key in request.data for key in ("githubRepo", "github_repo", "articleDeliveryMode",
                "article_delivery_mode", "dailyDiscoveryEnabled", "daily_discovery_enabled")):
            raise ValidationError("Manage GitHub in Connections and article preferences in Articles.")
        return super().post(request)


class ActiveCompanyView(MyStartupAccess, founder.FounderToolsActiveCompanyView):
    def post(self, request):
        from founder_tools.profile_fields import visible_companies
        if not visible_companies(VibeRaisingCompany.objects.filter(pk=self.company.pk)).exists():
            raise ValidationError("Save this startup before selecting it.")
        return super().post(request)


class BootstrapView(MyStartupAccess, marketing.VibeMarketingBootstrapView):
    pass


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


class RepositoryView(MyStartupAccess, VibeMarketingGitHubRepositoryView):
    pass


class RepositoriesView(MyStartupAccess, marketing.VibeMarketingGitHubReposView):
    pass


class WebsiteConnectionView(MyStartupAccess, websites.WebsiteConnectionView):
    """Expose the selected company's canonical website binding to Chat sessions."""


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
    pass


class CancelRunView(MyStartupAccess, marketing.VibeMarketingRunControlView):
    def post(self, request, run_id):
        return super().post(request, run_id, "cancel")


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
