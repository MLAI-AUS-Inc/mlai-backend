from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from integrations.models import GmailMessageArtifact, GmailRelevanceLabel, StartupProfile
from integrations.services.startup_updates import apply_profile_scoring


LABEL_ORDER = (
    GmailRelevanceLabel.PENDING,
    GmailRelevanceLabel.RELEVANT,
    GmailRelevanceLabel.AMBIGUOUS,
    GmailRelevanceLabel.IRRELEVANT,
)


def _format_counts(counter: Counter) -> str:
    return ", ".join(f"{label}={counter.get(label, 0)}" for label in LABEL_ORDER)


class Command(BaseCommand):
    help = "Dry-run or relabel stored startup-update Gmail messages using current heuristics."

    def add_arguments(self, parser):
        parser.add_argument(
            "--domain",
            required=True,
            help="Organization domain whose stored Gmail messages should be rescored.",
        )
        parser.add_argument(
            "--connection-id",
            type=int,
            default=None,
            help="Optional GoogleConnection id to narrow the relabel pass.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist the relabel results. Without this flag, the command only reports changes.",
        )

    def handle(self, *args, **options):
        domain = str(options["domain"] or "").strip().lower()
        connection_id = options.get("connection_id")
        apply_changes = bool(options.get("apply"))

        try:
            profile = StartupProfile.objects.select_related("organization").get(organization__domain=domain)
        except StartupProfile.DoesNotExist as exc:
            raise CommandError(f"No startup profile found for domain '{domain}'.") from exc

        queryset = GmailMessageArtifact.objects.filter(
            organization=profile.organization,
            classified_at__isnull=True,
        ).order_by("id")
        if connection_id is not None:
            queryset = queryset.filter(google_connection_id=connection_id)

        artifacts = list(queryset)
        if not artifacts:
            self.stdout.write("No unclassified startup-update messages found.")
            return

        before_counts = Counter()
        after_counts = Counter()
        changed_count = 0

        for artifact in artifacts:
            before_state = (
                artifact.relevance_label,
                artifact.heuristic_score,
                tuple(artifact.heuristic_reasons or []),
                artifact.needs_thread_context,
            )
            before_counts[artifact.relevance_label] += 1

            score, reasons, final_label = apply_profile_scoring(
                profile,
                artifact,
                persist=apply_changes,
            )
            after_counts[final_label] += 1
            after_state = (
                final_label,
                score,
                tuple(reasons or []),
                artifact.needs_thread_context,
            )
            if before_state != after_state:
                changed_count += 1

        scope_bits = [f"domain={domain}"]
        if connection_id is not None:
            scope_bits.append(f"connection_id={connection_id}")
        self.stdout.write(
            f"Scanned {len(artifacts)} unclassified startup-update message(s) "
            f"for {' '.join(scope_bits)}."
        )
        self.stdout.write(f"Before: {_format_counts(before_counts)}")
        self.stdout.write(f"After:  {_format_counts(after_counts)}")

        if apply_changes:
            self.stdout.write(self.style.SUCCESS(f"Relabeled {changed_count} message(s)."))
            return

        self.stdout.write(f"Would relabel {changed_count} message(s).")
        self.stdout.write("Re-run with --apply to persist these heuristic updates.")
