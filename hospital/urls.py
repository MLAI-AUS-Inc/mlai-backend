# app/urls.py
from django.urls import path
from . import views
from . import world_views


urlpatterns = [
    path('world/', world_views.WorldStateView.as_view(), name='hospital-world'),
    path('teams/', views.TeamListView.as_view(), name='hospital-team-list'),
    path('teams/join/', views.JoinTeamView.as_view(), name='hospital-team-join'),
    path('submissions/', views.SubmissionListCreateView.as_view(), name='hospital-submissions'),
    path('leaderboard/', views.LeaderboardView.as_view(), name='hospital-leaderboard'),
    path('submit_predictions/', views.submit_predictions, name='submit_predictions'),
    path('get_submission/', views.get_submission, name='get_submission'),
    path('get_recent_submissions/', views.get_recent_submissions, name='get_recent_submissions'),
    path('get_submission/<int:submission_id>/', views.get_submission_by_id, name='get_submission_by_id'),
    path('announcements/', views.AnnouncementListView.as_view(), name='hospital-announcement-list'),
    path('channel/', views.ChannelMessagesView.as_view(), name='hospital-channel'),
    path('channel/thread/<str:thread_ts>/', views.ThreadRepliesView.as_view(), name='hospital-thread'),
]
