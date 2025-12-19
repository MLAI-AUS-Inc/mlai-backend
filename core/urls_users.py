from django.urls import path
from . import views

urlpatterns = [
    path('link-slack/', views.LinkSlackView.as_view(), name='link_slack'),
]
