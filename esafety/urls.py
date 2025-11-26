from django.urls import path
from .views import TeamListView, JoinTeamView, SubmissionView, LeaderboardView

urlpatterns = [
    path('teams/', TeamListView.as_view(), name='esafety-team-list'),
    path('teams/join/', JoinTeamView.as_view(), name='esafety-team-join'),
    path('submissions/', SubmissionView.as_view(), name='esafety-submission'),
    path('leaderboard/', LeaderboardView.as_view(), name='esafety-leaderboard'),
]
