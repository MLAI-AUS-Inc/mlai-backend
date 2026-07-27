import re
import uuid

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
from pgvector.django import HnswIndex, VectorField


class OrganizationIdentityProvider(models.TextChoices):
    SLACK = "slack", "Slack"
    GOOGLE = "google", "Google"
    MICROSOFT = "microsoft", "Microsoft"
    NOTION = "notion", "Notion"
    LINEAR = "linear", "Linear"
    XERO = "xero", "Xero"
    STRIPE = "stripe", "Stripe"
    LUMA = "luma", "Luma"
    OTHER = "other", "Other"


class CapabilityGrantEffect(models.TextChoices):
    ALLOW = "allow", "Allow"
    DENY = "deny", "Deny"


class MembershipSource(models.TextChoices):
    MANUAL = "manual", "Manual"
    REVIEWED_BACKFILL = "reviewed_backfill", "Reviewed backfill"


class MemoryClassification(models.TextChoices):
    INTERNAL = "internal", "Internal"
    COMMITTEE = "committee", "Committee"
    EXECUTIVE = "executive", "Executive"
    FINANCE = "finance", "Finance"
    PEOPLE_SENSITIVE = "people_sensitive", "People sensitive"
    NO_AGENT = "no_agent", "No agent"


class MemoryConnectionState(models.TextChoices):
    DRAFT = "draft", "Draft"
    SCOPED = "scoped", "Scoped"
    PREVIEWED = "previewed", "Previewed"
    DRY_RUN_READY = "dry_run_ready", "Dry run ready"
    APPROVED = "approved", "Approved"
    BACKFILL_PENDING = "backfill_pending", "Backfill pending"
    ACTIVE = "active", "Active"
    PAUSED = "paused", "Paused"
    DELETE_PENDING = "delete_pending", "Delete pending"
    DELETED = "deleted", "Deleted"
    ERROR = "error", "Error"


class MemoryScopeStatus(models.TextChoices):
    DISCOVERED = "discovered", "Discovered"
    SELECTED = "selected", "Selected"
    EXCLUDED = "excluded", "Excluded"
    REMOVED = "removed", "Removed"


class MemoryPreviewStatus(models.TextChoices):
    READY = "ready", "Ready"
    ERROR = "error", "Error"
    STALE = "stale", "Stale"


class MemoryActionType(models.TextChoices):
    DISCOVER = "discover", "Discover"
    PREVIEW = "preview", "Preview"
    DRY_RUN = "dry_run", "Dry run"
    BACKFILL = "backfill", "Backfill"
    SYNC = "sync", "Sync"
    REPROCESS = "reprocess", "Reprocess"
    REFRESH_PERMISSIONS = "refresh_permissions", "Refresh permissions"
    DELETE = "delete", "Delete"


class MemoryActionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class MemoryPolicyVolatility(models.TextChoices):
    STABLE = "stable", "Stable"
    NORMAL = "normal", "Normal"
    VOLATILE = "volatile", "Volatile"


class MemoryProvider(models.TextChoices):
    GOOGLE_DRIVE = "google_drive", "Google Drive"
    SLACK = "slack", "Slack"
    LINEAR = "linear", "Linear"
    NOTION = "notion", "Notion"
    GMAIL = "gmail", "Gmail"
    STRIPE = "stripe", "Stripe"
    XERO = "xero", "Xero"
    LUMA = "luma", "Luma"


class MemorySourceLifecycle(models.TextChoices):
    ACTIVE = "active", "Active"
    ACCESS_REVOKED = "access_revoked", "Access revoked"
    TOMBSTONED = "tombstoned", "Tombstoned"


class MemoryReviewType(models.TextChoices):
    CLAIM_ACTIVATION = "claim_activation", "Claim activation"
    CONTRADICTION = "contradiction", "Contradiction"
    CORRECTION = "correction", "Correction"
    SENSITIVITY = "sensitivity", "Sensitivity"
    STALE = "stale", "Stale"
    ENTITY_MERGE = "entity_merge", "Entity merge"
    PUBLICATION = "publication", "Publication"
    SOURCE_ACCESS = "source_access", "Source access"


class MemoryReviewSeverity(models.TextChoices):
    LOW = "low", "Low"
    NORMAL = "normal", "Normal"
    HIGH = "high", "High"
    CRITICAL = "critical", "Critical"


class MemoryReviewStatus(models.TextChoices):
    OPEN = "open", "Open"
    IN_REVIEW = "in_review", "In review"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    RESOLVED = "resolved", "Resolved"
    CANCELLED = "cancelled", "Cancelled"


class MemorySummaryType(models.TextChoices):
    THREAD = "thread", "Thread"
    DAY = "day", "Day"
    WEEK = "week", "Week"
    PROJECT = "project", "Project"


class MemoryDerivedArtifactStatus(models.TextChoices):
    READY = "ready", "Ready"
    EMPTY = "empty", "Empty"
    BLOCKED = "blocked", "Blocked"
    STALE = "stale", "Stale"


class MemoryDigestType(models.TextChoices):
    DAILY_OPEN_LOOPS = "daily_open_loops", "Daily open loops"
    WEEKLY_COMMITTEE = "weekly_committee", "Weekly committee"


class MemoryPublicationStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PENDING_REVIEW = "pending_review", "Pending review"
    PUBLISHED = "published", "Published"
    REJECTED = "rejected", "Rejected"
    REVOKED = "revoked", "Revoked"
    INVALIDATED = "invalidated", "Invalidated"


class MemoryPublicationEventType(models.TextChoices):
    CANDIDATE_CREATED = "candidate_created", "Candidate created"
    CANDIDATE_EDITED = "candidate_edited", "Candidate edited"
    SUBMITTED = "submitted", "Submitted"
    PUBLISHED = "published", "Published"
    REJECTED = "rejected", "Rejected"
    REVOKED = "revoked", "Revoked"
    INVALIDATED = "invalidated", "Invalidated"


class PublicKnowledgeStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    SUPERSEDED = "superseded", "Superseded"
    REVOKED = "revoked", "Revoked"


class AgentActionType(models.TextChoices):
    DRAFT_GMAIL = "draft_gmail", "Draft Gmail message"
    DRAFT_SLACK_POST = "draft_slack_post", "Draft Slack post"
    DRAFT_NOTION_UPDATE = "draft_notion_update", "Draft Notion update"
    CREATE_LINEAR_ISSUE = "create_linear_issue", "Create Linear issue"
    UPDATE_LINEAR_ISSUE = "update_linear_issue", "Update Linear issue"


class AgentActionRiskLevel(models.TextChoices):
    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"


class AgentActionStatus(models.TextChoices):
    PROPOSED = "proposed", "Proposed"
    AWAITING_APPROVAL = "awaiting_approval", "Awaiting approval"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    EXECUTING = "executing", "Executing"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    STALE = "stale", "Stale"
    REVERSING = "reversing", "Reversing"
    REVERSED = "reversed", "Reversed"
    CANCELLED = "cancelled", "Cancelled"


class AgentActionEventType(models.TextChoices):
    PROPOSED = "proposed", "Proposed"
    PRECONDITIONS_REFRESHED = "preconditions_refreshed", "Preconditions refreshed"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    EXECUTION_STARTED = "execution_started", "Execution started"
    EXECUTION_COMPLETED = "execution_completed", "Execution completed"
    EXECUTION_FAILED = "execution_failed", "Execution failed"
    APPROVAL_INVALIDATED = "approval_invalidated", "Approval invalidated"
    INGESTION_ENQUEUED = "ingestion_enqueued", "Ingestion enqueued"
    REVERSAL_STARTED = "reversal_started", "Reversal started"
    REVERSED = "reversed", "Reversed"
    REVERSAL_FAILED = "reversal_failed", "Reversal failed"
    CANCELLED = "cancelled", "Cancelled"


class MemoryEntityType(models.TextChoices):
    PERSON = "person", "Person"
    TEAM = "team", "Team"
    COMMITTEE = "committee", "Committee"
    PROJECT = "project", "Project"
    EVENT = "event", "Event"
    ORGANIZATION = "organization", "Organization"
    PARTNER = "partner", "Partner"
    SPONSOR = "sponsor", "Sponsor"
    CUSTOMER = "customer", "Customer"
    TOOL = "tool", "Tool"
    REPOSITORY = "repository", "Repository"
    CHANNEL = "channel", "Channel"
    DOCUMENT = "document", "Document"
    POLICY = "policy", "Policy"
    TASK = "task", "Task"
    METRIC = "metric", "Metric"
    VENUE = "venue", "Venue"
    SKILL = "skill", "Skill"


class MemoryClaimKind(models.TextChoices):
    FACT = "fact", "Fact"
    DECISION = "decision", "Decision"
    COMMITMENT = "commitment", "Commitment"
    TASK = "task", "Task"
    OPEN_LOOP = "open_loop", "Open loop"
    PROJECT_STATUS = "project_status", "Project status"
    EVENT = "event", "Event"
    POLICY = "policy", "Policy"
    PROCEDURE = "procedure", "Procedure"
    LESSON = "lesson", "Lesson"
    RELATIONSHIP = "relationship", "Relationship"
    PERSON_PROFILE = "person_profile", "Person profile"
    PREFERENCE = "preference", "Preference"
    RISK = "risk", "Risk"
    OPPORTUNITY = "opportunity", "Opportunity"
    METRIC = "metric", "Metric"
    QUESTION = "question", "Question"
    SUMMARY = "summary", "Summary"


class MemoryClaimStatus(models.TextChoices):
    CANDIDATE = "candidate", "Candidate"
    ACTIVE = "active", "Active"
    STALE = "stale", "Stale"
    SUPERSEDED = "superseded", "Superseded"
    CONTRADICTED = "contradicted", "Contradicted"
    RETRACTED = "retracted", "Retracted"
    ARCHIVED = "archived", "Archived"


class MemoryEpistemicType(models.TextChoices):
    PROPOSAL = "proposal", "Proposal"
    TESTIMONY = "testimony", "Testimony"
    DECISION = "decision", "Decision"
    SYSTEM_FACT = "system_fact", "System-of-record fact"
    OBSERVATION = "observation", "Observation"


class MemoryEvidenceRole(models.TextChoices):
    SUPPORTS = "supports", "Supports"
    CONTRADICTS = "contradicts", "Contradicts"
    CONTEXT = "context", "Context"


class MemoryClaimRelation(models.TextChoices):
    DUPLICATE_OF = "duplicate_of", "Duplicate of"
    SUPPORTS = "supports", "Supports"
    REFINES = "refines", "Refines"
    SUPERSEDES = "supersedes", "Supersedes"
    CONTRADICTS = "contradicts", "Contradicts"
    DERIVED_FROM = "derived_from", "Derived from"


class MemoryExtractionStatus(models.TextChoices):
    EXTRACTED = "extracted", "Extracted"
    NO_MEMORY = "no_memory", "No durable memory"
    QUARANTINED = "quarantined", "Quarantined"
    REJECTED = "rejected", "Rejected"


class MemoryConsolidationOperation(models.TextChoices):
    NEW = "new", "New"
    DUPLICATE = "duplicate", "Duplicate"
    SUPPORTS = "supports", "Supports"
    REFINES = "refines", "Refines"
    SUPERSEDES = "supersedes", "Supersedes"
    CONTRADICTS = "contradicts", "Contradicts"
    IGNORE = "ignore", "Ignore"


class MemoryConsolidationStatus(models.TextChoices):
    APPLIED = "applied", "Applied"
    REVIEW_REQUIRED = "review_required", "Review required"
    IGNORED = "ignored", "Ignored"
    QUARANTINED = "quarantined", "Quarantined"


class MemoryEntityResolutionOperation(models.TextChoices):
    MERGE = "merge", "Merge"
    SPLIT = "split", "Split"


class MemoryCorrectionStatus(models.TextChoices):
    PROPOSED = "proposed", "Proposed"
    APPLIED = "applied", "Applied"
    REJECTED = "rejected", "Rejected"


class MemoryQueryMode(models.TextChoices):
    CURRENT_STATE = "current_state", "Current state"
    HISTORICAL_AS_OF = "historical_as_of", "Historical as of"
    TIMELINE = "timeline", "Timeline"
    EVIDENCE_LOOKUP = "evidence_lookup", "Evidence lookup"
    OPEN_LOOPS = "open_loops", "Open loops"
    GLOBAL_SUMMARY = "global_summary", "Global summary"
    PERSON_OR_EXPERT = "person_or_expert", "Person or expert"
    RELATIONSHIP = "relationship", "Relationship"
    METRIC = "metric", "Metric"
    ACTION_PRECONDITION = "action_precondition", "Action precondition"


class MemoryQueryStatus(models.TextChoices):
    ANSWERED = "answered", "Answered"
    ABSTAINED = "abstained", "Abstained"
    SEARCH_ONLY = "search_only", "Search only"
    FAILED = "failed", "Failed"


class MemoryEvidenceSufficiency(models.TextChoices):
    SUFFICIENT = "sufficient", "Sufficient"
    PARTIAL = "partial", "Partial"
    INSUFFICIENT = "insufficient", "Insufficient"


class MemoryFeedbackType(models.TextChoices):
    RELEVANT = "relevant", "Relevant"
    IRRELEVANT = "irrelevant", "Irrelevant"
    CORRECT = "correct", "Correct"
    INCORRECT = "incorrect", "Incorrect"
    STALE = "stale", "Stale"
    MISSING = "missing", "Missing"
    HARMFUL = "harmful", "Harmful"


class MemoryPilotAuditRisk(models.TextChoices):
    STANDARD = "standard", "Standard"
    HIGH = "high", "High"


class MemoryPilotDeploymentState(models.TextChoices):
    STAGED = "staged", "Staged"
    ACTIVE = "active", "Active"
    SUSPENDED = "suspended", "Suspended"


class MemoryPilotSuspensionReason(models.TextChoices):
    MANUAL_STOP = "manual_stop", "Manual stop"
    SUSPECTED_LEAK = "suspected_leak", "Suspected leak"
    APPROVAL_REVOKED = "approval_revoked", "Approval revoked"
    SCOPE_CHANGED = "scope_changed", "Scope changed"
    CREDENTIAL_ROTATION = "credential_rotation", "Credential rotation"
    PILOT_COMPLETE = "pilot_complete", "Pilot complete"
    SUPERSEDED = "superseded", "Superseded"


class MemorySelectorShadowRunStatus(models.TextChoices):
    BLOCKED = "blocked", "Blocked"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class MemoryOutboxEventType(models.TextChoices):
    SOURCE_VERSION_CAPTURED = "source_version.captured", "Source version captured"
    SOURCE_ACCESS_REVOKED = "source.access_revoked", "Source access revoked"
    SOURCE_TOMBSTONED = "source.tombstoned", "Source tombstoned"


class MemoryOutboxStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PUBLISHED = "published", "Published"
    FAILED = "failed", "Failed"


class MemoryWorkTaskType(models.TextChoices):
    INGEST = "ingest", "Ingest"
    CHUNK = "chunk", "Chunk"
    EMBED = "embed", "Embed"
    EXTRACT = "extract", "Extract"
    CONSOLIDATE = "consolidate", "Consolidate"
    SUMMARIZE = "summarize", "Summarize"
    PUBLIC_CANDIDATE = "public_candidate", "Public candidate"
    RECONCILE = "reconcile", "Reconcile"
    REFRESH_PERMISSIONS = "refresh_permissions", "Refresh permissions"
    DELETE = "delete", "Delete"


class DriveArtifactState(models.TextChoices):
    ACTIVE = "active", "Active"
    TRASHED = "trashed", "Trashed"
    ACCESS_LOST = "access_lost", "Access lost"
    REMOVED = "removed", "Removed"


class DriveExtractionStatus(models.TextChoices):
    METADATA_ONLY = "metadata_only", "Metadata only"
    READY_FOR_PARSING = "ready_for_parsing", "Ready for parsing"
    UNSUPPORTED = "unsupported", "Unsupported"
    EXTRACTED = "extracted", "Extracted"
    DUPLICATE = "duplicate", "Duplicate suppressed"
    FAILED = "failed", "Failed"


class DriveWorkClassification(models.TextChoices):
    NONE = "", "None"
    NEEDS_OCR = "needs_ocr", "Needs OCR"
    NEEDS_TRANSCRIPTION = "needs_transcription", "Needs transcription"
    UNSUPPORTED_FORMAT = "unsupported_format", "Unsupported format"
    DOWNLOAD_RESTRICTED = "download_restricted", "Download restricted"
    SHORTCUT = "shortcut", "Shortcut not followed"
    NOT_TRANSCRIPT = "not_transcript_candidate", "Not a transcript candidate"
    DUPLICATE_SUPPRESSED = "duplicate_suppressed", "Duplicate suppressed"


class DriveMeetingRelation(models.TextChoices):
    CANONICAL = "canonical", "Canonical"
    SAME_MEETING_AS = "same_meeting_as", "Same meeting as"
    COPIED_FROM = "copied_from", "Copied from"
    DERIVED_FROM = "derived_from", "Derived from"


class DriveWatchStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    EXPIRED = "expired", "Expired"
    STOPPED = "stopped", "Stopped"


class NotionArtifactState(models.TextChoices):
    ACTIVE = "active", "Active"
    TRASHED = "trashed", "Trashed"
    ACCESS_LOST = "access_lost", "Access lost"


class GmailScopedArtifactState(models.TextChoices):
    ACTIVE = "active", "Active"
    LABEL_REMOVED = "label_removed", "Label removed"
    DELETED = "deleted", "Deleted"
    ACCESS_LOST = "access_lost", "Access lost"


class GmailWatchStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    EXPIRED = "expired", "Expired"
    ERROR = "error", "Error"
    STOPPED = "stopped", "Stopped"


class StructuredAggregateState(models.TextChoices):
    ACTIVE = "active", "Active"
    REMOVED = "removed", "Removed"
    ACCESS_LOST = "access_lost", "Access lost"


class MemoryWorkStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    DEAD = "dead", "Dead"
    CANCELLED = "cancelled", "Cancelled"


class MemorySyncRunStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class MemorySyncRunTrigger(models.TextChoices):
    MANUAL = "manual", "Manual"
    SCHEDULED = "scheduled", "Scheduled"


class MemoryDailyReconciliationStatus(models.TextChoices):
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    DEGRADED = "degraded", "Degraded"
    FAILED = "failed", "Failed"


class MemoryConnectionHealthStatus(models.TextChoices):
    HEALTHY = "healthy", "Healthy"
    SYNCING = "syncing", "Syncing"
    STALE = "stale", "Stale"
    ERROR = "error", "Error"


class MemoryCostReservationStatus(models.TextChoices):
    RESERVED = "reserved", "Reserved"
    CONSUMED = "consumed", "Consumed"
    RELEASED = "released", "Released"


class MemoryRuntimeLaneScope(models.TextChoices):
    ORGANIZATION = "organization", "Organization"
    PROVIDER = "provider", "Provider"


class MemoryDeletionTargetType(models.TextChoices):
    ORGANIZATION = "organization", "Organization"
    CONNECTION = "connection", "Connection"
    SCOPE = "scope", "Scope"
    SOURCE = "source", "Source"
    PROVIDER_PRINCIPAL = "provider_principal", "Provider principal"


class MemoryDeletionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class OrganizationIdentity(models.Model):
    """A verified external identity scoped to one provider tenant and organisation."""

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="external_identities",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="organization_identities",
    )
    provider = models.CharField(
        max_length=32,
        choices=OrganizationIdentityProvider.choices,
        db_index=True,
    )
    external_tenant_id = models.CharField(max_length=255)
    external_user_id = models.CharField(max_length=255)
    email_at_link_time = models.EmailField(blank=True, default="")
    verified_at = models.DateTimeField(null=True, blank=True, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=(
                    "organization",
                    "provider",
                    "external_tenant_id",
                    "external_user_id",
                ),
                name="orgmem_external_identity_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "provider", "external_tenant_id", "user"),
                condition=Q(is_active=True, user__isnull=False),
                name="orgmem_active_tenant_user_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("provider", "external_tenant_id", "external_user_id"),
                name="orgmem_ext_identity_lookup",
            ),
        ]
        ordering = ("organization", "provider", "external_tenant_id", "external_user_id")

    def __str__(self):
        return f"{self.provider}:{self.external_tenant_id}:{self.external_user_id}"

    @property
    def is_verified(self) -> bool:
        return bool(self.is_active and self.verified_at and self.user_id)


class OrganizationMembership(models.Model):
    """The canonical human membership used by organisational-memory authorization."""

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organization_memory_memberships",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    joined_at = models.DateTimeField(default=timezone.now, db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True, db_index=True)
    source = models.CharField(
        max_length=32,
        choices=MembershipSource.choices,
        default=MembershipSource.MANUAL,
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_org_memory_memberships",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "user"),
                name="orgmem_organization_member_uniq",
            ),
            models.CheckConstraint(
                check=Q(ended_at__isnull=True) | Q(ended_at__gt=models.F("joined_at")),
                name="orgmem_membership_dates_valid",
            ),
        ]
        ordering = ("organization", "user")

    def __str__(self):
        return f"{self.organization.domain}: {self.user.email}"

    def is_effective_at(self, at=None) -> bool:
        at = at or timezone.now()
        return bool(
            self.is_active
            and self.user.is_active
            and self.joined_at <= at
            and (self.ended_at is None or self.ended_at > at)
        )


class OrganizationRole(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_roles",
    )
    slug = models.SlugField(max_length=64)
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "slug"),
                name="orgmem_organization_role_uniq",
            ),
        ]
        ordering = ("organization", "slug")

    def __str__(self):
        return f"{self.organization.domain}: {self.name}"


class OrganizationCapability(models.Model):
    key = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("key",)

    def __str__(self):
        return self.key


class OrganizationRoleAssignment(models.Model):
    membership = models.ForeignKey(
        OrganizationMembership,
        on_delete=models.CASCADE,
        related_name="role_assignments",
    )
    role = models.ForeignKey(
        OrganizationRole,
        on_delete=models.CASCADE,
        related_name="assignments",
    )
    valid_from = models.DateTimeField(default=timezone.now, db_index=True)
    valid_until = models.DateTimeField(null=True, blank=True, db_index=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_org_memory_roles",
    )
    reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=Q(valid_until__isnull=True) | Q(valid_until__gt=models.F("valid_from")),
                name="orgmem_role_assignment_dates",
            ),
        ]
        indexes = [
            models.Index(
                fields=("membership", "valid_from", "valid_until"),
                name="orgmem_role_assignment_time",
            ),
        ]
        ordering = ("membership", "role", "-valid_from")

    def __str__(self):
        return f"{self.membership} -> {self.role.slug}"

    def clean(self):
        if (
            self.membership_id
            and self.role_id
            and self.membership.organization_id != self.role.organization_id
        ):
            raise ValidationError("Membership and role must belong to the same organisation.")


class OrganizationCapabilityGrant(models.Model):
    membership = models.ForeignKey(
        OrganizationMembership,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="capability_grants",
    )
    role = models.ForeignKey(
        OrganizationRole,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="capability_grants",
    )
    capability = models.ForeignKey(
        OrganizationCapability,
        on_delete=models.PROTECT,
        related_name="grants",
    )
    effect = models.CharField(
        max_length=8,
        choices=CapabilityGrantEffect.choices,
        default=CapabilityGrantEffect.ALLOW,
    )
    valid_from = models.DateTimeField(default=timezone.now, db_index=True)
    valid_until = models.DateTimeField(null=True, blank=True, db_index=True)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="granted_org_memory_capabilities",
    )
    reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(Q(membership__isnull=False, role__isnull=True)
                       | Q(membership__isnull=True, role__isnull=False)),
                name="orgmem_grant_one_subject",
            ),
            models.CheckConstraint(
                check=Q(valid_until__isnull=True) | Q(valid_until__gt=models.F("valid_from")),
                name="orgmem_capability_grant_dates",
            ),
        ]
        indexes = [
            models.Index(
                fields=("membership", "valid_from", "valid_until"),
                name="orgmem_member_grant_time",
            ),
            models.Index(
                fields=("role", "valid_from", "valid_until"),
                name="orgmem_role_grant_time",
            ),
        ]
        ordering = ("capability", "effect", "-valid_from")

    def __str__(self):
        subject = self.membership or self.role
        return f"{subject}: {self.effect} {self.capability.key}"

    def clean(self):
        if bool(self.membership_id) == bool(self.role_id):
            raise ValidationError("Exactly one membership or role is required.")


class MemoryProviderEnablement(models.Model):
    """Per-organisation feature flag beneath the deployment provider allowlist."""

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_provider_enablements",
    )
    provider = models.CharField(
        max_length=32,
        choices=MemoryProvider.choices,
        db_index=True,
    )
    is_enabled = models.BooleanField(default=False, db_index=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_memory_provider_enablements",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "provider"),
                name="orgmem_provider_enablement_uniq",
            ),
            models.CheckConstraint(
                check=(
                    Q(is_enabled=False)
                    | Q(approved_by__isnull=False, approved_at__isnull=False)
                ),
                name="orgmem_enabled_provider_approved",
            ),
        ]
        ordering = ("organization", "provider")

    def __str__(self):
        return f"{self.organization.domain}: {self.provider}"


class MemorySourcePolicy(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_source_policies",
    )
    provider = models.CharField(
        max_length=32,
        choices=MemoryProvider.choices,
        db_index=True,
    )
    policy_key = models.SlugField(max_length=64)
    name = models.CharField(max_length=128)
    scope_type = models.CharField(max_length=32, blank=True, default="")
    selector = models.JSONField(default=dict, blank=True)
    classification = models.CharField(
        max_length=32,
        choices=MemoryClassification.choices,
        default=MemoryClassification.INTERNAL,
        db_index=True,
    )
    authority_score = models.FloatField(default=0.5)
    volatility = models.CharField(
        max_length=16,
        choices=MemoryPolicyVolatility.choices,
        default=MemoryPolicyVolatility.NORMAL,
    )
    stale_after_seconds = models.PositiveIntegerField(default=86400)
    allowed_memory_kinds = models.JSONField(default=list, blank=True)
    auto_activation_rules = models.JSONField(default=dict, blank=True)
    review_rules = models.JSONField(default=dict, blank=True)
    retention_policy = models.JSONField(default=dict, blank=True)
    historical_cutoff = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_memory_source_policies",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "provider", "policy_key"),
                name="orgmem_source_policy_uniq",
            ),
            models.CheckConstraint(
                check=Q(authority_score__gte=0.0) & Q(authority_score__lte=1.0),
                name="orgmem_policy_authority_range",
            ),
        ]
        ordering = ("organization", "provider", "policy_key")

    def __str__(self):
        return f"{self.organization.domain}: {self.provider}/{self.policy_key}"


class MemoryConnectionConfiguration(models.Model):
    """Durable organisational-memory settings layered over an OAuth connection."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_connection_configurations",
    )
    provider = models.CharField(
        max_length=32,
        choices=MemoryProvider.choices,
        db_index=True,
    )
    external_connection = models.OneToOneField(
        "integrations.ExternalServiceConnection",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="memory_configuration",
    )
    google_connection = models.OneToOneField(
        "integrations.GoogleConnection",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="memory_configuration",
    )
    default_policy = models.ForeignKey(
        MemorySourcePolicy,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="connection_configurations",
    )
    lifecycle_state = models.CharField(
        max_length=32,
        choices=MemoryConnectionState.choices,
        default=MemoryConnectionState.DRAFT,
        db_index=True,
    )
    state_before_pause = models.CharField(max_length=32, blank=True, default="")
    default_classification = models.CharField(
        max_length=32,
        choices=MemoryClassification.choices,
        default=MemoryClassification.INTERNAL,
    )
    allowed_memory_kinds = models.JSONField(default=list, blank=True)
    historical_cutoff = models.DateTimeField(null=True, blank=True)
    retention_policy = models.JSONField(default=dict, blank=True)
    configuration = models.JSONField(default=dict, blank=True)
    approved_preview = models.ForeignKey(
        "MemorySourcePreview",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_for_configurations",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_memory_connections",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_memory_connections",
    )
    last_discovered_at = models.DateTimeField(null=True, blank=True)
    last_previewed_at = models.DateTimeField(null=True, blank=True)
    last_dry_run_at = models.DateTimeField(null=True, blank=True)
    last_backfill_requested_at = models.DateTimeField(null=True, blank=True)
    last_sync_requested_at = models.DateTimeField(null=True, blank=True)
    last_successful_sync_at = models.DateTimeField(null=True, blank=True)
    sync_cursor = models.TextField(blank=True, default="")
    sync_checkpoint = models.JSONField(default=dict, blank=True)
    next_scheduled_sync_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_error = models.TextField(blank=True, default="")
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    Q(external_connection__isnull=False, google_connection__isnull=True)
                    | Q(external_connection__isnull=True, google_connection__isnull=False)
                ),
                name="orgmem_config_one_connection",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "provider", "lifecycle_state"),
                name="orgmem_cfg_org_prov_state",
            ),
        ]
        ordering = ("organization", "provider", "created_at")

    def __str__(self):
        return f"{self.organization.domain}: {self.provider}/{self.pk}"

    @property
    def connection(self):
        return self.external_connection or self.google_connection

    def clean(self):
        if bool(self.external_connection_id) == bool(self.google_connection_id):
            raise ValidationError("Exactly one external or Google connection is required.")
        if self.external_connection_id:
            if self.external_connection.organization_id != self.organization_id:
                raise ValidationError("External connection must belong to the same organisation.")
            if self.external_connection.provider != self.provider:
                raise ValidationError("External connection provider does not match.")
        if self.google_connection_id and self.provider != "gmail":
            raise ValidationError("Google connections may only configure Gmail memory.")
        if self.default_policy_id and (
            self.default_policy.organization_id != self.organization_id
            or self.default_policy.provider != self.provider
        ):
            raise ValidationError("Default policy must match the organisation and provider.")


class MemorySourceScope(models.Model):
    configuration = models.ForeignKey(
        MemoryConnectionConfiguration,
        on_delete=models.CASCADE,
        related_name="source_scopes",
    )
    scope_type = models.CharField(max_length=32)
    external_id = models.CharField(max_length=512)
    name = models.CharField(max_length=512, blank=True, default="")
    canonical_url = models.URLField(max_length=1024, blank=True, default="")
    selected = models.BooleanField(default=False, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=MemoryScopeStatus.choices,
        default=MemoryScopeStatus.DISCOVERED,
        db_index=True,
    )
    default_classification = models.CharField(
        max_length=32,
        choices=MemoryClassification.choices,
        default=MemoryClassification.INTERNAL,
    )
    policy = models.ForeignKey(
        MemorySourcePolicy,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="source_scopes",
    )
    metadata = models.JSONField(default=dict, blank=True)
    discovered_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("configuration", "scope_type", "external_id"),
                name="orgmem_config_scope_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("configuration", "selected", "status"),
                name="orgmem_scope_config_selected",
            ),
        ]
        ordering = ("scope_type", "name", "external_id")

    def __str__(self):
        return f"{self.configuration_id}: {self.scope_type}/{self.external_id}"

    def clean(self):
        if self.policy_id and (
            self.policy.organization_id != self.configuration.organization_id
            or self.policy.provider != self.configuration.provider
        ):
            raise ValidationError("Scope policy must match the connection organisation and provider.")


class MemorySourcePreview(models.Model):
    configuration = models.ForeignKey(
        MemoryConnectionConfiguration,
        on_delete=models.CASCADE,
        related_name="previews",
    )
    version = models.PositiveIntegerField()
    status = models.CharField(
        max_length=16,
        choices=MemoryPreviewStatus.choices,
        default=MemoryPreviewStatus.READY,
        db_index=True,
    )
    is_current = models.BooleanField(default=True, db_index=True)
    selection_fingerprint = models.CharField(max_length=64, db_index=True)
    selection_snapshot = models.JSONField(default=list, blank=True)
    policy_snapshot = models.JSONField(default=dict, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    dry_run_summary = models.JSONField(default=dict, blank=True)
    dry_run_completed_at = models.DateTimeField(null=True, blank=True)
    dry_run_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dry_run_memory_source_previews",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_memory_source_previews",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("configuration", "version"),
                name="orgmem_config_preview_version_uniq",
            ),
            models.UniqueConstraint(
                fields=("configuration",),
                condition=Q(is_current=True),
                name="orgmem_config_current_preview_uniq",
            ),
        ]
        ordering = ("configuration", "-version")

    def __str__(self):
        return f"{self.configuration_id}: preview {self.version}"


class MemorySourceActionRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    configuration = models.ForeignKey(
        MemoryConnectionConfiguration,
        on_delete=models.CASCADE,
        related_name="action_requests",
    )
    action = models.CharField(max_length=32, choices=MemoryActionType.choices, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=MemoryActionStatus.choices,
        default=MemoryActionStatus.PENDING,
        db_index=True,
    )
    idempotency_key = models.CharField(max_length=128, null=True, blank=True)
    scope_external_ids = models.JSONField(default=list, blank=True)
    parameters = models.JSONField(default=dict, blank=True)
    result_summary = models.JSONField(default=dict, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_memory_source_actions",
    )
    request_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    requested_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("configuration", "idempotency_key"),
                condition=Q(idempotency_key__isnull=False),
                name="orgmem_action_idempotency_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("configuration", "status", "requested_at"),
                name="orgmem_action_config_status",
            ),
        ]
        ordering = ("-requested_at",)

    def __str__(self):
        return f"{self.configuration_id}: {self.action}/{self.status}"


class MemorySyncRun(models.Model):
    """One resumable, cursor-safe execution of a connection action."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_sync_runs",
    )
    configuration = models.ForeignKey(
        MemoryConnectionConfiguration,
        on_delete=models.CASCADE,
        related_name="sync_runs",
    )
    action_request = models.OneToOneField(
        MemorySourceActionRequest,
        on_delete=models.CASCADE,
        related_name="sync_run",
    )
    provider = models.CharField(max_length=32, choices=MemoryProvider.choices, db_index=True)
    action_type = models.CharField(max_length=32, choices=MemoryActionType.choices)
    trigger = models.CharField(
        max_length=16,
        choices=MemorySyncRunTrigger.choices,
        default=MemorySyncRunTrigger.MANUAL,
    )
    status = models.CharField(
        max_length=16,
        choices=MemorySyncRunStatus.choices,
        default=MemorySyncRunStatus.PENDING,
        db_index=True,
    )
    cursor_before = models.TextField(blank=True, default="")
    cursor_after = models.TextField(blank=True, default="")
    checkpoint_before = models.JSONField(default=dict, blank=True)
    checkpoint_after = models.JSONField(default=dict, blank=True)
    pages_completed = models.PositiveIntegerField(default=0)
    records_processed = models.PositiveIntegerField(default=0)
    removals_processed = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("configuration",),
                condition=Q(status__in=(MemorySyncRunStatus.PENDING, MemorySyncRunStatus.RUNNING)),
                name="orgmem_sync_active_config_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "status", "created_at"),
                name="orgmem_sync_org_status",
            ),
        ]
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.configuration_id}: {self.action_type}/{self.status}"

    def clean(self):
        if (
            self.configuration.organization_id != self.organization_id
            or self.configuration.provider != self.provider
            or self.action_request.configuration_id != self.configuration_id
            or self.action_request.action != self.action_type
        ):
            raise ValidationError(
                "Sync run organisation, provider, configuration, and action must match."
            )


