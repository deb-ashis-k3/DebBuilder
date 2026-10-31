"""Execution persistence built for sustained traffic (thousands of executions per second per cluster).

Writing every execution as its own INSERT at start and UPDATE at the end (two transactions, each carrying
the whole workflow and every node's items) melts Postgres at production volume: commit storms, WAL, TOAST
churn, index churn and lock contention. Instead:

- **Batched writes.** Executions are buffered and written by one background task per process every
  EXECUTIONS_FLUSH_MS (or as soon as EXECUTIONS_BATCH_SIZE rows are waiting) in a single transaction:
  one multi-row upsert. An execution that finishes before its first flush (most of them) is written once,
  already finished. Rows are written in primary-key order, so concurrent flushes from several replicas
  can't deadlock. Manual (editor) runs and runs that pause for input are flushed immediately.
- **A row the database refuses never blocks the others.** If a batch fails for a reason other than the
  connection (a constraint, a bad value), its rows are retried one by one and the ones that still fail are
  dropped and logged; on connection errors the whole batch is kept and retried with backoff.
- **Backpressure.** If the database falls behind, producers wait once the buffer holds EXECUTIONS_MAX_PENDING
  rows instead of growing memory without bound.
- **Workflow snapshots are stored once per version** (workflow_snapshots, keyed by a content hash); an
  execution only references the hash.
- **Save policy** (n8n's "save successful / failed / manual executions", "save execution data"): per workflow
  in its settings, defaulting to the EXECUTIONS_SAVE_* settings. Unsaved executions still count in the stats.
- **Stats rollup.** Per workflow, day and status counters (execution_stats) are incremented with each flush,
  so dashboards never count(*) a table with millions of rows.
- Retention / clean-up (see prune()) runs on the elected leader.
"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections import Counter, OrderedDict
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from ..config import get_settings
from ..db import Execution, ExecutionStat, SessionLocal, WorkflowSnapshot

log = logging.getLogger("debbuilder.store")

# Columns an upsert may change once a row exists (start-time columns never change).
_MUTABLE = ("status", "output", "node_runs", "waiting_for", "error", "finished_at", "duration_ms")
# What an upsert needs to (re)insert a row that is already stored: its identity and start columns.
_BASE = ("id", "workflow_id", "mode", "trigger_node", "started_at", "snapshot_hash", "workflow_snapshot")
# "summary" still keeps the workflow's final output when it is this small (bytes of JSON)
SUMMARY_OUTPUT_MAX = 4000
# Node record keys that carry item data; "summary" saving drops them but keeps status / timings / counts.
_DATA_KEYS = ("input", "output", "outputs", "sub_runs", "state")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def snapshot_hash(workflow: dict) -> str:
    raw = json.dumps(workflow, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


def _policy(workflow: dict, key: str, default: str) -> str:
    v = (workflow.get("settings") or {}).get(key)
    return str(v) if v not in (None, "", "default") else default


def should_save(workflow: dict, mode: str, status: str) -> bool:
    """n8n's save settings: successful / failed / manual executions (paused and running ones are always kept)."""
    s = get_settings()
    if status in ("waiting", "running"):
        return True
    if mode == "manual":
        return _policy(workflow, "save_manual", "all" if s.executions_save_manual else "none") != "none"
    if status in ("error", "canceled"):
        return _policy(workflow, "save_error", s.executions_save_error) != "none"
    return _policy(workflow, "save_success", s.executions_save_success) != "none"


def summarize(node_runs: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if k not in _DATA_KEYS} for r in node_runs]


