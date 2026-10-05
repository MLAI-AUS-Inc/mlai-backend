"""An explicit, atomic operational ownership transfer with an expected owner."""

from django.contrib.auth import get_user_model
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from community_chat.models import ChatRole, CommunityChatDevice


class Command(BaseCommand):
    help = (
        "Preview a Chat ownership transfer; --apply requires the exact current owner."
    )

    def add_arguments(self, parser):
        parser.add_argument("--current-owner-user-id", type=int, required=True)
        parser.add_argument("--new-owner-user-id", type=int, required=True)
        parser.add_argument("--apply", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        current_id, new_id = (
            options["current_owner_user_id"],
            options["new_owner_user_id"],
        )
        if current_id == new_id:
            raise CommandError("Choose a different account for ownership transfer.")
        users = {
            u.pk: u
            for u in get_user_model()
            .objects.select_for_update()
            .filter(pk__in=(current_id, new_id))
            .order_by("pk")
        }
        owner = ChatRole.objects.select_for_update().filter(role="owner").first()
        if owner is None or owner.user_id != current_id:
            raise CommandError("The expected current Chat owner does not match.")
        if (
            new_id not in users
            or not users[new_id].is_active
            or not CommunityChatDevice.objects.filter(
                user_id=new_id,
                status="verified",
                revoked_at__isnull=True,
            ).exists()
        ):
            raise CommandError(
                "The new owner must be active with a verified Chat installation."
            )
        self.stdout.write(
            f"Transfer Chat ownership from account {current_id} to {new_id}; previous owner becomes admin."
        )
        if not options["apply"]:
            self.stdout.write("Preview only. Ownership unchanged.")
            return
        owner.role = "admin"
        owner.save(update_fields=("role", "updated_at"))
        new_owner, _ = ChatRole.objects.update_or_create(
            user_id=new_id, defaults={"role": "owner"}
        )
        for appointment in (owner, new_owner):
            LogEntry.objects.log_action(
                user_id=current_id,
                content_type_id=ContentType.objects.get_for_model(ChatRole).pk,
                object_id=appointment.pk,
                object_repr=f"Chat appointment for account {appointment.user_id}",
                action_flag=CHANGE,
                change_message=f"Reviewed ownership transfer: {current_id} → {new_id}; role={appointment.role}",
            )
        self.stdout.write("Chat ownership transferred atomically.")
