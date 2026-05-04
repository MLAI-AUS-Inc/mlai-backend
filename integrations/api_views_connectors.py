from django.db import DatabaseError
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from integrations.services.external_connectors import (
    ConnectorConfigurationError,
    disconnect_external_connection,
    mark_sources_sync_requested,
    serialize_bank_feed_accounts,
    serialize_bank_feed_transactions,
    serialize_gmail_preview,
    serialize_linear_preview,
    serialize_linear_projects,
    serialize_slack_channels,
    serialize_slack_preview,
    serialize_source_status,
    serialize_xero_invoices,
    serialize_xero_preview,
    update_linear_project_selections,
    update_slack_channel_selections,
)
from startup_updates.data_deletion import disconnect_gmail_for_user


PREVIEW_STORAGE_UNAVAILABLE_PAYLOAD = {
    "detail": "Connector preview storage is not available. Run backend migrations before syncing financial records.",
    "code": "preview_storage_unavailable",
}


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


class ConnectorSourcesStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(serialize_source_status(request.user), status=status.HTTP_200_OK)


class ConnectorSourcesSyncView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        try:
            payload = mark_sources_sync_requested(request.user, _requested_providers(request))
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


class FinancialSourcesStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(serialize_source_status(request.user, financial_only=True), status=status.HTTP_200_OK)


class FinancialSourcesSyncView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        try:
            payload = mark_sources_sync_requested(
                request.user,
                _requested_providers(request),
                financial_only=True,
            )
        except ConnectorConfigurationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_202_ACCEPTED)


class BankFeedAccountListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(serialize_bank_feed_accounts(request.user), status=status.HTTP_200_OK)


class BankFeedTransactionListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
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
        )
        return Response(payload, status=status.HTTP_200_OK)


class XeroPreviewView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            payload = serialize_xero_preview(
                request.user,
                start_date=request.query_params.get("from") or request.query_params.get("start_date"),
                end_date=request.query_params.get("to") or request.query_params.get("end_date"),
            )
        except DatabaseError:
            return Response(PREVIEW_STORAGE_UNAVAILABLE_PAYLOAD, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(payload, status=status.HTTP_200_OK)


class XeroInvoiceListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
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
            )
        except DatabaseError:
            return Response(PREVIEW_STORAGE_UNAVAILABLE_PAYLOAD, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(payload, status=status.HTTP_200_OK)


class GmailPreviewView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        raw_limit = request.query_params.get("limit") or 5
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 5
        try:
            payload = serialize_gmail_preview(request.user, limit=limit)
        except DatabaseError:
            return Response(PREVIEW_STORAGE_UNAVAILABLE_PAYLOAD, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(payload, status=status.HTTP_200_OK)


class GmailConnectionDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request):
        raw_delete_derived = request.data.get("deleteDerivedData", request.data.get("delete_derived_data", False))
        delete_derived_data = raw_delete_derived is True or str(raw_delete_derived).strip().lower() in {"1", "true", "yes"}
        reason = str(request.data.get("reason") or "user_request").strip() or "user_request"
        payload = disconnect_gmail_for_user(
            request.user,
            delete_derived_data=delete_derived_data,
            reason=reason,
        )
        return Response(payload, status=status.HTTP_200_OK)


class SlackChannelListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
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
            )
        except ConnectorConfigurationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except DatabaseError:
            return Response(PREVIEW_STORAGE_UNAVAILABLE_PAYLOAD, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(payload, status=status.HTTP_200_OK)


class SlackChannelSelectionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
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
            payload = update_slack_channel_selections(request.user, channel_ids)
        except ConnectorConfigurationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)


class SlackPreviewView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        raw_limit = request.query_params.get("limit") or 5
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 5
        try:
            payload = serialize_slack_preview(request.user, limit=limit)
        except DatabaseError:
            return Response(PREVIEW_STORAGE_UNAVAILABLE_PAYLOAD, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(payload, status=status.HTTP_200_OK)


class LinearProjectListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
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
            )
        except ConnectorConfigurationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except DatabaseError:
            return Response(PREVIEW_STORAGE_UNAVAILABLE_PAYLOAD, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(payload, status=status.HTTP_200_OK)


class LinearProjectSelectionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
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
            payload = update_linear_project_selections(request.user, project_ids)
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
            payload = serialize_linear_preview(request.user, limit=limit)
        except DatabaseError:
            return Response(PREVIEW_STORAGE_UNAVAILABLE_PAYLOAD, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(payload, status=status.HTTP_200_OK)
