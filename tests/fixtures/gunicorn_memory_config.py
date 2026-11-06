"""Real worker lifecycle smoke; production URL warming has separate coverage."""

import importlib.util
from pathlib import Path

from scripts.worker_memory_profile import start_worker_profile

spec = importlib.util.spec_from_file_location("actual_gunicorn_hooks", Path(__file__).parents[2] / "scripts/gunicorn.conf.py")
production = importlib.util.module_from_spec(spec)
spec.loader.exec_module(production)
post_request = production.post_request
worker_exit = production.worker_exit


def post_worker_init(worker):
    """Profile the pure fixture without loading a database-backed application."""
    start_worker_profile(worker)
