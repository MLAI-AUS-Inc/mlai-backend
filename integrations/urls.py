from django.urls import path
from . import views

urlpatterns = [
    path('connect/google', views.google_connect, name='google_connect'),
    path('callback/google', views.google_callback, name='google_callback'),
]
