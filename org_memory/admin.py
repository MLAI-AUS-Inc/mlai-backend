from django.contrib import admin
from django.utils import timezone

from .models import (
    AgentActionEvent,
    AgentActionProposal,
    ActorAssertionReceipt,
    OrganizationCapability,
    OrganizationCapabilityGrant,
    OrganizationIdentity,
    OrganizationMembership,
    OrganizationRole,
    OrganizationRoleAssignment,
    OrganizationSlackIdentity,
    OrganizationSlackWorkspace,
    ServicePrincipal,
    ServicePrincipalAuditEvent,
    ServicePrincipalCredential,
    MemoryConnectionConfiguration,
    MemoryConnectionHealthSnapshot,
    MemoryCostReservation,
    MemoryDailyCostLedger,
    MemoryDailyReconciliationReport,
    DriveDocumentArtifact,
    DriveDocumentExtraction,
    DriveDocumentArtifactVersion,
    DriveInventoryManifest,
    DriveMeeting,
    DriveMeetingArtifactLink,
    DriveReconciliationReport,
    DriveWatchChannel,
    MemoryConnectionState,
    MemoryPreviewStatus,
    MemoryProviderEnablement,
    MemoryAclSnapshot,
    MemoryChunk,
    MemoryChunkEmbedding,
    MemoryClaim,
    MemoryClaimLink,
    MemoryClaimStateEvent,
    MemoryConsolidationRun,
    MemoryCorrectionProposal,
    MemoryCurrentState,
    MemoryDeadLetter,
    MemoryDeletionRequest,
    MemoryDigest,
    MemoryDigestItem,
    MemoryDigestItemEvidence,
    MemoryEntityResolutionEvent,
    MemoryFeedback,
    MemoryPilotDeployment,
    MemoryPilotQueryAudit,
    MemoryOutboxEvent,
    MemoryPublication,
    MemoryPublicationEvent,
    MemoryProviderEventReceipt,
    GmailMailboxWatch,
    GmailScopedMessageArtifact,
    StructuredAggregateArtifact,
    NotionBlockArtifact,
    NotionPageArtifact,
    MemoryQueryLog,
    MemoryEntity,
    MemoryEvidence,
    MemoryExtractionRun,
    MemoryReviewItem,
    MemoryReviewStatus,
    MemoryScopeStatus,
    MemorySource,
    MemorySourceActionRequest,
    MemorySourceAuditEvent,
    MemorySourcePolicy,
    MemorySourcePreview,
    MemorySourceScope,
    MemorySourceVersion,
    MemorySummary,
    MemorySummaryClaim,
    MemorySummaryEvidence,
    MemorySyncRun,
    MemoryRuntimeLane,
    MemoryWorkItem,
    MemoryWorkerLease,
    PublicKnowledgeItem,
)


def _invalidate_source_configuration(configuration, *, user, event_type):
    from .kernel import revoke_configuration_sources

    previous = configuration.lifecycle_state
    configuration.previews.filter(is_current=True).update(
        is_current=False,
        status=MemoryPreviewStatus.STALE,
    )
    configuration.lifecycle_state = MemoryConnectionState.SCOPED
    configuration.approved_preview = None
    configuration.approved_by = None
    configuration.approved_at = None
    configuration.last_dry_run_at = None
    revoke_configuration_sources(
        configuration,
        reason=event_type,
    )
    configuration.save(
        update_fields=(
            "lifecycle_state",
            "approved_preview",
            "approved_by",
            "approved_at",
            "last_dry_run_at",
            "updated_at",
        )
    )
    MemorySourceAuditEvent.objects.create(
        organization=configuration.organization,
        configuration=configuration,
        actor_user=user,
        event_type=event_type,
        from_state=previous,
        to_state=configuration.lifecycle_state,
        metadata={"source": "django_admin"},
    )


@admin.register(MemoryProviderEnablement)
class MemoryProviderEnablementAdmin(admin.ModelAdmin):
    list_display = ("provider", "organization", "is_enabled", "approved_by", "approved_at", "updated_at")
    search_fields = ("provider", "organization__name", "organization__domain")
    list_filter = ("is_enabled", "provider", "organization")
    autocomplete_fields = ("organization", "approved_by")
    readonly_fields = ("created_at", "updated_at")

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            fields.extend(("organization", "provider"))
        return tuple(fields)

    def save_model(self, request, obj, form, change):
        if obj.is_enabled:
            obj.approved_by = request.user
            obj.approved_at = timezone.now()
        else:
            obj.approved_by = None
            obj.approved_at = None
        super().save_model(request, obj, form, change)


@admin.register(MemorySourcePolicy)
class MemorySourcePolicyAdmin(admin.ModelAdmin):
    list_display = (
        "policy_key",
        "provider",
        "organization",
        "classification",
        "authority_score",
        "volatility",
        "is_active",
        "reviewed_at",
    )
    search_fields = ("policy_key", "name", "provider", "organization__domain")
    list_filter = ("provider", "classification", "volatility", "is_active", "organization")
    autocomplete_fields = ("organization", "reviewed_by")
    readonly_fields = ("created_at", "updated_at")

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            fields.extend(("organization", "provider", "policy_key"))
        return tuple(fields)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        configuration_ids = set(obj.connection_configurations.values_list("pk", flat=True))
        configuration_ids.update(
            obj.source_scopes.values_list("configuration_id", flat=True)
        )
        for configuration in MemoryConnectionConfiguration.objects.filter(
            pk__in=configuration_ids
        ):
            _invalidate_source_configuration(
                configuration,
                user=request.user,
                event_type="policy_changed_in_admin",
            )


