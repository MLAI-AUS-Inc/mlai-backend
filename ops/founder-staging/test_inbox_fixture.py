"""Real loopback HTTP and retention controls; no Django, DB or external mail."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
import http.client
import importlib
import io
import json
import os
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
import uuid

fixture = importlib.import_module("ops.founder-staging.inbox_fixture")
DOMAIN = "founder-fixtures.staging.example.test"
READER, WRITER = "R" * 48, "W" * 48  # Synthetic test credentials only.


def address():
    return "founder-acceptance-" + str(uuid.uuid4()) + "@" + DOMAIN


class Clock:
    seconds = 0

    def monotonic(self):
        return self.seconds

    def utc(self):
        return datetime(2026, 11, 4, 12, tzinfo=timezone.utc) + timedelta(seconds=self.seconds)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.inbox = fixture.Inbox(DOMAIN, clock=self.clock.monotonic, wall_clock=self.clock.utc, capacity=2)
        self.email, self.delivery = address(), str(uuid.uuid4())
        self.after = self.clock.utc().isoformat()
        self.expires = (self.clock.utc() + timedelta(seconds=600)).isoformat()

    def record(self, delivery, email, code, *, issued_at=None, expires_at=None):
        return self.inbox.record(delivery, email, code, issued_at or self.after, expires_at or self.expires)

    def test_only_fresh_uuid_addresses_in_the_exact_staging_domain(self):
        self.assertEqual(self.inbox.validate_email(self.email), self.email)
        for value in ("sam@mlai.au", "founder-acceptance-not-a-uuid@" + DOMAIN,
                      self.email.replace(DOMAIN, "production.example.test"), self.email.upper(), self.email + "\n"):
            with self.subTest(kind="nonfixture address"), self.assertRaises(ValueError):
                self.record(self.delivery, value, "004200")

    def test_retry_retains_original_timestamp_and_cannot_extend_expiry(self):
        self.record(self.delivery, self.email, "004200")
        original = self.inbox.read(self.email, self.after)
        self.clock.seconds = 299
        self.record(self.delivery, self.email, "004200")
        self.assertEqual(self.inbox.read(self.email, self.after), original)
        self.assertIsNone(self.inbox.read(self.email, self.clock.utc().isoformat()))
        self.clock.seconds = 301
        self.assertIsNone(self.inbox.read(self.email, self.after))

    def test_retry_conflict_and_capacity_do_not_replace_valid_deliveries(self):
        self.record(self.delivery, self.email, "004200")
        with self.assertRaises(fixture.Conflict):
            self.record(self.delivery, self.email, "999999")
        self.record(str(uuid.uuid4()), address(), "123456")
        with self.assertRaises(fixture.Capacity):
            self.record(str(uuid.uuid4()), address(), "654321")
        self.assertEqual(self.inbox.read(self.email, self.after)["code"], "004200")

    def test_same_digits_in_a_new_delivery_are_not_mistaken_for_an_old_retry(self):
        self.record(self.delivery, self.email, "004200")
        self.clock.seconds = 1
        self.record(str(uuid.uuid4()), self.email, "004200", issued_at=self.clock.utc().isoformat(),
                    expires_at=(self.clock.utc() + timedelta(seconds=600)).isoformat())
        self.assertEqual(self.inbox.read(self.email, self.clock.utc().isoformat())["sentAt"], "2026-11-04T12:00:01.000Z")

    def test_redelivery_after_expiry_or_store_recreation_cannot_become_fresh(self):
        self.record(self.delivery, self.email, "004200")
        self.clock.seconds = 100
        restarted = fixture.Inbox(DOMAIN, clock=self.clock.monotonic, wall_clock=self.clock.utc)
        restarted.record(self.delivery, self.email, "004200", self.after, self.expires)
        later_attempt = (self.clock.utc() - timedelta(seconds=1)).isoformat()
        self.assertIsNone(restarted.read(self.email, later_attempt))
        self.assertEqual(restarted.read(self.email, self.after)["code"], "004200")
        self.clock.seconds = 301
        for inbox in (self.inbox, restarted, fixture.Inbox(DOMAIN, clock=self.clock.monotonic, wall_clock=self.clock.utc)):
            with self.subTest(kind="expired source"), self.assertRaises(ValueError):
                inbox.record(self.delivery, self.email, "004200", self.after, self.expires)
            self.assertIsNone(inbox.read(self.email, self.after))

    def test_immutable_source_times_cannot_be_advanced_on_a_retry(self):
        self.record(self.delivery, self.email, "004200")
        self.clock.seconds = 1
        with self.assertRaises(fixture.Conflict):
            self.record(self.delivery, self.email, "004200", issued_at=self.clock.utc().isoformat(),
                        expires_at=(self.clock.utc() + timedelta(seconds=600)).isoformat())
        self.assertIsNone(self.inbox.read(self.email, self.clock.utc().isoformat()))

    def test_future_expired_naive_or_unbounded_source_times_are_refused(self):
        now = self.clock.utc()
        for issued, expires in ((now + timedelta(seconds=1), now + timedelta(seconds=601)),
                (now - timedelta(seconds=301), now + timedelta(seconds=299)), (now, now),
                (now, now + timedelta(seconds=601)), (now.replace(tzinfo=None), now + timedelta(seconds=600))):
            with self.subTest(kind="invalid source time"), self.assertRaises(ValueError):
                self.record(self.delivery, self.email, "004200", issued_at=issued.isoformat(), expires_at=expires.isoformat())

    def test_concurrent_retries_keep_one_delivery(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.record(self.delivery, self.email, "004200"), range(16)))
        self.assertEqual(len(self.inbox._deliveries), 1)
        self.assertEqual(self.inbox.read(self.email, self.after)["code"], "004200")

    def test_invalid_codes_ids_or_naive_cutoffs_are_refused(self):
        for delivery, code in (("bad", "123456"), (self.delivery.upper(), "123456"), (self.delivery, "12345"),
                               (self.delivery, 123456), (self.delivery, "１２３４５６")):
            with self.subTest(kind="invalid delivery"), self.assertRaises(ValueError):
                self.record(delivery, self.email, code)
        with self.assertRaises(ValueError):
            self.inbox.read(self.email, "2026-11-04T12:00:00")

    def test_configuration_is_explicit_and_cannot_expand_bounds(self):
        for domain in ("mlai.au", "staging.example.test/", "UPPER.staging.example.test", "@staging.example.test"):
            with self.subTest(kind="domain"), self.assertRaises(ValueError):
                fixture.Inbox(domain)
        with self.assertRaises(ValueError):
            fixture.Inbox(DOMAIN, capacity=257)
        for reader, writer in ((READER, READER), ("", WRITER), (READER, "short")):
            with self.subTest(kind="credential"), self.assertRaises(ValueError):
                fixture.handler(self.inbox, reader, writer)
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(SystemExit):
            fixture.main()


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.inbox = fixture.Inbox(DOMAIN, clock=self.clock.monotonic, wall_clock=self.clock.utc, capacity=2)
        self.server = fixture.InboxServer(("127.0.0.1", 0), fixture.handler(self.inbox, READER, WRITER))
        self.server.inbox = self.inbox
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.email, self.delivery = address(), str(uuid.uuid4())
        self.after = self.clock.utc().isoformat()
        self.expires = (self.clock.utc() + timedelta(seconds=600)).isoformat()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, *, token=None, body=None, headers=None):
        request_headers = {"Content-Type": "application/json"}
        if token:
            request_headers["Authorization"] = "Bearer " + token
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            value = json.loads(response.read())
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            return response.status, value
        finally:
            connection.close()

    def lookup(self, **overrides):
        return "/inbox?" + urlencode({"email": self.email, "after": self.after, **overrides})

    def payload(self, **overrides):
        return json.dumps({"deliveryId": self.delivery, "email": self.email, "code": "004200",
                           "issuedAt": self.after, "expiresAt": self.expires, **overrides})

    def test_actual_provider_delivery_and_exact_authenticated_fresh_lookup(self):
        self.assertEqual(self.request("GET", self.lookup(), token=READER)[0], 404)
        status, receipt = self.request("POST", "/deliver", token=WRITER, body=self.payload())
        self.assertEqual((status, receipt), (202, {"accepted": True, "deliveryId": self.delivery}))
        status, message = self.request("GET", self.lookup(), token=READER)
        self.assertEqual((status, message), (200, {"email": self.email, "code": "004200", "sentAt": "2026-11-04T12:00:00.000Z"}))
        self.assertEqual(self.request("GET", self.lookup(email=address()), token=READER)[0], 404)
        self.assertEqual(self.request("GET", self.lookup(after="2026-11-04T12:00:01Z"), token=READER)[0], 404)

    def test_read_and_write_authority_are_separate(self):
        for token in (None, WRITER, "invalid"):
            self.assertEqual(self.request("GET", self.lookup(), token=token)[0], 401)
        for token in (None, READER, "invalid"):
            self.assertEqual(self.request("POST", "/deliver", token=token, body=self.payload())[0], 401)

    def test_conflict_and_capacity_are_reported_without_evicting_a_code(self):
        self.assertEqual(self.request("POST", "/deliver", token=WRITER, body=self.payload())[0], 202)
        self.assertEqual(self.request("POST", "/deliver", token=WRITER, body=self.payload(code="999999"))[0], 409)
        self.assertEqual(self.request("POST", "/deliver", token=WRITER, body=self.payload(deliveryId=str(uuid.uuid4()), email=address()))[0], 202)
        self.assertEqual(self.request("POST", "/deliver", token=WRITER, body=self.payload(deliveryId=str(uuid.uuid4()), email=address()))[0], 503)
        self.assertEqual(self.request("GET", self.lookup(), token=READER)[1]["code"], "004200")

    def test_http_input_is_bounded_and_nonfixture_mail_is_rejected(self):
        self.assertEqual(self.request("POST", "/deliver", token=WRITER, body=b"x" * 2049)[0], 413)
        for body in ("[]", "{}", '{"email":"x","email":"y"}', b"\xff", self.payload(email="sam@mlai.au")):
            self.assertEqual(self.request("POST", "/deliver", token=WRITER, body=body)[0], 400)
        self.assertEqual(self.request("GET", self.lookup() + "&email=" + address(), token=READER)[0], 400)
        self.assertEqual(self.request("GET", self.lookup(after="naive"), token=READER)[0], 400)
        self.assertEqual(self.request("POST", "/deliver", token=WRITER, body=self.payload(), headers={"Content-Type": "text/plain"})[0], 400)

    def test_idle_service_actions_expire_codes_and_health_exposes_no_inbox_data(self):
        self.request("POST", "/deliver", token=WRITER, body=self.payload())
        self.clock.seconds = 301
        self.server.service_actions()
        self.assertEqual(len(self.inbox._deliveries), 0)
        self.assertEqual(self.request("GET", "/healthz"), (200, {"status": "ok"}))
        self.assertEqual(self.request("GET", self.lookup(), token=READER)[0], 404)

    def test_actual_http_redelivery_cannot_refresh_an_expired_challenge(self):
        self.assertEqual(self.request("POST", "/deliver", token=WRITER, body=self.payload())[0], 202)
        self.clock.seconds = 301
        self.assertEqual(self.request("POST", "/deliver", token=WRITER, body=self.payload())[0], 400)
        self.assertEqual(self.request("GET", self.lookup(), token=READER)[0], 404)

    def test_request_addresses_codes_and_credentials_never_enter_access_logs(self):
        log = io.StringIO()
        with redirect_stderr(log):
            self.request("POST", "/deliver", token=WRITER, body=self.payload())
            self.request("GET", self.lookup(), token=READER)
            self.request("POST", "/deliver", token=READER, body=self.payload())
        self.assertEqual(log.getvalue(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