class MemoryDailyReconciliationReport(models.Model):
    """One organisation-scoped daily reconciliation and health-report window."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_daily_reconciliation_reports",
    )
    report_date = models.DateField(db_index=True)
    time_zone = models.CharField(max_length=64)
    window_started_at = models.DateTimeField(db_index=True)
    status = models.CharField(
        max_length=16,
        choices=MemoryDailyReconciliationStatus.choices,
        default=MemoryDailyReconciliationStatus.RUNNING,
        db_index=True,
    )
    summary = models.JSONField(default=dict, blank=True)
    alerts = models.JSONField(default=list, blank=True)
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "report_date"),
                name="orgmem_daily_report_org_date_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "status", "report_date"),
                name="orgmem_daily_report_org_state",
            ),
        ]
        ordering = ("-report_date", "organization")

    def __str__(self):
        return f"{self.organization_id}: {self.report_date}/{self.status}"


class MemoryConnectionHealthSnapshot(models.Model):
    """Content-free health evidence for one connection in a daily report."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    report = models.ForeignKey(
        MemoryDailyReconciliationReport,
        on_delete=models.CASCADE,
        related_name="connection_snapshots",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_connection_health_snapshots",
    )
    configuration = models.ForeignKey(
        MemoryConnectionConfiguration,
        on_delete=models.CASCADE,
        related_name="daily_health_snapshots",
    )
    action_request = models.ForeignKey(
        MemorySourceActionRequest,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="daily_health_snapshots",
    )
    provider = models.CharField(max_length=32, choices=MemoryProvider.choices, db_index=True)
    health_status = models.CharField(
        max_length=16,
        choices=MemoryConnectionHealthStatus.choices,
        default=MemoryConnectionHealthStatus.SYNCING,
        db_index=True,
    )
    schedule_status = models.CharField(max_length=24, default="waiting", db_index=True)
    credential_status = models.CharField(max_length=32, blank=True, default="unknown")
    freshness_status = models.CharField(max_length=16, blank=True, default="unknown")
    watch_status = models.CharField(max_length=24, blank=True, default="not_applicable")
    provider_interval_seconds = models.PositiveIntegerField(default=86400)
    freshness_slo_seconds = models.PositiveIntegerField(default=86400)
    source_lag_seconds = models.PositiveIntegerField(null=True, blank=True)
    catch_up = models.BooleanField(default=False, db_index=True)
    last_attempted_sync_at = models.DateTimeField(null=True, blank=True)
    last_successful_sync_at = models.DateTimeField(null=True, blank=True)
    operator_action = models.CharField(max_length=512, blank=True, default="")
    counts = models.JSONField(default=dict, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("report", "configuration"),
                name="orgmem_health_report_config_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "health_status", "updated_at"),
                name="orgmem_health_org_state",
            ),
        ]
        ordering = ("provider", "configuration")

    def clean(self):
        if (
            self.report.organization_id != self.organization_id
            or self.configuration.organization_id != self.organization_id
            or self.configuration.provider != self.provider
        ):
            raise ValidationError(
                "Health report, configuration, organisation, and provider must match."
            )
        if self.action_request_id and self.action_request.configuration_id != self.configuration_id:
            raise ValidationError("Health snapshot action must belong to its configuration.")


class MemoryDailyCostLedger(models.Model):
    """Atomic daily reservation ledger for model and embedding work."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_daily_cost_ledgers",
    )
    budget_date = models.DateField(db_index=True)
    currency = models.CharField(max_length=3, default="AUD")
    ceiling_aud = models.DecimalField(max_digits=12, decimal_places=6)
    reserved_aud = models.DecimalField(max_digits=12, decimal_places=6, default=0)
    consumed_aud = models.DecimalField(max_digits=12, decimal_places=6, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "budget_date"),
                name="orgmem_cost_ledger_org_date_uniq",
            ),
            models.CheckConstraint(
                check=Q(ceiling_aud__gte=0) & Q(reserved_aud__gte=0) & Q(consumed_aud__gte=0),
                name="orgmem_cost_ledger_nonnegative",
            ),
        ]
        ordering = ("-budget_date", "organization")

    def __str__(self):
        return f"{self.organization_id}: {self.budget_date} {self.consumed_aud}/{self.ceiling_aud} AUD"


class MemoryCostReservation(models.Model):
    """A bounded estimate charged once for an expensive work item."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ledger = models.ForeignKey(
        MemoryDailyCostLedger,
        on_delete=models.CASCADE,
        related_name="reservations",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_cost_reservations",
    )
    work_item = models.OneToOneField(
        "MemoryWorkItem",
        on_delete=models.CASCADE,
        related_name="cost_reservation",
    )
    task_type = models.CharField(max_length=32, choices=MemoryWorkTaskType.choices)
    estimated_tokens = models.PositiveIntegerField(default=0)
    estimated_cost_aud = models.DecimalField(max_digits=12, decimal_places=6)
    actual_cost_aud = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=MemoryCostReservationStatus.choices,
        default=MemoryCostReservationStatus.RESERVED,
        db_index=True,
    )
    reserved_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("organization", "status", "reserved_at"),
                name="orgmem_cost_res_org_state",
            ),
        ]
        ordering = ("-reserved_at",)

    def clean(self):
        if (
            self.ledger.organization_id != self.organization_id
            or self.work_item.organization_id != self.organization_id
            or self.work_item.task_type != self.task_type
        ):
            raise ValidationError(
                "Cost ledger, work item, organisation, and task type must match."
            )


class MemoryRuntimeLane(models.Model):
    """A durable lock/throttle row used to serialize concurrency decisions."""

    key = models.CharField(max_length=160, primary_key=True)
    scope = models.CharField(max_length=16, choices=MemoryRuntimeLaneScope.choices)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="memory_runtime_lanes",
    )
    provider = models.CharField(
        max_length=32,
        choices=MemoryProvider.choices,
        blank=True,
        default="",
    )
    blocked_until = models.DateTimeField(null=True, blank=True, db_index=True)
    block_reason = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("scope", "blocked_until"),
                name="orgmem_lane_blocked",
            ),
        ]
        ordering = ("key",)

    def __str__(self):
        return self.key

    def clean(self):
        if self.scope == MemoryRuntimeLaneScope.ORGANIZATION:
            if self.organization_id is None or self.provider:
                raise ValidationError("An organisation lane requires only an organisation.")
        elif self.scope == MemoryRuntimeLaneScope.PROVIDER:
            if not self.provider or self.organization_id is not None:
                raise ValidationError("A provider lane requires only a provider.")


class MemorySourceAuditEvent(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_source_audit_events",
    )
    configuration = models.ForeignKey(
        MemoryConnectionConfiguration,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    actor_identity = models.ForeignKey(
        OrganizationIdentity,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memory_source_audit_events",
    )
    actor_membership = models.ForeignKey(
        OrganizationMembership,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memory_source_audit_events",
    )
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memory_source_audit_events",
    )
    event_type = models.CharField(max_length=64, db_index=True)
    from_state = models.CharField(max_length=32, blank=True, default="")
    to_state = models.CharField(max_length=32, blank=True, default="")
    request_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("organization", "event_type", "created_at"),
                name="orgmem_audit_org_event",
            ),
        ]
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.organization.domain}: {self.event_type}"


class ImmutableEvidenceMixin(models.Model):
    """Reject in-place edits to evidence fields while allowing lifecycle updates."""

    immutable_fields = ()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding and self.immutable_fields:
            existing = type(self).objects.filter(pk=self.pk).values(
                *self.immutable_fields
            ).first()
            if existing is not None:
                changed = []
                for field_name in self.immutable_fields:
                    persisted = existing[field_name]
                    current = getattr(self, field_name)
                    model_field_name = (
                        field_name[:-3] if field_name.endswith("_id") else field_name
                    )
                    try:
                        model_field = self._meta.get_field(model_field_name)
                    except FieldDoesNotExist:
                        model_field = None
                    if isinstance(model_field, models.DecimalField):
                        persisted = model_field.to_python(persisted)
                        current = model_field.to_python(current)
                    if persisted != current:
                        changed.append(field_name)
                if changed:
                    raise ValidationError(
                        "Immutable evidence fields cannot be edited in place: "
                        + ", ".join(changed)
                    )
        return super().save(*args, **kwargs)


class DriveInventoryManifest(ImmutableEvidenceMixin):
    """Immutable metadata-only inventory used for explicit operator approval."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="drive_inventory_manifests",
    )
    configuration = models.ForeignKey(
        MemoryConnectionConfiguration,
        on_delete=models.CASCADE,
        related_name="drive_inventory_manifests",
    )
    inventory_id = models.UUIDField()
    selection_fingerprint = models.CharField(max_length=64, db_index=True)
    selected_roots = models.JSONField(default=list)
    historical_cutoff = models.DateTimeField()
    allowed_mime_types = models.JSONField(default=list)
    is_partial = models.BooleanField(default=False, db_index=True)
    ceiling_reason = models.CharField(max_length=64, blank=True, default="")
    counts = models.JSONField(default=dict, blank=True)
    formats = models.JSONField(default=dict, blank=True)
    owners = models.JSONField(default=dict, blank=True)
    date_range = models.JSONField(default=dict, blank=True)
    estimated = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    start_page_token = models.TextField(blank=True, default="")
    snapshot = models.JSONField(default=list)
    snapshot_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    immutable_fields = (
        "organization_id",
        "configuration_id",
        "inventory_id",
        "selection_fingerprint",
        "selected_roots",
        "historical_cutoff",
        "allowed_mime_types",
        "is_partial",
        "ceiling_reason",
        "counts",
        "formats",
        "owners",
        "date_range",
        "estimated",
        "warnings",
        "start_page_token",
        "snapshot",
        "snapshot_hash",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("configuration", "inventory_id"),
                name="orgmem_drive_inventory_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("configuration", "created_at"),
                name="orgmem_drive_inv_created",
            ),
        ]
        ordering = ("-created_at",)

    def clean(self):
        if (
            self.configuration.organization_id != self.organization_id
            or self.configuration.provider != MemoryProvider.GOOGLE_DRIVE
        ):
            raise ValidationError("Drive inventory must match a Google Drive configuration.")


class DriveDocumentArtifact(models.Model):
    """Durable Drive file metadata; file bodies are intentionally not stored here."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="drive_document_artifacts",
    )
    configuration = models.ForeignKey(
        MemoryConnectionConfiguration,
        on_delete=models.CASCADE,
        related_name="drive_document_artifacts",
    )
    source_scope = models.ForeignKey(
        MemorySourceScope,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="drive_document_artifacts",
    )
    file_id = models.CharField(max_length=256)
    drive_id = models.CharField(max_length=256, blank=True, default="")
    shortcut_target_id = models.CharField(max_length=256, blank=True, default="")
    parent_ids = models.JSONField(default=list, blank=True)
    selected_root_ids = models.JSONField(default=list)
    lineages = models.JSONField(default=list, blank=True)
    name = models.CharField(max_length=512)
    mime_type = models.CharField(max_length=255)
    size_bytes = models.BigIntegerField(null=True, blank=True)
    web_view_url = models.URLField(max_length=2048, blank=True, default="")
    source_created_at = models.DateTimeField(null=True, blank=True)
    source_modified_at = models.DateTimeField(null=True, blank=True, db_index=True)
    provider_version = models.CharField(max_length=512, blank=True, default="")
    checksum = models.CharField(max_length=128, blank=True, default="")
    owner_snapshot = models.JSONField(default=list, blank=True)
    permission_snapshot = models.JSONField(default=dict, blank=True)
    lifecycle_state = models.CharField(
        max_length=24,
        choices=DriveArtifactState.choices,
        default=DriveArtifactState.ACTIVE,
        db_index=True,
    )
    supported = models.BooleanField(default=False, db_index=True)
    transcript_candidate = models.BooleanField(default=False, db_index=True)
    exclusion_reason = models.CharField(max_length=128, blank=True, default="")
    extraction_status = models.CharField(
        max_length=32,
        choices=DriveExtractionStatus.choices,
        default=DriveExtractionStatus.METADATA_ONLY,
        db_index=True,
    )
    extraction_error = models.TextField(blank=True, default="")
    work_classification = models.CharField(
        max_length=32,
        choices=DriveWorkClassification.choices,
        blank=True,
        default=DriveWorkClassification.NONE,
        db_index=True,
    )
    extracted_content_hash = models.CharField(max_length=64, blank=True, default="")
    parser_version = models.CharField(max_length=64, blank=True, default="")
    extraction_report = models.JSONField(default=dict, blank=True)
    last_extracted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    current_version = models.ForeignKey(
        "DriveDocumentArtifactVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="current_for_artifacts",
    )
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_synced_at = models.DateTimeField(default=timezone.now)
    removed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("configuration", "file_id"),
                name="orgmem_drive_artifact_file_uniq",
            ),
            models.CheckConstraint(
                check=(
                    Q(lifecycle_state=DriveArtifactState.ACTIVE, removed_at__isnull=True)
                    | ~Q(lifecycle_state=DriveArtifactState.ACTIVE)
                ),
                name="orgmem_drive_active_not_removed",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "lifecycle_state", "source_modified_at"),
                name="orgmem_drive_org_state",
            ),
            models.Index(
                fields=("configuration", "transcript_candidate", "extraction_status"),
                name="orgmem_drive_cfg_extract",
            ),
        ]
        ordering = ("configuration", "name", "file_id")

    def clean(self):
        if (
            self.configuration.organization_id != self.organization_id
            or self.configuration.provider != MemoryProvider.GOOGLE_DRIVE
        ):
            raise ValidationError("Drive artifact must match a Google Drive configuration.")
        if self.source_scope_id and self.source_scope.configuration_id != self.configuration_id:
            raise ValidationError("Drive artifact scope must belong to its configuration.")
        if self.current_version_id and self.current_version.artifact_id != self.pk:
            raise ValidationError("Current Drive version must belong to its artifact.")


class DriveDocumentArtifactVersion(ImmutableEvidenceMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    artifact = models.ForeignKey(
        DriveDocumentArtifact,
        on_delete=models.CASCADE,
        related_name="versions",
    )
    version_key = models.CharField(max_length=512)
    metadata_hash = models.CharField(max_length=64, db_index=True)
    metadata_snapshot = models.JSONField(default=dict)
    acl_snapshot = models.JSONField(default=dict)
    is_current = models.BooleanField(default=True, db_index=True)
    captured_at = models.DateTimeField(default=timezone.now, db_index=True)
    retired_at = models.DateTimeField(null=True, blank=True)

    immutable_fields = (
        "artifact_id",
        "version_key",
        "metadata_hash",
        "metadata_snapshot",
        "acl_snapshot",
        "captured_at",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("artifact", "version_key"),
                name="orgmem_drive_artifact_version_uniq",
            ),
            models.UniqueConstraint(
                fields=("artifact",),
                condition=Q(is_current=True),
                name="orgmem_drive_artifact_current_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("artifact", "is_current", "captured_at"),
                name="orgmem_drive_ver_current",
            ),
        ]
        ordering = ("artifact", "-captured_at")


class DriveMeeting(models.Model):
    """Stable meeting identity shared by transcripts, notes, and copied artifacts."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="drive_meetings",
    )
    configuration = models.ForeignKey(
        MemoryConnectionConfiguration,
        on_delete=models.CASCADE,
        related_name="drive_meetings",
    )
    identity_key = models.CharField(max_length=64)
    normalized_title = models.CharField(max_length=512)
    occurred_at = models.DateTimeField(null=True, blank=True, db_index=True)
    timezone_name = models.CharField(max_length=64, default="Australia/Sydney")
    participants = models.JSONField(default=list, blank=True)
    identity_basis = models.JSONField(default=dict)
    canonical_artifact = models.ForeignKey(
        DriveDocumentArtifact,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="canonical_for_meetings",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("configuration", "identity_key"),
                name="orgmem_drive_meeting_identity_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "occurred_at"),
                name="orgmem_drive_meeting_date",
            ),
        ]
        ordering = ("organization", "-occurred_at", "normalized_title")

    def clean(self):
        if self.configuration.organization_id != self.organization_id:
            raise ValidationError("Drive meeting must match its configuration organisation.")
        if self.canonical_artifact_id and (
            self.canonical_artifact.configuration_id != self.configuration_id
        ):
            raise ValidationError("Canonical meeting artifact must match the configuration.")


class DriveMeetingArtifactLink(models.Model):
    meeting = models.ForeignKey(
        DriveMeeting,
        on_delete=models.CASCADE,
        related_name="artifact_links",
    )
    artifact = models.OneToOneField(
        DriveDocumentArtifact,
        on_delete=models.CASCADE,
        related_name="meeting_link",
    )
    relation_type = models.CharField(
        max_length=24,
        choices=DriveMeetingRelation.choices,
        default=DriveMeetingRelation.SAME_MEETING_AS,
    )
    duplicate_of = models.ForeignKey(
        DriveDocumentArtifact,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="duplicate_drive_artifacts",
    )
    confidence = models.DecimalField(max_digits=4, decimal_places=3, default=1)
    evidence = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("meeting", "artifact")

    def clean(self):
        if self.artifact.configuration_id != self.meeting.configuration_id:
            raise ValidationError("Meeting link must stay within one Drive configuration.")
        if self.duplicate_of_id and (
            self.duplicate_of.configuration_id != self.artifact.configuration_id
            or self.duplicate_of_id == self.artifact_id
        ):
            raise ValidationError("Duplicate lineage must reference another artifact in the configuration.")


class DriveDocumentExtraction(ImmutableEvidenceMixin):
    """Immutable parser result for one immutable Drive metadata version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    artifact_version = models.ForeignKey(
        DriveDocumentArtifactVersion,
        on_delete=models.CASCADE,
        related_name="extractions",
    )
    source_version = models.OneToOneField(
        "MemorySourceVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="drive_extraction",
    )
    status = models.CharField(max_length=32, choices=DriveExtractionStatus.choices)
    work_classification = models.CharField(
        max_length=32,
        choices=DriveWorkClassification.choices,
        blank=True,
        default=DriveWorkClassification.NONE,
    )
    parser_name = models.CharField(max_length=64)
    parser_version = models.CharField(max_length=64)
    export_mime_type = models.CharField(max_length=255, blank=True, default="")
    content_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    normalized_content_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
    )
    content_signature = models.JSONField(default=list, blank=True)
    byte_count = models.PositiveBigIntegerField(default=0)
    character_count = models.PositiveBigIntegerField(default=0)
    chunk_count = models.PositiveIntegerField(default=0)
    warnings = models.JSONField(default=list, blank=True)
    parser_report = models.JSONField(default=dict, blank=True)
    extracted_at = models.DateTimeField(default=timezone.now, db_index=True)

    immutable_fields = (
        "artifact_version_id",
        "source_version_id",
        "status",
        "work_classification",
        "parser_name",
        "parser_version",
        "export_mime_type",
        "content_hash",
        "normalized_content_hash",
        "content_signature",
        "byte_count",
        "character_count",
        "chunk_count",
        "warnings",
        "parser_report",
        "extracted_at",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("artifact_version", "parser_version"),
                name="orgmem_drive_extract_parser_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("status", "work_classification", "extracted_at"),
                name="orgmem_drive_extract_status",
            ),
        ]
        ordering = ("-extracted_at",)


