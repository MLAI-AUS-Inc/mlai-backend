"""Service-authenticated read authority and result receipt for repo-health builds."""
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRooApiKey
from .repo_health_operations import ProbeError, receive


class RepoHealthProbeView(APIView):
    """Validate a probe against the selected installation, repository and current branch head."""

    authentication_classes = []
    permission_classes = [HasRooApiKey]
    complete = False

    def post(self, request):
        try:
            return Response(receive(request.data, complete=self.complete))
        except ProbeError as exc:
            return Response({"reason": str(exc)}, status=exc.status)
