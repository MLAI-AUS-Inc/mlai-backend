"""Independent bounded slots: slow provider I/O never stalls sibling slots."""
import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from django.db import close_old_connections
from .scheduler import heartbeat

logger = logging.getLogger(__name__)


def _database_call(operation, slot):
    close_old_connections()
    try:
        return operation(slot)
    finally:
        close_old_connections()


async def run_slots(operation, *, slots, idle_seconds=0.5, progress=None):
    """Run fixed independent slots on a dedicated executor until cancelled.

    No unbounded submission queue: each slot awaits its current call before
    submitting another. Read-state uses a separate pool from history. Cancelling
    stops new claims; in-flight synchronous calls still obey durable leases.
    """
    if not 1 <= slots <= 8 or idle_seconds < 0.05:
        raise ValueError("Invalid bounded worker configuration")
    executor = ThreadPoolExecutor(max_workers=slots, thread_name_prefix="message-sync")
    loop = asyncio.get_running_loop()

    async def run(slot):
        while True:
            try:
                count = await loop.run_in_executor(executor, partial(_database_call, operation, slot))
                if progress is not None:
                    progress(count or 0)
                await asyncio.sleep(idle_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("message_sync_slot_failed error_code=%s", type(exc).__name__)
                await asyncio.sleep(5)

    try:
        async with asyncio.TaskGroup() as group:
            for slot in range(slots):
                group.create_task(run(slot))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


async def run_lane(operation, *, worker_id, lane, slots, seed=None):
    """Maintain independent health and optional discovery while slots progress."""
    completed = 0
    last_tick = 0.0

    def progress(count):
        nonlocal completed, last_tick
        completed += count
        last_tick = time.monotonic()

    async def maintain():
        nonlocal completed
        while True:
            try:
                if seed is not None:
                    await asyncio.to_thread(_database_call, lambda _: seed(), None)
                # Do not report healthy forever if every slot is stuck.
                if last_tick and time.monotonic() - last_tick < 90:
                    count, completed = completed, 0
                    await asyncio.to_thread(_database_call, lambda _: heartbeat(
                        worker_id, lane, completed=count,
                    ), None)
            except Exception as exc:
                logger.warning("message_sync_maintenance_failed lane=%s error_code=%s", lane, type(exc).__name__)
            await asyncio.sleep(5)

    async with asyncio.TaskGroup() as group:
        group.create_task(run_slots(operation, slots=slots, progress=progress))
        group.create_task(maintain())
