# app/urls.py
from django.urls import path
from . import views


urlpatterns = [
    path('submit_predictions/', views.submit_predictions, name='submit_predictions'),
    path('get_submission/', views.get_submission, name='get_submission'),
    path('get_recent_submissions/', views.get_recent_submissions, name='get_recent_submissions'),
    path('send-magic-link/', views.SendMagicLinkView.as_view(), name='send_magic_link'),
    path('verify-magic-link/', views.MagicLinkVerifyView.as_view(), name='verify_magic_link'),
    path('token/refresh/', views.CookieTokenRefreshView.as_view(), name='token_refresh'),
    path('get_current_user/', views.CurrentUserView.as_view(), name='get_current_user'),
    path('update_user/', views.UpdateProfileView.as_view(), name='update_user'),
    path('get_submission/<int:submission_id>/', views.get_submission_by_id, name='get_submission_by_id'),
    # path('get_leaderboard_submissions/', views.get_leaderboard_submissions, name='get_leaderboard_submissions'),
    path('get_team_names/', views.get_team_names, name='get_team_names'),
    path('logout/', views.logout_view, name='logout'),
]
