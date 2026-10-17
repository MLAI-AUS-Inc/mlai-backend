"""Real Django prefetch ownership without a database or an HTTP test client."""
from contextlib import ExitStack
import gc
from types import SimpleNamespace
import weakref
from unittest.mock import patch

from django.db.models import prefetch_related_objects
from django.db.models.query import QuerySet
from django.test import SimpleTestCase

from content_factory import vibe_marketing_views as views
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep


def prefetched_run(*, with_step=False, with_comment=False):
    """Use Django's actual prefetcher; replace only query evaluation with rows."""
    run = ContentFactoryRun(id=11, run_id="lifetime-fixture", result={"review_draft_html": "synthetic " * 1000})
    rows = [ContentFactoryRunStep(id=12, run=run, step_key="draft")] if with_step else []
    def fetch(query):
        if query._result_cache is None:
            if query.model is not ContentFactoryRunStep:
                raise AssertionError("Only the synthetic step query may be evaluated")
            query._result_cache = rows
    with patch.object(QuerySet, "_fetch_all", fetch):
        prefetch_related_objects([run], "steps")
    if with_comment:
        run._bootstrap_component_comments = [views.VibeMarketingComponentComment(id=13, run=run,
            component_id="title", body="Synthetic feedback")]
    return run


class BootstrapRunLifetimeTests(SimpleTestCase):
    def test_actual_django_empty_reverse_prefetch_retains_its_parent_until_cycle_collection(self):
        was_enabled = gc.isenabled()
        gc.disable()
        try:
            run = prefetched_run()
            reference = weakref.ref(run)
            del run
            self.assertIsNotNone(reference(), "The regression must reproduce Django's real ownership cycle")
            gc.collect()
            self.assertIsNone(reference())
        finally:
            if was_enabled: gc.enable()

    def test_bootstrap_releases_request_owned_prefetch_graphs_on_cold_warm_and_error_paths(self):
        for warm, error, with_step, with_comment in (
            (False, None, False, False), (True, None, False, False),
            (False, None, True, True), (True, None, True, True),
            (False, "compute", True, True), (True, "overlay", True, True)):
            with self.subTest(warm=warm, error=error, with_step=with_step, with_comment=with_comment):
                references = []
                context = SimpleNamespace(organization=SimpleNamespace(domain="synthetic.test"))
                config = SimpleNamespace(website_connection=None)
                def latest(*args, **kwargs):
                    run = prefetched_run(with_step=with_step, with_comment=with_comment)
                    references.append(weakref.ref(run))
                    return [run]
                def compute(*args, **kwargs):
                    self.assertIn("steps", kwargs["latest_runs"][0]._prefetched_objects_cache)
                    if error == "compute": raise RuntimeError("Synthetic projection failure")
                    return {"articleCapabilities": {}, "projected": True}
                def overlay(payload, **kwargs):
                    run = kwargs["latest_runs"][0]
                    self.assertIn("steps", run._prefetched_objects_cache)
                    self.assertEqual(len(run.steps.all()), int(with_step))
                    if error == "overlay": raise RuntimeError("Synthetic projection failure")
                    return payload
                was_enabled = gc.isenabled()
                gc.disable()
                try:
                    with ExitStack() as boundaries:
                        for name, callback in (("_get_config", lambda *args: config),
                            ("_bootstrap_cache_seconds", lambda: 30 if warm else 0),
                            ("_bootstrap_cache_key", lambda *args: "synthetic-lifetime"),
                            ("_latest_runs_for_org", latest), ("_compute_bootstrap_payload", compute),
                            ("_overlay_live_bootstrap_fields", overlay)):
                            # Plain callbacks deliberately avoid Mock's argument retention.
                            boundaries.enter_context(patch.object(views, name, callback))
                        boundaries.enter_context(patch.object(views.cache, "get", lambda *args: {"projected": True}))
                        try:
                            payload = views._serialize_bootstrap(context)
                        except RuntimeError:
                            self.assertIsNotNone(error)
                        else:
                            self.assertIsNone(error)
                            self.assertIs(payload["projected"], True)
                    self.assertEqual(len(references), 1)
                    self.assertIsNone(references[0](), "Completed projection must release its large run without gc.collect()")
                finally:
                    if was_enabled: gc.enable()
                    gc.collect()
