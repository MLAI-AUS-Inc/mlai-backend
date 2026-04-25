from rest_framework import serializers

from .models import VibeRaisingCompany, VibeRaisingProfile
from .services import ensure_company_organization, normalize_company_domain


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
        normalized = data.copy() if hasattr(data, "copy") else dict(data)
        for canonical_key, aliases in self.input_aliases.items():
            if canonical_key in normalized:
                continue
            for alias in aliases:
                if alias in normalized:
                    normalized[canonical_key] = normalized.get(alias)
                    break
        return super().to_internal_value(normalized)


class FounderCompanySerializer(serializers.ModelSerializer):
    organizationId = serializers.SerializerMethodField()
    organizationDomain = serializers.SerializerMethodField()

    class Meta:
        model = VibeRaisingCompany
        fields = [
            "id",
            "name",
            "domain",
            "abn",
            "registered",
            "organizationId",
            "organizationDomain",
        ]

    def get_organizationId(self, obj):
        return obj.organization_id

    def get_organizationDomain(self, obj):
        return obj.organization.domain if obj.organization_id else None


class FounderProfileSerializer(serializers.ModelSerializer):
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
        return str(obj.active_company_id) if obj.active_company_id else None

    def get_companies(self, obj):
        companies = obj.companies.select_related("organization").all()
        return FounderCompanySerializer(companies, many=True).data


class FounderProfileUpsertSerializer(AliasInputSerializer):
    input_aliases = {
        "organizationName": ("organization_name",),
    }

    role = serializers.ChoiceField(
        choices=((VibeRaisingProfile.ROLE_FOUNDER, "Founder"),),
        required=False,
        default=VibeRaisingProfile.ROLE_FOUNDER,
    )
    organizationName = serializers.CharField(
        allow_blank=True,
        allow_null=True,
        required=False,
    )

    def validate(self, attrs):
        attrs["role"] = VibeRaisingProfile.ROLE_FOUNDER
        attrs["organization_name"] = None
        attrs.pop("organizationName", None)
        return attrs


class FounderCompanyUpsertSerializer(AliasInputSerializer):
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
            domain = _blank_to_none(attrs.get("domain"))
            attrs["domain"] = normalize_company_domain(domain) if domain else None
        if "abn" in attrs:
            attrs["abn"] = _blank_to_none(attrs.get("abn"))
        return attrs


class FounderActiveCompanySerializer(AliasInputSerializer):
    input_aliases = {
        "companyId": ("company_id",),
    }

    companyId = serializers.UUIDField()


def serialize_founder_bootstrap(user, profile):
    active_company = profile.active_company or profile.companies.order_by("created_at", "name").first()
    if active_company and not active_company.organization_id:
        ensure_company_organization(active_company)
    companies = profile.companies.select_related("organization").all()

    supported = profile.role == VibeRaisingProfile.ROLE_FOUNDER
    redirect_hint = None
    if not supported:
        redirect_hint = {"to": "/founder-tools", "reason": "unsupported_profile"}
    elif not companies.exists():
        redirect_hint = {"to": "/founder-tools/company-setup", "reason": "company_required"}

    return {
        "user": {
            "id": user.id,
            "email": user.email,
            "firstName": user.first_name,
            "lastName": user.last_name,
            "fullName": user.full_name,
            "avatarUrl": user.avatar_url,
            "isSuperuser": user.is_superuser,
        },
        "profile": FounderProfileSerializer(profile).data,
        "activeCompany": FounderCompanySerializer(active_company).data if active_company else None,
        "companies": FounderCompanySerializer(companies, many=True).data,
        "linkedOrganization": (
            {
                "id": active_company.organization_id,
                "domain": active_company.organization.domain,
                "name": active_company.organization.name,
                "competitors": active_company.organization.competitors,
                "seedKeywords": active_company.organization.seed_keywords,
            }
            if active_company and active_company.organization_id
            else None
        ),
        "availableTabs": [
            {"id": "updates", "label": "Vibe Raising", "path": "/founder-tools/updates"},
            {"id": "marketing", "label": "Vibe Marketing", "path": "/founder-tools/marketing"},
            {"id": "data_sources", "label": "Data Sources", "path": "/founder-tools/data-sources"},
            {"id": "companies", "label": "Companies", "path": "/founder-tools/companies"},
        ],
        "connectors": [],
        "redirectHint": redirect_hint,
        "unsupported": not supported,
    }