class DriveReconciliationReport(models.Model):
    """Cumulative outcome counters for one resumable Drive sync run."""

    sync_run = models.OneToOneField(
        MemorySyncRun,
        on_delete=models.CASCADE,
        related_name="drive_reconciliation_report",
    )
    configuration = models.ForeignKey(
        MemoryConnectionConfiguration,
        on_delete=models.CASCADE,
        related_name="drive_reconciliation_reports",
    )
    manifest = models.ForeignKey(
        DriveInventoryManifest,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reconciliation_reports",
    )
    counts = models.JSONField(default=dict)
    last_checkpoint = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-started_at",)

    def clean(self):
        if self.sync_run.configuration_id != self.configuration_id:
            raise ValidationError("Drive reconciliation report must match its sync run.")


class DriveWatchChannel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    configuration = models.ForeignKey(
        MemoryConnectionConfiguration,
        on_delete=models.CASCADE,
        related_name="drive_watch_channels",
    )
    channel_id = models.CharField(max_length=64, unique=True)
    resource_id = models.CharField(max_length=255)
    resource_uri = models.URLField(max_length=2048, blank=True, default="")
    token_hash = models.CharField(max_length=64)
    expiration_at = models.DateTimeField(db_index=True)
    status = models.CharField(
        max_length=16,
        choices=DriveWatchStatus.choices,
        default=DriveWatchStatus.ACTIVE,
        db_index=True,
    )
    last_message_number = models.PositiveBigIntegerField(default=0)
    last_resource_state = models.CharField(max_length=32, blank=True, default="")
    last_notified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("configuration", "status", "expiration_at"),
                name="orgmem_drive_watch_active",
            ),
        ]
        ordering = ("-created_at",)

    def clean(self):
        if self.configuration.provider != MemoryProvider.GOOGLE_DRIVE:
            raise ValidationError("Drive watch channels require a Google Drive configuration.")


class NotionPageArtifact(models.Model):
    """Durable, selected-root Notion page inventory owned by organisational memory."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="notion_page_artifacts",
    )
    configuration = models.ForeignKey(
        MemoryConnectionConfiguration,
        on_delete=models.CASCADE,
        related_name="notion_page_artifacts",
    )
    source_scope = models.ForeignKey(
        MemorySourceScope,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notion_page_artifacts",
    )
    notion_page_id = models.CharField(max_length=128)
    selected_root_ids = models.JSONField(default=list)
    ancestor_page_ids = models.JSONField(default=list, blank=True)
    parent_type = models.CharField(max_length=32, blank=True, default="")
    parent_external_id = models.CharField(max_length=128, blank=True, default="")
    title = models.CharField(max_length=512, blank=True, default="")
    canonical_url = models.URLField(max_length=2048, blank=True, default="")
    property_text = models.TextField(blank=True, default="")
    cleaned_text = models.TextField(blank=True, default="")
    source_created_at = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True, db_index=True)
    lifecycle_state = models.CharField(
        max_length=24,
        choices=NotionArtifactState.choices,
        default=NotionArtifactState.ACTIVE,
        db_index=True,
    )
    in_trash = models.BooleanField(default=False, db_index=True)
    is_archived = models.BooleanField(default=False)
    provider_revision = models.CharField(max_length=128, blank=True, default="")
    content_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    scan_generation = models.UUIDField(null=True, blank=True, db_index=True)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("configuration", "notion_page_id"),
                name="orgmem_notion_page_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("configuration", "lifecycle_state", "source_updated_at"),
                name="orgmem_notion_cfg_state",
            ),
        ]
        ordering = ("configuration", "title", "notion_page_id")

    def clean(self):
        if (
            self.configuration.organization_id != self.organization_id
            or self.configuration.provider != MemoryProvider.NOTION
        ):
            raise ValidationError("Notion page must match a Notion memory configuration.")
        if self.source_scope_id and self.source_scope.configuration_id != self.configuration_id:
            raise ValidationError("Notion page scope must belong to its configuration.")


class NotionBlockArtifact(models.Model):
    """Normalized Notion block text with stable, provider-native locators."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    page = models.ForeignKey(
        NotionPageArtifact,
        on_delete=models.CASCADE,
        related_name="blocks",
    )
    notion_block_id = models.CharField(max_length=128)
    parent_block_id = models.CharField(max_length=128, blank=True, default="")
    block_type = models.CharField(max_length=64)
    ordinal = models.PositiveIntegerField()
    depth = models.PositiveSmallIntegerField(default=0)
    heading_path = models.JSONField(default=list, blank=True)
    plain_text = models.TextField(blank=True, default="")
    has_children = models.BooleanField(default=False)
    in_trash = models.BooleanField(default=False)
    is_archived = models.BooleanField(default=False)
    source_created_at = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    content_hash = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("page", "notion_block_id"),
                name="orgmem_notion_block_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("page", "ordinal"),
                name="orgmem_notion_block_order",
            ),
        ]
        ordering = ("page", "ordinal", "notion_block_id")


class GmailScopedMessageArtifact(models.Model):
    """Exact selected-label membership for a Gmail message in one memory config."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="gmail_scoped_message_artifacts",
    )
    configuration = models.ForeignKey(
        MemoryConnectionConfiguration,
        on_delete=models.CASCADE,
        related_name="gmail_scoped_message_artifacts",
    )
    source_scope = models.ForeignKey(
        MemorySourceScope,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="gmail_scoped_message_artifacts",
    )
    message_artifact = models.ForeignKey(
        "startup_updates.GmailMessageArtifact",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memory_scope_artifacts",
    )
    gmail_message_id = models.CharField(max_length=255)
    gmail_thread_id = models.CharField(max_length=255, db_index=True)
    selected_label_ids = models.JSONField(default=list)
    lifecycle_state = models.CharField(
        max_length=24,
        choices=GmailScopedArtifactState.choices,
        default=GmailScopedArtifactState.ACTIVE,
        db_index=True,
    )
    history_id = models.CharField(max_length=255, blank=True, default="")
    internal_date = models.DateTimeField(null=True, blank=True, db_index=True)
    scan_generation = models.UUIDField(null=True, blank=True, db_index=True)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    removed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("configuration", "gmail_message_id"),
                name="orgmem_gmail_scoped_msg_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("configuration", "lifecycle_state", "gmail_thread_id"),
                name="orgmem_gmail_cfg_thread",
            ),
        ]
        ordering = ("configuration", "gmail_thread_id", "internal_date", "gmail_message_id")

    def clean(self):
        if (
            self.configuration.organization_id != self.organization_id
            or self.configuration.provider != MemoryProvider.GMAIL
        ):
            raise ValidationError("Gmail scoped message must match a Gmail memory configuration.")
        if self.source_scope_id and self.source_scope.configuration_id != self.configuration_id:
            raise ValidationError("Gmail message scope must belong to its configuration.")
        if self.message_artifact_id and (
            self.message_artifact.organization_id != self.organization_id
            or self.message_artifact.google_connection_id
            != self.configuration.google_connection_id
        ):
            raise ValidationError("Gmail message artifact must match the configured mailbox.")


class GmailMailboxWatch(models.Model):
    """Current optional Gmail/Pub/Sub watch metadata; never stores message content."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    configuration = models.OneToOneField(
        MemoryConnectionConfiguration,
        on_delete=models.CASCADE,
        related_name="gmail_mailbox_watch",
    )
    email_address = models.EmailField()
    topic_name = models.CharField(max_length=512)
    label_ids = models.JSONField(default=list)
    history_id = models.CharField(max_length=255, blank=True, default="")
    expiration_at = models.DateTimeField(null=True, blank=True, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=GmailWatchStatus.choices,
        default=GmailWatchStatus.ACTIVE,
        db_index=True,
    )
    last_renewed_at = models.DateTimeField(null=True, blank=True)
    last_notification_at = models.DateTimeField(null=True, blank=True)
    last_notification_history_id = models.CharField(max_length=255, blank=True, default="")
    last_pubsub_message_id = models.CharField(max_length=255, blank=True, default="")
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("status", "expiration_at"),
                name="orgmem_gmail_watch_expiry",
            ),
        ]
        ordering = ("expiration_at", "configuration")

    def clean(self):
        if self.configuration.provider != MemoryProvider.GMAIL:
            raise ValidationError("Gmail mailbox watches require a Gmail configuration.")


class StructuredAggregateArtifact(models.Model):
    """Sanitized finance/event aggregate owned by one approved memory config."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="structured_memory_aggregates",
    )
    configuration = models.ForeignKey(
        MemoryConnectionConfiguration,
        on_delete=models.CASCADE,
        related_name="structured_aggregate_artifacts",
    )
    source_scope = models.ForeignKey(
        MemorySourceScope,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="structured_aggregate_artifacts",
    )
    provider = models.CharField(
        max_length=32,
        choices=MemoryProvider.choices,
        db_index=True,
    )
    source_type = models.CharField(max_length=64)
    external_id = models.CharField(max_length=512)
    metric_key = models.CharField(max_length=100, blank=True, default="", db_index=True)
    name = models.CharField(max_length=255)
    period_start = models.DateField(null=True, blank=True, db_index=True)
    period_end = models.DateField(null=True, blank=True)
    value_number = models.DecimalField(
        max_digits=24,
        decimal_places=6,
        null=True,
        blank=True,
    )
    value_text = models.CharField(max_length=255, blank=True, default="")
    unit = models.CharField(max_length=32, blank=True, default="")
    dimensions = models.JSONField(default=dict, blank=True)
    source_revision = models.CharField(max_length=64)
    occurred_at = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    volatile_until = models.DateTimeField(null=True, blank=True)
    stale_after = models.DateTimeField(null=True, blank=True, db_index=True)
    lifecycle_state = models.CharField(
        max_length=24,
        choices=StructuredAggregateState.choices,
        default=StructuredAggregateState.ACTIVE,
        db_index=True,
    )
    scan_generation = models.UUIDField(null=True, blank=True, db_index=True)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("configuration", "source_type", "external_id"),
                name="orgmem_struct_agg_identity_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("configuration", "lifecycle_state", "period_start"),
                name="orgmem_struct_agg_cfg_state",
            ),
            models.Index(
                fields=("provider", "metric_key", "period_start"),
                name="orgmem_struct_agg_metric",
            ),
        ]
        ordering = (
            "configuration",
            "source_type",
            "period_start",
            "external_id",
        )

    def clean(self):
        if (
            self.configuration.organization_id != self.organization_id
            or self.configuration.provider != self.provider
        ):
            raise ValidationError(
                "Structured aggregate must match its memory configuration."
            )
        if self.source_scope_id and self.source_scope.configuration_id != self.configuration_id:
            raise ValidationError("Structured aggregate scope must belong to its configuration.")


class MemoryProviderEventReceipt(models.Model):
    """Replay-safe receipt for a verified provider wake event; never stores content."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(
        max_length=32,
        choices=MemoryProvider.choices,
        db_index=True,
    )
    receipt_key = models.CharField(max_length=64)
    external_account_id = models.CharField(max_length=512, blank=True, default="")
    external_scope_id = models.CharField(max_length=512, blank=True, default="")
    event_type = models.CharField(max_length=128, blank=True, default="")
    payload_hash = models.CharField(max_length=64)
    scheduled_configuration_count = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("provider", "receipt_key"),
                name="orgmem_provider_event_receipt_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("provider", "received_at"),
                name="orgmem_event_provider_received",
            ),
        ]
        ordering = ("-received_at",)


class MemorySource(models.Model):
    """Stable identity for one provider object across immutable versions."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_sources",
    )
    configuration = models.ForeignKey(
        MemoryConnectionConfiguration,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memory_sources",
    )
    source_scope = models.ForeignKey(
        MemorySourceScope,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memory_sources",
    )
    provider = models.CharField(
        max_length=32,
        choices=MemoryProvider.choices,
        db_index=True,
    )
    external_account_id = models.CharField(max_length=512)
    source_type = models.CharField(max_length=64)
    external_id = models.CharField(max_length=1024)
    canonical_url = models.URLField(max_length=2048, blank=True, default="")
    title = models.CharField(max_length=512, blank=True, default="")
    author_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="authored_memory_sources",
    )
    author_external_id = models.CharField(max_length=512, blank=True, default="")
    lifecycle_state = models.CharField(
        max_length=24,
        choices=MemorySourceLifecycle.choices,
        default=MemorySourceLifecycle.ACTIVE,
        db_index=True,
    )
    current_version = models.ForeignKey(
        "MemorySourceVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="current_for_sources",
    )
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    access_revoked_at = models.DateTimeField(null=True, blank=True, db_index=True)
    tombstoned_at = models.DateTimeField(null=True, blank=True, db_index=True)
    tombstone_reason = models.CharField(max_length=512, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=(
                    "organization",
                    "provider",
                    "external_account_id",
                    "source_type",
                    "external_id",
                ),
                name="orgmem_source_external_uniq",
            ),
            models.CheckConstraint(
                check=(
                    Q(lifecycle_state=MemorySourceLifecycle.TOMBSTONED, tombstoned_at__isnull=False)
                    | ~Q(lifecycle_state=MemorySourceLifecycle.TOMBSTONED)
                ),
                name="orgmem_source_tombstone_state",
            ),
            models.CheckConstraint(
                check=(
                    Q(lifecycle_state=MemorySourceLifecycle.ACCESS_REVOKED, access_revoked_at__isnull=False)
                    | ~Q(lifecycle_state=MemorySourceLifecycle.ACCESS_REVOKED)
                ),
                name="orgmem_source_revoked_state",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "provider", "source_type", "lifecycle_state"),
                name="orgmem_src_org_prov_state",
            ),
            models.Index(
                fields=("configuration", "lifecycle_state", "last_seen_at"),
                name="orgmem_src_cfg_state_seen",
            ),
        ]
        ordering = ("organization", "provider", "source_type", "external_id")

    def __str__(self):
        return f"{self.provider}:{self.source_type}:{self.external_id}"

    def clean(self):
        if self.configuration_id and (
            self.configuration.organization_id != self.organization_id
            or self.configuration.provider != self.provider
        ):
            raise ValidationError("Source configuration must match organisation and provider.")
        if self.source_scope_id and (
            self.source_scope.configuration.organization_id != self.organization_id
            or self.source_scope.configuration.provider != self.provider
        ):
            raise ValidationError("Source scope must match organisation and provider.")
        if self.current_version_id and self.current_version.source_id != self.pk:
            raise ValidationError("Current version must belong to this source.")


class MemorySourceVersion(ImmutableEvidenceMixin):
    """An immutable capture of a provider source at a specific revision."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(
        MemorySource,
        on_delete=models.CASCADE,
        related_name="versions",
    )
    version_key = models.CharField(max_length=512)
    content_hash = models.CharField(max_length=64, db_index=True)
    source_created_at = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    occurred_at = models.DateTimeField(null=True, blank=True, db_index=True)
    bounded_excerpt = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    classification = models.CharField(
        max_length=32,
        choices=MemoryClassification.choices,
        default=MemoryClassification.INTERNAL,
        db_index=True,
    )
    is_current = models.BooleanField(default=True, db_index=True)
    captured_at = models.DateTimeField(default=timezone.now, db_index=True)
    retired_at = models.DateTimeField(null=True, blank=True, db_index=True)
    tombstoned_at = models.DateTimeField(null=True, blank=True, db_index=True)

    immutable_fields = (
        "source_id",
        "version_key",
        "content_hash",
        "source_created_at",
        "source_updated_at",
        "occurred_at",
        "bounded_excerpt",
        "metadata",
        "classification",
        "captured_at",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("source", "version_key"),
                name="orgmem_source_version_key_uniq",
            ),
            models.UniqueConstraint(
                fields=("source",),
                condition=Q(is_current=True),
                name="orgmem_source_current_ver_uniq",
            ),
            models.CheckConstraint(
                check=Q(is_current=False) | Q(tombstoned_at__isnull=True),
                name="orgmem_current_ver_not_tomb",
            ),
        ]
        indexes = [
            models.Index(
                fields=("source", "is_current", "captured_at"),
                name="orgmem_ver_source_current",
            ),
        ]
        ordering = ("source", "-captured_at")

    def __str__(self):
        return f"{self.source_id}: {self.version_key}"

    def clean(self):
        if len(self.bounded_excerpt or "") > 4096:
            raise ValidationError("Source-version excerpts are limited to 4096 characters.")
        if self.retired_at and self.retired_at < self.captured_at:
            raise ValidationError("A version cannot retire before it was captured.")


