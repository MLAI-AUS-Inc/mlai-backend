"""Backfill verified ACNs onto already-registered vibe-raising companies.

Existing companies were marked ``registered=True`` before the ACN gate existed, so they
carry no ``acn``/``abr_verified_at``. This command re-verifies each one against the ABR
and, on success, persists the resolved ACN. Companies that fail verification (sole
traders, trusts, cancelled ABNs) are reported for manual review and left untouched —
the command never flips ``registered`` off, so it can't silently lock anyone out; the
update guard re-checks at point of use.

Read-only by default. Pass ``--commit`` to persist.
"""

import time

from django.core.management.base import BaseCommand
from django.db.models import Q

from founder_tools.models import VibeRaisingCompany
from vibe_raising.registration import (
    CompanyRegistrationError,
    verify_and_persist_company_registration,
)


class Command(BaseCommand):
    help = "Verify and backfill ACNs for registered companies that predate the ACN gate."

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Persist verified ACNs. Without this flag the command only reports.",
        )
        parser.add_argument(
            "--sleep",
            type=float,
            default=0.5,
            help="Seconds to pause between ABR lookups (default 0.5) to be gentle on the register.",
        )

    def handle(self, *args, **options):
        commit = options["commit"]
        sleep_seconds = max(0.0, options["sleep"])

        companies = VibeRaisingCompany.objects.filter(registered=True).filter(
            Q(acn__isnull=True) | Q(acn="")
        ).select_related("profile").order_by("created_at")

        total = companies.count()
        self.stdout.write(f"Backfilling ACNs for {total} registered company(ies) (commit={commit})")

        verified = 0
        failures = {}  # error code -> count
        flagged = []   # (label, code) for the review report

        for index, company in enumerate(companies):
            label = self._label(company)
            try:
                verify_and_persist_company_registration(
                    company, abn=company.abn, save=commit
                )
            except CompanyRegistrationError as exc:
                failures[exc.code] = failures.get(exc.code, 0) + 1
                flagged.append((label, exc.code))
                self.stdout.write(f"  FLAG  {label}: {exc.code}")
            else:
                verified += 1
                self.stdout.write(f"  OK    {label}: ACN {company.acn}")

            # Pause between live ABR calls (skip the wait after the last one).
            if sleep_seconds and index < total - 1:
                time.sleep(sleep_seconds)

        self._report(commit=commit, total=total, verified=verified, failures=failures, flagged=flagged)

    def _label(self, company) -> str:
        domain = company.domain or "<no-domain>"
        return f"{company.name} ({domain})"

    def _report(self, *, commit, total, verified, failures, flagged):
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Verified: {verified}/{total}"))
        if failures:
            breakdown = ", ".join(f"{code}={count}" for code, count in sorted(failures.items()))
            self.stdout.write(self.style.WARNING(f"Needs review ({len(flagged)}): {breakdown}"))
            for label, code in flagged:
                self.stdout.write(f"  - {label}: {code}")
        if not commit:
            self.stdout.write("")
            self.stdout.write("Dry run — re-run with --commit to persist verified ACNs.")
