"""MLAI Chat's Admin area: the shared base view and ``admin/access/``.

Every Admin endpoint subclasses ``MlaiAdminView``, which answers 403
``not_authorised`` to anyone who isn't an MLAI admin on a verified device
installation (``admin_access.is_mlai_admin_request``). Only the access
endpoints set ``admin_only = False``. Not Django's ``/admin/`` site.
"""

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.authentication import CustomJWTAuthentication

from .admin_access import AdminError, admin_sections, is_mlai_admin_request
from .authentication import CommunityChatAccountAuthentication
from .throttles import CommunityChatScopedThrottle


class MlaiAdminView(APIView):
    """Volunteer's account-session and JWT contracts, with the MLAI admin gate."""

    authentication_classes = (
        CommunityChatAccountAuthentication,
        CustomJWTAuthentication,
    )
    permission_classes = (IsAuthenticated,)
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_admin"
    admin_only = True

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if self.admin_only and not is_mlai_admin_request(request):
            raise AdminError()

    def handle_exception(self, exc):
        if isinstance(exc, AdminError):
            return Response({"error": exc.code, "outcome": exc.code}, status=exc.status)
        return super().handle_exception(exc)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response


class AdminAccessView(MlaiAdminView):
    """Always 200 for a signed-in account, so the client can hide the Admin tab quietly.

    The general administrator role is separate from permission to use a
    specific section such as Mentoring.
    """

    admin_only = False

    def get(self, request):
        from .mentoring_views import is_mentoring_admin, mentor_booking_enabled

        is_admin = is_mlai_admin_request(request)
        sections = admin_sections(request) if is_admin else []
        if (
            mentor_booking_enabled()
            and getattr(request, "community_chat_account_session", None) is not None
            and is_mentoring_admin(request)
        ):
            sections.append("mentoring")
        return Response({"is_admin": is_admin, "sections": sections})


class AdminTeamView(MlaiAdminView):
    """``admin/team/``: list everyone with admin access (GET), add someone (POST)."""

    def get(self, request):
        from . import admin_team

        return Response(admin_team.list_team(request.user))

    def post(self, request):
        from . import admin_team

        data = request.data if isinstance(request.data, dict) else {}
        try:
            member = admin_team.grant(
                request.user, data.get("account_id"), data.get("role"), data.get("team"),
            )
        except admin_team.TeamError as exc:
            return Response({"error": exc.code}, status=exc.status)
        return Response({"member": member})


class AdminTeamMemberView(MlaiAdminView):
    """``admin/team/<id>/``: change a role or team (PATCH), remove someone (DELETE)."""

    def patch(self, request, member_id):
        from . import admin_team

        data = request.data if isinstance(request.data, dict) else {}
        changes = {key: data[key] for key in ("role", "team") if key in data}
        try:
            member = admin_team.update(request.user, member_id, **changes)
        except admin_team.TeamError as exc:
            return Response({"error": exc.code}, status=exc.status)
        return Response({"member": member})

    def delete(self, request, member_id):
        from . import admin_team

        try:
            admin_team.remove(request.user, member_id)
        except admin_team.TeamError as exc:
            return Response({"error": exc.code}, status=exc.status)
        return Response({"removed": True})


class AdminTeamSearchView(MlaiAdminView):
    """``admin/team/search/?q=``: accounts to add, for those who may manage."""

    def get(self, request):
        from . import admin_team

        try:
            results = admin_team.search_accounts(request.user, request.query_params.get("q", ""))
        except admin_team.TeamError as exc:
            return Response({"error": exc.code}, status=exc.status)
        return Response({"results": results})
