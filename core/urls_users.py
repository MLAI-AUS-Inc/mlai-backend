from django.urls import path
from . import views

urlpatterns = [
    path('link-slack/', views.LinkSlackView.as_view(), name='link_slack'),
    path('slack-user/', views.GetOrCreateSlackUserView.as_view(), name='get_or_create_slack_user'),
    path(
        'slack-founder-link/start/',
        views.SlackFounderLinkStartView.as_view(),
        name='slack_founder_link_start',
    ),
    path(
        'slack-founder-link/status/',
        views.SlackFounderLinkStatusView.as_view(),
        name='slack_founder_link_status',
    ),
    path(
        'slack-founder-link/preview/',
        views.SlackFounderLinkPreviewView.as_view(),
        name='slack_founder_link_preview',
    ),
    path(
        'slack-founder-link/complete/',
        views.SlackFounderLinkCompleteView.as_view(),
        name='slack_founder_link_complete',
    ),
]
