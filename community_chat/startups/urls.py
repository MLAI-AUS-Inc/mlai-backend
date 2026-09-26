from django.urls import path
from . import views
from .connections import ConnectView, DisconnectView, connect_browser

urlpatterns = [
    path("connect/browser/", connect_browser, name="chat_startups_connect_browser"),
    path("connect/<str:provider>/", ConnectView.as_view()),
    path("bootstrap/", views.BootstrapView.as_view(), name="chat_startups_bootstrap"),
    path("companies/", views.CompaniesView.as_view(), name="chat_startups_companies"),
    path("active-company/", views.ActiveCompanyView.as_view()),
    path("settings/", views.SettingsView.as_view()),
    path("sources/", views.SourcesView.as_view()),
    path("sources/<str:provider>/", DisconnectView.as_view()),
    path("updates/", views.UpdatesView.as_view(), name="chat_startups_updates"),
    path("updates/<int:update_id>/", views.UpdateView.as_view(), name="chat_startups_update"),
    path("updates/<int:update_id>/publish/", views.PublishView.as_view(), name="chat_startups_publish"),
    path("community/", views.CommunityView.as_view(), name="chat_startups_community"),
    path("generate/", views.GenerateView.as_view()),
    path("runs/active/", views.ActiveRunView.as_view()),
    path("runs/<str:run_id>/", views.RunView.as_view()),
    path("runs/<str:run_id>/results/", views.ResultsView.as_view()),
    path("runs/<str:run_id>/cancel/", views.CancelView.as_view()),
    path("documents/", views.DocumentsView.as_view()),
    path("documents/session/", views.UploadSessionView.as_view()),
    path("documents/complete/", views.UploadCompleteView.as_view()),
]
