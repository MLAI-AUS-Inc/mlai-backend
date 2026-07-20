from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRooApiKey
from roo.permissions import is_points_admin
from organizations.models import Organization
from integrations.models import (
    ReconciliationMapping,
    ReconciliationProfile,
    StripePayoutReconciliation,
)
from integrations.services.reconciliation import (
    ReconciliationReportService,
    StripeAPIError,
    StripeConfigurationError,
)
from integrations.services.xero_reconciliation import (
    ReconciliationValidationError,
    XeroPostingError,
    build_xero_preview,
    persist_report,
    post_xero_bank_transaction,
    resolve_xero_connection,
    serialize_mapping,
    serialize_payout,
    serialize_profile,
)


MAX_WINDOW_DAYS = 92
DEFAULT_WINDOW_DAYS = 30


def _admin_or_response(request, *, from_body: bool = False):
    values = request.data if from_body else request.query_params
    slack_user_id = str(values.get("slack_user_id") or "").strip()
    if not slack_user_id:
        return None, Response({"error": "slack_user_id is required"}, status=status.HTTP_400_BAD_REQUEST)
    if not is_points_admin(slack_user_id):
        return None, Response(
            {"error": "Only Points Admins can manage payout reconciliation"},
            status=status.HTTP_403_FORBIDDEN,
        )
    return slack_user_id, None


def _organization_or_response(request, *, from_body: bool = False):
    from django.conf import settings

    values = request.data if from_body else request.query_params
    domain = str(values.get("domain") or getattr(settings, "RECONCILIATION_DEFAULT_DOMAIN", "mlai.au")).strip().lower()
    organization = Organization.objects.filter(domain__iexact=domain).first()
    if organization is None:
        return None, Response({"error": f"Unknown organisation domain: {domain}"}, status=status.HTTP_404_NOT_FOUND)
    return organization, None


class ReconciliationAdminView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def context(self, request, *, from_body: bool = False):
        slack_user_id, response = _admin_or_response(request, from_body=from_body)
        if response:
            return None, None, response
        organization, response = _organization_or_response(request, from_body=from_body)
        return slack_user_id, organization, response


