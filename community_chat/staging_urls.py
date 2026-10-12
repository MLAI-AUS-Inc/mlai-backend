from django.urls import path
from .staging_views import (StagingStartView,StagingCallbackView,StagingRefreshView,
    StagingAccountView,StagingSessionView,StagingChallengeView,StagingInviteView,StagingConfirmView,StagingDeviceView)
from .views import AccountSessionLogoutView, PublicProfileBatchView
from .permission_views import ChatPermissionsView

urlpatterns=[
    path('federation/start/',StagingStartView.as_view()),
    path('federation/callback/',StagingCallbackView.as_view()),
    path('account/',StagingAccountView.as_view(),name='community_chat_account'),
    path('auth/session/refresh/',StagingRefreshView.as_view()),
    path('auth/session/logout/',AccountSessionLogoutView.as_view()),
    path('session/',StagingSessionView.as_view()),
    path('profiles/batch/',PublicProfileBatchView.as_view()),
    path('permissions/',ChatPermissionsView.as_view()),
    path('bootstrap/challenge/',StagingChallengeView.as_view()),
    path('bootstrap/invite/',StagingInviteView.as_view()),
    path('bootstrap/confirm/',StagingConfirmView.as_view()),
    path('devices/<str:public_key>/',StagingDeviceView.as_view()),
]
