"""Private founder Progress API; all writes are company scoped and versioned."""
from datetime import date
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import permissions, serializers
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.throttling import UserRateThrottle


class ProgressWriteThrottle(UserRateThrottle):
    scope = "progress_write"
    rate = "60/minute"


class ProgressSyncThrottle(UserRateThrottle):
    scope = "progress_sync"
    rate = "6/hour"

from startup_updates.models import StartupProfile, StartupMetricObservation
from .progress import CATEGORIES, get_progress_series, materialize_charts, validate_chart_specs, month_end


class ProgressConflict(APIException):
    status_code = 409
    default_detail = "Progress changed in another tab. Reload before saving your choices."


def context_for(request):
    from .views import _get_founder_company_context_or_response, _is_admin_user, user_may_use_organization, DomainOwnershipError
    if not getattr(settings, "STARTUP_PROGRESS_ENABLED", False):
        return None, Response({"detail": "Progress is not enabled."}, status=404)
    context, error = _get_founder_company_context_or_response(request)
    if error:
        return None, error
    company = context["company"]
    if not _is_admin_user(request.user) and not user_may_use_organization(request.user, company.organization):
        raise DomainOwnershipError()
    return company, None


def payload(company):
    profile, _ = StartupProfile.objects.get_or_create(organization=company.organization)
    configuration = profile.progress_configuration or {}
    series = get_progress_series(company.organization)
    from startup_updates.models import GoogleAnalyticsPropertySelection
    properties = list(GoogleAnalyticsPropertySelection.objects.filter(
        organization=company.organization, connection__organization=company.organization, selected=True,
    ).exclude(connection__status="disconnected").values("property_id", "property_display_name"))
    return {
        "schemaVersion": 1, "companyId": str(company.pk), "timezone": profile.reporting_timezone,
        "version": configuration.get("version", 0), "series": series,
        "charts": configuration.get("charts"), "range": configuration.get("range", 6),
        "definitions": configuration.get("definitions", []),
        "googleAnalytics": {"properties": properties, "events": configuration.get("ga_events", {}), "mappings": configuration.get("ga_mappings", {}), "lastError": configuration.get("ga_error")},
        "asOf": timezone.now().astimezone(ZoneInfo(profile.reporting_timezone)).date().isoformat(),
    }


class PreferencesSerializer(serializers.Serializer):
    expectedVersion = serializers.IntegerField(min_value=0)
    charts = serializers.ListField(child=serializers.DictField(), max_length=12)
    range = serializers.ChoiceField(choices=(3, 6, 12, 24), default=6)


class CustomMetricSerializer(serializers.Serializer):
    expectedVersion = serializers.IntegerField(min_value=0)
    key = serializers.CharField(required=False, max_length=64)
    label = serializers.CharField(max_length=80)
    definition = serializers.CharField(max_length=500)
    category = serializers.ChoiceField(choices=CATEGORIES)
    unit = serializers.ChoiceField(choices=("count", "people", "accounts", "pilots", "subscribers", "actions", "%", "seconds", "hours"))
    aggregation = serializers.ChoiceField(choices=("sum", "stock", "unique", "ratio", "average"))
    points = serializers.ListField(child=serializers.DictField(), min_length=1, max_length=24)

    def validate_points(self, points):
        values, seen = [], set()
        for point in points:
            try:
                period = date.fromisoformat(str(point.get("date")))
                amount = Decimal(str(point.get("value")))
            except Exception as exc:
                raise serializers.ValidationError("Each row needs a valid date and number.") from exc
            if period.day != 1 or not amount.is_finite() or abs(amount) >= Decimal("1000000000000000"):
                raise serializers.ValidationError("Use month-start dates and finite values below 1 quadrillion.")
            if period in seen:
                raise serializers.ValidationError("Use only one value per month.")
            seen.add(period)
            values.append({"date": period, "value": amount})
        return values