class ReconciliationReportView(APIView):
    """Roo-only endpoint: Luma->Stripe reconciliation report for Points Admins.

    Payout-driven: returns each Stripe payout (= one bank deposit) with the
    ticket charges behind it, a Cowork markdown brief, and an optional xlsx
    audit workbook. Read-only against Stripe. Points-Admin gated (contains PII).
    """

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        slack_user_id = str(request.query_params.get("slack_user_id") or "").strip()
        if not slack_user_id:
            return Response({"error": "slack_user_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        if not is_points_admin(slack_user_id):
            return Response(
                {"error": "Only Points Admins (admin, committee, or portfolio_lead) can run reconciliation reports"},
                status=status.HTTP_403_FORBIDDEN,
            )

        window, error_response = self._resolve_window(request.query_params)
        if error_response:
            return error_response
        since, until = window

        include_workbook = self._parse_bool(request.query_params.get("include_workbook"), default=True)

        service = ReconciliationReportService()
        try:
            report = service.build_report(since=since, until=until, include_workbook=include_workbook)
        except StripeConfigurationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except StripeAPIError as exc:
            if exc.status_code == 429:
                return Response({"error": str(exc)}, status=status.HTTP_429_TOO_MANY_REQUESTS)
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response(report)

    def post(self, request):
        """Run a bounded backfill and persist its payout ledger (never posts Xero)."""
        slack_user_id, error = _admin_or_response(request, from_body=True)
        if error:
            return error
        organization, error = _organization_or_response(request, from_body=True)
        if error:
            return error
        window, error = self._resolve_window(request.data)
        if error:
            return error
        try:
            report = ReconciliationReportService().build_report(
                since=window[0], until=window[1], include_workbook=False
            )
            profile = ReconciliationProfile.objects.filter(organization=organization).first()
            account_id = profile.stripe_account_id if profile else ""
            records = persist_report(
                organization=organization,
                report=report,
                stripe_account_id=account_id,
            )
        except StripeConfigurationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except StripeAPIError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(
            {
                "payout_count": len(records),
                "payouts": [serialize_payout(record) for record in records],
                "requested_by": slack_user_id,
            },
            status=status.HTTP_200_OK,
        )

    # ---- window parsing --------------------------------------------------

    def _resolve_window(self, params):
        """Return ((since, until), None) or (None, error_response).

        Precedence: explicit since/until (YYYY-MM-DD) override the rolling
        `days` window. `until` defaults to now; `since` defaults to
        until - days. Window is capped at MAX_WINDOW_DAYS.
        """
        now = datetime.now(timezone.utc)

        # Validate `days` unconditionally so a malformed value is a clear 400 even
        # when since/until take precedence over it.
        days, err = self._parse_days(params.get("days"))
        if err:
            return None, err

        until, err = self._parse_date_end(params.get("until"), default=now)
        if err:
            return None, err
        since, err = self._parse_date_start(params.get("since"), default=None)
        if err:
            return None, err

        if since is None:
            since = until - timedelta(days=days)

        if since >= until:
            return None, Response(
                {"error": "since must be before until"}, status=status.HTTP_400_BAD_REQUEST
            )
        # Compare on calendar days: an explicit end date is inflated to end-of-day,
        # so a raw timedelta would spuriously reject an exactly-MAX_WINDOW_DAYS span.
        if (until.date() - since.date()).days > MAX_WINDOW_DAYS:
            return None, Response(
                {"error": f"window too large; max {MAX_WINDOW_DAYS} days"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return (since, until), None

    @staticmethod
    def _parse_days(raw_value):
        if raw_value in (None, ""):
            return DEFAULT_WINDOW_DAYS, None
        try:
            days = int(raw_value)
        except (TypeError, ValueError):
            return None, Response({"error": "days must be an integer"}, status=status.HTTP_400_BAD_REQUEST)
        if days < 1:
            return None, Response({"error": "days must be at least 1"}, status=status.HTTP_400_BAD_REQUEST)
        return min(days, MAX_WINDOW_DAYS), None

    @staticmethod
    def _parse_date_start(raw_value, *, default):
        if raw_value in (None, ""):
            return default, None
        try:
            d = datetime.fromisoformat(str(raw_value).strip()).date()
        except ValueError:
            return None, Response({"error": "since must use YYYY-MM-DD"}, status=status.HTTP_400_BAD_REQUEST)
        return datetime.combine(d, time.min, tzinfo=timezone.utc), None

    @staticmethod
    def _parse_date_end(raw_value, *, default):
        if raw_value in (None, ""):
            return default, None
        try:
            d = datetime.fromisoformat(str(raw_value).strip()).date()
        except ValueError:
            return None, Response({"error": "until must use YYYY-MM-DD"}, status=status.HTTP_400_BAD_REQUEST)
        # inclusive end-of-day for the given date
        return datetime.combine(d, time.max, tzinfo=timezone.utc), None

    @staticmethod
    def _parse_bool(raw_value, *, default: bool) -> bool:
        if raw_value is None:
            return default
        normalized = str(raw_value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default


class ReconciliationProfileView(ReconciliationAdminView):
    EDITABLE_FIELDS = {
        "stripe_account_id",
        "xero_bank_account_id",
        "xero_bank_account_name",
        "xero_contact_id",
        "xero_contact_name",
        "revenue_account_code",
        "fee_account_code",
        "refund_account_code",
        "revenue_tax_type",
        "fee_tax_type",
        "refund_tax_type",
        "line_amount_types",
        "event_tracking_category_id",
        "event_tracking_category_name",
        "project_tracking_category_id",
        "project_tracking_category_name",
        "standalone_fee_project_option_id",
        "standalone_fee_project_option_name",
        "enabled",
    }

    def get(self, request):
        _, organization, error = self.context(request)
        if error:
            return error
        profile, _ = ReconciliationProfile.objects.get_or_create(organization=organization)
        if profile.xero_connection_id is None:
            connection = resolve_xero_connection(organization)
            if connection:
                profile.xero_connection = connection
                profile.save(update_fields=["xero_connection", "updated_at"])
        return Response({"profile": serialize_profile(profile)})

    def put(self, request):
        _, organization, error = self.context(request, from_body=True)
        if error:
            return error
        profile, _ = ReconciliationProfile.objects.get_or_create(organization=organization)
        connection_id = request.data.get("xero_connection_id")
        if connection_id not in (None, ""):
            try:
                connection_id = int(connection_id)
            except (TypeError, ValueError):
                return Response({"error": "xero_connection_id must be an integer"}, status=status.HTTP_400_BAD_REQUEST)
            connection = resolve_xero_connection(organization, connection_id)
            if connection is None:
                return Response({"error": "Xero connection does not belong to this organisation"}, status=status.HTTP_400_BAD_REQUEST)
            profile.xero_connection = connection
        for field in self.EDITABLE_FIELDS:
            if field in request.data:
                setattr(profile, field, request.data[field])
        if profile.line_amount_types not in {"Inclusive", "Exclusive", "NoTax"}:
            return Response({"error": "line_amount_types must be Inclusive, Exclusive, or NoTax"}, status=status.HTTP_400_BAD_REQUEST)
        profile.save()
        return Response({"profile": serialize_profile(profile)})


class ReconciliationMappingView(ReconciliationAdminView):
    EDITABLE_FIELDS = {
        "source_label",
        "accounting_treatment",
        "event_tracking_option_id",
        "event_tracking_option_name",
        "project_tracking_option_id",
        "project_tracking_option_name",
        "account_code",
        "tax_type",
        "active",
    }

    def get(self, request):
        _, organization, error = self.context(request)
        if error:
            return error
        mappings = ReconciliationMapping.objects.filter(organization=organization).order_by("source_type", "source_label", "source_id")
        return Response({"mappings": [serialize_mapping(mapping) for mapping in mappings]})

    def put(self, request):
        _, organization, error = self.context(request, from_body=True)
        if error:
            return error
        items = request.data.get("mappings")
        if not isinstance(items, list):
            items = [request.data]
        saved = []
        valid_types = {choice[0] for choice in ReconciliationMapping.SOURCE_CHOICES}
        for item in items:
            if not isinstance(item, dict):
                return Response({"error": "Each mapping must be an object"}, status=status.HTTP_400_BAD_REQUEST)
            source_type = str(item.get("source_type") or "").strip()
            source_id = str(item.get("source_id") or "").strip()
            if source_type not in valid_types or not source_id:
                return Response({"error": "Each mapping needs a valid source_type and source_id"}, status=status.HTTP_400_BAD_REQUEST)
            defaults = {field: item[field] for field in self.EDITABLE_FIELDS if field in item}
            treatment = str(defaults.get("accounting_treatment") or "").strip()
            if treatment and treatment not in {choice[0] for choice in ReconciliationMapping.TREATMENT_CHOICES}:
                return Response({"error": "accounting_treatment must be revenue or clearing"}, status=status.HTTP_400_BAD_REQUEST)
            mapping, _ = ReconciliationMapping.objects.update_or_create(
                organization=organization,
                source_type=source_type,
                source_id=source_id,
                defaults=defaults,
            )
            saved.append(mapping)
        return Response({"mappings": [serialize_mapping(mapping) for mapping in saved]})


class ReconciliationPayoutListView(ReconciliationAdminView):
    def get(self, request):
        _, organization, error = self.context(request)
        if error:
            return error
        records = StripePayoutReconciliation.objects.filter(organization=organization).order_by("-arrival_date", "-id")[:250]
        return Response({"payouts": [serialize_payout(record) for record in records]})


class ReconciliationPayoutPreviewView(ReconciliationAdminView):
    def get(self, request, payout_id: str):
        _, organization, error = self.context(request)
        if error:
            return error
        record = StripePayoutReconciliation.objects.filter(organization=organization, payout_id=payout_id).first()
        if record is None:
            return Response({"error": "Payout was not found"}, status=status.HTTP_404_NOT_FOUND)
        try:
            preview = build_xero_preview(record)
        except ReconciliationValidationError as exc:
            return Response({"error": str(exc), "errors": exc.errors}, status=status.HTTP_409_CONFLICT)
        return Response({"payout": serialize_payout(record), "preview": preview})


class ReconciliationPayoutPostView(ReconciliationAdminView):
    def post(self, request, payout_id: str):
        slack_user_id, organization, error = self.context(request, from_body=True)
        if error:
            return error
        if request.data.get("confirm") is not True:
            return Response({"error": "confirm must be true to post to Xero"}, status=status.HTTP_400_BAD_REQUEST)
        record = StripePayoutReconciliation.objects.filter(organization=organization, payout_id=payout_id).first()
        if record is None:
            return Response({"error": "Payout was not found"}, status=status.HTTP_404_NOT_FOUND)
        try:
            posted = post_xero_bank_transaction(record, approved_by_slack_id=slack_user_id)
        except ReconciliationValidationError as exc:
            return Response({"error": str(exc), "errors": exc.errors}, status=status.HTTP_409_CONFLICT)
        except XeroPostingError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({"payout": serialize_payout(posted, include_payload=True)})
