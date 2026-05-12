from django.db import migrations
from django.utils import timezone
from django.utils.text import slugify


HOME_EQUITY_DECLINES = [
    "how to calculate equity in a house",
    "how to calculate total equity",
]


def normalize_topic(value):
    return " ".join(str(value or "").replace("-", " ").replace("_", " ").lower().strip().split())


def article_topic_texts(article):
    values = [
        getattr(article, "primary_keyword", ""),
        getattr(article, "title", ""),
        str(getattr(article, "slug", "") or "").replace("-", " "),
    ]
    seen = set()
    for value in values:
        normalized = normalize_topic(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            yield normalized
        slug_text = normalize_topic(slugify(str(value or "")).replace("-", " "))
        if slug_text and slug_text not in seen:
            seen.add(slug_text)
            yield slug_text


def link_exact_written_keywords(apps, schema_editor):
    Organization = apps.get_model("organizations", "Organization")
    ResearchedKeyword = apps.get_model("content_factory", "ResearchedKeyword")
    WrittenArticle = apps.get_model("content_factory", "WrittenArticle")

    now = timezone.now()
    for organization in Organization.objects.all().iterator():
        articles_by_key = {}
        for article in WrittenArticle.objects.filter(organization=organization).iterator():
            for key in article_topic_texts(article):
                articles_by_key.setdefault(key, article)
        if not articles_by_key:
            continue

        for keyword in ResearchedKeyword.objects.filter(
            organization=organization,
            keyword_normalized__in=list(articles_by_key.keys()),
        ).iterator():
            article = articles_by_key.get(normalize_topic(keyword.keyword_normalized))
            if article is None:
                article = articles_by_key.get(normalize_topic(keyword.keyword))
            if article is None:
                continue
            keyword.status = "written"
            keyword.written_article_id = article.id
            keyword.status_changed_at = now
            keyword.save(update_fields=["status", "written_article", "status_changed_at"])


def decline_known_mlai_off_lane_topics(apps, schema_editor):
    Organization = apps.get_model("organizations", "Organization")
    TopicFeedback = apps.get_model("content_factory", "TopicFeedback")

    organization = Organization.objects.filter(domain__iexact="mlai.au").first()
    if organization is None:
        return

    for keyword in HOME_EQUITY_DECLINES:
        normalized = normalize_topic(keyword)
        feedback = TopicFeedback.objects.filter(
            organization=organization,
            keyword_normalized=normalized,
            feedback_type="declined",
            restored_at__isnull=True,
        ).first()
        defaults = {
            "keyword": keyword,
            "reason_code": "off_topic",
            "reason_text": "Home-equity search intent is not appropriate for MLAI startup marketing.",
            "decline_scope": "similar",
            "source": "backfill_already_written_topic_leakage",
        }
        if feedback:
            for field, value in defaults.items():
                setattr(feedback, field, value)
            feedback.save(update_fields=[*defaults.keys(), "updated_at"])
            continue
        TopicFeedback.objects.create(
            organization=organization,
            keyword_normalized=normalized,
            feedback_type="declined",
            **defaults,
        )


def backfill_topic_coverage_memory(apps, schema_editor):
    link_exact_written_keywords(apps, schema_editor)
    decline_known_mlai_off_lane_topics(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("content_factory", "0006_topic_feedback"),
    ]

    operations = [
        migrations.RunPython(backfill_topic_coverage_memory, migrations.RunPython.noop),
    ]
