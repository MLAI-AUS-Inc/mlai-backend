from django.urls import path
from . import cybertest_views as views
from . import cybertest_entry_views as entry

urlpatterns=[
    path('entry/',entry.EntryView.as_view(),name='cybertest-entry'),
    path('authorize/',entry.AuthorizeView.as_view(),name='cybertest-authorize'),
    path('capture/',views.CaptureView.as_view(),name='cybertest-capture'),
    path('preview/',views.PreviewView.as_view(),name='cybertest-preview'),
    path('confirm/',views.ConfirmView.as_view(),name='cybertest-confirm'),
]
