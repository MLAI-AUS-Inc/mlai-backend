"""B20: pause then resume keeps everything; revoke then reconnect restores proof only after a fresh head read (D17)."""
from unittest.mock import patch

from django.test import TestCase, override_settings

from workflow_runs.models import ContentFactoryRun

from . import website_source_carry as carry
from . import website_source_sync as source_sync
from .system_stopped_runs import FOLLOWUP_KEY, follow_up, stop_cause
from .tests_live_url_verification import MERGED
from .tests_website_source_carry import CarriedSite, _compare
from .website_connections import authority_guard, bind_website, contract_for, summary_for, transition_connection
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnectionTarget

MOVED = "a" * 40


FLAGS = dict(ARTICLES_READY_ON_MERGE_ENABLED=True, ARTICLES_PROOF_CARRY_FORWARD_ENABLED=True)


class LifecycleSite(CarriedSite):
    def setUp(self):
        super().setUp()
        self.owner = self.built.owner
        self.connection.authorized_by = self.owner
        self.connection.save(update_fields=["authorized_by"])
        self.config.publish_targets = [{"target_id": "articles", "verification": {"source_sha": MERGED}}]
        self.config.save(update_fields=["publish_targets"])
        self.run = ContentFactoryRun.objects.create(run_id="in-flight", workflow="confirmed_topic",
            domain=self.built.organization.domain, organization=self.built.organization,
            github_repo=self.connection.github_repo, run_request=contract_for(self.connection), status="running",
            result={"topic": "Synthetic topic"})

    def metadata(self, **overrides):
        return {"repository_id": 777, "github_repo": "banksia/website", "installation_id": "900", "branch": "main", **overrides}

    def reconnect(self, *, head=MERGED, user=None, **metadata):
        with patch("content_factory.website_connections.verify_repository_access", return_value=self.metadata(**metadata)), \
                patch.object(source_sync, "read_head", return_value=head) as read:
            bind_website(self.config, user=user or self.owner, repo="banksia/website", branch="main",
                site_url=self.connection.site_url, reconnect=True, expected=contract_for(self.connection),
                repository_id=777)
        self.connection.refresh_from_db()
        self.config.refresh_from_db()
        self.run.refresh_from_db()
        return read

    def current_target(self):
        return WebsiteConnectionTarget.objects.filter(connection=self.connection, generation=self.connection.generation,
            target_key="articles").first()


@override_settings(**FLAGS)
class PauseResumeTests(LifecycleSite, TestCase):
    def test_pause_then_resume_keeps_generation_proof_targets_and_runs(self):
        generation = self.connection.generation
        transition_connection(self.config, action="pause", expected=contract_for(self.connection), idempotency_key="pause-1")
        self.connection.refresh_from_db(); self.config.refresh_from_db(); self.run.refresh_from_db()
        self.assertEqual((self.connection.state, self.connection.generation, self.connection.verified_sha),
            ("paused", generation, MERGED))
        self.assertEqual(self.run.status, "running")
        self.assertEqual(self.config.default_publish_target_id, "articles")
        self.assertTrue(self.config.publish_targets)
        self.assertFalse(summary_for(self.config)["capabilities"]["publishingReady"])
        with self.assertRaises(WebsiteAuthorityError) as refused:
            with authority_guard(contract_for(self.connection), action="publish"):
                self.fail("a paused website accepted a write")
        self.assertEqual(refused.exception.code, "website_publishing_paused")

        read = self.reconnect()
        read.assert_not_called()  # Pushes were tracked while paused: no restore step is needed.
        self.assertEqual((self.connection.state, self.connection.generation, self.connection.verified_sha),
            ("connected", generation, MERGED))
        self.assertEqual(self.run.status, "running")
        self.assertIsNotNone(self.current_target())
        self.assertTrue(summary_for(self.config)["capabilities"]["publishingReady"])

    def test_a_different_installation_resumes_as_a_fresh_connection(self):
        transition_connection(self.config, action="pause", expected=contract_for(self.connection), idempotency_key="pause-1")
        self.connection.refresh_from_db()
        generation = self.connection.generation
        self.reconnect(installation_id="901")
        self.assertEqual(self.connection.generation, generation + 1)
        self.assertEqual(self.run.status, "cancelled")


