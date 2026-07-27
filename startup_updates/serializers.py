from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from rest_framework import serializers

from startup_updates.models import (
    ArtifactProcessingStatus,
    GmailRelevanceLabel,
    GroundednessStatus,
    MonthlyUpdateDraftStatus,
    StartupEventDatePrecision,
    StartupEventType,
)
from vibe_raising.serializer_fields import AudienceVisibilityField


class RoundingDecimalField(serializers.DecimalField):
    """A DecimalField that rounds over-precise input down to ``decimal_places``
    instead of rejecting it with a 400.

    LLM-extracted metric values routinely arrive with more precision than we
    store (e.g. ``0.33333333``). Rounding to the stored precision is the intended
    behaviour rather than failing the whole extraction batch. DRF rejects in
    ``validate_precision`` before it would quantize, so we round there first.
    """

    def validate_precision(self, value):
        if self.decimal_places is not None:
            try:
                value = value.quantize(
                    Decimal(1).scaleb(-self.decimal_places),
                    rounding=ROUND_HALF_UP,
                )
            except InvalidOperation:
                # Magnitude too large to quantize into the decimal context; let
                # the parent raise the normal max_digits validation error (400).
                pass
        return super().validate_precision(value)


class TruncatingCharField(serializers.CharField):
    """CharField that truncates over-long input to ``truncate_length`` instead
    of rejecting it. LLM-extracted free text can exceed the DB column width
    (varchar(N)); truncating keeps the record rather than 500-ing the whole
    extraction batch on a DataError at INSERT time."""

    def __init__(self, *args, truncate_length: int, **kwargs):
        self.truncate_length = truncate_length
        super().__init__(*args, **kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if isinstance(value, str) and len(value) > self.truncate_length:
            value = value[: self.truncate_length]
        return value


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
    organization_kind = serializers.CharField(required=False, allow_blank=True)
    organizationKind = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        if "organizationKind" in attrs and "organization_kind" not in attrs:
            attrs["organization_kind"] = attrs["organizationKind"]
        attrs.pop("organizationKind", None)
        return attrs


class StartupUpdateRunCreateSerializer(serializers.Serializer):
    user_id = serializers.IntegerField()
    domain = serializers.CharField()
    window_months = serializers.IntegerField(required=False, default=1, min_value=1, max_value=24)
    input_sources = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    inputSources = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    target_month = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    targetMonth = serializers.CharField(required=False, allow_blank=True, allow_null=True)


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


class SlackClassificationResultItemSerializer(serializers.Serializer):
    slack_thread_id = serializers.CharField()
    relevance_label = serializers.ChoiceField(choices=GmailRelevanceLabel.choices)
    relevance_score = serializers.FloatField(required=False, default=0.0)
    relevance_reason = serializers.CharField(required=False, allow_blank=True, default="")
    needs_extraction = serializers.BooleanField(required=False)
    extraction_hints = serializers.DictField(required=False, default=dict)


class SlackClassificationResultsSerializer(serializers.Serializer):
    results = SlackClassificationResultItemSerializer(many=True)


class LinearClassificationResultItemSerializer(serializers.Serializer):
    linear_project_id = serializers.CharField()
    relevance_label = serializers.ChoiceField(choices=GmailRelevanceLabel.choices)
    relevance_score = serializers.FloatField(required=False, default=0.0)
    relevance_reason = serializers.CharField(required=False, allow_blank=True, default="")
    needs_extraction = serializers.BooleanField(required=False)
    extraction_hints = serializers.DictField(required=False, default=dict)


class LinearClassificationResultsSerializer(serializers.Serializer):
    results = LinearClassificationResultItemSerializer(many=True)


class NotionClassificationResultItemSerializer(serializers.Serializer):
    notion_page_id = serializers.CharField()
    notion_chunk_id = serializers.CharField(required=False, allow_blank=True, default="")
    relevance_label = serializers.ChoiceField(choices=GmailRelevanceLabel.choices)
    relevance_score = serializers.FloatField(required=False, default=0.0)
    relevance_reason = serializers.CharField(required=False, allow_blank=True, default="")
    needs_extraction = serializers.BooleanField(required=False)
    important_block_ids = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    extraction_hint = serializers.CharField(required=False, allow_blank=True, default="")


class NotionClassificationResultsSerializer(serializers.Serializer):
    results = NotionClassificationResultItemSerializer(many=True)


class GoogleAnalyticsClassificationResultItemSerializer(serializers.Serializer):
    ga_report_id = serializers.CharField()
    relevance_label = serializers.ChoiceField(choices=GmailRelevanceLabel.choices)
    relevance_score = serializers.FloatField(required=False, default=0.0)
    relevance_reason = serializers.CharField(required=False, allow_blank=True, default="")
    needs_extraction = serializers.BooleanField(required=False)
    extraction_hints = serializers.DictField(required=False, default=dict)


class GoogleAnalyticsClassificationResultsSerializer(serializers.Serializer):
    results = GoogleAnalyticsClassificationResultItemSerializer(many=True)


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
    canonical_key = TruncatingCharField(truncate_length=255)
    event_type = serializers.ChoiceField(choices=StartupEventType.choices)
    title = TruncatingCharField(truncate_length=255)
    summary = serializers.CharField(required=False, allow_blank=True, default="")
    event_date = serializers.DateField(required=False, allow_null=True)
    month_bucket = serializers.DateField()
    date_precision = serializers.ChoiceField(
        choices=StartupEventDatePrecision.choices,
        required=False,
        default=StartupEventDatePrecision.DAY,
    )
    sentiment = TruncatingCharField(truncate_length=20, required=False, allow_blank=True, default="")
    investor_importance = serializers.IntegerField(required=False, default=3, min_value=1, max_value=5)
    quantitative_facts = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    evidence_message_ids = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    evidence_attachment_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    source_thread_ids = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    confidence = serializers.FloatField(required=False, default=0.0)
    status = TruncatingCharField(truncate_length=20, required=False, allow_blank=True, default="open")
    needs_review = serializers.BooleanField(required=False, default=False)
    merge_notes = serializers.CharField(required=False, allow_blank=True, default="")


class MetricResultSerializer(serializers.Serializer):
    metric_key = TruncatingCharField(truncate_length=100)
    metric_name = TruncatingCharField(truncate_length=255)
    value_text = TruncatingCharField(truncate_length=255)
    value_number = RoundingDecimalField(required=False, allow_null=True, max_digits=20, decimal_places=4)
    unit = TruncatingCharField(truncate_length=50, required=False, allow_blank=True, default="")
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


class SlackExtractionResultItemSerializer(serializers.Serializer):
    slack_thread_id = serializers.CharField()
    extraction_status = serializers.ChoiceField(
        choices=ArtifactProcessingStatus.choices,
        required=False,
        default=ArtifactProcessingStatus.PROCESSED,
    )
    events = EventResultSerializer(many=True, required=False, default=list)
    metrics = MetricResultSerializer(many=True, required=False, default=list)


class SlackExtractionResultsSerializer(serializers.Serializer):
    results = SlackExtractionResultItemSerializer(many=True)


class LinearExtractionResultItemSerializer(serializers.Serializer):
    linear_project_id = serializers.CharField()
    extraction_status = serializers.ChoiceField(
        choices=ArtifactProcessingStatus.choices,
        required=False,
        default=ArtifactProcessingStatus.PROCESSED,
    )
    events = EventResultSerializer(many=True, required=False, default=list)
    metrics = MetricResultSerializer(many=True, required=False, default=list)


class LinearExtractionResultsSerializer(serializers.Serializer):
    results = LinearExtractionResultItemSerializer(many=True)


class NotionExtractionResultItemSerializer(serializers.Serializer):
    notion_page_id = serializers.CharField()
    notion_chunk_id = serializers.CharField(required=False, allow_blank=True, default="")
    extraction_status = serializers.ChoiceField(
        choices=ArtifactProcessingStatus.choices,
        required=False,
        default=ArtifactProcessingStatus.PROCESSED,
    )
    events = EventResultSerializer(many=True, required=False, default=list)
    metrics = MetricResultSerializer(many=True, required=False, default=list)


class NotionExtractionResultsSerializer(serializers.Serializer):
    results = NotionExtractionResultItemSerializer(many=True)


class GoogleAnalyticsExtractionResultItemSerializer(serializers.Serializer):
    ga_report_id = serializers.CharField()
    extraction_status = serializers.ChoiceField(
        choices=ArtifactProcessingStatus.choices,
        required=False,
        default=ArtifactProcessingStatus.PROCESSED,
    )
    events = EventResultSerializer(many=True, required=False, default=list)
    metrics = MetricResultSerializer(many=True, required=False, default=list)


class GoogleAnalyticsExtractionResultsSerializer(serializers.Serializer):
    results = GoogleAnalyticsExtractionResultItemSerializer(many=True)


class CurationResultsSerializer(serializers.Serializer):
    candidates = serializers.ListField(child=serializers.DictField(), required=False, default=list)


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
    audience_visibility = AudienceVisibilityField(required=False)
    audienceVisibility = AudienceVisibilityField(required=False)

    def validate(self, attrs):
        snake_case_visibility = attrs.get("audience_visibility")
        camel_case_visibility = attrs.pop("audienceVisibility", None)
        if snake_case_visibility is not None and camel_case_visibility is not None:
            if snake_case_visibility != camel_case_visibility:
                raise serializers.ValidationError(
                    {"audienceVisibility": "Use one audience visibility value set per draft."}
                )
        if snake_case_visibility is None and camel_case_visibility is not None:
            attrs["audience_visibility"] = camel_case_visibility
        return attrs


class DraftResultsSerializer(serializers.Serializer):
    drafts = DraftResultSerializer(many=True)
