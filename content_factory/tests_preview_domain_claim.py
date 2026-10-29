"""A run's preview copies a live site's design only for a domain its own organisation controls."""
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import resolve
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun

from .hosted_article_models import HostedArticleSite
from .preview_domain_claim import PreviewDomainClaimServiceView, covers, free_mail, public_host, registrable_domain
from .website_models import WebsiteConnection

KEY = "synthetic-test-key"


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
        self.assertEqual(registrable_domain("blog.pawshare.com.au"), "pawshare.com.au")
        self.assertEqual(registrable_domain("pawshare.com.au"), "pawshare.com.au")
        self.assertEqual(registrable_domain("shop.acme.co.uk"), "acme.co.uk")
        self.assertEqual(registrable_domain("acme.au"), "acme.au")
        self.assertEqual(registrable_domain("docs.example.com"), "example.com")
        self.assertEqual(registrable_domain("x.alice.github.io"), "alice.github.io")
        for host in ("com.au", "co.uk", "nsw.au", "github.io", "pages.dev", "health.nsw.gov.au", "unimelb.edu.au", "mit.edu",
                     "ox.ac.uk"):
            self.assertEqual(registrable_domain(host), "", host)

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

    def test_free_mail_providers_are_recognised(self):
        for domain in ("gmail.com", "outlook.com.au", "bigpond.net.au", "mail.yahoo.com"):
            self.assertTrue(free_mail(domain), domain)
        for domain in ("pawshare.com.au", "notgmail.com"):
            self.assertFalse(free_mail(domain), domain)


