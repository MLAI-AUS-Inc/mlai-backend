"""Workstream 3: repo health at connect (no database, no GitHub, no network).

Done when: a stale lockfile, Next.js 16.4.0 blocked by an advisory and a host
with no deployments each produce the right code and founder message, and no
article is charged.
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch
import json

from django.test import SimpleTestCase, override_settings

from . import repo_health
from .founder_journey_contract import FounderContractMismatch, _schema, _validate
from .website_contract import WebsiteAuthorityError

NOW = datetime(2026, 11, 21, 9, 0, tzinfo=dt_timezone.utc)
HEAD = "a" * 40
OLD = "b" * 40
ON = override_settings(ARTICLES_REPO_HEALTH_ENABLED=True)


def connection(record=None, *, verified_sha=HEAD, generation=3):
    capabilities = {"publishingReady": True}
    if record is not None:
        capabilities[repo_health.KEY] = {"version": repo_health.VERSION, "generation": generation, **record}
    return SimpleNamespace(pk="c1", generation=generation, capabilities=capabilities, verified_sha=verified_sha,
                           branch="main", site_url="https://lavie.example", installation_id="9", github_repo="o/r")


def build(code=None, sha=HEAD):
    failed = code is not None
    return {"schema": repo_health.BUILD_SCHEMA, "status": "failed" if failed else "passed", "code": code,
            "blocking": failed, "commit_sha": sha, "log_tail": ["npm ERR! Missing: zod from lock file"] if failed else []}


class FakeReader:
    def __init__(self, commits=(), deployments=None, statuses=None, check_runs=None, unavailable=False):
        self._commits = list(commits)
        self._deployments = deployments or {}
        self._statuses = statuses or {}
        self._check_runs = check_runs or {}
        self.unavailable = unavailable

    def commits(self):
        if self.unavailable:
            raise repo_health.HostProbeUnavailable()
        return self._commits

    def deployments(self, sha):
        return self._deployments.get(sha, [])

    def statuses(self, sha):
        return self._statuses.get(sha, [])

    def check_runs(self, sha):
        return self._check_runs.get(sha, [])


def commits(*ages_days):
    return [{"sha": f"{index:x}" * 40 if index else HEAD, "date": (NOW - timedelta(days=age)).isoformat()}
            for index, age in enumerate(ages_days)]


class HostProbeTests(SimpleTestCase):
    def test_recent_commits_without_any_host_signal_are_not_deploying(self):
        result = repo_health.host_probe(FakeReader(commits(1, 3, 20)), checks_granted=True, now=NOW)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["codes"], [repo_health.HOST_NOT_DEPLOYING])
        self.assertEqual(result["commit_sha"], HEAD)

    def test_a_recent_deployment_status_or_host_check_counts(self):
        recent = (NOW - timedelta(days=2)).isoformat()
        for reader in (
            FakeReader(commits(1), deployments={HEAD: [{"created_at": recent}]}),
            FakeReader(commits(1), statuses={HEAD: [{"context": "Vercel", "updated_at": recent}]}),
            FakeReader(commits(1), check_runs={HEAD: [{"app": {"slug": "cloudflare-workers-and-pages"}, "completed_at": recent}]}),
        ):
            self.assertEqual(repo_health.host_probe(reader, checks_granted=True, now=NOW)["codes"], [])

    def test_ci_statuses_and_old_deployments_do_not_count(self):
        old = (NOW - timedelta(days=30)).isoformat()
        reader = FakeReader(commits(1), deployments={HEAD: [{"created_at": old}]},
                            statuses={HEAD: [{"context": "ci/lint", "updated_at": NOW.isoformat()}]})
        self.assertEqual(repo_health.host_probe(reader, checks_granted=True, now=NOW)["codes"], [repo_health.HOST_NOT_DEPLOYING])

    def test_a_quiet_repo_is_not_flagged(self):
        self.assertEqual(repo_health.host_probe(FakeReader(commits(40, 60)), checks_granted=True, now=NOW)["status"], "passed")

    def test_missing_checks_grant_warns_and_outage_is_unknown(self):
        recent = (NOW - timedelta(days=1)).isoformat()
        result = repo_health.host_probe(FakeReader(commits(1), deployments={HEAD: [{"created_at": recent}]}),
                                        checks_granted=False, now=NOW)
        self.assertEqual(result["codes"], [repo_health.CHECKS_PERMISSION_MISSING])
        self.assertEqual(result["status"], "passed")
        down = repo_health.host_probe(FakeReader(unavailable=True), checks_granted=True, now=NOW)
        self.assertEqual((down["status"], down["codes"]), ("unknown", []))


class SitemapProbeTests(SimpleTestCase):
    def test_sitemap_reachable_unreachable_and_not_xml(self):
        calls = []
        def ok(url, domain):
            calls.append((url, domain))
            return b'<?xml version="1.0"?><urlset></urlset>', {}
        self.assertEqual(repo_health.sitemap_probe("https://lavie.example/", fetch=ok, now=NOW)["status"], "passed")
        self.assertEqual(calls, [("https://lavie.example/sitemap.xml", "lavie.example")])
        def down(url, domain):
            raise WebsiteAuthorityError("live_route_unavailable", "x")
        self.assertEqual(repo_health.sitemap_probe("lavie.example", fetch=down, now=NOW)["status"], "failed")
        self.assertEqual(repo_health.sitemap_probe("lavie.example", fetch=lambda u, d: (b"<html>", {}), now=NOW)["status"], "failed")


class ProjectionTests(SimpleTestCase):
    def test_off_by_default_nothing_is_projected_or_gated(self):
        site = connection({"build": build(repo_health.LOCKFILE_OUT_OF_SYNC)})
        self.assertFalse(repo_health.enabled())
        self.assertIsNone(repo_health.journey_block(site))
        self.assertFalse(repo_health.setup_blocked(site))
        repo_health.require_green_build(site)  # no raise
        self.assertIsNone(repo_health.record_head(site, HEAD))
        self.assertEqual(repo_health.on_push({"repository": {"id": 1}, "after": HEAD}), {})

    @ON
    def test_stale_lockfile_blocks_with_its_plain_message_and_no_charge(self):
        site = connection({"build": build(repo_health.LOCKFILE_OUT_OF_SYNC)})
        block = repo_health.journey_block(site)
        self.assertTrue(block["blocking"])
        self.assertEqual(block["buildStatus"], "failed")
        self.assertEqual(block["items"], [{"code": "lockfile_out_of_sync", "severity": "blocking",
            "message": "Your lockfile is out of date, so a clean install of your site fails.",
            "action": "Run npm install and commit package-lock.json."}])
        self.assertTrue(repo_health.setup_blocked(site))
        with self.assertRaises(WebsiteAuthorityError) as raised:
            repo_health.require_green_build(site)
        self.assertEqual(raised.exception.code, "repo_health_build_not_green")
        self.assertFalse(raised.exception.retryable)

    @ON
    def test_next_16_4_0_advisory_blocks(self):
        site = connection({"build": build(repo_health.FRAMEWORK_BUILD_BLOCKED_BY_ADVISORY)})
        [item] = repo_health.journey_block(site)["items"]
        self.assertEqual((item["code"], item["severity"]), ("framework_build_blocked_by_advisory", "blocking"))
        with self.assertRaises(WebsiteAuthorityError):
            repo_health.require_green_build(site)

    @ON
    def test_host_not_deploying_blocks_even_with_a_green_build(self):
        site = connection({"build": build(), "host": {"status": "failed", "codes": ["host_not_deploying"]}})
        self.assertEqual([row["code"] for row in repo_health.items(site)], ["host_not_deploying"])
        with self.assertRaises(WebsiteAuthorityError) as raised:
            repo_health.require_green_build(site)
        self.assertEqual(raised.exception.code, "repo_health_blocked")

    @ON
    def test_warnings_show_but_never_block(self):
        site = connection({"build": build(), "host": {"status": "passed", "codes": ["checks_permission_missing"]},
                           "sitemap": {"status": "failed"}})
        block = repo_health.journey_block(site)
        self.assertFalse(block["blocking"])
        self.assertEqual([(row["code"], row["severity"]) for row in block["items"]],
                         [("checks_permission_missing", "warning"), ("sitemap_unreachable", "warning")])
        repo_health.require_green_build(site)
        self.assertFalse(repo_health.setup_blocked(site))

    @ON
    def test_green_probe_only_counts_for_the_current_head(self):
        site = connection({"build": build(sha=OLD)})
        with self.assertRaises(WebsiteAuthorityError) as raised:
            repo_health.require_green_build(site)
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(repo_health.journey_block(site)["buildStatus"], "not_checked")
        # A push moved the head past the green build.
        site = connection({"build": build(), "head_sha": OLD})
        with self.assertRaises(WebsiteAuthorityError):
            repo_health.require_green_build(site)
        repo_health.require_green_build(connection({"build": build()}))

    @ON
    def test_new_generation_starts_clean(self):
        site = connection({"build": build(repo_health.TYPE_ERROR)}, generation=2)
        site.generation = 3
        self.assertIsNone(repo_health.journey_block(site))

    @ON
    def test_journey_block_matches_the_strict_contract(self):
        schema = _schema()
        rule = {"$ref": "#/$defs/FounderRepoHealth"}
        for record in ({"build": build(repo_health.LOCKFILE_OUT_OF_SYNC), "sitemap": {"status": "failed"},
                        "host": {"status": "failed", "codes": ["host_not_deploying", "checks_permission_missing"],
                                 "checked_at": NOW.isoformat()}},
                       {"build": build()}, {"host": {"status": "unknown", "codes": []}}):
            _validate(repo_health.journey_block(connection(record)), rule, schema["$defs"])
        with self.assertRaises(FounderContractMismatch):
            _validate({**repo_health.journey_block(connection({"build": build()})), "extra": 1}, rule, schema["$defs"])
        journey = schema["$defs"]["FounderJourney"]
        self.assertEqual(journey["properties"]["repoHealth"], rule)
        self.assertNotIn("repoHealth", journey["required"])


class RecordTests(SimpleTestCase):
    @ON
    def test_build_result_is_validated_and_stored_for_its_commit(self):
        site = connection()
        with patch.object(repo_health, "_store", side_effect=lambda conn, patch_: patch_) as store:
            stored = repo_health.record_build_probe(site, build(repo_health.LOCKFILE_OUT_OF_SYNC), now=NOW)
        self.assertEqual(stored["build"]["code"], "lockfile_out_of_sync")
        self.assertEqual(stored["build"]["commit_sha"], HEAD)
        self.assertEqual(stored["build"]["checked_at"], NOW.isoformat())
        store.assert_called_once()
        for bad in ({**build(), "commit_sha": "HEAD"}, {**build("made_up")}, {**build(repo_health.TYPE_ERROR), "blocking": False}):
            with self.assertRaises(ValueError):
                repo_health.record_build_probe(site, bad)

    @ON
    def test_refresh_host_stores_host_sitemap_and_head(self):
        recent = (NOW - timedelta(days=1)).isoformat()
        reader = FakeReader(commits(1), statuses={HEAD: [{"context": "netlify/deploy", "updated_at": recent}]})
        with patch.object(repo_health, "_store", side_effect=lambda conn, patch_: patch_):
            stored = repo_health.refresh_host(connection(), reader=reader, checks_granted=True,
                                              fetch=lambda u, d: (b"<urlset/>", {}), now=NOW)
        self.assertEqual(stored["host"]["status"], "passed")
        self.assertEqual(stored["sitemap"]["status"], "passed")
        self.assertEqual(stored["head_sha"], HEAD)

    def test_build_probe_reruns_only_for_build_inputs(self):
        self.assertTrue(repo_health.build_probe_needed(["package-lock.json"]))
        self.assertTrue(repo_health.build_probe_needed(["apps/web/next.config.mjs"]))
        self.assertTrue(repo_health.build_probe_needed(["package.json"]))
        self.assertFalse(repo_health.build_probe_needed(["app/page.tsx", "README.md"]))


class WebhookAndChargeWiringTests(SimpleTestCase):
    PUSH = {"ref": "refs/heads/main", "after": HEAD, "repository": {"id": 7, "full_name": "o/r", "default_branch": "main"},
            "commits": [{"modified": ["package-lock.json"]}]}

    def _process(self):
        from . import github_webhook
        with patch("content_factory.website_reconciliation.handle_website_github_event", return_value={"invalidated": 0}), \
             patch.object(github_webhook, "handle_push", return_value={"event": "push", "matched": 0, "updated": 0}), \
             patch.object(repo_health, "on_push", return_value={"repo_health_refreshed": 1}) as on_push:
            return github_webhook.process_github_event("push", self.PUSH), on_push

    def test_push_leaves_today_response_when_off(self):
        result, on_push = self._process()
        on_push.assert_not_called()
        self.assertNotIn("repo_health_refreshed", result)

    @ON
    def test_push_reruns_the_host_probe_when_on(self):
        result, on_push = self._process()
        on_push.assert_called_once_with(self.PUSH)
        self.assertEqual(result["repo_health_refreshed"], 1)

    def _reserve(self, site):
        from . import vibe_marketing_views as views
        context = SimpleNamespace(organization=SimpleNamespace(pk=1))
        config = SimpleNamespace(website_connection=site, default_publish_target_id="")
        with patch.object(views, "_get_config", return_value=config), \
             patch.object(views, "contract_for", return_value={}), \
             patch.object(views, "connection_contract", return_value={}), \
             patch.object(views, "owner_operation_contract", return_value={}):
            return views._reserve_article_start_connection(context, {"delivery_mode": "website"}, reserve_operation=False)

    def test_charge_is_not_gated_when_off(self):
        self.assertIsNone(self._reserve(connection({"build": build(repo_health.LOCKFILE_OUT_OF_SYNC)})))

    @ON
    def test_charge_is_refused_before_debit_without_a_green_build(self):
        response = self._reserve(connection({"build": build(repo_health.LOCKFILE_OUT_OF_SYNC)}))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "repo_health_build_not_green")
        self.assertIsNone(self._reserve(connection({"build": build()})))


class FakeRepoClient:
    def __init__(self):
        self.calls = []

    def create_branch(self, name, sha):
        self.calls.append(("branch", name, sha))

    def put_file(self, branch, path, content, message):
        self.calls.append(("file", branch, path))

    def create_pull(self, head, base, title, body):
        self.calls.append(("pull", head, base, title))
        return "https://github.com/o/r/pull/12"


class FixPullRequestTests(SimpleTestCase):
    FILES = {"package-lock.json": "{}\n"}

    def test_held_off_by_default_and_with_only_the_main_setting(self):
        client = FakeRepoClient()
        self.assertIsNone(repo_health.open_fix_pull_request(connection(), "lockfile_out_of_sync", client=client,
                                                            files=self.FILES, base_sha=HEAD))
        with ON:
            self.assertIsNone(repo_health.open_fix_pull_request(connection(), "lockfile_out_of_sync", client=client,
                                                                files=self.FILES, base_sha=HEAD))
        self.assertEqual(client.calls, [])

    @override_settings(ARTICLES_REPO_HEALTH_ENABLED=True, ARTICLES_REPO_HEALTH_FIX_PR_ENABLED=True)
    def test_opens_one_branch_and_pull_request_against_the_connected_branch(self):
        client = FakeRepoClient()
        url = repo_health.open_fix_pull_request(connection(), "lockfile_out_of_sync", client=client,
                                                files=self.FILES, base_sha=HEAD)
        self.assertEqual(url, "https://github.com/o/r/pull/12")
        branch = "mlai/repo-health-lockfile-out-of-sync-aaaaaaa"
        self.assertEqual(client.calls, [("branch", branch, HEAD), ("file", branch, "package-lock.json"),
            ("pull", branch, "main", "Update the lockfile so a clean install works")])

    @override_settings(ARTICLES_REPO_HEALTH_ENABLED=True, ARTICLES_REPO_HEALTH_FIX_PR_ENABLED=True)
    def test_refuses_other_codes_and_other_files(self):
        client = FakeRepoClient()
        with self.assertRaises(repo_health.FixNotOffered):
            repo_health.open_fix_pull_request(connection(), "type_error", client=client, files=self.FILES, base_sha=HEAD)
        with self.assertRaises(repo_health.FixNotOffered):
            repo_health.open_fix_pull_request(connection(), "lockfile_out_of_sync", client=client,
                                              files={"src/app.ts": "x"}, base_sha=HEAD)
        self.assertEqual(client.calls, [])

    def test_patch_bump_stays_within_the_minor_version(self):
        package = json.dumps({"dependencies": {"next": "16.4.0", "react": "19.0.0"}})
        self.assertEqual(json.loads(repo_health.patch_bump(package, "next", "16.4.1"))["dependencies"]["next"], "16.4.1")
        caret = json.dumps({"dependencies": {"next": "^16.4.0"}})
        self.assertEqual(json.loads(repo_health.patch_bump(caret, "next", "16.4.2"))["dependencies"]["next"], "^16.4.2")
        for target in ("16.5.0", "17.0.0", "16.3.9", "16.4.0"):
            with self.assertRaises(repo_health.FixNotOffered):
                repo_health.patch_bump(package, "next", target)
        with self.assertRaises(repo_health.FixNotOffered):
            repo_health.patch_bump(package, "vite", "5.0.1")
