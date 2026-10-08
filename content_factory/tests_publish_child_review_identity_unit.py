"""Replay publish-child callbacks without a database or external services."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase

from .incident_guards import publish_child_binding
from .website_contract import WebsiteAuthorityError


class PublishChildReviewIdentityTests(SimpleTestCase):
    def setUp(self):
        self.slug = "reviewed-fixture-article"
        target = SimpleNamespace(
            target_key="featured", capabilities={"publishingReady": True},
            contract={"route_template": "/articles/featured/{slug}"},
        )
        connection = SimpleNamespace(
            pk=uuid4(), generation=3, github_repo="fixture/site", repository_id=42,
            verified_sha="a" * 40, targets=Mock(),
        )
        connection.targets.filter.return_value.first.return_value = target
        self.config = SimpleNamespace(website_connection=connection, default_publish_target_id="featured")
        request = {"website_connection_id": str(connection.pk), "connection_generation": 3,
                   "expected_source_sha": connection.verified_sha, "operation_id": "original-operation",
                   "operation_attempt": 8, "deletion_epoch": 0}
        self.source = SimpleNamespace(
            run_id="review-source", organization_id=7, github_repo=connection.github_repo,
            run_request=request, result={"delivery_package": {"slug": self.slug}}, acceptance_summary={},
        )
        self.child = SimpleNamespace(
            run_id="publish-child", organization_id=7, github_repo=connection.github_repo,
            run_request={**request, "source_run_id": self.source.run_id, "delivery_mode": "publish_code"},
            result={}, acceptance_summary={"evidence_summary": {"content_package_slug": None}},
        )

    def test_empty_child_uses_reviewed_parent_slug_without_mutating_saved_requests(self):
        before = deepcopy((self.source.run_request, self.child.run_request, self.child.result))
        binding = publish_child_binding(self.config, self.child, {}, {}, reviewed_source=self.source)
        self.assertEqual(binding["article_slug"], self.slug)
        self.assertEqual(binding["route_path"], f"/articles/featured/{self.slug}")
        self.assertEqual(before, (self.source.run_request, self.child.run_request, self.child.result))
        self.assertNotIn("repository_id", self.child.run_request)
        self.assertNotIn("connection_target_id", self.child.run_request)

    def test_unrelated_or_changed_scope_cannot_borrow_a_parent_slug(self):
        changes = [
            {"source_run_id": "another-source"}, {"operation_id": "replacement-operation"},
            {"operation_attempt": 9}, {"deletion_epoch": 1},
            {"connection_target_id": "replacement-target"}, {"repository_id": 42},
        ]
        for change in changes:
            with self.subTest(change=change):
                child = SimpleNamespace(**{**vars(self.child), "run_request": {**self.child.run_request, **change}})
                with self.assertRaises(WebsiteAuthorityError):
                    publish_child_binding(self.config, child, {}, {}, reviewed_source=self.source)
        self.child.organization_id = 8
        with self.assertRaises(WebsiteAuthorityError):
            publish_child_binding(self.config, self.child, {}, {}, reviewed_source=self.source)

    def test_child_and_remote_slug_or_route_mismatch_still_fail_closed(self):
        for result, remote in [
            ({"slug": "another-article"}, {}), ({}, {"slug": "another-article"}),
            ({}, {"route_path": "/articles/featured/another-article"}),
        ]:
            with self.subTest(result=result, remote=remote):
                self.child.result = result
                with self.assertRaises(WebsiteAuthorityError) as error:
                    publish_child_binding(self.config, self.child, {}, remote, reviewed_source=self.source)
                self.assertEqual(error.exception.code, "capture_target_mismatch")

    def test_parent_without_slug_or_with_stale_source_is_not_authority(self):
        for result, request in [({}, self.source.run_request),
                                (self.source.result, {**self.source.run_request, "expected_source_sha": "b" * 40})]:
            with self.subTest(result=result, request=request):
                source = SimpleNamespace(**{**vars(self.source), "result": result, "run_request": request})
                with self.assertRaises(WebsiteAuthorityError):
                    publish_child_binding(self.config, self.child, {}, {}, reviewed_source=source)

    def test_independent_draft_still_requires_its_own_saved_slug(self):
        with self.assertRaises(WebsiteAuthorityError) as error:
            publish_child_binding(self.config, self.child, {}, {})
        self.assertEqual(error.exception.code, "capture_target_mismatch")

    def test_existing_child_acknowledgement_validates_against_its_reviewed_source(self):
        from . import vibe_marketing_views as views
        context = SimpleNamespace(organization=SimpleNamespace(domain="fixture.test"))
        with patch.object(views, "_get_config", return_value=self.config), \
                patch.object(views.ContentFactoryRun.objects, "filter") as runs, \
                patch.object(views, "_run_belongs_to_context", return_value=True), \
                patch.object(views, "_create_local_run") as create:
            runs.return_value.prefetch_related.return_value.first.return_value = self.child
            acknowledged = views._ensure_local_publish_child_from_known_id(
                child_run_id=self.child.run_id, source_run=self.source,
                request=SimpleNamespace(user=object()), context=context,
                remote_data={"status": "queued"},
            )
        self.assertIs(acknowledged, self.child)
        create.assert_not_called()