class MemoryAclSnapshot(ImmutableEvidenceMixin):
    """Provider ACL state captured with exactly one source version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_version = models.OneToOneField(
        MemorySourceVersion,
        on_delete=models.CASCADE,
        related_name="acl_snapshot",
    )
    provider_revision = models.CharField(max_length=512, blank=True, default="")
    principal_refs = models.JSONField(default=list, blank=True)
    group_refs = models.JSONField(default=list, blank=True)
    link_sharing = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    fingerprint = models.CharField(max_length=64, db_index=True)
    is_accessible = models.BooleanField(default=True, db_index=True)
    captured_at = models.DateTimeField(default=timezone.now, db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True, db_index=True)

    immutable_fields = (
        "source_version_id",
        "provider_revision",
        "principal_refs",
        "group_refs",
        "link_sharing",
        "metadata",
        "fingerprint",
        "captured_at",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=Q(is_accessible=False) | Q(revoked_at__isnull=True),
                name="orgmem_acl_accessible_not_revoked",
            ),
        ]
        ordering = ("-captured_at",)

    def __str__(self):
        return f"{self.source_version_id}: {self.fingerprint[:12]}"


class MemoryChunk(ImmutableEvidenceMixin):
    """Verbatim evidence unit belonging to one immutable source version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_version = models.ForeignKey(
        MemorySourceVersion,
        on_delete=models.CASCADE,
        related_name="chunks",
    )
    ordinal = models.PositiveIntegerField()
    chunk_kind = models.CharField(max_length=64, blank=True, default="text")
    source_locator = models.JSONField(default=dict, blank=True)
    text = models.TextField()
    token_count = models.PositiveIntegerField(default=0)
    start_offset = models.PositiveIntegerField(null=True, blank=True)
    end_offset = models.PositiveIntegerField(null=True, blank=True)
    occurred_at = models.DateTimeField(null=True, blank=True, db_index=True)
    content_hash = models.CharField(max_length=64, db_index=True)
    classification = models.CharField(
        max_length=32,
        choices=MemoryClassification.choices,
        default=MemoryClassification.INTERNAL,
        db_index=True,
    )
    search_vector = SearchVectorField(null=True, editable=False)
    embedding_model = models.CharField(max_length=128, blank=True, default="")
    embedding_version = models.CharField(max_length=64, blank=True, default="")
    active_for_retrieval = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    immutable_fields = (
        "source_version_id",
        "ordinal",
        "chunk_kind",
        "source_locator",
        "text",
        "token_count",
        "start_offset",
        "end_offset",
        "occurred_at",
        "content_hash",
        "classification",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("source_version", "ordinal"),
                name="orgmem_chunk_version_ordinal_uniq",
            ),
            models.CheckConstraint(
                check=(
                    Q(start_offset__isnull=True, end_offset__isnull=True)
                    | Q(start_offset__isnull=False, end_offset__gt=models.F("start_offset"))
                ),
                name="orgmem_chunk_offsets_valid",
            ),
            models.CheckConstraint(
                check=Q(active_for_retrieval=False) | ~Q(classification=MemoryClassification.NO_AGENT),
                name="orgmem_chunk_no_agent_inactive",
            ),
        ]
        indexes = [
            models.Index(
                fields=("source_version", "active_for_retrieval", "ordinal"),
                name="orgmem_chunk_ver_active",
            ),
            models.Index(
                fields=("classification", "active_for_retrieval", "occurred_at"),
                name="orgmem_chunk_class_active",
            ),
            GinIndex(fields=("search_vector",), name="orgmem_chunk_search_gin"),
        ]
        ordering = ("source_version", "ordinal")

    def __str__(self):
        return f"{self.source_version_id}: chunk {self.ordinal}"

    def clean(self):
        if len(self.text or "") > 250000:
            raise ValidationError("A memory chunk is limited to 250,000 characters.")
        if self.classification != self.source_version.classification:
            raise ValidationError("Chunk classification must match its source version.")


class MemoryChunkEmbedding(models.Model):
    """A versioned vector for an immutable evidence chunk.

    Historical versions are retained so an embedding rollout can be rebuilt and
    promoted atomically without mixing model versions in one retrieval lane.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_chunk_embeddings",
    )
    chunk = models.ForeignKey(
        MemoryChunk,
        on_delete=models.CASCADE,
        related_name="embeddings",
    )
    model = models.CharField(max_length=128)
    version = models.CharField(max_length=64)
    dimensions = models.PositiveIntegerField(default=1536)
    vector = VectorField(dimensions=1536)
    vector_hash = models.CharField(max_length=64)
    is_current = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("chunk", "model", "version"),
                name="orgmem_embedding_chunk_version_uniq",
            ),
            models.UniqueConstraint(
                fields=("chunk",),
                condition=Q(is_current=True),
                name="orgmem_embedding_one_current",
            ),
            models.CheckConstraint(
                check=Q(dimensions=1536),
                name="orgmem_embedding_dimensions_1536",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "model", "version", "is_current"),
                name="orgmem_embed_org_version",
            ),
            HnswIndex(
                fields=("vector",),
                name="orgmem_embed_vector_hnsw",
                m=16,
                ef_construction=64,
                opclasses=("vector_cosine_ops",),
            ),
        ]
        ordering = ("chunk", "-created_at")

    def __str__(self):
        return f"{self.chunk_id}: {self.model}/{self.version}"

    def clean(self):
        if self.organization_id != self.chunk.source_version.source.organization_id:
            raise ValidationError("Embedding organization must match its chunk.")


class MemoryExtractionRun(ImmutableEvidenceMixin):
    """One immutable, versioned extraction outcome for a source version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_extraction_runs",
    )
    source_version = models.ForeignKey(
        MemorySourceVersion,
        on_delete=models.CASCADE,
        related_name="extraction_runs",
    )
    idempotency_key = models.CharField(max_length=64, unique=True)
    status = models.CharField(
        max_length=16,
        choices=MemoryExtractionStatus.choices,
        db_index=True,
    )
    extractor_version = models.CharField(max_length=64)
    schema_version = models.CharField(max_length=64)
    prompt_version = models.CharField(max_length=64)
    model = models.CharField(max_length=128)
    prompt_input_hash = models.CharField(max_length=64)
    candidate_payload_hash = models.CharField(max_length=64, blank=True, default="")
    source_summary = models.TextField(blank=True, default="")
    safety_flags = models.JSONField(default=list, blank=True)
    no_memory_reason = models.CharField(max_length=512, blank=True, default="")
    provider_response_id = models.CharField(max_length=255, blank=True, default="")
    usage = models.JSONField(default=dict, blank=True)
    completed_at = models.DateTimeField(default=timezone.now, db_index=True)

    immutable_fields = (
        "organization_id",
        "source_version_id",
        "idempotency_key",
        "status",
        "extractor_version",
        "schema_version",
        "prompt_version",
        "model",
        "prompt_input_hash",
        "candidate_payload_hash",
        "source_summary",
        "safety_flags",
        "no_memory_reason",
        "provider_response_id",
        "usage",
        "completed_at",
    )

    class Meta:
        indexes = [
            models.Index(
                fields=("organization", "status", "completed_at"),
                name="orgmem_extract_org_status",
            ),
            models.Index(
                fields=("source_version", "extractor_version"),
                name="orgmem_extract_version",
            ),
        ]
        ordering = ("-completed_at",)

    def clean(self):
        if self.organization_id != self.source_version.source.organization_id:
            raise ValidationError("Extraction organization must match its source version.")
        if len(self.source_summary or "") > 4096:
            raise ValidationError("Extraction summaries are limited to 4096 characters.")


class MemoryEntity(models.Model):
    """A governed entity candidate; ambiguous people remain source-scoped."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_entities",
    )
    entity_type = models.CharField(max_length=32, choices=MemoryEntityType.choices, db_index=True)
    canonical_name = models.CharField(max_length=512)
    normalized_name = models.CharField(max_length=512, db_index=True)
    resolved_key = models.CharField(max_length=255, blank=True, default="")
    description = models.TextField(blank=True, default="")
    aliases = models.JSONField(default=list, blank=True)
    external_refs = models.JSONField(default=dict, blank=True)
    merged_into = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="merged_entities",
    )
    merged_at = models.DateTimeField(null=True, blank=True, db_index=True)
    classification = models.CharField(
        max_length=32,
        choices=MemoryClassification.choices,
        default=MemoryClassification.INTERNAL,
        db_index=True,
    )
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "resolved_key"),
                condition=~Q(resolved_key=""),
                name="orgmem_entity_resolved_key_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "entity_type", "normalized_name"),
                name="orgmem_entity_name",
            ),
        ]
        ordering = ("organization", "entity_type", "normalized_name")

    def __str__(self):
        return f"{self.entity_type}: {self.canonical_name}"

    def clean(self):
        if self.merged_into_id:
            if self.merged_into_id == self.pk:
                raise ValidationError("An entity cannot be merged into itself.")
            if (
                self.merged_into.organization_id != self.organization_id
                or self.merged_into.entity_type != self.entity_type
            ):
                raise ValidationError("Merged entities must share organization and type.")


class MemoryClaim(ImmutableEvidenceMixin):
    """An atomic, time-aware candidate assertion grounded in exact evidence."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_claims",
    )
    extraction_run = models.ForeignKey(
        MemoryExtractionRun,
        on_delete=models.CASCADE,
        related_name="claims",
    )
    candidate_key = models.CharField(max_length=64)
    kind = models.CharField(max_length=32, choices=MemoryClaimKind.choices, db_index=True)
    epistemic_type = models.CharField(
        max_length=24,
        choices=MemoryEpistemicType.choices,
        db_index=True,
    )
    subject_entity = models.ForeignKey(
        MemoryEntity,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="subject_claims",
    )
    predicate = models.CharField(max_length=255)
    object_entity = models.ForeignKey(
        MemoryEntity,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="object_claims",
    )
    object_value = models.JSONField(null=True, blank=True)
    statement = models.TextField()
    normalized_key = models.CharField(max_length=64, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=MemoryClaimStatus.choices,
        default=MemoryClaimStatus.CANDIDATE,
        db_index=True,
    )
    classification = models.CharField(
        max_length=32,
        choices=MemoryClassification.choices,
        default=MemoryClassification.INTERNAL,
        db_index=True,
    )
    confidence = models.DecimalField(max_digits=4, decimal_places=3)
    importance = models.DecimalField(max_digits=4, decimal_places=3)
    source_authority = models.DecimalField(max_digits=4, decimal_places=3, default=0.5)
    volatility = models.CharField(
        max_length=16,
        choices=MemoryPolicyVolatility.choices,
        default=MemoryPolicyVolatility.NORMAL,
    )
    observed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    event_start_at = models.DateTimeField(null=True, blank=True)
    event_end_at = models.DateTimeField(null=True, blank=True)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    last_confirmed_at = models.DateTimeField(null=True, blank=True)
    stale_after = models.DateTimeField(null=True, blank=True, db_index=True)
    recorded_at = models.DateTimeField(default=timezone.now, db_index=True)
    superseded_at = models.DateTimeField(null=True, blank=True)
    review_required = models.BooleanField(default=True, db_index=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_memory_claims",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    extractor_version = models.CharField(max_length=64)
    extractor_model = models.CharField(max_length=128)
    extractor_prompt_version = models.CharField(max_length=64)
    extractor_schema_version = models.CharField(max_length=64)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    immutable_fields = (
        "organization_id",
        "extraction_run_id",
        "candidate_key",
        "kind",
        "epistemic_type",
        "subject_entity_id",
        "predicate",
        "object_entity_id",
        "object_value",
        "statement",
        "normalized_key",
        "classification",
        "confidence",
        "importance",
        "source_authority",
        "volatility",
        "observed_at",
        "event_start_at",
        "event_end_at",
        "recorded_at",
        "extractor_version",
        "extractor_model",
        "extractor_prompt_version",
        "extractor_schema_version",
        "metadata",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("extraction_run", "candidate_key"),
                name="orgmem_claim_candidate_uniq",
            ),
            models.CheckConstraint(
                check=Q(confidence__gte=0, confidence__lte=1),
                name="orgmem_claim_confidence_range",
            ),
            models.CheckConstraint(
                check=Q(importance__gte=0, importance__lte=1),
                name="orgmem_claim_importance_range",
            ),
            models.CheckConstraint(
                check=Q(source_authority__gte=0, source_authority__lte=1),
                name="orgmem_claim_authority_range",
            ),
            models.CheckConstraint(
                check=Q(valid_until__isnull=True) | Q(valid_from__isnull=True) | Q(valid_until__gt=models.F("valid_from")),
                name="orgmem_claim_valid_interval",
            ),
            models.CheckConstraint(
                check=Q(event_end_at__isnull=True) | Q(event_start_at__isnull=True) | Q(event_end_at__gte=models.F("event_start_at")),
                name="orgmem_claim_event_interval",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "status", "kind", "recorded_at"),
                name="orgmem_claim_org_state",
            ),
            models.Index(
                fields=("organization", "normalized_key", "status"),
                name="orgmem_claim_normalized",
            ),
        ]
        ordering = ("-recorded_at",)

    def clean(self):
        if self.organization_id != self.extraction_run.organization_id:
            raise ValidationError("Claim organization must match its extraction run.")
        for entity in (self.subject_entity, self.object_entity):
            if entity is not None and entity.organization_id != self.organization_id:
                raise ValidationError("Claim entities must belong to the claim organization.")
        if self.epistemic_type == MemoryEpistemicType.PROPOSAL and self.kind == MemoryClaimKind.DECISION:
            raise ValidationError("A proposal cannot be stored as a decision.")
        if not (self.object_entity_id or self.object_value is not None):
            raise ValidationError("A claim requires an object entity or object value.")


class MemoryEvidence(ImmutableEvidenceMixin):
    """A bounded exact quote connecting a claim to immutable source content."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    claim = models.ForeignKey(MemoryClaim, on_delete=models.CASCADE, related_name="evidence")
    source = models.ForeignKey(MemorySource, on_delete=models.CASCADE, related_name="claim_evidence")
    source_version = models.ForeignKey(
        MemorySourceVersion,
        on_delete=models.CASCADE,
        related_name="claim_evidence",
    )
    chunk = models.ForeignKey(MemoryChunk, on_delete=models.CASCADE, related_name="claim_evidence")
    evidence_role = models.CharField(
        max_length=16,
        choices=MemoryEvidenceRole.choices,
        default=MemoryEvidenceRole.SUPPORTS,
    )
    quote = models.TextField()
    quote_start = models.PositiveIntegerField(null=True, blank=True)
    quote_end = models.PositiveIntegerField(null=True, blank=True)
    quote_hash = models.CharField(max_length=64)
    source_locator = models.JSONField(default=dict)
    evidence_confidence = models.DecimalField(max_digits=4, decimal_places=3, default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    immutable_fields = (
        "claim_id",
        "source_id",
        "source_version_id",
        "chunk_id",
        "evidence_role",
        "quote",
        "quote_start",
        "quote_end",
        "quote_hash",
        "source_locator",
        "evidence_confidence",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("claim", "chunk", "evidence_role", "quote_hash"),
                name="orgmem_evidence_quote_uniq",
            ),
            models.CheckConstraint(
                check=Q(evidence_confidence__gte=0, evidence_confidence__lte=1),
                name="orgmem_evidence_conf_range",
            ),
            models.CheckConstraint(
                check=(
                    Q(quote_start__isnull=True, quote_end__isnull=True)
                    | Q(quote_start__isnull=False, quote_end__gt=models.F("quote_start"))
                ),
                name="orgmem_evidence_offsets",
            ),
        ]
        ordering = ("claim", "created_at")

    def clean(self):
        if self.source_version.source_id != self.source_id or self.chunk.source_version_id != self.source_version_id:
            raise ValidationError("Evidence source, version, and chunk must describe the same source.")
        if self.claim.organization_id != self.source.organization_id:
            raise ValidationError("Evidence and claim must belong to the same organization.")
        if not self.quote or len(self.quote) > 2000:
            raise ValidationError("Evidence quotes must contain between 1 and 2,000 characters.")
        if self.quote not in self.chunk.text:
            raise ValidationError("Evidence quote must occur verbatim in its chunk.")


class MemoryClaimLink(ImmutableEvidenceMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_claim_links",
    )
    from_claim = models.ForeignKey(MemoryClaim, on_delete=models.CASCADE, related_name="outgoing_links")
    to_claim = models.ForeignKey(MemoryClaim, on_delete=models.CASCADE, related_name="incoming_links")
    relation_type = models.CharField(max_length=24, choices=MemoryClaimRelation.choices)
    confidence = models.DecimalField(max_digits=4, decimal_places=3, default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    immutable_fields = (
        "organization_id",
        "from_claim_id",
        "to_claim_id",
        "relation_type",
        "confidence",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("from_claim", "to_claim", "relation_type"),
                name="orgmem_claim_link_uniq",
            ),
            models.CheckConstraint(
                check=~Q(from_claim=models.F("to_claim")),
                name="orgmem_claim_link_not_self",
            ),
            models.CheckConstraint(
                check=Q(confidence__gte=0, confidence__lte=1),
                name="orgmem_claim_link_conf_range",
            ),
        ]
        ordering = ("created_at",)

    def clean(self):
        if self.from_claim.organization_id != self.organization_id or self.to_claim.organization_id != self.organization_id:
            raise ValidationError("Linked claims must belong to the link organization.")


class MemoryClaimStateEvent(ImmutableEvidenceMixin):
    """Append-only claim lifecycle history; consolidation owns later transitions."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    claim = models.ForeignKey(MemoryClaim, on_delete=models.CASCADE, related_name="state_events")
    from_status = models.CharField(
        max_length=16,
        choices=MemoryClaimStatus.choices,
        blank=True,
        default="",
    )
    to_status = models.CharField(max_length=16, choices=MemoryClaimStatus.choices)
    reason = models.CharField(max_length=512)
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memory_claim_state_events",
    )
    review_item = models.ForeignKey(
        "MemoryReviewItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="claim_state_events",
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    immutable_fields = (
        "claim_id",
        "from_status",
        "to_status",
        "reason",
        "actor_user_id",
        "review_item_id",
        "metadata",
        "created_at",
    )

    class Meta:
        indexes = [
            models.Index(fields=("claim", "created_at"), name="orgmem_claim_state_time"),
        ]
        ordering = ("claim", "created_at")


class MemoryConsolidationRun(ImmutableEvidenceMixin):
    """Versioned consolidation decision; application code applies its deterministic effect."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_consolidation_runs",
    )
    candidate_claim = models.ForeignKey(
        MemoryClaim,
        on_delete=models.CASCADE,
        related_name="consolidation_runs",
    )
    matched_claim = models.ForeignKey(
        MemoryClaim,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="matched_consolidation_runs",
    )
    idempotency_key = models.CharField(max_length=64, unique=True)
    operation = models.CharField(max_length=16, choices=MemoryConsolidationOperation.choices)
    status = models.CharField(
        max_length=24,
        choices=MemoryConsolidationStatus.choices,
        db_index=True,
    )
    confidence = models.DecimalField(max_digits=4, decimal_places=3)
    reason = models.CharField(max_length=1000)
    deterministic = models.BooleanField(default=False)
    consolidator_version = models.CharField(max_length=64)
    schema_version = models.CharField(max_length=64)
    prompt_version = models.CharField(max_length=64)
    model = models.CharField(max_length=128)
    prompt_input_hash = models.CharField(max_length=64)
    output_hash = models.CharField(max_length=64)
    provider_response_id = models.CharField(max_length=255, blank=True, default="")
    usage = models.JSONField(default=dict, blank=True)
    review_item = models.ForeignKey(
        "MemoryReviewItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="consolidation_runs",
    )
    completed_at = models.DateTimeField(default=timezone.now, db_index=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    immutable_fields = (
        "organization_id",
        "candidate_claim_id",
        "matched_claim_id",
        "idempotency_key",
        "operation",
        "confidence",
        "reason",
        "deterministic",
        "consolidator_version",
        "schema_version",
        "prompt_version",
        "model",
        "prompt_input_hash",
        "output_hash",
        "provider_response_id",
        "usage",
        "completed_at",
    )

    class Meta:
        indexes = [
            models.Index(
                fields=("organization", "status", "completed_at"),
                name="orgmem_consol_org_status",
            ),
            models.Index(
                fields=("candidate_claim", "consolidator_version"),
                name="orgmem_consol_claim_ver",
            ),
        ]
        ordering = ("-completed_at",)

    def clean(self):
        if self.candidate_claim.organization_id != self.organization_id:
            raise ValidationError("Consolidation candidate must match its organization.")
        if self.matched_claim_id and self.matched_claim.organization_id != self.organization_id:
            raise ValidationError("Matched consolidation claim must match its organization.")
        if self.matched_claim_id == self.candidate_claim_id:
            raise ValidationError("A claim cannot be consolidated against itself.")


class MemoryCurrentState(models.Model):
    """Deterministic projection of the best known state for one entity/key."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_current_states",
    )
    scope_entity = models.ForeignKey(
        MemoryEntity,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="current_states",
    )
    scope_key = models.CharField(max_length=255)
    state_key = models.CharField(max_length=320)
    claim = models.ForeignKey(MemoryClaim, on_delete=models.CASCADE, related_name="current_state_rows")
    state_value = models.JSONField(default=dict)
    valid_as_of = models.DateTimeField(db_index=True)
    is_stale = models.BooleanField(default=False, db_index=True)
    has_conflict = models.BooleanField(default=False, db_index=True)
    warnings = models.JSONField(default=list, blank=True)
    distinct_source_count = models.PositiveIntegerField(default=1)
    refreshed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "scope_key", "state_key"),
                name="orgmem_current_state_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "is_stale", "has_conflict"),
                name="orgmem_current_warnings",
            ),
        ]
        ordering = ("organization", "scope_key", "state_key")

    def clean(self):
        if self.claim.organization_id != self.organization_id:
            raise ValidationError("Current-state claim must match its organization.")
        if self.scope_entity_id and self.scope_entity.organization_id != self.organization_id:
            raise ValidationError("Current-state entity must match its organization.")


