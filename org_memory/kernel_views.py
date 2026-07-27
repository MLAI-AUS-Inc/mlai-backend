from rest_framework import status
from rest_framework.response import Response

from .kernel import kernel_health_snapshot
from .source_views import OrgMemorySourceControlView


class MemoryKernelHealthView(OrgMemorySourceControlView):
    """Organisation-scoped operational health; never returns source content."""

    def get(self, request):
        payload = kernel_health_snapshot(
            organization=request.org_memory_actor.organization,
        )
        response_status = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if payload["status"] == "error"
            else status.HTTP_200_OK
        )
        return Response(payload, status=response_status)
