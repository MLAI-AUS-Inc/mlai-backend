# app/urls.py
from django.urls import path
from . import views
from . import world_views
from . import sim_contest_views
from . import sim_patient_views
from .announcement_views import HealthHackAnnouncementListCreateView


urlpatterns = [
    path('world/', world_views.WorldStateView.as_view(), name='hospital-world'),
    path('sim-guess/check/', sim_contest_views.SimGuessCheckView.as_view(), name='hospital-sim-guess-check'),
    path('sim-guess/status/', sim_contest_views.SimGuessStatusView.as_view(), name='hospital-sim-guess-status'),
    path('sim-guess/record/', sim_contest_views.SimGuessRecordView.as_view(), name='hospital-sim-guess-record'),
    path('sim-guess/claim/', sim_contest_views.SimGuessClaimView.as_view(), name='hospital-sim-guess-claim'),
    path('sim-patient/', sim_patient_views.SimPatientProxyView.as_view(), name='hospital-sim-patient'),
    path('teams/', views.TeamListView.as_view(), name='hospital-team-list'),
    path('teams/join/', views.JoinTeamView.as_view(), name='hospital-team-join'),
    path('submissions/', views.SubmissionListCreateView.as_view(), name='hospital-submissions'),
    path('leaderboard/', views.LeaderboardView.as_view(), name='hospital-leaderboard'),
    path('submit_predictions/', views.submit_predictions, name='submit_predictions'),
    path('get_submission/', views.get_submission, name='get_submission'),
    path('get_recent_submissions/', views.get_recent_submissions, name='get_recent_submissions'),
    path('get_submission/<int:submission_id>/', views.get_submission_by_id, name='get_submission_by_id'),
    path('announcements/', HealthHackAnnouncementListCreateView.as_view(), name='hospital-announcement-list'),
    path('channel/', views.ChannelMessagesView.as_view(), name='hospital-channel'),
    path('channel/thread/<str:thread_ts>/', views.ThreadRepliesView.as_view(), name='hospital-thread'),
]
