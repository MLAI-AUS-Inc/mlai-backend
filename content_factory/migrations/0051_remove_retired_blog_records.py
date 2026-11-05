"""Remove retired founder-blog tables only after their protected export matches."""
import hashlib
import io
import json
import zipfile

from django.db import migrations

ARCHIVE = {'archive_uri': 'gs://mlai-main-website-retired-blog-archive/hosted-blog-retirement/2026-11-05/c293aa206addda0885ad0fe282e4185bdd7f72462dacac5db1094f45a72cd2dd.zip', 'generation': '1793845488886957', 'sha256': 'c293aa206addda0885ad0fe282e4185bdd7f72462dacac5db1094f45a72cd2dd', 'row_counts': {'publications': 0, 'sites': 1}, 'row_digests': {'publications': '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945', 'sites': '5e19ab828f73c67df64abff1f71b12feca44510ea60671fa6a84228c3b7c4324'}}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def require_retirement_archive(apps, schema_editor):
    """Refuse table removal without the exact protected export of every retired row."""
    site_model = apps.get_model("content_factory", "HostedArticleSite")
    publication_model = apps.get_model("content_factory", "HostedArticlePublication")
    database = schema_editor.connection.alias
    # Hold the tables through this atomic PostgreSQL migration. No callback can
    # insert a row between the export comparison and DeleteModel operations.
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute("LOCK TABLE content_factory_hosted_article_site, "
                           "content_factory_hosted_article_publication IN ACCESS EXCLUSIVE MODE")
    sites = list(site_model.objects.using(database).order_by("pk"))
    publications = list(publication_model.objects.using(database).order_by("pk"))
    # Fresh local/test installations have no historical data to export.
    if not sites and not publications:
        return
    if any(site.state != "retired" or site.worker_url
           or (site.profile.get("retirement") or {}).get("status") != "retired" for site in sites):
        raise RuntimeError("Every legacy blog must be fully retired before removing its tables.")
    if any(publication.status != "withdrawn" for publication in publications):
        raise RuntimeError("Legacy publications must be withdrawn before removing their table.")
    from django.core import serializers
    records = {"sites": json.loads(serializers.serialize("json", sites)),
               "publications": json.loads(serializers.serialize("json", publications))}
    counts = {key: len(rows) for key, rows in records.items()}
    digests = {key: _digest(rows) for key, rows in records.items()}
    if ARCHIVE is None or counts != ARCHIVE["row_counts"] or digests != ARCHIVE["row_digests"]:
        raise RuntimeError("Legacy blog rows differ from the reviewed cold-storage export.")
    from core.firebase_utils import get_storage_bucket
    client = get_storage_bucket().client
    bucket_name, object_name = ARCHIVE["archive_uri"].removeprefix("gs://").split("/", 1)
    bucket = client.get_bucket(bucket_name)
    if (bucket.storage_class != "COLDLINE"
            or not bucket.iam_configuration.uniform_bucket_level_access_enabled
            or bucket.iam_configuration.public_access_prevention != "enforced"):
        raise RuntimeError("The reviewed archive bucket must remain private and in cold storage.")
    generation = int(ARCHIVE["generation"])
    blob = bucket.blob(object_name, generation=generation)
    blob.reload(if_generation_match=generation)
    if not blob.temporary_hold or blob.storage_class != "COLDLINE":
        raise RuntimeError("The reviewed archive object must retain its deletion hold.")
    data = blob.download_as_bytes(if_generation_match=generation, checksum="crc32c")
    if hashlib.sha256(data).hexdigest() != ARCHIVE["sha256"]:
        raise RuntimeError("The downloaded archive does not match its reviewed digest.")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if set(archive.namelist()) != {"records.json", "hosted-tables.dump", "pg-restore-list.txt", "RESTORE.txt"}:
            raise RuntimeError("The reviewed archive is incomplete.")
        archived = json.loads(archive.read("records.json"))
    if archived != records:
        raise RuntimeError("The immutable archive does not contain the current retired rows.")


class Migration(migrations.Migration):
    atomic = True
    dependencies = [("content_factory", "0050_retire_hosted_article_sites")]
    operations = [
        migrations.RunPython(require_retirement_archive, migrations.RunPython.noop),
        migrations.DeleteModel(name="HostedArticlePublication"),
        migrations.DeleteModel(name="HostedArticleSite"),
    ]
