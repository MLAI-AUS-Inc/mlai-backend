"""A run's preview copies a live site's design only for a domain its own organisation controls.

Database-backed, so these run in CI only; tests_preview_domain_claim_unit covers
the same rules without a database.
"""
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import resolve
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from community_chat.email_codes import email_digest
from community_chat.models import CommunityChatEmailCodeChallenge
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun

from .hosted_article_models import HostedArticleSite
from .preview_domain_claim import PreviewDomainClaimServiceView
from .website_models import WebsiteConnection, WebsiteConnectionOperation

KEY = "synthetic-test-key"


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

    def deployed(self, connection, domain, **receipt):
        # The receipt verify_live_deployment writes after finding the CI artifact marker on the live site.
        return WebsiteConnectionOperation.objects.create(
            connection=connection, generation=connection.generation, action="deployment-verify", state="completed",
            idempotency_key=f"{connection.pk}:deployment:{uuid.uuid4()}",
            receipt={"status": "passed", "provider_verified": True, "public_url": f"https://{domain}/articles", **receipt})

    def prove_mailbox(self, user, email, *, consumed=True):
        user.email = email
        user.save(update_fields=["email"])
        CommunityChatEmailCodeChallenge.objects.create(
            user=user, email_digest=email_digest(email), code_digest="b" * 64, client_id="mlai-chat-web",
            installation_id=uuid.uuid4(), origin="https://chat.example.test", platform="web", public_key="c" * 64,
            expires_at=timezone.now() + timedelta(minutes=10), consumed_at=timezone.now() if consumed else None)

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

    def test_a_connection_needs_a_passed_live_deployment_receipt_for_the_domain(self):
        connection = WebsiteConnection.objects.create(organization=self.org, github_repo="example/brand",
                                                      site_url="https://www.brand.example.test:443/")
        # A typed site_url and a bound repository prove nothing about the live site.
        self.assertEqual(self.basis("brand.example.test"), "none")
        self.deployed(connection, "another-brand.example.test")
        self.deployed(connection, "brand.example.test", status="failed")
        self.deployed(connection, "brand.example.test", provider_verified=False)
        self.assertEqual(self.basis("brand.example.test"), "none")
        self.assertEqual(self.basis("another-brand.example.test"), "website_connection")
        self.deployed(connection, "brand.example.test", checked_at="2024-01-01T00:00:00+00:00")
        self.assertEqual(self.basis("brand.example.test"), "website_connection")
        connection.state = "paused"
        connection.save(update_fields=["state"])
        self.assertEqual(self.basis("brand.example.test"), "website_connection")
        for state in ("revoked", "disconnected"):
            connection.state = state
            connection.save(update_fields=["state"])
            self.assertEqual(self.basis("brand.example.test"), "none", state)

    def test_the_run_owners_proven_work_email_is_a_basis(self):
        self.prove_mailbox(self.owner, "founder@brand.example.test", consumed=False)
        self.assertEqual(self.basis("brand.example.test"), "none")
        self.prove_mailbox(self.owner, "founder@brand.example.test")
        self.assertEqual(self.basis("brand.example.test"), "email_domain")
        self.assertEqual(self.basis("www.brand.example.test"), "email_domain")
        self.owner.email_verified_at = None
        self.owner.save(update_fields=["email_verified_at"])
        self.assertEqual(self.basis("brand.example.test"), "none")

    def test_a_verified_stamp_does_not_follow_an_email_change(self):
        self.prove_mailbox(self.owner, self.owner.email)
        self.owner.email = "ceo@brand.example.test"
        self.owner.save(update_fields=["email"])
        self.assertEqual(self.basis("brand.example.test"), "none")

    def test_an_email_only_counts_for_the_runs_requester_while_they_belong_to_its_organisation(self):
        self.prove_mailbox(self.owner, "founder@brand.example.test")
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
        self.prove_mailbox(self.owner, "founder@gmail.com")
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
        self.prove_mailbox(self.owner, f"founder@{self.org.domain}")
        site = HostedArticleSite.objects.create(organization=self.org, domain=self.org.domain, state="active")
        connection = WebsiteConnection.objects.create(organization=self.org, github_repo="example/site", site_url=self.org.domain)
        self.deployed(connection, self.org.domain)
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
        copy = WebsiteConnection.objects.create(organization=self.org, github_repo="example/copy", site_url="rival.example.test")
        self.deployed(copy, "rival.example.test")
        self.deployed(copy, "rival-blog.example.test")
        self.prove_mailbox(self.owner, "founder@rival.example.test")
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
