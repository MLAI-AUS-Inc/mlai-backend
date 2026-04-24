from django.urls import path
from . import views

urlpatterns = [
    path('connect/google', views.google_connect, name='google_connect'),
    path('callback/google', views.google_callback, name='google_callback'),
    path('connect/stripe', views.stripe_connect, name='stripe_connect'),
    path('callback/stripe', views.stripe_callback, name='stripe_callback'),
    path('connect/xero', views.xero_connect, name='xero_connect'),
    path('callback/xero', views.xero_callback, name='xero_callback'),
    path('connect/github', views.github_connect, name='github_connect'),
    path('callback/github', views.github_callback, name='github_callback'),
    path('connect/github/select', views.github_select_repo, name='github_select_repo'),
]
