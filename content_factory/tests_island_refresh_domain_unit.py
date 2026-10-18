"""Reject malformed organization research scope before any dispatch effects."""
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from content_factory.services import island_refresh_scheduler as scheduler


class IslandRefreshDomainTests(SimpleTestCase):
    def test_invalid_domain_never_creates_dispatch_or_calls_provider(self):
        for domain in ("n", "https://example.com", "example.com/path", "user@example.com", "127.0.0.1", "a..com", "-a.com", "example.com:443", "", None):
            with self.subTest(domain=domain), patch.object(scheduler.ContentIslandRefreshDispatch.objects, "update_or_create", return_value=(SimpleNamespace(save=Mock()), True)) as write, patch.object(scheduler, "_post_content_factory_queue_request", return_value=SimpleNamespace(status_code=202, json=lambda: {})) as post:
                result = scheduler.dispatch_island_refresh(SimpleNamespace(domain=domain), local_date=date(2026, 10, 18))
                self.assertEqual(result["status"], "failed")
                write.assert_not_called()
                post.assert_not_called()

    def test_manual_invalid_domain_does_not_lookup_or_replay_existing_dispatch(self):
        with patch.object(scheduler.Organization.objects, "filter") as lookup:
            lookup.return_value.first.return_value = None
            result = scheduler.refresh_islands_for_domain("n", force=True)
        self.assertEqual(result["status"], "failed")
        lookup.assert_not_called()

    def test_organization_research_has_no_website_dependency(self):
        organization = SimpleNamespace(domain="research.example.com", website_connection=None)
        dispatch = SimpleNamespace(save=Mock())
        response = SimpleNamespace(status_code=202, json=lambda: {"run_id": "research-run"})
        with patch.object(scheduler.ContentIslandRefreshDispatch.objects, "update_or_create", return_value=(dispatch, True)), patch.object(scheduler, "_post_content_factory_queue_request", return_value=response) as post:
            result = scheduler.dispatch_island_refresh(organization, local_date=date(2026, 10, 18))
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(set(post.call_args.kwargs["payload"]), {"domain", "client_request_id", "request_source", "include_expansion"})

    def test_domain_syntax_is_bounded_without_changing_organization_scope(self):
        from content_factory.services.island_refresh_domain import validate_refresh_domain
        for value, expected in ((" Example.COM ", "example.com"), ("www.example.com", "www.example.com"), ("xn--bcher-kva.de", "xn--bcher-kva.de"), ("a.example.test", "a.example.test")):
            self.assertEqual(validate_refresh_domain(value), expected)
        for value in ("bücher.de", "a.com.", "a_b.com", "a.-com", "a.com?token=x", "a.com#x", "a." + "x" * 64, "a" * 64 + ".com", ("a" * 63 + ".") * 4 + "com", "a.1", "a.c", "a.é"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_refresh_domain(value)
