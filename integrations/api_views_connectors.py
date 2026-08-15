import logging

import requests
from django.db import DatabaseError
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRooApiKey
from integrations.services.external_connectors import (
    _ACTIVE_ORG,
    ConnectorConfigurationError,
    ConnectorOAuthError,
    ConnectorRateLimitError,
    connect_humanitix_connection,
    connect_luma_connection,
    disconnect_external_connection,
    mark_sources_sync_requested,
    serialize_bank_feed_accounts,
    serialize_bank_feed_transactions,
    serialize_gmail_preview,
    serialize_google_analytics_properties,
    serialize_linear_preview,
    serialize_linear_projects,
    serialize_luma_events,
    serialize_slack_channels,
    serialize_slack_preview,
    serialize_source_status,
    serialize_xero_invoices,
    serialize_xero_preview,
    update_google_analytics_property_selections,
    update_linear_project_selections,
    update_luma_selections,
    update_slack_channel_selections,
)
from integrations.services.linear_meeting_actions import (
    LinearMeetingConfigurationError,
    LinearMeetingGraphQLError,
    LinearMeetingIdempotencyConflictError,
    LinearMeetingRateLimitError,
    LinearMeetingSizingConflictError,
    create_linear_meeting_issue,
    create_linear_meeting_project_update,
    get_linear_issue_receipt,
    get_linear_meeting_context,
    get_linear_project_sizing_context,
)
from startup_updates.data_deletion import disconnect_gmail_for_user


PREVIEW_STORAGE_UNAVAILABLE_PAYLOAD = {
    "detail": "Connector preview storage is not available. Run backend migrations before syncing financial records.",
    "code": "preview_storage_unavailable",
}
logger = logging.getLogger(__name__)


