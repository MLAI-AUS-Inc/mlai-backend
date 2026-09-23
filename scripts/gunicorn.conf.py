"""Warm Django's URL table after each Gunicorn worker forks.

The default lazy URL import puts the full founder tools and content factory
module graph on each worker's first request. Under load, even /healthz/live can
spend the whole worker timeout importing modules. Resolve routes before each
worker accepts requests, after fork: URL imports initialize a Firestore gRPC
client and those clients must not be inherited from a Gunicorn master process.
"""


def post_worker_init(worker):
    from django.urls import get_resolver

    get_resolver().url_patterns
    worker.log.info("Django URLconf warmed in Gunicorn worker")
