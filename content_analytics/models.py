from __future__ import annotations

from django.db import models
from django.db.models import F, Q


class AnalyticsProvisionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROVISIONED = "provisioned", "Provisioned"
    ERROR = "error", "Error"
    DISABLED = "disabled", "Disabled"


class AnalyticsSite(models.Model):
    """Platform-owned analytics site mapped to one Content Factory organization."""

    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="analytics_site",
    )
    provider = models.CharField(max_length=32, default="umami", db_index=True)
    external_website_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    domain = models.CharField(max_length=255, db_index=True)
    enabled = models.BooleanField(default=True, db_index=True)
    provision_status = models.CharField(
        max_length=24,
        choices=AnalyticsProvisionStatus.choices,
        default=AnalyticsProvisionStatus.PENDING,
        db_index=True,
    )
    team_id = models.CharField(max_length=64, blank=True, default="")
    # Relative first-party proxy paths are supported, so these are CharFields
    # rather than URLFields.
    tracker_script_url = models.CharField(max_length=2048, blank=True, default="")
    collector_url = models.CharField(
        max_length=2048,
        blank=True,
        default="",
        help_text="Public Umami host URL supplied to the tracker as data-host-url",
    )
    last_provisioned_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_analytics_site"
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "external_website_id"],
                condition=~Q(external_website_id=""),
                name="analytics_provider_site_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["enabled", "provision_status"], name="analytics_site_due_idx"),
        ]

    def __str__(self):
        return f"{self.organization.domain}:{self.provider}"


class ArticleAnalyticsLocationSource(models.TextChoices):
    GENERATED = "generated", "Generated canonical location"
    CANONICAL = "canonical", "Canonical location"
    SITEMAP = "sitemap", "Sitemap-confirmed live location"
    MIGRATION = "migration", "Migrated current location"


