from django.urls import path
from . import views

urlpatterns = [
    path(".well-known/openai-apps-challenge", views.DomainVerificationView.as_view(), name="valley_mcp_domain_verification"),
    path("mcp/valley", views.McpView.as_view(), name="valley_mcp"),
    path(".well-known/oauth-protected-resource/mcp/valley", views.ResourceMetadataView.as_view()),
    path(".well-known/oauth-protected-resource", views.ResourceMetadataView.as_view()),
    path(".well-known/oauth-authorization-server", views.AuthorizationMetadataView.as_view()),
    path("mcp/oauth/register", views.RegisterView.as_view()),
    path("mcp/oauth/authorize", views.AuthorizeView.as_view()),
    path("mcp/oauth/token", views.TokenView.as_view()),
    path("mcp/oauth/revoke", views.RevokeView.as_view()),
]
