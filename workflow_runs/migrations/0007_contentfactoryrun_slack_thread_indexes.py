from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models
from django.db.migrations.operations import AddIndex


class AddIndexConcurrentlyOnPostgres(AddIndexConcurrently):
    """CREATE INDEX CONCURRENTLY on PostgreSQL, so the run table is never locked
    for writes. CI and local checks build the same index the plain way on
    SQLite, whose schema editor has no concurrent form."""

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            return super().database_forwards(app_label, schema_editor, from_state, to_state)
        return AddIndex.database_forwards(self, app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            return super().database_backwards(app_label, schema_editor, from_state, to_state)
        return AddIndex.database_backwards(self, app_label, schema_editor, from_state, to_state)


# Robustness Phase 3 (R7 job/run merge), approved schema step 4.1: the Slack
# thread lookups content_views resolves by (channel, thread ts) and
# (channel, root message ts), now served from ContentFactoryRun.
class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('workflow_runs', '0006_contentfactoryrun_job_fields'),
    ]

    operations = [
        AddIndexConcurrentlyOnPostgres(
            model_name='contentfactoryrun',
            index=models.Index(fields=['slack_channel_id', 'slack_thread_ts'], name='cf_run_slack_thread_idx'),
        ),
        AddIndexConcurrentlyOnPostgres(
            model_name='contentfactoryrun',
            index=models.Index(fields=['slack_channel_id', 'slack_root_message_ts'], name='cf_run_slack_root_idx'),
        ),
    ]
