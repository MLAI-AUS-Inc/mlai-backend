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
    HomeView,
    InviteView,
    LinkPreviewImageView,
    LinkPreviewView,
    PasswordAuthView,
    PublicProfileBatchView,
    SessionView,
    SlackOriginMessageDeleteView,
    UpcomingEventsView,
)
from .coding_views import (
    CodingEntitlementView,
    CodingJwksView,
    CodingTurnCreateView,
    CodingTurnFinalizeView,
    CodingTurnTicketRefreshView,
)
from .home_views import CommunityHomeView
from .slack_views import SlackDmMirrorView, SlackDmStartView, SlackUserDirectoryView
from .usage_views import (
    TokenUsageHistoryView,
    TokenUsageIngestView,
    TokenUsageLeaderboardView,
    TokenUsageTokenView,
)


urlpatterns = [
    path("home/", CommunityHomeView.as_view(), name="community_chat_home"),
    path("slack/", SlackDmMirrorView.as_view(), name="community_chat_slack"),
    path(
        "slack/users/",
        SlackUserDirectoryView.as_view(),
        name="community_chat_slack_users",
    ),
    path(
        "slack/dms/",
        SlackDmStartView.as_view(),
        name="community_chat_slack_dms",
    ),
    path("coding/jwks/", CodingJwksView.as_view(), name="community_chat_coding_jwks"),
    path(
        "coding/entitlement/",
        CodingEntitlementView.as_view(),
        name="community_chat_coding_entitlement",
    ),
    path(
        "coding/turns/",
        CodingTurnCreateView.as_view(),
        name="community_chat_coding_turn_create",
    ),
    path(
        "coding/turns/<uuid:turn_id>/ticket/refresh/",
        CodingTurnTicketRefreshView.as_view(),
        name="community_chat_coding_turn_ticket_refresh",
    ),
    path(
        "coding/turns/<uuid:turn_id>/finalize/",
        CodingTurnFinalizeView.as_view(),
        name="community_chat_coding_turn_finalize",
    ),
    path("account/", AccountView.as_view(), name="community_chat_account"),
    path("home/", HomeView.as_view(), name="community_chat_home"),
    path(
        "upcoming-events/",
        UpcomingEventsView.as_view(),
        name="community_chat_upcoming_events",
    ),
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
    path(
        "messages/delete-slack-origin/",
        SlackOriginMessageDeleteView.as_view(),
        name="community_chat_delete_slack_origin",
    ),
    path(
        "messages/delete-slack-origin/",
        SlackOriginMessageDeleteView.as_view(),
        name="community_chat_delete_slack_origin",
    ),
    path("bootstrap/challenge/", ChallengeView.as_view(), name="community_chat_challenge"),
    path("bootstrap/invite/", InviteView.as_view(), name="community_chat_invite"),
    path("bootstrap/confirm/", ConfirmView.as_view(), name="community_chat_confirm"),
    path("devices/<str:public_key>/", DeviceView.as_view(), name="community_chat_device"),
    # The tokenmaxer reporter builds "{apiBase}/api/ingest" by plain string
    # concatenation, so these two paths must match literally — no trailing
    # slash, or APPEND_SLASH would 301 the POST and the reporter would record
    # a silent failure.
    path("usage/api/ingest", TokenUsageIngestView.as_view(), name="token_usage_ingest"),
    path("usage/api/history", TokenUsageHistoryView.as_view(), name="token_usage_history"),
    path("usage/token/", TokenUsageTokenView.as_view(), name="token_usage_token"),
    path(
        "usage/leaderboard/",
        TokenUsageLeaderboardView.as_view(),
        name="token_usage_leaderboard",
    ),
]
