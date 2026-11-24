from datetime import datetime, timezone
from types import SimpleNamespace

from django.test import SimpleTestCase

from community_chat.volunteer.admin_rank_history import _LevelEvidence, _recognition_history


def at(day):
    return datetime(2026, 11, day, tzinfo=timezone.utc)


class AdminRankHistoryTests(SimpleTestCase):
    def evidence(self, *, historical=0, cutoff=0, reconciled_at=None, entries=()):
        state = SimpleNamespace(
            historical_microroo=historical,
            historical_ledger_cutoff=cutoff,
            reconciled_at=reconciled_at,
        )
        return _LevelEvidence(state, entries, at(10), at(24))

    def recognition(self, *, status="approved", ledger_id=10, decisions=()):
        return SimpleNamespace(
            action_key="rate_card_task",
            reward_microroo=12_000_000,
            level_microroo=12_000_000,
            ledger_id=ledger_id,
            ledger=SimpleNamespace(created_at=at(7)) if ledger_id else None,
            status=status,
            review_history=[
                {"decision": decision, "at": when.isoformat()} for decision, when in decisions
            ],
        )

    def test_reversal_reduces_current_and_keeps_the_prior_earned_level(self):
        evidence = self.evidence()
        record = self.recognition(status="reversed", decisions=[("reverse", at(15))])
        _recognition_history(evidence, record, 0)
        self.assertEqual(evidence.result(), (0, 12_000_000))

    def test_mirror_before_cutoff_does_not_add_to_the_audited_opening(self):
        evidence = self.evidence(historical=12_000_000, cutoff=10, reconciled_at=at(7))
        _recognition_history(evidence, self.recognition(), 10)
        self.assertEqual(evidence.result(), (12_000_000, 12_000_000))

    def test_recent_opening_and_undated_approval_each_withhold_movement(self):
        evidence = self.evidence(historical=12_000_000, cutoff=10, reconciled_at=at(14))
        self.assertEqual(evidence.result(), (12_000_000, None))
        evidence = self.evidence()
        _recognition_history(evidence, self.recognition(ledger_id=None), 0)
        self.assertEqual(evidence.result(), (12_000_000, None))

    def test_unlinked_approval_uses_review_time_and_exclusive_end(self):
        evidence = self.evidence()
        _recognition_history(evidence, self.recognition(ledger_id=None, decisions=[("approve", at(10))]), 0)
        _recognition_history(evidence, self.recognition(ledger_id=None, decisions=[("approve", at(24))]), 0)
        self.assertEqual(evidence.result(), (12_000_000, 0))
