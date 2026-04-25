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


class Migration(migrations.Migration):
    dependencies = [
        ("founder_tools", "0002_company_organization"),
    ]

    operations = [
        migrations.RunPython(backfill_company_organizations, migrations.RunPython.noop),
    ]
