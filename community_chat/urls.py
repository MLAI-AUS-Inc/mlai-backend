from django.urls import path

from .views import (
    AccountSessionLogoutView,
    AccountSessionRefreshView,
    AccountView,
    ChallengeView,
    ConfirmView,
    DeviceAuthAuthorizeView,
    DeviceAuthExchangeView,
    DeviceAuthStartView,
    EmailCodeRequestView,
    EmailCodeVerifyView,
    DeviceView,
    InviteView,
    LinkPreviewImageView,
    LinkPreviewView,
    PasswordAuthView,
    PublicProfileBatchView,
    SessionView,
)


urlpatterns = [
    path("account/", AccountView.as_view(), name="community_chat_account"),
    path(
        "profiles/batch/",
        PublicProfileBatchView.as_view(),
        name="community_chat_public_profiles_batch",
    ),
    path("link-preview/", LinkPreviewView.as_view(), name="community_chat_link_preview"),
    path(
        "link-preview/image/",
        LinkPreviewImageView.as_view(),
        name="community_chat_link_preview_image",
    ),
    path("auth/session/refresh/", AccountSessionRefreshView.as_view(), name="community_chat_session_refresh"),
    path("auth/session/logout/", AccountSessionLogoutView.as_view(), name="community_chat_session_logout"),
    path("auth/email-code/request/", EmailCodeRequestView.as_view(), name="community_chat_email_code_request"),
    path("auth/email-code/verify/", EmailCodeVerifyView.as_view(), name="community_chat_email_code_verify"),
    path("auth/password/", PasswordAuthView.as_view(), name="community_chat_password_auth"),
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
