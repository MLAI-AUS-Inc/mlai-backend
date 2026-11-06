"""medhack_backend URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from .views import health_check, health_live, health_points, health_ready
from startup_updates import data_views as startup_data_views

urlpatterns = [
    path('', include('startup_updates.mcp.urls')),
    path('api/v1/agent-runtime/', include('agent_runtime.native_setup_urls')),
    path('', health_check, name='health_check'),
    path('healthz/live', health_live, name='health_live'),
    path('healthz/points', health_points, name='health_points'),
    path('healthz/ready', health_ready, name='health_ready'),
    path('admin/', admin.site.urls),
    path('api/v1/auth/', include('core.urls')),
    path('api/v1/hackathons/esafety/', include('esafety.urls')),
    path('api/v1/hackathons/watt-the-hack/sim/', include('hackathons.watt_the_hack.urls')),
    path('api/v1/hackathons/', include('core.urls_hackathons')),
    path('api/v1/hackathons/hospital/', include('hospital.urls')),
    path('api/v1/founder-tools/', include('founder_tools.urls')),
    path('api/v1/my-startup/', include('community_chat.my_startup_urls')),
    path('api/v1/my-startup/', include('founder_tools.my_startup.urls')),
    path('api/v1/vibe-raising/', include('vibe_raising.urls')),
    path('api/v1/vibe-marketing/', include('content_factory.urls_vibe_marketing')),
    path('api/v1/mlai-studio/', include('mlai_studio.urls')),
    path('api/v1/data/', include('data_access.urls')),
    path('api/v1/points/', include('roo.urls')),
    path('api/v1/org-memory/', include('org_memory.urls')),
    path('api/v1/community-chat/', include('community_chat.urls')),
    path('api/v1/crm/webhooks/', include('crm.urls_webhooks')),
    path('api/v1/crm/preferences/', include('crm.urls_preferences')),
    path('api/v1/system-email/webhooks/', include('system_email.urls')),
    path('api/v1/public-brain/', include('org_memory.public_urls')),
    path('api/v1/jobs/', include('jobs.urls')),
    path('api/v1/medhack/', include('hospital.medhack_urls')),
    # Content Factory API
    path('api/content-factory/', include('content_factory.urls_service')),
    path('api/v1/content-factory/app/', include('integrations.content_factory_app_urls')),
    path('api/v1/content/', include('content_factory.urls_content')),
    # SEO Research API
    path('api/seo/', include('content_factory.urls_seo')),

    path('integrations/', include('integrations.urls')),
    path('api/v1/integrations/', include('integrations.api_urls')),
    path(
        'api/v1/startups/<int:organization_id>/data/status',
        startup_data_views.StartupDataStatusView.as_view(),
        name='startup_data_status',
    ),
    path(
        'api/v1/startups/<int:organization_id>/data',
        startup_data_views.StartupDataDeletionView.as_view(),
        name='startup_data_delete',
    ),
    path('api/v1/users/', include('core.urls_users')),
]

# P2 (IF-P2-25): the source re-pin endpoint. It sits just before the
# content-factory include, whose fenced runs/<run_id>/<action> route would
# otherwise catch it. It answers 404 until its view ships.
from content_factory.lazy_views import lazy_view  # noqa: E402

_content_factory_include = next(index for index, pattern in enumerate(urlpatterns)
                                if str(pattern.pattern) == 'api/content-factory/')
urlpatterns[_content_factory_include:_content_factory_include] = [
    path(f'api/content-factory/runs/<str:run_id>/repin{suffix}',
         lazy_view('content_factory.source_repin_views', 'SourceRepinView'))
    for suffix in ('', '/')
]
