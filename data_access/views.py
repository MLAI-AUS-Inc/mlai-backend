from django.db import connection, transaction
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRooApiKey

from .registry import build_actor, get_resource, list_resources
from .resolvers import DataAccessError, DataAccessPermissionDenied
from .serializers import DataCatalogSerializer, DataQuerySerializer


class DataCatalogView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        serializer = DataCatalogSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        actor = build_actor(serializer.validated_data["requester_slack_id"])
        return Response({"resources": list_resources(actor)})


class DataQueryView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        serializer = DataQuerySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        resource = get_resource(payload["resource"])
        if resource is None:
            return Response(
                {"error": f"Unknown resource `{payload['resource']}`."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        actor = build_actor(payload["requester_slack_id"])
        try:
            with transaction.atomic():
                self._set_transaction_guards()
                result = resource.execute(actor, payload)
        except DataAccessPermissionDenied as exc:
            return Response({"error": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except DataAccessError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(result)

    def _set_transaction_guards(self):
        if connection.vendor != "postgresql":
            return
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("SET LOCAL statement_timeout = '30s'")
