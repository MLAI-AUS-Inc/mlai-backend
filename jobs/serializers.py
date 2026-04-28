from __future__ import annotations

from rest_framework import serializers

from .models import JobListing, JobRun


class DailyRunRequestSerializer(serializers.Serializer):
    collect_live = serializers.BooleanField(default=True)
    post_to_slack = serializers.BooleanField(default=False)
    post_to_notion = serializers.BooleanField(default=True)
    sources = serializers.ListField(child=serializers.CharField(), required=False, allow_null=True)
    max_pages = serializers.IntegerField(required=False, allow_null=True)
    per_keyword_limit = serializers.IntegerField(required=False, allow_null=True)


class DailyRunResponseSerializer(serializers.Serializer):
    run_id = serializers.CharField()
    status = serializers.CharField()
    status_url = serializers.CharField()
    full_list_url = serializers.CharField()


class JobListingSerializer(serializers.ModelSerializer):
    class Meta:
        model = JobListing
        fields = [
            "id",
            "title",
            "company_name",
            "company_logo_url",
            "company_domain",
            "company_stage",
            "company_size",
            "company_quality_score",
            "location",
            "is_remote",
            "remote_region",
            "remote_eligibility",
            "remote_eligibility_score",
            "country",
            "city",
            "job_url",
            "apply_url",
            "source_name",
            "source_type",
            "posted_text",
            "ai_score",
            "startup_score",
            "australia_score",
            "remote_score",
            "ranking_score",
            "bucket",
            "summary",
            "why_selected",
            "is_top_pick",
            "rank",
        ]


class JobRunSerializer(serializers.ModelSerializer):
    top_jobs = JobListingSerializer(many=True, read_only=True)

    class Meta:
        model = JobRun
        fields = "__all__"

