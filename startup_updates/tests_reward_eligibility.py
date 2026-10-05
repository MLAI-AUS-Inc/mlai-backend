"""Bonus verification contracts; no database, provider or live website required."""

from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from startup_updates import reward_eligibility as eligibility, reward_website as website


class RewardEligibilityTests(SimpleTestCase):
    def setUp(self):
        self.company = Obj(name="Example", domain="example.com.au", abn="89000000019", acn="000000019",
                           registered=False, abr_verified_at=None, entity_type_code="",
                           organization=Obj(startup_profile=Obj(short_description="Payments for retailers")))
        self.abr = {"configured": True, "reachable": True, "found": True, "active": True,
                    "abn": "89000000019", "acn": "000000019", "entity_type_code": "PRV",
                    "names": ["EXAMPLE PTY LTD"]}

    def check(self, text="Example — payments made simple for retailers", abr=None):
        with patch.object(eligibility, "cache") as cache, patch(
            "vibe_raising.registration._abr_verifier", return_value=MagicMock(return_value=abr or self.abr)
        ) as verifier, patch.object(eligibility, "website_evidence", return_value=text) as fetch:
            cache.get.return_value = None
            result = eligibility.startup_reward_eligibility(self.company)
            return result, fetch, verifier

    def test_live_registration_and_brand_activity_match_qualifies(self):
        result, _, _ = self.check()
        self.assertTrue(result["eligible"])
        self.assertEqual(result["abn"], "89000000019")
        self.assertFalse(self.company.registered)  # eligibility does not mutate profile

    def test_legal_suffix_punctuation_case_and_small_typo_are_tolerated(self):
        for name in ("Example Pty. Ltd.", "EXAMPLE", "Exampel"):
            with self.subTest(name=name):
                self.company.name = name
                self.assertTrue(self.check()[0]["eligible"])

    def test_registered_trading_name_can_match(self):
        self.abr["names"] = ["SOME HOLDING COMPANY PTY LTD", "EXAMPLE"]
        self.assertTrue(self.check()[0]["eligible"])

    def test_trading_brand_can_link_to_legal_name_or_abn_on_website(self):
        self.abr["names"] = ["SOME HOLDING COMPANY PTY LTD"]
        for evidence in ("Some Holding Company Pty Ltd", "ABN 89 000 000 019"):
            with self.subTest(evidence=evidence):
                self.assertTrue(self.check(f"Example payments for retailers. {evidence}")[0]["eligible"])

    def test_unrelated_abn_or_website_selects_standard_reward(self):
        self.assertFalse(self.check("Unrelated restaurant serving pizza")[0]["eligible"])
        self.abr["names"] = ["Unrelated Company"]
        self.assertFalse(self.check()[0]["eligible"])

    def test_conflicting_activity_selects_standard_reward(self):
        self.assertFalse(self.check("Example serves delicious Italian pizza")[0]["eligible"])

    def test_brand_and_legal_names_do_not_count_as_matching_activity(self):
        self.company.organization.startup_profile.short_description = "Example Pty Ltd builds payments for retailers"
        self.assertFalse(self.check("Example Pty Ltd makes delicious Italian pizza")[0]["eligible"])

    def test_missing_optional_description_is_not_a_failure(self):
        self.company.organization = None
        self.assertTrue(self.check("Example — Welcome to our website")[0]["eligible"])

    def test_missing_website_selects_standard_reward(self):
        self.company.domain = ""
        result, fetch, _ = self.check()
        self.assertEqual(result["reason"], "website_missing")
        fetch.assert_not_called()

    def test_no_identifiers_avoids_outbound_requests(self):
        self.company.abn = self.company.acn = ""
        result, fetch, verifier = self.check()
        self.assertFalse(result["eligible"])
        fetch.assert_not_called()
        verifier.assert_not_called()

    def test_acn_only_resolves_active_registration(self):
        self.company.abn = ""
        self.assertTrue(self.check()[0]["eligible"])

    def test_cancelled_missing_mismatched_or_unreachable_register_never_qualifies(self):
        for changes in ({"active": False}, {"found": False}, {"reachable": False},
                        {"configured": False}, {"abn": "94807394137"}):
            with self.subTest(changes=changes):
                result, fetch, _ = self.check(abr={**self.abr, **changes})
                self.assertFalse(result["eligible"])
                fetch.assert_not_called()

    def test_website_failure_is_nonblocking(self):
        with patch.object(eligibility, "cache") as cache, patch.object(
            eligibility, "verify_and_persist_company_registration", return_value=self.abr
        ), patch.object(eligibility, "website_evidence", side_effect=TimeoutError):
            cache.get.return_value = None
            self.assertEqual(eligibility.startup_reward_eligibility(self.company)["reason"], "website_unconfirmed")

    def test_input_changes_do_not_reuse_verification(self):
        with patch.object(eligibility, "cache") as cache, patch.object(eligibility, "_check", return_value={"eligible": True}):
            cache.get.return_value = None
            eligibility.startup_reward_eligibility(self.company)
            first = cache.get.call_args.args[0]
            for field in ("name", "domain", "abn", "acn"):
                setattr(self.company, field, "changed")
                eligibility.startup_reward_eligibility(self.company)
                self.assertNotEqual(cache.get.call_args.args[0], first)
                first = cache.get.call_args.args[0]
            self.company.organization.startup_profile.short_description = "Different business"
            eligibility.startup_reward_eligibility(self.company)
            self.assertNotEqual(cache.get.call_args.args[0], first)


