from django.db import models
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin, BaseUserManager
from django.utils import timezone

class CustomUserManager(BaseUserManager):
    def create_user(self, email, role='participant', password=None, **extra_fields):
        if not email:
            raise ValueError('Email is required.')
        email = self.normalize_email(email)
        user = self.model(email=email, role=role, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.is_active = True
        user.save(using=self._db)
        return user

    def create_superuser(self, email, role='admin', password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(email, role, password, **extra_fields)

class User(AbstractBaseUser, PermissionsMixin):
    ROLE_CHOICES = (
        ('admin', 'Admin'),
        ('participant', 'Participant'),
        ('professional', 'Professional'), # Added for flexibility
    )
    email = models.EmailField(unique=True)
    slack_id = models.CharField(max_length=50, blank=True, null=True, unique=True)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    
    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()
    
    @full_name.setter
    def full_name(self, value):
        parts = value.strip().split(' ', 1)
        self.first_name = parts[0]
        if len(parts) > 1:
            self.last_name = parts[1]
        else:
            self.last_name = ''
    phone = models.CharField(max_length=20, blank=True, null=True)
    about = models.TextField(blank=True, null=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='participant')
    is_superuser = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    has_team = models.BooleanField(default=False)
    is_staff = models.BooleanField(default=False)  # Required for admin interface
    date_joined = models.DateTimeField(default=timezone.now)
    avatar_url = models.URLField(blank=True, null=True)
    personas = models.JSONField(default=list, blank=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['role']

    objects = CustomUserManager()

    def __str__(self):
        return self.email

class Hackathon(models.Model):
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    description = models.TextField()
    start_date = models.DateField()
    end_date = models.DateField()
    bg_image_url = models.URLField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

from django.core.cache import cache

class GlobalSettings(models.Model):
    is_obscured = models.BooleanField(default=True, help_text="If set to True, submission scores will be hidden from users.")

    class Meta:
        verbose_name = "Global Settings"
        verbose_name_plural = "Global Settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super(GlobalSettings, self).save(*args, **kwargs)
        cache.set('global_settings', self)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        obj, created = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Global Settings"

class Organization(models.Model):
    """Organization that uses content factory."""
    name = models.CharField(max_length=255)
    domain = models.CharField(max_length=255, unique=True, db_index=True)
    competitors = models.JSONField(default=list, blank=True)
    seed_keywords = models.JSONField(default=list, blank=True, help_text="Seed keywords for content research")
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'content_factory_organization'

class OrganizationContentConfig(models.Model):
    """Content factory configuration per organization."""
    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name='content_config'
    )
    article_template = models.TextField(blank=True, null=True)
    design_guide = models.TextField(blank=True, null=True)
    resource_prompt = models.TextField(blank=True, null=True)
    company_context = models.TextField(blank=True, null=True, help_text="Auto-generated company overview for article generation context")
    github_repo = models.CharField(max_length=255, blank=True, null=True)
    github_token_encrypted = models.TextField(blank=True, null=True)

    # Domain-level GitHub credentials (supports multiple domains with different GitHub accounts)
    github_refresh_token_encrypted = models.TextField(blank=True, null=True)
    github_token_expires_at = models.DateTimeField(blank=True, null=True)
    github_user_name = models.CharField(max_length=255, blank=True, null=True)
    github_installation_id = models.CharField(max_length=50, blank=True, null=True)
    github_scopes = models.JSONField(default=list, blank=True)
    article_path_pattern = models.CharField(
        max_length=255, default="app/articles/content/{category}/{slug}.tsx"
    )
    registry_path = models.CharField(max_length=255, default="app/articles/registry.ts")
    scan_summary = models.TextField(blank=True, null=True)
    tech_stack = models.JSONField(default=dict, blank=True)
    installed_packages = models.JSONField(
        default=dict, blank=True,
        help_text="Full list of installed packages from package.json {name: version}"
    )
    pillar_strategy = models.JSONField(
        default=dict, blank=True,
        help_text="SEO content pillars with slugs and topics derived from company context"
    )
    brand_name = models.CharField(max_length=100, blank=True, null=True)
    articles_scaffolded = models.BooleanField(
        default=False,
        help_text="Whether the articles directory has been scaffolded in the GitHub repo"
    )
    articles_scaffold_pr_url = models.URLField(
        blank=True, null=True,
        help_text="PR URL from the articles scaffolding operation"
    )
    articles_scaffold_preview_url = models.URLField(
        blank=True, null=True,
        help_text="Cloudflare Pages preview URL from the articles scaffolding operation"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'content_factory_org_config'


class GeneratedComponent(models.Model):
    """
    Stores a generated/adapted React component for an organization.
    
    Components are created by content-factory's component generation pipeline
    during codebase scanning.
    """
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='generated_components'
    )
    
    # Component identity
    name = models.CharField(max_length=100)  # e.g., "ArticleHeroHeader"
    
    # Component content
    content = models.TextField()  # Full TSX code
    
    # Source tracking
    SOURCE_CHOICES = [
        ('generated', 'Generated'),
        ('adapted', 'Adapted'),
    ]
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    original_path = models.CharField(max_length=500, blank=True, null=True)  # If adapted
    
    # Matching metadata
    similarity_score = models.FloatField(default=0.0)  # 0.0 - 1.0
    matched_component = models.CharField(max_length=100, blank=True, null=True)  # Their component name
    adaptation_notes = models.TextField(blank=True, default='')
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'content_factory_generated_component'
        unique_together = ['organization', 'name']  # One component per name per org
        ordering = ['name']
    
    def __str__(self):
        return f"{self.organization.domain} / {self.name} ({self.source})"


class ComponentMapping(models.Model):
    """
    Stores the component mapping results from a scan.
    
    This is a JSON field containing the full mapping of:
    - our_component -> matched/unmatched status
    - similarity scores
    - adaptation notes
    """
    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name='component_mapping'
    )
    
    # Full mapping as JSON (Dict[str, ComponentMatch])
    mapping_data = models.JSONField(default=dict)
    
    # Summary stats
    total_components = models.IntegerField(default=0)
    matched_count = models.IntegerField(default=0)
    generated_count = models.IntegerField(default=0)
    
    # Generation pipeline result summary
    generation_status = models.CharField(max_length=20, blank=True, null=True)  # success/partial/failed
    design_guide_path = models.CharField(max_length=500, blank=True, null=True)
    storage_local_path = models.CharField(max_length=500, blank=True, null=True)
    storage_pr_url = models.URLField(blank=True, null=True)
    storage_branch_url = models.URLField(blank=True, null=True)
    failed_components = models.JSONField(default=list)  # List of failed component names
    
    # Last scan info
    last_scan_commit = models.CharField(max_length=40, blank=True, null=True)
    last_scan_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'content_factory_component_mapping'
    
    def __str__(self):
        return f"{self.organization.domain} mapping ({self.matched_count}/{self.total_components} matched)"


class ContentFactoryJob(models.Model):
    """
    Tracks content-factory pipeline jobs for callback routing.

    When content-factory sends callbacks (topic_selection, article_complete, error),
    this model maintains the mapping between job IDs and Slack users to enable
    proper notification routing.
    """
    job_id = models.CharField(max_length=100, unique=True, db_index=True)
    slack_user_id = models.CharField(max_length=50, db_index=True)
    domain = models.CharField(max_length=255)

    # Job state
    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('researching', 'Researching'),
        ('awaiting_confirmation', 'Awaiting Confirmation'),
        ('generating', 'Generating'),
        ('completed', 'Completed'),
        ('error', 'Error'),
        ('auth_required', 'Auth Required'),
    ]
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='queued')

    # Request metadata for retry (populated on creation)
    request_meta = models.JSONField(default=dict, blank=True, help_text="Original request parameters for retry")

    # Topic selection data (populated on topic_selection callback)
    selected_keyword = models.CharField(max_length=255, blank=True, null=True)
    selection_reason = models.TextField(blank=True, null=True)
    selection_data = models.JSONField(default=dict, blank=True)  # Full selection payload

    # Slack thread context for in-thread replies
    slack_channel_id = models.CharField(max_length=100, blank=True, default="")
    slack_thread_ts = models.CharField(max_length=50, blank=True, default="")

    # Result data (populated on article_complete callback)
    article_url = models.URLField(blank=True, null=True)
    pr_url = models.URLField(blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'content_factory_job'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.job_id} ({self.status}) - {self.domain}"