@admin.register(MemoryConnectionConfiguration)
class MemoryConnectionConfigurationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "provider",
        "organization",
        "lifecycle_state",
        "approved_by",
        "approved_at",
        "last_successful_sync_at",
        "next_scheduled_sync_at",
        "updated_at",
    )
    search_fields = ("id", "provider", "organization__name", "organization__domain")
    list_filter = ("provider", "lifecycle_state", "organization")
    autocomplete_fields = ("default_policy",)
    readonly_fields = (
        "id",
        "organization",
        "provider",
        "external_connection",
        "google_connection",
        "lifecycle_state",
        "state_before_pause",
        "approved_preview",
        "approved_by",
        "approved_at",
        "created_by",
        "last_discovered_at",
        "last_previewed_at",
        "last_dry_run_at",
        "last_backfill_requested_at",
        "last_sync_requested_at",
        "last_successful_sync_at",
        "sync_cursor",
        "sync_checkpoint",
        "next_scheduled_sync_at",
        "last_error",
        "deleted_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def save_model(self, request, obj, form, change):
        source_fields = {
            "default_policy",
            "default_classification",
            "allowed_memory_kinds",
            "historical_cutoff",
            "retention_policy",
            "configuration",
        }
        changed = bool(source_fields & set(form.changed_data))
        super().save_model(request, obj, form, change)
        if changed:
            _invalidate_source_configuration(
                obj,
                user=request.user,
                event_type="connection_configuration_changed_in_admin",
            )


@admin.register(MemorySourceScope)
class MemorySourceScopeAdmin(admin.ModelAdmin):
    list_display = (
        "external_id",
        "scope_type",
        "configuration",
        "selected",
        "status",
        "default_classification",
        "policy",
        "updated_at",
    )
    search_fields = ("external_id", "name", "configuration__organization__domain")
    list_filter = ("scope_type", "selected", "status", "default_classification")
    autocomplete_fields = ("policy",)
    readonly_fields = (
        "configuration",
        "metadata",
        "discovered_at",
        "last_seen_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def save_model(self, request, obj, form, change):
        obj.status = (
            MemoryScopeStatus.SELECTED
            if obj.selected
            else MemoryScopeStatus.EXCLUDED
        )
        super().save_model(request, obj, form, change)
        _invalidate_source_configuration(
            obj.configuration,
            user=request.user,
            event_type="source_scope_changed_in_admin",
        )


@admin.register(MemorySourcePreview)
class MemorySourcePreviewAdmin(admin.ModelAdmin):
    list_display = ("configuration", "version", "status", "is_current", "dry_run_completed_at", "created_at")
    search_fields = ("configuration__id", "configuration__organization__domain", "selection_fingerprint")
    list_filter = ("status", "is_current", "created_at")
    readonly_fields = tuple(field.name for field in MemorySourcePreview._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(MemorySourceActionRequest)
class MemorySourceActionRequestAdmin(admin.ModelAdmin):
    list_display = ("action", "status", "configuration", "requested_by", "request_id", "requested_at")
    search_fields = ("id", "configuration__id", "request_id", "idempotency_key")
    list_filter = ("action", "status", "requested_at")
    readonly_fields = tuple(field.name for field in MemorySourceActionRequest._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(MemorySourceAuditEvent)
class MemorySourceAuditEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "organization", "configuration", "actor_user", "request_id", "created_at")
    search_fields = ("event_type", "organization__domain", "configuration__id", "request_id")
    list_filter = ("event_type", "organization", "created_at")
    readonly_fields = tuple(field.name for field in MemorySourceAuditEvent._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(MemorySource)
class MemorySourceAdmin(admin.ModelAdmin):
    list_display = (
        "external_id",
        "source_type",
        "provider",
        "organization",
        "lifecycle_state",
        "current_version",
        "last_seen_at",
    )
    search_fields = (
        "external_id",
        "title",
        "external_account_id",
        "organization__domain",
    )
    list_filter = ("provider", "source_type", "lifecycle_state", "organization")
    readonly_fields = tuple(field.name for field in MemorySource._meta.fields)
    actions = ("revoke_access", "tombstone_sources")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Revoke access and deactivate current chunks")
    def revoke_access(self, request, queryset):
        from .kernel import revoke_source_access

        changed = 0
        for source in queryset:
            result = revoke_source_access(source, reason="django_admin_access_revocation")
            changed += result["sources_revoked"]
        self.message_user(request, f"Revoked access to {changed} source(s).")

    @admin.action(description="Tombstone sources and deactivate all chunks")
    def tombstone_sources(self, request, queryset):
        from .kernel import tombstone_source

        changed = 0
        for source in queryset:
            _deletion, result = tombstone_source(
                source,
                reason="django_admin_source_tombstone",
                requested_by=request.user,
                request_id=f"django-admin:{request.user.pk}",
            )
            changed += result.get("sources_tombstoned", 0)
        self.message_user(request, f"Tombstoned {changed} source(s).")


class ReadOnlyKernelAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MemoryProviderEventReceipt)
class MemoryProviderEventReceiptAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "provider",
        "event_type",
        "external_account_id",
        "external_scope_id",
        "scheduled_configuration_count",
        "received_at",
    )
    search_fields = (
        "receipt_key",
        "payload_hash",
        "external_account_id",
        "external_scope_id",
    )
    list_filter = ("provider", "event_type", "received_at")
    readonly_fields = tuple(field.name for field in MemoryProviderEventReceipt._meta.fields)


@admin.register(NotionPageArtifact)
class NotionPageArtifactAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "notion_page_id",
        "title",
        "configuration",
        "lifecycle_state",
        "source_updated_at",
        "last_seen_at",
    )
    search_fields = (
        "notion_page_id",
        "title",
        "configuration__organization__domain",
    )
    list_filter = ("lifecycle_state", "in_trash", "is_archived", "organization")
    readonly_fields = tuple(field.name for field in NotionPageArtifact._meta.fields)


@admin.register(NotionBlockArtifact)
class NotionBlockArtifactAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "notion_block_id",
        "page",
        "block_type",
        "ordinal",
        "depth",
        "source_updated_at",
    )
    search_fields = (
        "notion_block_id",
        "page__notion_page_id",
        "page__title",
    )
    list_filter = ("block_type", "in_trash", "is_archived")
    readonly_fields = tuple(field.name for field in NotionBlockArtifact._meta.fields)


