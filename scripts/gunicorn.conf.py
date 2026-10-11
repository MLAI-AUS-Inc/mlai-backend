"""Warm Django's URL table after each Gunicorn worker forks.

The default lazy URL import puts the full founder tools and content factory
module graph on each worker's first request. Under load, even /healthz/live can
spend the whole worker timeout importing modules. Resolve routes before each
worker accepts requests, after fork. Firebase is initialized lazily by the
features that use it, so route warming does not create a Firestore client.
"""


def post_worker_init(worker):
    from django.urls import get_resolver

    get_resolver().url_patterns
    worker.log.info("Django URLconf warmed in Gunicorn worker")
