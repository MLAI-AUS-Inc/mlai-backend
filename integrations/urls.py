from django.urls import path
from . import views

urlpatterns = [
    path('connect/google', views.google_connect, name='google_connect'),
    path('callback/google', views.google_callback, name='google_callback'),
    path('connect/github', views.github_connect, name='github_connect'),
    path('callback/github', views.github_callback, name='github_callback'),
    path('connect/github/select', views.github_select_repo, name='github_select_repo'),
    path('connect/<str:provider>', views.connector_connect, name='connector_connect'),
    path('callback/<str:provider>', views.connector_callback, name='connector_callback'),
]
