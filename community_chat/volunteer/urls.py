"""Volunteer routes under the existing authenticated community-chat API."""

from django.urls import path
from . import views
from roo.manual_time_views import ChatManualTimeView

urlpatterns = [
    path("manual-time/", ChatManualTimeView.as_view()),
    path("manual-time/<int:request_id>/", ChatManualTimeView.as_view()),
    path("journey/", views.JourneyView.as_view()),
    path("point-history/", views.PointHistoryView.as_view()),
    path("policy/", views.PolicyView.as_view()),
    path("opportunities/", views.OpportunitiesView.as_view()),
    path("opportunities/<uuid:opportunity_id>/", views.OpportunitiesView.as_view()),
    path("projects/", views.ProjectsView.as_view()),
    path("projects/<uuid:project_id>/", views.ProjectsView.as_view()),
    path("contributions/", views.ContributionsView.as_view()),
    path("contributions/<uuid:contribution_id>/", views.ContributionsView.as_view()),
    path(
        "contributions/<uuid:contribution_id>/<str:operation>/",
        views.ReviseView.as_view(),
    ),
    path("requests/", views.RequestsView.as_view()),
    path("manage/reviews/", views.ReviewsView.as_view()),
    path("manage/contributions/<uuid:contribution_id>/", views.DecisionView.as_view()),
    path(
        "manage/contributions/<uuid:contribution_id>/decision/",
        views.DecisionView.as_view(),
    ),
    path("manage/recognitions/", views.DirectRecognitionView.as_view()),
    path(
        "manage/events/<str:event_id>/recognitions/",
        views.EventRecognitionsView.as_view(),
    ),
    path("manage/members/", views.MembersView.as_view()),
    path("manage/opportunities/", views.ManageOpportunitiesView.as_view()),
    path(
        "manage/opportunities/<uuid:opportunity_id>/",
        views.ManageOpportunitiesView.as_view(),
    ),
    path("manage/projects/", views.ManageProjectsView.as_view()),
    path("manage/projects/<uuid:project_id>/", views.ManageProjectsView.as_view()),
    path("manage/attendance/", views.AttendanceView.as_view()),
    path("manage/reconcile/", views.ReconciliationView.as_view()),
    path("internal/receipts/", views.ReceiptView.as_view()),
]
