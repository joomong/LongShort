"""cache_v21/db.py - shared read-only xfeed (PostgreSQL) helpers for the cache_v2.1 build modules.

Rules implemented here (BRIEF section 1):
  * every session is read-only (`default_transaction_read_only=on`, `set_session(readonly=True)`),
    autocommit so no snapshot is held between chunk queries, `statement_timeout` set;
  * big tables are only ever read with `tradingitemid = any(%s)` chunks (`chunked`, <= 1000 ids);
  * `wait_for_quiet` polls `pg_stat_activity` and waits while more than `max_active` other
    queries are running on the xfeed database;
  * credentials are provided by XFEED_DB_PASSWORD or libpq's standard sources
    (PGPASSWORD / .pgpass) and are never logged.

Usage:
    from db import DB, chunked, wait_for_quiet, rss_gb
    db = DB()
    df = db.query("select ... where tradingitemid = any(%s)", (ids,))
    table = db.copy_arrow("select ... where tradingitemid = any(%s)", (ids,), column_types)
"""
from __future__ import annotations

import io
import logging
import os
import resource
import time

for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS",
           "POLARS_MAX_THREADS"):
    os.environ.setdefault(_v, "8")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.csv as pacsv  # noqa: E402

SCHEMA = "xfl_target"
DEFAULT_TIMEOUT_MS = 600_000
MAX_CHUNK_IDS = 1000          # hard cap for big-table chunk queries
DB_PARAMS = dict(host="localhost", database="xfeed", user="loader", port="5432")

log = logging.getLogger("cache_v21.db")


def rss_gb() -> float:
    """Peak RSS of this process in GB (resource.getrusage)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024


def connect(statement_timeout_ms: int = DEFAULT_TIMEOUT_MS, application_name: str = "cache_v21"):
    """Strictly read-only autocommit connection."""
    import psycopg2

    params = dict(DB_PARAMS)
    if os.environ.get("XFEED_DB_PASSWORD"):
        params["password"] = os.environ["XFEED_DB_PASSWORD"]
    conn = psycopg2.connect(
        **params,
        options=("-c default_transaction_read_only=on "
                 f"-c statement_timeout={int(statement_timeout_ms)} "
                 f"-c application_name={application_name}"),
    )
    conn.set_session(readonly=True, autocommit=True)
    return conn


def chunked(values, size: int = MAX_CHUNK_IDS):
    """Yield consecutive slices of `values` (sorted order is preserved) of at most `size` items."""
    values = list(values)
    for i in range(0, len(values), size):
        yield values[i:i + size]


class DB:
    """Lazy read-only connection with retry/reconnect for plain queries and COPY-to-arrow pulls."""

    def __init__(self, statement_timeout_ms: int = DEFAULT_TIMEOUT_MS, application_name: str = "cache_v21",
                 retries: int = 3):
        self.timeout_ms = statement_timeout_ms
        self.app = application_name
        self.retries = retries
        self._conn = None
        self.n_queries = 0
        self.seconds = 0.0

    def conn(self):
        if self._conn is None or self._conn.closed:
            self._conn = connect(self.timeout_ms, self.app)
        return self._conn

    def close(self):
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def pid(self) -> int:
        with self.conn().cursor() as cur:
            cur.execute("select pg_backend_pid()")
            return int(cur.fetchone()[0])

    def _reset(self):
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def query(self, sql: str, params=None) -> pd.DataFrame:
        """SELECT -> DataFrame (column names from the cursor)."""
        last = None
        for attempt in range(self.retries):
            t0 = time.time()
            try:
                with self.conn().cursor() as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
                    cols = [d[0] for d in cur.description]
                self.n_queries += 1
                self.seconds += time.time() - t0
                return pd.DataFrame(rows, columns=cols)
            except Exception as exc:  # never log params (long id lists)
                last = exc
                log.warning("query failed (attempt %d/%d): %s", attempt + 1, self.retries, str(exc)[:200])
                self._reset()
                time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"query failed {self.retries} times: {str(last)[:200]}")

    def copy_arrow(self, sql: str, params, column_types: dict) -> pa.Table:
        """COPY (sql) TO STDOUT CSV -> pyarrow Table with the given column types (fast path for big pulls).

        `params` are bound with cursor.mogrify (so `= any(%s)` id lists work like in `query`)."""
        last = None
        for attempt in range(self.retries):
            t0 = time.time()
            try:
                with self.conn().cursor() as cur:
                    bound = cur.mogrify(sql, params).decode() if params is not None else sql
                    buf = io.BytesIO()
                    cur.copy_expert(f"COPY ({bound}) TO STDOUT WITH CSV HEADER", buf)
                buf.seek(0)
                table = pacsv.read_csv(
                    buf,
                    read_options=pacsv.ReadOptions(use_threads=True, block_size=64 << 20),
                    convert_options=pacsv.ConvertOptions(column_types=column_types),
                )
                self.n_queries += 1
                self.seconds += time.time() - t0
                return table
            except Exception as exc:
                last = exc
                log.warning("COPY failed (attempt %d/%d): %s", attempt + 1, self.retries, str(exc)[:200])
                self._reset()
                time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"COPY failed {self.retries} times: {str(last)[:200]}")

    def scalar(self, sql: str, params=None):
        return self.query(sql, params).iloc[0, 0]


def active_queries(db: DB, min_age_s: float = 1.0) -> pd.DataFrame:
    """Other sessions' active queries on xfeed older than `min_age_s` seconds (own pid excluded)."""
    return db.query(
        """select pid, usename, application_name, state, now() - query_start as age, left(query, 100) as query
           from pg_stat_activity
           where datname = 'xfeed' and state = 'active' and pid <> pg_backend_pid()
             and now() - query_start > make_interval(secs => %s)
           order by query_start""", (float(min_age_s),))


def wait_for_quiet(db: DB, max_active: int = 2, poll_s: float = 20.0, max_wait_s: float = 600.0) -> dict:
    """Block while more than `max_active` other queries are active on xfeed (BRIEF: loader >2 -> wait).

    Returns {"waited_s", "n_active_at_start", "n_active_at_end", "gave_up"}; gives up (and proceeds)
    after `max_wait_s` so that several agents waiting on each other cannot deadlock the build."""
    t0 = time.time()
    n0 = None
    while True:
        act = active_queries(db)
        n = len(act)
        if n0 is None:
            n0 = n
        if n <= max_active:
            return {"waited_s": round(time.time() - t0, 1), "n_active_at_start": n0, "n_active_at_end": n,
                    "gave_up": False}
        if time.time() - t0 >= max_wait_s:
            log.warning("pg_stat_activity: %d other active queries after %.0fs of waiting; proceeding", n, max_wait_s)
            return {"waited_s": round(time.time() - t0, 1), "n_active_at_start": n0, "n_active_at_end": n,
                    "gave_up": True}
        log.info("pg_stat_activity: %d other active queries (> %d), waiting %.0fs ...", n, max_active, poll_s)
        time.sleep(poll_s)


def to_ns(x) -> np.ndarray:
    """datetime64[ns] array from any datetime-like array (arrow timestamp[s]/date32 come back coarser)."""
    return np.asarray(pd.to_datetime(np.asarray(x))).astype("datetime64[ns]")


def sql_int_array(ids) -> str:
    """Inline int[] literal (for places where parameter binding is not available)."""
    return "ARRAY[" + ",".join(str(int(i)) for i in ids) + "]::int[]"
