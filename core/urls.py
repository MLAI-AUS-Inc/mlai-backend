from django.urls import path
from . import views
from .auth_views import PasswordChangeView, PasswordResetConfirmView, PasswordResetRequestView

urlpatterns = [
    path('check-user/', views.CheckUserView.as_view(), name='check_user'),
    path('send-magic-link/', views.SendMagicLinkView.as_view(), name='send_magic_link'),
    path('create-user/', views.CreateUserView.as_view(), name='create_user'),
    path('verify-magic-link/', views.MagicLinkVerifyView.as_view(), name='verify_magic_link'),
    path('token/refresh/', views.CookieTokenRefreshView.as_view(), name='token_refresh'),
    path('password/reset/request/', PasswordResetRequestView.as_view(), name='password_reset_request'),
    path('password/reset/confirm/', PasswordResetConfirmView.as_view(), name='password_reset_confirm'),
    path('password/change/', PasswordChangeView.as_view(), name='password_change'),
    path('get_current_user/', views.CurrentUserView.as_view(), name='get_current_user'),
    path('me/', views.CurrentUserView.as_view(), name='current_user'),
    path('update_user/', views.UpdateProfileView.as_view(), name='update_user'),
    path('update-profile/', views.UpdateProfileView.as_view(), name='update_profile'),
    path('logout/', views.logout_view, name='logout'),
    path('users/<int:pk>/', views.UserDetailView.as_view(), name='user_detail'),
    path('users/slack-user/', views.GetOrCreateSlackUserView.as_view(), name='get_or_create_slack_user'),
]
