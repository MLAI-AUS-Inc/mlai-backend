"""P1.14 (IF-P1-17): the vmv call sites of the owner-click hooks.

be-github-b owns ``content_factory.website_setup_owner_approval``. This
package only calls it, through ``importlib``: the owner's approve of a setup
run records the owner click before anything can merge, "manual" never merges
server-side and returns the pull request, and the automatic setup publish
waits for an "auto" receipt. A missing module, an organisation that needs no
owner click and every legacy organisation keep today's path exactly. The
hook module here is a stub injected through ``sys.modules``.
"""
import sys
import types
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from founder_tools.services import FounderCompanyContext
from workflow_runs.models import ContentFactoryRun

from . import vibe_marketing_views as views
from .founder_journey_cohort import is_new_journey, read_marker
from .founder_journey_testing import ALL_P1_FLAGS_ON, NAMED_LEGACY_ORGS, _connect, make_named_legacy_org, make_new_journey_org
from .website_views import SETUP_OWNER_APPROVAL_MODULE, setup_owner_approval_hooks

WITHOUT_V2 = {**ALL_P1_FLAGS_ON, "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED": False}
PR_URL_SUFFIX = "/pull/7"


class OwnerApprovalStub(types.ModuleType):
    """The IF-P1-17 hook surface, recording every call.

    ``requires`` None computes the contract's rule (new journey, or a sticky
    ``ownerClickRequired``); True or False forces it.
    """

    def __init__(self, *, requires=None, may_merge=None, events=None):
        super().__init__(SETUP_OWNER_APPROVAL_MODULE)
        self.requires, self.may_merge, self.events = requires, may_merge, events if events is not None else []
        self.approvals, self.merge_checks = [], []

    def requires_owner_click(self, organization):
        if self.requires is not None:
            return self.requires
        return is_new_journey(organization) or bool((read_marker(organization) or {}).get("ownerClickRequired"))

    def on_owner_setup_approve(self, *, organization, user, run, data):
        if not self.requires_owner_click(organization):
            return None
        self.events.append("owner_click")
        self.approvals.append({"organization": organization.pk, "user": user.pk, "run": run.run_id, "data": dict(data)})
        return {"merge": "manual"} if data.get("merge") == "manual" else {"merge": "auto"}

    def may_merge_setup(self, *, organization, run):
        self.merge_checks.append(run.run_id)
        if not self.requires_owner_click(organization):
            return None
        return self.may_merge


def deterministic_run(run, context=None, **kwargs):
    return {"runId": run.run_id, "status": run.status, "approvalState": run.approval_state,
            "currentStep": run.current_step}


class _SetupRunHarness:
    def make_setup_run(self, built):
        repo = built.connection.github_repo
        return ContentFactoryRun.objects.create(
            run_id=f"setup-{built.organization.pk}-{ContentFactoryRun.objects.count()}", workflow="article_system_setup",
            domain=built.organization.domain, organization=built.organization, github_repo=repo,
            status="awaiting_approval", current_step="await_review", approval_state="pending",
            result={"article_system_setup": {"status": "pr_created", "pr_number": 7,
                                             "pr_url": f"https://github.com/{repo}{PR_URL_SUFFIX}"}})

    def approve(self, built, run, *, data=None, user=None, stub=None, absent=False):
        """Approve the setup run; returns (response, CF payload or None, merge mock, events)."""
        events = stub.events if stub is not None else []
        repo = built.connection.github_repo

        def content_factory(**kwargs):
            events.append("content_factory")
            return {"status": "setup_pr_created", "pr_url": f"https://github.com/{repo}{PR_URL_SUFFIX}",
                    "pr_number": 7, "current_step": "create_pull_request"}

        modules = {SETUP_OWNER_APPROVAL_MODULE: None if absent or stub is None else stub}
        context = FounderCompanyContext(profile=built.company.profile, company=built.company,
                                        organization=built.organization)
        with patch.dict(sys.modules, modules), \
             patch.object(views, "_call_content_factory_run_action", side_effect=content_factory) as remote, \
             patch.object(views, "_attempt_setup_publish_merge") as merge, \
             patch.object(views, "_serialize_run", side_effect=deterministic_run):
            response = views.control_article_run(user=user or built.owner, context=context, run=run,
                                                 action="approve", data=data or {})
        payload = remote.call_args.kwargs["payload"] if remote.called else None
        return response, payload, merge, events

    def auto_publish(self, built, run, *, stub=None, absent=False):
        modules = {SETUP_OWNER_APPROVAL_MODULE: None if absent or stub is None else stub}
        context = FounderCompanyContext(profile=built.company.profile, company=built.company,
                                        organization=built.organization)
        with patch.dict(sys.modules, modules), \
             patch.object(views, "_org_config_enables_auto_publish", return_value=True), \
             patch.object(views, "_attempt_setup_publish_merge", return_value={"outcome": "merged"}) as merge:
            views._maybe_auto_publish_setup(run=run, context=context)
        return merge