def _strip_nul(v):
    """Copy of a row without NUL characters, which Postgres can't store in text or JSONB."""
    if isinstance(v, str):
        return v.replace("\x00", "")
    if isinstance(v, dict):
        return {_strip_nul(k): _strip_nul(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_strip_nul(x) for x in v]
    return v


def _transient(e: BaseException) -> bool:
    """The database is unreachable / restarting (retry everything later), as opposed to refusing a row."""
    if isinstance(e, (OperationalError, InterfaceError, OSError, asyncio.TimeoutError)):
        return True
    return isinstance(e, DBAPIError) and bool(e.connection_invalidated)


# One upsert statement per column set, built once: executemany lets SQLAlchemy reuse the compiled statement
# (a multi-row VALUES literal was compiled anew for every flush, on the event loop).
_UPSERTS: dict[frozenset, object] = {}


def _upsert(cols: frozenset):
    stmt = _UPSERTS.get(cols)
    if stmt is None:
        ins = pg_insert(Execution)
        stmt = _UPSERTS[cols] = ins.on_conflict_do_update(
            index_elements=["id"], set_={c: getattr(ins.excluded, c) for c in _MUTABLE if c in cols})
    return stmt


class ExecutionStore:
    def __init__(self) -> None:
        self._rows: dict[uuid.UUID, dict] = {}          # waiting to be written (full rows)
        self._drop: set[uuid.UUID] = set()               # written earlier, not to be kept after all
        self._snapshots: dict[str, dict] = {}            # new snapshot versions to write
        self._known: OrderedDict[str, None] = OrderedDict()  # snapshot hashes already in the database
        self._stats: Counter = Counter()                 # (workflow_id, day, status) → finished executions
        # rows already in the DB as "running": their start columns, so finishing them joins the bulk upsert
        # (instead of one UPDATE each). Light: the input items are not kept (an upsert never changes them).
        self._flushed: dict[uuid.UUID, dict] = {}
        self._inflight: dict[uuid.UUID, dict] = {}       # rows being written right now
        self._wake = asyncio.Event()
        self._room = asyncio.Event()
        self._room.set()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stopping = False
        self.stats = {"flushes": 0, "rows": 0, "failures": 0, "dropped": 0, "last_flush_ms": 0.0}

    # ---- producers -------------------------------------------------------------------------------------
    async def _wait_for_room(self) -> None:
        while len(self._rows) >= get_settings().executions_max_pending and not self._stopping:
            self._room.clear()
            self._wake.set()
            await self._room.wait()

    async def started(self, row: dict, workflow: dict, immediate: bool, digest: str | None = None,
                      raw: str | None = None) -> None:
        """A new execution. `row` holds the Execution columns; the snapshot is stored by hash.
        `digest` / `raw`: the workflow's snapshot_hash and JSON when the caller has them already."""
        await self._wait_for_room()
        h = digest or snapshot_hash(workflow)
        if h not in self._known and h not in self._snapshots:
            self._snapshots[h] = json.loads(raw) if raw else json.loads(json.dumps(workflow, default=str))
        self._rows[row["id"]] = {**row, "snapshot_hash": h}
        if immediate:
            await self._flush_now()
        elif len(self._rows) >= get_settings().executions_batch_size:
            self._wake.set()

    async def finished(self, exec_id: uuid.UUID, workflow: dict, mode: str, fields: dict, immediate: bool) -> None:
        """Final (or paused) state of an execution: merged into its buffered row, or upserted later."""
        status = fields["status"]
        if status not in ("waiting", "running"):
            day = (fields.get("finished_at") or _now()).date()
            self._stats[(str(workflow.get("id")), day, status)] += 1
        row = self._rows.get(exec_id)
        if not should_save(workflow, mode, status):
            self._rows.pop(exec_id, None)
            if exec_id in self._flushed or exec_id in self._inflight:  # its "running" row is (being) written
                self._drop.add(exec_id)
            self._flushed.pop(exec_id, None)
        else:
            # "summary": keep status, timings and item counts per node, not the items themselves
            # (manual runs keep everything: they are how workflows get debugged).
            if (mode != "manual" and status != "waiting"
                    and _policy(workflow, "save_data", get_settings().executions_save_data) == "summary"):
                out = fields.get("output") or []
                small = len(json.dumps(out, default=str)) <= SUMMARY_OUTPUT_MAX  # e.g. a one-line batch report
                fields = {**fields, "node_runs": summarize(fields.get("node_runs") or []), "output": out if small else []}
                if row is not None:
                    row["input"] = []
            if row is not None:
                row.update(fields)
            elif exec_id in self._flushed:
                self._rows[exec_id] = {**self._flushed.pop(exec_id), **fields}
            elif exec_id in self._inflight:  # its "running" row is being written: upsert the final state after it
                base = self._inflight[exec_id]
                if base.get("_update_only"):  # an update in flight carries no start columns: update again
                    self._rows[exec_id] = {"id": exec_id, "_update_only": True, **fields}
                else:
                    self._rows[exec_id] = {**{c: base[c] for c in _BASE if c in base}, "input": [], **fields}
            else:  # e.g. resumed from the database by another process: plain update
                self._rows[exec_id] = {"id": exec_id, "_update_only": True, **fields}
        if immediate or status == "waiting":  # a paused run must be resumable right away
            await self._flush_now()
        elif len(self._rows) >= get_settings().executions_batch_size:
            self._wake.set()

    def pending(self, exec_id: uuid.UUID) -> Execution | None:
        """An execution not written yet (read-your-writes for the API on this replica)."""
        row = self._rows.get(exec_id) or self._inflight.get(exec_id)  # also while its write is in flight
        if not row or row.get("_update_only"):
            return None
        snap = self._snapshots.get(row.get("snapshot_hash") or "")
        cols = {k: v for k, v in row.items() if not k.startswith("_") and k not in ("snapshot_hash", "workflow_snapshot")}
        return Execution(**cols, workflow_snapshot=snap, snapshot_hash=row.get("snapshot_hash"))

    async def _flush_now(self) -> None:
        """Write right away (editor runs, paused runs); on failure the rows stay queued for the writer."""
        try:
            await self.flush()
        except Exception:  # noqa: BLE001 - already logged; an execution never fails because of its history
            self._wake.set()

    # ---- the writer --------------------------------------------------------------------------------------
    async def flush(self) -> None:
        async with self._lock:
            rows, self._rows = self._rows, {}
            drop, self._drop = self._drop, set()
            snaps, self._snapshots = self._snapshots, {}
            stats, self._stats = self._stats, Counter()
            if not (rows or drop or snaps or stats):
                return
            t0 = time.perf_counter()
            self._inflight = rows
            try:
                await self._write(rows, drop, snaps, stats)
            except Exception as e:  # noqa: BLE001
                if _transient(e):  # the database is away: keep everything and retry with the next flush
                    self.stats["failures"] += 1
                    log.error("writing %d executions failed (will retry): %s", len(rows), e)
                    self._requeue(rows, drop, snaps, stats)
                    raise
                # Something in the batch is refused: write row by row so one bad row can't block the rest
                # (requeues what is left and raises if the database goes away meanwhile).
                log.warning("writing %d executions failed (%s): retrying them one by one", len(rows), e)
                rows = await self._write_each(rows, drop, snaps, stats)
            finally:
                self._inflight = {}
                if len(self._rows) < get_settings().executions_max_pending:
                    self._room.set()
            for k, v in rows.items():
                if v.get("status") in ("running", "waiting") and not v.get("_update_only"):
                    self._flushed[k] = {c: v[c] for c in _BASE if c in v} | {"input": []}
                else:
                    self._flushed.pop(k, None)
            for h in snaps:
                self._known[h] = None
                if len(self._known) > 5000:
                    self._known.popitem(last=False)
            self.stats["flushes"] += 1
            self.stats["rows"] += len(rows)
            self.stats["last_flush_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    def _requeue(self, rows: dict, drop: set, snaps: dict, stats: Counter) -> None:
        for k, v in rows.items():
            newer = self._rows.get(k)
            if newer is None:
                self._rows[k] = v
            else:  # finished while we were failing: its newer state wins, start columns fill in
                merged = {**v, **newer}
                if not v.get("_update_only"):
                    merged.pop("_update_only", None)
                self._rows[k] = merged
        self._drop |= drop
        self._snapshots.update(snaps)
        self._stats.update(stats)

    async def _write_each(self, rows: dict, drop: set, snaps: dict, stats: Counter) -> dict:
        """After a refused batch: snapshots / deletes / stats, then every row in its own transaction. Rows the
        database still refuses are dropped and logged. A connection error requeues whatever isn't written
        yet (and raises). Returns the rows written."""
        try:
            await self._write({}, drop, snaps, Counter())
        except Exception as e:  # noqa: BLE001
            self.stats["failures"] += 1
            log.error("writing %d executions failed (will retry): %s", len(rows), e)
            self._requeue(rows, drop, snaps, stats)  # (a refused snapshot would retry with backoff)
            raise
        for key in sorted(stats):  # per counter: a workflow deleted meanwhile (foreign key) loses only its own
            try:
                await self._write({}, set(), {}, Counter({key: stats[key]}))
            except Exception as e:  # noqa: BLE001
                if _transient(e):
                    self._requeue(rows, set(), {}, Counter({k: n for k, n in stats.items() if k >= key}))
                    raise
                log.warning("execution counter %s dropped: %s", key, e)
        written: dict = {}
        for k in sorted(rows):
            try:
                await self._write({k: rows[k]}, set(), {}, Counter())
                written[k] = rows[k]
            except Exception as e:  # noqa: BLE001
                if _transient(e):
                    self.stats["failures"] += 1
                    log.error("writing executions failed (will retry): %s", e)
                    self._requeue({j: r for j, r in rows.items() if j not in written}, set(), {}, Counter())
                    raise
                try:  # Postgres refuses NUL characters in text / JSONB (models and binary data produce them)
                    clean = _strip_nul(rows[k])
                    await self._write({k: clean}, set(), {}, Counter())
                    written[k] = clean
                    log.warning("execution %s saved after removing NUL characters", k)
                    continue
                except Exception as e2:  # noqa: BLE001
                    e = e2
                self.stats["dropped"] += 1
                log.error("execution %s can't be saved and is dropped: %s (row columns: %s)",
                          k, e, sorted(c for c in rows[k] if c not in ("node_runs", "output", "input")))
        return written

    async def _write(self, rows: dict, drop: set, snaps: dict, stats: Counter) -> None:
        async with SessionLocal() as s, s.begin():
            if snaps:
                await s.execute(pg_insert(WorkflowSnapshot).values(
                    [{"hash": h, "data": snaps[h]} for h in sorted(snaps)]).on_conflict_do_nothing(index_elements=["hash"]))
            # Grouped by column set (paused rows carry no finished_at, …): one executemany per group, rows
            # in primary-key order so concurrent flushes from several replicas can't deadlock.
            groups: dict[frozenset, list] = {}
            updates = []
            for k, r in sorted(rows.items()):
                if r.get("_update_only"):
                    updates.append(r)
                else:
                    vals = {c: v for c, v in r.items() if not c.startswith("_")}
                    groups.setdefault(frozenset(vals), []).append(vals)
            for cols, vals in groups.items():
                await s.execute(_upsert(cols), vals)
            for r in updates:  # finished after their "running" row was written: rare (long runs)
                await s.execute(update(Execution).where(Execution.id == r["id"]).values(
                    {c: r[c] for c in _MUTABLE if c in r}))
            if drop:
                await s.execute(delete(Execution).where(Execution.id.in_(sorted(drop))))
            if stats:
                values = [{"workflow_id": uuid.UUID(w), "day": d, "status": st, "count": n}
                          for (w, d, st), n in sorted(stats.items()) if w and w != "None"]
                if values:
                    stmt = pg_insert(ExecutionStat).values(values)
                    await s.execute(stmt.on_conflict_do_update(
                        index_elements=["workflow_id", "day", "status"],
                        set_={"count": ExecutionStat.count + stmt.excluded.count}))

    async def _loop(self) -> None:
        backoff = 0.0
        while not self._stopping:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=backoff or get_settings().executions_flush_ms / 1000)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.flush()
                backoff = 0.0
            except Exception:  # noqa: BLE001 - logged in flush(); back off while the database recovers
                backoff = min(max(backoff * 2, 0.5), 10.0)

    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        for _ in range(3):  # last writes before the process exits
            try:
                await self.flush()
                break
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.5)
        self._room.set()


