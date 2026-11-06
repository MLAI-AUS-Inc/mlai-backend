"""Rate-based health for transactional email. Alerts only; never pauses sending."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

WINDOW = timedelta(minutes=15)
FAILURE_RATE = 0.05
MIN_SENDS = 5
P95_DELIVERY_SECONDS = 120


@dataclass(frozen=True)
class CategoryHealth:
    category: str
    sends: int
    failures: int
    hard_bounces: int
    complaints: int
    p95_delivery_seconds: float | None

    def problems(self) -> list[str]:
        found = []
        if self.sends >= MIN_SENDS and self.failures / self.sends > FAILURE_RATE:
            found.append(f"{self.failures}/{self.sends} {self.category} emails failed in 15 min")
        if self.p95_delivery_seconds is not None and self.p95_delivery_seconds > P95_DELIVERY_SECONDS:
            found.append(f"{self.category} p95 delivery {int(self.p95_delivery_seconds)}s")
        if self.category == "auth" and self.complaints:
            found.append(f"{self.complaints} spam complaint(s) on sign-in email")
        return found


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]


def snapshot(now=None) -> list[CategoryHealth]:
    from .models import SystemEmail

    now = now or timezone.now()
    rows = list(
        SystemEmail.objects.filter(created_at__gte=now - WINDOW)
        .values_list("category", "status", "sent_at", "delivered_at")
    )
    result = []
    for category in sorted({row[0] for row in rows}):
        mine = [row for row in rows if row[0] == category]
        delays = [(row[3] - row[2]).total_seconds() for row in mine if row[2] and row[3]]
        result.append(CategoryHealth(
            category=category,
            sends=len(mine),
            failures=sum(1 for row in mine if row[1] in {"failed", "suppressed"}),
            hard_bounces=sum(1 for row in mine if row[1] == "bounced"),
            complaints=sum(1 for row in mine if row[1] == "complained"),
            p95_delivery_seconds=p95(delays),
        ))
    return result


def alert(problems: list[str]) -> bool:
    channel = str(getattr(settings, "SYSTEM_EMAIL_ALERT_SLACK_CHANNEL_ID", "") or "").strip()
    if not problems or not channel:
        return False
    from integrations.services.slack import SlackService

    sent, _ = SlackService.send_message(channel, ":rotating_light: System email: " + "; ".join(problems))
    return bool(sent)
