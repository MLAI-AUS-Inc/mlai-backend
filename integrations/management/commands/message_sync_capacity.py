"""Read-only quota floor for a stated import workload; never a completion ETA."""
import json

from django.core.management.base import BaseCommand, CommandError
from integrations.services.message_sync.slack_client import provider_interval

METHODS = {
    "directory_pages": "users.conversations",
    "info_probes": "conversations.info",
    "history_pages": "conversations.history",
    "reply_pages": "conversations.replies",
    "member_pages": "conversations.members",
    "profile_lookups": "users.info",
}


class Command(BaseCommand):
    help = "Estimate a provider quota lower bound for a stated per-owner workload (no I/O)."
    requires_system_checks = []

    def add_arguments(self, parser):
        for name in METHODS:
            parser.add_argument("--" + name.replace("_", "-"), type=int, default=0)
        parser.add_argument("--owners", type=int, default=1)
        parser.add_argument("--import-share", type=float, default=0.5)
        parser.add_argument("--mirrors-per-owner", type=int, default=0,
                            help="Mirrors sharing this app/workspace budget; estimates recurring head work.")
        parser.add_argument("--repair-interval-minutes", type=float, default=15,
                            help="Assumed completed head-sweep interval, not an enforced freshness promise.")

    def handle(self, *args, **options):
        owners, share = options["owners"], options["import_share"]
        mirrors, repair_minutes = options["mirrors_per_owner"], options["repair_interval_minutes"]
        if owners < 1 or not 0 < share <= 1 or mirrors < 0 or not 0 < repair_minutes <= 1440 or any(options[name] < 0 for name in METHODS):
            raise CommandError("Use positive owners, share in (0,1], and nonnegative request counts.")
        if not any(options[name] for name in METHODS) and not mirrors:
            raise CommandError("Supply the remaining request workload; 30 days alone is not a workload size.")
        rows = {method: {"requests": options[name] * owners,
                         "minimum_minutes": round(options[name] * owners * provider_interval(method) / 60, 3)}
                for name, method in METHODS.items()}
        minimum = max(row["minimum_minutes"] for row in rows.values())
        recurring_rate = owners * mirrors / repair_minutes
        history_allowance = 60 / provider_interval("conversations.history")
        self.stdout.write(json.dumps({
            "kind": "capacity_model_not_measured_import",
            "same_app_workspace_owners": owners,
            "methods": rows,
            "minimum_provider_minutes": minimum,
            "model_minutes_at_import_share": round(minimum / share, 3),
            "import_share": share,
            "recurring_head_model": {
                "owner_conversation_pairs": owners * mirrors,
                "assumed_sweep_interval_minutes": repair_minutes,
                "minimum_history_requests_per_minute": round(recurring_rate, 3),
                "configured_history_requests_per_minute": history_allowance,
                "minimum_budget_used_percent": round(100 * recurring_rate / history_allowance, 1),
                "assessment": "unsustainable_at_assumed_cadence" if recurring_rate >= history_allowance else "requires_measured_headroom",
                "assumes": "one page per mirror per sweep; excludes active heads, threads, reads and imports",
            },
            "target_assessment": "impossible_with_this_budget" if minimum > 120 else "requires_measured_end_to_end_import",
            "excludes": ["unknown discovery probes", "network latency", "database and relay delivery", "dependency ordering", "provider Retry-After", "outages"],
        }, sort_keys=True))