@override_settings(**ALL_P1_FLAGS_ON)
class NewJourneyOwnerClickTests(_SetupRunHarness, TestCase):
    def setUp(self):
        self.built = make_new_journey_org()
        self.built.connection, self.built.target = _connect(self.built.organization, self.built.config,
                                                            repo="example-new/website")
        self.run = self.make_setup_run(self.built)

    def test_the_loader_returns_the_module_or_none_when_it_is_missing(self):
        stub = OwnerApprovalStub()
        with patch.dict(sys.modules, {SETUP_OWNER_APPROVAL_MODULE: stub}):
            self.assertIs(setup_owner_approval_hooks(), stub)
        with patch.dict(sys.modules, {SETUP_OWNER_APPROVAL_MODULE: None}):
            self.assertIsNone(setup_owner_approval_hooks())

    def test_auto_records_the_owner_click_before_the_approve_and_continues_todays_path(self):
        stub = OwnerApprovalStub()
        response, payload, merge, events = self.approve(self.built, self.run, data={"merge": "auto"}, stub=stub)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(events, ["owner_click", "content_factory"])
        self.assertEqual(stub.approvals, [{"organization": self.built.organization.pk, "user": self.built.owner.pk,
                                           "run": self.run.run_id, "data": {"merge": "auto"}}])
        self.assertNotIn("setupPullRequestUrl", response.data)
        self.assertNotIn("merge", response.data)
        self.assertEqual(payload["merge"], "auto")
        merge.assert_not_called()
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "completed")

    def test_manual_records_the_approval_never_merges_and_returns_the_pull_request(self):
        stub = OwnerApprovalStub()
        response, payload, merge, events = self.approve(self.built, self.run, data={"merge": "manual"}, stub=stub)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(events, ["owner_click", "content_factory"])
        # The merge key reaches the hook exactly as the client sent it.
        self.assertEqual(stub.approvals[0]["data"], {"merge": "manual"})
        self.assertEqual(response.data["merge"], "manual")
        self.assertEqual(response.data["setupPullRequestUrl"], f"https://github.com/example-new/website{PR_URL_SUFFIX}")
        merge.assert_not_called()

    def test_only_the_owner_can_make_the_owner_click(self):
        stub = OwnerApprovalStub()
        stranger = get_user_model().objects.create_user(email="not-the-owner@synthetic.example.test")
        response, payload, merge, events = self.approve(self.built, self.run, data={"merge": "auto"}, stub=stub,
                                                        user=stranger)
        self.assertEqual((response.status_code, response.data["reason"]), (403, "owner_required"))
        self.assertIsNone(payload)
        self.assertEqual((stub.approvals, events), ([], []))
        merge.assert_not_called()

    def test_auto_publish_waits_for_an_auto_receipt(self):
        for may_merge, merges in ((False, False), (None, True), (True, True)):
            with self.subTest(may_merge=may_merge):
                merge = self.auto_publish(self.built, self.run, stub=OwnerApprovalStub(may_merge=may_merge))
                self.assertEqual(merge.called, merges)
                if merges:
                    merge.assert_called_once()
                    self.assertIs(merge.call_args.kwargs["automatic"], True)

    def test_the_owner_pressing_merge_setup_pr_still_merges(self):
        stub = OwnerApprovalStub(may_merge=False)
        context = FounderCompanyContext(profile=self.built.company.profile, company=self.built.company,
                                        organization=self.built.organization)
        with patch.dict(sys.modules, {SETUP_OWNER_APPROVAL_MODULE: stub}), \
             patch.object(views, "_attempt_setup_publish_merge",
                          return_value={"outcome": "merged", "run": self.run}) as merge, \
             patch.object(views, "_serialize_run", side_effect=deterministic_run):
            response = views.control_article_run(user=self.built.owner, context=context, run=self.run,
                                                 action="merge-setup-pr", data={})
        self.assertEqual(response.status_code, 200, response.data)
        merge.assert_called_once()
        self.assertEqual(stub.approvals, [])

    def test_a_missing_module_or_no_owner_click_is_todays_path(self):
        today, today_payload, _merge, _events = self.approve(self.built, self.make_setup_run(self.built),
                                                             data={"merge": "manual"}, absent=True)
        self.assertEqual(today.status_code, 200)
        self.assertNotIn("setupPullRequestUrl", today.data)
        stub = OwnerApprovalStub(requires=False)
        run = self.make_setup_run(self.built)
        response, payload, merge, events = self.approve(self.built, run, data={"merge": "manual"}, stub=stub)
        self.assertEqual(response.data, {**today.data, "runId": run.run_id})
        self.assertEqual(payload, today_payload)
        self.assertEqual((stub.approvals, events), ([], ["content_factory"]))
        merge.assert_not_called()
        self.assertTrue(self.auto_publish(self.built, self.run, absent=True).called)