# =============================================================================
# SEO Research Models
# =============================================================================

import uuid


class KeywordTier(models.TextChoices):
    """GEO-based topic prioritization tiers."""
    TIER_1_BLUE_OCEAN = "tier_1_blue_ocean", "Blue Ocean"
    TIER_2_AUTHORITY = "tier_2_authority", "Authority Builder"
    TIER_3_LONG_TAIL = "tier_3_long_tail", "Long Tail Gem"
    TIER_4_DISCARD = "tier_4_discard", "Discard"


class KeywordSource(models.TextChoices):
    """How the keyword was discovered."""
    SEED = "seed", "Seed Keyword"
    COMPETITOR = "competitor", "Competitor Analysis"
    RELATED = "related", "Related/Semantic"
    PAA = "paa", "People Also Ask"


class KeywordStatus(models.TextChoices):
    """Keyword writing status."""
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    IN_PROGRESS = "in_progress", "In Progress"
    WRITTEN = "written", "Written"
    SKIPPED = "skipped", "Skipped"


class TrendStatus(models.TextChoices):
    """Trend velocity status."""
    BREAKOUT = "breakout", "Breakout"
    RISING = "rising", "Rising"
    STABLE = "stable", "Stable"
    DECLINING = "declining", "Declining"


class WrittenArticle(models.Model):
    """
    Links researched keywords to published articles.

    Tracks the output of the content-factory pipeline.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='written_articles'
    )

    # Article identity
    title = models.CharField(max_length=500)
    slug = models.CharField(max_length=255, db_index=True)
    category = models.CharField(max_length=100)

    # URLs
    article_url = models.URLField(blank=True, null=True)
    pr_url = models.URLField(blank=True, null=True)

    # Primary keyword (denormalized for quick access)
    primary_keyword = models.CharField(max_length=500)

    # Content factory job reference
    job = models.ForeignKey(
        ContentFactoryJob,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='articles'
    )

    # Timestamps
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'seo_written_article'
        unique_together = ['organization', 'slug']
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', 'primary_keyword']),
        ]

    def __str__(self):
        return f"{self.title} ({self.slug})"


class ResearchedKeyword(models.Model):
    """
    Core SEO keyword with metrics from content-factory research.

    This is the central table linking keywords to organizations
    with full GEO metrics and writing status tracking.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='researched_keywords',
        db_index=True
    )

    # Keyword identity
    keyword = models.CharField(max_length=500, db_index=True)
    keyword_normalized = models.CharField(
        max_length=500,
        db_index=True,
        help_text="Lowercase, trimmed version for deduplication"
    )

    # Core metrics (refreshable)
    volume = models.IntegerField(default=0, help_text="Monthly search volume")
    difficulty = models.IntegerField(default=50, help_text="SEO difficulty 0-100")
    intent = models.CharField(max_length=50, default="informational")

    # GEO metrics
    tier = models.CharField(
        max_length=30,
        choices=KeywordTier.choices,
        default=KeywordTier.TIER_4_DISCARD
    )
    opportunity_index = models.FloatField(default=0.0, db_index=True)

    # Provenance
    source = models.CharField(
        max_length=20,
        choices=KeywordSource.choices,
        default=KeywordSource.SEED
    )
    source_detail = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="E.g., competitor domain or seed keyword"
    )
    competitor_urls = models.JSONField(default=list, blank=True)

    # Writing status
    status = models.CharField(
        max_length=20,
        choices=KeywordStatus.choices,
        default=KeywordStatus.PENDING,
        db_index=True
    )

    # Research memory
    times_shown = models.IntegerField(default=0)
    last_shown_at = models.DateTimeField(null=True, blank=True)
    times_rejected = models.IntegerField(default=0)
    last_rejected_at = models.DateTimeField(null=True, blank=True)
    cooldown_until = models.DateTimeField(null=True, blank=True)
    times_selected = models.IntegerField(default=0)
    last_selected_at = models.DateTimeField(null=True, blank=True)
    cluster_fingerprint = models.CharField(max_length=255, blank=True, default="", db_index=True)

    # Link to written article
    written_article = models.ForeignKey(
        WrittenArticle,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='source_keywords'
    )

    # Timestamps
    discovered_at = models.DateTimeField(default=timezone.now)
    metrics_updated_at = models.DateTimeField(auto_now=True)
    status_changed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'seo_researched_keyword'
        unique_together = ['organization', 'keyword_normalized']
        ordering = ['-opportunity_index']
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['organization', 'tier']),
            models.Index(fields=['organization', 'opportunity_index']),
            models.Index(fields=['organization', 'status', 'opportunity_index']),
            models.Index(fields=['organization', 'cooldown_until']),
        ]

    def save(self, *args, **kwargs):
        # Auto-generate normalized keyword
        self.keyword_normalized = self.keyword.lower().strip()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.keyword} ({self.organization.domain}) - {self.tier}"


