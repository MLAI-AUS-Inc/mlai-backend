"""Read-only operational visibility; services enforce all state transitions."""

from django.contrib import admin

from .models import (AgentActionProposal, AgentArtifact, AgentAuditEvent, AgentCredential,
                     AgentEvent, AgentGrant, AgentInstallation, AgentResponsibility,
                     AgentSubscription, AgentTask)
from .policy import feature_enabled


class ReadOnlyAgentAdmin(admin.ModelAdmin):
    def has_module_permission(self, request):
        return feature_enabled() and super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        return feature_enabled() and super().has_view_permission(request, obj)

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields if field.name not in {"token_hash", "sealed_signing_key"})

    def get_exclude(self, request, obj=None):
        if self.model is AgentCredential:
            return ("token_hash",)
        return ("sealed_signing_key",) if self.model is AgentInstallation else ()

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


for model in (AgentInstallation, AgentGrant, AgentResponsibility, AgentTask,
              AgentCredential, AgentEvent, AgentSubscription, AgentArtifact,
              AgentActionProposal, AgentAuditEvent):
    admin.site.register(model, ReadOnlyAgentAdmin)
