from django.urls import path
from . import views
from .mcp import MCPView
from .model_proxy import model_chat_completions
from . import native_views

urlpatterns = [
    path("apps/", native_views.NativeAppsView.as_view()),
    path("installations/", native_views.NativeInstallationsView.as_view()),
    path("enrollments/", native_views.NativeEnrollmentView.as_view()),
    path("enrollments/<uuid:enrollment_id>/", native_views.NativeEnrollmentDetailView.as_view()),
    path("enrollments/<uuid:enrollment_id>/confirm/", native_views.NativeEnrollmentConfirmView.as_view()),
    path("installations/<uuid:installation_id>/state/", native_views.NativeInstallationStateView.as_view()),
    path("internal/native/registry/<str:public_key>/", native_views.NativeRegistryView.as_view()),
    path("connected/", views.ConnectedAgentsView.as_view(), name="agent-connected"),
    path("channels/", views.EligibleChannelsView.as_view()),
    path("grants/<uuid:grant_id>/revoke/", views.RevokeGrantView.as_view()),
    path("installations/<uuid:installation_id>/revoke/", views.RevokeInstallationView.as_view()),
    path("grants/<uuid:grant_id>/tasks/", views.OwnerTasksView.as_view()),
    path("grants/<uuid:grant_id>/responsibilities/", views.ResponsibilitiesView.as_view()),
    path("grants/<uuid:grant_id>/subscriptions/", views.SubscriptionsView.as_view()),
    path("responsibilities/<uuid:responsibility_id>/stop/", views.StopResponsibilityView.as_view()),
    path("subscriptions/<uuid:subscription_id>/stop/", views.StopSubscriptionView.as_view()),
    path("oauth/authorize/", views.AuthorizeView.as_view()),
    path("oauth/token/", views.TokenView.as_view()),
    path("tools/", views.ToolsView.as_view()),
    path("mcp/", MCPView.as_view()),
    path("tasks/", views.TasksView.as_view()),
    path("tasks/<uuid:task_id>/cancel/", views.CancelTaskView.as_view()),
    path("actions/<uuid:action_id>/", views.ActionApprovalView.as_view()),
    path("artifacts/<uuid:artifact_id>/", views.ArtifactView.as_view()),
    path("internal/tasks/claim/", views.ClaimTaskView.as_view()),
    path("internal/tasks/admit-slack/", views.SlackAdmissionView.as_view()),
    path("internal/tasks/<uuid:task_id>/heartbeat/", views.HeartbeatTaskView.as_view()),
    path("internal/tasks/<uuid:task_id>/complete/", views.CompleteTaskView.as_view()),
    path("internal/tasks/<uuid:task_id>/fail/", views.FailTaskView.as_view()),
    path("internal/events/", views.IngestEventView.as_view()),
    path("model/v1/chat/completions", model_chat_completions),
]
