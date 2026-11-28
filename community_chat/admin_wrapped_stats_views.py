"""Admin-only MLAI Wrapped stat-sheet export, gated like Member progress."""

import logging

from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response

from .admin_access import AdminError, member_progress_capabilities
from .admin_member_progress_data import local_day
from .admin_views import MlaiAdminView
from .wrapped_stats import sheet
from .wrapped_stats_data import SCOPES, wrapped_people, year_bounds

logger = logging.getLogger(__name__)
MAX_MEMBERS = 100


class WrappedStatsParameters(serializers.Serializer):
    """Validate the year and selection before reading any member records."""

    year = serializers.IntegerField(min_value=2025)
    scope = serializers.ChoiceField(choices=SCOPES, default="committee")
    member = serializers.ListField(
        child=serializers.UUIDField(), max_length=MAX_MEMBERS, required=False, default=list,
    )

    def validate(self, attrs):
        if attrs["year"] > local_day(timezone.now()).year:
            raise serializers.ValidationError({"year": "That year hasn't started."})
        attrs["member"] = [str(value) for value in attrs["member"]]
        if attrs["member"]:
            attrs["scope"] = "members"
        elif attrs["scope"] == "members":
            raise serializers.ValidationError({"member": "Choose at least one member."})
        return attrs


class AdminWrappedStatsView(MlaiAdminView):
    """Return the media-intel CSV plus the name review an admin confirms before upload."""

    def get(self, request):
        if "member_progress" not in member_progress_capabilities(request.user):
            raise AdminError()
        query = WrappedStatsParameters(data={
            "year": request.query_params.get("year"),
            "scope": request.query_params.get("scope", "committee"),
            "member": request.query_params.getlist("member"),
        })
        query.is_valid(raise_exception=True)
        params = query.validated_data
        now = timezone.now()
        people, selection = wrapped_people(
            params["year"], now, scope=params["scope"], member_ids=params["member"],
        )
        try:
            result = sheet(people)
        except ValueError as error:
            raise serializers.ValidationError({"scope": str(error)}) from None
        logger.info(
            "Wrapped stats exported by account %s: year %s, scope %s, %s rows",
            request.user.pk, params["year"], params["scope"], result["rows"],
        )
        return Response(dict(
            year=params["year"], scope=params["scope"], as_of=year_bounds(params["year"], now)[1],
            selection=selection, **result,
        ))
