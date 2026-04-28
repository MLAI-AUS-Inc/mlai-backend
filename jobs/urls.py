from django.urls import path

from . import views


urlpatterns = [
    path("daily-run", views.DailyRunTriggerView.as_view(), name="jobs_daily_run"),
    path("runs/<str:run_id>", views.JobRunDetailView.as_view(), name="jobs_run_detail"),
    path("runs/<str:run_id>/slack-payload", views.JobRunSlackPayloadView.as_view(), name="jobs_run_slack_payload"),
    path("daily/<str:run_date>/json", views.DailyJobsJsonView.as_view(), name="jobs_daily_json"),
    path("daily/<str:run_date>", views.DailyJobsHtmlView.as_view(), name="jobs_daily_html"),
]

