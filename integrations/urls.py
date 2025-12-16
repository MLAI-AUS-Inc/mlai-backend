from django.urls import path
from . import views

urlpatterns = [
    path('connect/google', views.google_connect, name='google_connect'),
    path('callback/google', views.google_callback, name='google_callback'),
    path('emails', views.get_gmail_emails, name='get_gmail_emails'),
    path('test/gmail', views.test_gmail_fetch, name='test_gmail_fetch'),
]
