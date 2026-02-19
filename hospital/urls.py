# app/urls.py
from django.urls import path
from . import views


urlpatterns = [
    path('teams/', views.TeamListView.as_view(), name='hospital-team-list'),
    path('teams/join/', views.JoinTeamView.as_view(), name='hospital-team-join'),
    path('submissions/', views.SubmissionListCreateView.as_view(), name='hospital-submissions'),
    path('leaderboard/', views.LeaderboardView.as_view(), name='hospital-leaderboard'),
    path('submit_predictions/', views.submit_predictions, name='submit_predictions'),
    path('get_submission/', views.get_submission, name='get_submission'),
    path('get_recent_submissions/', views.get_recent_submissions, name='get_recent_submissions'),
    path('get_submission/<int:submission_id>/', views.get_submission_by_id, name='get_submission_by_id'),
    path('get_team_names/', views.get_team_names, name='get_team_names'),
    path('announcements/', views.AnnouncementListView.as_view(), name='hospital-announcement-list'),
    path('channel/', views.ChannelMessagesView.as_view(), name='hospital-channel'),
    path('channel/thread/<str:thread_ts>/', views.ThreadRepliesView.as_view(), name='hospital-thread'),
]