@admin.register(GmailScopedMessageArtifact)
class GmailScopedMessageArtifactAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "gmail_message_id",
        "gmail_thread_id",
        "configuration",
        "lifecycle_state",
        "internal_date",
        "last_seen_at",
    )
    search_fields = (
        "gmail_message_id",
        "gmail_thread_id",
        "configuration__organization__domain",
    )
    list_filter = ("lifecycle_state", "organization")
    readonly_fields = tuple(field.name for field in GmailScopedMessageArtifact._meta.fields)


@admin.register(GmailMailboxWatch)
class GmailMailboxWatchAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "email_address",
        "configuration",
        "status",
        "expiration_at",
        "last_renewed_at",
        "last_notification_at",
    )
    search_fields = ("email_address", "topic_name", "configuration__organization__domain")
    list_filter = ("status", "expiration_at")
    readonly_fields = tuple(field.name for field in GmailMailboxWatch._meta.fields)


@admin.register(MemoryDailyReconciliationReport)
class MemoryDailyReconciliationReportAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "report_date",
        "organization",
        "status",
        "started_at",
        "completed_at",
    )
    search_fields = ("id", "organization__name", "organization__domain")
    list_filter = ("status", "report_date", "organization")
    readonly_fields = tuple(
        field.name for field in MemoryDailyReconciliationReport._meta.fields
    )


@admin.register(MemoryConnectionHealthSnapshot)
class MemoryConnectionHealthSnapshotAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "configuration",
        "provider",
        "health_status",
        "freshness_status",
        "schedule_status",
        "watch_status",
        "updated_at",
    )
    search_fields = ("configuration__id", "organization__domain")
    list_filter = (
        "provider",
        "health_status",
        "freshness_status",
        "schedule_status",
        "watch_status",
    )
    readonly_fields = tuple(
        field.name for field in MemoryConnectionHealthSnapshot._meta.fields
    )


@admin.register(MemoryDailyCostLedger)
class MemoryDailyCostLedgerAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "budget_date",
        "organization",
        "ceiling_aud",
        "reserved_aud",
        "consumed_aud",
    )
    search_fields = ("organization__name", "organization__domain")
    list_filter = ("budget_date", "organization")
    readonly_fields = tuple(field.name for field in MemoryDailyCostLedger._meta.fields)


@admin.register(MemoryCostReservation)
class MemoryCostReservationAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "work_item",
        "task_type",
        "status",
        "estimated_tokens",
        "estimated_cost_aud",
        "reserved_at",
    )
    search_fields = ("work_item__id", "organization__domain")
    list_filter = ("task_type", "status", "organization")
    readonly_fields = tuple(field.name for field in MemoryCostReservation._meta.fields)


@admin.register(StructuredAggregateArtifact)
class StructuredAggregateArtifactAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "provider",
        "metric_key",
        "name",
        "period_start",
        "value_text",
        "lifecycle_state",
        "stale_after",
    )
    search_fields = (
        "external_id",
        "metric_key",
        "name",
        "configuration__organization__domain",
    )
    list_filter = ("provider", "source_type", "lifecycle_state", "period_start")
    readonly_fields = tuple(field.name for field in StructuredAggregateArtifact._meta.fields)


@admin.register(MemorySourceVersion)
class MemorySourceVersionAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "version_key",
        "source",
        "classification",
        "is_current",
        "captured_at",
        "retired_at",
        "tombstoned_at",
    )
    search_fields = ("version_key", "content_hash", "source__external_id")
    list_filter = ("classification", "is_current", "captured_at")
    readonly_fields = tuple(field.name for field in MemorySourceVersion._meta.fields)


