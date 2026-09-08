"""Run Volunteer tests against an explicitly approved, disposable PostgreSQL DB.

This bypasses the default runner's all-app migration setup. Only
community_chat.0009_volunteer, explicitly supplied approved existing test
prerequisite targets, and their transitive migration dependencies apply.
The connection JSON must describe a newly provisioned loopback test cluster;
the script never loads .env or accepts a pre-existing test database.
"""

import argparse
import json
import os
from pathlib import Path
import re
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-json", required=True)
    parser.add_argument(
        "--approved-migration", required=True, choices=["community_chat.0009_volunteer"]
    )
    parser.add_argument(
        "--approved-existing-prerequisite",
        action="append",
        default=[],
        choices=[
            "core.0056_user_community_chat_profile_id",
            "core.0068_merge_0059_purge_stale_content_types_0067_merge_0058_drop_orphan_tables_from_removed_apps_0066_guard_orphaned_actor_migration_history",
            "founder_tools.0010_company_default_audience_visibility",
            "integrations.0046_communitybridgedeletionrequest",
            "organizations.0002_organization_company_linkedin_url",
        ],
        help="Explicitly approved existing test prerequisite; repeat for each approved target.",
    )
    parser.add_argument(
        "--approved-plan",
        type=Path,
        help="Approved proposal whose full migration closure must exactly match this run.",
    )
    parser.add_argument(
        "--use-approved-plan-targets",
        action="store_true",
        help="Use the exact targets listed in --approved-plan instead of prerequisite flags.",
    )
    parser.add_argument("tests", nargs="*")
    args = parser.parse_args()
    if args.use_approved_plan_targets and not args.approved_plan:
        parser.error("--use-approved-plan-targets requires --approved-plan")
    config = json.loads(Path(args.connection_json).read_text())
    if config["host"] not in ("127.0.0.1", "::1") or not config[
        "test_database"
    ].startswith("test_volunteer_disposable"):
        raise SystemExit("Expected a dedicated loopback Volunteer disposable database.")
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo))
    os.environ.clear()
    os.environ.update(
        PATH="/usr/bin:/bin",
        DATABASE_URL="sqlite:///:memory:",
        SECRET_KEY="volunteer-synthetic-test-key",
        DJANGO_SETTINGS_MODULE="mlai.settings",
        DEBUG="true",
        APP_ENV="test",
        APP_RELEASE="synthetic-volunteer-tests",
        CONNECTOR_CREDENTIAL_KEYS='{"test":"MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="}',
        CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID="test",
    )
    # python-dotenv 1.0.1 has no reliable environment-only disable switch.
    import dotenv

    dotenv.load_dotenv = lambda *args, **kwargs: False
    sys.argv.append("test")
    import django
    from django.conf import settings

    settings.DATABASES["default"] = dict(
        ENGINE="django.db.backends.postgresql",
        NAME=config["test_database"],
        USER=config["user"],
        PASSWORD=config["password"],
        HOST=config["host"],
        PORT=config["port"],
        CONN_MAX_AGE=0,
    )
    # No network-backed cache, task queue, email or production telemetry.
    settings.CACHES = {
        alias: {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": f"volunteer-synthetic-{alias}",
        }
        for alias in settings.CACHES
    }
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    django.setup()

    import psycopg
    from psycopg import sql
    from django.db import connection, connections
    from django.db.migrations.executor import MigrationExecutor
    from django.test.runner import DiscoverRunner

    class ScopedRunner(DiscoverRunner):
        def setup_databases(self, **kwargs):
            executor = MigrationExecutor(connection)
            targets = [("community_chat", "0009_volunteer")]
            targets.extend(
                tuple(value.split(".", 1))
                for value in args.approved_existing_prerequisite
            )
            if args.use_approved_plan_targets:
                target_section = (
                    args.approved_plan.read_text()
                    .split("\nTargets:\n", 1)[1]
                    .split("\nFull existing dependency closure (", 1)[0]
                )
                targets = [
                    tuple(value.split(".", 1))
                    for value in re.findall(
                        r"^- `([^`]+)`$", target_section, re.MULTILINE
                    )
                ]
                if ("community_chat", "0009_volunteer") not in targets:
                    raise RuntimeError(
                        "Approved plan must include the Volunteer target."
                    )
            plan = executor.migration_plan(targets)
            if any(backwards for _, backwards in plan):
                raise RuntimeError(
                    "Unexpected backwards migration in empty test database"
                )
            if args.approved_plan:
                proposal = args.approved_plan.read_text()
                closure = proposal.split("Full existing dependency closure (", 1)[1]
                approved = re.findall(r"^- `([^`]+)`$", closure, re.MULTILINE)
                actual = [
                    f"{migration.app_label}.{migration.name}" for migration, _ in plan
                ]
                if len(approved) != len(set(approved)) or set(actual) != set(approved):
                    raise RuntimeError(
                        "Migration plan differs from the approved dependency closure: "
                        f"unapproved={sorted(set(actual) - set(approved))}; "
                        f"missing={sorted(set(approved) - set(actual))}"
                    )
                print(
                    f"Verified exact approved closure: {len(actual)} existing migrations."
                )
            print(
                f"Applying approved migration and {len(plan) - 1} dependency migrations."
            )
            executor.migrate(targets)
            return []

        def teardown_databases(self, old_config, **kwargs):
            connections.close_all()

    with psycopg.connect(
        host=config["host"],
        port=config["port"],
        user=config["user"],
        password=config["password"],
        dbname=config["database"],
        autocommit=True,
    ) as control:
        exists = control.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (config["test_database"],)
        ).fetchone()
        if exists:
            raise SystemExit(
                "Disposable test database already exists; refusing to reuse or remove it."
            )
        control.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(config["test_database"])
            )
        )
        try:
            failures = ScopedRunner(verbosity=2, interactive=False).run_tests(
                args.tests
                or [
                    "community_chat.tests.test_volunteer_policy",
                    "community_chat.tests.test_volunteer",
                    "community_chat.tests.test_volunteer_concurrency",
                    "community_chat.tests.test_volunteer_backfill",
                    "community_chat.tests.test_volunteer_presentation",
                    "roo.tests.StartupUpdateRewardServiceTests",
                    "roo.tests_api.FirstChannelPostAwardViewTests",
                    "roo.tests_api.FirstChannelPostAwardConcurrencyTests",
                    "roo.tests_permissions",
                ]
            )
        finally:
            connections.close_all()
            control.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    sql.Identifier(config["test_database"])
                )
            )
            print("Removed this run's disposable test database.")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