class ArticleAnalyticsLocation(models.Model):
    """Effective-dated canonical location for a stable analytics article id.

    Umami and Search Console key their aggregate APIs by path/URL, while
    ``WrittenArticle.analytics_id`` is stable across renames. Keeping the
    location timeline separate lets rolling syncs continue to query the URL
    that was actually live on each day without changing aggregate ownership.
    Date intervals are inclusive.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="article_analytics_locations",
    )
    article = models.ForeignKey(
        "content_factory.WrittenArticle",
        on_delete=models.CASCADE,
        related_name="analytics_locations",
    )
    canonical_url = models.URLField(max_length=2048)
    canonical_path = models.CharField(max_length=1024, db_index=True)
    valid_from = models.DateField(db_index=True)
    valid_to = models.DateField(null=True, blank=True, db_index=True)
    source = models.CharField(
        max_length=24,
        choices=ArticleAnalyticsLocationSource.choices,
        default=ArticleAnalyticsLocationSource.CANONICAL,
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_analytics_article_location"
        ordering = ["valid_from", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["article"],
                condition=Q(valid_to__isnull=True),
                name="an_article_one_active_loc",
            ),
            models.UniqueConstraint(
                fields=["article", "valid_from"],
                name="an_article_loc_start_uniq",
            ),
            models.CheckConstraint(
                check=Q(valid_to__isnull=True) | Q(valid_to__gte=F("valid_from")),
                name="an_location_valid_range",
            ),
        ]
        indexes = [
            models.Index(fields=["article", "valid_from", "valid_to"], name="analytics_location_day_idx"),
            models.Index(fields=["organization", "valid_from"], name="analytics_location_org_idx"),
        ]

    def __str__(self):
        end = self.valid_to.isoformat() if self.valid_to else "current"
        return f"{self.article_id}:{self.canonical_path} ({self.valid_from}..{end})"


class ArticleBehaviorDaily(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="article_behavior_daily",
    )
    article = models.ForeignKey(
        "content_factory.WrittenArticle",
        on_delete=models.CASCADE,
        related_name="behavior_daily",
    )
    date = models.DateField(db_index=True)
    pageviews = models.PositiveBigIntegerField(default=0)
    visitors = models.PositiveBigIntegerField(default=0)
    visits = models.PositiveBigIntegerField(default=0)
    bounces = models.PositiveBigIntegerField(default=0)
    umami_total_time = models.PositiveBigIntegerField(default=0)
    # These milestone counts are unique visits, obtained from Umami's filtered
    # event stats endpoint. They are deliberately not raw event totals.
    engaged_30_count = models.PositiveBigIntegerField(default=0, help_text="Unique visits that reached 30 seconds")
    scroll_50_count = models.PositiveBigIntegerField(default=0, help_text="Unique visits that reached 50% scroll")
    scroll_90_count = models.PositiveBigIntegerField(default=0, help_text="Unique visits that reached 90% scroll")
    cta_impression_count = models.PositiveBigIntegerField(default=0, help_text="Unique visits where a CTA became visible")
    cta_click_count = models.PositiveBigIntegerField(default=0, help_text="Unique visits with a CTA click")
    outbound_click_count = models.PositiveBigIntegerField(default=0, help_text="Unique visits with a tracked outbound click")
    source_updated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_analytics_behavior_daily"
        constraints = [
            models.UniqueConstraint(fields=["article", "date"], name="article_behavior_day_uniq"),
        ]
        indexes = [
            models.Index(fields=["organization", "date"], name="behavior_org_day_idx"),
        ]
        ordering = ["date", "article_id"]


class TrafficSourceCategory(models.TextChoices):
    SEARCH = "search", "Search"
    AI = "ai", "AI assistant"
    SOCIAL = "social", "Social"
    EMAIL = "email", "Email"
    PAID = "paid", "Paid"
    REFERRAL = "referral", "Referral"
    DIRECT = "direct", "Direct or unknown"
    OTHER = "other", "Other"


class ArticleTrafficSourceDaily(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="article_traffic_source_daily",
    )
    article = models.ForeignKey(
        "content_factory.WrittenArticle",
        on_delete=models.CASCADE,
        related_name="traffic_source_daily",
    )
    date = models.DateField(db_index=True)
    source_category = models.CharField(max_length=24, choices=TrafficSourceCategory.choices, db_index=True)
    source_name = models.CharField(max_length=255, blank=True, default="")
    pageviews = models.PositiveBigIntegerField(default=0)
    visitors = models.PositiveBigIntegerField(default=0)
    visits = models.PositiveBigIntegerField(default=0)
    cta_impression_count = models.PositiveBigIntegerField(default=0)
    cta_click_count = models.PositiveBigIntegerField(default=0)
    conversion_attribution_available = models.BooleanField(
        default=False,
        help_text="Whether CTA visits were attributed to this source rather than left unassigned",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_analytics_traffic_daily"
        constraints = [
            models.UniqueConstraint(
                fields=["article", "date", "source_category", "source_name"],
                name="article_traffic_day_source_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "date", "source_category"], name="traffic_org_day_src_idx"),
        ]


class SearchConsoleAccessMethod(models.TextChoices):
    SERVICE_ACCOUNT = "service_account", "Service account"
    OAUTH = "oauth", "OAuth"


class SearchConsolePropertyStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    VERIFIED = "verified", "Verified"
    ERROR = "error", "Error"
    DISABLED = "disabled", "Disabled"


class SearchConsoleProperty(models.Model):
    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="search_console_property",
    )
    site_url = models.CharField(max_length=2048, blank=True, default="", db_index=True)
    access_method = models.CharField(
        max_length=24,
        choices=SearchConsoleAccessMethod.choices,
        default=SearchConsoleAccessMethod.SERVICE_ACCOUNT,
    )
    google_connection = models.ForeignKey(
        "integrations.GoogleConnection",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="search_console_properties",
    )
    status = models.CharField(
        max_length=20,
        choices=SearchConsolePropertyStatus.choices,
        default=SearchConsolePropertyStatus.PENDING,
        db_index=True,
    )
    permission_level = models.CharField(max_length=64, blank=True, default="")
    service_account_email = models.EmailField(blank=True, default="")
    sync_enabled = models.BooleanField(default=True, db_index=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_analytics_gsc_property"
        indexes = [
            models.Index(fields=["sync_enabled", "status"], name="gsc_property_due_idx"),
        ]


class ArticleSearchDaily(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="article_search_daily",
    )
    article = models.ForeignKey(
        "content_factory.WrittenArticle",
        on_delete=models.CASCADE,
        related_name="search_daily",
    )
    date = models.DateField(db_index=True)
    engine = models.CharField(max_length=32, default="google", db_index=True)
    surface = models.CharField(max_length=64, default="web", db_index=True)
    country = models.CharField(max_length=8, blank=True, default="")
    device = models.CharField(max_length=32, blank=True, default="")
    clicks = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    impressions = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    ctr = models.DecimalField(max_digits=12, decimal_places=10, default=0)
    position = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_analytics_search_daily"
        constraints = [
            models.UniqueConstraint(
                fields=["article", "date", "engine", "surface", "country", "device"],
                name="article_search_day_grain_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "date", "engine"], name="search_org_day_engine_idx"),
        ]


class ArticleSearchQueryDaily(models.Model):
    """Bounded top-query rows; synchronization caps rows per article/day."""

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="article_search_query_daily",
    )
    article = models.ForeignKey(
        "content_factory.WrittenArticle",
        on_delete=models.CASCADE,
        related_name="search_query_daily",
    )
    date = models.DateField(db_index=True)
    engine = models.CharField(max_length=32, default="google", db_index=True)
    surface = models.CharField(max_length=64, default="web")
    query = models.CharField(max_length=1024)
    clicks = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    impressions = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    ctr = models.DecimalField(max_digits=12, decimal_places=10, default=0)
    position = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_analytics_search_query_daily"
        constraints = [
            models.UniqueConstraint(
                fields=["article", "date", "engine", "surface", "query"],
                name="article_search_query_day_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "date", "engine"], name="query_org_day_engine_idx"),
        ]


class AnalyticsSyncSource(models.TextChoices):
    UMAMI = "umami", "Umami"
    SEARCH_CONSOLE = "search_console", "Google Search Console"


class AnalyticsSyncStatus(models.TextChoices):
    IDLE = "idle", "Idle"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"


class AnalyticsSyncState(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="analytics_sync_states",
    )
    source = models.CharField(max_length=32, choices=AnalyticsSyncSource.choices)
    status = models.CharField(max_length=20, choices=AnalyticsSyncStatus.choices, default=AnalyticsSyncStatus.IDLE)
    cursor = models.JSONField(default=dict, blank=True)
    backfill_started_on = models.DateField(null=True, blank=True)
    synced_through = models.DateField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_attempted_at = models.DateTimeField(null=True, blank=True)
    last_completed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_analytics_sync_state"
        constraints = [
            models.UniqueConstraint(fields=["organization", "source"], name="analytics_org_source_uniq"),
        ]
        indexes = [
            models.Index(fields=["source", "status", "updated_at"], name="analytics_sync_due_idx"),
        ]
