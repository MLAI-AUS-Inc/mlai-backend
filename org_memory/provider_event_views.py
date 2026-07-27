from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .provider_events import (
    ProviderEventError,
    receive_gmail_push,
    receive_linear_event,
    receive_notion_event,
    receive_slack_event,
    receive_xero_event,
)


class ProviderEventWebhookView(APIView):
    authentication_classes = ()
    permission_classes = ()

    receiver = None

    def post(self, request):
        try:
            result = self.receiver(request.headers, request.body)
        except ProviderEventError:
            return Response(status=status.HTTP_401_UNAUTHORIZED)
        if result.challenge:
            return Response({"challenge": result.challenge}, status=status.HTTP_200_OK)
        return Response(
            {
                "status": result.status,
                "wake_scheduled": result.wake_scheduled,
            },
            status=status.HTTP_200_OK,
        )


class LinearMemoryWebhookView(ProviderEventWebhookView):
    receiver = staticmethod(receive_linear_event)


class SlackMemoryWebhookView(ProviderEventWebhookView):
    receiver = staticmethod(receive_slack_event)


class NotionMemoryWebhookView(ProviderEventWebhookView):
    receiver = staticmethod(receive_notion_event)


class XeroMemoryWebhookView(ProviderEventWebhookView):
    receiver = staticmethod(receive_xero_event)


class GmailMemoryPushView(ProviderEventWebhookView):
    receiver = staticmethod(receive_gmail_push)
