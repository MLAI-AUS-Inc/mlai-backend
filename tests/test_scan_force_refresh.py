"""A manual founder-tools scan force-refreshes by default: it bypasses content-factory's
unchanged-HEAD reuse AND drives a fresh visual capture + design-snapshot re-synthesis, so a
website restyle reaches the org's active design snapshot even when the repo is unchanged.
An explicit forceRefresh=false opts out for a lightweight scan."""
from datetime import timedelta

from django.test import SimpleTestCase
from django.utils import timezone

from content_factory.vibe_marketing_views import _scan_should_force_refresh


class _Cfg:
    def __init__(self, last_scanned_at=None):
        self.last_scanned_at = last_scanned_at


class ScanForceRefreshTests(SimpleTestCase):
    def test_manual_scan_forces_by_default(self):
        # No explicit flag -> force, regardless of when the repo was last scanned, so a site
        # restyle is captured even on an unchanged repo.
        self.assertTrue(_scan_should_force_refresh(_Cfg(last_scanned_at=None), {}))
        self.assertTrue(_scan_should_force_refresh(_Cfg(last_scanned_at=timezone.now() - timedelta(hours=5)), {}))
        self.assertTrue(_scan_should_force_refresh(_Cfg(last_scanned_at=timezone.now() - timedelta(minutes=2)), {}))

    def test_explicit_force_refresh_true(self):
        self.assertTrue(_scan_should_force_refresh(_Cfg(), {"forceRefresh": True}))
        self.assertTrue(_scan_should_force_refresh(_Cfg(), {"force_refresh": "true"}))

    def test_explicit_opt_out_is_honored(self):
        # A caller can request a lightweight scan that keeps the unchanged-HEAD reuse.
        self.assertFalse(_scan_should_force_refresh(_Cfg(), {"forceRefresh": False}))
        self.assertFalse(_scan_should_force_refresh(_Cfg(), {"force_refresh": "false"}))
