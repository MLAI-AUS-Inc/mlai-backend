from django.urls import path

from .views import ChallengeView, ConfirmView, DeviceView, InviteView, SessionView


urlpatterns = [
    path("session/", SessionView.as_view(), name="community_chat_session"),
    path("bootstrap/challenge/", ChallengeView.as_view(), name="community_chat_challenge"),
    path("bootstrap/invite/", InviteView.as_view(), name="community_chat_invite"),
    path("bootstrap/confirm/", ConfirmView.as_view(), name="community_chat_confirm"),
    path("devices/<str:public_key>/", DeviceView.as_view(), name="community_chat_device"),
]

