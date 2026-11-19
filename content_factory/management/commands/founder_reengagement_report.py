"""Read-only re-engagement report and email drafts for stalled startups (plan E11, section 6.7).

Buckets every organisation whose startup never connected its website by its
server-owned next step, gives each a deep link its owner can open, and drafts
the one email and one follow-up per bucket (decision D14). It writes the
report and drafts to stdout or files and never sends anything.

``--send`` exists only to refuse: without the owner's go-ahead setting
(``FOUNDER_REENGAGEMENT_SEND_APPROVED``) it stops with an error; with it, it
is still a dry run that lists who would get which email, and asking for a
real send (``--no-dry-run``) stops with an error because no sender is built
for this one-off email. Plan 6.7 sends it as personal email, by hand.

Postgres runs the reads inside a READ ONLY transaction that is rolled back.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.utils import timezone

from content_factory import founder_reengagement as reengagement


def _read_json(path, label):
    if not path:
        return {}
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise CommandError(f"Could not read {label} {path!r}: {exc}") from exc
    if not isinstance(value, dict):
        raise CommandError(f"{label} {path!r} must be a JSON object keyed by organisation id.")
    return value


def _now(value):
    if not value:
        return timezone.now()
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CommandError(f"--now {value!r} is not an ISO datetime.") from exc
    return moment if timezone.is_aware(moment) else timezone.make_aware(moment, timezone.get_default_timezone())


class Command(BaseCommand):
    help = ("Read-only report of never-connected startups bucketed by their next setup step, with a deep link "
            "per startup and the one-off re-engagement email drafts. Never sends.")

    def add_arguments(self, parser):
        parser.add_argument("--database", default=DEFAULT_DB_ALIAS, help="Database alias to read.")
        parser.add_argument("--org", type=int, action="append", default=[], help="Only this organisation id.")
        parser.add_argument("--now", default="", help="Report time (ISO datetime). Defaults to now.")
        parser.add_argument("--overrides", default="",
                            help="JSON {org_id: {bucket, suggestion}} from ops' own checks (typo fix, test org).")
        parser.add_argument("--sent-ledger", default="",
                            help="JSON {org_id: {first, follow_up}} recording what was already sent by hand.")
        parser.add_argument("--workshop-day", action="append", default=[],
                            help="ISO date of a workshop session; its sign-ups also get the session offer.")
        parser.add_argument("--json", action="store_true", help="Print the report as JSON instead of a table.")
        parser.add_argument("--out", default="", help="Also write the JSON report to this file.")
        parser.add_argument("--drafts-out", default="", help="Write the drafts to this file (.json or text).")
        parser.add_argument("--send", action="store_true",
                            help="Refused without FOUNDER_REENGAGEMENT_SEND_APPROVED; a dry run even with it.")
        parser.add_argument("--no-dry-run", action="store_true",
                            help="With --send: refused. No sender is built for this one-off email.")

    def handle(self, *args, **options):
        if options["no_dry_run"] and not options["send"]:
            raise CommandError("--no-dry-run only applies to --send.")
        if options["send"] and not reengagement.send_approved():
            raise CommandError("Refusing to send: the owner's go-ahead (FOUNDER_REENGAGEMENT_SEND_APPROVED) is "
                               "not set. Nothing was sent.")
        if options["send"] and options["no_dry_run"]:
            raise CommandError("Refusing to send: this command has no sender. Plan 6.7 sends one personal email "
                               "per founder by hand from the drafts. Nothing was sent.")
        now = _now(options["now"])
        overrides = _read_json(options["overrides"], "overrides")
        ledger = _read_json(options["sent_ledger"], "sent ledger")
        using = options["database"]
        with transaction.atomic(using=using):
            if connections[using].vendor == "postgresql":
                with connections[using].cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
            report = reengagement.build_report(now=now, using=using, organization_ids=options["org"] or None,
                                               overrides=overrides, ledger=ledger,
                                               workshop_days=options["workshop_day"])
            transaction.set_rollback(True, using=using)
        drafts = reengagement.render_drafts(report)
        if options["out"]:
            Path(options["out"]).write_text(reengagement.dumps(report) + "\n")
        if options["drafts_out"]:
            path = Path(options["drafts_out"])
            text = (reengagement.dumps(drafts) + "\n" if path.suffix == ".json"
                    else reengagement.drafts_text(drafts))
            path.write_text(text)
        if options["send"]:
            self._dry_run(drafts)
            return
        if options["json"]:
            self.stdout.write(reengagement.dumps(report))
            return
        self._table(report, drafts)

    def _dry_run(self, drafts):
        self.stdout.write(f"DRY RUN: nothing was sent. {len(drafts['drafts'])} email(s) would be due:")
        for draft in drafts["drafts"]:
            self.stdout.write(f"  org {draft['organizationId']:<6} {draft['stage']:<10} {draft['bucket']:<22} "
                              f"{draft['to']}")

    def _table(self, report, drafts):
        self.stdout.write(f"Never-connected startups as of {report['asOf']}: {report['total']}")
        for bucket in reengagement.BUCKETS:
            self.stdout.write(f"  {bucket:<24} {report['buckets'][bucket]}")
        self.stdout.write("")
        for row in report["organizations"]:
            stage = row["contact"]["stage"]
            note = row["hold"] or (f"with org {row['sendWith']}" if row["sendWith"] else "")
            if row["contactBlock"]:
                note = row["contactBlock"]
            self.stdout.write(f"  org {row['organizationId']:<6} {row['bucket']:<22} {stage:<10} "
                              f"{row['domain'] or '-':<36} {row['link'] or '-'} {note}".rstrip())
        self.stdout.write("")
        self.stdout.write(f"{len(drafts['drafts'])} draft(s) due. Nothing is sent by this command.")
