"""Plan E1, E2, E4 and E5: the website preflight, the one name rule, a website
required at the My Startup create, and the own-profile duplicate check.

No test fetches a real website or asks a real resolver: every answer comes
from ``test_data/website_preflight_catalogue.json`` (simulated responses, see
its ``about``) or from a fake in the test, through the real probe's redirect
and classification code.
"""
import json
import socket
import ssl
from functools import partial
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from content_factory.founder_journey_testing import ALL_P1_FLAGS_ON
from content_factory.website_live_fetch import probe_homepage, resolve_public
from founder_tools import website_hosts, website_preflight
from founder_tools.models import VibeRaisingCompany
from founder_tools.startup_lookalikes import cross_account_lookalikes

CATALOGUE = json.loads((Path(__file__).with_name("test_data") / "website_preflight_catalogue.json").read_text())
PREFLIGHT_URL = "/api/v1/founder-tools/website-preflight/"
ALIAS_URL = "/api/v1/my-startup/founder-tools/website-preflight/"
COMPANIES_URL = "/api/v1/founder-tools/companies/"
PUBLIC_ADDRESS = "93.184.216.34"
GATE = "ARTICLES_FIRST_ARTICLE_REQUIRES_CONNECTED_WEBSITE"
PREFLIGHT = "MY_STARTUP_WEBSITE_PREFLIGHT_ENABLED"


class _Response:
    def __init__(self, spec):
        self.status = spec.get("status", 200)
        self._headers = list((spec.get("headers") or {}).items())
        self._body = str(spec.get("html") or "").encode()

    def getheaders(self):
        return self._headers

    def read(self, limit):
        return self._body[:limit]

    def close(self):
        pass


class FakeNet:
    """A recorded internet: names that resolve, and each host's homepage answer."""

    def __init__(self, responses, *, resolves=(), nxdomain=(), addresses=None):
        self.responses = dict(responses)
        self.resolves = set(resolves) | set(self.responses)
        self.nxdomain = set(nxdomain)
        self.addresses = dict(addresses or {})
        self.lookups, self.connections = [], []

    def resolve(self, host):
        self.lookups.append(host)
        if host in self.addresses:
            value = self.addresses[host]
            return ("ok", [value]) if value == PUBLIC_ADDRESS else ("not_public", [])
        if host in self.resolves and host not in self.nxdomain:
            return "ok", [PUBLIC_ADDRESS]
        return "nxdomain", []

    def connection(self, host, address):
        net = self

        class Connection:
            def request(self, method, path, headers):
                net.connections.append((host, address, path, headers.get("Host"), headers.get("User-Agent")))
                spec = net.responses.get(host) or {"status": 404}
                if spec.get("refused"):
                    raise ConnectionRefusedError()
                if spec.get("timeout"):
                    raise socket.timeout()
                if spec.get("tls"):
                    raise ssl.SSLCertVerificationError("bad certificate")
                self.spec = spec

            def getresponse(self):
                return _Response(self.spec)

            def close(self):
                pass

        return Connection()

    def check(self, host):
        return website_preflight.check_domain(host, probe=partial(probe_homepage, connection=self.connection),
                                              resolve=self.resolve)


def _catalogue_net():
    responses, nxdomain = {}, set()
    for row in CATALOGUE["rows"]:
        responses.update(row["responses"])
        if row["dns"] == "nxdomain":
            nxdomain.add(row["domain"])
    return FakeNet(responses, resolves=CATALOGUE["resolves"], nxdomain=nxdomain)


