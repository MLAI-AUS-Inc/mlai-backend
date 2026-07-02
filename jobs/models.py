from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone


class SeekJob(models.Model):
    run_date = models.CharField(max_length=32, db_index=True)
    source_name = models.CharField(max_length=255, default="SEEK")
    keyword = models.CharField(max_length=255, db_index=True)
    title = models.CharField(max_length=500)
    company_name = models.CharField(max_length=255, blank=True, null=True)
    company_logo_url = models.URLField(max_length=1000, blank=True, null=True)
    company_domain = models.CharField(max_length=255, blank=True, null=True)
    company_stage = models.CharField(max_length=255, blank=True, null=True)
    company_size = models.CharField(max_length=255, blank=True, null=True)
    company_quality_score = models.FloatField(default=0.0)
    location = models.CharField(max_length=255, blank=True, null=True)
    job_url = models.URLField(max_length=1500)
    posted_text = models.CharField(max_length=255, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    scraped_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run_date", "job_url"], name="jobs_seek_jobs_run_url_unique"),
        ]


class JobRun(models.Model):
    run_id = models.CharField(max_length=64, unique=True, db_index=True, editable=False)
    run_date = models.CharField(max_length=32, db_index=True)
    status = models.CharField(max_length=64, db_index=True, default="queued")
    trigger_source = models.CharField(max_length=64, default="manual_api")
    collect_live = models.BooleanField(default=True)
    post_to_slack = models.BooleanField(default=False)
    post_to_notion = models.BooleanField(default=True)
    source_names = models.JSONField(blank=True, null=True)
    max_pages = models.IntegerField(blank=True, null=True)
    per_keyword_limit = models.IntegerField(blank=True, null=True)
    claimed_at = models.DateTimeField(blank=True, null=True)
    started_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)
    full_list_url = models.URLField(max_length=1500, blank=True, null=True)
    slack_posted_at = models.DateTimeField(blank=True, null=True)
    fetched_count = models.IntegerField(default=0)
    matched_count = models.IntegerField(default=0)
    deduped_count = models.IntegerField(default=0)
    ranked_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self.run_id:
            self.run_id = f"{self.run_date}-{uuid.uuid4().hex[:8]}"
        super().save(*args, **kwargs)


class JobListing(models.Model):
    run = models.ForeignKey(JobRun, to_field="run_id", db_column="run_id", related_name="jobs", on_delete=models.CASCADE)
    run_date = models.CharField(max_length=32, db_index=True)
    title = models.CharField(max_length=500)
    company_name = models.CharField(max_length=255, blank=True, null=True)
    company_logo_url = models.URLField(max_length=1000, blank=True, null=True)
    company_domain = models.CharField(max_length=255, blank=True, null=True)
    company_stage = models.CharField(max_length=255, blank=True, null=True)
    company_size = models.CharField(max_length=255, blank=True, null=True)
    company_quality_score = models.FloatField(default=0.0)
    location = models.CharField(max_length=255, blank=True, null=True)
    is_remote = models.BooleanField(default=False)
    remote_region = models.CharField(max_length=255, blank=True, null=True)
    remote_eligibility = models.CharField(max_length=255, blank=True, null=True)
    remote_eligibility_score = models.FloatField(default=0.0)
    country = models.CharField(max_length=255, blank=True, null=True)
    city = models.CharField(max_length=255, blank=True, null=True)
    job_url = models.URLField(max_length=1500)
    apply_url = models.URLField(max_length=1500, blank=True, null=True)
    source_name = models.CharField(max_length=255, db_index=True)
    source_type = models.CharField(max_length=255, blank=True, null=True)
    date_posted = models.DateTimeField(blank=True, null=True)
    date_scraped = models.DateTimeField(default=timezone.now)
    posted_text = models.CharField(max_length=255, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    ai_score = models.FloatField(default=0.0)
    startup_score = models.FloatField(default=0.0)
    australia_score = models.FloatField(default=0.0)
    remote_score = models.FloatField(default=0.0)
    recency_score = models.FloatField(default=0.0)
    source_score = models.FloatField(default=0.0)
    quality_score = models.FloatField(default=0.0)
    ranking_score = models.FloatField(default=0.0)
    bucket = models.CharField(max_length=255, db_index=True, blank=True, null=True)
    summary = models.TextField(blank=True, null=True)
    why_selected = models.TextField(blank=True, null=True)
    dedupe_key = models.CharField(max_length=1500, db_index=True)
    is_top_pick = models.BooleanField(default=False)
    rank = models.IntegerField(blank=True, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "dedupe_key"], name="jobs_job_listing_run_dedupe_unique"),
        ]


class SourceRunLog(models.Model):
    run = models.ForeignKey(JobRun, to_field="run_id", db_column="run_id", related_name="source_logs", on_delete=models.CASCADE)
    source_name = models.CharField(max_length=255)
    status = models.CharField(max_length=64)
    fetched_count = models.IntegerField(default=0)
    error_message = models.TextField(blank=True, null=True)
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(blank=True, null=True)
