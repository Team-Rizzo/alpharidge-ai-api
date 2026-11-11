"""Page parsing in worker processes, so extraction runs beside the event loop."""

import logging
import multiprocessing
from concurrent.futures import Executor, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Optional

from alpharidge_ai.verify import fetch

logger = logging.getLogger(__name__)


class ParsePool(Executor):
    """A process pool for the page fetcher's parse step, started fresh if a worker dies."""

    def __init__(self, workers: int):
        self.workers = workers
        self._pool = self._start()

    def _start(self) -> ProcessPoolExecutor:
        # Spawned, not forked: the API process runs threads and an event loop.
        return ProcessPoolExecutor(max_workers=self.workers,
                                   mp_context=multiprocessing.get_context("spawn"))

    def submit(self, fn, /, *args, **kwargs):
        try:
            return self._pool.submit(fn, *args, **kwargs)
        except BrokenProcessPool:
            logger.warning("Page parse workers stopped; starting them again")
            self._pool = self._start()
            return self._pool.submit(fn, *args, **kwargs)

    def shutdown(self, wait=True, *, cancel_futures=False):
        self._pool.shutdown(wait=wait, cancel_futures=cancel_futures)


_installed: Optional[ParsePool] = None


def install(workers: int) -> None:
    """Parse fetched pages in `workers` processes instead of the fetcher's one thread."""
    global _installed
    if workers <= 0 or _installed is not None:
        return
    _installed = ParsePool(workers)
    fetch._PARSE = _installed
    logger.info(f"Parsing pages in {workers} processes")


def current() -> Optional[ParsePool]:
    return _installed


def uninstall() -> None:
    global _installed
    if _installed is not None:
        _installed.shutdown(wait=False, cancel_futures=True)
        _installed = None