class KeywordVelocity(models.Model):
    """
    Trend velocity metrics for a keyword.

    Separate table to:
    1. Allow historical tracking (multiple snapshots)
    2. Store the daily_volumes array without bloating the main table
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    keyword = models.ForeignKey(
        ResearchedKeyword,
        on_delete=models.CASCADE,
        related_name='velocity_snapshots'
    )

    # Velocity metrics
    absolute_volume = models.IntegerField(default=0)
    velocity_score = models.FloatField(default=0.0, help_text="-1.0 to 1.0+")
    trend_status = models.CharField(
        max_length=20,
        choices=TrendStatus.choices,
        default=TrendStatus.STABLE
    )
    daily_volumes = models.JSONField(
        default=list,
        blank=True,
        help_text="Raw daily volume data from Glimpse/pytrends"
    )

    # Snapshot timestamp
    captured_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'seo_keyword_velocity'
        ordering = ['-captured_at']
        indexes = [
            models.Index(fields=['keyword', 'captured_at']),
        ]

    def __str__(self):
        return f"{self.keyword.keyword} velocity: {self.velocity_score:.2f} ({self.trend_status})"


class AISaturation(models.Model):
    """
    AI saturation metrics for a keyword.

    Tracks SERP features that compete with organic clicks.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    keyword = models.ForeignKey(
        ResearchedKeyword,
        on_delete=models.CASCADE,
        related_name='ai_saturation_snapshots'
    )
    domain = models.CharField(max_length=255, blank=True, default='', db_index=True)

    # AI Overview detection
    ai_overview_present = models.BooleanField(default=False)
    ai_overview_quality = models.CharField(
        max_length=20,
        choices=[
            ('comprehensive', 'Comprehensive'),
            ('partial', 'Partial'),
            ('none', 'None'),
        ],
        default='none'
    )

    # Other SERP features
    featured_snippet_present = models.BooleanField(default=False)
    video_carousel_present = models.BooleanField(default=False)
    knowledge_panel_present = models.BooleanField(default=False)

    # Calculated score
    saturation_score = models.FloatField(
        default=0.0,
        help_text="0.0 (no AI) to 1.0 (fully saturated)"
    )

    # SERP hostility (combined metric)
    hostility_score = models.FloatField(default=0.0)
    hostility_recommendation = models.CharField(
        max_length=20,
        choices=[
            ('high_priority', 'High Priority'),
            ('pivot_angle', 'Pivot Angle'),
            ('low_priority', 'Low Priority'),
        ],
        default='high_priority'
    )
    organic_positions_above_fold = models.IntegerField(default=0)

    # Additional SERP features as flags
    serp_features = models.JSONField(
        default=list,
        blank=True,
        help_text="List of SERP feature types present"
    )

    # Snapshot timestamp
    captured_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'seo_ai_saturation'
        ordering = ['-captured_at']
        indexes = [
            models.Index(fields=['keyword', 'captured_at']),
            models.Index(fields=['saturation_score']),
        ]

    def __str__(self):
        ai_status = "AI Overview" if self.ai_overview_present else "No AI"
        return f"{self.keyword.keyword}: {ai_status}, score={self.saturation_score:.2f}"