class RewardWebsiteTests(SimpleTestCase):
    def response(self, body=b"<title>Example</title><p>Payment services</p>", status=200, headers=None):
        return Obj(status=status, headers=headers or {"Content-Type": "text/html"},
                   read1=MagicMock(side_effect=[body, b""]), close=MagicMock())

    def test_html_text_and_metadata_are_read_without_script_content(self):
        response = self.response(b'<title>Example</title><meta name="description" content="Payments"><script>secret</script>Retailers')
        with patch.object(website.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]), patch.object(
            website.urllib3, "HTTPSConnectionPool"
        ) as pool:
            pool.return_value.request.return_value = response
            text = website.website_evidence("example.com.au")
            self.assertIn("Payments", text)
            self.assertIn("Example", text)
            self.assertNotIn("secret", text)
            self.assertEqual(pool.call_args.args[0], "8.8.8.8")
            self.assertEqual(pool.call_args.kwargs["assert_hostname"], "example.com.au")
            self.assertEqual(pool.call_args.kwargs["server_hostname"], "example.com.au")
            self.assertEqual(pool.return_value.request.call_args.kwargs["headers"]["Host"], "example.com.au")
            response.close.assert_called_once()

    def test_private_address_and_mixed_public_private_dns_are_rejected(self):
        for addresses in (["127.0.0.1"], ["8.8.8.8", "169.254.169.254"], ["::1"]):
            with self.subTest(addresses=addresses), patch.object(website.socket, "getaddrinfo", return_value=[
                (2, 1, 6, "", (ip, 443)) for ip in addresses
            ]), patch.object(website.urllib3, "HTTPSConnectionPool") as pool:
                with self.assertRaises(ValueError):
                    website.website_evidence("example.com.au")
                pool.assert_not_called()

    def test_redirect_to_private_network_is_rejected(self):
        responses = [[(2, 1, 6, "", ("8.8.8.8", 443))], [(2, 1, 6, "", ("127.0.0.1", 443))]]
        with patch.object(website.socket, "getaddrinfo", side_effect=responses), patch.object(
            website.urllib3, "HTTPSConnectionPool"
        ) as pool:
            pool.return_value.request.return_value = self.response(status=302, headers={"Location": "https://127.0.0.1/"})
            with self.assertRaises(ValueError):
                website.website_evidence("example.com.au")
            self.assertEqual(pool.call_count, 1)

    def test_bad_scheme_credentials_and_ports_are_rejected_before_lookup(self):
        for url in ("file:///etc/passwd", "https://user:pass@example.com", "https://example.com:1234", "https://localhost"):
            with self.subTest(url=url), patch.object(website.socket, "getaddrinfo") as dns:
                with self.assertRaises(ValueError):
                    website.website_evidence(url)
                dns.assert_not_called()

    def test_non_html_error_and_oversized_pages_are_rejected(self):
        for response in (self.response(headers={"Content-Type": "image/png"}), self.response(status=500),
                         self.response(body=b"x" * (website.MAX_BYTES + 1))):
            with patch.object(website.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]), patch.object(
                website.urllib3, "HTTPSConnectionPool"
            ) as pool:
                pool.return_value.request.return_value = response
                with self.assertRaises(ValueError):
                    website.website_evidence("example.com.au")
                response.close.assert_called_once()

    def test_trickling_page_cannot_extend_evidence_deadline(self):
        response = self.response()
        response.read1.side_effect = [b"a"] * 20
        with patch.object(website.time, "monotonic", side_effect=range(30)), patch.object(
            website.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]
        ), patch.object(website.urllib3, "HTTPSConnectionPool") as pool:
            pool.return_value.request.return_value = response
            with self.assertRaises(TimeoutError):
                website.website_evidence("example.com.au")
            self.assertLess(response.read1.call_count, 20)
            response.close.assert_called_once()

    def test_dns_wait_is_bounded_and_cancelled(self):
        with patch.object(website, "_DNS_POOL") as resolver:
            resolver.submit.return_value.result.side_effect = TimeoutError
            with self.assertRaises(TimeoutError):
                website.website_evidence("example.com.au")
            self.assertLessEqual(resolver.submit.return_value.result.call_args.kwargs["timeout"], 2)
            resolver.submit.return_value.cancel.assert_called_once()
