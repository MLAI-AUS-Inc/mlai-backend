"""Backfill account bindings without changing backend models or migrations."""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from community_chat.adapter import MembershipAdapterError
from community_chat.inbox_accounts import bind_verified_device
from community_chat.models import CommunityChatDevice, DeviceBindingStatus


class Command(BaseCommand):
    help = "Preview verified device bindings. --apply opts into private relay writes."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--apply", action="store_true")
        mode.add_argument("--dry-run", action="store_true")
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--after-device-id", type=int, default=0)

    def handle(self, *args, **options):
        limit = options["limit"]
        after = options["after_device_id"]
        if not 1 <= limit <= 10000 or after < 0:
            raise CommandError("limit must be 1..10000 and after-device-id nonnegative")
        apply = options["apply"]
        if apply and not settings.COMMUNITY_CHAT_MEMBER_ACCOUNTS_ENABLED:
            raise CommandError("COMMUNITY_CHAT_MEMBER_ACCOUNTS_ENABLED is required for --apply")
        candidates = list(
            CommunityChatDevice.objects.filter(
                id__gt=after,
                status=DeviceBindingStatus.VERIFIED,
                revoked_at__isnull=True,
                user__is_active=True,
            ).order_by("id").values_list("id", "user_id")[:limit]
        )
        bound = unchanged = skipped = 0
        if apply:
            for device_id, user_id in candidates:
                # User -> device is the existing revocation/verification order.
                # Re-check authority after acquiring the locks; a dry-run list
                # is never authorization to revive an intervening revocation.
                with transaction.atomic():
                    user = get_user_model().objects.select_for_update().get(pk=user_id)
                    device = CommunityChatDevice.objects.select_for_update().get(pk=device_id)
                    if (not user.is_active or device.user_id != user.pk
                            or device.status != DeviceBindingStatus.VERIFIED
                            or device.revoked_at is not None):
                        skipped += 1
                        continue
                    try:
                        changed = bind_verified_device(user.pk, device.public_key)
                    except MembershipAdapterError as exc:
                        raise CommandError(f"Binding failed at device {device_id}; resume before it") from exc
                    bound += int(changed)
                    unchanged += int(not changed)
        last = candidates[-1][0] if candidates else after
        self.stdout.write(
            f"mode={'apply' if apply else 'dry-run'} candidates={len(candidates)} "
            f"bound={bound} unchanged={unchanged} skipped={skipped} next_after_device_id={last}"
        )