class PAQuestion(models.Model):
    """
    'People Also Ask' question with depth tracking.

    Normalized to support nested question relationships
    and efficient querying by keyword or question text.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    keyword = models.ForeignKey(
        ResearchedKeyword,
        on_delete=models.CASCADE,
        related_name='paa_questions'
    )
    domain = models.CharField(max_length=255, blank=True, default='', db_index=True)

    # Question content
    question = models.TextField()
    question_normalized = models.CharField(
        max_length=500,
        db_index=True,
        help_text="Lowercase version for dedup"
    )
    answer_snippet = models.TextField(blank=True, default='')
    source_url = models.URLField(blank=True, null=True)

    # Depth tracking (1 = top-level, 2-4 = nested)
    depth = models.IntegerField(default=1)

    # AI presence in this specific PAA
    has_ai_overview = models.BooleanField(default=False)

    # Parent question (for nested structure)
    parent_question = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='child_questions'
    )

    # Order within parent
    order = models.IntegerField(default=0)

    # Timestamps
    discovered_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'seo_paa_question'
        ordering = ['depth', 'order']
        indexes = [
            models.Index(fields=['keyword', 'depth']),
            models.Index(fields=['question_normalized']),
        ]

    def save(self, *args, **kwargs):
        self.question_normalized = self.question.lower().strip()[:500]
        super().save(*args, **kwargs)

    def __str__(self):
        depth_indicator = "  " * (self.depth - 1)
        return f"{depth_indicator}Q: {self.question[:80]}..."


class SemanticCluster(models.Model):
    """
    A cluster of semantically related keywords (pillar structure).

    Maps to content-factory's TopicMap.pillars structure.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='semantic_clusters'
    )

    # Cluster identity
    cluster_id = models.IntegerField(help_text="Local ID within the topic map")
    pillar_keyword = models.CharField(max_length=500, db_index=True)

    # Cluster metrics (aggregated from members)
    average_similarity = models.FloatField(default=0.0)
    total_volume = models.IntegerField(default=0)
    avg_difficulty = models.FloatField(default=0.0)
    avg_velocity = models.FloatField(default=0.0)

    # Assigned tier for the cluster
    topic_tier = models.CharField(
        max_length=30,
        choices=KeywordTier.choices,
        default=KeywordTier.TIER_4_DISCARD
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'seo_semantic_cluster'
        unique_together = ['organization', 'cluster_id']
        ordering = ['-total_volume']
        indexes = [
            models.Index(fields=['organization', 'topic_tier']),
            models.Index(fields=['pillar_keyword']),
        ]

    def __str__(self):
        return f"Cluster {self.cluster_id}: {self.pillar_keyword} ({self.topic_tier})"


class ClusterMembership(models.Model):
    """
    Many-to-many relationship between keywords and clusters.

    A keyword can belong to one cluster per organization.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    keyword = models.ForeignKey(
        ResearchedKeyword,
        on_delete=models.CASCADE,
        related_name='cluster_memberships'
    )
    cluster = models.ForeignKey(
        SemanticCluster,
        on_delete=models.CASCADE,
        related_name='member_keywords'
    )

    # Whether this keyword is the pillar
    is_pillar = models.BooleanField(default=False)

    # Similarity to cluster centroid
    similarity_score = models.FloatField(default=0.0)

    class Meta:
        db_table = 'seo_cluster_membership'
        unique_together = ['keyword', 'cluster']
        indexes = [
            models.Index(fields=['cluster', 'is_pillar']),
        ]

    def __str__(self):
        pillar_marker = " (PILLAR)" if self.is_pillar else ""
        return f"{self.keyword.keyword} -> {self.cluster.pillar_keyword}{pillar_marker}"


class TopicMap(models.Model):
    """
    Complete topic map snapshot for an organization.

    Represents a research session's clustering results.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='topic_maps'
    )

    # Clustering parameters
    clustering_threshold = models.FloatField(default=0.85)
    total_keywords = models.IntegerField(default=0)

    # Unclustered keywords (JSON list of keyword IDs or text)
    unclustered_keywords = models.JSONField(default=list, blank=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'seo_topic_map'
        ordering = ['-created_at']

    def __str__(self):
        return f"TopicMap for {self.organization.domain} ({self.created_at.date()})"


class ResearchSession(models.Model):
    """
    Tracks a research session for provenance and refresh tracking.

    Each time content-factory runs research, it creates a session.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='research_sessions'
    )

    # Session metadata
    seed_keywords_used = models.JSONField(default=list)
    competitors_analyzed = models.JSONField(default=list)

    # Statistics
    keywords_discovered = models.IntegerField(default=0)
    keywords_updated = models.IntegerField(default=0)
    clusters_created = models.IntegerField(default=0)

    # GEO settings used
    geo_config = models.JSONField(
        default=dict,
        blank=True,
        help_text="GEO flags/thresholds used in this session"
    )

    # Timestamps
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'seo_research_session'
        ordering = ['-started_at']

    def __str__(self):
        return f"Research {self.organization.domain} @ {self.started_at.date()}"