class CatalogueTests(SimpleTestCase):
    """E1 acceptance: every catalogue domain classifies as expected."""

    def test_the_catalogue_domains_classify_as_expected(self):
        self.assertEqual(len(CATALOGUE["rows"]), 44)  # 46 catalogue orgs less the two test orgs with no domain
        net = _catalogue_net()
        for row in CATALOGUE["rows"]:
            expect = row["expect"]
            with self.subTest(org=row["org"], domain=row["domain"]):
                host = website_preflight.normalise(row["domain"])
                if expect.get("invalid"):
                    self.assertEqual(host, "")
                    continue
                self.assertEqual(host, row["domain"])
                result = net.check(host)
                self.assertEqual(result["reachable"], expect["reachable"])
                self.assertEqual(result["check"]["outcome"], expect["outcome"])
                self.assertEqual(result["hostClass"], expect["hostClass"])
                self.assertEqual(result["guidanceId"], expect["guidanceId"])
                self.assertEqual(result["deploymentUrl"], expect["deploymentUrl"])
                self.assertEqual(result["suggestion"], expect["suggestion"])
                self.assertEqual(result["suggestedName"], expect["suggestedName"])
                if "httpStatus" in expect:
                    self.assertEqual(result["check"]["httpStatus"], expect["httpStatus"])
                if "note" in expect:
                    self.assertIn(expect["note"], [note["code"] for note in result["notes"]])
                self.assertNotEqual(result["suggestedName"], "Workers")

    def test_a_403_homepage_is_unknown_never_no(self):
        result = _catalogue_net().check("platform.tailoredagents.ai")
        self.assertEqual(result["reachable"], "unknown")
        self.assertEqual(result["check"], {"outcome": "http_status", "httpStatus": 403, "redirectHost": None})
        self.assertIn("carry on", result["notes"][0]["message"])

    def test_only_nxdomain_and_refused_are_unreachable(self):
        for outcome, reachable in (("ok", "yes"), ("redirect", "yes"), ("nxdomain", "no"), ("refused", "no"),
                                   ("http_status", "unknown"), ("timeout", "unknown"), ("dns_error", "unknown"),
                                   ("tls_invalid", "unknown"), ("tls_error", "unknown"), ("connect_error", "unknown"),
                                   ("not_public", "unknown")):
            with self.subTest(outcome):
                observed = {"outcome": outcome, "status": None, "headers": {}, "body": b"", "redirectHost": None}
                result = website_preflight.check_domain("example.com", probe=lambda host, resolve: observed,
                                                        resolve=lambda host: ("ok", [PUBLIC_ADDRESS]))
                self.assertEqual(result["reachable"], reachable)
                self.assertEqual(result["check"]["outcome"], outcome)

    def test_a_probe_crash_is_classified_not_swallowed(self):
        def crash(host, resolve):
            raise RuntimeError("boom")
        result = website_preflight.check_domain("example.com", probe=crash, resolve=lambda host: ("ok", []))
        self.assertEqual((result["reachable"], result["check"]["outcome"]), ("unknown", "connect_error"))
        self.assertEqual(result["notes"][0]["code"], "check_unavailable")