@override_settings(ROO_API_KEY=KEY, INTERNAL_API_KEY=KEY)
class PreviewDomainClaimTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.org, self.owner, self.run = self.tenant("pawshare.example.test", "claim-run")

    def tenant(self, domain, run_id):
        # A verified address that proves nothing about the tenant's own site until a test moves it there.
        user = get_user_model().objects.create_user(email=f"{run_id}@founders.example.org", email_verified_at=timezone.now())
        org = Organization.objects.create(domain=domain, name=f"Synthetic {domain}")
        profile = VibeRaisingProfile.objects.create(user=user, role="founder")
        VibeRaisingCompany.objects.create(profile=profile, organization=org, domain=domain, name=f"Synthetic {domain}")
        run = ContentFactoryRun.objects.create(run_id=run_id, domain=domain, organization=org, workflow="direct_generate",
                                               status="completed", github_repo="", result={},
                                               run_request={"delivery_mode": "content_only", "delivery_mode_confirmed": True,
                                                            "roo_points_requested_by_user_id": user.pk})
        return org, user, run

    def claim(self, domain, run_id="claim-run", **headers):
        headers = headers or {"HTTP_X_API_KEY": KEY}
        request = self.factory.get("/synthetic", {"run_id": run_id, "domain": domain}, **headers)
        return PreviewDomainClaimServiceView.as_view()(request)

    def basis(self, domain, run_id="claim-run"):
        response = self.claim(domain, run_id)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["basis"]

    def test_the_route_is_a_service_endpoint(self):
        self.assertIs(resolve("/api/content-factory/preview-domain-claim/").func.view_class, PreviewDomainClaimServiceView)
        self.assertIs(resolve("/api/content-factory/preview-domain-claim").func.view_class, PreviewDomainClaimServiceView)

    def test_the_service_key_is_required(self):
        self.assertEqual(self.claim(self.org.domain, HTTP_ACCEPT="application/json").status_code, 403)
        self.assertEqual(self.claim(self.org.domain, HTTP_X_API_KEY="wrong-key").status_code, 403)
        response = self.claim(self.org.domain, HTTP_AUTHORIZATION=f"Api-Key {KEY}")
        self.assertEqual((response.status_code, response.data), (200, {"basis": "org_website"}))
        self.assertEqual(self.claim(self.org.domain).data, {"basis": "org_website"})

    def test_an_active_or_draft_hosted_site_of_the_runs_organisation_is_the_strongest_basis(self):
        site = HostedArticleSite.objects.create(organization=self.org, domain="brand.example.test", state="draft")
        self.assertEqual(self.basis("brand.example.test"), "hosted_site")
        site.state = "active"
        site.save(update_fields=["state"])
        self.assertEqual(self.basis("https://www.brand.example.test/blog/"), "hosted_site")
        self.assertEqual(self.basis("blog.brand.example.test"), "hosted_site")
        site.state = "paused"
        site.save(update_fields=["state"])
        self.assertEqual(self.basis("brand.example.test"), "none")

    def test_a_hosted_site_stored_under_its_unicode_name_matches_its_a_label(self):
        HostedArticleSite.objects.create(organization=self.org, domain="bücher.example.test", state="active")
        self.assertEqual(self.basis("xn--bcher-kva.example.test"), "hosted_site")
        self.assertEqual(self.basis("BÜCHER.example.test."), "hosted_site")

    def test_a_connected_or_paused_website_connection_is_a_basis(self):
        connection = WebsiteConnection.objects.create(organization=self.org, github_repo="example/brand",
                                                      site_url="https://www.brand.example.test:443/")
        self.assertEqual(self.basis("brand.example.test"), "website_connection")
        connection.state = "paused"
        connection.save(update_fields=["state"])
        self.assertEqual(self.basis("brand.example.test"), "website_connection")
        for state in ("revoked", "disconnected"):
            connection.state = state
            connection.save(update_fields=["state"])
            self.assertEqual(self.basis("brand.example.test"), "none", state)

    def test_the_run_owners_verified_work_email_is_a_basis(self):
        self.owner.email = "founder@brand.example.test"
        self.owner.save(update_fields=["email"])
        self.assertEqual(self.basis("brand.example.test"), "email_domain")
        self.assertEqual(self.basis("www.brand.example.test"), "email_domain")
        self.owner.email_verified_at = None
        self.owner.save(update_fields=["email_verified_at"])
        self.assertEqual(self.basis("brand.example.test"), "none")

    def test_an_email_only_counts_for_the_runs_requester_while_they_belong_to_its_organisation(self):
        self.owner.email = "founder@brand.example.test"
        self.owner.save(update_fields=["email"])
        for requester in (None, "", "not-a-user", True, 10 ** 30, self.owner.pk + 1000):
            self.run.run_request = {**self.run.run_request, "roo_points_requested_by_user_id": requester}
            self.run.save(update_fields=["run_request"])
            self.assertEqual(self.basis("brand.example.test"), "none", requester)
        self.run.run_request = {**self.run.run_request, "roo_points_requested_by_user_id": str(self.owner.pk)}
        self.run.save(update_fields=["run_request"])
        self.assertEqual(self.basis("brand.example.test"), "email_domain")
        VibeRaisingCompany.objects.filter(profile__user=self.owner).update(organization=None)
        self.assertEqual(self.basis("brand.example.test"), "none")

    def test_a_free_mail_address_proves_nothing(self):
        self.owner.email = "founder@gmail.com"
        self.owner.save(update_fields=["email"])
        self.assertEqual(self.basis("gmail.com"), "none")
        self.assertEqual(self.basis("mail.gmail.com"), "none")
        # Nor does a mail provider typed as the startup's website.
        VibeRaisingCompany.objects.create(profile=self.owner.vibe_raising_profile, organization=self.org,
                                          domain="gmail.com", name="Mailbox")
        self.assertEqual(self.basis("gmail.com"), "none")

    def test_the_organisations_saved_website_is_the_weakest_basis(self):
        self.assertEqual(self.basis("pawshare.example.test"), "org_website")
        self.assertEqual(self.basis("https://Blog.PawShare.example.test.:8443/posts"), "org_website")
        self.assertEqual(self.basis("example.test"), "none")
        self.assertEqual(self.basis("shop.example.test"), "none")
        VibeRaisingCompany.objects.create(profile=self.owner.vibe_raising_profile, organization=self.org,
                                          domain="second-brand.example.test", name="Second brand")
        self.assertEqual(self.basis("second-brand.example.test"), "org_website")

    def test_bases_are_reported_strongest_first(self):
        self.owner.email = f"founder@{self.org.domain}"
        self.owner.save(update_fields=["email"])
        site = HostedArticleSite.objects.create(organization=self.org, domain=self.org.domain, state="active")
        connection = WebsiteConnection.objects.create(organization=self.org, github_repo="example/site", site_url=self.org.domain)
        self.assertEqual(self.basis(self.org.domain), "hosted_site")
        site.delete()
        self.assertEqual(self.basis(self.org.domain), "website_connection")
        connection.delete()
        self.assertEqual(self.basis(self.org.domain), "email_domain")
        self.owner.email_verified_at = None
        self.owner.save(update_fields=["email_verified_at"])
        self.assertEqual(self.basis(self.org.domain), "org_website")

    def test_another_organisations_site_is_never_claimed(self):
        rival, _rival_owner, _rival_run = self.tenant("rival.example.test", "rival-run")
        HostedArticleSite.objects.create(organization=rival, domain="rival-blog.example.test", state="active")
        # The rival's own records never lend the claim run's organisation a basis.
        for domain in ("rival.example.test", "blog.rival.example.test", "rival-blog.example.test"):
            self.assertEqual(self.basis(domain), "none", domain)
        # Nor do the claim run's own records reach a site another organisation holds.
        WebsiteConnection.objects.create(organization=self.org, github_repo="example/copy", site_url="rival.example.test")
        WebsiteConnection.objects.create(organization=self.org, github_repo="example/copy", site_url="rival-blog.example.test")
        self.owner.email = "founder@rival.example.test"
        self.owner.save(update_fields=["email"])
        VibeRaisingCompany.objects.create(profile=self.owner.vibe_raising_profile, organization=self.org,
                                          domain="rival-blog.example.test", name="Copied brand")
        for domain in ("rival.example.test", "blog.rival.example.test", "rival-blog.example.test"):
            self.assertEqual(self.basis(domain), "none", domain)
        # Each run still claims its own site.
        self.assertEqual(self.basis("rival.example.test", run_id="rival-run"), "org_website")
        self.assertEqual(self.basis("rival-blog.example.test", run_id="rival-run"), "hosted_site")
        self.assertEqual(self.basis(self.org.domain, run_id="rival-run"), "none")
        self.assertEqual(self.basis(self.org.domain), "org_website")

    def test_a_run_without_an_organisation_resolves_it_from_the_runs_domain(self):
        run = ContentFactoryRun.objects.create(run_id="unscoped-run", domain="orphan.example.test", workflow="direct_generate",
                                               status="completed", result={}, run_request={})
        self.assertIsNone(run.organization_id)
        self.assertEqual(self.basis("orphan.example.test", run_id="unscoped-run"), "none")
        org = Organization.objects.create(domain="orphan.example.test", name="Synthetic orphan")
        HostedArticleSite.objects.create(organization=org, domain="orphan.example.test", state="draft")
        self.assertEqual(self.basis("orphan.example.test", run_id="unscoped-run"), "hosted_site")
        self.assertEqual(self.basis("orphan.example.test", run_id="missing-run"), "none")

    def test_public_suffixes_shared_hosts_and_institutions_are_never_claimed(self):
        HostedArticleSite.objects.create(organization=self.org, domain="github.io", state="active")
        HostedArticleSite.objects.create(organization=self.org, domain="com.au", state="active")
        HostedArticleSite.objects.create(organization=self.org, domain="unimelb.edu.au", state="active")
        for domain in ("github.io", "someone.github.io", "com.au", "anyone.com.au", "unimelb.edu.au"):
            self.assertEqual(self.basis(domain), "none", domain)

    def test_malformed_requests_are_refused_without_a_lookup(self):
        for run_id, domain in (("", self.org.domain), ("../claim-run", self.org.domain), ("x" * 101, self.org.domain),
                               ("claim-run", ""), ("claim-run", "localhost"), ("claim-run", "127.0.0.1"),
                               ("claim-run", "owner@pawshare.example.test"), ("claim-run", "pawshare.example.test:99999"),
                               ("claim-run", "ftp://pawshare.example.test"), ("claim-run", "pawshare_example.test")):
            with self.assertNumQueries(0):
                response = self.claim(domain, run_id)
            self.assertEqual((response.status_code, response.data["basis"], response.data["reason"]), (400, "none", "invalid_request"),
                             (run_id, domain))
        request = self.factory.get("/synthetic", HTTP_X_API_KEY=KEY)
        self.assertEqual(PreviewDomainClaimServiceView.as_view()(request).status_code, 400)
