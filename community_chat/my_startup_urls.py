"""Explicit paths only: no arbitrary proxy, admin, billing, or worker endpoints."""
from django.urls import path
from . import my_startup_views as views

ROUTES = (
    ("auth/me", views.AccountView),
    ("points/me/balance", views.BalanceView),
    ("founder-tools/bootstrap", views.FounderBootstrapView),
    ("founder-tools/profile", views.ProfileView),
    ("founder-tools/companies", views.CompaniesView),
    ("founder-tools/active-company", views.ActiveCompanyView),
    ("vibe-marketing/bootstrap", views.BootstrapView),
    ("vibe-marketing/settings", views.SettingsView),
    ("vibe-marketing/autofill", views.ResearchView),
    ("vibe-marketing/company/avatar", views.AvatarView),
    ("vibe-marketing/github/connect", views.GitHubConnectView),
    ("vibe-marketing/github/repos", views.RepositoriesView),
    ("vibe-marketing/github/repository", views.RepositoryView),
    ("vibe-marketing/website-connection", views.WebsiteConnectionView),
    ("vibe-marketing/website-connection/<str:action>", views.WebsiteConnectionActionView),
    ("vibe-marketing/notifications/channels", views.NotificationChannelsView),
    ("vibe-marketing/notifications/channels/delivery", views.NotificationDeliveryView),
    ("vibe-marketing/notifications/channels/<uuid:channel_id>", views.NotificationChannelView),
    ("vibe-marketing/notifications/channels/<uuid:channel_id>/verify", views.VerifyNotificationChannelView),
    ("vibe-marketing/notifications/channels/<uuid:channel_id>/resend", views.ResendNotificationChannelView),
    ("vibe-marketing/notifications/automation", views.AutomationStatusView),
    ("vibe-marketing/learned-rules", views.LearnedRulesView),
    ("vibe-marketing/learned-rules/<int:rule_id>", views.LearnedRuleView),
    ("integrations/sources/status", views.SourcesStatusView),
    ("vibe-marketing/lookups/locations", views.LocationsView),
    ("vibe-marketing/lookups/abns", views.AbnsView),
    ("vibe-marketing/runs/<str:run_id>", views.RunView),
    ("vibe-marketing/runs/<str:run_id>/article-review", views.ArticleReviewView),
    ("vibe-marketing/runs/<str:run_id>/article-review/preview-lease", views.ArticlePreviewView),
    ("vibe-marketing/runs/<str:run_id>/comments", views.ArticleCommentsView),
    ("vibe-marketing/runs/<str:run_id>/comments/<uuid:comment_id>", views.ArticleCommentView),
    ("vibe-marketing/runs/<str:run_id>/cancel", views.CancelRunView),
)

urlpatterns = [path(route + suffix, view.as_view()) for route, view in ROUTES for suffix in ("", "/")]
