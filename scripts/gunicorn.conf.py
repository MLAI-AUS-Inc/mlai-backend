"""Warm Django's URL table once in the Gunicorn master before worker fork.

The default lazy URL import puts the full founder tools and content factory
module graph on each worker's first request. Under load, even /healthz/live
can spend the whole worker timeout importing modules. ``--preload`` loads the
WSGI app in the master; this hook also resolves URLconf before workers start.
"""


def when_ready(server):
    from django.db import connections
    from django.urls import get_resolver

    get_resolver().url_patterns
    # URL modules should not query the DB at import time, but close any
    # connection opened by a transitive import before forking workers.
    connections.close_all()
    server.log.info("Django URLconf warmed before Gunicorn worker fork")
