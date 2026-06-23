from __future__ import annotations

import importlib

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from organizations.models import Organization


class Command(BaseCommand):
    help = (
        "Completely remove a company (Organization) and everything attached to it, "
        "or reset it to a pre-scaffold state with --keep-org. Dry-run by default; "
        "pass --apply to make changes.\n\n"
        "Why a dedicated command instead of Organization.delete():\n"
        "  * A bare Organization.delete() raises ProtectedError -- NotificationChannel "
        "(a cascade child of the org) is referenced by two PROTECT FKs "
        "(ResearchAutomation.notification_channel, NotificationDelivery.channel) and "
        "Django evaluates PROTECT during collection, before anything is deleted.\n"
        "  * SET_NULL rows (ContentFactoryHealingRecord, VibeRaisingCompany) would be "
        "orphaned, not removed.\n"
        "  * Domain-keyed tables carry no organization FK and a cascade never touches "
        "them (ContentFactoryJob, ScheduledDiscoveryDispatch, AISaturation, PAQuestion, "
        "and workflow_runs.ContentFactoryRun).\n"
        "  * External state (Firebase document blobs, content-factory scaffold branches) "
        "lives outside the DB.\n"
        "This command handles all of the above in dependency order.\n\n"
        "Out of scope (by design):\n"
        "  * The founder's user account and their user-scoped GoogleConnection are NOT "
        "deleted -- removing a company must not delete a person. Use the per-user Gmail "
        "disconnect flow (disconnect_gmail_for_user) for that.\n"
        "  * A merged articles/ directory in the GitHub repo is NOT removed (only "
        "unmerged scaffold branches are cancelled). Delete it via a normal PR if needed.\n\n"
        "Select the company with --domain or --org-id."
    )

    # Headline tables removed automatically by Organization.delete() (all have an
    # `organization` FK with on_delete=CASCADE). Shown in the dry-run preview only;
    # the real deletion happens via the cascade in phase C.
    CASCADE_PREVIEW = [
        ("startup_updates.models", "StartupProfile"),
        ("startup_updates.models", "UserStartupBinding"),
        ("startup_updates.models", "StartupManualDocument"),
        ("startup_updates.models", "GmailMessageArtifact"),
        ("startup_updates.models", "SlackMessageArtifact"),
        ("startup_updates.models", "LinearIssueArtifact"),
        ("startup_updates.models", "StartupMetricObservation"),
        ("startup_updates.models", "StartupEvent"),
        ("startup_updates.models", "MonthlyUpdateDraft"),
        ("content_factory.models", "OrganizationContentConfig"),
        ("content_factory.models", "WrittenArticle"),
        ("content_factory.models", "ResearchedKeyword"),
        ("content_factory.models", "TopicMap"),
        ("content_factory.models", "GeneratedComponent"),
        ("integrations.models", "ExternalServiceConnection"),
        ("integrations.models", "FinancialAccount"),
    ]

    def add_arguments(self, parser):
        parser.add_argument("--domain", default="", help="Company domain (Organization.domain).")
        parser.add_argument(
            "--org-id",
            type=int,
            default=None,
            help="Company Organization id (alternative to --domain).",
        )
        parser.add_argument(
            "--keep-org",
            action="store_true",
            help=(
                "Reset-in-place instead of deleting: scrub startup data and reset the "
                "article-setup state (cancel scaffold branches, clear scaffold flags) so "
                "the company can be re-scaffolded from scratch, but KEEP the Organization "
                "and its content config. Does not purge generated articles/automations -- "
                "use full teardown for a total wipe."
            ),
        )
        parser.add_argument(
            "--no-storage",
            action="store_true",
            help="Skip deleting StartupManualDocument blobs from Firebase Storage.",
        )
        parser.add_argument(
            "--reason",
            default="company_teardown",
            help="Audit reason recorded on the startup data-deletion request.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually make changes. Without it the command only reports (dry-run).",
        )

    # ------------------------------------------------------------------ #
    # resolution / inventory
    # ------------------------------------------------------------------ #

    def _resolve_org(self, domain: str, org_id):
        if bool(domain) == bool(org_id):
            raise CommandError("Pass exactly one of --domain or --org-id.")
        if org_id:
            org = Organization.objects.filter(id=org_id).first()
            selector = f"id={org_id}"
        else:
            org = Organization.objects.filter(domain__iexact=domain).first()
            selector = f"domain={domain!r}"
        if org is None:
            raise CommandError(f"No Organization matched {selector}.")
        return org

    def _get_config(self, org):
        from content_factory.models import OrganizationContentConfig

        return OrganizationContentConfig.objects.filter(organization=org).first()

    def _storage_paths(self, org):
        from startup_updates.models import StartupManualDocument

        return list(
            StartupManualDocument.objects.filter(organization=org)
            .exclude(storage_path="")
            .values_list("storage_path", flat=True)
        )

    def _structural_querysets(self, org):
        """Rows that must be deleted explicitly, in dependency order, BEFORE
        org.delete(). Phase A clears the PROTECT subgraph; phase B clears the
        SET_NULL rows while the organization link still exists."""
        from content_factory.models import (
            ContentFactoryHealingRecord,
            NotificationChannel,
            ResearchAutomation,
        )
        from founder_tools.models import VibeRaisingCompany

        return [
            # A. PROTECT subgraph -- deleting the automations cascades AutomationRun
            #    and NotificationDelivery, which clears BOTH protectors of
            #    NotificationChannel; the channel can then be deleted.
            ("A.protect", "ResearchAutomation (+AutomationRun, NotificationDelivery)", ResearchAutomation.objects.filter(organization=org)),
            ("A.protect", "NotificationChannel", NotificationChannel.objects.filter(organization=org)),
            # B. SET_NULL rows -- org.delete() would null the FK and orphan these,
            #    so remove them while organization= still matches.
            ("B.orphans", "ContentFactoryHealingRecord", ContentFactoryHealingRecord.objects.filter(organization=org)),
            ("B.orphans", "VibeRaisingCompany", VibeRaisingCompany.objects.filter(organization=org)),
        ]

    def _residue_querysets(self, domain: str):
        """Domain-keyed tables with no organization FK -- a cascade never reaches
        them. Safe to delete by domain because Organization.domain is unique.

        Returns [] for a blank domain: an empty filter would match unrelated rows
        from other companies that also have no domain set."""
        if not domain:
            return []
        from content_factory.models import (
            AISaturation,
            ContentFactoryJob,
            PAQuestion,
            ScheduledDiscoveryDispatch,
        )
        from workflow_runs.models import ContentFactoryRun

        return [
            ("D.residue", "ContentFactoryJob", ContentFactoryJob.objects.filter(domain__iexact=domain)),
            ("D.residue", "ScheduledDiscoveryDispatch", ScheduledDiscoveryDispatch.objects.filter(domain__iexact=domain)),
            ("D.residue", "AISaturation", AISaturation.objects.filter(domain__iexact=domain)),
            ("D.residue", "PAQuestion", PAQuestion.objects.filter(domain__iexact=domain)),
            ("D.residue", "ContentFactoryRun (+steps)", ContentFactoryRun.objects.filter(domain__iexact=domain)),
        ]

    def _cascade_preview(self, org):
        rows = []
        for module_path, cls_name in self.CASCADE_PREVIEW:
            try:
                model = getattr(importlib.import_module(module_path), cls_name)
                rows.append((cls_name, model.objects.filter(organization=org).count()))
            except Exception:
                continue
        return rows

    # ------------------------------------------------------------------ #
    # external + reused cleanup
    # ------------------------------------------------------------------ #

    def _external_cleanup(self, config, storage_paths, skip_storage):
        # Article-setup scaffold branches (best-effort; helper never raises).
        if config is not None:
            try:
                from content_factory.vibe_marketing_views import (
                    _delete_article_setup_scaffold_branches,
                )

                res = _delete_article_setup_scaffold_branches(config)
                self.stdout.write(f"  scaffold branches: {res.get('status')}")
            except Exception as exc:  # pragma: no cover - defensive
                self.stdout.write(self.style.WARNING(f"  scaffold branch cleanup failed (ignored): {exc}"))

        # Firebase document blobs.
        if skip_storage or not storage_paths:
            return
        try:
            from core.firebase_utils import delete_storage_object
        except Exception as exc:  # pragma: no cover - firebase optional locally
            self.stdout.write(self.style.WARNING(f"  Firebase unavailable, skipping {len(storage_paths)} blob(s): {exc}"))
            return
        ok = 0
        for path in storage_paths:
            try:
                if delete_storage_object(path):
                    ok += 1
            except Exception as exc:
                self.stdout.write(self.style.WARNING(f"  blob delete failed {path!r} (ignored): {exc}"))
        self.stdout.write(f"  Firebase blobs deleted: {ok}/{len(storage_paths)}")

    def _scrub_startup_data(self, org, reason: str):
        """Reuse the tested data-deletion entry point: scrubs Gmail/Slack/Linear/GA
        artifacts and derived outputs, cancels open startup-update runs, and records
        an audit request. (In full teardown that audit row is later removed with the
        org; in --keep-org it survives.)"""
        from startup_updates.data_deletion import delete_startup_data_for_organization

        result = delete_startup_data_for_organization(
            org,
            reason=reason,
            request_id=f"teardown-{org.id}",
        )
        deleted = result.get("deleted") or {}
        total = sum(v for v in deleted.values() if isinstance(v, int))
        self.stdout.write(f"  startup data scrub: {total} row(s); runs cancelled: {len(result.get('cancelledRuns') or [])}")

    @staticmethod
    def _merge(totals, details):
        for key, value in (details or {}).items():
            totals[key] = totals.get(key, 0) + value

    # ------------------------------------------------------------------ #
    # handle
    # ------------------------------------------------------------------ #

    def handle(self, *args, **options):
        apply = bool(options["apply"])
        keep_org = bool(options["keep_org"])
        skip_storage = bool(options["no_storage"])
        reason = str(options["reason"] or "company_teardown")

        org = self._resolve_org(str(options["domain"] or "").strip(), options["org_id"])
        domain = str(org.domain or "").strip()
        config = self._get_config(org)
        storage_paths = self._storage_paths(org)

        mode = "RESET (keep org)" if keep_org else "FULL TEARDOWN (delete org)"
        self.stdout.write(f"Company: org_id={org.id} name={org.name!r} domain={domain!r}")
        self.stdout.write(f"Mode:    {mode}")
        self.stdout.write("")

        # ---------------- preview (always) ----------------
        self.stdout.write("Plan:")
        self.stdout.write("  External (best-effort, non-transactional):")
        self.stdout.write(f"    - cancel article-setup scaffold branches: {'yes' if config else 'n/a (no content config)'}")
        if skip_storage:
            self.stdout.write("    - delete Firebase document blobs: skipped (--no-storage)")
        else:
            self.stdout.write(f"    - delete Firebase document blobs: {len(storage_paths)}")
        self.stdout.write("  Startup data scrub (delete_startup_data_for_organization): artifacts + cancel runs")

        if keep_org:
            self.stdout.write("  Article-setup reset (deep): clear scaffold flags/state + scan/reuse/design caches, delete article_system_setup runs, drop design snapshots (Organization kept)")
        else:
            self.stdout.write("  Direct deletes (dependency-ordered):")
            for phase, label, qs in self._structural_querysets(org) + self._residue_querysets(domain):
                self.stdout.write(f"    - [{phase}] {label}: {qs.count()} row(s)")
            if not domain:
                self.stdout.write(self.style.WARNING(
                    "    - domain is blank: domain-keyed residue (ContentFactoryJob/Run, "
                    "etc.) will NOT be swept -- clean it up manually if any exists."
                ))
            self.stdout.write("  Cascaded by Organization.delete() (headline tables):")
            for label, count in self._cascade_preview(org):
                self.stdout.write(f"    - {label}: {count} row(s)")

        if not apply:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("DRY RUN -- nothing changed. Re-run with --apply to execute."))
            return

        # ---------------- apply ----------------
        self.stdout.write("")
        self.stdout.write("Applying:")

        # 1. external best-effort cleanup (before DB rows disappear)
        self._external_cleanup(config, storage_paths, skip_storage)

        # 2. startup-data scrub + run cancellation (own transaction; idempotent on re-run)
        self._scrub_startup_data(org, reason)

        # 3a. keep-org: reset article-setup state in place and stop
        if keep_org:
            if config is not None:
                from content_factory.article_setup_reset import reset_article_setup_config

                payload = reset_article_setup_config(
                    config, github_repo=str(getattr(config, "github_repo", "") or ""), deep=True
                )
                self.stdout.write(f"  article setup reset: cleared {payload.get('cleared_fields') or []}")
                self.stdout.write(
                    "  deep reset: deleted "
                    f"{payload.get('deleted_setup_runs', 0)} article_system_setup run(s), dropped "
                    f"{payload.get('dropped_design_snapshots', 0)} design snapshot(s)"
                )
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS(f"Reset complete for org_id={org.id} (organization kept)."))
            self.stdout.write("Re-scaffold via POST /api/v1/integrations/github/scaffold for this domain.")
            return

        # 3b. full teardown: structural deletes in dependency order, all-or-nothing
        totals: dict[str, int] = {}
        with transaction.atomic():
            for phase, label, qs in self._structural_querysets(org):
                deleted, details = qs.delete()
                self._merge(totals, details)
                self.stdout.write(f"  [{phase}] {label}: {deleted} row(s)")

            deleted, details = org.delete()  # phase C -- cascades remaining children
            self._merge(totals, details)
            self.stdout.write(f"  [C.org] Organization.delete(): {deleted} row(s)")

            for phase, label, qs in self._residue_querysets(domain):
                deleted, details = qs.delete()
                self._merge(totals, details)
                self.stdout.write(f"  [{phase}] {label}: {deleted} row(s)")

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Teardown complete: removed {sum(totals.values())} row(s) across {len(totals)} table(s)."
            )
        )
        self.stdout.write(
            "Restart from scratch: recreate the Organization + content config, then trigger "
            "article scaffolding via POST /api/v1/integrations/github/scaffold (GithubScaffoldView)."
        )
