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

    Non-admins learn nothing: no flags and no sections.
    """

    admin_only = False

    def get(self, request):
        if not is_mlai_admin_request(request):
            return Response({"is_admin": False, "sections": []})
        return Response({"is_admin": True, "sections": admin_sections(request)})
