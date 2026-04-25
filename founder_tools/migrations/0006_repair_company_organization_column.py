from django.db import migrations


def normalize_domain(domain):
    if not domain:
        return ""
    domain = str(domain).strip().lower()
    if domain.startswith("https://"):
        domain = domain[8:]
    elif domain.startswith("http://"):
        domain = domain[7:]
    if domain.startswith("www."):
        domain = domain[4:]
    if "/" in domain:
        domain = domain.split("/", 1)[0]
    return domain


def ensure_organization_column(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return

    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            ALTER TABLE vibe_raising_viberaisingcompany
            ADD COLUMN IF NOT EXISTS organization_id bigint NULL
            """
        )
        cursor.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_constraint c
                    JOIN pg_attribute a
                      ON a.attrelid = c.conrelid
                     AND a.attnum = ANY(c.conkey)
                    WHERE c.conrelid = 'vibe_raising_viberaisingcompany'::regclass
                      AND c.contype = 'f'
                      AND a.attname = 'organization_id'
                ) THEN
                    ALTER TABLE vibe_raising_viberaisingcompany
                    ADD CONSTRAINT vibe_raising_company_organization_repair_fk
                    FOREIGN KEY (organization_id)
                    REFERENCES content_factory_organization(id)
                    DEFERRABLE INITIALLY DEFERRED;
                END IF;
            END $$;
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS vibe_raising_viberaisingcompany_organization_id_91373ad8
            ON vibe_raising_viberaisingcompany (organization_id)
            """
        )


def backfill_company_organizations(apps, schema_editor):
    VibeRaisingCompany = apps.get_model("founder_tools", "VibeRaisingCompany")
    Organization = apps.get_model("organizations", "Organization")

    for company in VibeRaisingCompany.objects.all().iterator():
        normalized_domain = normalize_domain(company.domain)
        if not normalized_domain:
            continue
        organization, created = Organization.objects.get_or_create(
            domain=normalized_domain,
            defaults={"name": company.name},
        )
        if not created and not organization.name:
            organization.name = company.name
            organization.save(update_fields=["name"])
        update_fields = []
        if company.domain != normalized_domain:
            company.domain = normalized_domain
            update_fields.append("domain")
        if company.organization_id != organization.id:
            company.organization_id = organization.id
            update_fields.append("organization")
        if update_fields:
            company.save(update_fields=update_fields)


def repair_company_organizations(apps, schema_editor):
    ensure_organization_column(apps, schema_editor)
    backfill_company_organizations(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("founder_tools", "0005_viberaisingcompany_location"),
    ]

    operations = [
        migrations.RunPython(repair_company_organizations, migrations.RunPython.noop),
    ]
