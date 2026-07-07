"""
Tests for the web ward-game diagnosis contest endpoints
(hospital/sim_contest_views.py).
"""
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .models import SimCaseWinner, SimDiagnosisGuess

RECORD_URL = '/api/v1/hackathons/hospital/sim-guess/record/'
CLAIM_URL = '/api/v1/hackathons/hospital/sim-guess/claim/'

ROO_KEY = 'test-roo-key'
CLIENT_A = 'aaaaaaaa-1111-4111-8111-111111111111'
CLIENT_B = 'bbbbbbbb-2222-4222-8222-222222222222'
CLIENT_C = 'cccccccc-3333-4333-8333-333333333333'


@override_settings(ROO_API_KEY=ROO_KEY)
class SimGuessRecordTests(TestCase):
    def setUp(self):
        cache.clear()  # throttle + any view caches
        self.client = APIClient()

    def _record(self, *, case_id=1, client_id=CLIENT_A, guess_text='adrenal crisis',
                is_correct=True, key=ROO_KEY):
        kwargs = {}
        if key is not None:
            kwargs['HTTP_X_API_KEY'] = key
        return self.client.post(RECORD_URL, {
            'case_id': case_id,
            'client_id': client_id,
            'guess_text': guess_text,
            'is_correct': is_correct,
        }, format='json', **kwargs)

    def test_record_requires_service_key(self):
        resp = self._record(key=None)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(SimDiagnosisGuess.objects.count(), 0)

        resp = self._record(key='wrong-key')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(SimDiagnosisGuess.objects.count(), 0)

    def test_record_correct_guess_creates_pending_claim(self):
        resp = self._record(is_correct=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, {
            'already_guessed': False,
            'is_correct': True,
            'outcome': 'pending_claim',
            'winner_taken': False,
        })
        guess = SimDiagnosisGuess.objects.get()
        self.assertEqual(guess.case_id, 1)
        self.assertEqual(guess.client_id, CLIENT_A)
        self.assertEqual(guess.outcome, 'pending_claim')
        self.assertEqual(guess.email, '')

    def test_record_incorrect_guess_locks_out(self):
        resp = self._record(is_correct=False, guess_text='gastro')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['outcome'], 'incorrect')
        self.assertFalse(resp.data['is_correct'])

    def test_duplicate_guess_returns_stored_verdict(self):
        self._record(is_correct=False, guess_text='gastro')
        # A re-submission claiming to be correct must NOT upgrade the burnt guess.
        resp = self._record(is_correct=True, guess_text='adrenal crisis')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['already_guessed'])
        self.assertFalse(resp.data['is_correct'])
        self.assertEqual(resp.data['outcome'], 'incorrect')
        self.assertEqual(SimDiagnosisGuess.objects.count(), 1)

    def test_winner_taken_flag(self):
        self._record(client_id=CLIENT_A, is_correct=True)
        winner_guess = SimDiagnosisGuess.objects.get(client_id=CLIENT_A)
        SimCaseWinner.objects.create(case_id=1, guess=winner_guess)

        resp = self._record(client_id=CLIENT_B, is_correct=True)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['winner_taken'])
        # Different case → fresh winner slot.
        resp = self._record(case_id=2, client_id=CLIENT_B, is_correct=True)
        self.assertFalse(resp.data['winner_taken'])

    def test_record_validation(self):
        resp = self._record(client_id='short')  # too short / bad format
        self.assertEqual(resp.status_code, 400)
        resp = self._record(guess_text='x' * 301)
        self.assertEqual(resp.status_code, 400)
        resp = self._record(case_id=0)
        self.assertEqual(resp.status_code, 400)


