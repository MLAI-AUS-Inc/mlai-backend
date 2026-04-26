import calendar

from rest_framework import serializers

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from startup_updates.metric_catalog import (
    STARTUP_UPDATE_METRIC_KEY_SET,
    startup_update_metric_key,
    startup_update_metric_label,
)


VIBE_RAISING_UPDATE_METRIC_KEYS = STARTUP_UPDATE_METRIC_KEY_SET


def _blank_to_none(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


class AliasInputSerializer(serializers.Serializer):
    input_aliases = {}

    def to_internal_value(self, data):
        if hasattr(data, "copy"):
            normalized = data.copy()
        else:
            normalized = dict(data)

        for canonical_key, aliases in self.input_aliases.items():
            if canonical_key in normalized:
                continue
            for alias in aliases:
                if alias in normalized:
                    normalized[canonical_key] = normalized.get(alias)
                    break

        return super().to_internal_value(normalized)


class VibeRaisingCompanySerializer(serializers.ModelSerializer):
    organizationId = serializers.SerializerMethodField()
    organizationDomain = serializers.SerializerMethodField()

    class Meta:
        model = VibeRaisingCompany
        fields = ["id", "name", "domain", "abn", "registered", "organizationId", "organizationDomain"]

    def get_organizationId(self, obj):
        return obj.organization_id

    def get_organizationDomain(self, obj):
        return obj.organization.domain if obj.organization_id else None


class VibeRaisingProfileSerializer(serializers.ModelSerializer):
    organizationName = serializers.CharField(
        source="organization_name",
        allow_null=True,
        required=False,
    )
    activeCompanyId = serializers.SerializerMethodField()
    companies = serializers.SerializerMethodField()

    class Meta:
        model = VibeRaisingProfile
        fields = ["role", "organizationName", "activeCompanyId", "companies"]

    def get_activeCompanyId(self, obj):
        if obj.role == VibeRaisingProfile.ROLE_INVESTOR:
            return None
        return str(obj.active_company_id) if obj.active_company_id else None

    def get_companies(self, obj):
        if obj.role == VibeRaisingProfile.ROLE_INVESTOR:
            return []
        companies = obj.companies.all()
        return VibeRaisingCompanySerializer(companies, many=True).data


class VibeRaisingProfileUpsertSerializer(AliasInputSerializer):
    input_aliases = {
        "organizationName": ("organization_name",),
    }

    role = serializers.ChoiceField(choices=VibeRaisingProfile.ROLE_CHOICES)
    organizationName = serializers.CharField(
        allow_blank=True,
        allow_null=True,
        required=False,
    )

    def validate(self, attrs):
        role = attrs["role"]
        organization_name = _blank_to_none(attrs.get("organizationName"))

        if role == VibeRaisingProfile.ROLE_INVESTOR and not organization_name:
            raise serializers.ValidationError(
                {"organizationName": "This field is required for investors."}
            )

        attrs["organization_name"] = (
            organization_name if role == VibeRaisingProfile.ROLE_INVESTOR else None
        )
        attrs.pop("organizationName", None)
        return attrs


class VibeRaisingCompanyUpsertSerializer(AliasInputSerializer):
    input_aliases = {
        "companyId": ("company_id",),
    }

    companyId = serializers.UUIDField(required=False, allow_null=True)
    name = serializers.CharField()
    domain = serializers.CharField(allow_blank=True, allow_null=True, required=False)
    abn = serializers.CharField(allow_blank=True, allow_null=True, required=False)
    registered = serializers.BooleanField(required=False)

    def validate(self, attrs):
        attrs["name"] = attrs["name"].strip()
        if not attrs["name"]:
            raise serializers.ValidationError({"name": "This field may not be blank."})

        if "domain" in attrs:
            attrs["domain"] = _blank_to_none(attrs.get("domain"))
        if "abn" in attrs:
            attrs["abn"] = _blank_to_none(attrs.get("abn"))
        return attrs


class VibeRaisingActiveCompanySerializer(AliasInputSerializer):
    input_aliases = {
        "companyId": ("company_id",),
    }

    companyId = serializers.UUIDField()


class VibeRaisingMonthlyUpdateUpsertSerializer(AliasInputSerializer):
    input_aliases = {
        "sourceUrl": ("source_url",),
        "videoUrl": ("video_url",),
        "videoStoragePath": ("video_storage_path",),
        "videoContentType": ("video_content_type",),
        "videoFileSizeBytes": ("video_file_size_bytes",),
        "videoOriginalFilename": ("video_original_filename",),
        "metricSuggestions": ("metric_suggestions",),
        "next30Days": ("next_30_days",),
    }

    month = serializers.CharField()
    year = serializers.IntegerField(min_value=2000, max_value=2100)
    summary = serializers.CharField(allow_blank=True, allow_null=True, required=False, default="")
    sourceUrl = serializers.URLField(allow_blank=True, allow_null=True, required=False)
    videoUrl = serializers.URLField(allow_blank=True, allow_null=True, required=False)
    videoStoragePath = serializers.CharField(allow_blank=True, allow_null=True, required=False)
    videoContentType = serializers.CharField(allow_blank=True, allow_null=True, required=False)
    videoFileSizeBytes = serializers.IntegerField(min_value=0, required=False, allow_null=True)
    videoOriginalFilename = serializers.CharField(allow_blank=True, allow_null=True, required=False)
    highlights = serializers.CharField(allow_blank=True, required=False, default="")
    challenges = serializers.CharField(allow_blank=True, required=False, default="")
    asks = serializers.CharField(allow_blank=True, required=False, default="")
    learnings = serializers.CharField(allow_blank=True, required=False, default="")
    next30Days = serializers.CharField(allow_blank=True, required=False, default="")
    metrics = serializers.DictField(
        child=serializers.CharField(allow_blank=True),
        required=False,
        default=dict,
    )
    metricSuggestions = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        default=list,
    )

    def validate(self, attrs):
        month_lookup = {
            name.lower(): index
            for index, name in enumerate(calendar.month_name)
            if index
        }
        raw_month = str(attrs.get("month") or "").strip()
        month_number = month_lookup.get(raw_month.lower())
        if month_number is None:
            raise serializers.ValidationError({"month": "Use a full month name."})

        attrs["month"] = calendar.month_name[month_number]
        attrs["month_number"] = month_number

        for field in ("highlights", "challenges", "asks", "learnings", "next30Days"):
            attrs[field] = str(attrs.get(field) or "").strip()

        for field in (
            "summary",
            "sourceUrl",
            "videoUrl",
            "videoStoragePath",
            "videoContentType",
            "videoOriginalFilename",
        ):
            if field in attrs:
                attrs[field] = _blank_to_none(attrs.get(field))

        normalized_metrics = {}
        for key, value in (attrs.get("metrics") or {}).items():
            metric_key = startup_update_metric_key(key)
            if metric_key not in VIBE_RAISING_UPDATE_METRIC_KEYS:
                continue
            normalized_value = _blank_to_none(value)
            if normalized_value is not None:
                normalized_metrics[metric_key] = normalized_value

        attrs["metrics"] = normalized_metrics

        normalized_suggestions = []
        seen_suggestions = set()
        for item in attrs.get("metricSuggestions") or []:
            if not isinstance(item, dict):
                continue
            metric_key = startup_update_metric_key(
                item.get("metricKey") or item.get("metric_key") or item.get("label")
            )
            if metric_key not in VIBE_RAISING_UPDATE_METRIC_KEYS or metric_key in seen_suggestions:
                continue
            seen_suggestions.add(metric_key)
            normalized_suggestions.append(
                {
                    "metric_key": metric_key,
                    "label": str(item.get("label") or startup_update_metric_label(metric_key)).strip()
                    or startup_update_metric_label(metric_key),
                    "reason": str(item.get("reason") or "").strip(),
                }
            )

        attrs["metricSuggestions"] = normalized_suggestions
        return attrs
