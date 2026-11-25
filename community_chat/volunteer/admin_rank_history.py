"""Bulk read-only rank evidence for the admin member report."""

from collections import defaultdict

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from roo.models import Ledger

from .credits import COUNTS, classify, earned, ledger_amount, one_record_enabled, recognition_level_microroo
from .models import VolunteerMemberState, VolunteerOpeningEntry, VolunteerRecognition
from .services import contribution_total


def _decision_time(record, decision):
    for entry in reversed(record.review_history):
        if not isinstance(entry, dict) or entry.get("decision") != decision:
            continue
        value = entry.get("at")
        if not isinstance(value, str):
            return None
        try:
            occurred = parse_datetime(value)
        except ValueError:
            return None
        return occurred if occurred and timezone.is_aware(occurred) else None
    return None


class _LevelEvidence:
    def __init__(self, state, entries, start_at, end_at):
        self.start_at = start_at
        self.end_at = end_at
        self.current = state.historical_microroo
        self.opening = self.current
        if entries:
            self.current = sum(
                entry.amount_microroo
                for entry in entries
                if not entry.reversed_at or entry.reversed_at >= end_at
            )
            self.opening = sum(
                entry.amount_microroo
                for entry in entries
                if not entry.reversed_at or entry.reversed_at >= start_at
            )
            # An opening entered in the window has no comparable prior snapshot.
            if any(entry.created_at >= start_at for entry in entries):
                self.opening = None
        elif state.historical_ledger_cutoff or state.historical_microroo:
            if not state.reconciled_at or state.reconciled_at >= start_at:
                self.opening = None

    def apply(self, amount, occurred):
        if not amount:
            return
        if occurred is None:
            self.opening = None
        if self.current is not None and (occurred is None or occurred < self.end_at):
            self.current += amount
        if self.opening is not None and occurred < self.start_at:
            self.opening += amount

    def result(self):
        return tuple(max(0, value) if value is not None else None for value in (self.current, self.opening))


def _recognition_history(evidence, record, cutoff):
    amount = recognition_level_microroo(record)
    if not amount or record.status not in {"approved", "reversed"}:
        return
    included_in_opening = record.ledger_id and record.ledger_id <= cutoff
    when_approved = record.ledger.created_at if record.ledger_id else _decision_time(record, "approve")
    if record.status == "approved":
        if not included_in_opening:
            evidence.apply(amount, when_approved)
        return
    when_reversed = _decision_time(record, "reverse")
    if included_in_opening:
        evidence.apply(-amount, when_reversed)
    elif when_approved and when_reversed and when_approved <= when_reversed:
        evidence.apply(amount, when_approved)
        evidence.apply(-amount, when_reversed)
    else:
        # The revoked award contributes zero now. The opening may include it.
        evidence.opening = None


def report_ranks(user_ids, scope, start_at, end_at):
    """Return current and opening contribution microroo by canonical user ID."""
    states = {
        state.user_id: state
        for state in VolunteerMemberState.objects.filter(community=scope, user_id__in=user_ids)
        .select_related("user")
    }
    if not states:
        return {}
    if not one_record_enabled():
        # The older policy has its own admission and caps. Reuse it for current
        # rank. A comparable historical projection is unavailable for it.
        return {
            user_id: (contribution_total(state.user, state), None)
            for user_id, state in states.items()
        }

    openings = defaultdict(list)
    for entry in VolunteerOpeningEntry.objects.filter(community=scope, user_id__in=states):
        openings[entry.user_id].append(entry)
    evidence = {
        user_id: _LevelEvidence(state, openings[user_id], start_at, end_at)
        for user_id, state in states.items()
    }
    known_ledgers = set()
    records = VolunteerRecognition.objects.filter(community=scope, user_id__in=states)
    for record in records.select_related("ledger").iterator():
        if record.ledger_id:
            known_ledgers.add(record.ledger_id)
        _recognition_history(evidence[record.user_id], record, states[record.user_id].historical_ledger_cutoff)

    ledger_rows = earned(Ledger.objects.filter(
        user_id__in=states,
        pk__gt=min(state.historical_ledger_cutoff for state in states.values()),
        created_at__lt=end_at,
    ))
    for ledger in ledger_rows.iterator():
        if ledger.pk in known_ledgers or ledger.pk <= states[ledger.user_id].historical_ledger_cutoff:
            continue
        if classify(ledger)[0] == COUNTS:
            evidence[ledger.user_id].apply(ledger_amount(ledger), ledger.created_at)
    return {user_id: rows.result() for user_id, rows in evidence.items()}
