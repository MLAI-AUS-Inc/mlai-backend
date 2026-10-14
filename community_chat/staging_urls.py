from django.urls import path
from .staging_views import (StagingStartView,StagingCallbackView,StagingRefreshView,
    StagingAccountView,StagingSessionView,StagingChallengeView,StagingInviteView,StagingConfirmView,StagingDeviceView,
    StagingProfileBatchView,StagingPermissionsView)
from .views import AccountSessionLogoutView, PublicProfileBatchView
from .permission_views import ChatPermissionsView
from .staging_password import StagingPasswordView,StagingLogoutView

urlpatterns=[
    path('auth/password/',StagingPasswordView.as_view(),name='community_chat_password_auth'),
    path('federation/start/',StagingStartView.as_view()),
    path('federation/callback/',StagingCallbackView.as_view()),
    path('account/',StagingAccountView.as_view(),name='community_chat_account'),
    path('auth/session/refresh/',StagingRefreshView.as_view()),
    path('auth/session/logout/',StagingLogoutView.as_view()),
    path('session/',StagingSessionView.as_view()),
    path('profiles/batch/',StagingProfileBatchView.as_view()),
    path('permissions/',StagingPermissionsView.as_view()),
    path('bootstrap/challenge/',StagingChallengeView.as_view()),
    path('bootstrap/invite/',StagingInviteView.as_view()),
    path('bootstrap/confirm/',StagingConfirmView.as_view()),
    path('devices/<str:public_key>/',StagingDeviceView.as_view()),
]
