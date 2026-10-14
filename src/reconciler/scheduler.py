"""
Reconciler scheduler for self-healing background tasks.

This module provides a scheduler for running reconciliation and cleanup tasks
like vector store sync and soft-delete cleanup. It ensures only one task of each
type runs at a time across multiple deriver instances by using the queue table
for coordination.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import sentry_sdk
from pydantic import BaseModel
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src import models
from src.config import settings
from src.dependencies import tracked_db
from src.models import QueueItem
from src.reconciler.backfill import BACKFILLS, Backfill, record_backfill_pending
from src.reconciler.sync_vectors import (
    has_pending_work,
    record_pending_embeddings_backlog,
)
from src.schemas import ReconcilerType

logger = logging.getLogger(__name__)


class ReconcilerTask(BaseModel):
    """Definition of a reconciler task."""

    name: str
    work_unit_key: str
    interval_seconds: int
    payload: dict[str, Any]
    # Enqueue is skipped when this returns False.
    has_pending: Callable[[AsyncSession], Awaitable[bool]] | None = None


def backfill_task(backfill: Backfill) -> ReconcilerTask:
    """Reconciler task that runs one cycle of ``backfill``."""
    return ReconcilerTask(
        name=f"backfill_{backfill.name}",
        work_unit_key=backfill.work_unit_key,
        interval_seconds=backfill.interval_seconds,
        payload=backfill.payload,
        has_pending=backfill.has_pending,
    )


# Task intervals
QUEUE_CLEANUP_INTERVAL_SECONDS = 12 * 3600  # 12 hours

# Task registry - add new tasks here
RECONCILER_TASKS: dict[str, ReconcilerTask] = {
    "sync_vectors": ReconcilerTask(
        name="sync_vectors",
        work_unit_key="reconciler:sync_vectors",
        interval_seconds=settings.VECTOR_STORE.RECONCILIATION_INTERVAL_SECONDS,
        payload={"reconciler_type": ReconcilerType.SYNC_VECTORS.value},
        has_pending=has_pending_work,
    ),
    "cleanup_queue": ReconcilerTask(
        name="cleanup_queue",
        work_unit_key="reconciler:cleanup_queue",
        interval_seconds=QUEUE_CLEANUP_INTERVAL_SECONDS,
        payload={"reconciler_type": ReconcilerType.CLEANUP_QUEUE.value},
    ),
    **{task.name: task for task in map(backfill_task, BACKFILLS.values())},
}


_reconciler_scheduler: "ReconcilerScheduler | None" = None


def set_reconciler_scheduler(scheduler: "ReconcilerScheduler") -> None:
    """Set the global reconciler scheduler reference."""
    global _reconciler_scheduler
    _reconciler_scheduler = scheduler


def get_reconciler_scheduler() -> "ReconcilerScheduler | None":
    """Get the global reconciler scheduler reference."""
    return _reconciler_scheduler


class ReconcilerScheduler:
    """
    Scheduler for self-healing reconciliation and cleanup tasks.

    Ensures only one task of each type runs at a time across multiple deriver
    instances by using the queue table for coordination. This provides:
    - Vector store synchronization (syncing pending documents/embeddings)
    - Soft-delete cleanup (removing soft-deleted documents from vector stores)
    - Self-healing behavior (retrying failed syncs)
    - Extensible task registry for adding new maintenance tasks

    Each task type has its own interval and work_unit_key, allowing multiple
    different tasks to be queued simultaneously while preventing duplicates
    of the same task type.
    """

    _instance: "ReconcilerScheduler | None" = None
    _initialized: bool = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # Only initialize once
        if not ReconcilerScheduler._initialized:
            self._scheduler_task: asyncio.Task[None] | None = None
            self._shutdown_event: asyncio.Event = asyncio.Event()
            # Track next run time for each task
            self._next_run: dict[str, datetime] = {}
            ReconcilerScheduler._initialized = True

    @classmethod
    def reset_singleton(cls) -> None:
        """Reset the singleton instance. Only use this in tests."""
        cls._instance = None
        cls._initialized = False

    async def start(self) -> None:
        """Start the reconciler scheduler loop."""
        if self._scheduler_task is not None:
            logger.warning("ReconcilerScheduler already running")
            return

        self._shutdown_event.clear()
        # Initialize next run times to first interval
        now = datetime.now(UTC)
        for task_name, task in RECONCILER_TASKS.items():
            self._next_run[task_name] = now + timedelta(seconds=task.interval_seconds)

        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        logger.info(
            "ReconcilerScheduler started with %d tasks: %s",
            len(RECONCILER_TASKS),
            list(RECONCILER_TASKS.keys()),
        )

    async def shutdown(self) -> None:
        """Stop the reconciler scheduler."""
        if self._scheduler_task is None:
            return

        logger.info("Shutting down ReconcilerScheduler...")
        self._shutdown_event.set()

        try:
            await asyncio.wait_for(self._scheduler_task, timeout=5.0)
        except TimeoutError:
            logger.warning("ReconcilerScheduler shutdown timed out, cancelling task")
            self._scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scheduler_task

        self._scheduler_task = None
        self._next_run.clear()
        logger.info("ReconcilerScheduler stopped")

    async def _scheduler_loop(self) -> None:
        """
        Main scheduler loop that enqueues tasks based on their intervals, and
        refreshes the service-wide pending-embeddings and backfill gauges each pass.

        Each task has its own interval and the loop checks all tasks on each
        iteration, enqueueing any that are due. The loop sleeps until the next
        task is due, so the gauge's refresh cadence tracks the SHORTEST task
        interval.
        """
        try:
            while not self._shutdown_event.is_set():
                now = datetime.now(UTC)

                # region ai
                # Refresh on EVERY replica, not just whichever wins the sync_vectors
                # work unit: the count is DB-global, so a replica that never ran a
                # cycle would otherwise export a stale (or zero-initialized) value
                # forever. Full rationale in record_pending_embeddings_backlog.
                # endregion
                await record_pending_embeddings_backlog()
                await record_backfill_pending()

                # Check each task and enqueue if due
                for task_name, task in RECONCILER_TASKS.items():
                    next_run = self._next_run.get(task_name, now)
                    if now >= next_run:
                        try:
                            enqueued = await self._try_enqueue_task(task)
                            if enqueued:
                                logger.debug("Enqueued task: %s", task_name)
                        except Exception as e:
                            logger.exception("Error enqueueing task %s", task_name)
                            if settings.SENTRY.ENABLED:
                                sentry_sdk.capture_exception(e)

                        # Schedule next run regardless of whether we enqueued
                        # (if already pending/in-progress, we'll skip next time too)
                        self._next_run[task_name] = now + timedelta(
                            seconds=task.interval_seconds
                        )

                # Calculate sleep time until next task is due
                if self._next_run:
                    next_task_time = min(self._next_run.values())
                    sleep_seconds = max(
                        1.0,  # At least 1 second to avoid busy loop
                        (next_task_time - datetime.now(UTC)).total_seconds(),
                    )
                else:
                    sleep_seconds = 60.0  # Default if no tasks

                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=sleep_seconds,
                    )
                    break  # Shutdown event was set
                except TimeoutError:
                    # Timeout means interval elapsed, continue loop
                    pass

        except asyncio.CancelledError:
            logger.debug("ReconcilerScheduler loop cancelled")
            raise

    async def _try_enqueue_task(self, task: ReconcilerTask) -> bool:
        """
        Attempt to enqueue a reconciler task.

        This is idempotent - if the task is already in-progress (has an
        ActiveQueueSession) or pending in the queue, the enqueue is skipped.

        Args:
            task: The task definition to enqueue

        Returns:
            True if a task was enqueued, False if skipped
        """
        from src.sentica.workspace_scope import (
            fetch_pending_counts,
            reset_workspace,
            set_workspace,
        )

        async with tracked_db("reconciler_enqueue_plan", read_only=True) as db:
            counts = await fetch_pending_counts(db)
            if counts is None:
                if task.has_pending is not None and not await task.has_pending(db):
                    logger.debug(
                        "Task %s has nothing to do, skipping enqueue", task.name
                    )
                    return False
                names = list(await db.scalars(select(models.Workspace.name)))
            else:
                names = [
                    workspace
                    for workspace, _queue, embeddings in counts
                    if embeddings > 0 or task.has_pending is None
                ]
        if not names:
            return False
        inserted = False
        for name in names:
            async with tracked_db("reconciler_enqueue") as db:
                token = set_workspace(name)
                try:
                    if await self._enqueue_one(
                        db,
                        f"{task.work_unit_key}:{name}",
                        name,
                        task.payload,
                    ):
                        try:
                            await db.commit()
                            inserted = True
                        except IntegrityError:
                            await db.rollback()
                finally:
                    reset_workspace(token)
        if inserted:
            logger.debug("Enqueued reconciler task: %s", task.name)
        return inserted

    async def _enqueue_one(
        self,
        db: AsyncSession,
        key: str,
        workspace_name: str | None,
        payload: dict[str, Any],
    ) -> bool:
        """Insert one reconciler row while the workspace setting is current."""
        in_progress = await db.scalar(
            select(
                exists(
                    select(models.ActiveQueueSession.id).where(
                        models.ActiveQueueSession.work_unit_key == key
                    )
                )
            )
        )
        if in_progress:
            return False
        pending = await db.scalar(
            select(
                exists(
                    select(QueueItem.id).where(
                        QueueItem.work_unit_key == key,
                        QueueItem.processed == False,  # noqa: E712
                    )
                )
            )
        )
        if pending:
            return False
        db.add(
            QueueItem(
                work_unit_key=key,
                payload=payload,
                session_id=None,
                task_type="reconciler",
                workspace_name=workspace_name,
                message_id=None,
            )
        )
        await db.flush()
        return True
