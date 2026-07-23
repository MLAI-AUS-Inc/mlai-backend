from django.urls import path

from . import views
from . import roo_views

urlpatterns = [
    path('applications/', views.VictorApplicationSubmitView.as_view(), name='victor-ai-applications'),
    path(
        'roo/applications/summary/',
        roo_views.VictorApplicationSummaryView.as_view(),
        name='victor-roo-applications-summary',
    ),
    path(
        'roo/applications/',
        roo_views.VictorApplicationListView.as_view(),
        name='victor-roo-applications-list',
    ),
    path(
        'roo/applications/export.csv',
        roo_views.VictorApplicationCsvView.as_view(),
        name='victor-roo-applications-csv',
    ),
    path(
        'roo/applications/<int:application_id>/',
        roo_views.VictorApplicationDetailView.as_view(),
        name='victor-roo-applications-detail',
    ),
]
