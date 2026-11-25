"""Print one article run's final-check record: its findings, hold owner and repair history.

Read-only. Staff use it instead of opening a server session on Content Factory:
``python manage.py article_quality <run_id>`` (add ``--json`` for the raw record).
"""
import json

from django.core.management.base import BaseCommand, CommandError

from content_factory.article_quality_record import quality_record


def render(record) -> list[str]:
    """The record as plain lines, most important first."""
    lines = [
        f"Run {record['runId']}: status {record['status'] or 'none'}, held for {record['holdOwner'] or 'nobody'}.",
        f"Blocker {record['blockerCode'] or 'none'}. Stop reason {record['stopReason'] or 'none'}. "
        f"Repair attempts {record['attempts']}.",
    ]
    for entry in record["history"]:
        lines.append(f"  Attempt {entry.get('attempt')}: {', '.join(entry.get('buckets') or []) or 'no action'}"
                     f" -> {entry.get('result') or 'unknown'}"
                     + (f" (targeted {', '.join(entry['targets'])})" if entry.get("targets") else ""))
    if not record["findingsReported"]:
        lines.append("No findings list was sent for this check (CONTENT_FACTORY_QUALITY_FINDINGS_CALLBACK is off "
                     "or the check predates it).")
    for index, item in enumerate(record["findings"], start=1):
        lines.append(f"{index}. [{item['severity']}] {item['code']} ({item['source']})")
        if item["claim"]:
            lines.append(f"   Claim: {item['claim']}")
        if item["reason"]:
            lines.append(f"   Reason: {item['reason']}")
    for entry in record["removedClaims"]:
        for text in entry["removed"]:
            lines.append(f"Removed automatically in attempt {entry['attempt']}: {text}")
    return lines


class Command(BaseCommand):
    """Read-only view of the final check that is holding an article."""
    help = "Show an article run's final-check findings, hold owner and repair history, without changes."

    def add_arguments(self, parser):
        parser.add_argument("run_id")
        parser.add_argument("--json", action="store_true", dest="as_json")

    def handle(self, *args, **options):
        from workflow_runs.models import ContentFactoryRun
        run = ContentFactoryRun.objects.filter(run_id=options["run_id"]).first()
        if run is None:
            raise CommandError(f"No run {options['run_id']!r}.")
        record = quality_record(run)
        if options["as_json"]:
            self.stdout.write(json.dumps(record, ensure_ascii=False, indent=2))
            return
        for line in render(record):
            self.stdout.write(line)
