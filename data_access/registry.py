from __future__ import annotations

from django.db.models import Q

from content_factory import models as cf_models
from core.models import User
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from integrations.models import (
    ExternalFinancialRecord,
    ExternalServiceConnection,
    FinancialAccount,
    GoogleConnection,
    UserIntegration,
)
from organizations.models import Organization
from roo.models import (
    ChannelFirstPost,
    CoworkingBooking,
    CoworkingDayCapacity,
    Ledger,
    PointsAccount,
    PointsAdmin,
    PointsPurchase,
    PointsRequest,
    QuestProgress,
    RewardsCatalog,
    RewardRedemption,
    Task,
    TaskActivity,
    TaskAssignment,
    TaskSubmission,
    TaskTemplate,
)
from roo.permissions import get_admin_role
from startup_updates import models as su_models
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep, ContentFactoryRunStepAttempt

from .resolvers import Actor, FieldSpec, ModelResolver, Policy, Resource, ServiceResolver


SENSITIVE_FIELD_MARKERS = (
    "password",
    "token",
    "secret",
    "credential",
    "raw_payload",
    "message_payloads",
    "raw_content_base64",
    "storage_path",
    "last_history_id",
    "sync_cursor",
)

INTERNAL_ALL = (
    Policy(("django_staff",), "all"),
    Policy(("django_superuser",), "all"),
)

POINTS_ADMIN_ALL = (
    Policy(("points_admin:admin", "points_admin:committee"), "all"),
    Policy(("points_admin:portfolio_lead",), "all"),
)

POINTS_ADMIN_NO_PARTNER_ALL = POINTS_ADMIN_ALL + INTERNAL_ALL
POINTS_REPORT_POLICIES = POINTS_ADMIN_ALL + (
    Policy(("points_admin:partner",), "all", operations=("count", "aggregate")),
) + INTERNAL_ALL

ANY_SLACK_READ = (Policy(("authenticated_slack",), "all", operations=("list", "count")),)


def f(name: str, source: str | None = None, *, searchable: bool = False, filterable: bool = True, orderable: bool = True, groupable: bool = True):
    return FieldSpec(
        name=name,
        source=source,
        searchable=searchable,
        filterable=filterable,
        orderable=orderable,
        groupable=groupable,
    )


def simple_fields(*names: str, searchable: tuple[str, ...] = ()):
    return tuple(f(name, searchable=name in searchable) for name in names)


def model_resource(
    key: str,
    model,
    description: str,
    fields,
    policies,
    *,
    default_order_by=(),
    default_limit=100,
    max_limit=500,
    operations=("list", "count", "aggregate"),
):
    return Resource(
        key=key,
        description=description,
        resolver=ModelResolver(model, default_order_by=default_order_by),
        fields=fields,
        policies=policies,
        default_limit=default_limit,
        max_limit=max_limit,
        operations=operations,
    )


def self_user_policy(field: str = "user_id") -> Policy:
    return Policy(("authenticated_slack",), "self_user", field)


def self_slack_policy(field: str) -> Policy:
    return Policy(("authenticated_slack",), "self_slack", field)


def founder_org_policy(field: str = "organization_id") -> Policy:
    return Policy(("founder",), "founder_org", field)


def founder_domain_policy(field: str = "domain") -> Policy:
    return Policy(("founder",), "founder_domain", field)


def content_org_policies(field: str = "organization_id"):
    return (founder_org_policy(field),) + INTERNAL_ALL


def content_domain_policies(field: str = "domain"):
    return (founder_domain_policy(field),) + INTERNAL_ALL


def build_actor(slack_id: str) -> Actor:
    clean_slack_id = str(slack_id or "").strip()
    roles = {"authenticated_slack"} if clean_slack_id else set()

    user = User.objects.filter(slack_id=clean_slack_id).first() if clean_slack_id else None
    if user:
        roles.add("user")
        if user.is_staff:
            roles.add("django_staff")
        if user.is_superuser:
            roles.add("django_superuser")

    points_role = get_admin_role(clean_slack_id) if clean_slack_id else None
    points_portfolio = ""
    if points_role:
        roles.add(f"points_admin:{points_role}")
        if points_role in {"admin", "committee", "portfolio_lead"}:
            roles.add("points_admin:full")
        admin = PointsAdmin.objects.filter(slack_user_id=clean_slack_id, is_active=True).first()
        points_portfolio = admin.portfolio if admin and admin.portfolio else ""

    organization_ids: set[int] = set()
    organization_domains: set[str] = set()
    if user:
        profile = VibeRaisingProfile.objects.filter(user=user).first()
        if profile:
            roles.add(profile.role)
            for company in VibeRaisingCompany.objects.filter(profile=profile).select_related("organization"):
                if company.organization_id:
                    organization_ids.add(company.organization_id)
                    organization_domains.add(company.organization.domain)
                if company.domain:
                    organization_domains.add(company.domain)

        for binding in su_models.UserStartupBinding.objects.filter(user=user).select_related("organization"):
            organization_ids.add(binding.organization_id)
            if binding.organization.domain:
                organization_domains.add(binding.organization.domain)

    return Actor(
        slack_id=clean_slack_id,
        user=user,
        roles=frozenset(roles),
        organization_ids=frozenset(organization_ids),
        organization_domains=frozenset(organization_domains),
        points_portfolio=points_portfolio,
    )