@override_settings(ROO_API_KEY=ROO_KEY)
class SimGuessClaimTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def _seed_guess(self, *, case_id=1, client_id=CLIENT_A, is_correct=True):
        return SimDiagnosisGuess.objects.create(
            case_id=case_id,
            client_id=client_id,
            guess_text='adrenal crisis' if is_correct else 'gastro',
            is_correct=is_correct,
            outcome=(SimDiagnosisGuess.OUTCOME_PENDING_CLAIM if is_correct
                     else SimDiagnosisGuess.OUTCOME_INCORRECT),
        )

    def _claim(self, *, case_id=1, client_id=CLIENT_A, email='doc@example.com'):
        return self.client.post(CLAIM_URL, {
            'case_id': case_id,
            'client_id': client_id,
            'email': email,
        }, format='json')

    def test_first_claim_wins_ticket(self):
        self._seed_guess()
        resp = self._claim(email='Winner@Example.com')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, {'result': 'ticket', 'already_claimed': False})

        guess = SimDiagnosisGuess.objects.get()
        self.assertEqual(guess.outcome, 'ticket')
        self.assertEqual(guess.email, 'winner@example.com')  # normalized lower
        self.assertIsNotNone(guess.claimed_at)
        winner = SimCaseWinner.objects.get()
        self.assertEqual(winner.case_id, 1)
        self.assertEqual(winner.guess, guess)

    def test_second_claimant_gets_discount(self):
        self._seed_guess(client_id=CLIENT_A)
        self._seed_guess(client_id=CLIENT_B)
        self._claim(client_id=CLIENT_A, email='first@example.com')

        resp = self._claim(client_id=CLIENT_B, email='second@example.com')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, {'result': 'discount', 'already_claimed': False})
        self.assertEqual(SimCaseWinner.objects.count(), 1)
        self.assertEqual(
            SimDiagnosisGuess.objects.get(client_id=CLIENT_B).outcome, 'discount')

    def test_claim_is_idempotent(self):
        self._seed_guess()
        first = self._claim()
        self.assertEqual(first.data['result'], 'ticket')

        again = self._claim(email='other@example.com')  # even with a new email
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.data, {'result': 'ticket', 'already_claimed': True})
        # Stored email unchanged, still exactly one winner row.
        self.assertEqual(SimDiagnosisGuess.objects.get().email, 'doc@example.com')
        self.assertEqual(SimCaseWinner.objects.count(), 1)

    def test_claim_without_correct_guess_404s(self):
        # No guess at all
        resp = self._claim()
        self.assertEqual(resp.status_code, 404)
        # Incorrect guess has nothing to claim
        self._seed_guess(is_correct=False)
        resp = self._claim()
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(SimCaseWinner.objects.count(), 0)

    def test_duplicate_email_on_same_case_409s(self):
        self._seed_guess(client_id=CLIENT_A)
        self._seed_guess(client_id=CLIENT_B)
        self._claim(client_id=CLIENT_A, email='same@example.com')

        resp = self._claim(client_id=CLIENT_B, email='Same@Example.com')
        self.assertEqual(resp.status_code, 409)
        # B's guess stays claimable with a different email.
        b = SimDiagnosisGuess.objects.get(client_id=CLIENT_B)
        self.assertEqual(b.outcome, 'pending_claim')
        resp = self._claim(client_id=CLIENT_B, email='different@example.com')
        self.assertEqual(resp.data['result'], 'discount')

    def test_same_email_on_other_case_is_fine(self):
        self._seed_guess(case_id=1, client_id=CLIENT_A)
        self._seed_guess(case_id=2, client_id=CLIENT_B)
        self._claim(case_id=1, client_id=CLIENT_A, email='doc@example.com')
        resp = self._claim(case_id=2, client_id=CLIENT_B, email='doc@example.com')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['result'], 'ticket')  # fresh case, fresh slot

    def test_invalid_email_400s(self):
        self._seed_guess()
        resp = self._claim(email='not-an-email')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(SimDiagnosisGuess.objects.get().outcome, 'pending_claim')

    def test_winner_slot_race_falls_through_to_discount(self):
        """Direct IntegrityError path: winner row exists but MY guess is
        still pending (the 'told free ticket, pipped at the post' race)."""
        self._seed_guess(client_id=CLIENT_A)
        self._seed_guess(client_id=CLIENT_B)
        # A won the slot, e.g. concurrently.
        SimCaseWinner.objects.create(
            case_id=1, guess=SimDiagnosisGuess.objects.get(client_id=CLIENT_A))

        resp = self._claim(client_id=CLIENT_B, email='raced@example.com')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['result'], 'discount')

    def test_full_flow_through_record_endpoint(self):
        """record → claim wired end to end through both endpoints."""
        service = APIClient()
        for client_id, correct in ((CLIENT_A, True), (CLIENT_B, True), (CLIENT_C, False)):
            resp = service.post(RECORD_URL, {
                'case_id': 7,
                'client_id': client_id,
                'guess_text': 'cerebral venous thrombosis' if correct else 'migraine',
                'is_correct': correct,
            }, format='json', HTTP_X_API_KEY=ROO_KEY)
            self.assertEqual(resp.status_code, 200)

        self.assertEqual(self._claim(case_id=7, client_id=CLIENT_A,
                                     email='a@example.com').data['result'], 'ticket')
        self.assertEqual(self._claim(case_id=7, client_id=CLIENT_B,
                                     email='b@example.com').data['result'], 'discount')
        self.assertEqual(self._claim(case_id=7, client_id=CLIENT_C,
                                     email='c@example.com').status_code, 404)
