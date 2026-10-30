"""User-first admin mutations that retain durable Members cleanup wakes."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction


class MemberAudienceAuthorityAdminMixin:
    """Fence admin save/delete with the same User boundary as room authority."""

    @staticmethod
    def _owner_id(obj):
        return obj.pk if isinstance(obj, get_user_model()) else getattr(obj, "user_id", None)

    def _lock_owners(self, user_ids):
        users = list(get_user_model().objects.select_for_update().filter(
            pk__in={value for value in user_ids if value is not None},
        ).order_by("pk"))
        if self.model._meta.model_name in {"user", "slackdmmirrorgrant", "externalserviceconnection"}:
            from integrations.services.slack_member_membership_epoch import invalidate_memberships_locked

            for user in users:
                invalidate_memberships_locked(user.pk)
        return users

    @staticmethod
    def _wake(user_ids, reason):
        from integrations.services.community_bridge.member_channels import schedule_member_authority_change

        for user_id in sorted({value for value in user_ids if value is not None}):
            schedule_member_authority_change(user_id, reason=reason, reduction=True)

    def save_model(self, request, obj, form, change):
        previous = self.model.objects.filter(pk=obj.pk).first() if change else None
        owner_ids = {self._owner_id(obj), self._owner_id(previous) if previous else None}
        with transaction.atomic():
            self._lock_owners(owner_ids)
            if previous is not None:
                current = self.model.objects.select_for_update().filter(pk=obj.pk).first()
                if current is None or self._owner_id(current) != self._owner_id(previous):
                    raise ValidationError("Account ownership changed. Reload this form.")
                if self.model._meta.model_name == "externalserviceconnection" and current.provider == "slack":
                    from integrations.services.slack_member_membership_epoch import KEY

                    # The submitted JSON predates the authority lock. Keep the
                    # floor just advanced under that lock when saving the form.
                    current_cursor = current.sync_cursor if isinstance(current.sync_cursor, dict) else {}
                    if KEY in current_cursor:
                        submitted_cursor = obj.sync_cursor if isinstance(obj.sync_cursor, dict) else {}
                        obj.sync_cursor = {**submitted_cursor, KEY: current_cursor[KEY]}
            reason = f"admin_{self.model._meta.model_name}_change"
            self._wake(owner_ids, reason)
            super().save_model(request, obj, form, change)
            # Capture a newly saved Slack workspace too; ownership moves must
            # wake both the previous and new account's existing destinations.
            self._wake(owner_ids, reason)

    def delete_model(self, request, obj):
        with transaction.atomic():
            owner_ids = {self._owner_id(obj)}
            self._lock_owners(owner_ids)
            current = self.model.objects.select_for_update().get(pk=obj.pk)
            if self._owner_id(current) != self._owner_id(obj):
                raise ValidationError("Account ownership changed. Reload this form.")
            self._wake(owner_ids, f"admin_{self.model._meta.model_name}_delete")
            super().delete_model(request, current)

    def delete_queryset(self, request, queryset):
        with transaction.atomic():
            objects = list(queryset.order_by("pk"))
            owner_ids = {self._owner_id(obj) for obj in objects}
            self._lock_owners(owner_ids)
            locked = list(self.model.objects.select_for_update().filter(
                pk__in=[obj.pk for obj in objects],
            ).order_by("pk"))
            before = {obj.pk: self._owner_id(obj) for obj in objects}
            if any(before[obj.pk] != self._owner_id(obj) for obj in locked):
                raise ValidationError("Account ownership changed. Reload this selection.")
            self._wake(owner_ids, f"admin_{self.model._meta.model_name}_delete")
            super().delete_queryset(request, self.model.objects.filter(pk__in=[obj.pk for obj in locked]))
