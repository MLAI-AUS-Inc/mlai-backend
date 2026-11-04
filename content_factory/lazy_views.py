"""Routes whose views ship in a later P2 package (IF-P2-25).

``lazy_view(module, attr)`` is a plain Django view function that imports the
DRF view class on its first call and caches ``as_view()``. Until that package
has merged, the route answers 404 ``{"detail": "Not found.", "reason":
"not_found"}``, exactly what an unrouted path would say to a client, so the
URL files can carry every P2 route from the first release.
"""
from __future__ import annotations

import importlib
import threading

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

NOT_FOUND = {"detail": "Not found.", "reason": "not_found"}


def not_found_response() -> JsonResponse:
    """The 404 a lazy route gives while its view is missing."""
    return JsonResponse(NOT_FOUND, status=404)


def lazy_view(module: str, attr: str):
    """A view that resolves ``module.attr`` on first use, or answers 404 while it is missing.

    A missing module or class is looked up again on the next request, so a
    process that started before the package merged needs no restart once the
    code is there. The resolved ``as_view()`` callable is cached.
    """
    lock = threading.Lock()
    resolved = []

    def _resolve():
        if resolved:
            return resolved[0]
        with lock:
            if resolved:
                return resolved[0]
            try:
                view_class = getattr(importlib.import_module(module), attr)
            except (ImportError, AttributeError):
                return None
            as_view = getattr(view_class, "as_view", None)
            if as_view is None:
                return None
            resolved.append(as_view())
            return resolved[0]

    @csrf_exempt
    def view(request, *args, **kwargs):
        target = _resolve()
        if target is None:
            return not_found_response()
        return target(request, *args, **kwargs)

    view.lazy_target = (module, attr)
    view.lazy_reset = resolved.clear  # tests only: forget a resolved view
    view.__name__ = f"lazy_{attr}"
    view.__qualname__ = view.__name__
    return view
