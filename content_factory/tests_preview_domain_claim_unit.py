"""DB/network-free coverage of preview domain claims.

Domain matching is pure. The claim itself reads a handful of models, which these
tests replace with in-memory rows (``Rows``) supporting just the lookups
``claim_basis`` uses, so every basis and refusal runs without a database.
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from itertools import count
from types import SimpleNamespace as NS
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from content_factory import preview_domain_claim as claims
from content_factory.preview_domain_claim import (PreviewDomainClaimServiceView, covers, free_mail, public_host,
                                                  registrable_domain)

KEY = "synthetic-test-key"
NOW = datetime(2026, 10, 30, tzinfo=dt_timezone.utc)
LOOKUPS = {"in", "isnull", "icontains", "iendswith"}


def _value(row, parts):
    for part in parts:
        row = row.get(part) if isinstance(row, dict) else getattr(row, part, None)
    return row


def _same(got, want):
    if hasattr(want, "pk"):
        return getattr(got, "pk", object()) == want.pk
    return got == want


def _match(row, key, want):
    parts = key.split("__")
    lookup = parts.pop() if len(parts) > 1 and parts[-1] in LOOKUPS else "exact"
    got = _value(row, parts)
    if lookup == "in":
        return any(_same(got, item) for item in want)
    if lookup == "isnull":
        return (got is None) is want
    if lookup == "icontains":
        return isinstance(got, str) and want.lower() in got.lower()
    if lookup == "iendswith":
        return isinstance(got, str) and got.lower().endswith(want.lower())
    return _same(got, want)


class Rows:
    """The slice of the queryset API ``claim_basis`` uses, over in-memory rows."""

    def __init__(self, rows):
        self.rows = list(rows)

    def filter(self, **lookups):
        return Rows(row for row in self.rows if all(_match(row, key, want) for key, want in lookups.items()))

    def exclude(self, **lookups):
        return Rows(row for row in self.rows if not all(_match(row, key, want) for key, want in lookups.items()))

    def order_by(self, *fields):
        rows = list(self.rows)
        for field in reversed(fields):
            rows.sort(key=lambda row: _value(row, field.lstrip("-").split("__")), reverse=field.startswith("-"))
        return Rows(rows)

    def values_list(self, field, flat=False):
        assert flat
        return Rows(_value(row, field.split("__")) for row in self.rows)

    def exists(self):
        return bool(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def __getitem__(self, index):
        return Rows(self.rows[index]) if isinstance(index, slice) else self.rows[index]

    def __iter__(self):
        return iter(self.rows)


class World:
    """Synthetic tenants, founders, runs and the records that may prove a domain."""

    def __init__(self):
        self.ids = count(1)
        self.orgs, self.runs, self.users, self.companies = [], [], [], []
        self.sites, self.connections, self.operations, self.challenges = [], [], [], []

    def org(self, domain):
        org = NS(pk=next(self.ids), domain=domain)
        self.orgs.append(org)
        return org

    def founder(self, org, email, *, verified=True):
        user = NS(pk=next(self.ids), is_active=True, email=email, email_verified_at=NOW if verified else None)
        self.users.append(user)
        self.company(org, user)
        return user

    def company(self, org, user, domain=None):
        self.companies.append(NS(organization=org, profile=NS(user=user), domain=domain or (org.domain if org else "")))

    def run(self, run_id, org, owner=None):
        request = {"roo_points_requested_by_user_id": owner.pk} if owner else {}
        run = NS(run_id=run_id, organization_id=org.pk if org else None, organization=org,
                 domain=org.domain if org else "", run_request=request)
        self.runs.append(run)
        return run

    def site(self, org, domain, state="active"):
        self.sites.append(NS(organization=org, domain=domain, state=state))

    def connection(self, org, site_url="", state="connected"):
        connection = NS(pk=next(self.ids), organization=org, state=state, site_url=site_url, generation=3)
        self.connections.append(connection)
        return connection

    def receipt(self, connection, public_url, *, action="deployment-verify", state="completed", age=timedelta(0),
                generation=None, **receipt):
        receipt = {"status": "passed", "provider_verified": True, "public_url": public_url, **receipt}
        self.operations.append(NS(connection=connection, action=action, state=state, created_at=NOW - age,
                                  generation=connection.generation if generation is None else generation,
                                  receipt=receipt))

    def code(self, user, email, *, consumed=True):
        self.challenges.append(NS(user=user, email_digest=claims.email_digest(email),
                                  consumed_at=NOW if consumed else None))

    def basis(self, run_id, domain):
        managers = ((claims.ContentFactoryRun, self.runs), (claims.Organization, self.orgs),
                    (claims.HostedArticleSite, self.sites), (claims.WebsiteConnection, self.connections),
                    (claims.WebsiteConnectionOperation, self.operations),
                    (claims.CommunityChatEmailCodeChallenge, self.challenges),
                    (claims.VibeRaisingCompany, self.companies))
        patches = [patch.object(model, "objects", Rows(rows)) for model, rows in managers]
        patches.append(patch.object(claims, "get_user_model", lambda: NS(objects=Rows(self.users))))
        for active in patches:
            active.start()
        try:
            return claims.claim_basis(run_id, public_host(domain))
        finally:
            for active in reversed(patches):
                active.stop()


class DomainMatchingTests(SimpleTestCase):
    def test_every_spelling_of_one_site_normalizes_to_the_same_host(self):
        for value in ("pawshare.com.au", "PawShare.com.au.", "www.pawshare.com.au", "https://WWW.PawShare.com.au:443/blog/?x=1",
                      "http://pawshare.com.au:8080", "//pawshare.com.au/"):
            self.assertEqual(public_host(value), "pawshare.com.au", value)
        self.assertEqual(public_host("bücher.de"), "xn--bcher-kva.de")
        self.assertEqual(public_host("XN--BCHER-KVA.DE"), "xn--bcher-kva.de")
        self.assertEqual(public_host("ＥＸＡＭＰＬＥ．ｃｏｍ"), "example.com")
        self.assertEqual(public_host("www.co.uk"), "co.uk")

    def test_anything_that_is_not_a_public_dns_name_is_refused(self):
        for value in (None, "", " ", "localhost", "intranet", "a.localhost", "startup-1.invalid", "127.0.0.1", "[::1]", "::1",
                      "10.0.0.1:80", "1.2.3.4.5", "owner@pawshare.com.au", "https://a:b@pawshare.com.au", "ftp://pawshare.com.au",
                      "javascript:alert(1)", "pawshare.com.au:99999", "pawshare.com.au:port", "a_b.com", "-a.com", "a..com",
                      ".a.com", "pawshare com.au", "pawshare\\.com.au", "a%2e.com", "😀.com", "x" * 64 + ".com",
                      ".".join(["a"] * 128) + ".com"):
            self.assertEqual(public_host(value), "", value)

    def test_the_registrable_domain_stops_at_public_suffixes_and_shared_hosts(self):
        for host, registrable in (("blog.pawshare.com.au", "pawshare.com.au"), ("pawshare.com.au", "pawshare.com.au"),
                                  ("shop.acme.co.uk", "acme.co.uk"), ("acme.au", "acme.au"), ("docs.example.com", "example.com"),
                                  ("x.alice.github.io", "alice.github.io"), ("acme.co.jp", "acme.co.jp"), ("acme.us", "acme.us"),
                                  ("www2.acme.qc.ca", "acme.qc.ca"), ("acme.gr.jp", "acme.gr.jp")):
            self.assertEqual(registrable_domain(host), registrable, host)
        for host in ("com.au", "co.uk", "nsw.au", "github.io", "pages.dev", "health.nsw.gov.au", "unimelb.edu.au", "mit.edu",
                     "ox.ac.uk", "qc.ca", "gr.jp"):
            self.assertEqual(registrable_domain(host), "", host)

    def test_one_tenant_of_a_nested_shared_root_never_covers_the_others(self):
        for root, tenant in (("up.railway.app", "x.up.railway.app"), ("my.canva.site", "x.my.canva.site"),
                             ("blogspot.com.au", "x.blogspot.com.au"), ("blogspot.de", "x.blogspot.de"),
                             ("uk.com", "acme.uk.com"), ("medium.com", "x.medium.com"), ("eu.org", "acme.eu.org"),
                             ("qc.ca", "acme.qc.ca")):
            self.assertEqual(registrable_domain(root), "", root)
            self.assertEqual(registrable_domain(tenant), tenant, tenant)
            self.assertFalse(covers(root, tenant), root)

    def test_deep_cloud_roots_tunnels_and_wildcard_dns_are_never_claimable(self):
        for host in ("bob.s3.amazonaws.com", "s3.amazonaws.com", "bob.s3-website-us-east-1.amazonaws.com",
                     "x.execute-api.us-east-1.amazonaws.com", "app.us-east-1.elasticbeanstalk.com", "svc-abc.a.run.app",
                     "proj.cloudfunctions.net", "acct.blob.core.windows.net", "x.r.appspot.com", "x.2.azurestaticapps.net",
                     "abc.ngrok-free.app", "abc.ngrok.io", "x.trycloudflare.com", "127.0.0.1.nip.io", "x.sslip.io",
                     "victim-com.translate.goog", "victim-com.cdn.ampproject.org", "x.replit.dev"):
            self.assertEqual(registrable_domain(public_host(host)), "", host)

    def test_government_and_education_namespaces_are_never_claimable(self):
        for host in ("x.nhs.uk", "nhs.uk", "x.police.uk", "x.gc.ca", "x.canada.ca", "x.gouv.qc.ca", "x.europa.eu", "x.bund.de",
                     "x.k12.ca.us", "k12.ca.us", "ca.us", "ci.sf.ca.us", "x.lg.jp", "x.tokyo.jp", "x.school.nz", "x.health.nz"):
            self.assertEqual(registrable_domain(host), "", host)
        self.assertFalse(covers("k12.ca.us", "x.k12.ca.us"))

    def test_a_record_covers_its_own_domain_and_subdomains_only(self):
        self.assertTrue(covers("pawshare.com.au", "pawshare.com.au"))
        self.assertTrue(covers("https://www.pawshare.com.au/", "blog.pawshare.com.au"))
        self.assertTrue(covers("bücher.de", "xn--bcher-kva.de"))
        self.assertTrue(covers("alice.github.io", "alice.github.io"))
        self.assertFalse(covers("blog.pawshare.com.au", "pawshare.com.au"))
        self.assertFalse(covers("shop.pawshare.com.au", "blog.pawshare.com.au"))
        self.assertFalse(covers("notpawshare.com.au", "pawshare.com.au"))
        self.assertFalse(covers("com.au", "pawshare.com.au"))
        self.assertFalse(covers("github.io", "alice.github.io"))
        self.assertFalse(covers("alice.github.io", "bob.github.io"))
        self.assertFalse(covers("", "pawshare.com.au"))
        self.assertFalse(covers(None, "pawshare.com.au"))

    def test_mail_providers_are_recognised_across_countries_isps_relays_and_disposable_inboxes(self):
        for domain in ("gmail.com", "outlook.com.au", "bigpond.net.au", "mail.yahoo.com", "web.de", "gmx.de", "gmx.at",
                       "t-online.de", "orange.fr", "free.fr", "laposte.net", "libero.it", "naver.com", "daum.net",
                       "hanmail.net", "yandex.ru", "ya.ru", "inbox.ru", "list.ru", "bk.ru", "rediffmail.com", "seznam.cz",
                       "wp.pl", "o2.pl", "onet.pl", "protonmail.ch", "proton.ch", "tutanota.de", "tuta.com", "duck.com",
                       "mailbox.org", "posteo.de", "aim.com", "comcast.net", "verizon.net", "att.net", "sbcglobal.net",
                       "btinternet.com", "sky.com", "virginmedia.com", "shaw.ca", "rogers.com", "xtra.co.nz",
                       "skymail.com.au", "aussiebb.com.au", "sina.com", "sohu.com", "foxmail.com", "yeah.net", "email.com",
                       "usa.com", "fastmail.net", "fastmail.org", "fastmail.com.au", "yahoo.fr", "yahoo.co.jp",
                       "yahoo.co.in", "hotmail.fr", "hotmail.de", "live.co.uk", "mailinator.com", "yopmail.com",
                       "guerrillamail.com", "sharklasers.com", "10minutemail.com", "temp-mail.org", "simplelogin.com",
                       "addy.io", "mozmail.com", "relay.firefox.com"):
            self.assertTrue(free_mail(public_host(domain)), domain)
        for domain in ("pawshare.com.au", "notgmail.com", "liveshop.com", "mail.pawshare.com.au", "google.com"):
            self.assertFalse(free_mail(public_host(domain)), domain)


class ClaimBasisTests(SimpleTestCase):
    def setUp(self):
        self.world = World()
        self.org = self.world.org("pawshare.example.com")
        self.owner = self.world.founder(self.org, "founder@founders.example.org")
        self.world.run("claim-run", self.org, self.owner)

    def basis(self, domain, run_id="claim-run"):
        return self.world.basis(run_id, domain)

    def prove_mailbox(self, email):
        self.owner.email = email
        self.world.code(self.owner, email)

    def test_an_unknown_run_or_a_run_without_an_organisation_claims_nothing(self):
        self.assertEqual(self.basis("pawshare.example.com", run_id="missing-run"), "none")
        self.world.run("orphan-run", None)
        self.assertEqual(self.basis("pawshare.example.com", run_id="orphan-run"), "none")

    def test_an_active_or_draft_hosted_site_of_the_runs_organisation_is_the_strongest_basis(self):
        self.world.site(self.org, "brand.example.com", state="draft")
        self.assertEqual(self.basis("brand.example.com"), "hosted_site")
        self.assertEqual(self.basis("https://www.brand.example.com/blog/"), "hosted_site")
        self.world.sites[0].state = "paused"
        self.assertEqual(self.basis("brand.example.com"), "none")

    def test_a_connection_with_no_live_deployment_receipt_proves_nothing(self):
        # BEB-1: a typed site_url plus any bound repository is not evidence of the live site.
        self.world.connection(self.org, site_url="https://victim-brand.com/")
        self.assertEqual(self.basis("victim-brand.com"), "none")
        self.world.connection(self.org, site_url=self.org.domain)
        self.assertEqual(self.basis(self.org.domain), "org_website")

    def test_a_passed_live_deployment_receipt_for_the_domain_is_a_basis_at_any_age(self):
        connection = self.world.connection(self.org)
        self.world.receipt(connection, f"https://{self.org.domain}/articles", age=timedelta(days=900), generation=1)
        self.assertEqual(self.basis(self.org.domain), "website_connection")
        self.assertEqual(self.basis(f"https://www.{self.org.domain}/blog/"), "website_connection")
        self.assertEqual(self.basis(f"blog.{self.org.domain}"), "website_connection")
        connection.state = "paused"
        self.assertEqual(self.basis(self.org.domain), "website_connection")
        for state in ("revoked", "disconnected"):
            connection.state = state
            self.assertEqual(self.basis(self.org.domain), "org_website", state)

    def test_a_receipt_only_counts_for_its_own_domain_and_only_when_it_passed(self):
        connection = self.world.connection(self.org)
        self.world.receipt(connection, "https://another-brand.example.net/articles")
        self.world.receipt(connection, "https://victim-brand.com/articles", status="failed")
        self.world.receipt(connection, "https://victim-brand.com/articles", provider_verified=False)
        self.world.receipt(connection, "https://victim-brand.com/articles", provider_verified="true")
        self.world.receipt(connection, "https://victim-brand.com/articles", state="pending")
        self.world.receipt(connection, "https://victim-brand.com/articles", action="ci-verify")
        self.world.receipt(connection, "https://github.io/articles")
        for domain in ("victim-brand.com", "github.io", "x.github.io", "example.net"):
            self.assertEqual(self.basis(domain), "none", domain)
        self.assertEqual(self.basis("another-brand.example.net"), "website_connection")

    def test_another_organisations_receipt_never_lends_a_basis(self):
        rival = self.world.org("rival-brand.example.com")
        self.world.receipt(self.world.connection(rival), "https://victim-brand.com/articles")
        self.assertEqual(self.basis("victim-brand.com"), "none")

    def test_a_verified_stamp_without_proof_for_the_current_address_proves_nothing(self):
        # BEB-2: email_verified_at survives a profile email change.
        self.world.code(self.owner, self.owner.email)
        self.owner.email = "ceo@victim-brand.com"
        self.assertEqual(self.basis("victim-brand.com"), "none")

    def test_a_consumed_code_for_the_current_work_address_is_a_basis(self):
        self.prove_mailbox("founder@brand.example.com")
        self.assertEqual(self.basis("brand.example.com"), "email_domain")
        self.assertEqual(self.basis("www.brand.example.com"), "email_domain")
        self.assertEqual(self.basis("other-brand.example.com"), "none")
        self.owner.email_verified_at = None
        self.assertEqual(self.basis("brand.example.com"), "none")

    def test_an_unconsumed_code_or_another_users_code_proves_nothing(self):
        self.owner.email = "founder@brand.example.com"
        self.world.code(self.owner, self.owner.email, consumed=False)
        self.world.code(NS(pk=999), self.owner.email)
        self.assertEqual(self.basis("brand.example.com"), "none")

    def test_an_email_only_counts_for_the_runs_requester_while_they_belong_to_its_organisation(self):
        self.prove_mailbox("founder@brand.example.com")
        run = self.world.runs[0]
        for requester in (None, "", "not-a-user", True, 10 ** 30, self.owner.pk + 1000):
            run.run_request = {"roo_points_requested_by_user_id": requester}
            self.assertEqual(self.basis("brand.example.com"), "none", requester)
        run.run_request = {"roo_points_requested_by_user_id": str(self.owner.pk)}
        self.assertEqual(self.basis("brand.example.com"), "email_domain")
        self.owner.is_active = False
        self.assertEqual(self.basis("brand.example.com"), "none")
        self.owner.is_active = True
        self.world.companies[0].organization = None
        self.assertEqual(self.basis("brand.example.com"), "none")

    def test_a_domain_with_verified_members_in_other_organisations_is_a_mail_provider(self):
        self.prove_mailbox("founder@brand.example.com")
        other = self.world.org("elsewhere.example.org")
        # The owner's own second startup and an unverified stranger say nothing about the domain.
        self.world.company(other, self.owner)
        self.world.founder(other, "stranger@brand.example.com", verified=False)
        self.assertEqual(self.basis("brand.example.com"), "email_domain")
        self.world.founder(other, "member@BRAND.example.com")
        self.assertEqual(self.basis("brand.example.com"), "none")

    def test_free_mail_addresses_and_mail_providers_typed_as_websites_prove_nothing(self):
        # BEB-3: a genuine mailbox at a provider never claims the provider's own site.
        for provider in ("web.de", "orange.fr", "naver.com", "comcast.net", "yahoo.co.jp", "hotmail.fr", "yandex.ru",
                         "protonmail.ch", "mailinator.com", "gmail.com"):
            world = World()
            org = world.org(provider)
            owner = world.founder(org, f"someone@{provider}")
            world.code(owner, owner.email)
            world.site(org, provider)
            world.receipt(world.connection(org), f"https://{provider}/articles")
            world.run("provider-run", org, owner)
            self.assertEqual(world.basis("provider-run", provider), "none", provider)
            self.assertEqual(world.basis("provider-run", f"mail.{provider}"), "none", provider)

    def test_the_organisations_saved_website_is_the_weakest_basis(self):
        self.assertEqual(self.basis("pawshare.example.com"), "org_website")
        self.assertEqual(self.basis("https://Blog.PawShare.example.com.:8443/posts"), "org_website")
        self.assertEqual(self.basis("example.com"), "none")
        self.assertEqual(self.basis("shop.example.com"), "none")
        self.world.company(self.org, self.owner, "second-brand.example.com")
        self.assertEqual(self.basis("second-brand.example.com"), "org_website")

    def test_bases_are_reported_strongest_first(self):
        domain = self.org.domain
        self.prove_mailbox(f"founder@{domain}")
        self.world.site(self.org, domain)
        self.world.receipt(self.world.connection(self.org), f"https://{domain}/articles")
        self.assertEqual(self.basis(domain), "hosted_site")
        self.world.sites.clear()
        self.assertEqual(self.basis(domain), "website_connection")
        self.world.operations.clear()
        self.assertEqual(self.basis(domain), "email_domain")
        self.world.challenges.clear()
        self.assertEqual(self.basis(domain), "org_website")

    def test_another_organisations_site_is_never_claimed_whatever_spelling_it_was_stored_under(self):
        # BEB-7: Organization.domain keeps ports and trailing dots, so every stored spelling must block.
        for stored in ("tenant-brand.com", "tenant-brand.com.", "tenant-brand.com:443", "https://tenant-brand.com/",
                       "TENANT-BRAND.COM", "www.tenant-brand.com", "tenant-brand.com:abc"):
            world = World()
            world.org(stored)
            attacker = world.org("tenant-brand.com")
            world.receipt(world.connection(attacker), "https://tenant-brand.com/articles")
            world.run("attacker-run", attacker)
            self.assertEqual(world.basis("attacker-run", "tenant-brand.com"), "none", stored)

    def test_a_parent_or_child_held_by_another_organisation_blocks_the_claim(self):
        rival = self.world.org("shop.acme-group.com")
        self.world.site(rival, "blog.other-group.com")
        self.world.company(self.org, self.owner, "acme-group.com")
        self.world.company(self.org, self.owner, "other-group.com")
        self.world.company(self.org, self.owner, "deep.shop.acme-group.com")
        for domain in ("acme-group.com", "other-group.com", "shop.acme-group.com", "deep.shop.acme-group.com"):
            self.assertEqual(self.basis(domain), "none", domain)
        # A tenant whose name merely contains this one's is someone else.
        self.world.org("notpawshare.example.com")
        self.world.org("pawshare.example.com.evil.example.net")
        self.assertEqual(self.basis(self.org.domain), "org_website")

    def test_a_rival_stored_under_its_unicode_name_blocks_its_a_label(self):
        self.world.org("bücher.de")
        self.world.company(self.org, self.owner, "xn--bcher-kva.de")
        self.assertEqual(self.basis("xn--bcher-kva.de"), "none")
        self.assertEqual(self.basis("shop.xn--bcher-kva.de"), "none")

    def test_each_run_still_claims_its_own_site(self):
        rival = self.world.org("rival.example.com")
        rival_owner = self.world.founder(rival, "rival@rival.example.com")
        self.world.run("rival-run", rival, rival_owner)
        self.world.site(rival, "rival-blog.example.com")
        self.assertEqual(self.basis("rival.example.com", run_id="rival-run"), "org_website")
        self.assertEqual(self.basis("rival-blog.example.com", run_id="rival-run"), "hosted_site")
        self.assertEqual(self.basis(self.org.domain, run_id="rival-run"), "none")
        self.assertEqual(self.basis(self.org.domain), "org_website")
        for domain in ("rival.example.com", "blog.rival.example.com", "rival-blog.example.com"):
            self.assertEqual(self.basis(domain), "none", domain)


@override_settings(ROO_API_KEY=KEY, INTERNAL_API_KEY=KEY)
class PreviewDomainClaimViewTests(SimpleTestCase):
    def claim(self, params, **headers):
        request = APIRequestFactory().get("/synthetic", params, **(headers or {"HTTP_X_API_KEY": KEY}))
        return PreviewDomainClaimServiceView.as_view()(request)

    def test_the_service_key_is_required(self):
        with patch.object(claims, "claim_basis", return_value="org_website") as claim:
            params = {"run_id": "claim-run", "domain": "pawshare.example.com"}
            self.assertEqual(self.claim(params, HTTP_ACCEPT="application/json").status_code, 403)
            self.assertEqual(self.claim(params, HTTP_X_API_KEY="wrong-key").status_code, 403)
            claim.assert_not_called()
            response = self.claim(params, HTTP_AUTHORIZATION=f"Api-Key {KEY}")
            self.assertEqual((response.status_code, response.data), (200, {"basis": "org_website"}))
            claim.assert_called_once_with("claim-run", "pawshare.example.com")

    def test_the_domain_is_normalised_before_the_claim(self):
        with patch.object(claims, "claim_basis", return_value="none") as claim:
            response = self.claim({"run_id": "claim-run", "domain": "https://WWW.PawShare.example.com:443/blog/"})
        self.assertEqual((response.status_code, response.data), (200, {"basis": "none"}))
        claim.assert_called_once_with("claim-run", "pawshare.example.com")

    def test_malformed_requests_are_refused_without_a_lookup(self):
        with patch.object(claims, "claim_basis") as claim:
            for run_id, domain in (("", "pawshare.example.com"), ("../claim-run", "pawshare.example.com"),
                                   ("x" * 101, "pawshare.example.com"), ("claim-run", ""), ("claim-run", "localhost"),
                                   ("claim-run", "127.0.0.1"), ("claim-run", "owner@pawshare.example.com"),
                                   ("claim-run", "pawshare.example.com:99999"), ("claim-run", "ftp://pawshare.example.com"),
                                   ("claim-run", "pawshare_example.com")):
                response = self.claim({"run_id": run_id, "domain": domain})
                self.assertEqual((response.status_code, response.data["basis"], response.data["reason"]),
                                 (400, "none", "invalid_request"), (run_id, domain))
            self.assertEqual(self.claim({}).status_code, 400)
            claim.assert_not_called()
