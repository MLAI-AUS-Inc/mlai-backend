import ast
import re
import shlex
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import yaml

from scripts.ci_reuse_pr_checks import (
    attestation_name,
    find_reusable_validation,
    main,
)


REPOSITORY = "MLAI-AUS-Inc/mlai-backend"
COMMIT_SHA = "a" * 40
HEAD_SHA = "b" * 40
TREE_SHA = "c" * 40
WORKFLOW_ID = 123
RUN_ID = 456
IN_MEMORY_DATA_OPTION = " --tmpfs /var/lib/postgresql/data"


def api_fixture(
    *,
    artifact_tree=TREE_SHA,
    expired=False,
    run_pull_number=42,
    run_attempt=1,
    artifact_attempt=1,
):
    def api_get(path, params=None):
        if path.endswith(f"/commits/{COMMIT_SHA}/pulls"):
            return [
                {
                    "base": {"ref": "main"},
                    "head": {"sha": HEAD_SHA},
                    "merge_commit_sha": COMMIT_SHA,
                    "merged_at": "2026-08-13T00:00:00Z",
                    "number": 42,
                }
            ]
        if path.endswith(f"/actions/workflows/{WORKFLOW_ID}/runs"):
            return {
                "workflow_runs": [
                    {
                        "conclusion": "success",
                        "event": "pull_request",
                        "id": RUN_ID,
                        "pull_requests": (
                            []
                            if run_pull_number is None
                            else [{"number": run_pull_number}]
                        ),
                        "run_attempt": run_attempt,
                    }
                ]
            }
        if path.endswith(f"/actions/runs/{RUN_ID}/artifacts"):
            return {
                "artifacts": [
                    {
                        "expired": expired,
                        "name": attestation_name(artifact_tree, HEAD_SHA, artifact_attempt),
                    }
                ]
            }
        raise AssertionError(f"Unexpected GitHub API request: {path} {params}")

    return api_get


