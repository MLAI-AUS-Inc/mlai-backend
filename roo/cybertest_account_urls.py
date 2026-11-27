from django.urls import path
from . import cybertest_views as views
from . import cybertest_entry_views as entry
from .cybertest_personal import AuthorizePersonal
from .cybertest_results import ResultsView

urlpatterns=[
    path('personal/authorize/', AuthorizePersonal.as_view(), name='personal-security-authorize'),
    path('entry/',entry.EntryView.as_view(),name='cybertest-entry'),
    path('authorize/',entry.AuthorizeView.as_view(),name='cybertest-authorize'),
    path('capture/',views.CaptureView.as_view(),name='cybertest-capture'),
    path('preview/',views.PreviewView.as_view(),name='cybertest-preview'),
    path('confirm/',views.ConfirmView.as_view(),name='cybertest-confirm'),
    path('results/', ResultsView.as_view(), name='cybertest-results'),
    path('results/<uuid:audit_id>/', ResultsView.as_view(), name='cybertest-result'),
]