class FingerprintTests(SimpleTestCase):
    """Host classes from a response, on example domains (never a founder's site)."""

    def test_response_fingerprints_name_the_host(self):
        page = "<html><head><title>Acme</title>{}</head></html>"
        cases = (
            ({"x-wix-request-id": "1"}, "", "wix", "no_export"),
            ({}, '<meta name="generator" content="Wix.com Website Builder">', "wix", "no_export"),
            ({"server": "Squarespace"}, "", "squarespace", "no_export"),
            ({}, '<meta name="generator" content="Webflow">', "webflow", "no_export"),
            ({}, '<script src="https://framerusercontent.com/a.js"></script>', "framer", "no_export"),
            ({"x-shopid": "1"}, "", "shopify", "no_export"),
            ({"x-vercel-id": "syd1::1"}, "", "vercel", "git_host"),
            ({"server": "Netlify"}, "", "netlify", "git_host"),
            ({"server": "GitHub.com"}, "", "github_pages", "git_host"),
            ({"server": "cloudflare"}, "", None, "unknown"),
        )
        for headers, extra, guidance, host_class in cases:
            with self.subTest(guidance=guidance, headers=headers):
                net = FakeNet({"acme.example.com": {"status": 200, "headers": headers, "html": page.format(extra)}})
                result = net.check("acme.example.com")
                self.assertEqual((result["guidanceId"], result["hostClass"]), (guidance, host_class))
                self.assertFalse(result["deploymentUrl"])

    def test_a_content_system_is_advice_not_a_host_class(self):
        net = FakeNet({"acme.example.com": {"status": 200, "html": '<meta name="generator" content="WordPress 6.6">'}})
        result = net.check("acme.example.com")
        self.assertEqual((result["contentSystem"], result["hostClass"]), ("wordpress", "unknown"))

    def test_an_off_site_redirect_is_reported_never_followed(self):
        net = FakeNet({"acme.example.com": {"status": 302, "headers": {"location": "https://acme.lovable.app/"}},
                       "acme.lovable.app": {"status": 200, "html": "<title>Leak</title>"}})
        result = net.check("acme.example.com")
        self.assertEqual(result["check"]["outcome"], "redirect")
        self.assertEqual(result["check"]["redirectHost"], "acme.lovable.app")
        self.assertEqual((result["guidanceId"], result["hostClass"]), ("lovable", "ai_builder"))
        self.assertEqual([row[0] for row in net.connections], ["acme.example.com"])
        self.assertIn("redirects", [note["code"] for note in result["notes"]])

    def test_platform_hosts_are_deployment_addresses(self):
        for host, guidance, host_class in (
            ("acme.vercel.app", "vercel", "git_host"), ("acme.netlify.app", "netlify", "git_host"),
            ("acme.pages.dev", "cloudflare_pages", "git_host"), ("acme.onrender.com", "render", "git_host"),
            ("acme.github.io", "github_pages", "git_host"), ("acme.lovable.app", "lovable", "ai_builder"),
            ("acme.bolt.host", "bolt", "ai_builder"), ("acme.replit.app", "replit", "ai_builder"),
            ("acme.base44.app", "base44", "ai_builder"), ("acme.framer.website", "framer", "no_export"),
            ("acme.webflow.io", "webflow", "no_export"), ("acme.myshopify.com", "shopify", "no_export"),
        ):
            with self.subTest(host):
                result = FakeNet({host: {"status": 200, "html": "<title>Acme</title>"}}).check(host)
                self.assertEqual((result["guidanceId"], result["hostClass"], result["deploymentUrl"]),
                                 (guidance, host_class, True))
        # The platform's own site is not one of its customers' sites.
        self.assertIsNone(website_hosts.platform_for("squarespace.com"))


class ProbeTests(SimpleTestCase):
    """The status-returning probe keeps the SSRF guards of the verification fetch."""

    def test_resolve_public_tells_nxdomain_from_a_temporary_failure(self):
        cases = (
            (socket.gaierror(socket.EAI_NONAME, "Name or service not known"), "nxdomain"),
            (socket.gaierror(socket.EAI_AGAIN, "Temporary failure"), "dns_error"),
        )
        for error, state in cases:
            with self.subTest(state), patch("socket.getaddrinfo", side_effect=error):
                self.assertEqual(resolve_public("acme.example.com"), (state, []))

    def test_resolve_public_refuses_any_non_global_answer(self):
        for answers in (["10.0.0.5"], ["93.184.216.34", "127.0.0.1"], ["fe80::1%eth0"], ["169.254.169.254"]):
            rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (value, 443)) for value in answers]
            with self.subTest(answers), patch("socket.getaddrinfo", return_value=rows):
                self.assertEqual(resolve_public("acme.example.com"), ("not_public", []))
        rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_ADDRESS, 443))]
        with patch("socket.getaddrinfo", return_value=rows):
            self.assertEqual(resolve_public("acme.example.com"), ("ok", [PUBLIC_ADDRESS]))

    def test_a_non_public_address_is_never_connected_to(self):
        net = FakeNet({}, addresses={"acme.example.com": "10.0.0.5"})
        result = probe_homepage("acme.example.com", resolve=net.resolve, connection=net.connection)
        self.assertEqual(result["outcome"], "not_public")
        self.assertEqual(net.connections, [])

    def test_a_www_hop_is_repinned_and_resolved_again(self):
        net = FakeNet({"acme.example.com": {"status": 301, "headers": {"location": "http://www.acme.example.com/home"}},
                       "www.acme.example.com": {"status": 200, "html": "<title>Acme</title>"}},
                      addresses={"www.acme.example.com": "10.0.0.9"})
        result = probe_homepage("acme.example.com", resolve=net.resolve, connection=net.connection)
        # The www hop resolved to a private address: never fetched.
        self.assertEqual(result["outcome"], "not_public")
        self.assertEqual([row[0] for row in net.connections], ["acme.example.com"])
        net.addresses.clear()
        result = probe_homepage("acme.example.com", resolve=net.resolve, connection=net.connection)
        self.assertEqual((result["outcome"], result["finalHost"]), ("ok", "www.acme.example.com"))
        self.assertEqual(net.connections[-1][2:4], ("/home", "www.acme.example.com"))

    def test_connection_faults_are_classified(self):
        for spec, outcome in (({"refused": True}, "refused"), ({"timeout": True}, "timeout"),
                              ({"tls": True}, "tls_invalid")):
            with self.subTest(outcome):
                net = FakeNet({"acme.example.com": spec})
                result = probe_homepage("acme.example.com", resolve=net.resolve, connection=net.connection)
                self.assertEqual(result["outcome"], outcome)


