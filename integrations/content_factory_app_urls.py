from django.urls import path

from . import api_views_content_factory_app as views

urlpatterns = [
    path("bootstrap", views.ContentFactoryAppBootstrapView.as_view(), name="content_factory_app_bootstrap"),
    path("bootstrap/", views.ContentFactoryAppBootstrapView.as_view(), name="content_factory_app_bootstrap_slash"),
    path("settings", views.ContentFactoryAppSettingsView.as_view(), name="content_factory_app_settings"),
    path("settings/", views.ContentFactoryAppSettingsView.as_view(), name="content_factory_app_settings_slash"),
    path("github/connect", views.ContentFactoryAppGitHubConnectView.as_view(), name="content_factory_app_github_connect"),
    path("github/connect/", views.ContentFactoryAppGitHubConnectView.as_view(), name="content_factory_app_github_connect_slash"),
    path("scan", views.ContentFactoryAppScanView.as_view(), name="content_factory_app_scan"),
    path("scan/", views.ContentFactoryAppScanView.as_view(), name="content_factory_app_scan_slash"),
    path("discovery", views.ContentFactoryAppDiscoveryView.as_view(), name="content_factory_app_discovery"),
    path("discovery/", views.ContentFactoryAppDiscoveryView.as_view(), name="content_factory_app_discovery_slash"),
    path("article", views.ContentFactoryAppArticleView.as_view(), name="content_factory_app_article"),
    path("article/", views.ContentFactoryAppArticleView.as_view(), name="content_factory_app_article_slash"),
    path("daily/replay", views.ContentFactoryAppDailyReplayView.as_view(), name="content_factory_app_daily_replay"),
    path("daily/replay/", views.ContentFactoryAppDailyReplayView.as_view(), name="content_factory_app_daily_replay_slash"),
    path("runs/<str:run_id>", views.ContentFactoryAppRunView.as_view(), name="content_factory_app_run"),
    path("runs/<str:run_id>/", views.ContentFactoryAppRunView.as_view(), name="content_factory_app_run_slash"),
    path("runs/<str:run_id>/artifacts", views.ContentFactoryAppRunArtifactsView.as_view(), name="content_factory_app_run_artifacts"),
    path("runs/<str:run_id>/artifacts/", views.ContentFactoryAppRunArtifactsView.as_view(), name="content_factory_app_run_artifacts_slash"),
    path("runs/<str:run_id>/<str:action>", views.ContentFactoryAppRunControlView.as_view(), name="content_factory_app_run_control"),
    path("runs/<str:run_id>/<str:action>/", views.ContentFactoryAppRunControlView.as_view(), name="content_factory_app_run_control_slash"),
]
