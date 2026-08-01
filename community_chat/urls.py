from django.urls import path

from .views import (
    ChallengeView,
    ConfirmView,
    DeviceAuthAuthorizeView,
    DeviceAuthExchangeView,
    DeviceAuthStartView,
    DeviceView,
    InviteView,
    SessionView,
)


urlpatterns = [
    path("auth/device/start/", DeviceAuthStartView.as_view(), name="community_chat_auth_start"),
    path(
        "auth/device/authorize/",
        DeviceAuthAuthorizeView.as_view(),
        name="community_chat_auth_authorize",
    ),
    path(
        "auth/device/exchange/",
        DeviceAuthExchangeView.as_view(),
        name="community_chat_auth_exchange",
    ),
    path("session/", SessionView.as_view(), name="community_chat_session"),
    path("bootstrap/challenge/", ChallengeView.as_view(), name="community_chat_challenge"),
    path("bootstrap/invite/", InviteView.as_view(), name="community_chat_invite"),
    path("bootstrap/confirm/", ConfirmView.as_view(), name="community_chat_confirm"),
    path("devices/<str:public_key>/", DeviceView.as_view(), name="community_chat_device"),
]
