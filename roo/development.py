from django.conf import settings
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import NotFound

class DevelopmentBuildView(APIView):
    """Read-only developer receipt; ordinary domain route, stage only."""
    permission_classes=(IsAuthenticated,)
    def get(self,request):
        if settings.APP_ENV!='chat-staging':raise NotFound()
        return Response({'environment':settings.APP_ENV,'release':settings.APP_RELEASE})
