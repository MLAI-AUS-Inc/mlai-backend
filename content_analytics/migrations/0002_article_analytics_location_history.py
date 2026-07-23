from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit, urlunsplit

from django.db import migrations, models
import django.db.models.deletion
from django.utils import timezone


def _normalize_domain(value):
    text = str(value or "").strip().lower()
    parsed = urlsplit(text if "://" in text else f"//{text}")
    host = parsed.hostname or parsed.path.split("/", 1)[0]
    return host.removeprefix("www.").rstrip(".")


def _normalize_path(value):
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text if "://" in text else f"//placeholder{text if text.startswith('/') else '/' + text}")
    path = parsed.path or "/"
    protected = re.sub(
        r"%(?=(?:3B|2C|2F|3F|3A|40|26|3D|2B|24|23))",
        "%25",
        path,
        flags=re.IGNORECASE,
    )
    path = unquote(protected)
    if not path.startswith("/"):
        path = f"/{path}"
    return path.rstrip("/") or "/"


def backfill_article_locations(apps, schema_editor):
    WrittenArticle = apps.get_model("content_factory", "WrittenArticle")
    Organization = apps.get_model("organizations", "Organization")
    ArticleAnalyticsLocation = apps.get_model("content_analytics", "ArticleAnalyticsLocation")
    domains = {
        row["id"]: _normalize_domain(row["domain"])
        for row in Organization.objects.values("id", "domain")
    }
    rows = []
    for article in WrittenArticle.objects.all().iterator(chunk_size=500):
        expected_domain = domains.get(article.organization_id, "")
        if not expected_domain:
            continue
        url_candidates = []
        if article.publish_status == "live" and article.live_url:
            url_candidates.append(article.live_url)
        if article.canonical_url:
            url_candidates.append(article.canonical_url)
        if article.live_url:
            url_candidates.append(article.live_url)
        canonical_url = ""
        canonical_path = ""
        for raw_url in url_candidates:
            parsed = urlsplit(str(raw_url or "").strip())
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                continue
            if _normalize_domain(parsed.hostname) != expected_domain:
                continue
            exact_path = parsed.path or "/"
            canonical_url = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), exact_path, "", ""))
            canonical_path = _normalize_path(exact_path)
            break
        if not canonical_path:
            canonical_path = _normalize_path(article.canonical_path)
        if not canonical_path:
            continue
        if not canonical_url:
            canonical_url = f"https://{expected_domain}{canonical_path}"
        initial_at = article.created_at or article.published_at or article.live_verified_at or timezone.now()
        rows.append(
            ArticleAnalyticsLocation(
                organization_id=article.organization_id,
                article_id=article.pk,
                canonical_url=canonical_url,
                canonical_path=canonical_path,
                valid_from=initial_at.date(),
                source="migration",
                confirmed_at=article.live_verified_at,
            )
        )
        if len(rows) >= 500:
            ArticleAnalyticsLocation.objects.bulk_create(rows, batch_size=500)
            rows = []
    if rows:
        ArticleAnalyticsLocation.objects.bulk_create(rows, batch_size=500)


def reverse_article_locations(apps, schema_editor):
    apps.get_model("content_analytics", "ArticleAnalyticsLocation").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("content_analytics", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ArticleAnalyticsLocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("canonical_url", models.URLField(max_length=2048)),
                ("canonical_path", models.CharField(db_index=True, max_length=1024)),
                ("valid_from", models.DateField(db_index=True)),
                ("valid_to", models.DateField(blank=True, db_index=True, null=True)),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("generated", "Generated canonical location"),
                            ("canonical", "Canonical location"),
                            ("sitemap", "Sitemap-confirmed live location"),
                            ("migration", "Migrated current location"),
                        ],
                        default="canonical",
                        max_length=24,
                    ),
                ),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "article",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="analytics_locations",
                        to="content_factory.writtenarticle",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="article_analytics_locations",
                        to="organizations.organization",
                    ),
                ),
            ],
            options={
                "db_table": "content_analytics_article_location",
                "ordering": ["valid_from", "id"],
            },
        ),
        migrations.AddConstraint(
            model_name="articleanalyticslocation",
            constraint=models.UniqueConstraint(
                condition=models.Q(("valid_to__isnull", True)),
                fields=("article",),
                name="an_article_one_active_loc",
            ),
        ),
        migrations.AddConstraint(
            model_name="articleanalyticslocation",
            constraint=models.UniqueConstraint(
                fields=("article", "valid_from"),
                name="an_article_loc_start_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="articleanalyticslocation",
            constraint=models.CheckConstraint(
                check=models.Q(valid_to__isnull=True) | models.Q(valid_to__gte=models.F("valid_from")),
                name="an_location_valid_range",
            ),
        ),
        migrations.AddIndex(
            model_name="articleanalyticslocation",
            index=models.Index(fields=["article", "valid_from", "valid_to"], name="analytics_location_day_idx"),
        ),
        migrations.AddIndex(
            model_name="articleanalyticslocation",
            index=models.Index(fields=["organization", "valid_from"], name="analytics_location_org_idx"),
        ),
        migrations.RunPython(backfill_article_locations, reverse_article_locations),
    ]
