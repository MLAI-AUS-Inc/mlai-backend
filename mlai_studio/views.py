from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from .models import StudioApplication
from .serializers import StudioApplicationSerializer


class StudioApplicationSubmitView(APIView):
    """Public submit endpoint for the MLAI Studio landing-page form.

    POST-only. Upserts on ``client_ref`` so the form can save a partial lead
    after step 1 and replace it with the full application on submit. There is
    deliberately no read access — applications are reviewed in the Django
    admin.
    """

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [AnonRateThrottle]

    def post(self, request):
        client_ref = str(request.data.get('client_ref') or '').strip()
        if not client_ref:
            return Response({'detail': 'client_ref is required'}, status=status.HTTP_400_BAD_REQUEST)

        instance = StudioApplication.objects.filter(client_ref=client_ref).first()
        data = {key: value for key, value in request.data.items()}
        # A late lead-stage save (e.g. a replayed step-1 request) must never
        # downgrade an application that was already submitted in full.
        if instance is not None and instance.stage == StudioApplication.STAGE_COMPLETE:
            data['stage'] = StudioApplication.STAGE_COMPLETE

        serializer = StudioApplicationSerializer(instance, data=data, partial=instance is not None)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        application = serializer.save()

        # Echo only non-personal fields back to the public caller.
        return Response(
            {'client_ref': application.client_ref, 'stage': application.stage},
            status=status.HTTP_200_OK if instance is not None else status.HTTP_201_CREATED,
        )
