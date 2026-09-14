"""Provider quotas commit independently of message/auth transaction rollback."""

from contextlib import contextmanager
from datetime import timedelta

from django.db import connections

from .scheduler import BudgetDeferred


@contextmanager
def budget_row(app_id, workspace_id, method):
    if not app_id or not workspace_id or not method:
        raise ValueError("Explicit provider app, workspace and method required")
    # A Slack call often occurs under an authorization transaction. A failed
    # call rolls that transaction back, but MUST NOT refund the API request or
    # erase Retry-After. Use a short, independent connection to the same DB.
    database = connections["default"].copy()
    try:
        if database.vendor != "postgresql":
            raise RuntimeError("Durable provider budgets require PostgreSQL")
        database.ensure_connection()
        database.set_autocommit(False)
        with database.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '3s'")
            cursor.execute("""
                INSERT INTO bridge_api_budget
                    (app_id, workspace_id, method, next_admitted_at, cooldown_until, updated_at)
                VALUES (%s, %s, %s, clock_timestamp(), NULL, clock_timestamp())
                ON CONFLICT (app_id, workspace_id, method) DO NOTHING
            """, [app_id, workspace_id, method])
            cursor.execute("""
                SELECT id, next_admitted_at, cooldown_until FROM bridge_api_budget
                WHERE app_id = %s AND workspace_id = %s AND method = %s FOR UPDATE
            """, [app_id, workspace_id, method])
            row = cursor.fetchone()
            cursor.execute("SELECT clock_timestamp()")
            now = cursor.fetchone()[0]
            yield cursor, row, now
        database.commit()
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def admit_request(*, app_id, workspace_id, method, interval_seconds):
    """Admit now or defer; never reserve slots for a backlog of waiting users."""
    if interval_seconds <= 0:
        raise ValueError("Positive provider interval required")
    with budget_row(app_id, workspace_id, method) as (cursor, row, now):
        row_id, admitted, cooldown = row
        available = max(admitted, cooldown or now)
        if available > now:
            raise BudgetDeferred((available - now).total_seconds())
        cursor.execute("""
            UPDATE bridge_api_budget SET next_admitted_at = %s, updated_at = %s WHERE id = %s
        """, [now + timedelta(seconds=interval_seconds), now, row_id])


def record_cooldown(*, app_id, workspace_id, method, retry_after):
    """All tokens share Retry-After; a later shorter response cannot shorten it."""
    with budget_row(app_id, workspace_id, method) as (cursor, row, now):
        row_id, _, cooldown = row
        until = now + timedelta(seconds=max(1, retry_after))
        cursor.execute("""
            UPDATE bridge_api_budget SET cooldown_until = %s, updated_at = %s WHERE id = %s
        """, [max(until, cooldown or until), now, row_id])
