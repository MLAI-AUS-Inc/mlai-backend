"""Public withdrawal needs an actual exact-URL absence plus the owned CMS draft."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit, urlunsplit

from django.test import SimpleTestCase

from . import cms_post_contract as posts
from . import cms_unpublish_contract as withdraw
from . import cms_unpublish_public as public
from .cms_api_transport import CmsApiError
from .tests_cms_post_contract_unit import intent, response, issued


class CmsUnpublishPublicTests(SimpleTestCase):
    def fixture(self, *, plain=False):
        from uuid import uuid4
        publication = intent()
        row = response(publication)
        if plain: row["link"] = "https://fixture.test/?p=42"
        confirmed = posts.observe_posts(publication, issued(publication)[0], row, lookup=False)
        chosen = withdraw.make_intent(operation_id=str(uuid4()), request_id=str(uuid4()), publication=publication, state=confirmed)
        context = {"publication": publication, "published_state": confirmed}
        state = withdraw.new_state(chosen, **context)
        state = withdraw.observe(chosen, state, {**row, "status": "draft"}, **context)
        return chosen, state, context

    def fetch(self, chosen, state, context, status):
        reply = SimpleNamespace(status=status, close=Mock(), read=Mock(side_effect=AssertionError("No error-page body")))
        client = Mock()
        client.getresponse.return_value = reply
        with patch.object(public, "public_addresses", return_value=(None, ["203.0.113.8"])) as dns, \
                patch.object(public, "PinnedHTTPSConnection", return_value=client) as tls:
            actual = public.fetch_absence(chosen, state, **context)
        return actual, reply, client, dns, tls

    def test_real_404_and_410_are_bound_to_both_provider_draft_and_exact_scope(self):
        chosen, state, context = self.fixture()
        now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        for status in (404, 410):
            actual, reply, client, dns, tls = self.fetch(chosen, state, context, status)
            proof = public.observation(chosen, state, actual, checked_at=now.isoformat(), **context)
            self.assertEqual(public.validate_observation(proof, chosen, state, now=now, **context), proof)
            self.assertEqual((proof["status"], proof["http_status"]), ("withdrawn", status))
            self.assertEqual(proof["provider_observation_digest"], state["observation_digest"])
            self.assertNotIn("html", proof)
            parsed = urlsplit(chosen["public_url"])
            dns.assert_called_once_with(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")), "fixture.test")
            tls.assert_called_once_with("fixture.test", "203.0.113.8")
            self.assertEqual(client.request.call_args.args, ("GET", parsed.path))
            headers = client.request.call_args.kwargs["headers"]
            self.assertFalse({"authorization", "cookie", "referer"} & {name.lower() for name in headers})
            reply.read.assert_not_called(); reply.close.assert_called_once(); client.close.assert_called_once()

    def test_plain_wordpress_post_query_is_retained_in_http_but_not_dns_resolution(self):
        chosen, state, context = self.fixture(plain=True)
        _actual, _reply, client, dns, _tls = self.fetch(chosen, state, context, 404)
        self.assertEqual(client.request.call_args.args, ("GET", "/?p=42"))
        dns.assert_called_once_with("https://fixture.test/", "fixture.test")

    def test_timeout_or_generic_200_page_is_never_public_withdrawal(self):
        chosen, state, context = self.fixture()
        for status in (200, 204, 301, 302, 303, 307, 308, 401, 403, 429, 500):
            reply = SimpleNamespace(status=status, close=Mock())
            client = Mock(); client.getresponse.return_value = reply
            with patch.object(public, "public_addresses", return_value=(None, ["203.0.113.8"])), \
                    patch.object(public, "PinnedHTTPSConnection", return_value=client), self.assertRaises(CmsApiError):
                public.fetch_absence(chosen, state, **context)
            self.assertEqual(client.request.call_count, 1)
            reply.close.assert_called_once(); client.close.assert_called_once()
            with self.assertRaises(CmsApiError): public.observation(chosen, state, status, **context)

    def test_malformed_transport_status_cannot_coerce_absence(self):
        chosen, state, context = self.fixture()
        for status in (True, "404", None):
            with self.subTest(status=status), self.assertRaises(CmsApiError): self.fetch(chosen, state, context, status)

    def test_provider_state_must_be_an_observed_draft_before_any_public_request(self):
        chosen, _state, context = self.fixture()
        reserved = withdraw.new_state(chosen, **context)
        with patch.object(public, "public_addresses") as dns, self.assertRaises(CmsApiError):
            public.fetch_absence(chosen, reserved, **context)
        dns.assert_not_called()

    def test_network_diagnostics_export_no_destination_or_transport_exception(self):
        chosen, state, context = self.fixture()
        client = Mock(); client.getresponse.side_effect = OSError("SYNTHETIC-SECRET-CANARY")
        with patch.object(public, "public_addresses", return_value=(None, ["203.0.113.8"])), \
                patch.object(public, "PinnedHTTPSConnection", return_value=client), self.assertRaises(CmsApiError) as failure:
            public.fetch_absence(chosen, state, **context)
        self.assertNotIn("CANARY", str(failure.exception)); self.assertNotIn("fixture.test", str(failure.exception))
        client.close.assert_called_once()

    def test_current_proof_has_a_finite_window_and_historical_reads_grant_no_freshness(self):
        chosen, state, context = self.fixture()
        now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        proof = public.observation(chosen, state, 404, checked_at=now.isoformat(), **context)
        self.assertEqual(public.validate_observation(proof, chosen, state, now=now + timedelta(minutes=4), **context), proof)
        for clock in (now - timedelta(seconds=1), now + timedelta(minutes=6), now.replace(tzinfo=None)):
            with self.assertRaises(CmsApiError): public.validate_observation(proof, chosen, state, now=clock, **context)
        self.assertEqual(public.validate_observation(proof, chosen, state, **context), proof)

    def test_rechecksummed_altered_proofs_or_changed_provider_draft_are_refused(self):
        chosen, state, context = self.fixture()
        proof = public.observation(chosen, state, 410, **context)
        for changes in ({"http_status": 200}, {"provider_id": 99}, {"public_url": "https://other.test/"},
                {"scope_sha256": "f" * 64}, {"provider_observation_digest": "f" * 64}, {"schema_version": True},
                {"checked_at": "2026-10-18T00:00:00+01:00"}, {"secret": "SYNTHETIC-SECRET"}):
            changed = {**proof, **changes}
            changed["digest"] = public.digest({key: item for key, item in changed.items() if key != "digest"})
            with self.subTest(changes=changes), self.assertRaises(CmsApiError): public.validate_observation(changed, chosen, state, **context)
        changed_state = {**deepcopy(state), "observation_digest": "f" * 64}
        with self.assertRaises(CmsApiError): public.validate_observation(proof, chosen, changed_state, **context)