store = ExecutionStore()


async def workflow_snapshot(ex: Execution, session) -> dict:
    """The graph an execution ran: stored inline by older versions, by hash since."""
    if ex.workflow_snapshot:
        return ex.workflow_snapshot
    if ex.snapshot_hash:
        snap = await session.get(WorkflowSnapshot, ex.snapshot_hash)
        if snap:
            return snap.data
    return {}


# ---------------------------------------------------------------------------
# retention (leader only)
# ---------------------------------------------------------------------------

async def prune() -> dict:
    """Delete executions past EXECUTIONS_MAX_AGE_DAYS in small batches (no long locks, no huge transactions),
    mark runs orphaned by a crash, and drop snapshots / stats nothing needs any more."""
    s_ = get_settings()
    done = {"executions": 0, "interrupted": 0, "snapshots": 0, "stats": 0}
    if s_.executions_max_age_days > 0:
        cutoff = _now() - timedelta(days=s_.executions_max_age_days)
        while True:
            async with SessionLocal() as s, s.begin():
                res = await s.execute(text(
                    "DELETE FROM executions WHERE id IN (SELECT id FROM executions WHERE started_at < :c "
                    "AND status NOT IN ('running', 'waiting') LIMIT 5000)"), {"c": cutoff})
            done["executions"] += res.rowcount or 0
            if (res.rowcount or 0) < 5000:
                break
            await asyncio.sleep(0.2)  # let normal traffic through between batches
    if s_.executions_stale_after_hours > 0:
        from .runtime import RUNNING
        cutoff = _now() - timedelta(hours=s_.executions_stale_after_hours)
        async with SessionLocal() as s, s.begin():
            res = await s.execute(update(Execution).where(
                Execution.status == "running", Execution.started_at < cutoff,
                Execution.id.not_in([uuid.UUID(i) for i in RUNNING] or [uuid.uuid4()])).values(
                status="error", error="Interrupted: the server running it stopped", finished_at=_now()))
            done["interrupted"] = res.rowcount or 0
    async with SessionLocal() as s, s.begin():
        res = await s.execute(text(
            "DELETE FROM workflow_snapshots ws WHERE ws.created_at < now() - interval '1 day' "
            "AND NOT EXISTS (SELECT 1 FROM executions e WHERE e.snapshot_hash = ws.hash)"))
        done["snapshots"] = res.rowcount or 0
        res = await s.execute(delete(ExecutionStat).where(ExecutionStat.day < (_now() - timedelta(days=400)).date()))
        done["stats"] = res.rowcount or 0
    if any(done.values()):
        log.info("retention: %s", done)
    return done


async def prune_loop() -> None:
    while True:
        try:
            await prune()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("retention run failed")
        await asyncio.sleep(get_settings().executions_prune_every_minutes * 60)


async def stats_for(session, workflow_ids_query) -> dict[str, int]:
    """Totals per status from the rollup, plus the live running / waiting rows (a small partial index)."""
    from sqlalchemy import func
    out: dict[str, int] = {}
    q = select(ExecutionStat.status, func.sum(ExecutionStat.count)).where(
        ExecutionStat.workflow_id.in_(workflow_ids_query)).group_by(ExecutionStat.status)
    for st, n in (await session.execute(q)).all():
        out[st] = int(n or 0)
    q = select(Execution.status, func.count()).where(
        Execution.status.in_(["running", "waiting"]), Execution.workflow_id.in_(workflow_ids_query)).group_by(Execution.status)
    for st, n in (await session.execute(q)).all():
        out[st] = int(n)
    return out
