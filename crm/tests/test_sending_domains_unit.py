"""Database-free checks for sending domains: the setting, DNS-over-HTTPS answers and record verdicts.

HTTP never leaves the process: ``crm.sending_domains.http_client.get`` or
``lookup`` is mocked.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.exceptions import ValidationError

from community_chat.crm import serializers as wire
from crm import sending_domains
from crm.sending_domains import FAILING, UNKNOWN, VERIFIED

NOW = datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)
CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def answer(rtype, *data, status=0):
    code = {"TXT": 16, "MX": 15, "CNAME": 5}[rtype]
    return FakeResponse({"Status": status, "Answer": [{"type": code, "data": item} for item in data]})


def resend_zone(domain="mlai.au", overrides=None):
    """A Resend domain whose records all pass, as name/type -> answers; overrides replace entries."""

    zone = {
        (f"resend._domainkey.{domain}", "TXT"): ['"p=MIGfMA0GCSqGSIb3DQEB"'],
        (f"send.{domain}", "TXT"): ['"v=spf1 ip4:52.3.252.119 ~all"'],
        (f"send.{domain}", "MX"): ["10 feedback.forge.rmta.net."],
        (f"_dmarc.{domain}", "TXT"): [f'"v=DMARC1; p=none; rua=mailto:team@{domain}"'],
        (domain, "TXT"): ['"google-site-verification=x"', '"v=spf1 include:_spf.google.com ~all"'],
        (domain, "MX"): ["1 smtp.google.com."],
    }
    zone.update(overrides or {})
    return zone


def zone_lookup(zone):
    def lookup(name, rtype):
        values = zone.get((name, rtype), [])
        return None if values is None else [sending_domains._answer_value(rtype, value) for value in values]

    return lookup


class ConfiguredDomainsTests(SimpleTestCase):
    @override_settings(
        CRM_SENDING_DOMAINS=[
            "MLAI.au:resend",
            "mlai-aus.com:google_relay",
            "mlai.org.au:ses",
            "not a domain:resend",
            "mlai.au:google_relay",
        ]
    )
    def test_entries_are_lowercased_and_bad_ones_skipped(self):
        with self.assertLogs("crm.sending_domains", "WARNING") as logs:
            domains = sending_domains.configured_domains()
        self.assertEqual(domains, {"mlai.au": "resend", "mlai-aus.com": "google_relay"})
        self.assertEqual(len(logs.records), 2)

    def test_domain_of_an_address(self):
        self.assertEqual(sending_domains.domain_of("Hey@MLAI.org.au"), "mlai.org.au")


class LookupTests(SimpleTestCase):
    def test_split_txt_strings_join_and_cnames_are_followed(self):
        response = FakeResponse(
            {
                "Status": 0,
                "Answer": [
                    {"type": 5, "data": "send.forge.rmta.net."},
                    {"type": 16, "data": '"v=spf1 ip4:52.3.252.119 " "~all"'},
                ],
            }
        )
        with patch("crm.sending_domains.http_client.get", return_value=response) as get:
            values = sending_domains.lookup("send.mlai.au", "TXT")
        self.assertEqual(values, ["v=spf1 ip4:52.3.252.119 ~all"])
        self.assertEqual(get.call_args.kwargs["params"], {"name": "send.mlai.au", "type": "TXT"})
        self.assertEqual(get.call_args.kwargs["headers"], {"accept": "application/dns-json"})

    def test_unquoted_answers_and_mx_hosts(self):
        with patch("crm.sending_domains.http_client.get", return_value=answer("TXT", "v=DMARC1; p=none")):
            self.assertEqual(sending_domains.lookup("_dmarc.mlai.au", "TXT"), ["v=DMARC1; p=none"])
        with patch("crm.sending_domains.http_client.get", return_value=answer("MX", "1 SMTP.google.com.")):
            self.assertEqual(sending_domains.lookup("mlai.au", "MX"), ["1 smtp.google.com"])

    def test_nxdomain_is_no_records(self):
        with patch("crm.sending_domains.http_client.get", return_value=answer("TXT", status=3)) as get:
            self.assertEqual(sending_domains.lookup("_dmarc.example.test", "TXT"), [])
        self.assertEqual(get.call_count, 1)

    def test_the_second_resolver_answers_when_the_first_fails(self):
        responses = [requests.ConnectionError("down"), answer("TXT", '"p=abc"')]
        with patch("crm.sending_domains.http_client.get", side_effect=responses) as get:
            self.assertEqual(sending_domains.lookup("resend._domainkey.mlai.au", "TXT"), ["p=abc"])
        self.assertEqual(
            [call.args[0] for call in get.call_args_list],
            ["https://cloudflare-dns.com/dns-query", "https://dns.google/resolve"],
        )

    def test_no_answer_from_either_resolver_is_none(self):
        responses = [FakeResponse({}, status_code=502), answer("TXT", status=2)]
        with patch("crm.sending_domains.http_client.get", side_effect=responses):
            self.assertIsNone(sending_domains.lookup("mlai.au", "TXT"))


@override_settings(CACHES=CACHE)
class DomainHealthTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def health(self, zone, domain="mlai.au", route="resend", **kwargs):
        with patch("crm.sending_domains.lookup", side_effect=zone_lookup(zone)) as lookup:
            result = sending_domains.domain_health(domain, route, now=NOW, **kwargs)
        return result, lookup.call_count

    def records(self, result):
        return {record["key"]: record for record in result["records"]}

    def test_a_complete_resend_domain_is_verified(self):
        result, calls = self.health(resend_zone())
        self.assertEqual(result["status"], VERIFIED)
        self.assertEqual(result["checked_at"], NOW)
        self.assertEqual(calls, 6)
        records = self.records(result)
        self.assertEqual(
            list(records), ["dkim", "return_path_spf", "return_path_mx", "dmarc", "spf", "mx"]
        )
        self.assertEqual(records["dkim"]["host"], "resend._domainkey.mlai.au")
        self.assertEqual(records["dkim"]["found"], ["p=MIGfMA0GCSqGSIb3DQEB"])
        self.assertIn("p=none", records["dmarc"]["detail"])
        self.assertEqual(records["spf"]["found"], ["v=spf1 include:_spf.google.com ~all"])
        self.assertTrue(all(record["status"] == "pass" for record in records.values()))

    def test_a_missing_required_record_fails_the_domain(self):
        result, _ = self.health(resend_zone(overrides={("resend._domainkey.mlai.au", "TXT"): []}))
        self.assertEqual(result["status"], FAILING)
        dkim = self.records(result)["dkim"]
        self.assertEqual((dkim["status"], dkim["detail"]), ("fail", "No DKIM public key."))

    def test_advisory_records_never_fail_the_domain(self):
        zone = resend_zone(overrides={("mlai.au", "TXT"): ['"v=spf1 -all"', '"v=spf1 include:x ~all"'], ("mlai.au", "MX"): []})
        result, _ = self.health(zone)
        records = self.records(result)
        self.assertEqual(result["status"], VERIFIED)
        self.assertEqual((records["spf"]["status"], records["mx"]["status"]), ("fail", "fail"))
        self.assertIn("More than one SPF record", records["spf"]["detail"])

    def test_dmarc_without_reports_is_noted(self):
        zone = resend_zone(overrides={("_dmarc.mlai.au", "TXT"): ['"v=DMARC1; p=quarantine"']})
        dmarc = self.records(self.health(zone)[0])["dmarc"]
        self.assertEqual(dmarc["status"], "pass")
        self.assertEqual(dmarc["detail"], "No rua= address, so no aggregate reports arrive.")

    def test_google_relay_needs_googles_dkim_and_spf_include(self):
        zone = {
            ("google._domainkey.mlai.au", "TXT"): ['"v=DKIM1; k=rsa; p=MIIBIjAN"'],
            ("mlai.au", "TXT"): ['"v=spf1 include:sendersrv.com ~all"'],
            ("_dmarc.mlai.au", "TXT"): ['"v=DMARC1; p=none; rua=mailto:team@mlai.au"'],
            ("mlai.au", "MX"): ["1 smtp.google.com."],
        }
        result, _ = self.health(zone, route="google_relay")
        records = self.records(result)
        self.assertEqual(result["status"], FAILING)
        self.assertEqual(records["dkim"]["status"], "pass")
        self.assertEqual(records["spf"]["detail"], "The SPF record doesn't include _spf.google.com.")

    def test_results_are_cached_until_refreshed(self):
        self.health(resend_zone())
        _, calls = self.health(resend_zone())
        self.assertEqual(calls, 0)
        _, calls = self.health(resend_zone(), refresh=True)
        self.assertEqual(calls, 6)

    def test_failed_lookups_are_unknown_and_not_cached(self):
        result, _ = self.health(resend_zone(overrides={("send.mlai.au", "MX"): None}))
        self.assertEqual(result["status"], UNKNOWN)
        self.assertEqual(self.records(result)["return_path_mx"]["status"], "error")
        _, calls = self.health(resend_zone())
        self.assertEqual(calls, 6)

    @override_settings(CRM_SENDING_DOMAINS=["mlai.au:resend"])
    def test_is_verified_only_for_listed_passing_domains(self):
        with patch("crm.sending_domains.lookup", side_effect=zone_lookup(resend_zone())):
            self.assertTrue(sending_domains.is_verified("mlai.au"))
            self.assertFalse(sending_domains.is_verified("example.com"))


class MailboxInputTests(SimpleTestCase):
    def valid(self, serializer_class, data, **kwargs):
        serializer = serializer_class(data=data, **kwargs)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data

    def test_a_new_sender_is_normalised(self):
        data = self.valid(
            wire.MailboxCreateInput,
            {"email": " Hey@MLAI.au ", "display_name": "  MLAI   Team ", "reply_to": "HI@mlai.au"},
        )
        self.assertEqual(
            dict(data),
            {
                "email": "hey@mlai.au",
                "display_name": "MLAI Team",
                "reply_to": "hi@mlai.au",
                "daily_limit": 500,
                "enabled": False,
            },
        )

    def test_bad_values_are_refused(self):
        cases = [
            ({"email": "nope", "display_name": "MLAI"}, "email"),
            ({"email": "hey@mlai.au", "display_name": "   "}, "display_name"),
            ({"email": "hey@mlai.au", "display_name": "MLAI", "reply_to": "nope"}, "reply_to"),
            ({"email": "hey@mlai.au", "display_name": "MLAI", "daily_limit": 0}, "daily_limit"),
            ({"email": "hey@mlai.au", "display_name": "MLAI", "route": "ses"}, "route"),
        ]
        for data, field in cases:
            with self.subTest(field=field), self.assertRaises(ValidationError) as caught:
                self.valid(wire.MailboxCreateInput, data)
            self.assertIn(field, caught.exception.detail)

    def test_an_edit_cannot_change_the_address_and_keeps_unsent_keys(self):
        with self.assertRaises(ValidationError) as caught:
            self.valid(wire.MailboxUpdateInput, {"email": "other@mlai.au"}, partial=True)
        self.assertEqual(caught.exception.get_codes(), {"email": ["unknown_field"]})
        self.assertEqual(dict(self.valid(wire.MailboxUpdateInput, {"reply_to": ""}, partial=True)), {"reply_to": ""})