class TypoTests(SimpleTestCase):
    def test_a_misspelt_domain_gets_the_spelling_that_resolves(self):
        candidates = website_preflight.typo_candidates("reidandcoconsuliting.com.au")
        self.assertIn("reidandcoconsulting.com.au", candidates)
        self.assertLessEqual(len(candidates), website_preflight.TYPO_MAX_CANDIDATES)
        self.assertTrue(all(candidate.endswith(".com.au") for candidate in candidates))

    def test_no_suggestion_unless_it_resolves(self):
        net = FakeNet({}, nxdomain={"reidandcoconsuliting.com.au"})
        self.assertIsNone(website_preflight.suggest_spelling("reidandcoconsuliting.com.au", resolve=net.resolve))

    def test_a_tld_slip_is_fixed_first(self):
        self.assertEqual(website_preflight.typo_candidates("acmeworks.con")[0], "acmeworks.com")

    def test_a_platform_suffix_is_never_edited(self):
        for candidate in website_preflight.typo_candidates("lazarus-care.chalkypeach513.chatgpt.site"):
            self.assertTrue(candidate.endswith(".chalkypeach513.chatgpt.site"), candidate)

    def test_short_labels_get_no_edit_guesses(self):
        self.assertEqual(website_preflight.typo_candidates("abc.com"), [])


class NameRuleTests(SimpleTestCase):
    """E2: one name rule, owned by the server."""

    def test_the_platform_hostnames_in_the_catalogue(self):
        for domain, expected in (
            ("spring-surf-fc2e.sofia-vi-lee.workers.dev", ""),
            ("orange-sea-9c9f.kienplays9.workers.dev", ""),
            ("auditmind-prod.up.railway.app", "Auditmind"),
            ("lazarus-care.chalkypeach513.chatgpt.site", "Lazarus Care"),
            ("find-your-way-10.emergent.host", "Find Your Way"),
        ):
            with self.subTest(domain):
                self.assertEqual(website_hosts.derive_company_name(domain), expected)

    def test_own_domains(self):
        for domain, expected in (
            ("platform.tailoredagents.ai", "Tailoredagents"), ("examon.jtutes.com", "Examon"),
            ("prognosole.eliascorp.org", "Prognosole"), ("la-vie.com.au", "La Vie"),
            ("banksia.studio", "Banksia"), ("banksia-studio.com.au", "Banksia Studio"),
            ("www.ledger_mates.co.nz", "Ledger Mates"), ("app.banksia.io", "Banksia"),
            ("https://banksia.studio/about", "Banksia"), ("startup-1.invalid", ""), ("", ""),
        ):
            with self.subTest(domain):
                self.assertEqual(website_hosts.derive_company_name(domain), expected)

    def test_the_homepage_name_comes_first_and_template_titles_never_count(self):
        for meta, expected in (
            ({"ogSiteName": "Lilia", "title": "Lilia Daily Planner"}, "Lilia"),
            ({"title": "Lilia Daily Planner | Plan your day"}, "Lilia Daily Planner"),
            ({"title": "Vite + React + TS"}, ""),
            ({"title": "Just a moment..."}, ""),
            ({"title": "spring-surf-fc2e.sofia-vi-lee.workers.dev"}, ""),
        ):
            with self.subTest(meta):
                self.assertEqual(website_hosts.suggested_company_name("spring-surf-fc2e.sofia-vi-lee.workers.dev",
                                                                      meta), expected)

    def test_the_content_factory_name_is_the_same_rule(self):
        from content_factory.startup_first_profile import derive_company_name
        self.assertEqual(derive_company_name("auditmind-prod.up.railway.app"), "Auditmind")

    def test_site_keys(self):
        self.assertEqual(website_hosts.site_key("platform.tailoredagents.ai"), website_hosts.site_key("tailoredagents.ai"))
        self.assertNotEqual(website_hosts.site_key("prognosole.eliascorp.org"), website_hosts.site_key("viterbi.eliascorp.org"))
        self.assertNotEqual(website_hosts.site_key("orange-sea-9c9f.kienplays9.workers.dev"),
                            website_hosts.site_key("spring-surf-fc2e.sofia-vi-lee.workers.dev"))
        self.assertNotEqual(website_hosts.site_key("studionarrm.com"), website_hosts.site_key("stolenminutes.app"))
        self.assertEqual(website_hosts.site_key("startup-1.invalid"), "")


