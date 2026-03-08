from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.contrib.auth import get_user_model

User = get_user_model()

class MyTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token['role'] = user.role
        return token

class RegisterSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['email', 'first_name', 'last_name']
        extra_kwargs = {
            'email': {'required': True},
            'first_name': {'required': False},
            'last_name': {'required': False},
        } 

    def create(self, validated_data):
        email = validated_data['email']
        first_name = validated_data.get('first_name', '')
        last_name = validated_data.get('last_name', '')
        user, created = User.objects.get_or_create(email=email)
        if created:
            user.first_name = first_name
            user.last_name = last_name
            user.is_active = False
            user.save()
        return user

from .models import Hackathon

class HackathonSerializer(serializers.ModelSerializer):
    class Meta:
        model = Hackathon
        fields = ['name', 'slug', 'description', 'start_date', 'end_date', 'bg_image_url']

class UserSerializer(serializers.ModelSerializer):
    team_avatar = serializers.FileField(write_only=True, required=False)
    
    class Meta:
        model = User
        fields = ['id', 'email', 'first_name', 'last_name', 'full_name', 'phone', 'about', 'role', 'avatar_url', 'is_superuser', 'team_avatar']
        read_only_fields = ['email', 'role', 'is_superuser']


from .models import GeneratedComponent, ComponentMapping


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
    SemanticCluster, TopicMap, WrittenArticle, ResearchSession
)


class KeywordVelocitySerializer(serializers.ModelSerializer):
    """Serializer for velocity snapshot data."""

    class Meta:
        model = KeywordVelocity
        fields = [
            'absolute_volume', 'velocity_score', 'trend_status',
            'daily_volumes', 'captured_at'
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
            'id', 'keyword', 'volume', 'difficulty', 'intent',
            'tier', 'opportunity_index', 'source', 'status',
            'times_shown', 'last_shown_at', 'times_rejected', 'last_rejected_at',
            'cooldown_until', 'times_selected', 'last_selected_at',
            'cluster_fingerprint',
            'latest_velocity', 'latest_saturation', 'paa_count',
            'discovered_at', 'metrics_updated_at'
        ]

    def get_latest_velocity(self, obj):
        snapshot = obj.velocity_snapshots.first()
        if snapshot:
            return {
                'velocity_score': snapshot.velocity_score,
                'trend_status': snapshot.trend_status
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
            'id', 'keyword', 'keyword_normalized', 'volume', 'difficulty',
            'intent', 'tier', 'opportunity_index', 'source', 'source_detail',
            'competitor_urls', 'status', 'times_shown', 'last_shown_at',
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
            'id', 'title', 'slug', 'category', 'article_url', 'pr_url',
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


class SEODashboardSerializer(serializers.Serializer):
    """Serializer for SEO dashboard aggregate data."""
    total_keywords = serializers.IntegerField()
    by_status = serializers.DictField()
    by_tier = serializers.DictField()
    top_opportunities = ResearchedKeywordListSerializer(many=True)
    clusters = serializers.IntegerField()
    articles_written = serializers.IntegerField()


class ContentFactoryRunAttemptSerializer(serializers.Serializer):
    attempt = serializers.IntegerField()
    status = serializers.CharField()
    message = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    started_at = serializers.DateTimeField(required=False, allow_null=True)
    completed_at = serializers.DateTimeField(required=False, allow_null=True)
    artifacts = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    error = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    input_path = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    output_path = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    notes_path = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    status_path = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class ContentFactoryRunStepSerializer(serializers.Serializer):
    name = serializers.CharField()
    required = serializers.BooleanField(required=False, default=True)
    status = serializers.CharField(required=False, default="pending")
    attempts = serializers.IntegerField(required=False, default=0)
    message = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    started_at = serializers.DateTimeField(required=False, allow_null=True)
    completed_at = serializers.DateTimeField(required=False, allow_null=True)
    artifacts = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    error = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    latest_attempt_path = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    attempt_history = ContentFactoryRunAttemptSerializer(many=True, required=False, default=list)


class ContentFactoryRunSyncSerializer(serializers.Serializer):
    run_id = serializers.CharField()
    workflow = serializers.CharField()
    domain = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    github_repo = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    slack_user_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    status = serializers.CharField()
    current_step = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    artifact_root = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    step_order = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    started_at = serializers.DateTimeField(required=False, allow_null=True)
    updated_at = serializers.DateTimeField(required=False, allow_null=True)
    acceptance_summary = serializers.DictField(required=False, default=dict)
    verification_summary = serializers.DictField(required=False, default=dict)
    approval_state = serializers.CharField(required=False, default="not_required")
    resume_available = serializers.BooleanField(required=False, default=False)
    error = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    result = serializers.DictField(required=False, default=dict, allow_null=True)
    run_request = serializers.DictField(required=False, default=dict, allow_null=True)
    run_request_path = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    step_states = serializers.DictField(required=False, default=dict)


class ContentFactoryRunControlSerializer(serializers.Serializer):
    actor = serializers.CharField(required=False, allow_blank=True, default="content-factory")
