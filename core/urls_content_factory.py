from django.urls import path
from . import views

urlpatterns = [
    path('org/config/', views.ContentFactoryOrgConfigView.as_view(), name='content_factory_org_config'),
    path('org/config', views.ContentFactoryOrgConfigView.as_view(), name='content_factory_org_config_no_slash'),
    # Component library endpoints
    path('org/components/', views.ContentFactoryComponentsView.as_view(), name='content_factory_components'),
    path('org/components', views.ContentFactoryComponentsView.as_view(), name='content_factory_components_no_slash'),
    path('org/components/<str:name>/', views.ContentFactoryComponentDetailView.as_view(), name='content_factory_component_detail'),
    path('org/components/<str:name>', views.ContentFactoryComponentDetailView.as_view(), name='content_factory_component_detail_no_slash'),
    # GitHub connection status endpoint
    path('org/github-status/', views.ContentFactoryGitHubStatusView.as_view(), name='content_factory_github_status'),
    path('org/github-status', views.ContentFactoryGitHubStatusView.as_view(), name='content_factory_github_status_no_slash'),
    # GitHub OAuth initiate endpoint (for domain-level OAuth)
    path('oauth/initiate/', views.ContentFactoryOAuthInitiateView.as_view(), name='content_factory_oauth_initiate'),
    path('oauth/initiate', views.ContentFactoryOAuthInitiateView.as_view(), name='content_factory_oauth_initiate_no_slash'),
    # Connect GitHub to organization endpoint
    path('org/connect-github/', views.ContentFactoryConnectGitHubView.as_view(), name='content_factory_connect_github'),
    path('org/connect-github', views.ContentFactoryConnectGitHubView.as_view(), name='content_factory_connect_github_no_slash'),
    # Organization domains listing (for fuzzy matching)
    path('orgs/domains/', views.ContentFactoryOrgDomainsView.as_view(), name='content_factory_org_domains'),
    path('orgs/domains', views.ContentFactoryOrgDomainsView.as_view(), name='content_factory_org_domains_no_slash'),
    # Callback endpoint for content-factory events
    path('callback/', views.ContentFactoryCallbackView.as_view(), name='content_factory_callback'),
    path('callback', views.ContentFactoryCallbackView.as_view(), name='content_factory_callback_no_slash'),
    # Token refresh endpoint for content-factory
    path('token/', views.ContentFactoryTokenView.as_view(), name='content_factory_token'),
    path('token', views.ContentFactoryTokenView.as_view(), name='content_factory_token_no_slash'),
]

