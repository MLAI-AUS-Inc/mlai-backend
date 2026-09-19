"""Deliberately bounded API aliases; no admin or service-key endpoints."""

from django.urls import URLPattern, URLResolver, include, path

from content_factory import urls_vibe_marketing
from core import urls as auth_urls, urls_users
from founder_tools import urls as founder_urls
from integrations import api_urls as connector_urls
from roo import urls as points_urls

from .purchases import (
    StartupPurchaseCreateView,
    StartupPurchaseView,
    StartupPurchaseCheckoutView,
)
from .roo_link import CaptureRooLinkView, PreviewRooLinkView, CompleteRooLinkView
from .handoff import RedeemStartupHandoffView
from .connectors import MyStartupConnectorConnectView, MyStartupConnectorDisconnectView
from .api import startup_view
from .registry import (
    ACCOUNT_VIEWS,
    CONNECTOR_VIEWS,
    FOUNDER_VIEWS,
    LINK_VIEWS,
    MARKETING_VIEWS,
    POINTS_VIEWS,
)


def selected_patterns(patterns, allowed):
    """Clone registered, explicitly reviewed views while preserving URL shape."""
    result = []
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            children = selected_patterns(pattern.url_patterns, allowed)
            if children:
                result.append(path(str(pattern.pattern), include(children)))
        elif isinstance(pattern, URLPattern):
            view_class = getattr(pattern.callback, "view_class", None)
            if view_class is not None and view_class.__name__ in allowed:
                result.append(
                    path(
                        str(pattern.pattern),
                        startup_view(view_class, pattern.callback.view_initkwargs),
                        pattern.default_args,
                        name=f"my-startup-{pattern.name}" if pattern.name else None,
                    )
                )
    return result


marketing = selected_patterns(urls_vibe_marketing.urlpatterns, MARKETING_VIEWS)
preview = [item for item in marketing if "/live-preview/" in str(item.pattern)]
urlpatterns = [
    path(
        "integrations/sources/connections/<int:connection_id>",
        MyStartupConnectorDisconnectView.as_view(),
    ),
    path("points/me/purchases/", StartupPurchaseCreateView.as_view()),
    path("points/purchases/<uuid:purchase_id>/", StartupPurchaseView.as_view()),
    path(
        "points/purchases/<uuid:purchase_id>/checkout/",
        StartupPurchaseCheckoutView.as_view(),
    ),
    path("roo-link/capture/", CaptureRooLinkView.as_view()),
    path("roo-link/preview/", PreviewRooLinkView.as_view()),
    path("roo-link/complete/", CompleteRooLinkView.as_view()),
    path("handoff/redeem/", RedeemStartupHandoffView.as_view()),
    path("connectors/<str:provider>/connect/", MyStartupConnectorConnectView.as_view()),
    path("vibe-marketing/", include(marketing)),
    path("companies/<uuid:startup_company_id>/vibe-marketing/", include(preview)),
    path(
        "founder-tools/",
        include(selected_patterns(founder_urls.urlpatterns, FOUNDER_VIEWS)),
    ),
    path("auth/", include(selected_patterns(auth_urls.urlpatterns, ACCOUNT_VIEWS))),
    path("users/", include(selected_patterns(urls_users.urlpatterns, LINK_VIEWS))),
    path("points/", include(selected_patterns(points_urls.urlpatterns, POINTS_VIEWS))),
    path(
        "integrations/",
        include(selected_patterns(connector_urls.urlpatterns, CONNECTOR_VIEWS)),
    ),
]