class MemoryEntityResolutionEvent(ImmutableEvidenceMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_entity_resolution_events",
    )
    primary_entity = models.ForeignKey(
        MemoryEntity,
        on_delete=models.CASCADE,
        related_name="primary_resolution_events",
    )
    secondary_entity = models.ForeignKey(
        MemoryEntity,
        on_delete=models.CASCADE,
        related_name="secondary_resolution_events",
    )
    operation = models.CharField(max_length=16, choices=MemoryEntityResolutionOperation.choices)
    reason = models.CharField(max_length=1000)
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memory_entity_resolution_events",
    )
    review_item = models.ForeignKey(
        "MemoryReviewItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="entity_resolution_events",
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    immutable_fields = (
        "organization_id",
        "primary_entity_id",
        "secondary_entity_id",
        "operation",
        "reason",
        "actor_user_id",
        "review_item_id",
        "metadata",
        "created_at",
    )

    class Meta:
        indexes = [
            models.Index(
                fields=("organization", "operation", "created_at"),
                name="orgmem_entity_resolution",
            ),
        ]
        ordering = ("-created_at",)


class MemoryCorrectionProposal(models.Model):
    """A correction request that can only apply an independently evidenced replacement claim."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_correction_proposals",
    )
    original_claim = models.ForeignKey(
        MemoryClaim,
        on_delete=models.CASCADE,
        related_name="correction_proposals",
    )
    replacement_claim = models.ForeignKey(
        MemoryClaim,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="replacement_for_corrections",
    )
    correction_text = models.TextField()
    status = models.CharField(
        max_length=16,
        choices=MemoryCorrectionStatus.choices,
        default=MemoryCorrectionStatus.PROPOSED,
        db_index=True,
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memory_corrections_requested",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memory_corrections_reviewed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_item = models.ForeignKey(
        "MemoryReviewItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="correction_proposals",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("organization", "status", "created_at"),
                name="orgmem_correction_queue",
            ),
        ]
        ordering = ("-created_at",)

    def clean(self):
        if self.original_claim.organization_id != self.organization_id:
            raise ValidationError("Correction claim must match its organization.")
        if self.replacement_claim_id and self.replacement_claim.organization_id != self.organization_id:
            raise ValidationError("Correction replacement must match its organization.")
        if self.replacement_claim_id == self.original_claim_id:
            raise ValidationError("A correction must reference a different replacement claim.")
        if not self.correction_text or len(self.correction_text) > 4000:
            raise ValidationError("Correction text must contain between 1 and 4,000 characters.")


class MemoryQueryLog(ImmutableEvidenceMixin):
    """Append-only, redacted trace of one private organisational-memory query."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_query_logs",
    )
    audience = models.CharField(max_length=32, default="committee")
    requester_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memory_query_logs",
    )
    requester_slack_id = models.CharField(max_length=32, blank=True, default="")
    channel_id = models.CharField(max_length=32, blank=True, default="")
    request_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    query = models.TextField()
    query_hash = models.CharField(max_length=64)
    query_plan = models.JSONField(default=dict)
    as_of = models.DateTimeField(null=True, blank=True)
    candidate_trace = models.JSONField(default=list, blank=True)
    selected_claim_ids = models.JSONField(default=list, blank=True)
    selected_chunk_ids = models.JSONField(default=list, blank=True)
    answer = models.TextField(blank=True, default="")
    citation_data = models.JSONField(default=list, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=16, choices=MemoryQueryStatus.choices, db_index=True)
    evidence_sufficiency = models.CharField(
        max_length=16,
        choices=MemoryEvidenceSufficiency.choices,
        db_index=True,
    )
    confidence = models.DecimalField(max_digits=4, decimal_places=3, default=0)
    selector_version = models.CharField(max_length=64)
    embedding_model = models.CharField(max_length=128, blank=True, default="")
    embedding_version = models.CharField(max_length=128, blank=True, default="")
    model_name = models.CharField(max_length=128, blank=True, default="")
    answerer_version = models.CharField(max_length=64, blank=True, default="")
    prompt_version = models.CharField(max_length=64, blank=True, default="")
    schema_version = models.CharField(max_length=64, blank=True, default="")
    provider_response_id = models.CharField(max_length=255, blank=True, default="")
    latency_ms = models.PositiveIntegerField(default=0)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    immutable_fields = (
        "organization_id",
        "audience",
        "requester_user_id",
        "requester_slack_id",
        "channel_id",
        "request_id",
        "query",
        "query_hash",
        "query_plan",
        "as_of",
        "candidate_trace",
        "selected_claim_ids",
        "selected_chunk_ids",
        "answer",
        "citation_data",
        "warnings",
        "status",
        "evidence_sufficiency",
        "confidence",
        "selector_version",
        "embedding_model",
        "embedding_version",
        "model_name",
        "answerer_version",
        "prompt_version",
        "schema_version",
        "provider_response_id",
        "latency_ms",
        "input_tokens",
        "output_tokens",
    )

    class Meta:
        indexes = [
            models.Index(
                fields=("organization", "created_at"),
                name="orgmem_query_org_created",
            ),
            models.Index(
                fields=("organization", "requester_user", "created_at"),
                name="orgmem_query_user_created",
            ),
        ]
        ordering = ("-created_at",)


class MemoryFeedback(ImmutableEvidenceMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_feedback",
    )
    query_log = models.ForeignKey(
        MemoryQueryLog,
        on_delete=models.CASCADE,
        related_name="feedback",
    )
    claim = models.ForeignKey(
        MemoryClaim,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="feedback",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memory_feedback",
    )
    feedback_type = models.CharField(max_length=16, choices=MemoryFeedbackType.choices)
    correction_text = models.TextField(blank=True, default="")
    correction_proposal = models.ForeignKey(
        MemoryCorrectionProposal,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="feedback",
    )
    request_id = models.CharField(max_length=128, blank=True, default="")
    idempotency_key = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    immutable_fields = (
        "organization_id",
        "query_log_id",
        "claim_id",
        "user_id",
        "feedback_type",
        "correction_text",
        "correction_proposal_id",
        "request_id",
        "idempotency_key",
    )

    class Meta:
        indexes = [
            models.Index(
                fields=("organization", "feedback_type", "created_at"),
                name="orgmem_feedback_queue",
            ),
        ]
        ordering = ("-created_at",)

    def clean(self):
        if self.query_log_id and self.query_log.organization_id != self.organization_id:
            raise ValidationError("Feedback query must match its organization.")
        if self.claim_id and self.claim.organization_id != self.organization_id:
            raise ValidationError("Feedback claim must match its organization.")
        if (
            self.correction_proposal_id
            and self.correction_proposal.organization_id != self.organization_id
        ):
            raise ValidationError("Feedback correction must match its organization.")
        if len(self.correction_text or "") > 4000:
            raise ValidationError("Feedback correction text cannot exceed 4,000 characters.")


class MemoryPilotQueryAudit(ImmutableEvidenceMixin):
    """Immutable, content-free human assessment of one pilot query."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_pilot_query_audits",
    )
    query_log = models.ForeignKey(
        MemoryQueryLog,
        on_delete=models.CASCADE,
        related_name="pilot_audits",
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="reviewed_memory_pilot_queries",
    )
    rubric_version = models.CharField(max_length=64)
    risk = models.CharField(
        max_length=16,
        choices=MemoryPilotAuditRisk.choices,
        default=MemoryPilotAuditRisk.STANDARD,
        db_index=True,
    )
    answer_correct = models.BooleanField(null=True, blank=True)
    faithfulness_correct = models.BooleanField(null=True, blank=True)
    abstention_correct = models.BooleanField()
    current_state_correct = models.BooleanField(null=True, blank=True)
    temporal_correct = models.BooleanField(null=True, blank=True)
    citation_count = models.PositiveIntegerField(default=0)
    correct_citation_count = models.PositiveIntegerField(default=0)
    permission_leak = models.BooleanField(default=False, db_index=True)
    public_admin_leak = models.BooleanField(default=False, db_index=True)
    idempotency_key = models.CharField(max_length=128)
    batch_hash = models.CharField(max_length=64)
    reviewed_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    immutable_fields = (
        "organization_id",
        "query_log_id",
        "reviewer_id",
        "rubric_version",
        "risk",
        "answer_correct",
        "faithfulness_correct",
        "abstention_correct",
        "current_state_correct",
        "temporal_correct",
        "citation_count",
        "correct_citation_count",
        "permission_leak",
        "public_admin_leak",
        "idempotency_key",
        "batch_hash",
        "reviewed_at",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "idempotency_key"),
                name="orgmem_pilot_audit_idem_uniq",
            ),
            models.UniqueConstraint(
                fields=("query_log", "rubric_version"),
                name="orgmem_pilot_audit_query_rubric_uniq",
            ),
            models.CheckConstraint(
                check=Q(correct_citation_count__lte=models.F("citation_count")),
                name="orgmem_pilot_audit_citations_valid",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "rubric_version", "reviewed_at"),
                name="orgmem_pilot_audit_window",
            ),
        ]
        ordering = ("-reviewed_at",)

    def clean(self):
        if self.query_log_id and self.query_log.organization_id != self.organization_id:
            raise ValidationError("Pilot audit query must match its organization.")
        if self.reviewer_id and self.query_log.requester_user_id == self.reviewer_id:
            raise ValidationError("Pilot query audits require an independent reviewer.")
        if self.reviewed_at and self.query_log_id:
            if self.reviewed_at < self.query_log.created_at:
                raise ValidationError("Pilot audit cannot predate its query.")
        if self.correct_citation_count > self.citation_count:
            raise ValidationError("Correct citation count cannot exceed citation count.")

        query_status = self.query_log.status if self.query_log_id else ""
        actual_citation_count = (
            len(self.query_log.citation_data or ()) if self.query_log_id else 0
        )
        if query_status == MemoryQueryStatus.ANSWERED:
            if self.answer_correct is None or self.faithfulness_correct is None:
                raise ValidationError(
                    "Answered pilot queries require correctness and faithfulness review."
                )
            if self.citation_count != actual_citation_count:
                raise ValidationError(
                    "Pilot audit citation count must match the recorded query."
                )
        elif query_status == MemoryQueryStatus.ABSTAINED:
            if (
                self.answer_correct is not None
                or self.faithfulness_correct is not None
                or self.citation_count
                or self.correct_citation_count
            ):
                raise ValidationError(
                    "Abstained pilot queries cannot carry answer or citation scores."
                )
        elif self.query_log_id:
            raise ValidationError(
                "Only answered or abstained pilot queries can receive an official audit."
            )

        query_mode = (
            str((self.query_log.query_plan or {}).get("mode") or "").upper()
            if self.query_log_id
            else ""
        )
        if query_mode == MemoryQueryMode.CURRENT_STATE.upper():
            if self.current_state_correct is None:
                raise ValidationError(
                    "Current-state queries require a current-state assessment."
                )
        elif self.current_state_correct is not None:
            raise ValidationError(
                "Current-state assessment is only valid for current-state queries."
            )
        temporal_modes = {
            MemoryQueryMode.HISTORICAL_AS_OF.upper(),
            MemoryQueryMode.TIMELINE.upper(),
        }
        if query_mode in temporal_modes:
            if self.temporal_correct is None:
                raise ValidationError(
                    "Historical and timeline queries require a temporal assessment."
                )
        elif self.temporal_correct is not None:
            raise ValidationError(
                "Temporal assessment is only valid for historical or timeline queries."
            )


class MemoryPilotDeployment(ImmutableEvidenceMixin):
    """Approval-bound runtime allowlist for one staged or active pilot."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_pilot_deployments",
    )
    state = models.CharField(
        max_length=16,
        choices=MemoryPilotDeploymentState.choices,
        default=MemoryPilotDeploymentState.STAGED,
        db_index=True,
    )
    approval_manifest_hash = models.CharField(max_length=64, db_index=True)
    approval_review_due_at = models.DateTimeField(db_index=True)
    allowlist_key_version = models.CharField(max_length=64)
    actor_ref_hashes = models.JSONField(default=list)
    context_ref_hashes = models.JSONField(default=list)
    approved_provider_count = models.PositiveIntegerField()
    approved_source_scope_count = models.PositiveIntegerField()
    stage_idempotency_key = models.CharField(max_length=128)
    activation_idempotency_key = models.CharField(
        max_length=128,
        blank=True,
        default="",
    )
    staged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="staged_memory_pilot_deployments",
    )
    staged_at = models.DateTimeField(default=timezone.now, db_index=True)
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activated_memory_pilot_deployments",
    )
    activated_at = models.DateTimeField(null=True, blank=True, db_index=True)
    suspended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="suspended_memory_pilot_deployments",
    )
    suspended_at = models.DateTimeField(null=True, blank=True, db_index=True)
    suspension_reason = models.CharField(
        max_length=32,
        choices=MemoryPilotSuspensionReason.choices,
        blank=True,
        default="",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    immutable_fields = (
        "organization_id",
        "approval_manifest_hash",
        "approval_review_due_at",
        "allowlist_key_version",
        "actor_ref_hashes",
        "context_ref_hashes",
        "approved_provider_count",
        "approved_source_scope_count",
        "stage_idempotency_key",
        "staged_by_id",
        "staged_at",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "stage_idempotency_key"),
                name="orgmem_pilot_stage_idem_uniq",
            ),
            models.UniqueConstraint(
                fields=(
                    "organization",
                    "approval_manifest_hash",
                    "allowlist_key_version",
                ),
                name="orgmem_pilot_approval_key_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization",),
                condition=Q(state=MemoryPilotDeploymentState.STAGED),
                name="orgmem_pilot_one_staged",
            ),
            models.UniqueConstraint(
                fields=("organization",),
                condition=Q(state=MemoryPilotDeploymentState.ACTIVE),
                name="orgmem_pilot_one_active",
            ),
            models.UniqueConstraint(
                fields=("organization", "activation_idempotency_key"),
                condition=~Q(activation_idempotency_key=""),
                name="orgmem_pilot_activate_idem_uniq",
            ),
            models.CheckConstraint(
                check=(
                    Q(
                        state=MemoryPilotDeploymentState.STAGED,
                        activated_at__isnull=True,
                        suspended_at__isnull=True,
                        suspension_reason="",
                    )
                    | Q(
                        state=MemoryPilotDeploymentState.ACTIVE,
                        activated_at__isnull=False,
                        suspended_at__isnull=True,
                        suspension_reason="",
                    )
                    | Q(
                        state=MemoryPilotDeploymentState.SUSPENDED,
                        suspended_at__isnull=False,
                    )
                ),
                name="orgmem_pilot_state_timestamps",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "state", "approval_review_due_at"),
                name="orgmem_pilot_runtime_state",
            ),
        ]
        ordering = ("-created_at",)

    def clean(self):
        digest_re = re.compile(r"^[a-f0-9]{64}$")
        version_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
        if not digest_re.fullmatch(str(self.approval_manifest_hash or "")):
            raise ValidationError("Pilot deployment approval hash is invalid.")
        if not version_re.fullmatch(str(self.allowlist_key_version or "")):
            raise ValidationError("Pilot deployment allowlist key version is invalid.")
        actor_hashes = self.actor_ref_hashes
        context_hashes = self.context_ref_hashes
        if (
            not isinstance(actor_hashes, list)
            or not 1 <= len(actor_hashes) <= 3
            or actor_hashes != sorted(set(actor_hashes))
            or any(not digest_re.fullmatch(str(value)) for value in actor_hashes)
        ):
            raise ValidationError("Pilot deployment actor allowlist is invalid.")
        if (
            not isinstance(context_hashes, list)
            or not context_hashes
            or context_hashes != sorted(set(context_hashes))
            or any(not digest_re.fullmatch(str(value)) for value in context_hashes)
        ):
            raise ValidationError("Pilot deployment context allowlist is invalid.")
        if not self.approved_provider_count or not self.approved_source_scope_count:
            raise ValidationError("Pilot deployment requires approved providers and scopes.")
        if (
            self.approval_review_due_at
            and self.staged_at
            and self.approval_review_due_at <= self.staged_at
        ):
            raise ValidationError("Pilot deployment approval is expired.")
        if (
            self.state == MemoryPilotDeploymentState.STAGED
            and (
                self.activated_at
                or self.suspended_at
                or self.suspension_reason
                or self.activation_idempotency_key
            )
        ):
            raise ValidationError("Staged pilot deployment lifecycle is invalid.")
        if self.state == MemoryPilotDeploymentState.ACTIVE:
            if (
                not self.activated_at
                or self.suspended_at
                or self.suspension_reason
                or not self.activation_idempotency_key
            ):
                raise ValidationError("Active pilot deployment lifecycle is invalid.")
            if (
                self.staged_by_id
                and self.activated_by_id
                and self.staged_by_id == self.activated_by_id
            ):
                raise ValidationError(
                    "Pilot activation requires an independent operator."
                )
        if self.state == MemoryPilotDeploymentState.SUSPENDED:
            if not self.suspended_at or not self.suspension_reason:
                raise ValidationError("Suspended pilot deployment lifecycle is invalid.")


