from django.urls import path

from .views import (
    AnnouncementListView,
    JoinTeamView,
    LatestSubmissionView,
    RecentSubmissionView,
    SubmissionDetailView,
    SubmissionListCreateView,
    TeamListView,
)


urlpatterns = [
    path("teams/", TeamListView.as_view(), name="ica-team-list"),
    path("teams/join/", JoinTeamView.as_view(), name="ica-team-join"),
    path("submissions/", SubmissionListCreateView.as_view(), name="ica-submissions"),
    path("submissions/latest/", LatestSubmissionView.as_view(), name="ica-submissions-latest"),
    path("submissions/recent/", RecentSubmissionView.as_view(), name="ica-submissions-recent"),
    path("submissions/<int:submission_id>/", SubmissionDetailView.as_view(), name="ica-submission-detail"),
    path("announcements/", AnnouncementListView.as_view(), name="ica-announcements"),
]