def _linear_meeting_error_response(exc):
    if isinstance(exc, LinearMeetingIdempotencyConflictError):
        return Response(
            {
                "detail": str(exc),
                "code": "linear_issue_creation_in_progress",
            },
            status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, LinearMeetingSizingConflictError):
        return Response(
            {
                "detail": str(exc),
                "code": "linear_studio_sizing_stale",
            },
            status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, LinearMeetingConfigurationError):
        return Response(
            {
                "detail": str(exc),
                "code": "linear_not_configured",
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if isinstance(exc, LinearMeetingRateLimitError):
        response = Response(
            {
                "detail": str(exc),
                "code": "linear_rate_limited",
                "retryAfter": exc.retry_after_seconds,
            },
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )
        response["Retry-After"] = str(exc.retry_after_seconds)
        return response
    if isinstance(exc, LinearMeetingGraphQLError):
        operation = getattr(exc, "operation", None)
        logger.error(
            "linear_meeting_actions_graphql_error operation=%s detail=%s",
            operation,
            str(exc),
        )
        payload = {
            "detail": str(exc),
            "code": "linear_graphql_error",
        }
        if operation:
            payload["operation"] = operation
        return Response(
            payload,
            status=status.HTTP_502_BAD_GATEWAY,
        )
    if isinstance(exc, ValueError):
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    raise exc


def _requested_providers(request):
    raw = (
        request.data.get("providers")
        or request.data.get("sources")
        or request.data.get("inputSources")
        or request.data.get("input_sources")
        or []
    )
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _string_list(raw, *object_keys):
    """Coerce a comma string / list of strings / list of {key} objects into a clean str list."""
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(raw, (list, tuple)):
        result = []
        for item in raw:
            if isinstance(item, dict):
                value = ""
                for key in object_keys:
                    candidate = str(item.get(key) or "").strip()
                    if candidate:
                        value = candidate
                        break
            else:
                value = str(item or "").strip()
            if value:
                result.append(value)
        return result
    return []


def _company_for_scope(request):
    """The explicit company on this request, or None.

    Callers run _org_scope_or_response first, so an invalid id has already
    been rejected by the time this returns.
    """
    company_id = (
        request.query_params.get("company_id")
        or request.query_params.get("companyId")
        or request.data.get("company_id")
        or request.data.get("companyId")
    )
    if not company_id:
        return None

    from founder_tools.models import VibeRaisingCompany

    return VibeRaisingCompany.objects.filter(pk=company_id, profile__user=request.user).first()


def _org_scope_or_response(request):
    """Resolve which startup's connections this request targets.

    An explicit company_id (query param or body) pins the request to that
    company's organization — validated as the requester's own — so a
    multi-startup founder's Data Sources tab operates on the company it shows,
    not whatever active_company another tab last selected. Without one, the
    _ACTIVE_ORG sentinel preserves the active-company behaviour.
    """
    company_id = (
        request.query_params.get("company_id")
        or request.query_params.get("companyId")
        or request.data.get("company_id")
        or request.data.get("companyId")
    )
    if not company_id:
        return _ACTIVE_ORG, None

    from django.core.exceptions import ValidationError as DjangoValidationError

    from founder_tools.models import VibeRaisingCompany
    from founder_tools.services import ensure_company_organization

    try:
        company = VibeRaisingCompany.objects.select_related("organization").get(
            pk=company_id, profile__user=request.user
        )
    except (VibeRaisingCompany.DoesNotExist, ValueError, DjangoValidationError):
        return None, Response({"detail": "Company not found."}, status=status.HTTP_404_NOT_FOUND)
    # A domainless company has no organization yet: treated as "no tenant", so
    # every connector reads as disconnected rather than leaking the active org.
    return ensure_company_organization(company), None


class ConnectorSourcesStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        scope, error_response = _org_scope_or_response(request)
        if error_response:
            return error_response
        return Response(serialize_source_status(request.user, organization=scope), status=status.HTTP_200_OK)


class ConnectorSourcesSyncView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        scope, error_response = _org_scope_or_response(request)
        if error_response:
            return error_response
        try:
            payload = mark_sources_sync_requested(request.user, _requested_providers(request), organization=scope)
        except ConnectorConfigurationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_202_ACCEPTED)


class ConnectorSourceConnectionDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, connection_id: int):
        deleted = disconnect_external_connection(request.user, connection_id)
        if not deleted:
            return Response({"detail": "Connection not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response({"status": "disconnected"}, status=status.HTTP_200_OK)


class LumaConnectView(APIView):
    """Link a founder's own Luma account via a pasted API key (Luma has no OAuth)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        api_key = str(request.data.get("apiKey") or request.data.get("api_key") or "").strip()
        if not api_key:
            return Response(
                {"detail": "A Luma API key is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        scope, error_response = _org_scope_or_response(request)
        if error_response:
            return error_response
        try:
            connect_luma_connection(request.user, api_key, company=_company_for_scope(request))
        except ConnectorConfigurationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(serialize_source_status(request.user, organization=scope), status=status.HTTP_200_OK)


class HumanitixConnectView(APIView):
    """Link a Humanitix account via its public read-only API key."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        api_key = str(request.data.get("apiKey") or request.data.get("api_key") or "").strip()
        if not api_key:
            return Response(
                {"detail": "A Humanitix API key is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        scope, error_response = _org_scope_or_response(request)
        if error_response:
            return error_response
        try:
            connect_humanitix_connection(
                request.user,
                api_key,
                company=_company_for_scope(request),
            )
        except ConnectorConfigurationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            serialize_source_status(request.user, organization=scope),
            status=status.HTTP_200_OK,
        )


class LumaEventListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        scope, error_response = _org_scope_or_response(request)
        if error_response:
            return error_response
        raw_limit = request.query_params.get("limit") or 50
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 50
        try:
            payload = serialize_luma_events(
                request.user,
                cursor=request.query_params.get("cursor") or None,
                limit=limit,
                organization=scope,
            )
        except ConnectorConfigurationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except DatabaseError:
            return Response(PREVIEW_STORAGE_UNAVAILABLE_PAYLOAD, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(payload, status=status.HTTP_200_OK)


class LumaSelectionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        scope, error_response = _org_scope_or_response(request)
        if error_response:
            return error_response
        event_ids = _string_list(
            request.data.get("eventIds")
            or request.data.get("event_ids")
            or request.data.get("events")
            or [],
            "eventId",
            "event_id",
            "id",
        )
        metric_keys = _string_list(
            request.data.get("metrics")
            or request.data.get("metricKeys")
            or request.data.get("metric_keys")
            or [],
            "key",
            "metricKey",
            "metric_key",
        )
        try:
            payload = update_luma_selections(request.user, event_ids, metric_keys, organization=scope)
        except ConnectorConfigurationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)


class FinancialSourcesStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        scope, error_response = _org_scope_or_response(request)
        if error_response:
            return error_response
        return Response(serialize_source_status(request.user, financial_only=True, organization=scope), status=status.HTTP_200_OK)


class FinancialSourcesSyncView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        scope, error_response = _org_scope_or_response(request)
        if error_response:
            return error_response
        try:
            payload = mark_sources_sync_requested(
                request.user,
                _requested_providers(request),
                financial_only=True,
                organization=scope,
            )
        except ConnectorConfigurationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_202_ACCEPTED)


class BankFeedAccountListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        scope, error_response = _org_scope_or_response(request)
        if error_response:
            return error_response
        return Response(serialize_bank_feed_accounts(request.user, organization=scope), status=status.HTTP_200_OK)


class BankFeedTransactionListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        scope, error_response = _org_scope_or_response(request)
        if error_response:
            return error_response
        raw_limit = request.query_params.get("limit") or 50
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 50
        payload = serialize_bank_feed_transactions(
            request.user,
            account_id=request.query_params.get("accountId") or request.query_params.get("account_id"),
            start_date=request.query_params.get("from") or request.query_params.get("start_date"),
            end_date=request.query_params.get("to") or request.query_params.get("end_date"),
            limit=limit,
            organization=scope,
        )
        return Response(payload, status=status.HTTP_200_OK)


class XeroPreviewView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        scope, error_response = _org_scope_or_response(request)
        if error_response:
            return error_response
        try:
            payload = serialize_xero_preview(
                request.user,
                start_date=request.query_params.get("from") or request.query_params.get("start_date"),
                end_date=request.query_params.get("to") or request.query_params.get("end_date"),
                organization=scope,
            )
        except DatabaseError:
            return Response(PREVIEW_STORAGE_UNAVAILABLE_PAYLOAD, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(payload, status=status.HTTP_200_OK)


class XeroInvoiceListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        scope, error_response = _org_scope_or_response(request)
        if error_response:
            return error_response
        raw_limit = request.query_params.get("limit") or 50
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 50
        try:
            payload = serialize_xero_invoices(
                request.user,
                start_date=request.query_params.get("from") or request.query_params.get("start_date"),
                end_date=request.query_params.get("to") or request.query_params.get("end_date"),
                limit=limit,
                organization=scope,
            )
        except DatabaseError:
            return Response(PREVIEW_STORAGE_UNAVAILABLE_PAYLOAD, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(payload, status=status.HTTP_200_OK)


class GmailPreviewView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        scope, error_response = _org_scope_or_response(request)
        if error_response:
            return error_response
        raw_limit = request.query_params.get("limit") or 5
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 5
        try:
            payload = serialize_gmail_preview(request.user, limit=limit, organization=scope)
        except DatabaseError:
            return Response(PREVIEW_STORAGE_UNAVAILABLE_PAYLOAD, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(payload, status=status.HTTP_200_OK)


class GmailConnectionDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request):
        scope, error_response = _org_scope_or_response(request)
        if error_response:
            return error_response
        raw_delete_derived = request.data.get("deleteDerivedData", request.data.get("delete_derived_data", False))
        delete_derived_data = raw_delete_derived is True or str(raw_delete_derived).strip().lower() in {"1", "true", "yes"}
        reason = str(request.data.get("reason") or "user_request").strip() or "user_request"
        disconnect_kwargs = {}
        if scope is not _ACTIVE_ORG:
            disconnect_kwargs["organization"] = scope
        payload = disconnect_gmail_for_user(
            request.user,
            delete_derived_data=delete_derived_data,
            reason=reason,
            **disconnect_kwargs,
        )
        return Response(payload, status=status.HTTP_200_OK)


class SlackChannelListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        scope, error_response = _org_scope_or_response(request)
        if error_response:
            return error_response
        raw_limit = request.query_params.get("limit") or 200
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 200
        try:
            payload = serialize_slack_channels(
                request.user,
                cursor=request.query_params.get("cursor") or None,
                limit=limit,
                organization=scope,
            )
        except ConnectorConfigurationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except DatabaseError:
            return Response(PREVIEW_STORAGE_UNAVAILABLE_PAYLOAD, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(payload, status=status.HTTP_200_OK)


class SlackChannelSelectionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        scope, error_response = _org_scope_or_response(request)
        if error_response:
            return error_response
        raw_channel_ids = (
            request.data.get("channelIds")
            or request.data.get("channel_ids")
            or request.data.get("channels")
            or []
        )
        if isinstance(raw_channel_ids, str):
            channel_ids = [item.strip() for item in raw_channel_ids.split(",") if item.strip()]
        elif isinstance(raw_channel_ids, (list, tuple)):
            channel_ids = [
                str(item.get("channelId") or item.get("channel_id") or item.get("id") if isinstance(item, dict) else item).strip()
                for item in raw_channel_ids
            ]
            channel_ids = [item for item in channel_ids if item]
        else:
            channel_ids = []
        try:
            payload = update_slack_channel_selections(request.user, channel_ids, organization=scope)
        except ConnectorConfigurationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)


class SlackPreviewView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        scope, error_response = _org_scope_or_response(request)
        if error_response:
            return error_response
        raw_limit = request.query_params.get("limit") or 5
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 5
        try:
            payload = serialize_slack_preview(request.user, limit=limit, organization=scope)
        except DatabaseError:
            return Response(PREVIEW_STORAGE_UNAVAILABLE_PAYLOAD, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(payload, status=status.HTTP_200_OK)


class LinearProjectListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        scope, error_response = _org_scope_or_response(request)
        if error_response:
            return error_response
        raw_limit = request.query_params.get("limit") or 100
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 100
        try:
            payload = serialize_linear_projects(
                request.user,
                cursor=request.query_params.get("cursor") or None,
                limit=limit,
                organization=scope,
            )
        except ConnectorConfigurationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ConnectorOAuthError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_401_UNAUTHORIZED)
        except ConnectorRateLimitError as exc:
            return Response(
                {"detail": str(exc), "retryAfterSeconds": exc.retry_after_seconds},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        except requests.RequestException as exc:
            logger.exception("Unable to load automatic Linear project activity")
            return Response(
                {"detail": str(exc) or "Linear is temporarily unavailable."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except DatabaseError:
            return Response(PREVIEW_STORAGE_UNAVAILABLE_PAYLOAD, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(payload, status=status.HTTP_200_OK)


class LinearProjectSelectionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        scope, error_response = _org_scope_or_response(request)
        if error_response:
            return error_response
        raw_project_ids = (
            request.data.get("projectIds")
            or request.data.get("project_ids")
            or request.data.get("projects")
            or []
        )
        if isinstance(raw_project_ids, str):
            project_ids = [item.strip() for item in raw_project_ids.split(",") if item.strip()]
        elif isinstance(raw_project_ids, (list, tuple)):
            project_ids = [
                str(
                    item.get("projectId") or item.get("project_id") or item.get("linearProjectId") or item.get("id")
                    if isinstance(item, dict)
                    else item
                ).strip()
                for item in raw_project_ids
            ]
            project_ids = [item for item in project_ids if item]
        else:
            project_ids = []
        try:
            payload = update_linear_project_selections(request.user, project_ids, organization=scope)
        except ConnectorConfigurationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ConnectorOAuthError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_401_UNAUTHORIZED)
        except ConnectorRateLimitError as exc:
            return Response(
                {"detail": str(exc), "retryAfterSeconds": exc.retry_after_seconds},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        except requests.RequestException as exc:
            logger.exception("Unable to refresh automatic Linear project activity")
            return Response(
                {"detail": str(exc) or "Linear is temporarily unavailable."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(payload, status=status.HTTP_200_OK)


class GoogleAnalyticsPropertyListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        scope, error_response = _org_scope_or_response(request)
        if error_response:
            return error_response
        raw_limit = request.query_params.get("limit") or 200
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 200
        try:
            payload = serialize_google_analytics_properties(
                request.user,
                cursor=request.query_params.get("cursor") or None,
                limit=limit,
                organization=scope,
            )
        except ConnectorConfigurationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except DatabaseError:
            return Response(PREVIEW_STORAGE_UNAVAILABLE_PAYLOAD, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(payload, status=status.HTTP_200_OK)


class GoogleAnalyticsPropertySelectionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        scope, error_response = _org_scope_or_response(request)
        if error_response:
            return error_response
        raw_property_ids = (
            request.data.get("propertyIds")
            or request.data.get("property_ids")
            or request.data.get("properties")
            or []
        )
        if isinstance(raw_property_ids, str):
            property_ids = [item.strip() for item in raw_property_ids.split(",") if item.strip()]
        elif isinstance(raw_property_ids, (list, tuple)):
            property_ids = [
                str(
                    item.get("propertyId") or item.get("property_id") or item.get("id")
                    if isinstance(item, dict)
                    else item
                ).strip()
                for item in raw_property_ids
            ]
            property_ids = [item for item in property_ids if item]
        else:
            property_ids = []
        try:
            payload = update_google_analytics_property_selections(request.user, property_ids, organization=scope)
        except ConnectorConfigurationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)


class LinearPreviewView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        raw_limit = request.query_params.get("limit") or 5
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 5
        try:
            payload = serialize_linear_preview(request.user, limit=limit, organization=scope)
        except DatabaseError:
            return Response(PREVIEW_STORAGE_UNAVAILABLE_PAYLOAD, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(payload, status=status.HTTP_200_OK)


class LinearMeetingContextView(APIView):
    permission_classes = [HasRooApiKey]

    def get(self, request):
        try:
            payload = get_linear_meeting_context()
        except (
            LinearMeetingConfigurationError,
            LinearMeetingRateLimitError,
            LinearMeetingGraphQLError,
            LinearMeetingIdempotencyConflictError,
            ValueError,
        ) as exc:
            return _linear_meeting_error_response(exc)
        return Response(payload, status=status.HTTP_200_OK)


class LinearMeetingIssueCreateView(APIView):
    permission_classes = [HasRooApiKey]

    def post(self, request):
        try:
            payload = create_linear_meeting_issue(request.data)
        except (
            LinearMeetingConfigurationError,
            LinearMeetingRateLimitError,
            LinearMeetingGraphQLError,
            LinearMeetingIdempotencyConflictError,
            LinearMeetingSizingConflictError,
            ValueError,
        ) as exc:
            return _linear_meeting_error_response(exc)
        return Response(payload, status=status.HTTP_201_CREATED)


class LinearProjectSizingContextView(APIView):
    permission_classes = [HasRooApiKey]

    def get(self, request, project_id):
        try:
            payload = get_linear_project_sizing_context(
                project_id,
                update_limit=request.query_params.get("update_limit") or 5,
                active_issue_limit=request.query_params.get("active_issue_limit") or 40,
                terminal_issue_limit=request.query_params.get("terminal_issue_limit") or 10,
                precedent_limit=request.query_params.get("precedent_limit") or 20,
            )
        except (
            LinearMeetingConfigurationError,
            LinearMeetingRateLimitError,
            LinearMeetingGraphQLError,
            ValueError,
        ) as exc:
            return _linear_meeting_error_response(exc)
        return Response(payload, status=status.HTTP_200_OK)


class LinearMeetingIssueReceiptView(APIView):
    permission_classes = [HasRooApiKey]

    def get(self, request, idempotency_key):
        try:
            payload = get_linear_issue_receipt(idempotency_key)
        except ValueError as exc:
            return _linear_meeting_error_response(exc)
        response_status = (
            status.HTTP_404_NOT_FOUND
            if payload.get("status") == "not_found"
            else status.HTTP_200_OK
        )
        return Response(payload, status=response_status)


class LinearMeetingProjectUpdateCreateView(APIView):
    permission_classes = [HasRooApiKey]

    def post(self, request):
        try:
            payload = create_linear_meeting_project_update(request.data)
        except (
            LinearMeetingConfigurationError,
            LinearMeetingRateLimitError,
            LinearMeetingGraphQLError,
            ValueError,
        ) as exc:
            return _linear_meeting_error_response(exc)
        return Response(payload, status=status.HTTP_201_CREATED)
