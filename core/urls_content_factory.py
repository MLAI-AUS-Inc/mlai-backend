from django.urls import path
from . import views

urlpatterns = [
    path('org/config/', views.ContentFactoryOrgConfigView.as_view(), name='content_factory_org_config'),
    path('org/config', views.ContentFactoryOrgConfigView.as_view(), name='content_factory_org_config_no_slash'),
]