def _user(email):
    return get_user_model().objects.create_user(email=email, first_name="Ada", last_name="Lovelace", role="participant")


class _ApiBase:
    def setUp(self):
        patcher = patch("founder_tools.services.reconcile_user_slack_id_from_email")
        patcher.start()
        self.addCleanup(patcher.stop)
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = _user("preflight@example.test")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.net = _catalogue_net()
        self.net.responses["banksia.studio"] = {"status": 200, "html": "<title>Banksia | Garden design</title>"}
        self.net.resolves.add("banksia.studio")
        probe = patch("content_factory.website_live_fetch.probe_homepage",
                      side_effect=lambda host, resolve: probe_homepage(host, resolve=resolve,
                                                                       connection=self.net.connection))
        resolve = patch("content_factory.website_live_fetch.resolve_public", side_effect=self.net.resolve)
        probe.start()
        resolve.start()
        self.addCleanup(probe.stop)
        self.addCleanup(resolve.stop)

    def preflight(self, domain, client=None, url=PREFLIGHT_URL):
        return (client or self.client).post(url, {"domain": domain}, format="json")

    def create(self, body, key, client=None):
        return (client or self.client).post(COMPANIES_URL, {"createNew": True, "firstProfile": True, **body},
                                            format="json", HTTP_IDEMPOTENCY_KEY=key)