@admin.register(MemoryAclSnapshot)
class MemoryAclSnapshotAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "source_version",
        "is_accessible",
        "fingerprint",
        "captured_at",
        "revoked_at",
    )
    search_fields = ("source_version__source__external_id", "fingerprint")
    list_filter = ("is_accessible", "captured_at", "revoked_at")
    readonly_fields = tuple(field.name for field in MemoryAclSnapshot._meta.fields)


@admin.register(MemoryChunk)
class MemoryChunkAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "source_version",
        "ordinal",
        "chunk_kind",
        "classification",
        "token_count",
        "active_for_retrieval",
    )
    search_fields = ("source_version__source__external_id", "content_hash")
    list_filter = ("classification", "chunk_kind", "active_for_retrieval")
    readonly_fields = tuple(field.name for field in MemoryChunk._meta.fields)


@admin.register(MemoryChunkEmbedding)
class MemoryChunkEmbeddingAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "chunk",
        "organization",
        "model",
        "version",
        "dimensions",
        "is_current",
        "created_at",
    )
    search_fields = (
        "chunk__source_version__source__external_id",
        "vector_hash",
        "organization__domain",
    )
    list_filter = ("model", "version", "is_current", "organization")
    readonly_fields = tuple(field.name for field in MemoryChunkEmbedding._meta.fields)


@admin.register(MemoryExtractionRun)
class MemoryExtractionRunAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "source_version",
        "status",
        "model",
        "extractor_version",
        "completed_at",
    )
    search_fields = ("idempotency_key", "source_version__source__external_id", "provider_response_id")
    list_filter = ("status", "model", "extractor_version", "organization")
    readonly_fields = tuple(field.name for field in MemoryExtractionRun._meta.fields)


@admin.register(MemoryConsolidationRun)
class MemoryConsolidationRunAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "candidate_claim",
        "operation",
        "status",
        "matched_claim",
        "deterministic",
        "model",
        "completed_at",
    )
    search_fields = (
        "idempotency_key",
        "candidate_claim__statement",
        "matched_claim__statement",
        "reason",
        "provider_response_id",
    )
    list_filter = ("operation", "status", "deterministic", "model", "organization")
    readonly_fields = tuple(field.name for field in MemoryConsolidationRun._meta.fields)


@admin.register(MemoryEntity)
class MemoryEntityAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "canonical_name",
        "entity_type",
        "organization",
        "classification",
        "merged_into",
        "last_seen_at",
    )
    search_fields = ("canonical_name", "normalized_name", "resolved_key", "organization__domain")
    list_filter = ("entity_type", "classification", "organization")
    readonly_fields = tuple(field.name for field in MemoryEntity._meta.fields)


@admin.register(MemoryClaim)
class MemoryClaimAdmin(ReadOnlyKernelAdmin):
    list_display = ("statement", "kind", "epistemic_type", "status", "organization", "review_required")
    search_fields = ("statement", "predicate", "normalized_key", "organization__domain")
    list_filter = ("kind", "epistemic_type", "status", "classification", "review_required", "organization")
    readonly_fields = tuple(field.name for field in MemoryClaim._meta.fields)


@admin.register(MemoryEvidence)
class MemoryEvidenceAdmin(ReadOnlyKernelAdmin):
    list_display = ("claim", "evidence_role", "source", "chunk", "created_at")
    search_fields = ("quote", "quote_hash", "source__external_id")
    list_filter = ("evidence_role",)
    readonly_fields = tuple(field.name for field in MemoryEvidence._meta.fields)


@admin.register(MemoryClaimLink)
class MemoryClaimLinkAdmin(ReadOnlyKernelAdmin):
    list_display = ("from_claim", "relation_type", "to_claim", "confidence", "created_at")
    search_fields = ("from_claim__statement", "to_claim__statement")
    list_filter = ("relation_type",)
    readonly_fields = tuple(field.name for field in MemoryClaimLink._meta.fields)


@admin.register(MemoryClaimStateEvent)
class MemoryClaimStateEventAdmin(ReadOnlyKernelAdmin):
    list_display = ("claim", "from_status", "to_status", "reason", "created_at")
    search_fields = ("claim__statement", "reason")
    list_filter = ("from_status", "to_status")
    readonly_fields = tuple(field.name for field in MemoryClaimStateEvent._meta.fields)


@admin.register(MemoryCurrentState)
class MemoryCurrentStateAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "state_key",
        "scope_key",
        "claim",
        "organization",
        "is_stale",
        "has_conflict",
        "distinct_source_count",
        "refreshed_at",
    )
    search_fields = ("state_key", "scope_key", "claim__statement", "organization__domain")
    list_filter = ("is_stale", "has_conflict", "organization")
    readonly_fields = tuple(field.name for field in MemoryCurrentState._meta.fields)


