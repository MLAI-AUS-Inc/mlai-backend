"""Narrow Chat-session facade; shared founder views own domain mutations."""
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import F
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from community_chat.authentication import CommunityChatAccountAuthentication
from community_chat.throttles import CommunityChatScopedThrottle
from founder_tools.models import VibeRaisingCompany
from founder_tools.serializers import FounderProfileSerializer
from founder_tools.services import get_or_create_founder_profile
from founder_tools.views import FounderToolsCompanyView, FounderToolsActiveCompanyView
from integrations.api_views_connectors import ConnectorSourcesStatusView
from integrations.services.external_connectors import ConnectorConfigurationError
from startup_updates.models import MonthlyUpdateDraft
from vibe_raising import views as founder
from workflow_runs.models import ContentFactoryRun
from .presentation import update_payload
from .lifecycle import delete_update, source_capabilities, validate_generation_sources
from .source_preferences import ACTIVITY_WINDOW_DAYS, source_preferences


def enabled():
    return bool(getattr(settings, "COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED", False))


class ChatStartupAccess:
    """Accept only revocable Chat sessions and require explicit company ownership."""
    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_home"
    requires_company = True

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not enabled():
            raise NotFound("Startup updates are not enabled yet.")
        self.company = None
        if self.requires_company:
            raw = founder._company_id_from_request(request)
            if not raw:
                raise ValidationError({"companyId": "Choose a startup."})
            try:
                self.company = get_object_or_404(
                    VibeRaisingCompany.objects.select_related("organization"),
                    pk=raw, profile__user=request.user,
                )
            except (ValueError, TypeError, DjangoValidationError):
                raise NotFound("Startup not found.")
            run_id = kwargs.get("run_id")
            if run_id:
                get_object_or_404(ContentFactoryRun, run_id=run_id,
                    workflow=founder.STARTUP_UPDATE_WORKFLOW,
                    domain=self.company.organization.domain if self.company.organization else "")

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response


class BootstrapView(ChatStartupAccess, APIView):
    requires_company = False

    def get(self, request):
        profile = get_or_create_founder_profile(request.user)
        return Response({
            "enabled": True, "accountId": str(request.user.community_chat_profile_id),
            "relayUrl": settings.COMMUNITY_CHAT_RELAY_URL,
            "profile": FounderProfileSerializer(profile).data,
            "capabilities": {"manual": True, "generation": True, "community": True,
                "delete": True, "draftReadyPush": False},
        })


class CompaniesView(ChatStartupAccess, FounderToolsCompanyView):
    requires_company = False


class ActiveCompanyView(ChatStartupAccess, FounderToolsActiveCompanyView):
    pass


class UpdatesView(ChatStartupAccess, founder.VibeRaisingMonthlyUpdateView):
    def get(self, request):
        drafts = MonthlyUpdateDraft.objects.filter(organization=self.company.organization).select_related(
            "organization", "current_revision__snapshot", "published_revision__snapshot"
        ).order_by("-month", "-id")
        try:
            offset = max(0, int(request.query_params.get("offset", 0)))
        except (ValueError, TypeError, DjangoValidationError):
            raise ValidationError({"offset": "Use a nonnegative integer."})
        rows = list(drafts[offset:offset + 51])
        return Response({"updates": [update_payload(row) for row in rows[:50]],
            "nextOffset": offset + 50 if len(rows) > 50 else None})

    def post(self, request):
        # Every save is a draft. Completion/rewards occur on exact-version approval.
        if request.data.get("saveMode") != "draft":
            raise ValidationError({"saveMode": "Save a draft before reviewing it."})
        return super().post(request)


class UpdateView(ChatStartupAccess, APIView):
    def delete(self, request, update_id):
        delete_update(organization=self.company.organization, update_id=update_id,
            revision_id=request.data.get("revisionId"), revision_hash=request.data.get("revisionHash"))
        return Response({"deleted": True, "updateId": update_id})

    def get(self, request, update_id):
        draft = get_object_or_404(MonthlyUpdateDraft.objects.select_related(
            "organization", "current_revision__snapshot", "published_revision__snapshot"
        ), pk=update_id, organization=self.company.organization)
        published = request.query_params.get("version") == "published"
        if published and not draft.published_revision_id:
            raise NotFound("No approved version exists yet.")
        return Response({"update": update_payload(draft, published=published),
            "communityPreview": update_payload(draft, published=published, community=True)})


class PublishView(ChatStartupAccess, founder.VibeRaisingMonthlyUpdatePublishView):
    def post(self, request, update_id):
        if request.data.get("reviewed") is not True:
            raise ValidationError({"reviewed": "Confirm you reviewed the saved update and audience."})
        draft = get_object_or_404(MonthlyUpdateDraft, pk=update_id, organization=self.company.organization)
        if not draft.current_revision or draft.current_revision.validation.get("legacy_unverified"):
            raise ValidationError("Save and review a new revision before approving this update.")
        return super().post(request, update_id)


class CommunityView(ChatStartupAccess, APIView):
    requires_company = False

    def get(self, request):
        try:
            offset = max(0, int(request.query_params.get("offset", 0)))
        except (TypeError, ValueError):
            raise ValidationError({"offset": "Use a nonnegative integer."})
        # Approval must still refer to the same hash and audience as the publication.
        rows = MonthlyUpdateDraft.objects.filter(
            published_revision__audience="community",
            published_revision__approval__audience_visibility=["community"],
            published_revision__approval__content_hash=F("published_revision__content_hash"),
        ).select_related("organization", "published_revision__snapshot").order_by("-published_at", "-id")
        page = list(rows[offset:offset + 51])
        return Response({"updates": [update_payload(row, published=True, community=True) for row in page[:50]],
            "nextOffset": offset + 50 if len(page) > 50 else None})


class SettingsView(ChatStartupAccess, founder.VibeRaisingBusinessHealthView):
    pass


class SourcesView(ChatStartupAccess, ConnectorSourcesStatusView):
    def get(self, request):
        response = super().get(request)
        if response.status_code == 200:
            preferences = source_preferences(self.company)
            sources = [source_capabilities(source, preferences=preferences) for source in response.data.get("sources", [])]
            response.data = {**response.data, "sources": sources, "connections": sources}
        return response


class GenerateView(ChatStartupAccess, founder.VibeRaisingEmailDraftStartView):
    activity_window_days = ACTIVITY_WINDOW_DAYS

    def post(self, request):
        validate_generation_sources(request.data)
        try:
            return super().post(request)
        except ConnectorConfigurationError as exc:
            raise ValidationError({"sources": str(exc)}) from exc


class ActiveRunView(ChatStartupAccess, founder.VibeRaisingEmailDraftActiveRunView):
    def get(self, request):
        response = super().get(request)
        # DRF renders Response(None) as an empty body. Chat's query client
        # requires a defined JSON value, including when no run is active.
        if response.status_code == 200 and response.data is None:
            response.data = {"run": None}
        return response


class RunView(ChatStartupAccess, founder.VibeRaisingEmailDraftStatusView):
    pass


class ResultsView(ChatStartupAccess, founder.VibeRaisingEmailDraftResultsView):
    pass


class CancelView(ChatStartupAccess, founder.VibeRaisingEmailDraftCancelView):
    pass


class DocumentsView(ChatStartupAccess, founder.VibeRaisingManualDocumentListView):
    pass


class UploadSessionView(ChatStartupAccess, founder.VibeRaisingManualDocumentUploadSessionView):
    pass


class UploadCompleteView(ChatStartupAccess, founder.VibeRaisingManualDocumentUploadCompleteView):
    pass
