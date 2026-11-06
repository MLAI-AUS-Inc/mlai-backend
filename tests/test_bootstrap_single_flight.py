"""Concurrent bootstrap misses share one compute (database/network free)."""
import threading
from types import SimpleNamespace
from unittest.mock import patch

import django
from django.apps import apps
from django.core.cache import cache
from django.test import SimpleTestCase

if not apps.ready:
    django.setup()

from content_factory import vibe_marketing_views as views


class BootstrapSingleFlightTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.context = SimpleNamespace(organization=SimpleNamespace(id=801, pk=801, domain="example.test"),
            profile=SimpleNamespace(user=SimpleNamespace(id=9, pk=9)), company=None)

    def _patches(self, compute):
        return [
            patch.object(views, "_get_config", return_value=SimpleNamespace()),
            patch.object(views, "_bootstrap_cache_seconds", return_value=20),
            patch.object(views, "_bootstrap_cache_key", return_value="vibe_bootstrap:test-key"),
            patch.object(views, "_latest_runs_for_org", return_value=[]),
            patch.object(views, "_compute_bootstrap_payload", side_effect=compute),
            patch.object(views, "_overlay_live_bootstrap_fields", side_effect=lambda payload, **_: dict(payload)),
            patch.object(views, "_BOOTSTRAP_COMPUTE_POLL_SECONDS", 0.01),
        ]

    def _run(self, patches, fn):
        for item in patches:
            item.start()
        try:
            return fn()
        finally:
            for item in reversed(patches):
                item.stop()

    def test_concurrent_misses_compute_once(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def compute(*_args, **_kwargs):
            calls.append(1)
            started.set()
            release.wait(2)
            return {"articleCapabilities": {}, "marker": "computed"}

        results = []

        def read():
            results.append(views._serialize_bootstrap(self.context))

        def scenario():
            first = threading.Thread(target=read)
            first.start()
            self.assertTrue(started.wait(2))
            second = threading.Thread(target=read)
            second.start()
            release.set()
            first.join(5)
            second.join(5)

        self._run(self._patches(compute), scenario)
        self.assertEqual(len(calls), 1)
        self.assertEqual([result["marker"] for result in results], ["computed", "computed"])

    def test_waiter_computes_itself_when_the_holder_fails(self):
        calls = []

        def compute(*_args, **_kwargs):
            calls.append(1)
            return {"articleCapabilities": {}, "marker": "own"}

        def scenario():
            # Another worker holds the lock but never caches an answer.
            cache.add("vibe_bootstrap:test-key:computing", 1, 30)
            with patch.object(views, "_BOOTSTRAP_COMPUTE_WAIT_SECONDS", 0.05):
                return views._serialize_bootstrap(self.context)

        result = self._run(self._patches(compute), scenario)
        self.assertEqual(result["marker"], "own")
        self.assertEqual(len(calls), 1)

    def test_lock_is_released_after_a_failed_compute(self):
        def compute(*_args, **_kwargs):
            raise RuntimeError("boom")

        def scenario():
            with self.assertRaises(RuntimeError):
                views._serialize_bootstrap(self.context)

        self._run(self._patches(compute), scenario)
        self.assertIsNone(cache.get("vibe_bootstrap:test-key:computing"))

    def test_cache_hit_takes_no_lock(self):
        def compute(*_args, **_kwargs):
            raise AssertionError("cache hit must not compute")

        def scenario():
            cache.set("vibe_bootstrap:test-key", {"marker": "cached"}, 20)
            with patch.object(cache, "add", side_effect=AssertionError("no lock on a hit")):
                return views._serialize_bootstrap(self.context)

        result = self._run(self._patches(compute), scenario)
        self.assertEqual(result["marker"], "cached")

    def test_lock_is_released_when_the_run_lookup_fails(self):
        def compute(*_args, **_kwargs):
            raise AssertionError("compute must not run after a failed lookup")

        def scenario():
            with patch.object(views, "_latest_runs_for_org", side_effect=RuntimeError("db down")):
                with self.assertRaises(RuntimeError):
                    views._serialize_bootstrap(self.context)

        self._run(self._patches(compute), scenario)
        self.assertIsNone(cache.get("vibe_bootstrap:test-key:computing"))