@admin.register(MemorySummary)
class MemorySummaryAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "summary_type",
        "title",
        "organization",
        "status",
        "is_current",
        "window_end",
        "generated_at",
    )
    search_fields = ("title", "subject_key", "body", "organization__domain")
    list_filter = ("summary_type", "status", "is_current", "organization")
    readonly_fields = tuple(field.name for field in MemorySummary._meta.fields)


@admin.register(MemorySummaryClaim)
class MemorySummaryClaimAdmin(ReadOnlyKernelAdmin):
    list_display = ("summary", "ordinal", "claim", "created_at")
    search_fields = ("summary__title", "claim__statement")
    readonly_fields = tuple(field.name for field in MemorySummaryClaim._meta.fields)


@admin.register(MemorySummaryEvidence)
class MemorySummaryEvidenceAdmin(ReadOnlyKernelAdmin):
    list_display = ("summary", "evidence", "created_at")
    search_fields = ("summary__title", "evidence__claim__statement")
    readonly_fields = tuple(field.name for field in MemorySummaryEvidence._meta.fields)


@admin.register(MemoryDigest)
class MemoryDigestAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "digest_type",
        "digest_date",
        "organization",
        "status",
        "generated_at",
    )
    search_fields = ("title", "body", "organization__domain")
    list_filter = ("digest_type", "status", "organization")
    readonly_fields = tuple(field.name for field in MemoryDigest._meta.fields)


@admin.register(MemoryDigestItem)
class MemoryDigestItemAdmin(ReadOnlyKernelAdmin):
    list_display = ("digest", "ordinal", "claim", "summary", "created_at")
    search_fields = ("digest__title", "text", "claim__statement")
    readonly_fields = tuple(field.name for field in MemoryDigestItem._meta.fields)


@admin.register(MemoryDigestItemEvidence)
class MemoryDigestItemEvidenceAdmin(ReadOnlyKernelAdmin):
    list_display = ("item", "evidence", "created_at")
    search_fields = ("item__digest__title", "evidence__claim__statement")
    readonly_fields = tuple(
        field.name for field in MemoryDigestItemEvidence._meta.fields
    )


@admin.register(MemoryPublication)
class MemoryPublicationAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "public_key",
        "status",
        "organization",
        "proposed_by",
        "approved_by",
        "approved_at",
        "revoked_at",
        "created_at",
    )
    search_fields = (
        "id",
        "public_key",
        "proposed_title",
        "proposal_hash",
        "organization__domain",
    )
    list_filter = ("status", "organization", "created_at", "approved_at")
    readonly_fields = tuple(field.name for field in MemoryPublication._meta.fields)


@admin.register(AgentActionProposal)
class AgentActionProposalAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "action_type",
        "status",
        "risk_level",
        "organization",
        "requested_by",
        "approved_by",
        "executed_at",
        "reversed_at",
        "created_at",
    )
    search_fields = (
        "id",
        "idempotency_key",
        "input_hash",
        "organization__domain",
    )
    list_filter = (
        "action_type",
        "status",
        "risk_level",
        "target_system",
        "organization",
    )
    readonly_fields = tuple(field.name for field in AgentActionProposal._meta.fields)


@admin.register(AgentActionEvent)
class AgentActionEventAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "event_type",
        "proposal",
        "actor_user",
        "request_id",
        "created_at",
    )
    search_fields = (
        "proposal__id",
        "request_id",
        "payload_hash",
    )
    list_filter = ("event_type", "created_at")
    readonly_fields = tuple(field.name for field in AgentActionEvent._meta.fields)


@admin.register(MemoryPublicationEvent)
class MemoryPublicationEventAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "event_type",
        "publication",
        "actor_user",
        "payload_hash",
        "created_at",
    )
    search_fields = (
        "publication__id",
        "publication__public_key",
        "payload_hash",
    )
    list_filter = ("event_type", "created_at")
    readonly_fields = tuple(
        field.name for field in MemoryPublicationEvent._meta.fields
    )


@admin.register(PublicKnowledgeItem)
class PublicKnowledgeItemAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "public_key",
        "revision",
        "status",
        "organization",
        "published_at",
        "superseded_at",
        "revoked_at",
    )
    search_fields = (
        "id",
        "public_key",
        "title",
        "content_hash",
        "organization__domain",
    )
    list_filter = ("status", "organization", "published_at")
    readonly_fields = tuple(field.name for field in PublicKnowledgeItem._meta.fields)


@admin.register(MemoryEntityResolutionEvent)
class MemoryEntityResolutionEventAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "operation",
        "primary_entity",
        "secondary_entity",
        "organization",
        "actor_user",
        "created_at",
    )
    search_fields = (
        "primary_entity__canonical_name",
        "secondary_entity__canonical_name",
        "reason",
        "organization__domain",
    )
    list_filter = ("operation", "organization", "created_at")
    readonly_fields = tuple(field.name for field in MemoryEntityResolutionEvent._meta.fields)


@admin.register(MemoryCorrectionProposal)
class MemoryCorrectionProposalAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "original_claim",
        "replacement_claim",
        "status",
        "organization",
        "requested_by",
        "reviewed_by",
        "created_at",
    )
    search_fields = (
        "original_claim__statement",
        "replacement_claim__statement",
        "correction_text",
        "organization__domain",
    )
    list_filter = ("status", "organization", "created_at")
    readonly_fields = tuple(field.name for field in MemoryCorrectionProposal._meta.fields)


