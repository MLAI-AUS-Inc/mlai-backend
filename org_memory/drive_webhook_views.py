from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .drive_watch import DriveWatchError, receive_drive_notification


class DriveChangesWebhookView(APIView):
    authentication_classes = ()
    permission_classes = ()

    def post(self, request):
        try:
            result = receive_drive_notification(request.headers)
        except DriveWatchError:
            return Response(status=status.HTTP_401_UNAUTHORIZED)
        return Response(
            {
                "status": result.status,
                "wake_scheduled": result.wake_scheduled,
            },
            status=status.HTTP_202_ACCEPTED,
        )
