"""Claim worker: a DB-backed job queue drained in a loop."""

import asyncio
import logging
import time
from typing import Awaitable, Callable, Dict, Optional, Set

from alpharidge_ai.schema import Claim
from alpharidge_ai.verify.fetch import Paced
from app.config import EVENT_LANE
from app.services import (
    budget, recheck, intake, judge, ledger, line, novelty, points, registry, resolve, scoops,
    trust, verifier,
)
from app.services.entities import shared_index
from app.services.store import Job, Store

JOB_LEASE_S = 600
FETCH_PER_HOTKEY = 2
# A job taken this many times without finishing is dropped.
JOB_MAX_ATTEMPTS = 3
# Kinds tried again after a failure, this long after, until dropped.
RETRIED = (scoops.CONFIRM_JOB,)
JOB_RETRY_S = 60

logger = logging.getLogger(__name__)

Handler = Callable[[Store, Job], Awaitable[None]]
HANDLERS: Dict[str, Handler] = {}
# Run once when a job of the kind is dropped.
GIVE_UP: Dict[str, Handler] = {intake.FETCH_JOB: verifier.fetch_gave_up,
                               scoops.CONFIRM_JOB: scoops.confirm_gave_up}


def handler(kind: str):
    def register(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn
    return register


handler(intake.FETCH_JOB)(verifier.fetch_job)
handler(intake.REPORT_JOB)(verifier.report_job)
handler(recheck.JOB)(recheck.recheck_job)
handler(scoops.CONFIRM_JOB)(scoops.confirm_job)


async def expire_queued(store: Store, now_ms: int) -> int:
    """Claims that waited a full budget window without a check end as budget_expired."""
    cutoff = now_ms - budget.window_ms()
    reg, expired = registry.get_registry(), 0
    for claim in await store.claims_by_status("queued", received_before_ms=cutoff):
        if reg.holding(claim.received_ms, now_ms):
            continue
        try:
            await intake.transition(store, claim, "rejected", reason="budget_expired")
            expired += 1
        except intake.InvalidTransition:
            pass
    return expired


async def run_job(store: Store, job: Job) -> None:
    try:
        await HANDLERS[job.kind](store, job)
        await store.finish_job(job.id)
    except Paced as e:
        await store.defer_job(job.id, time.time_ns() // 1_000_000 + int(e.wait_s * 1000) + 1)
    except Exception as e:
        logger.warning(f"Job {job.id} ({job.kind}) failed: {e!r}")
        if job.kind in RETRIED:
            await store.defer_job(job.id, time.time_ns() // 1_000_000 + JOB_RETRY_S * 1000,
                                  retry=True)
        else:
            await store.finish_job(job.id, error=repr(e)[:500])


class JobPool:
    """Fetch and re-check jobs in their own tasks, bounded in total and per hotkey."""

    def __init__(self, size: int, per_hotkey: int):
        self.size, self.per_hotkey = size, per_hotkey
        self.tasks: Set[asyncio.Task] = set()
        self.running: Dict[Optional[str], int] = {}

    async def poll(self, store: Store, now_ms: int) -> int:
        free = self.size - len(self.tasks)
        if free <= 0:
            return 0
        busy = [hk for hk, n in self.running.items() if hk and n >= self.per_hotkey]
        jobs = await store.take_jobs(list(HANDLERS), free, JOB_LEASE_S * 1000,
                                     now_ms, busy, per_hotkey=self.per_hotkey)
        for job in jobs:
            if job.attempts > JOB_MAX_ATTEMPTS:
                await self._give_up(store, job)
                continue
            row = await store.get_claim(job.claim_id) if job.claim_id is not None else None
            hotkey = row.hotkey if row is not None else None
            if hotkey is not None and self.running.get(hotkey, 0) >= self.per_hotkey:
                await store.defer_job(job.id, now_ms + int(EVENT_LANE["WORKER_INTERVAL_S"] * 1e3))
                continue
            self.running[hotkey] = self.running.get(hotkey, 0) + 1
            task = asyncio.create_task(run_job(store, job))
            self.tasks.add(task)
            task.add_done_callback(lambda t, hk=hotkey: self._done(t, hk))
        return len(jobs)

    async def _give_up(self, store: Store, job: Job) -> None:
        logger.warning(f"Job {job.id} ({job.kind}) dropped after {job.attempts - 1} attempts")
        try:
            if job.kind in GIVE_UP:
                await GIVE_UP[job.kind](store, job)
        finally:
            await store.finish_job(job.id, error="attempts")

    def _done(self, task: asyncio.Task, hotkey: Optional[str]) -> None:
        self.tasks.discard(task)
        self.running[hotkey] -= 1
        if not self.running[hotkey]:
            del self.running[hotkey]

    async def drain(self) -> None:
        while self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


async def advance_queued(store: Store, now_ms: int, limit: int = 200) -> int:
    """Run novelty on queued claims whose bucket has closed, in line order."""
    v, reg = verifier.get_verifier(), registry.get_registry()
    if v.entities is None:
        v.entities = shared_index()
    await reg.warm(store, v.entities)
    await v.registry.refresh(store)
    await v.registry.filings.warm()
    done, waiting = 0, set()
    for row in await line.ready_claims(store, now_ms, limit):
        try:
            # Registry IDs are resolved first; the claim, and any later claim of its type,
            # waits in the queue until they are.
            claim = Claim.model_validate(row.payload)
            if row.event_type in waiting or not await reg.ensure(
                    store, v.entities, claim.actors, now_ms, row.received_ms):
                waiting.add(row.event_type)
                continue
            # The claimed URL's trust routes the claim; verify freezes it from the fetched page.
            source = await trust.resolve(store, row.source_url, now_ms)
            ct = await trust.of_claim(store, v.registry, v.entities, claim, source, row.category,
                                      row.source_url)
            await novelty.apply(store, row, v.entities, v.places, now_ms, trust=ct.t,
                                filer=ct.filer)
            done += 1
        except intake.InvalidTransition:
            pass
        except Paced:
            # A busy source host holds the claim and the rest of its type until next tick.
            waiting.add(row.event_type)
        except Exception as e:
            logger.warning(f"Novelty for claim {row.id} failed: {e!r}")
    return done


_last_settle_ms = 0


async def tick(store: Store, now_ms: int) -> None:
    global _last_settle_ms
    spent: Dict[str, float] = {}

    async def timed(name: str, step: Awaitable) -> None:
        at = time.perf_counter()
        await step
        spent[name] = time.perf_counter() - at

    await timed("expire", expire_queued(store, now_ms))
    await timed("advance", advance_queued(store, now_ms))
    await timed("verify", verifier.verify_ready(store, now_ms))
    await timed("resolve", resolve.get_resolver().run(store, now_ms))
    if now_ms - _last_settle_ms >= EVENT_LANE["SETTLE_EVERY_S"] * 1000:
        _last_settle_ms = now_ms
        await timed("params", ledger.refresh_params())
        await timed("settle", points.settle_due(store, now_ms))
        await timed("close", judge.close_moved(store))
    slow = EVENT_LANE["TICK_LOG_S"]
    if slow > 0 and sum(spent.values()) >= slow:
        logger.info("[TICK] " + " ".join(f"{k}={v:.2f}s" for k, v in spent.items()))


async def run_pool(store: Store, pool: JobPool) -> None:
    while True:
        try:
            await pool.poll(store, time.time_ns() // 1_000_000)
        except Exception as e:
            logger.error(f"Job pool poll failed: {e!r}")
        await asyncio.sleep(EVENT_LANE["WORKER_INTERVAL_S"])


async def run_worker(store: Store) -> None:
    pool = JobPool(EVENT_LANE["FETCH_CONCURRENCY"], FETCH_PER_HOTKEY)
    jobs = asyncio.create_task(run_pool(store, pool))
    try:
        while True:
            try:
                await tick(store, time.time_ns() // 1_000_000)
            except Exception as e:
                logger.error(f"Claim worker tick failed: {e!r}")
            await asyncio.sleep(EVENT_LANE["WORKER_INTERVAL_S"])
    finally:
        jobs.cancel()
