from rest_framework import serializers

from .models import VibeRaisingCompany, VibeRaisingProfile


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
    class Meta:
        model = VibeRaisingCompany
        fields = ["id", "name", "domain", "abn", "registered"]


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