@admin.register(MemoryQueryLog)
class MemoryQueryLogAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "created_at",
        "organization",
        "requester_user",
        "status",
        "evidence_sufficiency",
        "confidence",
        "model_name",
        "latency_ms",
    )
    search_fields = (
        "request_id",
        "query",
        "query_hash",
        "requester_slack_id",
        "organization__domain",
    )
    list_filter = (
        "status",
        "evidence_sufficiency",
        "audience",
        "model_name",
        "organization",
    )
    readonly_fields = tuple(field.name for field in MemoryQueryLog._meta.fields)


@admin.register(MemoryFeedback)
class MemoryFeedbackAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "feedback_type",
        "organization",
        "query_log",
        "claim",
        "user",
        "correction_proposal",
        "created_at",
    )
    search_fields = (
        "request_id",
        "correction_text",
        "query_log__query",
        "claim__statement",
        "organization__domain",
    )
    list_filter = ("feedback_type", "organization", "created_at")
    readonly_fields = tuple(field.name for field in MemoryFeedback._meta.fields)


@admin.register(MemoryPilotQueryAudit)
class MemoryPilotQueryAuditAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "reviewed_at",
        "organization",
        "query_log",
        "reviewer",
        "rubric_version",
        "risk",
        "permission_leak",
        "public_admin_leak",
    )
    search_fields = (
        "idempotency_key",
        "batch_hash",
        "rubric_version",
        "organization__domain",
    )
    list_filter = (
        "risk",
        "permission_leak",
        "public_admin_leak",
        "rubric_version",
        "organization",
    )
    readonly_fields = tuple(
        field.name for field in MemoryPilotQueryAudit._meta.fields
    )


@admin.register(MemoryPilotDeployment)
class MemoryPilotDeploymentAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "created_at",
        "organization",
        "state",
        "approval_review_due_at",
        "allowlist_key_version",
        "approved_provider_count",
        "approved_source_scope_count",
    )
    search_fields = (
        "approval_manifest_hash",
        "stage_idempotency_key",
        "activation_idempotency_key",
        "organization__domain",
    )
    list_filter = (
        "state",
        "allowlist_key_version",
        "organization",
    )
    readonly_fields = tuple(
        field.name for field in MemoryPilotDeployment._meta.fields
    )



@admin.register(DriveInventoryManifest)
class DriveInventoryManifestAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "inventory_id",
        "organization",
        "configuration",
        "is_partial",
        "created_at",
    )
    search_fields = ("inventory_id", "organization__domain", "snapshot_hash")
    list_filter = ("is_partial", "organization")
    readonly_fields = tuple(field.name for field in DriveInventoryManifest._meta.fields)


@admin.register(DriveDocumentArtifact)
class DriveDocumentArtifactAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "name",
        "organization",
        "mime_type",
        "lifecycle_state",
        "transcript_candidate",
        "extraction_status",
        "source_modified_at",
    )
    search_fields = ("name", "file_id", "organization__domain")
    list_filter = (
        "lifecycle_state",
        "transcript_candidate",
        "extraction_status",
        "mime_type",
        "organization",
    )
    readonly_fields = tuple(field.name for field in DriveDocumentArtifact._meta.fields)


@admin.register(DriveDocumentArtifactVersion)
class DriveDocumentArtifactVersionAdmin(ReadOnlyKernelAdmin):
    list_display = ("artifact", "version_key", "is_current", "captured_at")
    search_fields = ("artifact__file_id", "artifact__name", "metadata_hash")
    list_filter = ("is_current",)
    readonly_fields = tuple(
        field.name for field in DriveDocumentArtifactVersion._meta.fields
    )


@admin.register(DriveDocumentExtraction)
class DriveDocumentExtractionAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "artifact_version",
        "status",
        "work_classification",
        "parser_name",
        "parser_version",
        "chunk_count",
        "extracted_at",
    )
    search_fields = ("artifact_version__artifact__file_id", "content_hash")
    list_filter = ("status", "work_classification", "parser_name")
    readonly_fields = tuple(field.name for field in DriveDocumentExtraction._meta.fields)


@admin.register(DriveMeeting)
class DriveMeetingAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "normalized_title",
        "organization",
        "occurred_at",
        "canonical_artifact",
    )
    search_fields = ("normalized_title", "identity_key", "organization__domain")
    list_filter = ("organization", "timezone_name")
    readonly_fields = tuple(field.name for field in DriveMeeting._meta.fields)


@admin.register(DriveMeetingArtifactLink)
class DriveMeetingArtifactLinkAdmin(ReadOnlyKernelAdmin):
    list_display = ("meeting", "artifact", "relation_type", "duplicate_of", "confidence")
    search_fields = ("meeting__normalized_title", "artifact__file_id", "artifact__name")
    list_filter = ("relation_type",)
    readonly_fields = tuple(field.name for field in DriveMeetingArtifactLink._meta.fields)