@override_settings(**{**ALL_P1_FLAGS_ON, PREFLIGHT: True})
class PreflightEndpointTests(_ApiBase, TestCase):
    @override_settings(**{PREFLIGHT: False})
    def test_off_it_is_404_and_nothing_is_fetched(self):
        response = self.preflight("lifestitch.com.au")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["reason"], "not_enabled")
        self.assertEqual(self.net.connections, [])

    def test_it_needs_an_account(self):
        self.assertIn(self.preflight("lifestitch.com.au", client=APIClient()).status_code, {401, 403})

    def test_an_address_that_is_not_a_website_is_refused_without_a_lookup(self):
        for value in ("", "not a website", "localhost", "127.0.0.1", "startup-1.invalid", "x" * 301, "intranet.local"):
            with self.subTest(value):
                response = self.preflight(value)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["reason"], "invalid_website")
        self.assertEqual(self.net.lookups, [])

    def test_the_answer_is_cached_per_domain(self):
        first = self.preflight("https://www.Lifestitch.com.au/shop")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data["normalised"], "lifestitch.com.au")
        self.assertFalse(first.data["cached"])
        fetched = len(self.net.connections)
        second = self.preflight("lifestitch.com.au")
        self.assertTrue(second.data["cached"])
        self.assertEqual(len(self.net.connections), fetched)

    def test_the_chat_alias_answers_with_its_own_throttle(self):
        response = self.preflight("lifestitch.com.au", url=ALIAS_URL)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(website_preflight.FounderWebsitePreflightView.throttle_scope, "founder_website_preflight")

    def test_a_403_homepage_is_not_blocked(self):
        response = self.preflight("platform.tailoredagents.ai")
        self.assertEqual(response.data["reachable"], "unknown")
        with override_settings(**{GATE: True}):
            created = self.create({"domain": "platform.tailoredagents.ai"}, key="k-403")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["name"], "Tailoredagents")

    def test_a_deployment_url_is_accepted_with_a_note(self):
        response = self.preflight("auditmind-prod.up.railway.app")
        self.assertTrue(response.data["deploymentUrl"])
        self.assertIn("public address", response.data["notes"][0]["message"])
        with override_settings(**{GATE: True}):
            self.assertEqual(self.create({"domain": "auditmind-prod.up.railway.app"}, key="k-rail").status_code, 201)

    def test_the_server_name_is_saved(self):
        response = self.preflight("spring-surf-fc2e.sofia-vi-lee.workers.dev")
        self.assertEqual(response.data["suggestedName"], "Lilia Daily Planner")
        created = self.create({"domain": "spring-surf-fc2e.sofia-vi-lee.workers.dev"}, key="k-lilia")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["name"], "Lilia Daily Planner")

    def test_existing_is_only_ever_your_own(self):
        mine = self.create({"domain": "banksia.studio"}, key="k-mine")
        self.assertEqual(mine.status_code, 201, mine.data)
        response = self.preflight("app.banksia.studio")
        self.assertEqual(response.data["existing"]["state"], "yours")
        self.assertEqual(response.data["existing"]["companyId"], mine.data["id"])
        other = APIClient()
        other.force_authenticate(user=_user("other@example.test"))
        theirs = self.preflight("banksia.studio", client=other)
        self.assertEqual(theirs.data["existing"], {"state": "none"})
        self.assertNotIn(mine.data["id"], json.dumps(theirs.data))


