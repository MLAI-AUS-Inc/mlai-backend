import json
from collections import Counter

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Prefetch

from organizations.models import Organization
from org_memory.activation import evaluate_claim_auto_activation
from org_memory.consolidation import (
    ConsolidationInvariantError,
    apply_strong_grounding_auto_activation,
    refresh_current_state,
    restore_strong_grounding_durable_claim,
)
from org_memory.models import (
    MemoryClaim,
    MemoryClaimKind,
    MemoryClaimStatus,
    MemoryConsolidationOperation,
    MemoryConsolidationRun,
    MemoryConsolidationStatus,
    MemoryEvidence,
    MemoryProvider,
)
from org_memory.pilot_deployment import PilotDeploymentError, resolve_pilot_operator


class Command(BaseCommand):
    help = (
        "Preview or activate existing NEW claims that satisfy every invariant in "
        "their reviewed strong-grounding source policy."
    )

    def add_arguments(self, parser):
        parser.add_argument("--organization-domain", required=True)
        parser.add_argument("--provider", required=True)
        parser.add_argument("--operator-email")
        parser.add_argument("--limit", type=int, default=5000)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        organization = Organization.objects.filter(
            domain__iexact=options["organization_domain"]
        ).first()
        if organization is None:
            raise CommandError("Organization does not exist.")
        provider = str(options["provider"] or "").strip()
        if provider not in MemoryProvider.values:
            raise CommandError("Provider is not supported.")
        limit = int(options["limit"])
        if not 1 <= limit <= 10000:
            raise CommandError("--limit must be between 1 and 10000.")

        operator = None
        if options["apply"]:
            operator_email = str(options.get("operator_email") or "").strip()
            if not operator_email:
                raise CommandError("--operator-email is required with --apply.")
            try:
                operator = resolve_pilot_operator(organization, operator_email)
            except PilotDeploymentError as exc:
                raise CommandError(
                    "Operator lacks an active manage_sources capability."
                ) from exc

        runs = list(
            MemoryConsolidationRun.objects.filter(
                organization=organization,
                status=MemoryConsolidationStatus.REVIEW_REQUIRED,
                operation=MemoryConsolidationOperation.NEW,
                matched_claim__isnull=True,
                candidate_claim__status=MemoryClaimStatus.CANDIDATE,
                candidate_claim__extraction_run__source_version__source__provider=provider,
                candidate_claim__extractor_model=settings.ORG_MEMORY_EXTRACTION_MODEL,
                candidate_claim__extractor_version=settings.ORG_MEMORY_EXTRACTOR_VERSION,
                candidate_claim__extractor_schema_version=settings.ORG_MEMORY_EXTRACTION_SCHEMA_VERSION,
                candidate_claim__extractor_prompt_version=settings.ORG_MEMORY_EXTRACTION_PROMPT_VERSION,
            )
            .select_related(
                "candidate_claim__extraction_run__source_version__source__source_scope__policy",
                "candidate_claim__extraction_run__source_version__source__configuration__default_policy",
                "candidate_claim__extraction_run__source_version__acl_snapshot",
                "review_item",
            )
            .prefetch_related(
                Prefetch(
                    "candidate_claim__evidence",
                    queryset=MemoryEvidence.objects.select_related(
                        "source", "source_version", "chunk"
                    ),
                )
            )
            .order_by("completed_at", "pk")[:limit]
        )
        reason_counts = Counter()
        eligible = 0
        activated = 0
        for run in runs:
            decision = evaluate_claim_auto_activation(run.candidate_claim)
            if not decision.eligible:
                reason_counts.update(decision.reason_codes)
                continue
            eligible += 1
            if not options["apply"]:
                continue
            try:
                apply_strong_grounding_auto_activation(
                    run=run,
                    operator=operator,
                    refresh=False,
                )
            except ConsolidationInvariantError:
                reason_counts.update(["activation_invariant_changed"])
                continue
            activated += 1

        durable_stale_claims = list(
            MemoryClaim.objects.filter(
                organization=organization,
                kind=MemoryClaimKind.DECISION,
                status=MemoryClaimStatus.STALE,
                review_required=False,
                stale_after__isnull=False,
                extraction_run__source_version__source__provider=provider,
                state_events__reason="auto_activation_strong_grounding_v1",
            )
            .select_related(
                "extraction_run__source_version__source__source_scope__policy",
                "extraction_run__source_version__source__configuration__default_policy",
                "extraction_run__source_version__acl_snapshot",
            )
            .prefetch_related(
                Prefetch(
                    "evidence",
                    queryset=MemoryEvidence.objects.select_related(
                        "source", "source_version", "chunk"
                    ),
                )
            )
            .distinct()
            .order_by("stale_after", "pk")[:limit]
        )
        durable_stale_reason_counts = Counter()
        durable_stale_eligible = 0
        durable_stale_restored = 0
        for claim in durable_stale_claims:
            decision = evaluate_claim_auto_activation(claim)
            if not decision.eligible:
                durable_stale_reason_counts.update(decision.reason_codes)
                continue
            durable_stale_eligible += 1
            if not options["apply"]:
                continue
            try:
                restore_strong_grounding_durable_claim(
                    claim=claim,
                    operator=operator,
                    refresh=False,
                )
            except ConsolidationInvariantError:
                durable_stale_reason_counts.update(
                    ["durable_restoration_invariant_changed"]
                )
                continue
            durable_stale_restored += 1

        if activated or durable_stale_restored:
            refresh_current_state(organization)

        report = {
            "schema_version": "org-memory-strong-grounding-activation-v1",
            "organization_domain": organization.domain,
            "provider": provider,
            "apply": bool(options["apply"]),
            "candidates": len(runs),
            "eligible": eligible,
            "activated": activated,
            "durable_stale_candidates": len(durable_stale_claims),
            "durable_stale_eligible": durable_stale_eligible,
            "durable_stale_restored": durable_stale_restored,
            "durable_stale_reason_counts": dict(
                sorted(durable_stale_reason_counts.items())
            ),
            "remaining_review_required": MemoryConsolidationRun.objects.filter(
                organization=organization,
                status=MemoryConsolidationStatus.REVIEW_REQUIRED,
            ).count(),
            "reason_counts": dict(sorted(reason_counts.items())),
        }
        self.stdout.write(json.dumps(report, sort_keys=True))