@admin.register(DriveReconciliationReport)
class DriveReconciliationReportAdmin(ReadOnlyKernelAdmin):
    list_display = ("sync_run", "configuration", "started_at", "completed_at")
    search_fields = ("sync_run__id", "configuration__id")
    readonly_fields = tuple(field.name for field in DriveReconciliationReport._meta.fields)


@admin.register(DriveWatchChannel)
class DriveWatchChannelAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "channel_id",
        "configuration",
        "status",
        "expiration_at",
        "last_notified_at",
    )
    search_fields = ("channel_id", "resource_id")
    list_filter = ("status",)
    readonly_fields = tuple(field.name for field in DriveWatchChannel._meta.fields)


@admin.register(MemoryReviewItem)
class MemoryReviewItemAdmin(admin.ModelAdmin):
    list_display = (
        "review_type",
        "severity",
        "status",
        "organization",
        "assigned_to",
        "due_at",
        "created_at",
    )
    search_fields = ("reason", "idempotency_key", "target_object_id", "organization__domain")
    list_filter = ("review_type", "severity", "status", "organization")
    autocomplete_fields = ("assigned_to",)
    readonly_fields = (
        "id",
        "organization",
        "review_type",
        "target_content_type",
        "target_object_id",
        "reason",
        "idempotency_key",
        "resolved_by",
        "resolved_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def save_model(self, request, obj, form, change):
        resolved_statuses = {
            MemoryReviewStatus.APPROVED,
            MemoryReviewStatus.REJECTED,
            MemoryReviewStatus.RESOLVED,
            MemoryReviewStatus.CANCELLED,
        }
        if obj.status in resolved_statuses and obj.resolved_at is None:
            obj.resolved_at = timezone.now()
            obj.resolved_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(MemoryOutboxEvent)
class MemoryOutboxEventAdmin(ReadOnlyKernelAdmin):
    list_display = ("event_type", "status", "source", "attempts", "available_at", "created_at")
    search_fields = ("idempotency_key", "source__external_id", "source_version__version_key")
    list_filter = ("event_type", "status", "created_at")
    readonly_fields = tuple(field.name for field in MemoryOutboxEvent._meta.fields)


@admin.register(MemoryWorkItem)
class MemoryWorkItemAdmin(ReadOnlyKernelAdmin):
    list_display = ("task_type", "status", "provider", "organization", "configuration", "attempts", "available_at")
    search_fields = ("id", "idempotency_key", "source__external_id")
    list_filter = ("task_type", "status", "provider", "organization")
    readonly_fields = tuple(field.name for field in MemoryWorkItem._meta.fields)


@admin.register(MemoryWorkerLease)
class MemoryWorkerLeaseAdmin(ReadOnlyKernelAdmin):
    list_display = ("work_item", "worker_id", "acquired_at", "heartbeat_at", "expires_at", "released_at")
    search_fields = ("worker_id", "lease_token", "work_item__id")
    list_filter = ("acquired_at", "expires_at", "released_at")
    readonly_fields = tuple(field.name for field in MemoryWorkerLease._meta.fields)


@admin.register(MemorySyncRun)
class MemorySyncRunAdmin(ReadOnlyKernelAdmin):
    list_display = (
        "action_type",
        "status",
        "trigger",
        "provider",
        "organization",
        "pages_completed",
        "records_processed",
        "created_at",
    )
    search_fields = ("id", "configuration__id", "action_request__id", "organization__domain")
    list_filter = ("action_type", "status", "trigger", "provider", "organization")
    readonly_fields = tuple(field.name for field in MemorySyncRun._meta.fields)


@admin.register(MemoryRuntimeLane)
class MemoryRuntimeLaneAdmin(ReadOnlyKernelAdmin):
    list_display = ("key", "scope", "organization", "provider", "blocked_until", "block_reason")
    search_fields = ("key", "provider", "organization__domain", "block_reason")
    list_filter = ("scope", "provider", "blocked_until")
    readonly_fields = tuple(field.name for field in MemoryRuntimeLane._meta.fields)


@admin.register(MemoryDeadLetter)
class MemoryDeadLetterAdmin(ReadOnlyKernelAdmin):
    list_display = ("work_item", "task_type", "organization", "attempts", "dead_at", "resolved_at")
    search_fields = ("work_item__id", "last_error", "organization__domain")
    list_filter = ("task_type", "organization", "dead_at", "resolved_at")
    readonly_fields = tuple(field.name for field in MemoryDeadLetter._meta.fields)


@admin.register(MemoryDeletionRequest)
class MemoryDeletionRequestAdmin(ReadOnlyKernelAdmin):
    list_display = ("target_type", "target_id", "status", "organization", "requested_by", "requested_at")
    search_fields = ("target_id", "idempotency_key", "request_id", "organization__domain")
    list_filter = ("target_type", "status", "hard_delete", "organization")
    readonly_fields = tuple(field.name for field in MemoryDeletionRequest._meta.fields)


class MembershipRoleAssignmentInline(admin.TabularInline):
    model = OrganizationRoleAssignment
    extra = 0
    autocomplete_fields = ("role", "assigned_by")


class MembershipCapabilityGrantInline(admin.TabularInline):
    model = OrganizationCapabilityGrant
    fk_name = "membership"
    extra = 0
    autocomplete_fields = ("capability", "granted_by")
    exclude = ("role",)


class RoleCapabilityGrantInline(admin.TabularInline):
    model = OrganizationCapabilityGrant
    fk_name = "role"
    extra = 0
    autocomplete_fields = ("capability", "granted_by")
    exclude = ("membership",)


@admin.register(OrganizationIdentity)
class OrganizationIdentityAdmin(admin.ModelAdmin):
    list_display = (
        "external_user_id",
        "provider",
        "external_tenant_id",
        "organization",
        "user",
        "is_active",
        "verified_at",
    )
    search_fields = (
        "external_user_id",
        "external_tenant_id",
        "email_at_link_time",
        "user__email",
        "organization__name",
        "organization__domain",
    )
    list_filter = ("provider", "is_active", "organization")
    autocomplete_fields = ("organization", "user")


@admin.register(OrganizationMembership)
class OrganizationMembershipAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "organization",
        "is_active",
        "joined_at",
        "ended_at",
        "source",
        "reviewed_by",
    )
    search_fields = ("user__email", "organization__name", "organization__domain")
    list_filter = ("is_active", "source", "organization")
    autocomplete_fields = ("organization", "user", "reviewed_by")
    inlines = (MembershipRoleAssignmentInline, MembershipCapabilityGrantInline)


