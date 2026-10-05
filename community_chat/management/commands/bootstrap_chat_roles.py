"""Provision a reviewed account roster without granting backend or Roo powers."""

from django.contrib.auth import get_user_model
from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from community_chat.models import ChatRole, CommunityChatDevice


class Command(BaseCommand):
    help = (
        "Preview Chat owner/admin appointments by exact user IDs; --apply writes them."
    )

    def add_arguments(self, parser):
        parser.add_argument("--owner-user-id", type=int, required=True)
        parser.add_argument("--admin-user-id", type=int, action="append", default=[])
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        owner = options["owner_user_id"]
        admins = options["admin_user_id"]
        ids = [owner, *admins]
        if len(set(ids)) != len(ids):
            raise CommandError(
                "Use distinct account IDs; the owner cannot also be an admin."
            )
        try:
            with transaction.atomic():
                users = {
                    user.pk: user
                    for user in get_user_model()
                    .objects.select_for_update()
                    .filter(pk__in=ids)
                    .order_by("pk")
                }
                if set(users) != set(ids) or any(
                    not user.is_active for user in users.values()
                ):
                    raise CommandError(
                        "Every ID must identify an existing active MLAI account."
                    )
                verified = set(
                    CommunityChatDevice.objects.filter(
                        user_id__in=ids,
                        status="verified",
                        revoked_at__isnull=True,
                    ).values_list("user_id", flat=True)
                )
                if verified != set(ids):
                    raise CommandError(
                        "Every selected account must have a verified Chat installation."
                    )
                expected = {owner: "owner", **{user_id: "admin" for user_id in admins}}
                current = dict(
                    ChatRole.objects.select_for_update().values_list("user_id", "role")
                )
                if any(
                    expected.get(user_id) != role for user_id, role in current.items()
                ):
                    raise CommandError(
                        "Existing appointments differ from this roster. Use owner controls; bootstrap never replaces an owner or removes admins."
                    )
                for user_id in ids:
                    user = users[user_id]
                    self.stdout.write(
                        f"{user_id}: {user.full_name} <{user.email}> → {expected[user_id]}"
                    )
                if not options["apply"]:
                    self.stdout.write(
                        "Preview only. No accounts or appointments changed."
                    )
                    return
                for user_id in ids:
                    appointment, created = ChatRole.objects.get_or_create(
                        user_id=user_id,
                        defaults={"role": expected[user_id]},
                    )
                    if created:
                        LogEntry.objects.log_action(
                            user_id=owner,
                            content_type_id=ContentType.objects.get_for_model(
                                ChatRole
                            ).pk,
                            object_id=appointment.pk,
                            object_repr=f"Chat appointment for account {user_id}",
                            action_flag=ADDITION,
                            change_message=f"Reviewed bootstrap: {expected[user_id]}",
                        )
                self.stdout.write(
                    "Chat appointments applied. Backend and Roo permissions unchanged."
                )
        except IntegrityError as exc:
            raise CommandError(
                "Appointments changed concurrently; review the roster again."
            ) from exc
