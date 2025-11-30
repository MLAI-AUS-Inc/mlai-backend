from django.urls import path
from .views import TeamListView, JoinTeamView, submit_predictions, get_submission, LeaderboardView, AnnouncementListView

urlpatterns = [
    path('teams/', TeamListView.as_view(), name='esafety-team-list'),
    path('teams/join/', JoinTeamView.as_view(), name='esafety-team-join'),
    path('submissions/', submit_predictions, name='esafety-submission'),
    path('submission/', get_submission, name='esafety-get-submission'),
    path('leaderboard/', LeaderboardView.as_view(), name='esafety-leaderboard'),
    path('announcements/', AnnouncementListView.as_view(), name='esafety-announcement-list'),
]