@override_settings(**{**ALL_P1_FLAGS_ON, GATE: True, PREFLIGHT: True})
class WebsiteRequiredTests(_ApiBase, TestCase):
    """E4: a website is required at the My Startup one-field create only."""

    def test_create_without_a_domain_returns_website_required(self):
        for body in ({"name": "Ledger Mates", "shortDescription": "Bookkeeping for tradies"},
                     {"name": "Ledger Mates", "domain": ""},
                     {"name": "Placeholder", "domain": "startup-5d7facfa.invalid"}):
            with self.subTest(body):
                response = self.create(body, key=f"k-{body['name']}-{body.get('domain')}")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(response.data["reason"], "website_required")
        self.assertFalse(VibeRaisingCompany.objects.exists())

    def test_an_address_that_does_not_exist_needs_use_it_anyway(self):
        response = self.create({"domain": "reidandcoconsuliting.com.au"}, key="k-typo")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["reason"], "website_not_found")
        self.assertEqual(response.data["suggestion"], "reidandcoconsulting.com.au")
        anyway = self.create({"domain": "reidandcoconsuliting.com.au", "useAnyway": True}, key="k-typo-anyway")
        self.assertEqual(anyway.status_code, 201, anyway.data)

    @override_settings(**{PREFLIGHT: False})
    def test_with_the_preflight_off_any_real_hostname_passes(self):
        self.assertEqual(self.create({"domain": "reidandcoconsuliting.com.au"}, key="k-off").status_code, 201)
        self.assertEqual(self.net.connections, [])

    @override_settings(**{GATE: False, "MY_STARTUP_NO_WEBSITE_ARTICLES_ENABLED": True})
    def test_with_the_gate_off_the_no_website_create_is_unchanged(self):
        response = self.create({"name": "Ledger Mates", "shortDescription": "Bookkeeping for tradies"}, key="k-nw")
        self.assertEqual(response.status_code, 201, response.data)

    def test_the_legacy_company_create_still_takes_no_domain(self):
        # Fundraising and the old add-startup form post without firstProfile.
        response = self.client.post(COMPANIES_URL, {"createNew": True, "name": "Raising Co"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(VibeRaisingCompany.objects.get(pk=response.data["id"]).domain)

    def test_connector_and_fundraising_organisations_still_bind_domainless(self):
        from founder_tools.services import ensure_company_organization, get_or_create_founder_profile
        company = VibeRaisingCompany.objects.create(profile=get_or_create_founder_profile(self.user), name="Connector Co")
        organization = ensure_company_organization(company)
        self.assertIsNotNone(organization)
        self.assertTrue(organization.domain.endswith(".invalid"))


@override_settings(**{**ALL_P1_FLAGS_ON, PREFLIGHT: True})
class DuplicateTests(_ApiBase, TestCase):
    """E5: the duplicate check reads the caller's own profile only."""

    def test_same_name_twice_offers_open_it(self):
        first = self.create({"name": "SafeTrase", "domain": "safetrase.com"}, key="k-185")
        self.assertEqual(first.status_code, 201, first.data)
        again = self.create({"name": "Safe Trase", "domain": "safetrase.com.au"}, key="k-186")
        self.assertEqual(again.status_code, 409, again.data)
        self.assertEqual(again.data["reason"], "already_added")
        self.assertEqual(again.data["companyId"], first.data["id"])
        self.assertEqual(again.data["match"], "name")
        self.assertTrue(again.data["canAddAnyway"])
        self.assertIn("You already added this", again.data["detail"])
        anyway = self.create({"name": "Safe Trase", "domain": "safetrase.com.au", "notDuplicate": True}, key="k-186b")
        self.assertEqual(anyway.status_code, 201, anyway.data)

    def test_the_same_site_after_stripping_a_generic_subdomain(self):
        first = self.create({"domain": "tailoredagents.ai"}, key="k-ta")
        self.assertEqual(first.status_code, 201, first.data)
        again = self.create({"domain": "platform.tailoredagents.ai", "notDuplicate": True}, key="k-ta2")
        self.assertEqual(again.status_code, 409, again.data)
        self.assertEqual((again.data["match"], again.data["canAddAnyway"]), ("domain", False))

    def test_one_founder_two_real_startups_are_not_matched(self):
        # Orgs 194 and 195: the same founder, two startups, different names and sites.
        first = self.create({"name": "Studio Narrm", "domain": "studionarrm.com"}, key="k-194")
        second = self.create({"name": "Stolen Minutes", "domain": "stolenminutes.app"}, key="k-195")
        self.assertEqual((first.status_code, second.status_code), (201, 201), second.data)
        self.assertEqual(cross_account_lookalikes(), [])
        # Two startups on named subdomains of one holding domain (189 and 193).
        self.assertEqual(self.create({"domain": "prognosole.eliascorp.org"}, key="k-189").status_code, 201)
        self.assertEqual(self.create({"domain": "viterbi.eliascorp.org"}, key="k-193").status_code, 201)

    def test_another_account_is_never_named_and_goes_to_the_staff_list(self):
        mine = self.create({"domain": "orange-sea-9c9f.kienplays9.workers.dev", "name": "Lilia Daily Planner"},
                           key="k-208")
        self.assertEqual(mine.status_code, 201, mine.data)
        other = APIClient()
        other.force_authenticate(user=_user("sofia@example.test"))
        theirs = self.create({"domain": "spring-surf-fc2e.sofia-vi-lee.workers.dev"}, key="k-209", client=other)
        self.assertEqual(theirs.status_code, 201, theirs.data)
        self.assertNotIn(mine.data["id"], json.dumps(theirs.data))
        groups = cross_account_lookalikes()
        self.assertEqual([(group["match"], group["key"]) for group in groups], [("name", "liliadailyplanner")])
        self.assertEqual({row["companyId"] for row in groups[0]["companies"]}, {mine.data["id"], theirs.data["id"]})
        out, err = StringIO(), StringIO()
        call_command("founder_duplicate_lookalikes", stdout=out, stderr=err)
        self.assertEqual(json.loads(out.getvalue().strip())["key"], "liliadailyplanner")
        self.assertIn("nothing was changed", err.getvalue())