class MemorySelectorShadowRun(models.Model):
    """Content-minimised comparison of one learned artifact with production traces."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_selector_shadow_runs",
    )
    status = models.CharField(
        max_length=16,
        choices=MemorySelectorShadowRunStatus.choices,
        db_index=True,
    )
    baseline_selector_version = models.CharField(max_length=64)
    learned_selector_version = models.CharField(max_length=128)
    feature_schema_version = models.CharField(max_length=64)
    dataset_hash = models.CharField(max_length=64, db_index=True)
    model_artifact_hash = models.CharField(max_length=64)
    minimum_required_traces = models.PositiveIntegerField(default=3000)
    eligible_trace_count = models.PositiveIntegerField(default=0)
    labeled_trace_count = models.PositiveIntegerField(default=0)
    evaluated_trace_count = models.PositiveIntegerField(default=0)
    metrics = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default="")
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=(
                    "organization",
                    "dataset_hash",
                    "model_artifact_hash",
                    "learned_selector_version",
                ),
                name="orgmem_selector_shadow_run_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "status", "created_at"),
                name="orgmem_selector_shadow_status",
            ),
        ]
        ordering = ("-created_at",)


class MemorySelectorShadowResult(ImmutableEvidenceMixin):
    """Per-query hashes and metrics only; no query, answer, source, or candidate IDs."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(
        MemorySelectorShadowRun,
        on_delete=models.CASCADE,
        related_name="results",
    )
    query_log = models.ForeignKey(
        MemoryQueryLog,
        on_delete=models.CASCADE,
        related_name="selector_shadow_results",
    )
    query_ref = models.CharField(max_length=64)
    candidate_count = models.PositiveIntegerField(default=0)
    labeled_candidate_count = models.PositiveIntegerField(default=0)
    baseline_order_hash = models.CharField(max_length=64)
    shadow_order_hash = models.CharField(max_length=64)
    top_k_overlap = models.FloatField(default=0)
    baseline_ndcg = models.FloatField(null=True, blank=True)
    shadow_ndcg = models.FloatField(null=True, blank=True)
    baseline_pairwise_accuracy = models.FloatField(null=True, blank=True)
    shadow_pairwise_accuracy = models.FloatField(null=True, blank=True)
    disagreement = models.BooleanField(default=False, db_index=True)
    latency_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    immutable_fields = (
        "run_id",
        "query_log_id",
        "query_ref",
        "candidate_count",
        "labeled_candidate_count",
        "baseline_order_hash",
        "shadow_order_hash",
        "top_k_overlap",
        "baseline_ndcg",
        "shadow_ndcg",
        "baseline_pairwise_accuracy",
        "shadow_pairwise_accuracy",
        "disagreement",
        "latency_ms",
        "created_at",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("run", "query_log"),
                name="orgmem_selector_shadow_result_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("run", "disagreement"),
                name="orgmem_selector_disagree",
            ),
        ]
        ordering = ("run", "query_ref")

    def clean(self):
        if self.run_id and self.query_log_id:
            if self.run.organization_id != self.query_log.organization_id:
                raise ValidationError(
                    "Selector shadow result must stay within one organization."
                )


class MemoryReviewItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_review_items",
    )
    review_type = models.CharField(max_length=32, choices=MemoryReviewType.choices)
    target_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.PROTECT,
        related_name="org_memory_review_targets",
    )
    target_object_id = models.CharField(max_length=64)
    target = GenericForeignKey("target_content_type", "target_object_id")
    severity = models.CharField(
        max_length=16,
        choices=MemoryReviewSeverity.choices,
        default=MemoryReviewSeverity.NORMAL,
        db_index=True,
    )
    reason = models.TextField()
    status = models.CharField(
        max_length=16,
        choices=MemoryReviewStatus.choices,
        default=MemoryReviewStatus.OPEN,
        db_index=True,
    )
    idempotency_key = models.CharField(max_length=255)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_memory_review_items",
    )
    due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    resolution = models.JSONField(default=dict, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_memory_review_items",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "idempotency_key"),
                name="orgmem_review_idempotency_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "status", "severity", "due_at"),
                name="orgmem_review_queue",
            ),
        ]
        ordering = ("-severity", "due_at", "created_at")

    def __str__(self):
        return f"{self.review_type}: {self.target_content_type_id}/{self.target_object_id}"


class MemorySummary(models.Model):
    """Deterministic, evidence-linked roll-up produced after reconciliation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_summaries",
    )
    summary_type = models.CharField(
        max_length=16,
        choices=MemorySummaryType.choices,
        db_index=True,
    )
    subject_key = models.CharField(max_length=512, db_index=True)
    title = models.CharField(max_length=512)
    body = models.TextField(blank=True, default="")
    structured_data = models.JSONField(default=dict, blank=True)
    required_classifications = models.JSONField(default=list, blank=True)
    window_start = models.DateTimeField(db_index=True)
    window_end = models.DateTimeField(db_index=True)
    generation_key = models.CharField(max_length=255)
    fingerprint = models.CharField(max_length=64, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=MemoryDerivedArtifactStatus.choices,
        default=MemoryDerivedArtifactStatus.READY,
        db_index=True,
    )
    source_report = models.ForeignKey(
        MemoryDailyReconciliationReport,
        on_delete=models.CASCADE,
        related_name="summaries",
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children",
    )
    is_current = models.BooleanField(default=True, db_index=True)
    generated_at = models.DateTimeField(default=timezone.now, db_index=True)
    invalidated_at = models.DateTimeField(null=True, blank=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "generation_key"),
                name="orgmem_summary_generation_uniq",
            ),
            models.CheckConstraint(
                check=Q(window_end__gt=models.F("window_start")),
                name="orgmem_summary_window_valid",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "summary_type", "is_current", "window_end"),
                name="orgmem_summary_org_type_cur",
            ),
        ]
        ordering = ("-window_end", "summary_type", "subject_key")

    def __str__(self):
        return f"{self.summary_type}: {self.title}"

    def clean(self):
        if self.source_report.organization_id != self.organization_id:
            raise ValidationError("Summary report must match its organization.")
        if self.parent_id and self.parent.organization_id != self.organization_id:
            raise ValidationError("Summary parent must match its organization.")


class MemorySummaryClaim(models.Model):
    summary = models.ForeignKey(
        MemorySummary,
        on_delete=models.CASCADE,
        related_name="claim_links",
    )
    claim = models.ForeignKey(
        MemoryClaim,
        on_delete=models.CASCADE,
        related_name="summary_links",
    )
    ordinal = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("summary", "claim"),
                name="orgmem_summary_claim_uniq",
            ),
            models.UniqueConstraint(
                fields=("summary", "ordinal"),
                name="orgmem_summary_claim_ord_uniq",
            ),
        ]
        ordering = ("summary", "ordinal")

    def clean(self):
        if self.summary.organization_id != self.claim.organization_id:
            raise ValidationError("Summary claim must match its organization.")


class MemorySummaryEvidence(models.Model):
    summary = models.ForeignKey(
        MemorySummary,
        on_delete=models.CASCADE,
        related_name="evidence_links",
    )
    evidence = models.ForeignKey(
        MemoryEvidence,
        on_delete=models.CASCADE,
        related_name="summary_links",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("summary", "evidence"),
                name="orgmem_summary_evidence_uniq",
            ),
        ]
        ordering = ("summary", "evidence")

    def clean(self):
        if self.summary.organization_id != self.evidence.claim.organization_id:
            raise ValidationError("Summary evidence must match its organization.")


class MemoryDigest(models.Model):
    """A reconciliation-gated daily or weekly operator briefing."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_digests",
    )
    digest_type = models.CharField(
        max_length=32,
        choices=MemoryDigestType.choices,
        db_index=True,
    )
    digest_date = models.DateField(db_index=True)
    time_zone = models.CharField(max_length=64)
    window_start = models.DateTimeField(db_index=True)
    window_end = models.DateTimeField(db_index=True)
    title = models.CharField(max_length=512)
    body = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=16,
        choices=MemoryDerivedArtifactStatus.choices,
        default=MemoryDerivedArtifactStatus.READY,
        db_index=True,
    )
    warnings = models.JSONField(default=list, blank=True)
    required_classifications = models.JSONField(default=list, blank=True)
    source_report = models.ForeignKey(
        MemoryDailyReconciliationReport,
        on_delete=models.CASCADE,
        related_name="digests",
    )
    idempotency_key = models.CharField(max_length=255)
    generated_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "idempotency_key"),
                name="orgmem_digest_idempotency_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "digest_type", "digest_date"),
                name="orgmem_digest_org_type_date_uniq",
            ),
            models.CheckConstraint(
                check=Q(window_end__gt=models.F("window_start")),
                name="orgmem_digest_window_valid",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "digest_type", "status", "digest_date"),
                name="orgmem_digest_org_type_state",
            ),
        ]
        ordering = ("-digest_date", "digest_type")

    def __str__(self):
        return f"{self.digest_type}: {self.digest_date}/{self.status}"

    def clean(self):
        if self.source_report.organization_id != self.organization_id:
            raise ValidationError("Digest report must match its organization.")


class MemoryDigestItem(models.Model):
    digest = models.ForeignKey(
        MemoryDigest,
        on_delete=models.CASCADE,
        related_name="items",
    )
    claim = models.ForeignKey(
        MemoryClaim,
        on_delete=models.CASCADE,
        related_name="digest_items",
    )
    summary = models.ForeignKey(
        MemorySummary,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="digest_items",
    )
    ordinal = models.PositiveIntegerField()
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("digest", "claim"),
                name="orgmem_digest_claim_uniq",
            ),
            models.UniqueConstraint(
                fields=("digest", "ordinal"),
                name="orgmem_digest_item_order_uniq",
            ),
        ]
        ordering = ("digest", "ordinal")

    def clean(self):
        if self.digest.organization_id != self.claim.organization_id:
            raise ValidationError("Digest claim must match its organization.")
        if self.summary_id and self.summary.organization_id != self.digest.organization_id:
            raise ValidationError("Digest summary must match its organization.")


class MemoryDigestItemEvidence(models.Model):
    item = models.ForeignKey(
        MemoryDigestItem,
        on_delete=models.CASCADE,
        related_name="evidence_links",
    )
    evidence = models.ForeignKey(
        MemoryEvidence,
        on_delete=models.CASCADE,
        related_name="digest_item_links",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("item", "evidence"),
                name="orgmem_digest_item_ev_uniq",
            ),
        ]
        ordering = ("item", "evidence")

    def clean(self):
        if self.item.digest.organization_id != self.evidence.claim.organization_id:
            raise ValidationError("Digest evidence must match its organization.")


class PublicKnowledgeItem(models.Model):
    """A deliberately published snapshot with no private-memory foreign keys."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="public_knowledge_items",
    )
    public_key = models.SlugField(max_length=160)
    revision = models.PositiveIntegerField()
    title = models.CharField(max_length=300)
    body = models.TextField()
    tags = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=16,
        choices=PublicKnowledgeStatus.choices,
        default=PublicKnowledgeStatus.ACTIVE,
        db_index=True,
    )
    content_hash = models.CharField(max_length=64, db_index=True)
    search_vector = SearchVectorField(null=True, editable=False)
    embedding = VectorField(dimensions=1536, null=True, blank=True)
    embedding_model = models.CharField(max_length=128, blank=True, default="")
    embedding_version = models.CharField(max_length=64, blank=True, default="")
    embedding_hash = models.CharField(max_length=64, blank=True, default="")
    published_at = models.DateTimeField(default=timezone.now, db_index=True)
    superseded_at = models.DateTimeField(null=True, blank=True, db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "public_key", "revision"),
                name="orgmem_public_item_revision_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "public_key"),
                condition=Q(status=PublicKnowledgeStatus.ACTIVE),
                name="orgmem_public_item_one_active",
            ),
            models.CheckConstraint(
                check=Q(revision__gte=1),
                name="orgmem_public_item_revision_pos",
            ),
            models.CheckConstraint(
                check=(
                    Q(
                        status=PublicKnowledgeStatus.ACTIVE,
                        superseded_at__isnull=True,
                        revoked_at__isnull=True,
                    )
                    | Q(
                        status=PublicKnowledgeStatus.SUPERSEDED,
                        superseded_at__isnull=False,
                    )
                    | Q(
                        status=PublicKnowledgeStatus.REVOKED,
                        revoked_at__isnull=False,
                    )
                ),
                name="orgmem_public_item_state_time",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "status", "published_at"),
                name="orgmem_public_item_active",
            ),
            models.Index(
                fields=("organization", "public_key", "revision"),
                name="orgmem_public_item_key_rev",
            ),
            GinIndex(
                fields=("search_vector",),
                name="orgmem_public_search_gin",
            ),
            HnswIndex(
                fields=("embedding",),
                name="orgmem_public_vector_hnsw",
                m=16,
                ef_construction=64,
                opclasses=("vector_cosine_ops",),
            ),
        ]
        ordering = ("organization", "public_key", "-revision")

    def __str__(self):
        return f"{self.public_key}@{self.revision}: {self.status}"

    def clean(self):
        if not str(self.title or "").strip() or not str(self.body or "").strip():
            raise ValidationError("Published public knowledge requires a title and body.")
        if len(self.body) > 20000:
            raise ValidationError("Published public knowledge is limited to 20,000 characters.")
        if not isinstance(self.tags, list):
            raise ValidationError("Public knowledge tags must be a list.")
        embedding_fields = (
            self.embedding_model,
            self.embedding_version,
            self.embedding_hash,
        )
        if self.embedding is None and any(embedding_fields):
            raise ValidationError("Public embedding metadata requires an embedding.")
        if self.embedding is not None and not all(embedding_fields):
            raise ValidationError("A public embedding requires complete model metadata.")


class MemoryPublication(models.Model):
    """Private review/audit bridge to a physically separate public snapshot."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_publications",
    )
    source_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.PROTECT,
        related_name="org_memory_publication_sources",
    )
    source_object_id = models.CharField(max_length=64)
    source = GenericForeignKey("source_content_type", "source_object_id")
    source_fingerprint = models.CharField(max_length=64)
    public_key = models.SlugField(max_length=160)
    proposed_title = models.CharField(max_length=300)
    proposed_body = models.TextField()
    proposed_tags = models.JSONField(default=list, blank=True)
    proposal_hash = models.CharField(max_length=64, db_index=True)
    sensitivity_findings = models.JSONField(default=list, blank=True)
    redaction_notes = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=24,
        choices=MemoryPublicationStatus.choices,
        default=MemoryPublicationStatus.DRAFT,
        db_index=True,
    )
    idempotency_key = models.CharField(max_length=255)
    creation_request_hash = models.CharField(max_length=64)
    review_item = models.OneToOneField(
        MemoryReviewItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="publication",
    )
    published_item = models.OneToOneField(
        PublicKnowledgeItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="proposed_memory_publications",
    )
    redaction_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="redaction_confirmed_memory_publications",
    )
    redaction_confirmed_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_memory_publications",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="revoked_memory_publications",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    revocation_reason = models.CharField(max_length=512, blank=True, default="")
    revocation_idempotency_key = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "idempotency_key"),
                name="orgmem_publication_idempotency_uniq",
            ),
            models.CheckConstraint(
                check=(
                    ~Q(status=MemoryPublicationStatus.PENDING_REVIEW)
                    | Q(
                        review_item__isnull=False,
                        redaction_confirmed_at__isnull=False,
                    )
                ),
                name="orgmem_publication_review_ready",
            ),
            models.CheckConstraint(
                check=(
                    ~Q(status=MemoryPublicationStatus.PUBLISHED)
                    | Q(
                        published_item__isnull=False,
                        approved_at__isnull=False,
                    )
                ),
                name="orgmem_publication_published",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "status", "created_at"),
                name="orgmem_publication_queue",
            ),
            models.Index(
                fields=("organization", "public_key", "created_at"),
                name="orgmem_publication_key",
            ),
            models.Index(
                fields=("source_content_type", "source_object_id"),
                name="orgmem_publication_source",
            ),
        ]
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.public_key}: {self.status}"

    def clean(self):
        source_organization_id = getattr(self.source, "organization_id", None)
        if source_organization_id != self.organization_id:
            raise ValidationError("Publication source must match its organization.")
        if not isinstance(self.proposed_tags, list):
            raise ValidationError("Publication tags must be a list.")
        if len(self.proposed_body or "") > 20000:
            raise ValidationError("Publication body is limited to 20,000 characters.")
        if self.review_item_id and self.review_item.organization_id != self.organization_id:
            raise ValidationError("Publication review must match its organization.")
        if self.published_item_id and (
            self.published_item.organization_id != self.organization_id
            or self.published_item.public_key != self.public_key
        ):
            raise ValidationError("Published item must match the publication organization and key.")


