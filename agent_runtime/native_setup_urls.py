"""Human setup and private classification only; providers use /mcp/mlai."""
from django.urls import path
from . import native_views, views

urlpatterns = [
    path("apps/", native_views.NativeAppsView.as_view()),
    path("installations/", native_views.NativeInstallationsView.as_view()),
    path("enrollments/", native_views.NativeEnrollmentView.as_view()),
    path("enrollments/<uuid:enrollment_id>/", native_views.NativeEnrollmentDetailView.as_view()),
    path("enrollments/<uuid:enrollment_id>/confirm/", native_views.NativeEnrollmentConfirmView.as_view()),
    path("installations/<uuid:installation_id>/state/", native_views.NativeInstallationStateView.as_view()),
    path("installations/<uuid:installation_id>/revoke/", views.RevokeInstallationView.as_view()),
    path("internal/native/registry/<str:public_key>/", native_views.NativeRegistryView.as_view()),
]
