"""Admin-only reports for member contributions and co-working bookings."""

from datetime import date

from rest_framework import serializers
from rest_framework.response import Response

from .admin_access import AdminError, member_progress_capabilities
from .admin_member_progress import member_progress
from .admin_member_progress_data import CATEGORY_DEFINITIONS, REGIONS
from .admin_views import MlaiAdminView
from .volunteer.ladder import MAX_LEVEL


class MemberProgressParameters(serializers.Serializer):
    """Validate costs and selectors before querying any member records."""

    view = serializers.ChoiceField(
        choices=("overview", "locations", "leadership", "members", "member"), default="overview",
    )
    days = serializers.ChoiceField(choices=(7, 30, 90), default=30)
    months = serializers.ChoiceField(choices=(3, 6), default=6)
    region = serializers.ChoiceField(choices=tuple(REGIONS), default=None)
    level = serializers.ChoiceField(choices=("pending", *range(MAX_LEVEL + 1)), default=None)
    category = serializers.ChoiceField(choices=tuple(CATEGORY_DEFINITIONS), default=None)
    sort = serializers.ChoiceField(
        choices=("movement", "points", "attendance", "badges", "recent", "name", "bookings"), default="movement",
    )
    metric = serializers.ChoiceField(
        choices=("movement", "points", "attendance", "newBadge", "recent", "bookings"), default=None,
    )
    q = serializers.CharField(max_length=80, allow_blank=True, default="")
    page = serializers.IntegerField(min_value=1, max_value=1000, default=1)
    id = serializers.UUIDField(default=None)
    month = serializers.RegexField(regex=r"^20[0-9]{2}-(0[1-9]|1[0-2])$", default=None)

    def validate(self, attrs):
        if attrs["view"] == "member" and attrs["id"] is None:
            raise serializers.ValidationError({"id": "A member is required."})
        if attrs["id"] is not None:
            attrs["id"] = str(attrs["id"])
        if attrs["month"] is not None:
            attrs["month"] = date.fromisoformat(attrs["month"] + "-01")
        return attrs


class AdminMemberProgressView(MlaiAdminView):
    """A trusted installation and the report's dedicated access are required."""

    def get(self, request):
        access = member_progress_capabilities(request.user)
        if "member_progress" not in access:
            raise AdminError()
        query = MemberProgressParameters(data=request.query_params)
        query.is_valid(raise_exception=True)
        filters = query.validated_data
        if (
            filters["view"] == "locations" or filters["region"]
        ) and "member_progress_locations" not in access:
            raise AdminError()
        report = member_progress(filters)
        if report is None:
            return Response({"error": "not_found"}, status=404)
        return Response(report)
