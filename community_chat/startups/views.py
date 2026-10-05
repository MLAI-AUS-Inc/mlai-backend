"""Narrow Chat-session facade; shared founder views own domain mutations."""
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404
from django.urls import reverse
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from community_chat.authentication import CommunityChatAccountAuthentication
from community_chat.throttles import StartupThrottleMixin
from founder_tools.models import VibeRaisingCompany
from founder_tools.serializers import FounderProfileSerializer
from founder_tools.services import get_or_create_founder_profile
from founder_tools.views import FounderToolsCompanyView, FounderToolsActiveCompanyView
from integrations.api_views_connectors import ConnectorSourcesStatusView
from integrations.services.external_connectors import ConnectorConfigurationError
from startup_updates.models import MonthlyUpdateDraft
from startup_updates.monthly_groups import requested_month
from vibe_raising import views as founder
from workflow_runs.models import ContentFactoryRun
from .presentation import update_payload
from .publication import approved_updates
from .lifecycle import delete_update, source_capabilities, validate_generation_sources
from .source_preferences import source_preferences


def enabled():
    return bool(getattr(settings, "COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED", False))


class ChatStartupAccess(StartupThrottleMixin):
    """Accept only revocable Chat sessions and require explicit company ownership."""
    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = (IsAuthenticated,)
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
    startup_read_bucket = "bootstrap"
    requires_company = False

    def get(self, request):
        profile = get_or_create_founder_profile(request.user)
        return Response({
            "enabled": True, "accountId": str(request.user.community_chat_profile_id),
            "relayUrl": settings.COMMUNITY_CHAT_RELAY_URL,
            "profile": FounderProfileSerializer(profile).data,
            "capabilities": {"manual": True, "generation": True, "community": True, "public": True,
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
        )
        month = requested_month(request.query_params.get("month"))
        if month:
            drafts = drafts.filter(month=month)
        drafts = drafts.order_by("-month", "-id")
        try:
            offset = max(0, int(request.query_params.get("offset", 0)))
        except (ValueError, TypeError, DjangoValidationError):
            raise ValidationError({"offset": "Use a nonnegative integer."})
        rows = list(drafts[offset:offset + 51])
        return Response({"updates": [update_payload(row, user=request.user) for row in rows[:50]],
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
        siblings = MonthlyUpdateDraft.objects.filter(
            organization=self.company.organization, month=draft.month,
        ).select_related("organization", "current_revision__snapshot", "published_revision__snapshot")
        if published and not draft.published_revision_id:
            raise NotFound("No approved version exists yet.")
        value = update_payload(draft, published=published, user=request.user)
        # Use the approved receipt, never the working draft's selected audience.
        if published and approved_updates("public").filter(pk=draft.pk).exists():
            value["publicUrl"] = request.build_absolute_uri(reverse("chat_startups_public_update", kwargs={"update_id": draft.pk}))
        return Response({"update": value,
            "communityPreview": update_payload(draft, published=published, community=True),
            "previousUpdates": [] if published else [
                update_payload(row, user=request.user) for row in siblings.exclude(pk=draft.pk).order_by("-updated_at", "-pk")
            ]})


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
        rows = approved_updates("community", "public").order_by("-published_at", "-id")
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
            from .website_connections import website_connection_sources
            sources.extend(website_connection_sources(request.user, self.company))
            response.data = {**response.data, "sources": sources, "connections": sources}
        return response


class GenerateView(ChatStartupAccess, founder.VibeRaisingEmailDraftStartView):
    automatic_source_scope = True

    def post(self, request):
        validate_generation_sources(request.data)
        # Check the effective selection before the shared view can debit or queue.
        # Notes/documents are usable without an OAuth source; revoked accounts
        # and missing property selections require an explicit client decision.
        from integrations.services.external_connectors import serialize_source_status
        selected = founder._include_manual_source_if_needed(
            request.data.get("inputSources", request.data.get("input_sources", [])),
            manual_document_ids=founder._get_requested_manual_document_ids(request),
            manual_summary=founder._get_requested_manual_summary(request),
        )
        rows = serialize_source_status(request.user, organization=self.company.organization).get("sources", [])
        by_key = {row.get("key"): source_capabilities(row) for row in rows if isinstance(row, dict)}
        unavailable = [key for key in selected if key != "manual_documents" and
            (by_key.get(key, {}).get("status") not in {"connected", "ready"}
             or by_key.get(key, {}).get("usableForUpdates") is False
             or by_key.get(key, {}).get("available") is False)]
        if unavailable:
            return Response({"detail": "Reconnect or remove unavailable sources before generating.",
                "code": "startup_update_sources_unavailable", "unavailableSources": unavailable}, status=409)
        try:
            return super().post(request)
        except ConnectorConfigurationError as exc:
            raise ValidationError({"sources": str(exc)}) from exc


class ActiveRunView(ChatStartupAccess, founder.VibeRaisingEmailDraftActiveRunView):
    startup_read_bucket = "poll"

    def get(self, request):
        response = super().get(request)
        # DRF renders Response(None) as an empty body. Chat's query client
        # requires a defined JSON value, including when no run is active.
        if response.status_code == 200 and response.data is None:
            response.data = {"run": None}
        return response


class RunView(ChatStartupAccess, founder.VibeRaisingEmailDraftStatusView):
    startup_read_bucket = "poll"


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
