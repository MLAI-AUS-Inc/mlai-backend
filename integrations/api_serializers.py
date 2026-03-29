from rest_framework import serializers

from integrations.models import (
    ArtifactProcessingStatus,
    GmailRelevanceLabel,
    GroundednessStatus,
    MonthlyUpdateDraftStatus,
    StartupEventDatePrecision,
    StartupEventType,
)


class StartupProfileUpsertSerializer(serializers.Serializer):
    user_id = serializers.IntegerField()
    domain = serializers.CharField()
    role = serializers.CharField(required=False, allow_blank=True, default="")
    is_default_for_gmail = serializers.BooleanField(required=False, default=True)
    company_aliases = serializers.ListField(child=serializers.CharField(), required=False)
    domain_aliases = serializers.ListField(child=serializers.CharField(), required=False)
    product_names = serializers.ListField(child=serializers.CharField(), required=False)
    founder_names = serializers.ListField(child=serializers.CharField(), required=False)
    team_names = serializers.ListField(child=serializers.CharField(), required=False)
    investor_names = serializers.ListField(child=serializers.CharField(), required=False)
    investor_domains = serializers.ListField(child=serializers.CharField(), required=False)
    competitor_names = serializers.ListField(child=serializers.CharField(), required=False)
    competitor_domains = serializers.ListField(child=serializers.CharField(), required=False)
    customer_names = serializers.ListField(child=serializers.CharField(), required=False)
    customer_domains = serializers.ListField(child=serializers.CharField(), required=False)
    prospect_names = serializers.ListField(child=serializers.CharField(), required=False)
    prospect_domains = serializers.ListField(child=serializers.CharField(), required=False)
    positive_keywords = serializers.ListField(child=serializers.CharField(), required=False)
    negative_keywords = serializers.ListField(child=serializers.CharField(), required=False)
    kpi_definitions = serializers.ListField(child=serializers.DictField(), required=False)
    default_currency = serializers.CharField(required=False, allow_blank=True)
    stage = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)


class StartupUpdateRunCreateSerializer(serializers.Serializer):
    user_id = serializers.IntegerField()
    domain = serializers.CharField()
    window_months = serializers.IntegerField(required=False, default=1, min_value=1, max_value=24)


class StartupUpdateIngestSerializer(serializers.Serializer):
    page_token = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    max_results = serializers.IntegerField(required=False, default=250, min_value=1, max_value=500)
    mode = serializers.ChoiceField(required=False, default="backfill", choices=["backfill", "incremental"])
    start_history_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class StartupUpdateThreadHydrationSerializer(serializers.Serializer):
    thread_ids = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    message_ids = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    fetch_attachments = serializers.BooleanField(required=False, default=False)


class StartupUpdateBatchQuerySerializer(serializers.Serializer):
    limit = serializers.IntegerField(required=False, default=40, min_value=1, max_value=100)


class ClassificationResultItemSerializer(serializers.Serializer):
    gmail_message_id = serializers.CharField()
    relevance_label = serializers.ChoiceField(choices=GmailRelevanceLabel.choices)
    relevance_score = serializers.FloatField(required=False, default=0.0)
    relevance_reason = serializers.CharField(required=False, allow_blank=True, default="")
    needs_thread_context = serializers.BooleanField(required=False, default=False)


class ClassificationResultsSerializer(serializers.Serializer):
    results = ClassificationResultItemSerializer(many=True)


class AttachmentUpdateSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    extracted_text = serializers.CharField(required=False, allow_blank=True, default="")
    extraction_status = serializers.ChoiceField(
        choices=ArtifactProcessingStatus.choices,
        required=False,
        default=ArtifactProcessingStatus.PROCESSED,
    )
    parse_notes = serializers.CharField(required=False, allow_blank=True, default="")


class EventResultSerializer(serializers.Serializer):
    canonical_key = serializers.CharField()
    event_type = serializers.ChoiceField(choices=StartupEventType.choices)
    title = serializers.CharField()
    summary = serializers.CharField(required=False, allow_blank=True, default="")
    event_date = serializers.DateField(required=False, allow_null=True)
    month_bucket = serializers.DateField()
    date_precision = serializers.ChoiceField(
        choices=StartupEventDatePrecision.choices,
        required=False,
        default=StartupEventDatePrecision.DAY,
    )
    sentiment = serializers.CharField(required=False, allow_blank=True, default="")
    investor_importance = serializers.IntegerField(required=False, default=3, min_value=1, max_value=5)
    quantitative_facts = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    evidence_message_ids = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    evidence_attachment_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    source_thread_ids = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    confidence = serializers.FloatField(required=False, default=0.0)
    status = serializers.CharField(required=False, allow_blank=True, default="open")
    needs_review = serializers.BooleanField(required=False, default=False)
    merge_notes = serializers.CharField(required=False, allow_blank=True, default="")


class MetricResultSerializer(serializers.Serializer):
    metric_key = serializers.CharField()
    metric_name = serializers.CharField()
    value_text = serializers.CharField()
    value_number = serializers.DecimalField(required=False, allow_null=True, max_digits=20, decimal_places=4)
    unit = serializers.CharField(required=False, allow_blank=True, default="")
    observed_at = serializers.DateTimeField(required=False, allow_null=True)
    period_month = serializers.DateField()
    confidence = serializers.FloatField(required=False, default=0.0)
    evidence_message_ids = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    evidence_attachment_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    summary = serializers.CharField(required=False, allow_blank=True, default="")


class ExtractionResultItemSerializer(serializers.Serializer):
    gmail_thread_id = serializers.CharField()
    extraction_status = serializers.ChoiceField(
        choices=ArtifactProcessingStatus.choices,
        required=False,
        default=ArtifactProcessingStatus.PROCESSED,
    )
    attachment_updates = AttachmentUpdateSerializer(many=True, required=False, default=list)
    events = EventResultSerializer(many=True, required=False, default=list)
    metrics = MetricResultSerializer(many=True, required=False, default=list)


class ExtractionResultsSerializer(serializers.Serializer):
    results = ExtractionResultItemSerializer(many=True)


class DraftResultSerializer(serializers.Serializer):
    month = serializers.DateField()
    status = serializers.ChoiceField(
        choices=MonthlyUpdateDraftStatus.choices,
        required=False,
        default=MonthlyUpdateDraftStatus.DRAFT,
    )
    model_name = serializers.CharField(required=False, allow_blank=True, default="")
    groundedness_status = serializers.ChoiceField(
        choices=GroundednessStatus.choices,
        required=False,
        default=GroundednessStatus.PENDING,
    )
    structured_memo = serializers.DictField()
    evidence_event_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    evidence_metric_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    carry_forward_event_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    groundedness_notes = serializers.CharField(required=False, allow_blank=True, default="")


class DraftResultsSerializer(serializers.Serializer):
    drafts = DraftResultSerializer(many=True)
