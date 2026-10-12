from django.conf import settings
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import PermissionDenied

from .authentication import CommunityChatAccountAuthentication
from . import federation
from .throttles import enforce_dimension_limit, client_ip


class FederationIssuerView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get_authenticate_header(self, request):
        return "Bearer"

    def initial(self, request, *args, **kwargs):
        federation.require_issuer()
        super().initial(request, *args, **kwargs)
        enforce_dimension_limit(action='federation', dimension='ip', value=client_ip(request),
                                limit=1000 if getattr(settings, 'TESTING', False) else 60, window_seconds=60)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store'
        response['Referrer-Policy'] = 'no-referrer'
        return response


class FederationRequestView(FederationIssuerView):
    def post(self, request):
        federation.require_client(request)
        return Response(federation.create_request(request.data), status=201)


class FederationAuthorizeView(FederationIssuerView):
    authentication_classes = [CommunityChatAccountAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        origin = str(request.headers.get('Origin') or '').rstrip('/')
        if origin != settings.COMMUNITY_CHAT_FRONTEND_URL.rstrip('/'):
            raise PermissionDenied('Production account approval origin is required.')
        return Response(federation.authorize_request(request.data.get('request_id'),
                        request.community_chat_account_session))


class FederationTokenView(FederationIssuerView):
    def post(self, request):
        federation.require_client(request)
        return Response(federation.exchange_code(request.data))


class FederationEligibilityView(FederationIssuerView):
    def post(self, request):
        federation.require_client(request)
        return Response(federation.check_subject(request.data.get('subject')))
