"""Deadlock-victim retry for short request transactions."""
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.db import OperationalError, connections, transaction
from django.test import TransactionTestCase

from vibe_raising import transaction_retry


def aborted(sqlstate):
    """Mirror Django's wrapping of a psycopg transaction abort."""
    cause = Exception("deadlock detected")
    cause.sqlstate = sqlstate
    error = OperationalError("deadlock detected")
    error.__cause__ = cause
    return error


class RetryableTransactionTests(TransactionTestCase):
    def setUp(self):
        sleep = patch.object(transaction_retry.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def _fail_commits(self, *errors):
        """Abort the first commits the way PostgreSQL reports a deadlock victim."""
        wrapper = connections["default"]
        real_commit = wrapper.commit
        pending = list(errors)

        def commit():
            if pending:
                raise pending.pop(0)
            return real_commit()

        return patch.object(wrapper, "commit", side_effect=commit)

    def test_commit_time_deadlock_reruns_the_rolled_back_transaction(self):
        attempts = []

        def create_draft():
            attempts.append(len(attempts) + 1)
            return Group.objects.create(name=f"draft-{attempts[-1]}")

        with self._fail_commits(aborted("40P01")):
            created = transaction_retry.run_in_retryable_transaction(create_draft)

        self.assertEqual(attempts, [1, 2])
        self.assertEqual(created.name, "draft-2")
        # PostgreSQL rolled the victim back, so only the retried write survives.
        self.assertEqual(list(Group.objects.values_list("name", flat=True)), ["draft-2"])
        self.sleep.assert_called_once()

    def test_serialization_failure_is_retried(self):
        with self._fail_commits(aborted("40001")):
            created = transaction_retry.run_in_retryable_transaction(
                lambda: Group.objects.create(name="draft"),
            )
        self.assertEqual(created.name, "draft")
        self.assertEqual(Group.objects.count(), 1)

    def test_attempts_are_bounded(self):
        calls = []
        errors = [aborted("40P01") for _ in range(transaction_retry.TRANSACTION_RETRY_ATTEMPTS)]
        with self._fail_commits(*errors), self.assertRaises(OperationalError):
            transaction_retry.run_in_retryable_transaction(
                lambda: calls.append(Group.objects.create(name=f"draft-{len(calls)}")),
            )
        self.assertEqual(len(calls), transaction_retry.TRANSACTION_RETRY_ATTEMPTS)
        self.assertFalse(Group.objects.exists())
        self.assertEqual(self.sleep.call_count, transaction_retry.TRANSACTION_RETRY_ATTEMPTS - 1)

    def test_other_database_errors_are_not_retried(self):
        calls = []
        with self._fail_commits(aborted("57014")), self.assertRaises(OperationalError):
            transaction_retry.run_in_retryable_transaction(
                lambda: calls.append(Group.objects.create(name="draft")),
            )
        self.assertEqual(len(calls), 1)
        self.sleep.assert_not_called()

    def test_inside_a_caller_transaction_the_abort_is_not_retried(self):
        calls = []

        def fail():
            calls.append(1)
            raise aborted("40P01")

        with self.assertRaises(OperationalError), transaction.atomic():
            transaction_retry.run_in_retryable_transaction(fail)
        self.assertEqual(calls, [1])
        self.sleep.assert_not_called()
