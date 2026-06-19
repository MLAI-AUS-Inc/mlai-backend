"""Option A: a rapid re-scan forces a full scan (force_refresh) so the scaffold
plan refreshes instead of reusing the unchanged-HEAD short-circuit (which leaves
the wizard stuck on the "plan has drifted, re-run the scan" guard)."""
from datetime import timedelta

from django.test import SimpleTestCase
from django.utils import timezone

from content_factory.vibe_marketing_views import (
    ARTICLE_SCAN_RAPID_RESCAN_WINDOW,
    _scan_should_force_refresh,
)


class _Cfg:
    def __init__(self, last_scanned_at=None):
        self.last_scanned_at = last_scanned_at


class ScanForceRefreshTests(SimpleTestCase):
    def test_recent_scan_forces_full(self):
        cfg = _Cfg(last_scanned_at=timezone.now() - timedelta(minutes=2))
        self.assertTrue(_scan_should_force_refresh(cfg, {}))

    def test_old_scan_does_not_force(self):
        cfg = _Cfg(last_scanned_at=timezone.now() - timedelta(hours=2))
        self.assertFalse(_scan_should_force_refresh(cfg, {}))

    def test_no_prior_scan_does_not_force(self):
        self.assertFalse(_scan_should_force_refresh(_Cfg(last_scanned_at=None), {}))

    def test_explicit_force_refresh_overrides(self):
        cfg = _Cfg(last_scanned_at=None)  # no rapid signal
        self.assertTrue(_scan_should_force_refresh(cfg, {"forceRefresh": True}))
        self.assertTrue(_scan_should_force_refresh(cfg, {"force_refresh": "true"}))

    def test_just_inside_window_forces(self):
        cfg = _Cfg(last_scanned_at=timezone.now() - ARTICLE_SCAN_RAPID_RESCAN_WINDOW + timedelta(seconds=30))
        self.assertTrue(_scan_should_force_refresh(cfg, {}))

    def test_just_outside_window_does_not_force(self):
        cfg = _Cfg(last_scanned_at=timezone.now() - ARTICLE_SCAN_RAPID_RESCAN_WINDOW - timedelta(minutes=1))
        self.assertFalse(_scan_should_force_refresh(cfg, {}))
