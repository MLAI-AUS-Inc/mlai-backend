from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from roo.models import RooPointsMutePreference

from .authentication import CommunityChatAccountAuthentication
from .throttles import CommunityChatScopedThrottle


class RooPointsMuteView(APIView):
    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_roo_points_mute"

    def get(self, request):
        preference = RooPointsMutePreference.objects.filter(user=request.user).first()
        return self.preference_response(preference)

    def put(self, request):
        muted = request.data.get("muted")
        initialize = request.data.get("initialize", False)
        if type(muted) is not bool or type(initialize) is not bool:
            return Response({"error": "Expected boolean mute settings."}, status=400)
        save = RooPointsMutePreference.objects.get_or_create if initialize else RooPointsMutePreference.objects.update_or_create
        preference, _ = save(user=request.user, defaults={"muted": muted})
        return self.preference_response(preference)

    @staticmethod
    def preference_response(preference):
        response = Response({"muted": preference.muted if preference else True, "explicit": preference is not None})
        response["Cache-Control"] = "no-store"
        return response