class ProgressView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ProgressWriteThrottle]

    def get(self, request):
        company, error = context_for(request)
        return error if error is not None else Response(payload(company))

    @transaction.atomic
    def post(self, request):
        company, error = context_for(request)
        if error is not None:
            return error
        serializer = PreferencesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        StartupProfile.objects.get_or_create(organization=company.organization)
        profile = StartupProfile.objects.select_for_update().get(organization=company.organization)
        config = dict(profile.progress_configuration or {})
        if serializer.validated_data["expectedVersion"] != config.get("version", 0):
            raise ProgressConflict()
        specs = validate_chart_specs(serializer.validated_data["charts"])
        today = timezone.now().astimezone(ZoneInfo(profile.reporting_timezone)).date()
        materialize_charts(specs, get_progress_series(company.organization), end_date=today)
        config.update(charts=specs, range=serializer.validated_data["range"], version=config.get("version", 0) + 1)
        profile.progress_configuration = config
        profile.save(update_fields=["progress_configuration", "updated_at"])
        return Response(payload(company))


class ProgressCustomMetricView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ProgressWriteThrottle]

    @transaction.atomic
    def post(self, request):
        company, error = context_for(request)
        if error is not None:
            return error
        serializer = CustomMetricSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        StartupProfile.objects.get_or_create(organization=company.organization)
        profile = StartupProfile.objects.select_for_update().get(organization=company.organization)
        config = dict(profile.progress_configuration or {})
        if data["expectedVersion"] != config.get("version", 0):
            raise ProgressConflict()
        definitions = list(config.get("definitions", []))
        key = data.get("key") or f"custom.{uuid4().hex[:20]}"
        existing = next((item for item in definitions if item["key"] == key), None)
        if data.get("key") and existing is None:
            raise ValidationError("Only your own founder-provided metrics can be edited.")
        if not existing and len(definitions) >= 30:
            raise ValidationError("This startup already has 30 custom metrics.")
        definition = {field: data[field] for field in ("label", "definition", "category", "unit", "aggregation")}
        # A changed meaning must start a new metric, not create a false trend.
        if existing and any(existing[field] != definition[field] for field in definition):
            raise ValidationError("Create a new metric to change its definition, unit or counting basis.")
        definition.update(key=key, version=1)
        if not existing:
            definitions.append(definition)
        today = timezone.now().astimezone(ZoneInfo(profile.reporting_timezone)).date()
        for point in data["points"]:
            if point["date"] > today or point["date"].year < 2000:
                raise ValidationError("Metric dates must be between 2000 and today.")
            if point["value"] < 0:
                raise ValidationError("Counts, rates and durations cannot be negative.")
            if data["unit"] == "%" and not 0 <= point["value"] <= 100:
                raise ValidationError("Percentages must be between 0 and 100.")
            StartupMetricObservation.objects.update_or_create(
                organization=company.organization, source_provider="founder_progress", metric_key=key,
                period_month=point["date"], run=None, source_thread=None,
                defaults={"metric_name": data["label"], "value_number": point["value"], "value_text": str(point["value"]), "unit": data["unit"],
                    "observed_at": timezone.now(), "confidence": 1,
                    "source_metadata": {"definition_version": 1, "period_start": point["date"].isoformat(), "period_end": min(month_end(point["date"]), today).isoformat(), "timezone": profile.reporting_timezone, "entered_by": request.user.pk}},
            )
        config.update(definitions=definitions, version=config.get("version", 0) + 1)
        profile.progress_configuration = config
        profile.save(update_fields=["progress_configuration", "updated_at"])
        return Response(payload(company), status=201)


class ProgressGoogleAnalyticsView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ProgressSyncThrottle]

    def post(self, request):
        company, error = context_for(request)
        if error is not None:
            return error
        from .progress_google_analytics import sync_progress_google_analytics
        from integrations.services.external_connectors import ConnectorConfigurationError, ConnectorOAuthError, ConnectorRateLimitError
        from requests.exceptions import RequestException
        try:
            result = sync_progress_google_analytics(company.organization, request.data)
        except (ConnectorConfigurationError, ConnectorOAuthError, ConnectorRateLimitError, RequestException) as exc:
            raise ValidationError("Google Analytics could not refresh. Check the connection and try again; your saved history is unchanged.") from exc
        return Response({**payload(company), "sync": result})
