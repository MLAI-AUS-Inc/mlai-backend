from django.urls import path
from . import cybertest_views as views
from . import cybertest_entry_views as entry

urlpatterns=[
    path('quotes/recover/',views.RecoverQuoteView.as_view(),name='cybertest-recover-quote'),
    path('entry/exchange/',entry.ExchangeView.as_view(),name='cybertest-entry-exchange'),
    path('entry/inspect/',entry.InspectView.as_view(),name='cybertest-entry-inspect'),
    path('quotes/<uuid:checkout_id>/settle-failure/',views.FailedSettlementView.as_view(),name='cybertest-failed-settlement'),
    path('quotes/<uuid:checkout_id>/settle-delivery/',views.DeliverySettlementView.as_view(),name='cybertest-delivery-settlement'),
    path('quotes/',views.CreateQuoteView.as_view(),name='cybertest-quote'),
    path('quotes/<uuid:checkout_id>/',views.QuoteStatusView.as_view(),name='cybertest-status'),
    path('quotes/<uuid:checkout_id>/charge/',views.ChargeView.as_view(),name='cybertest-charge'),
    path('quotes/<uuid:checkout_id>/dispatch/',views.DispatchView.as_view(),name='cybertest-dispatch'),
    path('quotes/<uuid:checkout_id>/refund/',views.RefundView.as_view(),name='cybertest-refund'),
]
