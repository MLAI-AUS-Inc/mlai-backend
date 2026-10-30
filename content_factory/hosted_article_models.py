"""MLAI-hosted article directories.

A hosted site's article pages are built by Content Factory from the site's own
repository and served on the customer's own domain through a proxy Worker in
the customer's site. Publishing
an article needs no repository change; the site's existing pages stay its
fallback for anything MLAI does not serve.
"""

import uuid

from django.conf import settings
from django.db import models


class HostedArticleSite(models.Model):
    class State(models.TextChoices):
        DRAFT = "draft", "Being set up"
        ACTIVE = "active", "Publishing"
        PAUSED = "paused", "Paused"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="hosted_article_sites")
    domain = models.CharField(max_length=255, unique=True)
    base_path = models.CharField(max_length=255, default="/blog/")
    state = models.CharField(max_length=20, choices=State.choices, default=State.DRAFT)
    # The site's build contract under "build" (Content Factory's hosted_articles.site_sources.SourceContract).
    profile = models.JSONField(default=dict, blank=True)
    worker_name = models.CharField(max_length=100, blank=True, default="")
    worker_url = models.CharField(max_length=255, blank=True, default="")
    manifest_digest = models.CharField(max_length=64, blank=True, default="")
    last_deployed_at = models.DateTimeField(null=True, blank=True)
    # The first time a hosted page answered through the customer's own domain.
    proxy_verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_factory_hosted_article_site"
        indexes = [models.Index(fields=["organization", "state"], name="cf_hosted_site_org_state_idx")]

    def __str__(self):
        return f"{self.domain} ({self.state})"


class HostedArticlePublication(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Queued"
        RENDERING = "rendering", "Publishing"
        DEPLOYED = "deployed", "Uploaded, waiting for the live page"
        LIVE = "live", "Live"
        FAILED = "failed", "Failed"
        WITHDRAWN = "withdrawn", "Withdrawn"

    # Publications rendered into every new deployment of the site.
    RENDERED = (Status.RENDERING, Status.DEPLOYED, Status.LIVE)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    site = models.ForeignKey(HostedArticleSite, on_delete=models.CASCADE, related_name="publications")
    run_id = models.CharField(max_length=64)
    slug = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    revision_digest = models.CharField(max_length=64, blank=True, default="")
    # Content Factory's hosted_articles.article.HostedArticle fields: the exact sanitized revision served.
    article = models.JSONField(default=dict, blank=True)
    written_article = models.ForeignKey("content_factory.WrittenArticle", null=True, blank=True, on_delete=models.SET_NULL, related_name="hosted_publications")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="hosted_article_publications")
    failure_reason = models.CharField(max_length=100, blank=True, default="")
    error = models.CharField(max_length=500, blank=True, default="")
    deployed_at = models.DateTimeField(null=True, blank=True)
    live_verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_factory_hosted_article_publication"
        constraints = [
            models.UniqueConstraint(fields=["site", "run_id"], name="cf_hosted_pub_site_run_uniq"),
            models.UniqueConstraint(fields=["site", "slug"], condition=~models.Q(slug=""), name="cf_hosted_pub_site_slug_uniq"),
        ]
        indexes = [models.Index(fields=["site", "status"], name="cf_hosted_pub_site_status_idx")]

    def __str__(self):
        return f"{self.site.domain}{self.site.base_path}{self.slug or self.run_id} ({self.status})"