class LegacyOwnerClickTests(_SetupRunHarness, TestCase):
    """G1 and G10 for this package: the legacy cohort's approve and auto-publish are unchanged."""

    def results(self, built, **hooks):
        run = self.make_setup_run(built)
        response, payload, merge, events = self.approve(built, run, data={"merge": "manual"}, **hooks)
        auto = self.auto_publish(built, self.make_setup_run(built), **hooks)
        return {"status": response.status_code, "body": {**response.data, "runId": None},
                "payload": payload, "merged_on_approve": merge.called, "auto_publish_merged": auto.called}

    def test_every_named_legacy_org_keeps_todays_approve_and_auto_publish(self):
        for name in NAMED_LEGACY_ORGS:
            built = make_named_legacy_org(name)
            today = self.results(built, absent=True)
            self.assertEqual((today["status"], today["auto_publish_merged"]), (200, True), name)
            for label, flags in (("all P1 flags on", ALL_P1_FLAGS_ON), ("minus V2", WITHOUT_V2)):
                for hooks in ({"absent": True}, {"stub": OwnerApprovalStub(may_merge=False)}):
                    with self.subTest(name=name, flags=label, hooks=sorted(hooks)), override_settings(**flags):
                        self.assertEqual(self.results(built, **hooks), today)
                        if "stub" in hooks:
                            self.assertEqual(hooks["stub"].approvals, [])

    def test_a_sticky_owner_click_survives_v2_off(self):
        built = make_new_journey_org()
        built.connection, built.target = _connect(built.organization, built.config, repo="example-sticky/website")
        with override_settings(**ALL_P1_FLAGS_ON):
            from .founder_journey_cohort import update_marker
            update_marker(built.organization, owner_click_required=True)
        with override_settings(**WITHOUT_V2):
            stub = OwnerApprovalStub(may_merge=False)
            self.assertFalse(self.auto_publish(built, self.make_setup_run(built), stub=stub).called)
            response, _payload, merge, _events = self.approve(built, self.make_setup_run(built),
                                                              data={"merge": "manual"}, stub=stub)
            self.assertEqual(response.data["merge"], "manual")
            merge.assert_not_called()
