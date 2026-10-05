"""Replay approved community_chat.0014 and test governance in disposable Postgres.

Uses only synthetic accounts, blocks external network and .env loading, and
removes the socket-only cluster afterward. Requires explicit migration approval.
"""

from test_account_bans_disposable import main


def check_chat_roles_and_replay():
    from django.db import IntegrityError, connection, transaction
    from django.db.migrations.executor import MigrationExecutor

    migration = ("community_chat", "0014_chat_roles")
    previous = ("community_chat", "0013_account_deletion_tasks")
    executor = MigrationExecutor(connection)
    targets = executor.loader.graph.leaf_nodes()
    before = [previous if target == migration else target for target in targets]
    executor.migrate(before)
    old_apps = executor.loader.project_state(before).apps
    user_model = old_apps.get_model("core", "User")
    users = [
        user_model.objects.create(
            email=f"migration-{index}@example.test",
            is_active=True,
            is_staff=index == 0,
            is_superuser=index == 0,
        )
        for index in range(3)
    ]
    old_apps.get_model("roo", "PointsAdmin").objects.create(
        user=users[1], slack_user_id="SYNTHETIC_COMMITTEE", role="committee"
    )
    old_apps.get_model("community_chat", "Moderator").objects.create(user=users[2])
    for user, letter in zip(users, "abc"):
        old_apps.get_model("community_chat", "CommunityChatDevice").objects.create(
            user=user, public_key=letter * 64, status="verified"
        )
    preserved = (
        ("core", "User"),
        ("roo", "PointsAdmin"),
        ("community_chat", "Moderator"),
        ("community_chat", "CommunityChatDevice"),
    )
    snapshots = {
        model: list(old_apps.get_model(*model).objects.order_by("pk").values())
        for model in preserved
    }
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    apps = executor.loader.project_state(targets).apps
    for model, original in snapshots.items():
        assert list(apps.get_model(*model).objects.order_by("pk").values()) == original
    roles = apps.get_model("community_chat", "ChatRole")
    assert roles.objects.count() == 0, "Migration must not appoint existing users."
    roles.objects.create(user_id=users[0].pk, role="owner")
    roles.objects.create(user_id=users[1].pk, role="admin")
    for invalid_role in ("owner", "superuser"):
        try:
            with transaction.atomic():
                roles.objects.create(user_id=users[2].pk, role=invalid_role)
        except IntegrityError:
            pass
        else:
            raise AssertionError(
                f"Database accepted invalid appointment: {invalid_role}"
            )
    assert roles.objects.filter(role="owner").count() == 1
    roles.objects.all().delete()
    print(
        "0013 → 0014 passed: accounts, Roo roles, moderators and devices unchanged; "
        "no implicit appointments; single-owner and valid-role constraints enforced.",
        flush=True,
    )


if __name__ == "__main__":
    main(
        migration_check=check_chat_roles_and_replay,
        default_labels=[
            "community_chat.tests.test_chat_governance",
            "community_chat.tests.test_permissions",
            "community_chat.tests.test_account_bans",
            "community_chat.tests.test_account_privacy",
        ],
    )
