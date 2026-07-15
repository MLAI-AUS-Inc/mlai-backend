from django.urls import path

from content_analytics.views import (
    VibeMarketingAnalyticsDisableView,
    VibeMarketingAnalyticsEnableView,
    VibeMarketingAnalyticsStatusView,
    VibeMarketingAnalyticsSummaryView,
    VibeMarketingArticleAnalyticsView,
    VibeMarketingSearchConsoleVerifyView,
)


urlpatterns = [
    path("status", VibeMarketingAnalyticsStatusView.as_view(), name="vibe-marketing-analytics-status"),
    path("status/", VibeMarketingAnalyticsStatusView.as_view()),
    path("enable", VibeMarketingAnalyticsEnableView.as_view(), name="vibe-marketing-analytics-enable"),
    path("enable/", VibeMarketingAnalyticsEnableView.as_view()),
    path("disable", VibeMarketingAnalyticsDisableView.as_view(), name="vibe-marketing-analytics-disable"),
    path("disable/", VibeMarketingAnalyticsDisableView.as_view()),
    path("summary", VibeMarketingAnalyticsSummaryView.as_view(), name="vibe-marketing-analytics-summary"),
    path("summary/", VibeMarketingAnalyticsSummaryView.as_view()),
    path("articles/<uuid:article_id>", VibeMarketingArticleAnalyticsView.as_view(), name="vibe-marketing-article-analytics"),
    path("articles/<uuid:article_id>/", VibeMarketingArticleAnalyticsView.as_view()),
    path("search-console/verify", VibeMarketingSearchConsoleVerifyView.as_view(), name="vibe-marketing-gsc-verify"),
    path("search-console/verify/", VibeMarketingSearchConsoleVerifyView.as_view()),
    path("gsc/verify", VibeMarketingSearchConsoleVerifyView.as_view(), name="vibe-marketing-gsc-verify-alias"),
    path("gsc/verify/", VibeMarketingSearchConsoleVerifyView.as_view()),
]
