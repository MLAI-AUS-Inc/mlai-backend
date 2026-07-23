import logging

from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from .emails import send_registration_confirmation
from .models import VictorApplication
from .serializers import VictorApplicationSerializer


logger = logging.getLogger(__name__)


class VictorApplicationSubmitView(APIView):
    """Public endpoint the victorai.win registration form posts to.

    Upserts by ``client_ref`` so the two-step form can save a ``lead`` row at
    step 1 and upgrade it to ``complete`` at step 2 without duplicating.
    """

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [AnonRateThrottle]

    def post(self, request):
        client_ref = str(request.data.get('client_ref') or '').strip()
        if not client_ref:
            return Response({'detail': 'client_ref is required'}, status=status.HTTP_400_BAD_REQUEST)

        instance = VictorApplication.objects.filter(client_ref=client_ref).first()
        was_complete = instance is not None and instance.stage == VictorApplication.STAGE_COMPLETE
        data = {key: value for key, value in request.data.items()}
        if was_complete:
            # Never downgrade a completed registration back to lead.
            data['stage'] = VictorApplication.STAGE_COMPLETE

        serializer = VictorApplicationSerializer(instance, data=data, partial=instance is not None)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        application = serializer.save()
        if not was_complete and application.stage == VictorApplication.STAGE_COMPLETE:
            try:
                send_registration_confirmation(application)
            except Exception:
                # Saving the registration is the source of truth. A provider
                # outage should be visible in logs without making the applicant
                # resubmit and risk creating conflicting data.
                logger.exception(
                    'Failed to send Victor:AI registration confirmation for application %s',
                    application.pk,
                )
        return Response(
            {'client_ref': application.client_ref, 'stage': application.stage},
            status=status.HTTP_200_OK if instance is not None else status.HTTP_201_CREATED,
        )