class MemoryPublicationEvent(ImmutableEvidenceMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    publication = models.ForeignKey(
        MemoryPublication,
        on_delete=models.CASCADE,
        related_name="events",
    )
    event_type = models.CharField(
        max_length=32,
        choices=MemoryPublicationEventType.choices,
        db_index=True,
    )
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memory_publication_events",
    )
    payload_hash = models.CharField(max_length=64, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    immutable_fields = (
        "publication_id",
        "event_type",
        "actor_user_id",
        "payload_hash",
        "metadata",
        "created_at",
    )

    class Meta:
        indexes = [
            models.Index(
                fields=("publication", "created_at"),
                name="orgmem_publication_event_time",
            ),
        ]
        ordering = ("publication", "created_at")

    def __str__(self):
        return f"{self.publication_id}: {self.event_type}"


class AgentActionProposal(ImmutableEvidenceMixin):
    """A governed request to draft or mutate one explicitly supported system."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="agent_action_proposals",
    )
    configuration = models.ForeignKey(
        MemoryConnectionConfiguration,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="agent_action_proposals",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="requested_agent_actions",
    )
    requested_by_slack_id = models.CharField(max_length=32, blank=True, default="")
    action_type = models.CharField(
        max_length=32,
        choices=AgentActionType.choices,
        db_index=True,
    )
    target_system = models.CharField(max_length=32, db_index=True)
    input_payload = models.JSONField(default=dict)
    input_hash = models.CharField(max_length=64, db_index=True)
    evidence_claim_ids = models.JSONField(default=list, blank=True)
    evidence_source_ids = models.JSONField(default=list, blank=True)
    precondition_snapshot = models.JSONField(default=dict, blank=True)
    precondition_hash = models.CharField(max_length=64, blank=True, default="")
    preconditions_refreshed_at = models.DateTimeField(null=True, blank=True)
    risk_level = models.CharField(
        max_length=16,
        choices=AgentActionRiskLevel.choices,
        db_index=True,
    )
    requires_approval = models.BooleanField(default=True, db_index=True)
    status = models.CharField(
        max_length=32,
        choices=AgentActionStatus.choices,
        default=AgentActionStatus.PROPOSED,
        db_index=True,
    )
    idempotency_key = models.CharField(max_length=255)
    creation_request_hash = models.CharField(max_length=64)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_agent_actions",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_idempotency_key = models.CharField(max_length=255, blank=True, default="")
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rejected_agent_actions",
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=512, blank=True, default="")
    executed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="executed_agent_actions",
    )
    execution_idempotency_key = models.CharField(max_length=255, blank=True, default="")
    execution_attempts = models.PositiveIntegerField(default=0)
    executed_at = models.DateTimeField(null=True, blank=True)
    result_payload = models.JSONField(default=dict, blank=True)
    error_text = models.TextField(blank=True, default="")
    ingestion_action_request = models.ForeignKey(
        MemorySourceActionRequest,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_action_results",
    )
    reversal_supported = models.BooleanField(default=False)
    reversal_payload = models.JSONField(default=dict, blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reversed_agent_actions",
    )
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversal_idempotency_key = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    immutable_fields = (
        "organization_id",
        "configuration_id",
        "requested_by_id",
        "requested_by_slack_id",
        "action_type",
        "target_system",
        "input_payload",
        "input_hash",
        "evidence_claim_ids",
        "evidence_source_ids",
        "risk_level",
        "requires_approval",
        "idempotency_key",
        "creation_request_hash",
        "created_at",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "idempotency_key"),
                name="orgmem_agent_action_idem_uniq",
            ),
            models.CheckConstraint(
                check=(
                    ~Q(status=AgentActionStatus.APPROVED)
                    | Q(approved_at__isnull=False, approved_by__isnull=False)
                ),
                name="orgmem_agent_action_approved",
            ),
            models.CheckConstraint(
                check=(
                    Q(requires_approval=False)
                    | ~Q(
                        status__in=(
                            AgentActionStatus.APPROVED,
                            AgentActionStatus.EXECUTING,
                            AgentActionStatus.COMPLETED,
                            AgentActionStatus.REVERSING,
                            AgentActionStatus.REVERSED,
                        )
                    )
                    | Q(approved_at__isnull=False, approved_by__isnull=False)
                ),
                name="orgmem_agent_action_write_approved",
            ),
            models.CheckConstraint(
                check=(
                    ~Q(status=AgentActionStatus.COMPLETED)
                    | Q(executed_at__isnull=False)
                ),
                name="orgmem_agent_action_completed",
            ),
            models.CheckConstraint(
                check=(
                    ~Q(status=AgentActionStatus.REVERSED)
                    | Q(reversed_at__isnull=False, reversed_by__isnull=False)
                ),
                name="orgmem_agent_action_reversed",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "status", "created_at"),
                name="orgmem_agent_action_queue",
            ),
            models.Index(
                fields=("organization", "action_type", "created_at"),
                name="orgmem_agent_action_type",
            ),
        ]
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.action_type}: {self.status}"

    def clean(self):
        if self.configuration_id and (
            self.configuration.organization_id != self.organization_id
            or self.configuration.provider != self.target_system
        ):
            raise ValidationError(
                "Action configuration must match its organization and target system."
            )
        if not isinstance(self.input_payload, dict):
            raise ValidationError("Action input payload must be an object.")
        if not isinstance(self.evidence_claim_ids, list) or not isinstance(
            self.evidence_source_ids, list
        ):
            raise ValidationError("Action evidence identifiers must be lists.")
        if not isinstance(self.precondition_snapshot, dict):
            raise ValidationError("Action precondition snapshot must be an object.")
        if not isinstance(self.result_payload, dict) or not isinstance(
            self.reversal_payload, dict
        ):
            raise ValidationError("Action result and reversal payloads must be objects.")
        if self.ingestion_action_request_id and (
            self.configuration_id is None
            or self.ingestion_action_request.configuration_id != self.configuration_id
        ):
            raise ValidationError(
                "Action result ingestion must use the action's configuration."
            )


class AgentActionEvent(ImmutableEvidenceMixin):
    """Append-only, content-minimised action audit."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    proposal = models.ForeignKey(
        AgentActionProposal,
        on_delete=models.CASCADE,
        related_name="events",
    )
    event_type = models.CharField(
        max_length=32,
        choices=AgentActionEventType.choices,
        db_index=True,
    )
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_action_events",
    )
    request_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    payload_hash = models.CharField(max_length=64, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    immutable_fields = (
        "proposal_id",
        "event_type",
        "actor_user_id",
        "request_id",
        "payload_hash",
        "metadata",
        "created_at",
    )

    class Meta:
        indexes = [
            models.Index(
                fields=("proposal", "created_at"),
                name="orgmem_agent_event_time",
            ),
        ]
        ordering = ("proposal", "created_at")

    def __str__(self):
        return f"{self.proposal_id}: {self.event_type}"


class MemoryOutboxEvent(ImmutableEvidenceMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_outbox_events",
    )
    source = models.ForeignKey(
        MemorySource,
        on_delete=models.CASCADE,
        related_name="outbox_events",
    )
    source_version = models.ForeignKey(
        MemorySourceVersion,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="outbox_events",
    )
    event_type = models.CharField(max_length=64, choices=MemoryOutboxEventType.choices)
    payload = models.JSONField(default=dict, blank=True)
    idempotency_key = models.CharField(max_length=255, unique=True)
    status = models.CharField(
        max_length=16,
        choices=MemoryOutboxStatus.choices,
        default=MemoryOutboxStatus.PENDING,
        db_index=True,
    )
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now, db_index=True)
    published_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    immutable_fields = (
        "organization_id",
        "source_id",
        "source_version_id",
        "event_type",
        "payload",
        "idempotency_key",
    )

    class Meta:
        indexes = [
            models.Index(
                fields=("status", "available_at", "created_at"),
                name="orgmem_outbox_pending",
            ),
        ]
        ordering = ("created_at",)

    def __str__(self):
        return f"{self.event_type}: {self.source_id}"


class MemoryWorkItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_work_items",
    )
    provider = models.CharField(max_length=32, choices=MemoryProvider.choices, db_index=True)
    task_type = models.CharField(max_length=32, choices=MemoryWorkTaskType.choices, db_index=True)
    source = models.ForeignKey(
        MemorySource,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="work_items",
    )
    source_version = models.ForeignKey(
        MemorySourceVersion,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="work_items",
    )
    configuration = models.ForeignKey(
        MemoryConnectionConfiguration,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_items",
    )
    action_request = models.ForeignKey(
        MemorySourceActionRequest,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_items",
    )
    sync_run = models.ForeignKey(
        MemorySyncRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_items",
    )
    payload = models.JSONField(default=dict, blank=True)
    idempotency_key = models.CharField(max_length=255, unique=True)
    status = models.CharField(
        max_length=16,
        choices=MemoryWorkStatus.choices,
        default=MemoryWorkStatus.PENDING,
        db_index=True,
    )
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=5)
    available_at = models.DateTimeField(default=timezone.now, db_index=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=Q(max_attempts__gte=1),
                name="orgmem_work_max_attempts_pos",
            ),
            models.CheckConstraint(
                check=Q(attempts__lte=models.F("max_attempts")),
                name="orgmem_work_attempts_bounded",
            ),
        ]
        indexes = [
            models.Index(
                fields=("status", "available_at", "created_at"),
                name="orgmem_work_claim",
            ),
            models.Index(
                fields=("organization", "provider", "status"),
                name="orgmem_work_org_provider",
            ),
        ]
        ordering = ("available_at", "created_at")

    def __str__(self):
        return f"{self.task_type}: {self.status}/{self.pk}"

    def clean(self):
        if self.source_id and (
            self.source.organization_id != self.organization_id
            or self.source.provider != self.provider
        ):
            raise ValidationError("Work source must match organisation and provider.")
        if self.source_version_id and (
            self.source_version.source.organization_id != self.organization_id
            or self.source_version.source.provider != self.provider
        ):
            raise ValidationError("Work source version must match organisation and provider.")
        if self.configuration_id and (
            self.configuration.organization_id != self.organization_id
            or self.configuration.provider != self.provider
        ):
            raise ValidationError("Work configuration must match organisation and provider.")
        if self.action_request_id and (
            self.configuration_id is None
            or self.action_request.configuration_id != self.configuration_id
        ):
            raise ValidationError("Work action must belong to its configuration.")
        if self.sync_run_id and (
            self.configuration_id is None
            or self.sync_run.configuration_id != self.configuration_id
            or self.sync_run.action_request_id != self.action_request_id
        ):
            raise ValidationError("Work sync run must belong to its action and configuration.")


class MemoryWorkerLease(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    work_item = models.ForeignKey(
        MemoryWorkItem,
        on_delete=models.CASCADE,
        related_name="leases",
    )
    lease_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    worker_id = models.CharField(max_length=255)
    acquired_at = models.DateTimeField(default=timezone.now, db_index=True)
    heartbeat_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)
    released_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("work_item",),
                condition=Q(released_at__isnull=True),
                name="orgmem_work_active_lease_uniq",
            ),
            models.CheckConstraint(
                check=Q(expires_at__gt=models.F("acquired_at")),
                name="orgmem_lease_expiry_valid",
            ),
        ]
        indexes = [
            models.Index(
                fields=("released_at", "expires_at"),
                name="orgmem_lease_expiry",
            ),
        ]
        ordering = ("-acquired_at",)

    def __str__(self):
        return f"{self.work_item_id}: {self.worker_id}"


class MemoryDeadLetter(ImmutableEvidenceMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    work_item = models.OneToOneField(
        MemoryWorkItem,
        on_delete=models.PROTECT,
        related_name="dead_letter",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_dead_letters",
    )
    task_type = models.CharField(max_length=32, choices=MemoryWorkTaskType.choices)
    payload_snapshot = models.JSONField(default=dict, blank=True)
    attempts = models.PositiveIntegerField()
    last_error = models.TextField()
    dead_at = models.DateTimeField(default=timezone.now, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_memory_dead_letters",
    )
    requeued_work_item = models.ForeignKey(
        MemoryWorkItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requeued_from_dead_letters",
    )

    immutable_fields = (
        "work_item_id",
        "organization_id",
        "task_type",
        "payload_snapshot",
        "attempts",
        "last_error",
        "dead_at",
    )

    class Meta:
        indexes = [
            models.Index(
                fields=("organization", "resolved_at", "dead_at"),
                name="orgmem_dead_org_open",
            ),
        ]
        ordering = ("-dead_at",)

    def __str__(self):
        return f"{self.work_item_id}: {self.task_type}"


class MemoryDeletionRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memory_deletion_requests",
    )
    target_type = models.CharField(max_length=32, choices=MemoryDeletionTargetType.choices)
    target_id = models.CharField(max_length=1024)
    reason = models.CharField(max_length=512)
    status = models.CharField(
        max_length=16,
        choices=MemoryDeletionStatus.choices,
        default=MemoryDeletionStatus.PENDING,
        db_index=True,
    )
    hard_delete = models.BooleanField(default=False)
    idempotency_key = models.CharField(max_length=255)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_memory_deletions",
    )
    request_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    result_summary = models.JSONField(default=dict, blank=True)
    last_error = models.TextField(blank=True, default="")
    requested_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "idempotency_key"),
                name="orgmem_delete_idempotency_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "status", "requested_at"),
                name="orgmem_delete_org_status",
            ),
        ]
        ordering = ("-requested_at",)

    def __str__(self):
        return f"{self.target_type}:{self.target_id}/{self.status}"


class ServicePrincipal(models.Model):
    """A non-human identity bound to one organisation and explicit scopes."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=128, unique=True)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="service_principals",
    )
    scopes = models.JSONField(default=list, blank=True)
    allowed_surfaces = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name

    def has_scope(self, scope: str) -> bool:
        return scope in {str(value) for value in (self.scopes or [])}

    def allows_surface(self, surface: str) -> bool:
        return surface in {str(value) for value in (self.allowed_surfaces or [])}


class ServicePrincipalCredential(models.Model):
    """A rotatable credential; the plaintext secret is never persisted."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    principal = models.ForeignKey(
        ServicePrincipal,
        on_delete=models.CASCADE,
        related_name="credentials",
    )
    secret_hash = models.CharField(max_length=256)
    token_hint = models.CharField(max_length=48, db_index=True)
    rotated_from = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rotations",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_service_principal_credentials",
    )
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.principal.name} ({self.token_hint})"


class ServicePrincipalAuditEvent(models.Model):
    principal = models.ForeignKey(
        ServicePrincipal,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    credential = models.ForeignKey(
        ServicePrincipalCredential,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    event_type = models.CharField(max_length=64, db_index=True)
    request_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    remote_address = models.GenericIPAddressField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)


class OrganizationSlackWorkspace(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="slack_workspaces",
    )
    slack_team_id = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("organization__name", "slack_team_id")

    def __str__(self):
        return self.name or self.slack_team_id


class OrganizationSlackIdentity(models.Model):
    workspace = models.ForeignKey(
        OrganizationSlackWorkspace,
        on_delete=models.CASCADE,
        related_name="identities",
    )
    slack_user_id = models.CharField(max_length=32)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="organization_slack_identities",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("workspace", "slack_user_id"),
                name="orgmem_workspace_slack_user_uniq",
            ),
        ]
        ordering = ("workspace", "slack_user_id")

    def __str__(self):
        return f"{self.workspace.slack_team_id}:{self.slack_user_id}"


class ActorAssertionReceipt(models.Model):
    """Durable single-use nonce receipt for verified actor assertions."""

    principal = models.ForeignKey(
        ServicePrincipal,
        on_delete=models.CASCADE,
        related_name="actor_assertion_receipts",
    )
    nonce = models.CharField(max_length=128)
    request_id = models.CharField(max_length=128, db_index=True)
    event_id = models.CharField(max_length=128, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("principal", "nonce"),
                name="orgmem_principal_assertion_nonce_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=("expires_at",), name="orgmem_assertion_expiry_idx"),
        ]