@override_settings(**FLAGS)
class RevokeReconnectTests(LifecycleSite, TestCase):
    def revoke(self):
        transition_connection(self.config, action="revoke", expected=contract_for(self.connection), idempotency_key=f"revoke-{self.connection.generation}")
        self.connection.refresh_from_db(); self.config.refresh_from_db(); self.run.refresh_from_db()
        self.assertEqual(self.run.status, "cancelled")
        self.assertEqual(self.config.publish_targets, [])

    def test_reconnect_restores_proof_after_a_fresh_head_read_and_revives_no_run(self):
        self.revoke()
        read = self.reconnect(head=MERGED)
        read.assert_called_once()
        self.assertEqual(self.connection.state, "connected")
        self.assertEqual(self.connection.verified_sha, MERGED)
        target = self.current_target()
        self.assertIsNotNone(target)
        self.assertEqual(target.source_sha, MERGED)
        self.assertFalse([item for item in self.connection.blockers if item.get("code") == "scan_required"])
        self.assertEqual(self.config.default_publish_target_id, "articles")
        self.assertEqual(self.config.publish_targets[0]["target_id"], "articles")
        self.assertEqual(self.config.article_system_setup_cache, self.config.article_system_setup_cache or None)
        self.assertTrue(summary_for(self.config)["capabilities"]["publishingReady"])
        # The run the revoke stopped is never revived; the founder is offered "start again".
        self.assertEqual(self.run.status, "cancelled")
        self.assertEqual(stop_cause(self.run)["transition"], "revoke")
        self.assertEqual(follow_up(self.run), "notice")
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "cancelled")
        self.assertEqual(self.run.result[FOLLOWUP_KEY]["decision"], "notice")

    def test_a_head_that_moved_with_setup_files_untouched_is_carried(self):
        self.revoke()
        with patch.object(carry, "compare_sources", return_value=_compare("src/app/page.tsx")):
            self.reconnect(head=MOVED)
        self.assertEqual(self.connection.verified_sha, MOVED)
        self.assertEqual(self.current_target().capabilities["proofCarriedFrom"], MERGED)

    def test_a_head_that_changed_the_setup_files_starts_from_a_fresh_scan(self):
        self.revoke()
        with patch.object(carry, "compare_sources", return_value=_compare("src/app/blog/page.tsx")):
            self.reconnect(head=MOVED)
        self.assertEqual(self.connection.verified_sha, "")
        self.assertIsNone(self.current_target())
        self.assertTrue([item for item in self.connection.blockers if item.get("code") == "scan_required"])

    def test_an_unreadable_head_restores_nothing(self):
        self.revoke()
        self.reconnect(head="")
        self.assertEqual(self.connection.verified_sha, "")
        self.assertIsNone(self.current_target())

    def test_a_different_installation_or_owner_restores_nothing(self):
        from django.contrib.auth import get_user_model
        other = get_user_model().objects.create_user(email="other-founder@example.test", password="synthetic-password")
        for label, kwargs in (("installation", {"installation_id": "901"}), ("owner", {"user": other})):
            with self.subTest(label):
                self.revoke()
                self.reconnect(**kwargs)
                self.assertEqual(self.connection.verified_sha, "")
                self.assertIsNone(self.current_target())
                # Back to a Ready baseline for the next case.
                self.connection.state = "connected"
                self.connection.authorized_by = self.owner
                self.connection.installation_id = "900"
                self.connection.verified_sha = MERGED
                self.connection.capabilities = {**self.connection.capabilities, "publishingReady": True}
                self.connection.save()
                WebsiteConnectionTarget.objects.create(connection=self.connection, generation=self.connection.generation,
                    target_key="articles", adapter=self.target.adapter, source_sha=MERGED, contract=self.target.contract,
                    capabilities={"publishingReady": True}, verified_at=self.target.verified_at)
                self.config.default_publish_target_id = "articles"
                self.config.save(update_fields=["default_publish_target_id"])
                ContentFactoryRun.objects.filter(pk=self.run.pk).update(status="running",
                    run_request=contract_for(self.connection))
