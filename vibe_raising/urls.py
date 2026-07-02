from django.urls import path

from .views import (
    VibeRaisingActiveCompanyView,
    VibeRaisingCompanyView,
    VibeRaisingDraftView,
    VibeRaisingEmailDraftActiveRunView,
    VibeRaisingEmailDraftCancelView,
    VibeRaisingEmailDraftLatestView,
    VibeRaisingEmailDraftResultsView,
    VibeRaisingEmailDraftStartView,
    VibeRaisingEmailDraftStatusView,
    VibeRaisingManualDocumentDetailView,
    VibeRaisingManualDocumentDownloadView,
    VibeRaisingManualDocumentListView,
    VibeRaisingManualDocumentUploadCompleteView,
    VibeRaisingManualDocumentUploadSessionView,
    VibeRaisingMonthlyUpdatePublishView,
    VibeRaisingMonthlyUpdateView,
    VibeRaisingProfileView,
    VibeRaisingStartupUpdateBootstrapView,
    VibeRaisingStartupUpdateRunView,
    VibeRaisingStartupUpdateStatusView,
    VibeRaisingVideoUploadCompleteView,
    VibeRaisingVideoUploadSessionView,
    VibeRaisingVideoUploadView,
)
from .admin_views import (
    VibeRaisingAdminOverviewView,
    VibeRaisingAdminUpdateDetailView,
    VibeRaisingAdminUpdatesView,
)


urlpatterns = [
    path("admin/overview/", VibeRaisingAdminOverviewView.as_view(), name="vibe-raising-admin-overview"),
    path("admin/updates/", VibeRaisingAdminUpdatesView.as_view(), name="vibe-raising-admin-updates"),
    path(
        "admin/updates/<int:update_id>/",
        VibeRaisingAdminUpdateDetailView.as_view(),
        name="vibe-raising-admin-update-detail",
    ),
    path("profile/", VibeRaisingProfileView.as_view(), name="vibe-raising-profile"),
    path("companies/", VibeRaisingCompanyView.as_view(), name="vibe-raising-companies"),
    path("active-company/", VibeRaisingActiveCompanyView.as_view(), name="vibe-raising-active-company"),
    path("drafts/", VibeRaisingDraftView.as_view(), name="vibe-raising-drafts"),
    path("updates/", VibeRaisingMonthlyUpdateView.as_view(), name="vibe-raising-updates"),
    path(
        "updates/<int:update_id>/publish/",
        VibeRaisingMonthlyUpdatePublishView.as_view(),
        name="vibe-raising-update-publish",
    ),
    path("uploads/video/", VibeRaisingVideoUploadView.as_view(), name="vibe-raising-video-upload"),
    path("uploads/video/session/", VibeRaisingVideoUploadSessionView.as_view(), name="vibe-raising-video-upload-session"),
    path("uploads/video/complete/", VibeRaisingVideoUploadCompleteView.as_view(), name="vibe-raising-video-upload-complete"),
    path(
        "uploads/manual-documents/",
        VibeRaisingManualDocumentListView.as_view(),
        name="vibe-raising-manual-documents",
    ),
    path(
        "uploads/manual-documents/session/",
        VibeRaisingManualDocumentUploadSessionView.as_view(),
        name="vibe-raising-manual-document-upload-session",
    ),
    path(
        "uploads/manual-documents/complete/",
        VibeRaisingManualDocumentUploadCompleteView.as_view(),
        name="vibe-raising-manual-document-upload-complete",
    ),
    path(
        "uploads/manual-documents/<uuid:document_id>/",
        VibeRaisingManualDocumentDetailView.as_view(),
        name="vibe-raising-manual-document-detail",
    ),
    path(
        "uploads/manual-documents/<uuid:document_id>/download/",
        VibeRaisingManualDocumentDownloadView.as_view(),
        name="vibe-raising-manual-document-download",
    ),
    path(
        "startup-update/bootstrap/",
        VibeRaisingStartupUpdateBootstrapView.as_view(),
        name="vibe-raising-startup-update-bootstrap",
    ),
    path(
        "startup-update/run/",
        VibeRaisingStartupUpdateRunView.as_view(),
        name="vibe-raising-startup-update-run",
    ),
    path(
        "startup-update/status/",
        VibeRaisingStartupUpdateStatusView.as_view(),
        name="vibe-raising-startup-update-status",
    ),
    path(
        "email-draft/start/",
        VibeRaisingEmailDraftStartView.as_view(),
        name="vibe-raising-email-draft-start",
    ),
    path(
        "email-draft/status/",
        VibeRaisingEmailDraftStatusView.as_view(),
        name="vibe-raising-email-draft-status",
    ),
    path(
        "email-draft/active-run/",
        VibeRaisingEmailDraftActiveRunView.as_view(),
        name="vibe-raising-email-draft-active-run",
    ),
    path(
        "email-draft/runs/<str:run_id>/status/",
        VibeRaisingEmailDraftStatusView.as_view(),
        name="vibe-raising-email-draft-run-status",
    ),
    path(
        "email-draft/runs/<str:run_id>/cancel/",
        VibeRaisingEmailDraftCancelView.as_view(),
        name="vibe-raising-email-draft-run-cancel",
    ),
    path(
        "email-draft/draft-results/",
        VibeRaisingEmailDraftResultsView.as_view(),
        name="vibe-raising-email-draft-draft-results-latest",
    ),
    path(
        "email-draft/runs/<str:run_id>/draft-results/",
        VibeRaisingEmailDraftResultsView.as_view(),
        name="vibe-raising-email-draft-draft-results",
    ),
    path(
        "email-draft/latest/",
        VibeRaisingEmailDraftLatestView.as_view(),
        name="vibe-raising-email-draft-latest",
    ),
]
