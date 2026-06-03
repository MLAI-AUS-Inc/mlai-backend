import os

import requests
from django.contrib import admin, messages

from .models import (
    GenericHackathonAnnouncement,
    GenericHackathonResource,
    GenericHackathonSubmission,
    GenericHackathonTeam,
    WattTheHackSettings,
)

# Where the FastAPI eval gateway lives. Hardcoded because this is the only
# production target; if we ever need to point at a staging cluster, lift to
# an env var. See `Watt-The-Hack-Admin/eval_platform/api/gateway.py` for the
# `/admin/teams` endpoint contract.
WTH_EVAL_ADMIN_URL = "https://eval.eliascorp.org/admin/teams"

# 20s tolerates ingress + cold-path GKE Postgres comfortably without leaving
# the admin staring at a spinner for too long if something's really wrong.
_EVAL_PROVISION_TIMEOUT_S = 20


@admin.register(GenericHackathonTeam)
class GenericHackathonTeamAdmin(admin.ModelAdmin):
    list_display = ('team_name', 'team_id', 'hackathon', 'created_at', 'has_eval_token')
    list_filter = ('hackathon',)
    search_fields = ('team_name',)
    filter_horizontal = ('members',)
    actions = ['approve_teams_for_eval']

    def has_eval_token(self, obj):
        return bool(obj.eval_token)
    has_eval_token.boolean = True
    has_eval_token.short_description = "Eval Token"

    @admin.action(description="Approve selected teams for eval server")
    def approve_teams_for_eval(self, request, queryset):
        """Provision selected teams on the FastAPI eval cluster.

        Idempotent: teams that already have both credentials are skipped
        without an API call. Network errors on one team don't block the rest
        of the batch — each team is independent.
        """
        admin_token = os.environ.get("WTH_EVAL_ADMIN_TOKEN", "")
        if not admin_token:
            self.message_user(
                request,
                "WTH_EVAL_ADMIN_TOKEN is not set in the environment — cannot reach the eval cluster.",
                level=messages.ERROR,
            )
            return

        success_count = 0
        error_count = 0
        skipped_count = 0

        for team in queryset:
            # Idempotency gate: only re-call the gateway if at least one half
            # of the credential pair is missing. Reading-back teams with both
            # halves populated is the common case under retries.
            if team.eval_token and team.eval_team_uuid:
                skipped_count += 1
                continue

            try:
                response = requests.post(
                    WTH_EVAL_ADMIN_URL,
                    json={"name": team.team_name, "email": None},
                    headers={"X-Admin-Token": admin_token},
                    timeout=_EVAL_PROVISION_TIMEOUT_S,
                )
            except requests.RequestException as exc:
                # Network-level failure (DNS, timeout, connection refused).
                # Surface but continue — the next team is independent.
                error_count += 1
                self.message_user(
                    request,
                    f"Network error provisioning {team.team_name}: {exc}",
                    level=messages.ERROR,
                )
                continue

            if response.status_code == 409:
                # The eval cluster already has a team with this name. Likely a
                # half-rolled-back retry: Django thinks the team isn't
                # provisioned but the cluster does. Recovery is manual — see
                # /admin in the eval console.
                error_count += 1
                self.message_user(
                    request,
                    f"Conflict (409) for {team.team_name}: name already exists on the eval cluster. "
                    "Check eval.eliascorp.org/admin and reconcile by hand.",
                    level=messages.WARNING,
                )
                continue

            if not response.ok:
                error_count += 1
                # The gateway returns a `detail` field for 4xx; surface it
                # verbatim so the admin can act (e.g. a 422 means we sent a
                # malformed payload that needs code, not data, to fix).
                detail = ""
                try:
                    detail = response.json().get("detail", "")
                except ValueError:
                    detail = response.text[:200]
                self.message_user(
                    request,
                    f"Failed to provision {team.team_name}: HTTP {response.status_code} — {detail}",
                    level=messages.ERROR,
                )
                continue

            try:
                data = response.json()
            except ValueError as exc:
                error_count += 1
                self.message_user(
                    request,
                    f"Failed to parse eval response for {team.team_name}: {exc}",
                    level=messages.ERROR,
                )
                continue

            new_token = data.get("token")
            new_uuid = data.get("id")
            if not (new_token and new_uuid):
                error_count += 1
                self.message_user(
                    request,
                    f"Eval server returned an incomplete response for {team.team_name} "
                    f"(missing {'token' if not new_token else 'id'}).",
                    level=messages.ERROR,
                )
                continue

            team.eval_token = new_token
            team.eval_team_uuid = new_uuid
            team.save(update_fields=['eval_token', 'eval_team_uuid'])
            success_count += 1

        # Single summary message so the admin gets one line of feedback per
        # batch, on top of per-team errors above. Always emit it so the admin
        # sees the skipped count even when nothing new was provisioned.
        parts = []
        if success_count:
            parts.append(f"provisioned {success_count}")
        if skipped_count:
            parts.append(f"skipped {skipped_count} already provisioned")
        if error_count:
            parts.append(f"failed {error_count}")
        if parts:
            level = messages.SUCCESS if (success_count or skipped_count) and not error_count else messages.WARNING
            self.message_user(request, "Teams: " + ", ".join(parts) + ".", level=level)


@admin.register(GenericHackathonSubmission)
class GenericHackathonSubmissionAdmin(admin.ModelAdmin):
    list_display = ('title', 'team', 'hackathon', 'user', 'created_at')
    list_filter = ('hackathon', 'created_at')
    search_fields = ('title', 'summary', 'team__team_name', 'user__email')


@admin.register(GenericHackathonAnnouncement)
class GenericHackathonAnnouncementAdmin(admin.ModelAdmin):
    list_display = ('title', 'hackathon', 'author', 'created_at')
    list_filter = ('hackathon', 'created_at')
    search_fields = ('title', 'body')


@admin.register(GenericHackathonResource)
class GenericHackathonResourceAdmin(admin.ModelAdmin):
    list_display = ('title', 'hackathon', 'category', 'order')
    list_filter = ('hackathon', 'category')
    search_fields = ('title', 'summary', 'body')


@admin.register(WattTheHackSettings)
class WattTheHackSettingsAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        # Restrict adding new objects since it's a singleton
        return False if self.model.objects.count() > 0 else super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False
