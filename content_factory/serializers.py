from rest_framework import serializers

from .models import ComponentMapping, GeneratedComponent


class GeneratedComponentSerializer(serializers.ModelSerializer):
    """Serializer for individual generated components."""
    
    class Meta:
        model = GeneratedComponent
        fields = [
            'id', 'name', 'content', 'source', 'original_path',
            'similarity_score', 'matched_component', 'adaptation_notes',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class GeneratedComponentListSerializer(serializers.ModelSerializer):
    """Lighter serializer for component listings (without full content)."""
    
    class Meta:
        model = GeneratedComponent
        fields = ['name', 'source', 'similarity_score', 'updated_at']


class ComponentMappingSerializer(serializers.ModelSerializer):
    """Serializer for component mapping summary."""

    class Meta:
        model = ComponentMapping
        fields = [
            'mapping_data', 'total_components', 'matched_count', 'generated_count',
            'generation_status', 'design_guide_path', 'storage_local_path',
            'storage_pr_url', 'storage_branch_url', 'failed_components',
            'last_scan_commit', 'last_scan_at'
        ]
        read_only_fields = ['last_scan_at']


# =============================================================================
# SEO Research Serializers
# =============================================================================

from .models import (
    ResearchedKeyword, KeywordVelocity, AISaturation, PAQuestion,
    SemanticCluster, TopicMap, WrittenArticle, ResearchSession, TopicFeedback
)


class KeywordVelocitySerializer(serializers.ModelSerializer):
    """Serializer for velocity snapshot data."""

    class Meta:
        model = KeywordVelocity
        fields = [
            'absolute_volume', 'velocity_score', 'trend_status',
            'daily_volumes', 'source', 'basis', 'period_label',
            'is_estimated', 'captured_at'
        ]


class AISaturationSerializer(serializers.ModelSerializer):
    """Serializer for AI saturation snapshot data."""

    class Meta:
        model = AISaturation
        fields = [
            'ai_overview_present', 'ai_overview_quality',
            'featured_snippet_present', 'video_carousel_present',
            'knowledge_panel_present', 'saturation_score',
            'hostility_score', 'hostility_recommendation',
            'organic_positions_above_fold', 'serp_features', 'captured_at'
        ]


class PAQuestionSerializer(serializers.ModelSerializer):
    """Serializer for PAA questions with nesting support."""
    children = serializers.SerializerMethodField()

    class Meta:
        model = PAQuestion
        fields = [
            'id', 'question', 'answer_snippet', 'source_url',
            'depth', 'has_ai_overview', 'order', 'children'
        ]

    def get_children(self, obj):
        children = obj.child_questions.all()
        return PAQuestionSerializer(children, many=True).data


class ResearchedKeywordListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for keyword list views."""
    latest_velocity = serializers.SerializerMethodField()
    latest_saturation = serializers.SerializerMethodField()
    paa_count = serializers.SerializerMethodField()

    class Meta:
        model = ResearchedKeyword
        fields = [
            'id', 'keyword', 'volume', 'difficulty', 'difficulty_source', 'intent',
            'tier', 'opportunity_index', 'source', 'status',
            'times_shown', 'last_shown_at', 'times_rejected', 'last_rejected_at',
            'cooldown_until', 'times_selected', 'last_selected_at',
            'cluster_fingerprint', 'related_keywords', 'monthly_searches',
            'latest_velocity', 'latest_saturation', 'paa_count',
            'discovered_at', 'metrics_updated_at'
        ]

    def get_latest_velocity(self, obj):
        snapshot = obj.velocity_snapshots.first()
        if snapshot:
            return {
                'velocity_score': snapshot.velocity_score,
                'trend_status': snapshot.trend_status,
                'source': snapshot.source,
                'basis': snapshot.basis,
                'period_label': snapshot.period_label,
                'is_estimated': snapshot.is_estimated,
            }
        return None

    def get_latest_saturation(self, obj):
        snapshot = obj.ai_saturation_snapshots.first()
        if snapshot:
            return {
                'saturation_score': snapshot.saturation_score,
                'ai_overview_present': snapshot.ai_overview_present
            }
        return None

    def get_paa_count(self, obj):
        return obj.paa_questions.count()


class ResearchedKeywordDetailSerializer(serializers.ModelSerializer):
    """Full serializer with all related data for keyword detail view."""
    velocity_history = KeywordVelocitySerializer(
        source='velocity_snapshots', many=True, read_only=True
    )
    saturation_history = AISaturationSerializer(
        source='ai_saturation_snapshots', many=True, read_only=True
    )
    paa_questions = PAQuestionSerializer(many=True, read_only=True)
    cluster = serializers.SerializerMethodField()
    written_article = serializers.SerializerMethodField()

    class Meta:
        model = ResearchedKeyword
        fields = [
            'id', 'keyword', 'keyword_normalized', 'volume', 'difficulty', 'difficulty_source',
            'intent', 'tier', 'opportunity_index', 'source', 'source_detail',
            'competitor_urls', 'related_keywords', 'monthly_searches',
            'status', 'times_shown', 'last_shown_at',
            'times_rejected', 'last_rejected_at', 'cooldown_until',
            'times_selected', 'last_selected_at', 'cluster_fingerprint',
            'discovered_at', 'metrics_updated_at',
            'status_changed_at', 'velocity_history', 'saturation_history',
            'paa_questions', 'cluster', 'written_article'
        ]

    def get_cluster(self, obj):
        membership = obj.cluster_memberships.first()
        if membership:
            return {
                'cluster_id': str(membership.cluster.id),
                'pillar_keyword': membership.cluster.pillar_keyword,
                'is_pillar': membership.is_pillar
            }
        return None

    def get_written_article(self, obj):
        if obj.written_article:
            return {
                'id': str(obj.written_article.id),
                'title': obj.written_article.title,
                'article_url': obj.written_article.article_url
            }
        return None


class KeywordBulkUpsertSerializer(serializers.Serializer):
    """Serializer for bulk keyword upsert from content-factory."""
    domain = serializers.CharField()
    keywords = serializers.ListField(child=serializers.DictField())
    session_id = serializers.UUIDField(required=False, allow_null=True)


class SemanticClusterSerializer(serializers.ModelSerializer):
    """Serializer for semantic clusters (pillar topics)."""
    member_keywords = serializers.SerializerMethodField()

    class Meta:
        model = SemanticCluster
        fields = [
            'id', 'cluster_id', 'pillar_keyword', 'average_similarity',
            'total_volume', 'avg_difficulty', 'avg_velocity', 'topic_tier',
            'member_keywords', 'created_at', 'updated_at'
        ]

    def get_member_keywords(self, obj):
        return list(obj.member_keywords.values_list('keyword__keyword', flat=True))


class ClusterBulkUpsertSerializer(serializers.Serializer):
    """Serializer for bulk cluster upsert from content-factory."""
    domain = serializers.CharField()
    clusters = serializers.ListField(child=serializers.DictField())


class TopicMapSerializer(serializers.ModelSerializer):
    """Serializer for topic map snapshots."""

    class Meta:
        model = TopicMap
        fields = [
            'id', 'clustering_threshold', 'total_keywords',
            'unclustered_keywords', 'created_at'
        ]


class WrittenArticleSerializer(serializers.ModelSerializer):
    """Serializer for written article records."""

    class Meta:
        model = WrittenArticle
        fields = [
            'id', 'analytics_id', 'title', 'slug', 'category', 'article_url', 'pr_url',
            'canonical_url', 'canonical_path',
            'publish_status', 'pr_number', 'pr_merged_at', 'live_url', 'live_verified_at',
            'primary_keyword', 'published_at', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class WrittenArticleCreateSerializer(serializers.Serializer):
    """Serializer for creating written article records from content-factory."""
    domain = serializers.CharField()
    title = serializers.CharField()
    slug = serializers.CharField()
    category = serializers.CharField()
    primary_keyword = serializers.CharField()
    article_url = serializers.URLField(required=False, allow_null=True)
    pr_url = serializers.URLField(required=False, allow_null=True)
    analytics_id = serializers.UUIDField(required=False)
    canonical_url = serializers.URLField(required=False, allow_blank=True)
    canonical_path = serializers.CharField(required=False, allow_blank=True, max_length=1024)
    job_id = serializers.CharField(required=False, allow_null=True)


class ResearchSessionSerializer(serializers.ModelSerializer):
    """Serializer for research session records."""

    class Meta:
        model = ResearchSession
        fields = [
            'id', 'seed_keywords_used', 'competitors_analyzed',
            'keywords_discovered', 'keywords_updated', 'clusters_created',
            'geo_config', 'started_at', 'completed_at'
        ]
        read_only_fields = ['id', 'started_at']


class KeywordStatusUpdateSerializer(serializers.Serializer):
    """Serializer for updating keyword status."""
    status = serializers.ChoiceField(choices=[
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('in_progress', 'In Progress'),
        ('written', 'Written'),
        ('skipped', 'Skipped'),
    ])
    written_article_id = serializers.UUIDField(required=False, allow_null=True)


class ResearchFeedbackSerializer(serializers.Serializer):
    """Serializer for research exposure/selection/rejection memory updates."""
    domain = serializers.CharField()
    session_id = serializers.UUIDField(required=False, allow_null=True)
    shown_keywords = serializers.ListField(
        child=serializers.CharField(),
        allow_empty=True,
    )
    selected_keyword = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    rejected_keywords = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        allow_empty=True,
    )


class TopicFeedbackRequestSerializer(serializers.Serializer):
    """Serializer for explicit topic feedback such as a homepage thumbs-down."""
    domain = serializers.CharField(required=False, allow_blank=True)
    keyword = serializers.CharField()
    session_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    feedback_type = serializers.CharField(required=False, allow_blank=True, default='declined')
    reason_code = serializers.CharField(required=False, allow_blank=True, default='not_appropriate')
    reason_text = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    decline_scope = serializers.CharField(required=False, allow_blank=True, default='similar')
    source = serializers.CharField(required=False, allow_blank=True, default='homepage_topic_card')


class TopicFeedbackSerializer(serializers.ModelSerializer):
    """Serializer for stored topic feedback memory."""
    domain = serializers.CharField(source='organization.domain', read_only=True)
    active = serializers.SerializerMethodField()

    class Meta:
        model = TopicFeedback
        fields = [
            'id',
            'domain',
            'keyword',
            'keyword_normalized',
            'feedback_type',
            'reason_code',
            'reason_text',
            'decline_scope',
            'source',
            'session_id',
            'active',
            'created_at',
            'updated_at',
            'restored_at',
        ]
        read_only_fields = fields

    def get_active(self, obj):
        return obj.restored_at is None


class SEODashboardSerializer(serializers.Serializer):
    """Serializer for SEO dashboard aggregate data."""
    total_keywords = serializers.IntegerField()
    by_status = serializers.DictField()
    by_tier = serializers.DictField()
    top_opportunities = ResearchedKeywordListSerializer(many=True)
    clusters = serializers.IntegerField()
    articles_written = serializers.IntegerField()


from .models import ContentFactoryHealingRecord, ContentFactoryLearningEntry


class ContentFactoryHealingRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContentFactoryHealingRecord
        fields = [
            "domain",
            "github_repo",
            "failure_kind",
            "failure_family_key",
            "framework",
            "exact_signature",
            "summary",
            "normalized_failure",
            "changed_files",
            "patch_manifest",
            "validation_results",
            "evidence_artifacts",
            "snippet_or_rule",
            "applies_to",
            "promoted_payload",
            "promotion_state",
            "latest_run_id",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]
        validators = []


class ContentFactoryLearningEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = ContentFactoryLearningEntry
        fields = [
            "store",
            "scope",
            "repo_name",
            "framework",
            "entry_key",
            "payload",
            "occurrences",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]
        # The POST handler upserts on the unique tuple, so DRF's implicit
        # unique-together validator must not reject updates (same pattern as
        # ContentFactoryHealingRecordSerializer above).
        validators = []
