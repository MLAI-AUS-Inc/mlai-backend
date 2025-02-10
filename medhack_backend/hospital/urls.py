# app/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path('submit/', views.submit_predictions, name='submit_predictions'),
    path('send-magic-link/', views.SendMagicLinkView.as_view(), name='send_magic_link'),
    path('verify-magic-link/', views.MagicLinkVerifyView.as_view(), name='verify_magic_link'),
    path('token/refresh/', views.CookieTokenRefreshView.as_view(), name='token_refresh'),
    path('api/logout/', views.logout_view, name='logout'),
]
