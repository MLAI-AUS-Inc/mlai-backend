"""Provider quotas commit independently of message/auth transaction rollback."""

from contextlib import contextmanager
from datetime import timedelta

from django.db import connections

from .scheduler import BudgetDeferred

# The extra rows are local scheduling state, not additional Slack allowances.
# Every request still acquires the original app/workspace/method gate first.
PRIORITY_METHODS = frozenset({
    "conversations.history", "conversations.replies", "conversations.info",
    "conversations.members", "users.conversations", "users.info",
})
FOREGROUND_DEMAND_SECONDS = 30


def priority_available(cursor, *, app_id, workspace_id, method, priority, interval_seconds, now):
    """Read/update demand under the original method lock, never in local RAM."""
    local_method = "priority:" + method
    if priority == "foreground":
        cursor.execute("""
            INSERT INTO bridge_api_budget
                (app_id, workspace_id, method, next_admitted_at, cooldown_until, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (app_id, workspace_id, method) DO UPDATE
            SET cooldown_until = GREATEST(bridge_api_budget.cooldown_until, EXCLUDED.cooldown_until),
                updated_at = EXCLUDED.updated_at
        """, [app_id, workspace_id, local_method, now,
              now + timedelta(seconds=max(FOREGROUND_DEMAND_SECONDS, 2 * interval_seconds)), now])
        return None, now
    cursor.execute("""
        SELECT id, next_admitted_at, cooldown_until FROM bridge_api_budget
        WHERE app_id = %s AND workspace_id = %s AND method = %s
    """, [app_id, workspace_id, local_method])
    row = cursor.fetchone()
    if row is None:
        return None, now
    row_id, available, demand_until = row
    return row_id, available if demand_until and demand_until > now else now


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


def admit_request(*, app_id, workspace_id, method, interval_seconds, priority=None):
    """Pace the whole scope and let idle foreground capacity serve bulk work.

    While foreground demand is recent, background calls use at most half the
    configured method capacity. Idle imports retain the original allowance.
    This preference never raises the provider limit or shortens Retry-After.
    """
    if interval_seconds <= 0:
        raise ValueError("Positive provider interval required")
    if priority not in {None, "foreground", "background"}:
        raise ValueError("Unknown provider request priority")
    deferred = None
    with budget_row(app_id, workspace_id, method) as (cursor, row, now):
        row_id, admitted, cooldown = row
        available = max(admitted, cooldown or now)
        background_row_id = None
        if priority is not None and method in PRIORITY_METHODS:
            background_row_id, priority_at = priority_available(
                cursor, app_id=app_id, workspace_id=workspace_id, method=method,
                priority=priority, interval_seconds=interval_seconds, now=now,
            )
            available = max(available, priority_at)
        if available > now:
            deferred = BudgetDeferred((available - now).total_seconds(), before_request_method=method)
        else:
            cursor.execute("""
                UPDATE bridge_api_budget SET next_admitted_at = %s, updated_at = %s WHERE id = %s
            """, [now + timedelta(seconds=interval_seconds), now, row_id])
            if background_row_id is not None:
                cursor.execute("""
                    UPDATE bridge_api_budget SET next_admitted_at = %s, updated_at = %s WHERE id = %s
                """, [now + timedelta(seconds=2 * interval_seconds), now, background_row_id])
    # A deferred interactive caller still expresses demand. Commit that marker
    # before returning; never hold a DB connection while sleeping or doing I/O.
    if deferred is not None:
        raise deferred


def record_cooldown(*, app_id, workspace_id, method, retry_after):
    """All tokens share Retry-After; a later shorter response cannot shorten it."""
    with budget_row(app_id, workspace_id, method) as (cursor, row, now):
        row_id, _, cooldown = row
        until = now + timedelta(seconds=max(1, retry_after))
        cursor.execute("""
            UPDATE bridge_api_budget SET cooldown_until = %s, updated_at = %s WHERE id = %s
        """, [max(until, cooldown or until), now, row_id])