class ReusePrChecksTests(TestCase):
    def route(self, api_get):
        return find_reusable_validation(
            api_get=api_get,
            repository=REPOSITORY,
            commit_sha=COMMIT_SHA,
            branch="main",
            workflow_id=WORKFLOW_ID,
            current_tree=TREE_SHA,
        )

    def test_reuses_successful_validation_for_exact_tree_and_pull_request(self):
        reuse, reason = self.route(api_fixture())

        self.assertTrue(reuse)
        self.assertIn("PR #42", reason)
        self.assertIn(str(RUN_ID), reason)

    def test_reuses_when_github_omits_run_pull_request_associations(self):
        reuse, reason = self.route(api_fixture(run_pull_number=None))

        self.assertTrue(reuse)
        self.assertIn("PR #42", reason)

    def test_stale_base_tree_falls_back_to_full_checks(self):
        reuse, reason = self.route(api_fixture(artifact_tree="d" * 40))

        self.assertFalse(reuse)
        self.assertIn("exact deployed tree", reason)

    def test_expired_attestation_falls_back_to_full_checks(self):
        reuse, _reason = self.route(api_fixture(expired=True))

        self.assertFalse(reuse)

    def test_reuses_attestation_kept_from_an_earlier_attempt_of_the_run(self):
        # Re-running only a failed job keeps the checks job from attempt 1.
        reuse, reason = self.route(api_fixture(run_attempt=2, artifact_attempt=1))

        self.assertTrue(reuse)
        self.assertIn(str(RUN_ID), reason)

    def test_attestation_from_a_later_attempt_than_the_run_is_not_reused(self):
        reuse, _reason = self.route(api_fixture(run_attempt=1, artifact_attempt=2))

        self.assertFalse(reuse)

    def test_expired_earlier_attempt_attestation_falls_back_to_full_checks(self):
        reuse, _reason = self.route(
            api_fixture(run_attempt=2, artifact_attempt=1, expired=True)
        )

        self.assertFalse(reuse)

    def test_attestation_from_another_pull_request_is_not_reused(self):
        reuse, _reason = self.route(api_fixture(run_pull_number=99))

        self.assertFalse(reuse)

    def test_only_successful_runs_of_the_final_pull_request_commit_are_considered(self):
        # A newer push cancels the run for the commit it replaced. That is safe
        # because validation is only ever read from the final commit's runs.
        fixture = api_fixture()
        run_queries = []

        def api_get(path, params=None):
            if path.endswith(f"/actions/workflows/{WORKFLOW_ID}/runs"):
                run_queries.append(dict(params))
            return fixture(path, params)

        reuse, _reason = self.route(api_get)

        self.assertTrue(reuse)
        self.assertEqual(
            run_queries,
            [
                {
                    "event": "pull_request",
                    "head_sha": HEAD_SHA,
                    "per_page": "100",
                    "status": "success",
                }
            ],
        )

    def test_cancelled_run_is_not_reused_even_with_a_matching_attestation(self):
        # The checks job can finish and attest before the run is cancelled.
        fixture = api_fixture()

        def api_get(path, params=None):
            response = fixture(path, params)
            if path.endswith(f"/actions/workflows/{WORKFLOW_ID}/runs"):
                response["workflow_runs"][0]["conclusion"] = "cancelled"
            return response

        reuse, reason = self.route(api_get)

        self.assertFalse(reuse)
        self.assertIn("no successful workflow", reason)

    def test_direct_push_falls_back_without_querying_workflow_runs(self):
        def api_get(path, params=None):
            self.assertTrue(path.endswith(f"/commits/{COMMIT_SHA}/pulls"))
            return []

        reuse, reason = self.route(api_get)

        self.assertFalse(reuse)
        self.assertIn("no exact merged pull request", reason)

    def test_deploy_workflow_preserves_full_check_fallback(self):
        workflow = (
            Path(__file__).resolve().parents[1] / ".github/workflows/deploy.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("validation-route:", workflow)
        self.assertNotIn("record-pr-validation:", workflow)
        self.assertEqual(workflow.count("reuse_pr_checks != 'true'"), 4)
        self.assertIn("needs.validation-route.outputs.reuse_pr_checks == 'true'", workflow)
        self.assertIn("needs.checks.result == 'success'", workflow)
        self.assertIn("needs.postgres-search.result == 'success'", workflow)
        self.assertIn("needs.migration-tests.result == 'success'", workflow)
        self.assertIn("needs.p2-performance.result == 'success'", workflow)

    def test_isolated_tick_remains_a_required_release_gate_with_no_missing_tagged_cases(self):
        root = Path(__file__).resolve().parents[1]
        jobs = yaml.safe_load((root / '.github/workflows/deploy.yml').read_text())['jobs']
        performance = jobs['p2-performance']
        # The timed tick keeps the disk-backed service it was calibrated on. The
        # other PostgreSQL lanes run the same service with its files in memory.
        search_service = jobs['postgres-search']['services']['postgres']
        self.assertTrue(search_service['options'].endswith(IN_MEMORY_DATA_OPTION))
        self.assertEqual(performance['services'], {'postgres': {
            **search_service,
            'options': search_service['options'].removesuffix(IN_MEMORY_DATA_OPTION),
        }})
        self.assertNotIn('docker exec', ''.join(step.get('run', '') for step in performance['steps']))
        self.assertEqual(performance['env'], jobs['postgres-search']['env'])
        self.assertIn('p2-performance', jobs['deploy']['needs'])
        self.assertIn("needs.p2-performance.result == 'success'", jobs['deploy']['if'])
        command = performance['steps'][-1]['run']
        cases = []
        for path in root.rglob('test*.py'):
            if any(part.startswith('.') for part in path.relative_to(root).parts):
                continue
            module = '.'.join(path.relative_to(root).with_suffix('').parts)
            tree = ast.parse(path.read_text())
            for parent in tree.body:
                if not isinstance(parent, ast.ClassDef):
                    continue
                for node in parent.body:
                    if not isinstance(node, ast.FunctionDef):
                        continue
                    if any(isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name)
                           and decorator.func.id == 'tag' and any(isinstance(arg, ast.Constant)
                           and arg.value == 'p2_performance' for arg in decorator.args)
                           for decorator in node.decorator_list):
                        cases.append(f'{module}.{parent.name}.{node.name}')
        self.assertEqual(len(cases), 1)
        self.assertIn(cases[0], command)
        self.assertNotIn('--exclude-tag', command)
        policy = next(step['run'] for step in jobs['postgres-search']['steps']
                      if step.get('name') == 'Verify Founder recovery policy and authority on PostgreSQL')
        self.assertIn('--exclude-tag=p2_performance', policy)

    def test_manual_dispatch_uses_full_checks_and_main_release_gate(self):
        workflow = (
            Path(__file__).resolve().parents[1] / ".github/workflows/deploy.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("  workflow_dispatch:", workflow)
        self.assertIn("github.event_name == 'workflow_dispatch'", workflow)
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        # Main pushes and manual releases share one group, where a run in
        # progress is never cancelled. Each pull request has its own group,
        # where a later run cancels the one in progress.
        self.assertEqual(
            yaml.load(workflow, Loader=yaml.BaseLoader)["concurrency"],
            {
                "group": "mlai-backend-deploy-${{ github.ref }}",
                "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
            },
        )

        with TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "github-output"
            with patch.dict(
                "os.environ",
                {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_OUTPUT": str(output_path)},
            ):
                self.assertEqual(main(), 0)
            self.assertIn("reuse_pr_checks=false", output_path.read_text())

    def test_database_lanes_reuse_one_migrated_database_and_bound_in_memory_files(self):
        root = Path(__file__).resolve().parents[1]
        jobs = yaml.safe_load((root / '.github/workflows/deploy.yml').read_text())['jobs']
        # Without this switch every `manage.py test` call re-applies all migrations.
        for lane in ('checks', 'postgres-search', 'migration-tests', 'p2-performance'):
            self.assertEqual(jobs[lane]['env']['MLAI_TEST_DATABASE_TEMPLATE'], '1', lane)
        self.assertIn(
            "TEST_RUNNER = 'mlai.test_runner.TemplateDatabaseRunner'",
            (root / 'mlai/settings.py').read_text(),
        )
        # An in-memory data directory holds a limited number of files, and each
        # emptied table leaves one until the next checkpoint. The two settings
        # travel together, before any test command runs.
        for lane in ('postgres-search', 'migration-tests'):
            self.assertTrue(
                jobs[lane]['services']['postgres']['options'].endswith(IN_MEMORY_DATA_OPTION),
                lane,
            )
            first_step = jobs[lane]['steps'][0]['run']
            self.assertIn('= tmpfs', first_step, lane)
            # The image's first-start server answers on the socket only. Every
            # database command of this step waits for, and uses, the address
            # that only the final server serves.
            self.assertIn('pg_isready -q -h 127.0.0.1', first_step, lane)
            self.assertIn('psql -h 127.0.0.1', first_step, lane)
            self.assertEqual(first_step.count(' -h 127.0.0.1 '), 2, lane)
            self.assertIn("ALTER SYSTEM SET checkpoint_timeout = '30s'", first_step, lane)
            self.assertIn("ALTER SYSTEM SET max_wal_size = '256MB'", first_step, lane)
            self.assertIn('pg_reload_conf()', first_step, lane)

    def test_every_postgres_only_test_is_selected_by_a_postgresql_lane(self):
        root = Path(__file__).resolve().parents[1]
        jobs = yaml.safe_load((root / '.github/workflows/deploy.yml').read_text())['jobs']
        # Labels of the PostgreSQL commands that run tests with this tag. The
        # SQLite lane excludes the tag, so nothing else runs them.
        selected = set()
        for lane in ('postgres-search', 'migration-tests'):
            for step in jobs[lane]['steps']:
                for command in step.get('run', '').replace('\\\n', ' ').splitlines():
                    if 'manage.py test' not in command or '--tag' in command:
                        continue
                    if re.search(r'--exclude-tag[= ]postgres-only', command):
                        continue
                    selected.update(
                        token for token in shlex.split(command)
                        if re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+', token)
                    )

        def tagged(node):
            return any(
                isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name)
                and decorator.func.id == 'tag' and any(
                    isinstance(argument, ast.Constant) and argument.value == 'postgres-only'
                    for argument in decorator.args
                )
                for decorator in node.decorator_list
            )

        targets = []
        for path in root.rglob('test*.py'):
            if any(part.startswith('.') for part in path.relative_to(root).parts):
                continue
            module = '.'.join(path.relative_to(root).with_suffix('').parts)
            for parent in ast.parse(path.read_text()).body:
                if not isinstance(parent, ast.ClassDef):
                    continue
                if tagged(parent):
                    targets.append(f'{module}.{parent.name}')
                targets.extend(
                    f'{module}.{parent.name}.{node.name}' for node in parent.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and tagged(node)
                )

        # The six Slack founder-link migration classes run nowhere else.
        founder_links = [target for target in targets if target.startswith('core.tests_slack_founder_links.')]
        self.assertGreaterEqual(len(founder_links), 6)
        missing = sorted(
            target for target in targets
            if not any(target == label or target.startswith(label + '.') for label in selected)
        )
        self.assertEqual(missing, [])

    def test_parallel_test_commands_can_report_a_failure(self):
        root = Path(__file__).resolve().parents[1]
        jobs = yaml.safe_load((root / '.github/workflows/deploy.yml').read_text())['jobs']
        parallel = {}
        for name, job in jobs.items():
            runs = [step.get('run', '') for step in job.get('steps', [])]
            commands = [index for index, run in enumerate(runs) if '--parallel' in run]
            if not commands:
                continue
            parallel[name] = [job['steps'][index]['name'] for index in commands]
            # Without tblib a worker cannot send a failure's traceback back, and
            # Django stops at the first failing test with an error about that.
            installs = [
                index for index, run in enumerate(runs)
                if '-r .github/requirements-ci.lock' in run
                and '--require-hashes' in run and '--only-binary=:all:' in run
            ]
            self.assertTrue(installs and installs[0] < commands[0], name)
            # Each worker clones the migrated database, which the reuse sets up.
            self.assertEqual(job['env']['MLAI_TEST_DATABASE_TEMPLATE'], '1', name)
        # Only the migration tests, which are bound by one core each, run in
        # parallel. Timing-sensitive suites stay serial.
        self.assertEqual(parallel, {'migration-tests': ['Run migration tests']})
        self.assertIn('tblib==', (root / '.github/requirements-ci.txt').read_text())
