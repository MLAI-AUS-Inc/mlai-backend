"""Exercise the free probe outbox, service authentication and current-source settlement."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from organizations.models import Organization
from . import repo_health, repo_health_operations as probes
from .models import OrganizationContentConfig
from .website_models import WebsiteConnection, WebsiteConnectionOperation


@override_settings(ARTICLES_REPO_HEALTH_ENABLED=True, ROO_API_KEY="fixture-service",
                   CONTENT_FACTORY_API_KEY="fixture-worker", CONTENT_FACTORY_URL="http://worker.test")
class RepoHealthLifecycleTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Fixture", domain="health.example.test")
        self.site = WebsiteConnection.objects.create(organization=self.org, repository_id=42, github_repo="fixture/site",
                                                    installation_id="71", branch="main", app_root="web")
        OrganizationContentConfig.objects.create(organization=self.org, website_connection=self.site)
        self.sha = "a" * 40
        self.head = self.enterContext(patch.object(probes, "read_head", return_value=self.sha))
        self.dispatch = self.enterContext(patch("integrations.http_client.post", return_value=SimpleNamespace(status_code=202)))
        self.host_probe = self.enterContext(patch.object(repo_health, "refresh_host"))
        self.now = timezone.now()
        self.client = APIClient()

    def call(self, action, payload, *, authorized=True):
        headers = {"HTTP_X_API_KEY": "fixture-service"} if authorized else {}
        return self.client.post(f"/api/content-factory/repo-health/{action}", payload, format="json", **headers)

    def dispatched(self):
        # A pre-existing website can recover even if its connect event predates rollout.
        probes.sweep(self.now)
        probes.sweep(self.now + timedelta(seconds=1))
        self.assertEqual(self.dispatch.call_count, 1)
        return self.dispatch.call_args.kwargs["json"]

    def result(self, wire, *, sha=None):
        return {**wire, "result": {"schema": repo_health.BUILD_SCHEMA, "status": "passed", "code": None,
                                  "blocking": False, "commit_sha": sha or self.sha, "log_tail": ["private build output"]}}

    def test_retries_keep_identity_and_only_complete_the_exact_current_website(self):
        wire = self.dispatched()
        self.assertNotIn(self.call("authorize", wire, authorized=False).status_code, {200, 202})
        self.assertEqual(self.call("authorize", wire).data, {"authorized": True})
        probes.sweep(self.now + timedelta(hours=1))
        self.assertEqual(wire, self.dispatch.call_args.kwargs["json"])
        for key, other in {"organization_id": self.org.pk+1, "connection_generation": True, "repository_id": 99,
                           "installation_id": "99", "branch": "other", "app_root": "", "dispatch_nonce": "other"}.items():
            with self.subTest(key=key):
                self.assertEqual(self.call("result", self.result({**wire, key: other})).status_code, 409)
        self.assertEqual(self.call("result", self.result(wire, sha="b"*40)).status_code, 400)
        self.assertEqual(self.call("result", self.result(wire)).status_code, 200)
        self.assertEqual(self.call("result", self.result(wire)).status_code, 200)
        self.site.refresh_from_db()
        self.assertEqual(repo_health.stored(self.site)["build"]["log_tail"], [])
        repo_health.require_green_build(self.site)
        self.assertEqual(self.call("authorize", wire).status_code, 409)
        self.assertEqual(WebsiteConnectionOperation.objects.filter(action=probes.ACTION).count(), 2)

    def test_force_push_back_to_a_displaced_result_creates_one_new_probe(self):
        first = self.dispatched()
        self.assertEqual(self.call("result", self.result(first)).status_code, 200)
        second_head = "b" * 40
        self.head.return_value = second_head
        probes.queue(self.site, second_head)
        probes.sweep(self.now + timedelta(minutes=1))
        second = self.dispatch.call_args.kwargs["json"]
        self.assertEqual(self.call("result", self.result(second, sha=second_head)).status_code, 200)
        self.head.return_value = self.sha
        probes.queue(self.site, self.sha)
        probes.queue(self.site, self.sha)
        probes.sweep(self.now + timedelta(minutes=2))
        third = self.dispatch.call_args.kwargs["json"]
        self.assertEqual(self.dispatch.call_count, 3)
        self.assertNotEqual(first["probe_id"], third["probe_id"])
        self.assertEqual(self.call("result", self.result(first)).status_code, 409)
        self.assertEqual(self.call("result", self.result(third)).status_code, 200)

    def test_static_setup_requires_the_selected_sealed_recipe_and_verified_directory_membership(self):
        from .website_content_files import static_candidate, verified_static_target
        self.site.verified_sha = self.sha
        self.site.save(update_fields=("verified_sha",))
        key, digest = "static-directory-aef263185251c0d5", "c" * 64
        recipe = {"kind": "static_dropin_v1", "route_base": "articles", "static_root": "public",
                  "style": {"artifact_digest": "e" * 64, "route_base": "articles"}, "sitemap_source": ""}
        candidate = {"target_id": key, "delivery_adapter": "static_dropin_v1", "contract_digest": digest,
                     "registration_strategy": {"recipe": recipe}}
        self.assertEqual(static_candidate({"setup_ladder": {"tier": "static_dropin",
            "recipe": recipe, "candidate_publish_target": candidate}}), candidate)
        self.site.targets.create(generation=self.site.generation, source_sha=self.sha, target_key=key,
                                 verified_at=self.now, adapter="static_dropin_v1", contract=candidate)
        OrganizationContentConfig.objects.filter(website_connection=self.site).update(default_publish_target_id=key)
        self.assertFalse(verified_static_target(self.site, candidate))
        op = self.site.operations.create(generation=self.site.generation, action="deployment-verify",
            state="completed", idempotency_key="test-static-directory", receipt={"target_id": key, "source_sha": self.sha,
                "contract_digest": digest, "provider_verified": True})
        self.assertFalse(verified_static_target(self.site, candidate))
        op.receipt["static_directory_link_verified"] = True
        op.save(update_fields=("receipt",))
        self.assertTrue(verified_static_target(self.site, candidate))
        self.assertFalse(verified_static_target(self.site, {**candidate, "contract_digest": "0" * 64}))
        self.assertFalse(verified_static_target(self.site, {**candidate, "registration_strategy": {"recipe": {
            **recipe, "route_base": "changed"}}}))
        self.site.verified_sha = "c" * 40
        self.assertFalse(verified_static_target(self.site, candidate))

    def test_push_racing_result_verification_cannot_restore_an_older_head(self):
        wire = self.dispatched()
        def advance_while_reading(site):
            repo_health.record_head(site, "b" * 40)
            return self.sha
        self.head.side_effect = advance_while_reading
        self.assertEqual(self.call("result", self.result(wire)).status_code, 409)
        self.site.refresh_from_db()
        self.assertEqual(repo_health.current_head(self.site), "b" * 40)
        self.assertNotIn("build", repo_health.stored(self.site))

    def test_change_during_build_never_marks_new_head_green(self):
        wire = self.dispatched()
        self.head.return_value = "b" * 40
        self.assertEqual(self.call("result", self.result(wire)).status_code, 409)
        self.head.return_value = ""
        self.assertEqual(self.call("authorize", wire).status_code, 503)
        WebsiteConnection.objects.filter(pk=self.site.pk).update(state="revoked", generation=2)
        self.head.return_value = self.sha
        self.assertEqual(self.call("result", self.result(wire)).status_code, 409)
        self.site.refresh_from_db()
        self.assertNotIn("build", repo_health.stored(self.site))

    def test_callback_deadline_preserves_one_operation_for_staff_and_accepts_late_result(self):
        wire = self.dispatched()
        with patch("content_factory.founder_journey_alerts.queue_founder_journey_alert") as alert:
            probes.sweep(self.now + timedelta(hours=7))
            probes.sweep(self.now + timedelta(hours=8))
        alert.assert_called_once()
        self.assertEqual(self.dispatch.call_count, 1)
        self.assertEqual(self.call("authorize", wire).status_code, 409)
        self.assertEqual(self.call("result", self.result(wire)).status_code, 200)

    @override_settings(ARTICLES_REPO_HEALTH_SHADOW_ENABLED=True)
    def test_shadow_counts_eligible_sites_but_never_queues_or_dispatches(self):
        probes.queue(self.site)
        self.assertEqual(probes.sweep(self.now), {"eligible": 1, "due": 0, "processed": 0})
        self.assertFalse(WebsiteConnectionOperation.objects.exists())
        self.dispatch.assert_not_called()

    @override_settings(ARTICLES_REPO_HEALTH_ENABLED=False)
    def test_switch_off_never_dispatches_or_accepts(self):
        probes.queue(self.site, self.sha)
        self.assertEqual(probes.sweep(self.now), {"processed": 0})
        self.assertEqual(self.call("authorize", {}).status_code, 404)
        self.head.assert_not_called()
        self.dispatch.assert_not_called()
