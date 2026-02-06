from django.db import migrations


def backfill_domain(apps, schema_editor):
    """
    Populate domain on AISaturation and PAQuestion from keyword → organization → domain.
    Uses raw SQL for efficiency (single UPDATE per table, no row-by-row iteration).
    """
    with schema_editor.connection.cursor() as cursor:
        # Backfill AISaturation
        cursor.execute("""
            UPDATE seo_ai_saturation AS ai
            SET domain = org.domain
            FROM seo_researched_keyword AS kw
            JOIN core_organization AS org ON org.id = kw.organization_id
            WHERE ai.keyword_id = kw.id
              AND (ai.domain = '' OR ai.domain IS NULL)
        """)
        ai_count = cursor.rowcount

        # Backfill PAQuestion
        cursor.execute("""
            UPDATE seo_paa_question AS paa
            SET domain = org.domain
            FROM seo_researched_keyword AS kw
            JOIN core_organization AS org ON org.id = kw.organization_id
            WHERE paa.keyword_id = kw.id
              AND (paa.domain = '' OR paa.domain IS NULL)
        """)
        paa_count = cursor.rowcount

    print(f"\n  Backfilled domain: {ai_count} AISaturation rows, {paa_count} PAQuestion rows")


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0024_add_thread_context_to_contentfactoryjob'),
    ]

    operations = [
        migrations.RunPython(backfill_domain, migrations.RunPython.noop),
    ]