def luma_attendee_report_handler(*, resource: Resource, actor: Actor, query: dict) -> dict:
    if not actor.has_any_role(("points_admin:admin", "points_admin:committee", "points_admin:partner", "django_staff", "django_superuser")):
        from .resolvers import DataAccessPermissionDenied

        raise DataAccessPermissionDenied("You do not have access to this resource.")
    return {
        "resource": resource.key,
        "rows": [],
        "returned_count": 0,
        "limit": 0,
        "offset": 0,
        "has_more": False,
        "virtual": True,
        "message": "Use the existing Luma attendee report API for this virtual resource.",
    }


RESOURCES = {
    "core_users": model_resource(
        "core_users",
        User,
        "Safe user profile projection.",
        (
            f("id"),
            f("email", searchable=True),
            f("slack_id", searchable=True),
            f("first_name", searchable=True),
            f("last_name", searchable=True),
            f("is_active"),
            f("date_joined"),
            f("avatar_url"),
        ),
        (self_user_policy("id"),) + INTERNAL_ALL,
        default_order_by=("email",),
    ),
    "points_admins": model_resource(
        "points_admins",
        PointsAdmin,
        "Points admin roles and portfolio assignments.",
        simple_fields("slack_user_id", "user_id", "role", "portfolio", "is_active", "weekly_allowance", "created_at", searchable=("slack_user_id", "role", "portfolio")),
        POINTS_ADMIN_NO_PARTNER_ALL,
        default_order_by=("slack_user_id",),
    ),
    "points_accounts": model_resource(
        "points_accounts",
        PointsAccount,
        "Current Roo points balances.",
        (f("user_id"), f("user_email", "user__email", searchable=True), f("user_slack_id", "user__slack_id", searchable=True), f("balance"), f("earned_balance"), f("purchased_topup_balance"), f("lifetime_earned"), f("lifetime_purchased_topup"), f("lifetime_spent"), f("expired_or_reversed_points"), f("created_at"), f("updated_at")),
        (self_user_policy("user_id"),) + POINTS_ADMIN_NO_PARTNER_ALL,
        default_order_by=("user__email",),
    ),
    "points_ledger": model_resource(
        "points_ledger",
        Ledger,
        "Append-only Roo points ledger summary.",
        (f("id"), f("user_id"), f("user_email", "user__email", searchable=True), f("user_slack_id", "user__slack_id", searchable=True), f("delta"), f("kind"), f("source"), f("reference_type"), f("reference_id"), f("description", searchable=True), f("created_by_slack_id", searchable=True), f("created_at")),
        (self_user_policy("user_id"), self_slack_policy("slack_user_id")) + POINTS_ADMIN_NO_PARTNER_ALL,
        default_order_by=("-created_at",),
        max_limit=300,
    ),
    "points_purchases": model_resource(
        "points_purchases",
        PointsPurchase,
        "Top-up Roo Points purchase status.",
        simple_fields("id", "user_id", "slack_user_id", "pack_id", "points_amount", "amount_cents", "currency", "status", "paid_at", "expires_at", "created_at", "updated_at", searchable=("slack_user_id", "status")),
        (self_user_policy("user_id"), self_slack_policy("slack_user_id")) + POINTS_ADMIN_NO_PARTNER_ALL,
        default_order_by=("-created_at",),
    ),
    "task_templates": model_resource(
        "task_templates",
        TaskTemplate,
        "Roo task/rate-card templates.",
        simple_fields("id", "name", "alias", "points", "description", "is_active", "created_at", "updated_at", searchable=("name", "alias", "description")),
        ANY_SLACK_READ + POINTS_ADMIN_NO_PARTNER_ALL,
        default_order_by=("name",),
    ),
    "tasks": model_resource(
        "tasks",
        Task,
        "Roo task queue and task state.",
        simple_fields("id", "task_code", "title", "description", "portfolio", "work_domain", "review_flow", "points", "points_estimate", "points_min", "points_max", "status", "visibility", "volunteer_ready", "difficulty", "estimate_minutes", "created_by_user_id", "assigned_to_user_id", "closed_by_user_id", "reviewer_slack_id", "due_date", "created_at", "updated_at", "closed_at", searchable=("task_code", "title", "description", "portfolio", "status")),
        (self_slack_policy("assigned_to_user_id"), Policy(("points_admin:portfolio_lead",), "portfolio", "portfolio")) + POINTS_ADMIN_NO_PARTNER_ALL,
        default_order_by=("-created_at",),
    ),
    "task_assignments": model_resource(
        "task_assignments",
        TaskAssignment,
        "Roo task assignment state.",
        simple_fields("id", "task_id", "assigned_user_id", "assigned_to_slack_id", "claimed_points_snapshot", "status", "claimed_at", "released_at", "submitted_at", "approved_at", "approved_by_slack_id", "awarded_points", "closed_reason", "created_at", "updated_at", searchable=("assigned_to_slack_id", "status")),
        (self_user_policy("assigned_user_id"), self_slack_policy("assigned_to_slack_id")) + POINTS_ADMIN_NO_PARTNER_ALL,
        default_order_by=("-created_at",),
    ),
    "task_submissions": model_resource(
        "task_submissions",
        TaskSubmission,
        "Roo task submission records.",
        simple_fields("id", "task_id", "assignment_id", "user_id", "submission_text", "submission_url", "status", "evidence_kind", "review_notes", "reviewed_by_slack_id", "reviewed_at", "approved_by_slack_id", "approved_at", "rejection_reason", "created_at", searchable=("submission_text", "status")),
        (self_user_policy("user_id"),) + POINTS_ADMIN_NO_PARTNER_ALL,
        default_order_by=("-created_at",),
        max_limit=200,
    ),
    "task_activity": model_resource(
        "task_activity",
        TaskActivity,
        "Roo task workflow audit events.",
        simple_fields("id", "task_id", "assignment_id", "submission_id", "event_type", "actor_slack_id", "summary", "created_at", searchable=("event_type", "actor_slack_id", "summary")),
        POINTS_ADMIN_NO_PARTNER_ALL,
        default_order_by=("-created_at",),
    ),
    "coworking_bookings": model_resource(
        "coworking_bookings",
        CoworkingBooking,
        "Coworking bookings. Source is bookings, not door check-ins.",
        (f("id"), f("user_id"), f("user_email", "user__email", searchable=True), f("user_slack_id", "user__slack_id", searchable=True), f("date"), f("status"), f("points_cost"), f("slack_channel_id"), f("created_at"), f("cancelled_at")),
        (self_user_policy("user_id"),) + POINTS_REPORT_POLICIES,
        default_order_by=("-date",),
        max_limit=500,
    ),
    "coworking_capacity": model_resource(
        "coworking_capacity",
        CoworkingDayCapacity,
        "Coworking capacity overrides.",
        simple_fields("date", "capacity", "notes", "created_at", "updated_at", searchable=("notes",)),
        POINTS_REPORT_POLICIES,
        default_order_by=("-date",),
    ),
    "rewards_catalog": model_resource(
        "rewards_catalog",
        RewardsCatalog,
        "Available Roo rewards.",
        simple_fields("code", "name", "description", "cost_points", "fulfillment", "is_active", "max_per_user", "stock_remaining", "created_at", "updated_at", searchable=("code", "name", "description")),
        ANY_SLACK_READ + POINTS_ADMIN_NO_PARTNER_ALL,
        default_order_by=("cost_points",),
    ),
    "reward_redemptions": model_resource(
        "reward_redemptions",
        RewardRedemption,
        "Roo reward redemption status.",
        (f("id"), f("user_id"), f("user_slack_id", "user__slack_id", searchable=True), f("reward_code", "reward_id", searchable=True), f("quantity"), f("status"), f("requested_at"), f("approved_at"), f("fulfilled_at"), f("approved_by_slack_id"), f("notes", searchable=True), f("slack_channel_id"), f("slack_thread_ts")),
        (self_user_policy("user_id"),) + POINTS_ADMIN_NO_PARTNER_ALL,
        default_order_by=("-requested_at",),
    ),
    "points_requests": model_resource(
        "points_requests",
        PointsRequest,
        "Slack-created points requests.",
        simple_fields("id", "requester_slack_id", "target_slack_id", "points", "reason", "status", "approved_by_slack_id", "approved_at", "slack_channel_id", "slack_thread_ts", "created_at", "updated_at", searchable=("requester_slack_id", "target_slack_id", "reason", "status")),
        (self_slack_policy("requester_slack_id"), self_slack_policy("target_slack_id")) + POINTS_ADMIN_NO_PARTNER_ALL,
        default_order_by=("-created_at",),
    ),
    "channel_first_posts": model_resource(
        "channel_first_posts",
        ChannelFirstPost,
        "First-post point-award markers by Slack channel.",
        simple_fields("id", "slack_user_id", "channel_id", "posted_at", searchable=("slack_user_id", "channel_id")),
        (self_slack_policy("slack_user_id"),) + POINTS_ADMIN_NO_PARTNER_ALL,
        default_order_by=("-posted_at",),
    ),
    "quest_progress": model_resource(
        "quest_progress",
        QuestProgress,
        "Roo quest progress by Slack user.",
        simple_fields("id", "slack_user_id", "quest_id", "current_count", "completed", "first_progress_at", "completed_at", "created_at", "updated_at", searchable=("slack_user_id", "quest_id")),
        (self_slack_policy("slack_user_id"),) + POINTS_ADMIN_NO_PARTNER_ALL,
        default_order_by=("-updated_at",),
    ),
    "vibe_raising_profiles": model_resource(
        "vibe_raising_profiles",
        VibeRaisingProfile,
        "Vibe Raising founder/investor profiles.",
        (f("id"), f("user_id"), f("user_email", "user__email", searchable=True), f("user_slack_id", "user__slack_id", searchable=True), f("role"), f("organization_name", searchable=True), f("active_company_id"), f("created_at"), f("updated_at")),
        (self_user_policy("user_id"),) + INTERNAL_ALL,
        default_order_by=("user_id",),
    ),
    "vibe_raising_companies": model_resource(
        "vibe_raising_companies",
        VibeRaisingCompany,
        "Vibe Raising companies linked to founder profiles.",
        (f("id"), f("profile_id"), f("profile_user_id", "profile__user_id"), f("organization_id"), f("organization_domain", "organization__domain", searchable=True), f("name", searchable=True), f("domain", searchable=True), f("abn", searchable=True), f("location", searchable=True), f("avatar_url"), f("registered"), f("created_at"), f("updated_at")),
        (Policy(("founder",), "self_user", "profile__user_id"),) + INTERNAL_ALL,
        default_order_by=("-created_at",),
    ),
    "organizations": model_resource(
        "organizations",
        Organization,
        "Organizations used by Content Factory and Vibe Raising.",
        simple_fields("id", "name", "domain", "company_linkedin_url", "competitors", "seed_keywords", "created_at", searchable=("name", "domain")),
        content_org_policies("id"),
        default_order_by=("domain",),
    ),
    "startup_profiles": model_resource(
        "startup_profiles",
        su_models.StartupProfile,
        "Startup profile context for Vibe Raising updates.",
        simple_fields("id", "organization_id", "company_aliases", "domain_aliases", "product_names", "founder_names", "team_names", "investor_names", "investor_domains", "competitor_names", "competitor_domains", "customer_names", "customer_domains", "prospect_names", "prospect_domains", "positive_keywords", "negative_keywords", "kpi_definitions", "default_currency", "stage", "organization_kind", "short_description", "problem_solved", "target_audience", "notes", "created_at", "updated_at", searchable=("stage", "organization_kind", "short_description", "notes")),
        content_org_policies("organization_id"),
        default_order_by=("organization__domain",),
    ),
    "startup_bindings": model_resource(
        "startup_bindings",
        su_models.UserStartupBinding,
        "User to startup organization bindings.",
        (f("id"), f("user_id"), f("user_email", "user__email", searchable=True), f("organization_id"), f("organization_domain", "organization__domain", searchable=True), f("role", searchable=True), f("is_default_for_gmail"), f("created_at"), f("updated_at")),
        (self_user_policy("user_id"), founder_org_policy("organization_id")) + INTERNAL_ALL,
        default_order_by=("-updated_at",),
    ),
    "startup_manual_documents": model_resource(
        "startup_manual_documents",
        su_models.StartupManualDocument,
        "Manual document upload metadata for Vibe Raising.",
        simple_fields("id", "organization_id", "company_id", "created_by_id", "original_filename", "content_type", "file_size_bytes", "extraction_status", "text_size_chars", "created_at", "updated_at", searchable=("original_filename", "content_type", "extraction_status")),
        content_org_policies("organization_id"),
        default_order_by=("-created_at",),
    ),
    "gmail_messages": model_resource(
        "gmail_messages",
        su_models.GmailMessageArtifact,
        "Safe Gmail message metadata for startup updates.",
        simple_fields("id", "organization_id", "google_connection_id", "gmail_message_id", "gmail_thread_id", "internal_date", "subject", "from_address", "snippet", "body_preview", "has_attachments", "heuristic_score", "relevance_label", "relevance_score", "relevance_reason", "needs_thread_context", "metadata_hydrated_at", "classified_at", "created_at", "updated_at", searchable=("subject", "from_address", "snippet", "body_preview", "relevance_label")),
        content_org_policies("organization_id"),
        default_order_by=("-internal_date",),
        max_limit=200,
    ),
    "gmail_threads": model_resource(
        "gmail_threads",
        su_models.GmailThreadArtifact,
        "Safe Gmail thread metadata for startup updates.",
        simple_fields("id", "organization_id", "google_connection_id", "gmail_thread_id", "source_message_count", "hydration_status", "extraction_status", "latest_message_internal_date", "hydrated_at", "extracted_at", "created_at", "updated_at", searchable=("gmail_thread_id", "hydration_status", "extraction_status")),
        content_org_policies("organization_id"),
        default_order_by=("-latest_message_internal_date",),
    ),
    "gmail_attachments": model_resource(
        "gmail_attachments",
        su_models.GmailAttachmentArtifact,
        "Safe Gmail attachment metadata for startup updates.",
        simple_fields("id", "organization_id", "thread_artifact_id", "message_artifact_id", "mime_type", "filename", "content_disposition", "size_bytes", "is_inline", "extraction_status", "sha256", "hydrated_at", "extracted_at", "created_at", "updated_at", searchable=("filename", "mime_type", "extraction_status")),
        content_org_policies("organization_id"),
        default_order_by=("-created_at",),
    ),
    "slack_channel_selections": model_resource(
        "slack_channel_selections",
        su_models.SlackChannelSelection,
        "Selected Slack channels for startup updates.",
        simple_fields("id", "connection_id", "user_id", "organization_id", "channel_id", "channel_name", "is_private", "selected", "last_synced_at", "created_at", "updated_at", searchable=("channel_id", "channel_name")),
        (self_user_policy("user_id"), founder_org_policy("organization_id")) + INTERNAL_ALL,
        default_order_by=("channel_name",),
    ),
    "slack_messages": model_resource(
        "slack_messages",
        su_models.SlackMessageArtifact,
        "Safe Slack message metadata for startup updates.",
        simple_fields("id", "organization_id", "connection_id", "channel_id", "channel_name", "slack_message_ts", "thread_ts", "parent_ts", "author_id", "author_name", "posted_at", "created_at", "updated_at", searchable=("channel_name", "author_name", "thread_ts")),
        content_org_policies("organization_id"),
        default_order_by=("-posted_at",),
    ),
    "slack_threads": model_resource(
        "slack_threads",
        su_models.SlackThreadArtifact,
        "Safe Slack thread metadata for startup updates.",
        simple_fields("id", "organization_id", "connection_id", "channel_id", "channel_name", "thread_ts", "source_message_count", "latest_message_at", "heuristic_score", "relevance_label", "relevance_score", "needs_extraction", "extraction_status", "classified_at", "extracted_at", "created_at", "updated_at", searchable=("channel_name", "thread_ts", "relevance_label", "extraction_status")),
        content_org_policies("organization_id"),
        default_order_by=("-latest_message_at",),
    ),
    "linear_project_selections": model_resource(
        "linear_project_selections",
        su_models.LinearProjectSelection,
        "Selected Linear projects for startup updates.",
        simple_fields("id", "connection_id", "user_id", "organization_id", "linear_project_id", "project_name", "project_status", "project_health", "selected", "last_synced_at", "created_at", "updated_at", searchable=("linear_project_id", "project_name", "project_status", "project_health")),
        (self_user_policy("user_id"), founder_org_policy("organization_id")) + INTERNAL_ALL,
        default_order_by=("project_name",),
    ),
    "linear_projects": model_resource(
        "linear_projects",
        su_models.LinearProjectArtifact,
        "Synced Linear project metadata.",
        simple_fields("id", "organization_id", "connection_id", "linear_project_id", "name", "status_name", "status_type", "health", "progress", "scope", "priority", "lead_name", "lead_email", "team_names", "start_date", "target_date", "started_at", "completed_at", "canceled_at", "url", "heuristic_score", "relevance_label", "relevance_score", "needs_extraction", "extraction_status", "created_at", "updated_at", searchable=("linear_project_id", "name", "status_name", "health", "lead_name")),
        content_org_policies("organization_id"),
        default_order_by=("name",),
    ),
    "linear_issues": model_resource(
        "linear_issues",
        su_models.LinearIssueArtifact,
        "Synced Linear issue metadata.",
        simple_fields("id", "organization_id", "connection_id", "project_id", "linear_issue_id", "identifier", "title", "state_name", "state_type", "priority", "priority_label", "assignee_name", "assignee_email", "team_key", "team_name", "label_names", "estimate", "due_date", "created_at_linear", "updated_at_linear", "started_at", "completed_at", "canceled_at", "url", "created_at", "updated_at", searchable=("identifier", "title", "state_name", "assignee_name", "team_key")),
        content_org_policies("organization_id"),
        default_order_by=("-updated_at_linear",),
    ),
    "linear_project_updates": model_resource(
        "linear_project_updates",
        su_models.LinearProjectUpdateArtifact,
        "Synced Linear project update metadata.",
        simple_fields("id", "organization_id", "connection_id", "project_id", "linear_project_update_id", "health", "author_name", "author_email", "url", "created_at_linear", "updated_at_linear", "created_at", "updated_at", searchable=("linear_project_update_id", "health", "author_name")),
        content_org_policies("organization_id"),
        default_order_by=("-updated_at_linear",),
    ),
    "startup_metrics": model_resource(
        "startup_metrics",
        su_models.StartupMetricObservation,
        "Extracted startup metric observations.",
        simple_fields("id", "organization_id", "run_id", "metric_key", "metric_name", "value_text", "value_number", "unit", "observed_at", "period_month", "confidence", "source_provider", "summary", "created_at", "updated_at", searchable=("metric_key", "metric_name", "value_text", "unit", "summary")),
        content_org_policies("organization_id"),
        default_order_by=("-period_month",),
    ),
    "startup_events": model_resource(
        "startup_events",
        su_models.StartupEvent,
        "Extracted startup timeline events.",
        simple_fields("id", "organization_id", "run_id", "canonical_key", "event_type", "title", "summary", "event_date", "month_bucket", "date_precision", "sentiment", "investor_importance", "confidence", "status", "needs_review", "merge_notes", "created_at", "updated_at", searchable=("canonical_key", "event_type", "title", "summary", "status")),
        content_org_policies("organization_id"),
        default_order_by=("-month_bucket",),
    ),
    "monthly_update_drafts": model_resource(
        "monthly_update_drafts",
        su_models.MonthlyUpdateDraft,
        "Startup monthly update draft metadata.",
        simple_fields("id", "organization_id", "run_id", "month", "status", "title", "model_name", "groundedness_status", "created_at", "updated_at", searchable=("status", "title", "model_name", "groundedness_status")),
        content_org_policies("organization_id"),
        default_order_by=("-month",),
    ),
    "startup_data_deletion_requests": model_resource(
        "startup_data_deletion_requests",
        su_models.StartupDataDeletionRequest,
        "Startup data deletion request status.",
        simple_fields("id", "organization_id", "user_id", "request_id", "provider", "status", "delete_derived_data", "google_account", "reason", "deleted_counts", "warnings", "started_at", "completed_at", "created_at", "updated_at", searchable=("request_id", "provider", "status", "google_account", "reason")),
        (self_user_policy("user_id"), founder_org_policy("organization_id")) + INTERNAL_ALL,
        default_order_by=("-updated_at",),
    ),
    "google_connections": model_resource(
        "google_connections",
        GoogleConnection,
        "Google connection metadata without credentials.",
        simple_fields("id", "user_id", "google_email", "scope", "created_at", "updated_at", searchable=("google_email", "scope")),
        (self_user_policy("user_id"),) + INTERNAL_ALL,
        default_order_by=("-updated_at",),
    ),
    "external_service_connections": model_resource(
        "external_service_connections",
        ExternalServiceConnection,
        "External connector status without credentials.",
        simple_fields("id", "provider", "user_id", "organization_id", "scopes", "external_account_id", "account_label", "status", "last_synced_at", "last_error", "created_at", "updated_at", searchable=("provider", "external_account_id", "account_label", "status", "last_error")),
        (self_user_policy("user_id"), founder_org_policy("organization_id")) + INTERNAL_ALL,
        default_order_by=("provider", "-updated_at"),
    ),
    "financial_accounts": model_resource(
        "financial_accounts",
        FinancialAccount,
        "Financial account metadata and balances without raw provider payload.",
        simple_fields("id", "provider", "connection_id", "user_id", "organization_id", "external_account_id", "account_label", "institution_id", "institution_name", "account_type", "status", "currency", "balance", "available_funds", "last_synced_at", "created_at", "updated_at", searchable=("provider", "account_label", "institution_name", "status")),
        (self_user_policy("user_id"), founder_org_policy("organization_id")) + INTERNAL_ALL,
        default_order_by=("provider", "account_label"),
    ),
    "financial_records": model_resource(
        "financial_records",
        ExternalFinancialRecord,
        "Financial transaction/record metadata without raw provider payload.",
        simple_fields("id", "provider", "record_type", "connection_id", "financial_account_id", "user_id", "organization_id", "external_record_id", "external_account_id", "currency", "amount", "direction", "status", "posted_at", "transaction_date", "description", "merchant_name", "category", "class_name", "created_at", "updated_at", searchable=("provider", "record_type", "status", "description", "merchant_name", "category")),
        (self_user_policy("user_id"), founder_org_policy("organization_id")) + INTERNAL_ALL,
        default_order_by=("-posted_at", "-transaction_date"),
        max_limit=300,
    ),
    "github_integrations": model_resource(
        "github_integrations",
        UserIntegration,
        "GitHub integration status without credentials.",
        simple_fields("slack_user_id", "github_user_name", "github_repo", "github_scopes", "github_installation_id", "project_scanned", "last_scanned_sha", "last_scanned_at", "updated_at", searchable=("slack_user_id", "github_user_name", "github_repo")),
        (self_slack_policy("slack_user_id"),) + INTERNAL_ALL,
        default_order_by=("-updated_at",),
    ),
    "content_org_config": model_resource(
        "content_org_config",
        cf_models.OrganizationContentConfig,
        "Content Factory organization config without GitHub credentials.",
        simple_fields("id", "organization_id", "connected_slack_user_id", "default_timezone", "daily_discovery_enabled", "daily_discovery_priority", "baseline_skipped_at", "baseline_skip_reason", "github_repo", "github_user_name", "github_installation_id", "github_scopes", "article_delivery_mode", "article_path_pattern", "registry_path", "default_publish_target_id", "brand_name", "articles_scaffolded", "articles_scaffold_pr_url", "articles_scaffold_preview_url", "last_scanned_sha", "last_scanned_at", "created_at", "updated_at", searchable=("connected_slack_user_id", "github_repo", "github_user_name", "brand_name")),
        (self_slack_policy("connected_slack_user_id"), founder_org_policy("organization_id")) + INTERNAL_ALL,
        default_order_by=("-updated_at",),
    ),
    "website_baselines": model_resource(
        "website_baselines",
        cf_models.WebsiteBaselineSnapshot,
        "Vibe Marketing website baseline snapshots without raw payload.",
        simple_fields("id", "organization_id", "domain", "run_id", "status", "collected_at", "overall_score", "summary", "metrics", "source_status", "recommendations", "created_at", "updated_at", searchable=("domain", "run_id", "status")),
        content_org_policies("organization_id"),
        default_order_by=("-collected_at",),
    ),
    "generated_components": model_resource(
        "generated_components",
        cf_models.GeneratedComponent,
        "Generated/adapted component metadata without TSX content.",
        simple_fields("id", "organization_id", "name", "source", "original_path", "similarity_score", "matched_component", "adaptation_notes", "created_at", "updated_at", searchable=("name", "source", "original_path", "matched_component", "adaptation_notes")),
        content_org_policies("organization_id"),
        default_order_by=("name",),
    ),
    "component_mappings": model_resource(
        "component_mappings",
        cf_models.ComponentMapping,
        "Content Factory component mapping summary.",
        simple_fields("id", "organization_id", "total_components", "matched_count", "generated_count", "generation_status", "storage_pr_url", "storage_branch_url", "failed_components", "last_scan_commit", "last_scan_at", searchable=("generation_status", "last_scan_commit")),
        content_org_policies("organization_id"),
        default_order_by=("-last_scan_at",),
    ),
    "content_factory_jobs": model_resource(
        "content_factory_jobs",
        cf_models.ContentFactoryJob,
        "Content Factory Slack job state.",
        simple_fields("id", "job_id", "slack_user_id", "domain", "status", "client_request_id", "billing_source_job_id", "billing_amount", "billing_status", "selected_keyword", "selection_reason", "slack_channel_id", "slack_root_message_ts", "slack_thread_ts", "article_url", "pr_url", "error_message", "created_at", "updated_at", searchable=("job_id", "slack_user_id", "domain", "status", "selected_keyword", "error_message")),
        (self_slack_policy("slack_user_id"), founder_domain_policy("domain")) + INTERNAL_ALL,
        default_order_by=("-created_at",),
    ),
    "scheduled_discovery_dispatches": model_resource(
        "scheduled_discovery_dispatches",
        cf_models.ScheduledDiscoveryDispatch,
        "Scheduled daily discovery dispatch state.",
        simple_fields("id", "slack_user_id", "domain", "timezone", "local_date", "scheduled_for_at", "slot_index", "trigger_source", "state", "content_factory_job_id", "last_error", "slack_channel_id", "slack_message_ts", "slack_thread_ts", "created_at", "updated_at", searchable=("slack_user_id", "domain", "trigger_source", "state", "last_error")),
        (self_slack_policy("slack_user_id"), founder_domain_policy("domain")) + INTERNAL_ALL,
        default_order_by=("-local_date", "-updated_at"),
    ),
    "content_healing_records": model_resource(
        "content_healing_records",
        cf_models.ContentFactoryHealingRecord,
        "Content Factory healing memory.",
        simple_fields("id", "organization_id", "domain", "github_repo", "failure_kind", "failure_family_key", "exact_signature", "summary", "changed_files", "validation_results", "snippet_or_rule", "applies_to", "promotion_state", "latest_run_id", "created_at", "updated_at", searchable=("domain", "github_repo", "failure_kind", "summary", "promotion_state", "latest_run_id")),
        content_domain_policies("domain"),
        default_order_by=("-updated_at",),
    ),
    "vibe_component_comments": model_resource(
        "vibe_component_comments",
        cf_models.VibeMarketingComponentComment,
        "Vibe Marketing component review comments.",
        simple_fields("id", "run_id", "actor_id", "component_id", "component_type", "component_label", "source_section_id", "selector", "body", "status", "batch_id", "created_at", "updated_at", searchable=("component_id", "component_label", "body", "status")),
        (self_user_policy("actor_id"), founder_domain_policy("run__domain")) + INTERNAL_ALL,
        default_order_by=("-created_at",),
    ),
    "written_articles": model_resource(
        "written_articles",
        cf_models.WrittenArticle,
        "Published/generated article records.",
        simple_fields("id", "organization_id", "title", "slug", "category", "article_url", "pr_url", "primary_keyword", "job_id", "published_at", "created_at", searchable=("title", "slug", "category", "primary_keyword")),
        content_org_policies("organization_id"),
        default_order_by=("-created_at",),
    ),
    "researched_keywords": model_resource(
        "researched_keywords",
        cf_models.ResearchedKeyword,
        "SEO researched keywords and metrics.",
        simple_fields("id", "organization_id", "keyword", "keyword_normalized", "volume", "difficulty", "difficulty_source", "intent", "tier", "opportunity_index", "source", "source_detail", "status", "times_shown", "times_rejected", "times_selected", "cluster_fingerprint", "discovered_at", "metrics_updated_at", "status_changed_at", searchable=("keyword", "intent", "tier", "status", "source_detail")),
        content_org_policies("organization_id"),
        default_order_by=("-opportunity_index",),
    ),
    "topic_feedback": model_resource(
        "topic_feedback",
        cf_models.TopicFeedback,
        "SEO topic feedback memory.",
        simple_fields("id", "organization_id", "keyword", "keyword_normalized", "feedback_type", "reason_code", "reason_text", "decline_scope", "source", "session_id", "restored_at", "created_at", "updated_at", searchable=("keyword", "feedback_type", "reason_code", "reason_text")),
        content_org_policies("organization_id"),
        default_order_by=("-created_at",),
    ),
    "keyword_velocity": model_resource(
        "keyword_velocity",
        cf_models.KeywordVelocity,
        "SEO keyword velocity snapshots.",
        (f("id"), f("keyword_id"), f("keyword_text", "keyword__keyword", searchable=True), f("organization_id", "keyword__organization_id"), f("absolute_volume"), f("velocity_score"), f("trend_status"), f("captured_at")),
        content_org_policies("keyword__organization_id"),
        default_order_by=("-captured_at",),
    ),
    "ai_saturation": model_resource(
        "ai_saturation",
        cf_models.AISaturation,
        "SEO AI saturation snapshots.",
        (f("id"), f("keyword_id"), f("keyword_text", "keyword__keyword", searchable=True), f("organization_id", "keyword__organization_id"), f("domain", searchable=True), f("ai_overview_present"), f("ai_overview_quality"), f("featured_snippet_present"), f("video_carousel_present"), f("knowledge_panel_present"), f("saturation_score"), f("hostility_score"), f("hostility_recommendation"), f("organic_positions_above_fold"), f("captured_at")),
        content_org_policies("keyword__organization_id"),
        default_order_by=("-captured_at",),
    ),
    "paa_questions": model_resource(
        "paa_questions",
        cf_models.PAQuestion,
        "People Also Ask question research.",
        (f("id"), f("keyword_id"), f("keyword_text", "keyword__keyword", searchable=True), f("organization_id", "keyword__organization_id"), f("domain", searchable=True), f("question", searchable=True), f("source_url"), f("depth"), f("has_ai_overview"), f("parent_question_id"), f("order"), f("discovered_at")),
        content_org_policies("keyword__organization_id"),
        default_order_by=("depth", "order"),
    ),
    "semantic_clusters": model_resource(
        "semantic_clusters",
        cf_models.SemanticCluster,
        "SEO semantic topic clusters.",
        simple_fields("id", "organization_id", "cluster_id", "pillar_keyword", "average_similarity", "total_volume", "avg_difficulty", "avg_velocity", "topic_tier", "created_at", "updated_at", searchable=("pillar_keyword", "topic_tier")),
        content_org_policies("organization_id"),
        default_order_by=("-total_volume",),
    ),
    "cluster_memberships": model_resource(
        "cluster_memberships",
        cf_models.ClusterMembership,
        "SEO keyword to semantic cluster membership.",
        (f("id"), f("keyword_id"), f("keyword_text", "keyword__keyword", searchable=True), f("organization_id", "keyword__organization_id"), f("cluster_id"), f("cluster_pillar_keyword", "cluster__pillar_keyword", searchable=True), f("is_pillar"), f("similarity_score")),
        content_org_policies("keyword__organization_id"),
        default_order_by=("-similarity_score",),
    ),
    "topic_maps": model_resource(
        "topic_maps",
        cf_models.TopicMap,
        "SEO topic map snapshots.",
        simple_fields("id", "organization_id", "clustering_threshold", "total_keywords", "unclustered_keywords", "created_at"),
        content_org_policies("organization_id"),
        default_order_by=("-created_at",),
    ),
    "research_sessions": model_resource(
        "research_sessions",
        cf_models.ResearchSession,
        "SEO research session metadata.",
        simple_fields("id", "organization_id", "seed_keywords_used", "competitors_analyzed", "keywords_discovered", "keywords_updated", "clusters_created", "geo_config", "started_at", "completed_at"),
        content_org_policies("organization_id"),
        default_order_by=("-started_at",),
    ),
    "content_factory_runs": model_resource(
        "content_factory_runs",
        ContentFactoryRun,
        "Content Factory durable workflow runs.",
        simple_fields("id", "run_id", "workflow", "domain", "github_repo", "slack_user_id", "status", "current_step", "approval_state", "artifact_root", "step_order", "acceptance_summary", "verification_summary", "error", "resume_available", "created_at", "updated_at", searchable=("run_id", "workflow", "domain", "github_repo", "slack_user_id", "status", "current_step", "error")),
        (self_slack_policy("slack_user_id"), founder_domain_policy("domain")) + INTERNAL_ALL,
        default_order_by=("-updated_at",),
    ),
    "content_factory_run_steps": model_resource(
        "content_factory_run_steps",
        ContentFactoryRunStep,
        "Content Factory workflow run step state.",
        (f("id"), f("run_id"), f("run_key", "run__run_id", searchable=True), f("domain", "run__domain", searchable=True), f("step_key", searchable=True), f("display_order"), f("required"), f("status"), f("attempts"), f("message", searchable=True), f("started_at"), f("completed_at"), f("error", searchable=True), f("latest_attempt_path")),
        content_domain_policies("run__domain"),
        default_order_by=("run_id", "display_order"),
    ),
    "content_factory_run_step_attempts": model_resource(
        "content_factory_run_step_attempts",
        ContentFactoryRunStepAttempt,
        "Content Factory workflow run step attempt metadata.",
        (f("id"), f("step_id"), f("run_id", "step__run_id"), f("run_key", "step__run__run_id", searchable=True), f("domain", "step__run__domain", searchable=True), f("attempt"), f("status"), f("message", searchable=True), f("started_at"), f("completed_at"), f("error", searchable=True), f("created_at")),
        content_domain_policies("step__run__domain"),
        default_order_by=("-created_at",),
    ),
    "luma_attendee_report": Resource(
        key="luma_attendee_report",
        description="Virtual Luma attendee report resource. Use existing Luma API for report payloads.",
        resolver=ServiceResolver(luma_attendee_report_handler),
        fields=(f("message"),),
        policies=(Policy(("points_admin:admin", "points_admin:committee", "points_admin:partner", "django_staff", "django_superuser"), "all"),),
        operations=("list",),
        default_limit=0,
        max_limit=0,
    ),
}


def get_resource(key: str) -> Resource | None:
    return RESOURCES.get(key)


def list_resources() -> list[dict]:
    return [RESOURCES[key].catalog_entry() for key in sorted(RESOURCES)]


def assert_no_sensitive_fields_registered():
    violations = []
    for resource in RESOURCES.values():
        for field_name in resource.fields:
            lowered = field_name.lower()
            if any(marker in lowered for marker in SENSITIVE_FIELD_MARKERS):
                violations.append(f"{resource.key}.{field_name}")
    if violations:
        raise AssertionError(f"Sensitive fields registered: {', '.join(sorted(violations))}")