@admin.register(OrganizationRole)
class OrganizationRoleAdmin(admin.ModelAdmin):
    list_display = ("slug", "name", "organization", "is_active", "updated_at")
    search_fields = ("slug", "name", "organization__name", "organization__domain")
    list_filter = ("is_active", "organization")
    autocomplete_fields = ("organization",)
    inlines = (RoleCapabilityGrantInline,)


@admin.register(OrganizationCapability)
class OrganizationCapabilityAdmin(admin.ModelAdmin):
    list_display = ("key", "name", "is_active", "updated_at")
    search_fields = ("key", "name", "description")
    list_filter = ("is_active",)


@admin.register(OrganizationRoleAssignment)
class OrganizationRoleAssignmentAdmin(admin.ModelAdmin):
    list_display = ("membership", "role", "valid_from", "valid_until", "assigned_by")
    search_fields = ("membership__user__email", "role__slug", "role__name")
    list_filter = ("role__organization", "role", "valid_from", "valid_until")
    autocomplete_fields = ("membership", "role", "assigned_by")


@admin.register(OrganizationCapabilityGrant)
class OrganizationCapabilityGrantAdmin(admin.ModelAdmin):
    list_display = (
        "capability",
        "effect",
        "membership",
        "role",
        "valid_from",
        "valid_until",
        "granted_by",
    )
    search_fields = (
        "capability__key",
        "membership__user__email",
        "role__slug",
    )
    list_filter = ("effect", "capability", "valid_from", "valid_until")
    autocomplete_fields = ("membership", "role", "capability", "granted_by")


@admin.register(ServicePrincipal)
class ServicePrincipalAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "is_active", "scopes", "allowed_surfaces", "updated_at")
    search_fields = ("name", "organization__name", "organization__domain")
    list_filter = ("is_active", "allowed_surfaces")


@admin.register(ServicePrincipalCredential)
class ServicePrincipalCredentialAdmin(admin.ModelAdmin):
    list_display = ("token_hint", "principal", "created_at", "expires_at", "revoked_at", "last_used_at")
    search_fields = ("token_hint", "principal__name")
    list_filter = ("revoked_at", "expires_at", "created_at")
    readonly_fields = (
        "id",
        "principal",
        "secret_hash",
        "token_hint",
        "rotated_from",
        "created_by",
        "expires_at",
        "revoked_at",
        "last_used_at",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ServicePrincipalAuditEvent)
class ServicePrincipalAuditEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "principal", "credential", "request_id", "created_at")
    search_fields = ("principal__name", "credential__token_hint", "request_id", "event_type")
    list_filter = ("event_type", "created_at")
    readonly_fields = (
        "principal",
        "credential",
        "event_type",
        "request_id",
        "remote_address",
        "metadata",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(OrganizationSlackWorkspace)
class OrganizationSlackWorkspaceAdmin(admin.ModelAdmin):
    list_display = ("slack_team_id", "name", "organization", "is_active", "updated_at")
    search_fields = ("slack_team_id", "name", "organization__name", "organization__domain")
    list_filter = ("is_active",)


@admin.register(OrganizationSlackIdentity)
class OrganizationSlackIdentityAdmin(admin.ModelAdmin):
    list_display = ("slack_user_id", "workspace", "user", "is_active", "updated_at")
    search_fields = ("slack_user_id", "workspace__slack_team_id", "user__email")
    list_filter = ("is_active", "workspace")
    readonly_fields = ("workspace", "slack_user_id", "user", "is_active", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ActorAssertionReceipt)
class ActorAssertionReceiptAdmin(admin.ModelAdmin):
    list_display = ("principal", "request_id", "event_id", "expires_at", "created_at")
    search_fields = ("principal__name", "request_id", "event_id", "nonce")
    readonly_fields = ("principal", "nonce", "request_id", "event_id", "expires_at", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
