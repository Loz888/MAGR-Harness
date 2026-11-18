"""Bounded managed-project worker pool for the single Engine controller."""

from __future__ import annotations

import logging
import threading
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor

from mgar_v3.harness.service import HarnessCodingService

logger = logging.getLogger(__name__)


class HarnessScheduler:
    def __init__(self, harness: HarnessCodingService, capacity: int):
        self.harness = harness
        assert harness.store is not None
        self.store = harness.store
        self.capacity = capacity
        self._pool = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="harness-managed")
        self._active: dict[str, tuple[str, Future[None]]] = {}
        self._stopping = threading.Event()

    def tick(self) -> None:
        if self._stopping.is_set():
            return
        for task_id, (_, job) in tuple(self._active.items()):
            if job.done():
                del self._active[task_id]
                if job.exception() is not None:
                    logger.error("Harness worker exited unexpectedly for %s", task_id)
        if len(self._active) >= self.capacity:
            return
        running = Counter(project for project, _ in self._active.values())
        for row in self.store.scheduled():
            task_id, project = row["task_id"], row["workspace_id"]
            if task_id in self._active or running[project] >= row["max_parallel"]:
                continue
            self._active[task_id] = (
                project,
                self._pool.submit(self.harness.run_scheduled, task_id, self._stopping),
            )
            running[project] += 1
            if len(self._active) >= self.capacity:
                return

    def close(self) -> None:
        self._stopping.set()
        for task_id, (_, job) in self._active.items():
            if not job.done() and not job.cancel():
                self.harness.suspend_scheduled(task_id)
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._active.clear()
