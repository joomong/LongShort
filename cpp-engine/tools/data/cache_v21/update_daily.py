#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v21/update_daily.py - T9: daily update of a cache_v2.1 build (run every day after T-1 is complete).

Input  = the previous build (a stage build `<root>/{T1,T2,T2_work,...}` such as `stage_v21/` or an earlier
         `update_daily` output, or a published build written by publish.py: base_data.parquet at the root).
Output = a NEW build directory (`--out-stage`, stage layout) with a new build_id; the previous build is never
         modified.  On any gate failure the new directory stays for inspection, publish is not called and the
         previous build remains the current one (exit code != 0).

Principles (design doc cache_v2_design_2026-09-16.md section 3, BRIEF section 5):
  raw rows are appended / replaced only inside the re-fetched window (T-5..T-1) or for ids whose vendor
  adjustment factors changed (full re-fetch, raw must be invariant); every changed value is logged under
  archive/revisions/date=<observedAt>/; factor tables are re-received in full and re-derived; derived files
  (returns_total, lifecycle, universe, events) are recomputed in full; nothing under /home/base or /home/jgo/61
  is written; the DB is read-only and big tables are read only with `tradingitemid = any(%s)` chunks (<= 1000).

Steps (--steps, default all):
  U1 master   new tradingItemIds under the symbol rule with >= 1 close since 2000 (full history pulled once),
              identity/current changes -> classification_history / primary_history append (observed rows),
              id_mapping + identifier_history refreshed, master snapshot archive/master_snapshots/date=.../
  U2 raw      ciqpriceequity T-5..T-1 for all ids -> raw transform -> compare with the stored rows: changed values
              -> price_revisions (source=2), new rows appended, vendor-removed rows dropped (logged); miprice
              cross-check -> qualityFlag 0/2, T-1 rows 1 (provisional) unless ciq & mi closes agree; close-count
              threshold (asOf closes >= 90% of the previous trading day) else abort
  U3 factors  ciqsplitcache + ciqpriceequitydivadjfactor re-received in full; ids whose adjustment factors may
              have been restated (existing `ciqpriceequity_adjf` ids, changed splitcache windows, window rows
              whose adjustmentfactor differs from the stored side file or from the splitcache lookup) get their
              full ciqpriceequity history re-fetched (raw must be invariant, else abort) and the side file
              (adjustmentfactor per row, `T2_work/adjf_long.parquet`) refreshed; interval tables rebuilt with
              prices_factors (reconcile against the side file), diff -> factor_revisions, monthly snapshot
  U4 lifecycle ciq/mi last-close aggregates updated from the fetched rows (full re-aggregation with
              --full-lifecycle), master_lifecycle / master_lineage / exclusions / master_qa re-run
  U5 derived  returns_total in full (prices_returns v2, cash-style from the corporate-action ledger events/ca_ledger.parquet:
              --ledger, else the new build's T6/events, else the previous build's ledger carried forward; a ledger whose
              asOf (sidecar ca_ledger_build_info.json `asOf`; carried ledger = previous build asOf; missing key = unknown)
              is older than the build asOf OR unknown ABORTS unless --allow-stale-ledger, which flags the rows after the
              ledger asOf (unknown: after the previous build asOf) retFlag 4
              and records ledger_stale in BUILD.json/manifest; first-row links from --lineage or master/lineage.parquet;
              --returns-legacy = rc1 vendor-factor computation); marketcap + universe via the T5 CLI and events via the T6
              CLI when those modules exist (else previous files are carried forward with a warning)
  U6 gates    factor interval self-check, G-SPLIT (all rows), G-JUMP (boundaries of changed ids / window),
              G-DIV (new dividend steps), G-RAW (window), publish gate (every base_data id has a lifecycle row)
  U7 publish  publish.py --stage <out-stage> --out <publish dir> [--remote ...]  (remote off by default)

rc2f contract in the daily chain (D2, update_daily_rc2f.py; docs cache_v21_build_2026-09-16.md section 10/11):
  overrides/rc2f/** (price/split/lineage/lifecycle/aux; ledger overrides are the ledger builder's) are re-applied to the fresh
  vendor tables every day (U2/U3 price+split, U4 lineage, U5 lifecycle/events/dates after T6); the apply log of the day is compared
  with the previous build's log (G-OVR: a regression = blocking). aux instruments (master/aux_instruments) are part of the price
  and factor fetch (their rows stay in base_data), lifecycle_aux / lifecycle_dates / lineage.linkVerified are kept. U-L runs the
  ledger builder incrementally (ledger_build.py --incremental, D1) before returns; returns v2 pass returns_v2_qa on the stage and
  every changed cell vs the previous build is attributed (G-RET-ATTR, blocking); qa/exposure_daily.md crosses the new asOf
  rows / new settlements without terms / new display rows with the live book (G-EXPO: warning, blocking with --strict-exposure).
  publish (U7) runs on a view of the stage with --rc2-stage built from the daily build (engine exports regenerated; anchors re-checked).

CLI
  update_daily.py --prev-build <dir> --out-stage <dir> [--asof YYYY-MM-DD|auto] [--steps U1,U2,...]
                  [--dry-run] [--full-lifecycle] [--publish-out DIR] [--remote DIR] [--refresh-days 5]
  update_daily.py --selftest
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from db import DB, SCHEMA, chunked, rss_gb, wait_for_quiet  # noqa: E402  (sets the thread caps first)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.compute as pc  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

import prices_build as pb  # noqa: E402
import prices_factors as pfm  # noqa: E402
import prices_returns as prm  # noqa: E402
import prices_qa as pqa  # noqa: E402
import master_build as mb  # noqa: E402
import master_history as mh  # noqa: E402
import master_lifecycle as ml  # noqa: E402
import master_lineage as mg  # noqa: E402
import master_qa as mq  # noqa: E402
import observe_finalization as obsf  # noqa: E402  (NYSE calendar helpers; imports after db so thread caps stay 8)
import update_daily_rc2f as dr  # noqa: E402  (D2: rc2f rules of the daily chain)

log = logging.getLogger("cache_v21.update")
KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")

ALL_STEPS = ["U1", "U2", "U3", "U4", "U5", "U6", "U7"]
STEP_NAMES = {"U1": "master", "U2": "raw", "U3": "factors", "U4": "lifecycle", "U5": "derived", "U6": "gates", "U7": "publish"}
PRICE_FIELDS = ["open", "high", "low", "close", "vwap", "volume"]
Q_CONFIRMED, Q_PROVISIONAL, Q_MI_MISMATCH, Q_NULL_CLOSE, Q_POST_LIFECYCLE = 0, 1, 2, 3, 4
SOURCE_CIQ, SOURCE_CORRECTED = 1, 2
REL_TOL_REVISION = 1e-6          # |new-old| > tol x max(|old|,|new|) counts as a value revision
MI_FLAG_TOL = 1e-3               # T2 rule: |ciq/mi - 1| > 1e-3 -> qualityFlag 2
CLOSE_COUNT_MIN_RATIO = 0.90     # asOf closes >= 90% of the previous trading day's closes
REMOVED_ROWS_MAX_FRAC = 0.005    # fuse: > 0.5% of the stored window rows missing from the re-fetch -> abort [estimate]
CURRENT_FIELDS = ["symbol", "exchangeId", "primaryFlag", "primaryFlagTi", "currentPrimary", "sector", "simpleIndustryDescription",
                  "tradingItemStatusId", "securityStartDate", "securityEndDate", "gvkey", "iid"]
INSTRUMENT_REFRESH = ["securityId", "companyId", "companyName", "securitySubTypeId", "tiCurrencyId"]
CLASS_COLS = ["sector", "simpleIndustryDescription"]
PRIM_COLS = ["primaryFlag", "primaryFlagTi", "currentPrimary"]

REV_PRICE_SCHEMA = pa.schema([("tradingItemId", pa.int64()), ("date", pa.timestamp("ns")), ("field", pa.string()),
                              ("old", pa.float64()), ("new", pa.float64()), ("observedAt", pa.timestamp("ns")), ("reason", pa.string())])
REV_FACTOR_COLS = ["table", "tradingItemId", "validFrom", "validTo_old", "validTo_new", "factor_old", "factor_new",
                   "source_old", "source_new", "kind", "observedAt"]
REV_MASTER_COLS = ["table", "tradingItemId", "field", "old", "new", "observedAt"]

# T5 / T6 CLI hooks (other agents' modules; called as subprocesses when the script exists, otherwise stub step)
T5_MARKETCAP = ["universe_marketcap.py"]
T5_UNIVERSE = ["universe_build.py", "universe_membership.py", "universe_universe.py"]
T6_EVENTS = ["events_build.py"]


# ======================================================================================
# small helpers
# ======================================================================================
def now_str() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(32 << 20), b""):
            h.update(b)
    return h.hexdigest()


def write_parquet_atomic(df: pd.DataFrame, path: Path, **kw):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=False, compression="zstd", **kw)
    os.replace(tmp, path)


def write_table_atomic(table: pa.Table, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, path)


def copy_file(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def copy_tree(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return True


def row_keys(ids, dates) -> np.ndarray:
    """int64 key = id << 20 | (days since 1970 + 30000): sortable (id, date) key valid for 1900+ (prices_qa.key)."""
    return pqa.key(np.asarray(ids, dtype=np.int64), np.asarray(dates).astype("datetime64[ns]"))


def to_ns(x) -> np.ndarray:
    return np.asarray(pd.to_datetime(np.asarray(x))).astype("datetime64[ns]")


def ts(x) -> pd.Timestamp:
    return pd.Timestamp(x).normalize()


def nan_equal(a: np.ndarray, b: np.ndarray, rel_tol: float) -> np.ndarray:
    """True where both NaN or |a-b| <= rel_tol x max(|a|,|b|) (elementwise, float64)."""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    both_nan = np.isnan(a) & np.isnan(b)
    with np.errstate(invalid="ignore"):
        close = np.abs(a - b) <= rel_tol * np.maximum(np.abs(a), np.abs(b))
    return both_nan | (close & ~np.isnan(a) & ~np.isnan(b))


def md_table(rows: list[dict], cols: list[str] | None = None) -> str:
    return pqa.md_table(rows, cols)


def kv_str(d: dict) -> dict:
    """dict with tuple keys (groupby size) -> json-safe string keys."""
    return {("|".join(map(str, k)) if isinstance(k, tuple) else str(k)): int(v) for k, v in d.items()}


def run_cmd(cmd: list[str], log_path: Path, timeout_s: int = 7200) -> dict:
    """Run a subprocess (another module's CLI), capture output to log_path, return {rc, seconds}."""
    t0 = time.time()
    with open(log_path, "a") as f:
        f.write(f"\n==== {now_str()} $ {' '.join(cmd)}\n")
        f.flush()
        try:
            p = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=timeout_s)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            rc = -9
            f.write(f"TIMEOUT after {timeout_s}s\n")
    return {"rc": rc, "seconds": round(time.time() - t0, 1), "cmd": " ".join(cmd)}


# ======================================================================================
# layouts
# ======================================================================================
class PrevBuild:
    """Paths of the previous build. Stage layout (T1/T2/T2_work/...) or published layout (files at the root)."""

    def __init__(self, root: Path, sidecar: Path | None = None):
        self.root = Path(root)
        if (self.root / "T2" / "base_data.parquet").exists():
            self.layout = "stage"
            self.t1 = self.root / "T1"
            self.t2 = self.root / "T2"
            self.t2_work = self.root / "T2_work"
            self.master = self.t1 / "master"
            self.t1_work = self.t1 / "_work"
            self.t5 = self.root / "T5"
            self.t6 = self.root / "T6"
        elif (self.root / "base_data.parquet").exists():
            self.layout = "published"
            self.t1 = self.root
            self.t2 = self.root
            self.t2_work = self.root / "_work"
            self.master = self.root / "master"
            self.t1_work = self.root / "_work"
            self.t5 = self.root
            self.t6 = self.root
        else:
            raise SystemExit(f"--prev-build {root}: neither T2/base_data.parquet nor base_data.parquet found")
        self.base_data = self.t2 / "base_data.parquet"
        self.returns = self.t2 / "derived" / "returns_total.parquet"
        self.split = self.t2 / "factors" / "split.parquet"
        self.dividend = self.t2 / "factors" / "dividend.parquet"
        if sidecar:
            self.sidecar = Path(sidecar)
        elif self.layout == "published":
            # publish.py copies T2_work/adjf_long.parquet to factors/adjustment_factor_daily.parquet (verification side table)
            cand = [self.root / "factors" / "adjustment_factor_daily.parquet", self.root / "_work" / "adjf_long.parquet"]
            self.sidecar = next((c for c in cand if c.exists()), cand[0])
        else:
            cand = [self.t2_work / "adjf_long.parquet", self.t2 / "factors" / "adjustment_factor_daily.parquet"]
            self.sidecar = next((c for c in cand if c.exists()), cand[0])
        self.calendar = self.root / "calendar.parquet"          # publish.py / update_daily: date, nClose, rollingMedian, isTradingDay
        self.raw_splitcache = self.t2_work / "raw_splitcache.parquet"
        self.raw_divadj = self.t2_work / "raw_divadj.parquet"
        self.qa_t2 = self.t2 / "qa"
        # known G-JUMP vendor artifacts (T2 list): stage T2/qa or T2_work; published qa/T2/ (publish copies the stage qa dirs)
        self.known_artifacts = [self.qa_t2 / "factor_artifacts.csv", self.t2_work / "gjump_fake_jumps.csv",
                                self.root / "qa" / "T2" / "factor_artifacts.csv", self.root / "qa" / "factor_artifacts.csv",
                                self.root / "qa" / "T2" / "gjump_fake_jumps.csv"]
        self.universe = self.t5 / "universe" / "membership.parquet"
        self.marketcap = self.t5 / "derived" / "marketcap.parquet"
        self.events = self.t6 / "events" / "corporate_actions.parquet"
        self.ledger = self.t6 / "events" / "ca_ledger.parquet"          # rc2: corporate-action ledger (returns_total v2 input)
        self.archive = self.root / "archive"
        self.build_json = self.root / "BUILD.json"
        self.manifest = self.root / "manifest.json"
        # rc2f (D2): engine export of the previous build (display rows), apply log
        self.engine_export = (self.root / "engine" / "ca_ledger_cache.parquet") if self.layout == "published" else (self.t6 / "engine" / "ca_ledger_cache.parquet")
        self.rc2f_dir = (self.root / "qa" / "rc2f") if self.layout == "published" else (self.root / "rc2f")

    def master_file(self, name: str) -> Path:
        return self.master / f"{name}.parquet"

    def as_of(self) -> pd.Timestamp:
        """asOf of the previous build: BUILD.json / manifest.json, else max date of base_data row-group statistics."""
        for p, key in ((self.build_json, "asOf"), (self.manifest, "asOfDate")):
            if p.exists():
                v = json.loads(p.read_text()).get(key)
                if v:
                    return ts(v)
        pf = pq.ParquetFile(self.base_data)
        i = pf.schema_arrow.get_field_index("date")
        mx = None
        for rg in range(pf.num_row_groups):
            s = pf.metadata.row_group(rg).column(i).statistics
            if s is not None and s.has_min_max:
                mx = s.max if mx is None else max(mx, s.max)
        return ts(mx)


class OutBuild:
    """The new build directory (stage layout, publish.py-compatible)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.t1 = self.root / "T1"
        self.master = self.t1 / "master"
        self.t1_work = self.t1 / "_work"
        self.t1_qa = self.t1 / "qa"
        self.t2 = self.root / "T2"
        self.t2_work = self.root / "T2_work"
        self.t2_qa = self.t2 / "qa"
        self.t5 = self.root / "T5"
        self.t5_work = self.root / "T5_work"
        self.t6 = self.root / "T6"
        self.archive = self.root / "archive"
        self.qa = self.root / "qa"
        self.base_data = self.t2 / "base_data.parquet"
        self.sidecar = self.t2_work / "adjf_long.parquet"
        self.returns = self.t2 / "derived" / "returns_total.parquet"
        self.split = self.t2 / "factors" / "split.parquet"
        self.dividend = self.t2 / "factors" / "dividend.parquet"
        self.raw_splitcache = self.t2_work / "raw_splitcache.parquet"
        self.raw_divadj = self.t2_work / "raw_divadj.parquet"
        self.universe = self.t5 / "universe" / "membership.parquet"
        self.marketcap = self.t5 / "derived" / "marketcap.parquet"
        self.events = self.t6 / "events" / "corporate_actions.parquet"
        self.ledger = self.t6 / "events" / "ca_ledger.parquet"          # rc2: corporate-action ledger (returns_total v2 input)
        self.side_published_name = self.t2 / "factors" / "adjustment_factor_daily.parquet"   # hard link to the side file (publish name)
        self.calendar = self.root / "calendar.parquet"
        self.build_json = self.root / "BUILD.json"
        self.log_path = self.root / "update.log"
        self.cmd_log = self.root / "subprocess.log"
        # rc2f (D2)
        self.rc2f = self.root / "rc2f"                                    # apply log / info / override copies / aux work
        self.split_vendor = self.t2_work / "split_vendor.parquet"         # vendor-rebuilt split table before the rc2f split overrides (G-SPLIT reference)
        self.engine = self.t6 / "engine"                                  # pre-publish engine export (display rows, exposure)
        self.engine_export = self.engine / "ca_ledger_cache.parquet"

    def mkdirs(self):
        for d in (self.master, self.t1_work, self.t1_qa, self.t2 / "factors", self.t2 / "derived", self.t2_qa, self.t2_work,
                  self.archive, self.qa, self.rc2f, self.t6 / "events", self.t6 / "qa"):
            d.mkdir(parents=True, exist_ok=True)

    def master_file(self, name: str) -> Path:
        return self.master / f"{name}.parquet"

    def revisions_dir(self, observed: pd.Timestamp) -> Path:
        """archive/revisions/date=<observed>; a second build on the same observed date (rerun) does not overwrite the earlier attempt's files:
        the copied directory is renamed date=<observed>.run<N> once per build (D3 hand-over (5))."""
        d = self.archive / "revisions" / f"date={observed:%Y-%m-%d}"
        if not getattr(self, "_rev_fresh", False):
            self._rev_fresh = True
            if d.exists() and any(d.iterdir()):
                n = 1
                while (d.with_name(d.name + f".run{n}")).exists():
                    n += 1
                d.rename(d.with_name(d.name + f".run{n}"))
        d.mkdir(parents=True, exist_ok=True)
        return d


# ======================================================================================
# calendar / asOf
# ======================================================================================
def last_closed_session(now_utc: dt.datetime | None = None) -> pd.Timestamp:
    """Latest NYSE trading day whose regular session has closed (observe_finalization calendar, hard-coded holidays)."""
    now = now_utc or dt.datetime.now(dt.timezone.utc)
    t0, _, _, _ = obsf.target_days(now.astimezone(ET))
    return ts(pd.Timestamp(t0))


CAL_WINDOW, CAL_MIN_PERIODS, CAL_FRAC = 41, 5, 0.2      # publish.apply_calendar rule (lead, 2026-09-16)


def calendar_from_counts(n_close: pd.Series) -> pd.DataFrame:
    """publish.py rule: trading day = close count >= 20% of the centered 41-day rolling median (min_periods 5).
    Returns date, nClose, rollingMedian, isTradingDay (same schema as the published calendar.parquet)."""
    n = n_close.sort_index().astype("int64")
    med = n.rolling(CAL_WINDOW, center=True, min_periods=CAL_MIN_PERIODS).median()
    is_trading = n >= CAL_FRAC * med
    return pd.DataFrame({"date": pd.to_datetime(n.index).astype("datetime64[ns]"), "nClose": n.to_numpy(), "rollingMedian": med.to_numpy(),
                         "isTradingDay": is_trading.to_numpy()}).reset_index(drop=True)


def load_prev_calendar(prev: PrevBuild) -> pd.DataFrame:
    """Previous build's calendar.parquet, else recomputed from its base_data (date, close) with the same rule."""
    if prev.calendar.exists():
        c = pd.read_parquet(prev.calendar)
        c["date"] = pd.to_datetime(c.date).astype("datetime64[ns]")
        return c.sort_values("date").reset_index(drop=True)
    pf = pq.ParquetFile(prev.base_data)
    counts = None
    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg, columns=["date", "close"])
        d = t["date"].to_numpy().astype("datetime64[ns]")
        has = ~np.isnan(t["close"].to_numpy(zero_copy_only=False))
        c = pd.Series(has.astype(np.int64)).groupby(pd.Series(d)).sum()
        counts = c if counts is None else counts.add(c, fill_value=0)
    return calendar_from_counts(counts)


def pick_asof(ciq_close: pd.Series, mi_close: pd.Series, tdays: pd.DatetimeIndex, upto: pd.Timestamp, prev_asof: pd.Timestamp,
              explicit: pd.Timestamp | None, ratio: float = CLOSE_COUNT_MIN_RATIO) -> dict:
    """Close-count rule per trading day D in the window: ciq_close(D) >= ratio x ciq_close(previous trading day).

    explicit asOf: return its check (ok False -> caller aborts).  auto: latest passing day <= upto; days on/before
    prev_asof are considered already complete (they passed when they were appended)."""
    td = [d for d in tdays if d <= upto]
    checks = {}
    for i, d in enumerate(td):
        prev_d = td[i - 1] if i > 0 else None
        c, p = int(ciq_close.get(d, 0)), (int(ciq_close.get(prev_d, 0)) if prev_d is not None else None)
        ok = (p is None) or (c >= ratio * p)
        checks[str(d.date())] = {"ciq_close": c, "prev_ciq_close": p, "mi_close": int(mi_close.get(d, 0)), "ok": bool(ok)}
    if explicit is not None:
        key = str(explicit.date())
        chk = checks.get(key, {"ciq_close": int(ciq_close.get(explicit, 0)), "prev_ciq_close": None, "mi_close": int(mi_close.get(explicit, 0)),
                               "ok": explicit <= prev_asof})
        return {"asOf": explicit, "ok": bool(chk["ok"] and (explicit in tdays or explicit <= prev_asof)), "checks": checks, "mode": "explicit"}
    best = None
    for d in td:
        if d <= prev_asof or checks[str(d.date())]["ok"]:
            best = d
    return {"asOf": best, "ok": best is not None, "checks": checks, "mode": "auto"}


# ======================================================================================
# price rows: compare / flags / merge
# ======================================================================================
def quality_flags(ids, dates, close, mi_close_map: dict, asof: pd.Timestamp) -> np.ndarray:
    """qualityFlag for fetched rows: 3 null close; on asOf 0 only when ciq & mi closes exist and agree (else 1);
    before asOf 2 when mi disagrees (> MI_FLAG_TOL), else 0."""
    keys = row_keys(ids, dates)
    mi = np.array([mi_close_map.get(int(k), np.nan) for k in keys], dtype=np.float64)
    close = np.asarray(close, dtype=np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        rel = np.abs(close / mi - 1.0)
    has_mi = np.isfinite(mi)
    match = has_mi & (rel <= MI_FLAG_TOL)
    is_asof = np.asarray(dates).astype("datetime64[ns]") == np.datetime64(asof, "ns")
    q = np.where(is_asof, np.where(match, Q_CONFIRMED, Q_PROVISIONAL),
                 np.where(has_mi & ~match, Q_MI_MISMATCH, Q_CONFIRMED))
    q = np.where(np.isnan(close), Q_NULL_CLOSE, q)
    return q.astype(np.int8)


def compare_rows(prev: pd.DataFrame, new: pd.DataFrame, observed: pd.Timestamp, reason: str = "vendor_refetch",
                 rel_tol: float = REL_TOL_REVISION, prev_asof: pd.Timestamp | None = None) -> tuple[pd.DataFrame, np.ndarray, dict]:
    """prev/new: frames with tradingItemId, date, PRICE_FIELDS (+ prev has source). Returns
    (revisions frame, changed mask over new rows (any field changed), stats). Rows only in new on dates <= prev_asof
    are 'late rows' (field 'row', reason 'vendor_late_row'; rows on newer dates are ordinary appends, not revisions);
    rows only in prev are 'vendor_removed'."""
    kp = row_keys(prev.tradingItemId.to_numpy(), prev.date.to_numpy())
    kn = row_keys(new.tradingItemId.to_numpy(), new.date.to_numpy())
    pos = np.searchsorted(kp, kn)
    pos_c = np.minimum(pos, max(len(kp) - 1, 0))
    common = (len(kp) > 0) & (pos < len(kp))
    if len(kp):
        common &= kp[pos_c] == kn
    revs = []
    changed = np.zeros(len(new), dtype=bool)
    ci = np.flatnonzero(common)
    pi = pos_c[ci]
    for f in PRICE_FIELDS:
        a = prev[f].to_numpy(dtype=np.float64)[pi]
        b = new[f].to_numpy(dtype=np.float64)[ci]
        same = nan_equal(a, b, rel_tol)
        bad = ~same
        if bad.any():
            changed[ci[bad]] = True
            revs.append(pd.DataFrame({"tradingItemId": new.tradingItemId.to_numpy()[ci[bad]], "date": new.date.to_numpy()[ci[bad]],
                                      "field": f, "old": a[bad], "new": b[bad], "observedAt": observed, "reason": reason}))
    only_new = ~common
    late = only_new.copy()
    if prev_asof is not None:
        late &= to_ns(new.date) <= np.datetime64(prev_asof, "ns")
    if late.any():
        revs.append(pd.DataFrame({"tradingItemId": new.tradingItemId.to_numpy()[late], "date": new.date.to_numpy()[late], "field": "row",
                                  "old": np.nan, "new": new.close.to_numpy(dtype=np.float64)[late], "observedAt": observed,
                                  "reason": "vendor_late_row"}))
    in_new = np.isin(kp, kn)
    removed = ~in_new
    if removed.any():
        revs.append(pd.DataFrame({"tradingItemId": prev.tradingItemId.to_numpy()[removed], "date": prev.date.to_numpy()[removed], "field": "row",
                                  "old": prev.close.to_numpy(dtype=np.float64)[removed], "new": np.nan, "observedAt": observed,
                                  "reason": "vendor_removed"}))
    rev = pd.concat(revs, ignore_index=True) if revs else pd.DataFrame(columns=[f.name for f in REV_PRICE_SCHEMA])
    rev = finalize_revisions(rev)
    stats = {"prev_rows": int(len(prev)), "new_rows": int(len(new)), "common_rows": int(common.sum()), "late_rows": int(late.sum()),
             "appended_rows": int(only_new.sum() - late.sum()), "removed_rows": int(removed.sum()), "changed_rows": int(changed.sum()),
             "changed_cells": int((rev.field != "row").sum()), "changed_by_field": rev[rev.field != "row"].field.value_counts().to_dict()}
    return rev, changed, stats


def finalize_revisions(rev: pd.DataFrame) -> pd.DataFrame:
    if len(rev) == 0:
        return pd.DataFrame({"tradingItemId": pd.Series(dtype="int64"), "date": pd.Series(dtype="datetime64[ns]"), "field": pd.Series(dtype=object),
                             "old": pd.Series(dtype="float64"), "new": pd.Series(dtype="float64"), "observedAt": pd.Series(dtype="datetime64[ns]"),
                             "reason": pd.Series(dtype=object)})
    rev = rev.copy()
    rev["tradingItemId"] = rev.tradingItemId.astype(np.int64)
    rev["date"] = pd.to_datetime(rev.date).astype("datetime64[ns]")
    rev["observedAt"] = pd.to_datetime(rev.observedAt).astype("datetime64[ns]")
    rev["old"] = rev.old.astype(np.float64)
    rev["new"] = rev.new.astype(np.float64)
    return rev.sort_values(["tradingItemId", "date", "field"]).reset_index(drop=True)[[f.name for f in REV_PRICE_SCHEMA]]


def _float_array(values, typ) -> pa.Array:
    """float column with NaN stored as parquet null (the vendor pull yields nulls; a pandas round trip would turn them
    into NaN values and change the file bytes although every consumer reads both as NaN)."""
    v = np.asarray(values, dtype=np.float32 if typ == pa.float32() else np.float64)
    return pa.array(v, type=typ, mask=np.isnan(v))


def base_from_pandas(df: pd.DataFrame) -> pa.Table:
    cols = {"date": pa.array(to_ns(df.date), pa.timestamp("ns")), "tradingItemId": pa.array(df.tradingItemId.to_numpy(np.int64), pa.int64())}
    for f in ("open", "high", "low", "close", "vwap"):
        cols[f] = _float_array(df[f].to_numpy(dtype=np.float32), pa.float32())
    cols["volume"] = _float_array(df.volume.to_numpy(dtype=np.float64), pa.float64())
    cols["source"] = pa.array(df.source.to_numpy(dtype=np.int8), pa.int8())
    cols["qualityFlag"] = pa.array(df.qualityFlag.to_numpy(dtype=np.int8), pa.int8())
    return pa.table(cols, schema=pb.BASE_SCHEMA)


def side_from_pandas(df: pd.DataFrame) -> pa.Table:
    return pa.table({"tradingItemId": pa.array(df.tradingItemId.to_numpy(np.int64), pa.int64()),
                     "date": pa.array(to_ns(df.date), pa.timestamp("ns")),
                     "adjustmentfactor": _float_array(df.adjustmentfactor.to_numpy(dtype=np.float64), pa.float64()),
                     "ciqClose": _float_array(df.ciqClose.to_numpy(dtype=np.float64), pa.float64())}, schema=pb.ADJF_SCHEMA)


def read_rows_filtered(path: Path, columns: list[str] | None, date_from: pd.Timestamp | None = None, ids: np.ndarray | None = None) -> pd.DataFrame:
    """Row-group streaming read of a (tradingItemId, date)-sorted long file with a date lower bound and/or an id set."""
    pf = pq.ParquetFile(path)
    parts = []
    id_idx = pf.schema_arrow.get_field_index("tradingItemId")
    ids_sorted = np.sort(np.asarray(ids, dtype=np.int64)) if ids is not None else None
    for rg in range(pf.num_row_groups):
        if ids_sorted is not None:
            st = pf.metadata.row_group(rg).column(id_idx).statistics
            if st is not None and st.has_min_max:
                k = np.searchsorted(ids_sorted, st.min)
                if k >= len(ids_sorted) or ids_sorted[k] > st.max:
                    continue
        t = pf.read_row_group(rg, columns=columns)
        mask = None
        if date_from is not None:
            mask = pc.greater_equal(t["date"], pa.scalar(date_from.to_pydatetime(), type=pa.timestamp("ns")))
        if ids_sorted is not None:
            m2 = pc.is_in(t["tradingItemId"], value_set=pa.array(ids_sorted))
            mask = m2 if mask is None else pc.and_(mask, m2)
        if mask is not None:
            t = t.filter(mask)
        if t.num_rows:
            parts.append(t.to_pandas())
    if not parts:
        cols = columns or [f.name for f in pf.schema_arrow]
        return pd.DataFrame({c: pd.Series(dtype=pf.schema_arrow.field(c).type.to_pandas_dtype()) for c in cols})
    return pd.concat(parts, ignore_index=True)


class Pending:
    """Rows to write into the new base_data / side file, with the replacement scopes.

    window rows: authoritative for every id (not in full_ids) on dates in [win_lo, win_hi];
    full rows   : authoritative for full_ids on every date (new ids, adjustment-factor re-fetched ids)."""

    def __init__(self):
        self.asof = None                       # rows after asOf are dropped at merge time (auto mode: U1 runs before U2 fixes asOf)
        self.win_lo = self.win_hi = None
        self.win_base: list[pa.Table] = []
        self.win_side: list[pa.Table] = []
        self.full_ids: set[int] = set()
        self.full_base: list[pa.Table] = []
        self.full_side: list[pa.Table] = []
        self.side_only_ids: set[int] = set()     # full-scope ids whose base values are kept (only rows aligned) -> handled by caller

    def add_window(self, base: pa.Table, side: pa.Table, lo: pd.Timestamp, hi: pd.Timestamp):
        self.win_lo, self.win_hi = lo, hi
        self.win_base.append(base)
        self.win_side.append(side)

    def add_full(self, base: pa.Table, side: pa.Table, ids):
        self.full_ids |= set(int(i) for i in ids)
        self.full_base.append(base)
        self.full_side.append(side)

    def tables(self) -> tuple[pa.Table, pa.Table]:
        """Window rows of full-scope ids are superseded by their full rows (same vendor snapshot)."""
        win_b = pa.concat_tables(self.win_base) if self.win_base else pb.BASE_SCHEMA.empty_table()
        win_s = pa.concat_tables(self.win_side) if self.win_side else pb.ADJF_SCHEMA.empty_table()
        if self.full_ids and win_b.num_rows:
            fa = pa.array(np.array(sorted(self.full_ids), dtype=np.int64))
            win_b = win_b.filter(pc.invert(pc.is_in(win_b["tradingItemId"], value_set=fa)))
            win_s = win_s.filter(pc.invert(pc.is_in(win_s["tradingItemId"], value_set=fa)))
        base = pa.concat_tables([win_b] + self.full_base)
        side = pa.concat_tables([win_s] + self.full_side)
        if self.asof is not None and base.num_rows:
            cut = pa.scalar(pd.Timestamp(self.asof).to_pydatetime(), type=pa.timestamp("ns"))
            base = base.filter(pc.less_equal(base["date"], cut))
            side = side.filter(pc.less_equal(side["date"], cut))
        return base, side

    def empty(self) -> bool:
        return not (self.win_base or self.full_base)


def merge_prices(prev_base: Path, prev_side: Path | None, out_base: Path, out_side: Path, pend: Pending) -> dict:
    """Row group by row group: drop the prev rows inside the pending scopes, add the pending rows of that group's id
    range, sort (tradingItemId, date), write both files with the previous row-group structure. Post-lifecycle flags
    (4, publish-derived) are reset to 0. Returns row counts and the strict-sort / alignment proof."""
    t0 = time.time()
    pf = pq.ParquetFile(prev_base)
    pf_s = pq.ParquetFile(prev_side) if prev_side is not None and prev_side.exists() else None
    side_global = None
    n_side_unaligned = 0

    def load_side_global():
        ts_ = pq.read_table(prev_side)
        ks = row_keys(ts_["tradingItemId"].to_numpy(), ts_["date"].to_numpy())
        order = np.argsort(ks, kind="stable")
        return (ks[order], ts_["adjustmentfactor"].to_numpy(zero_copy_only=False)[order], ts_["ciqClose"].to_numpy(zero_copy_only=False)[order])

    if pf_s is not None and (pf_s.num_row_groups != pf.num_row_groups or pf_s.metadata.num_rows != pf.metadata.num_rows):
        # published build: base_data was rewritten by publish.py (row groups re-cut, off-calendar rows removed, aux rows merged) while the
        # side table kept the stage layout -> align by (id, date) key from the whole side table (about 1.5 GB in memory)
        side_global = load_side_global()
        log.warning("side file rows/row groups (%d/%d) differ from base_data (%d/%d): aligning by key", pf_s.metadata.num_rows, pf_s.num_row_groups,
                    pf.metadata.num_rows, pf.num_row_groups)
    id_idx = pf.schema_arrow.get_field_index("tradingItemId")
    los = np.array([pf.metadata.row_group(rg).column(id_idx).statistics.min for rg in range(pf.num_row_groups)], dtype=np.int64)
    new_base, new_side = pend.tables()
    nb_ids = new_base["tradingItemId"].to_numpy()
    group_of = np.maximum(np.searchsorted(los, nb_ids, side="right") - 1, 0)
    ns_ids = new_side["tradingItemId"].to_numpy()
    group_of_s = np.maximum(np.searchsorted(los, ns_ids, side="right") - 1, 0)
    full_arr = pa.array(np.array(sorted(pend.full_ids), dtype=np.int64)) if pend.full_ids else None
    wb = pb.RowGroupWriter(out_base, pb.BASE_SCHEMA)
    ws = pb.RowGroupWriter(out_side, pb.ADJF_SCHEMA)
    n_prev = n_dropped = n_added = n_out = n_reset4 = 0
    last_key = -1
    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg)
        s = None
        if pf_s is not None and side_global is None:
            s = pf_s.read_row_group(rg)
            ks_rg = row_keys(s["tradingItemId"].to_numpy(), s["date"].to_numpy())
            kb_rg = row_keys(t["tradingItemId"].to_numpy(), t["date"].to_numpy())
            if not np.array_equal(ks_rg, kb_rg):
                log.warning("row group %d: previous side file rows do not match base_data rows -> aligning the side table by key", rg)
                side_global = load_side_global()
                s = None
        if s is None and side_global is not None:
            kb_rg = row_keys(t["tradingItemId"].to_numpy(), t["date"].to_numpy())
            pos = np.searchsorted(side_global[0], kb_rg)
            pos_c = np.minimum(pos, len(side_global[0]) - 1)
            hit = side_global[0][pos_c] == kb_rg
            n_side_unaligned += int((~hit).sum())
            adj = np.where(hit, side_global[1][pos_c], np.nan)
            cc = np.where(hit, side_global[2][pos_c], np.nan)
            s = pa.table({"tradingItemId": t["tradingItemId"], "date": t["date"], "adjustmentfactor": _float_array(adj, pa.float64()),
                          "ciqClose": _float_array(cc, pa.float64())}, schema=pb.ADJF_SCHEMA)
        n_prev += t.num_rows
        keep = np.ones(t.num_rows, dtype=bool)
        if pend.win_lo is not None:
            d = t["date"].to_numpy().astype("datetime64[ns]")
            in_win = (d >= np.datetime64(pend.win_lo, "ns")) & (d <= np.datetime64(pend.win_hi, "ns"))
            keep &= ~in_win
        if full_arr is not None:
            in_full = pc.is_in(t["tradingItemId"], value_set=full_arr).to_numpy(zero_copy_only=False)
            keep &= ~in_full
        if pend.win_lo is not None and full_arr is not None:
            # rows of full ids inside the window were dropped by both rules: fine (full rows carry them)
            pass
        q = t["qualityFlag"].to_numpy()
        if (q == Q_POST_LIFECYCLE).any():
            q2 = q.copy()
            n_reset4 += int((q2 == Q_POST_LIFECYCLE).sum())
            q2[q2 == Q_POST_LIFECYCLE] = Q_CONFIRMED
            t = t.set_column(t.schema.get_field_index("qualityFlag"), "qualityFlag", pa.array(q2, pa.int8()))
        kept = t.filter(pa.array(keep))
        n_dropped += t.num_rows - kept.num_rows
        add = new_base.filter(pa.array(group_of == rg))
        n_added += add.num_rows
        merged = pa.concat_tables([kept, add]).sort_by([("tradingItemId", "ascending"), ("date", "ascending")]).combine_chunks()
        keys = row_keys(merged["tradingItemId"].to_numpy(), merged["date"].to_numpy())
        if len(keys) and (not (np.diff(keys) > 0).all() or keys[0] <= last_key):
            raise RuntimeError(f"row group {rg}: merged rows are not strictly sorted / unique on (tradingItemId, date)")
        if len(keys):
            last_key = int(keys[-1])
        wb.write(merged)
        n_out += merged.num_rows
        if s is not None:
            kept_s = s.filter(pa.array(keep))
            add_s = new_side.filter(pa.array(group_of_s == rg))
            merged_s = pa.concat_tables([kept_s, add_s]).sort_by([("tradingItemId", "ascending"), ("date", "ascending")]).combine_chunks()
            ks = row_keys(merged_s["tradingItemId"].to_numpy(), merged_s["date"].to_numpy())
            if not np.array_equal(ks, keys):
                raise RuntimeError(f"row group {rg}: side file rows do not align with base_data after the merge")
            ws.write(merged_s)
    wb.close()
    if pf_s is not None:
        ws.close()
    else:
        ws.writer.close()
        ws.tmp.unlink(missing_ok=True)
    if pf_s is not None:
        # publish name for the side table (contract: verification side table) -> hard link to the same file (copy if no link)
        pub = out_side.parent.parent / "T2" / "factors" / "adjustment_factor_daily.parquet" if out_side.parent.name == "T2_work" else None
        if pub is not None:
            pub.parent.mkdir(parents=True, exist_ok=True)
            pub.unlink(missing_ok=True)
            try:
                os.link(out_side, pub)
            except OSError:
                shutil.copy2(out_side, pub)
    return {"rows_prev": n_prev, "rows_dropped": n_dropped, "rows_added": n_added, "rows_out": n_out, "row_groups": wb.groups,
            "post_lifecycle_flags_reset": n_reset4, "bytes": out_base.stat().st_size, "seconds": round(time.time() - t0, 1),
            "side_written": pf_s is not None, "side_aligned_by_key": side_global is not None, "side_rows_missing_for_base_rows": n_side_unaligned}


# ======================================================================================
# factor diff / restatement detection / raw invariance
# ======================================================================================
def factor_diff(prev: pd.DataFrame, new: pd.DataFrame, table: str, observed: pd.Timestamp) -> pd.DataFrame:
    """Interval-level diff keyed on (tradingItemId, validFrom): added / removed / changed (validTo, factor, source)."""
    key = ["tradingItemId", "validFrom"]
    a = prev[key + ["validTo", "factor", "source"]].rename(columns={"validTo": "validTo_old", "factor": "factor_old", "source": "source_old"})
    b = new[key + ["validTo", "factor", "source"]].rename(columns={"validTo": "validTo_new", "factor": "factor_new", "source": "source_new"})
    j = a.merge(b, on=key, how="outer", indicator=True)
    both = j._merge == "both"
    same_to = (j.validTo_old.isna() & j.validTo_new.isna()) | (j.validTo_old == j.validTo_new)
    same_f = nan_equal(j.factor_old.to_numpy(), j.factor_new.to_numpy(), 1e-12)
    same_src = j.source_old.fillna("") == j.source_new.fillna("")
    changed = both & ~(same_to & same_f & same_src)
    j["kind"] = np.where(j._merge == "left_only", "removed", np.where(j._merge == "right_only", "added", np.where(changed, "changed", "same")))
    out = j[j.kind != "same"].copy()
    out["table"] = table
    out["observedAt"] = observed
    out = out[REV_FACTOR_COLS].sort_values(["table", "tradingItemId", "validFrom"]).reset_index(drop=True)
    out["tradingItemId"] = out.tradingItemId.astype(np.int64)
    for c in ("validFrom", "validTo_old", "validTo_new", "observedAt"):
        out[c] = pd.to_datetime(out[c]).astype("datetime64[ns]")
    return out


def splitcache_changed_ids(prev_sc: pd.DataFrame | None, new_sc: pd.DataFrame) -> set[int]:
    """Ids whose set of ciqsplitcache windows (splitdate, nextsplitdate, csf, lsf) differs from the stored copy."""
    if prev_sc is None:
        return set()

    def norm(sc: pd.DataFrame) -> pd.DataFrame:
        d = pd.DataFrame({"tid": sc.tradingitemid.astype(np.int64), "s": pd.to_datetime(sc.splitdate).astype("datetime64[ns]"),
                          "e": pd.to_datetime(sc.nextsplitdate).astype("datetime64[ns]"),
                          "csf": sc.cumulativesplitfactor.astype(float).round(12), "lsf": sc.latestsplitfactor.astype(float).round(12)})
        return d.drop_duplicates()

    a, b = norm(prev_sc), norm(new_sc)
    j = a.merge(b, on=["tid", "s", "e", "csf", "lsf"], how="outer", indicator=True)
    return set(int(i) for i in j.loc[j._merge != "both", "tid"].unique())


def adjf_mismatch_ids(win_side: pd.DataFrame, prev_side: pd.DataFrame, split_lookup: pfm.FactorLookup, tol_side: float = 1e-9,
                      tol_lookup: float = 1e-6) -> dict:
    """Ids whose re-fetched window adjustmentfactor differs from the stored side file (restated history) or from the
    splitcache-based lookup (splitcache does not reproduce the vendor factor)."""
    out = {"vs_side": set(), "vs_lookup": set()}
    if len(win_side) == 0:
        return out
    kn = row_keys(win_side.tradingItemId.to_numpy(), win_side.date.to_numpy())
    if len(prev_side):
        kp = row_keys(prev_side.tradingItemId.to_numpy(), prev_side.date.to_numpy())
        order = np.argsort(kp)
        kp_s, adj_p = kp[order], prev_side.adjustmentfactor.to_numpy(dtype=np.float64)[order]
        pos = np.searchsorted(kp_s, kn)
        pos_c = np.minimum(pos, len(kp_s) - 1)
        common = (pos < len(kp_s)) & (kp_s[pos_c] == kn)
        a = adj_p[pos_c][common]
        b = win_side.adjustmentfactor.to_numpy(dtype=np.float64)[common]
        with np.errstate(invalid="ignore", divide="ignore"):
            bad = ~(np.abs(a / b - 1.0) <= tol_side)
        out["vs_side"] = set(int(i) for i in win_side.tradingItemId.to_numpy()[common][bad])
    sf = split_lookup(win_side.tradingItemId.to_numpy(), win_side.date.to_numpy(), strict=False)
    with np.errstate(invalid="ignore", divide="ignore"):
        bad2 = ~(np.abs(sf / win_side.adjustmentfactor.to_numpy(dtype=np.float64) - 1.0) <= tol_lookup)
    out["vs_lookup"] = set(int(i) for i in win_side.tradingItemId.to_numpy()[bad2])
    return out


def raw_invariance(prev_rows: pd.DataFrame, new_rows: pd.DataFrame, new_side: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """For adjustment-factor re-fetched ids: raw = float32(ciq price x adjf) must equal the stored raw on common rows.
    Tolerance [estimate, from the vendor's 6-decimal price storage]: |d| <= 1e-6 x max(1, adjf_new) + 1.2e-7 x |raw|."""
    kp = row_keys(prev_rows.tradingItemId.to_numpy(), prev_rows.date.to_numpy())
    kn = row_keys(new_rows.tradingItemId.to_numpy(), new_rows.date.to_numpy())
    op = np.argsort(kp)
    kp_s = kp[op]
    pos = np.searchsorted(kp_s, kn)
    pos_c = np.minimum(pos, max(len(kp_s) - 1, 0))
    common = (len(kp_s) > 0) & (pos < len(kp_s))
    if len(kp_s):
        common &= kp_s[pos_c] == kn
    adj = new_side.adjustmentfactor.to_numpy(dtype=np.float64)
    viol = []
    max_rel = 0.0
    n_cmp = 0
    for f in ("open", "high", "low", "close", "vwap"):
        a = prev_rows[f].to_numpy(dtype=np.float64)[op][pos_c][common]
        b = new_rows[f].to_numpy(dtype=np.float64)[common]
        tol = 1e-6 * np.maximum(1.0, adj[common]) + 1.2e-7 * np.abs(a)
        both = np.isfinite(a) & np.isfinite(b)
        n_cmp += int(both.sum())
        d = np.abs(a - b)
        bad = both & (d > tol)
        with np.errstate(invalid="ignore", divide="ignore"):
            rel = np.where(both & (np.abs(a) > 0), d / np.abs(a), 0.0)
        if rel.size:
            max_rel = max(max_rel, float(np.nanmax(rel)))
        one_nan = np.isfinite(a) != np.isfinite(b)
        bad |= one_nan
        if bad.any():
            viol.append(pd.DataFrame({"tradingItemId": new_rows.tradingItemId.to_numpy()[common][bad], "date": new_rows.date.to_numpy()[common][bad],
                                      "field": f, "stored": a[bad], "refetched": b[bad], "adjf_new": adj[common][bad]}))
    v = pd.concat(viol, ignore_index=True) if viol else pd.DataFrame(columns=["tradingItemId", "date", "field", "stored", "refetched", "adjf_new"])
    return v, {"rows_common": int(common.sum()), "cells_compared": n_cmp, "violations": int(len(v)), "max_rel_diff": max_rel,
               "rows_only_in_refetch": int((~common).sum()), "rows_only_in_stored": int(len(kp) - common.sum())}


# ======================================================================================
# lifecycle aggregates (incremental)
# ======================================================================================
AGG_COLS = ["tradingItemId", "first_close", "last_close", "first_row", "last_row", "n_rows", "n_close", "n_close_2000", "queriedAt"]


def agg_from_rows(ids, dates, close_notna, queried: pd.Timestamp) -> pd.DataFrame:
    """Brute-force aggregate (same definition as master_lifecycle CIQ_SQL/MI_SQL) over a set of rows."""
    d = pd.DataFrame({"tradingItemId": np.asarray(ids, dtype=np.int64), "date": to_ns(dates), "c": np.asarray(close_notna, dtype=bool)})
    g = d.groupby("tradingItemId")
    out = pd.DataFrame({"first_row": g.date.min(), "last_row": g.date.max(), "n_rows": g.size()})
    dc = d[d.c]
    gc = dc.groupby("tradingItemId")
    out["first_close"] = gc.date.min()
    out["last_close"] = gc.date.max()
    out["n_close"] = gc.size().reindex(out.index).fillna(0).astype(np.int64)
    out["n_close_2000"] = dc[dc.date >= np.datetime64("2000-01-03", "ns")].groupby("tradingItemId").size().reindex(out.index).fillna(0).astype(np.int64)
    out["queriedAt"] = pd.Timestamp(queried)
    out = out.reset_index()
    for c in ("first_close", "last_close", "first_row", "last_row"):
        out[c] = pd.to_datetime(out[c]).astype("datetime64[ns]")
    return out[AGG_COLS]


def update_agg(prev: pd.DataFrame, window: pd.DataFrame, win_lo: pd.Timestamp, full: pd.DataFrame | None, queried: pd.Timestamp) -> pd.DataFrame:
    """prev: stored aggregate. window: aggregate of the re-fetched window rows (authoritative for dates >= win_lo for
    every id): counts of rows/closes on dates >= win_lo are replaced by the window's, first/last recomputed.
    full: aggregates of ids re-fetched in full (replace their rows entirely).  Ids absent from prev are added."""
    p = prev.set_index("tradingItemId").copy()
    if full is not None and len(full):
        f = full.set_index("tradingItemId")
        p = p.drop(index=[i for i in f.index if i in p.index])
        p = pd.concat([p, f])
    if len(window):
        w = window.set_index("tradingItemId")
        # rows of prev on/after win_lo cannot be counted exactly without the file -> the caller passes `window` built from
        # (prev rows >= win_lo replaced by new rows); here we assume prev counts exclude nothing: adjust by the caller-provided
        # 'delta' columns when present
        for tid in w.index:
            if tid in p.index:
                r = p.loc[tid]
                nr = int(w.at[tid, "n_rows"]) + int(r.n_rows) - int(w.at[tid, "prev_rows_in_window"]) if "prev_rows_in_window" in w.columns else int(r.n_rows)
                nc = int(w.at[tid, "n_close"]) + int(r.n_close) - int(w.at[tid, "prev_close_in_window"]) if "prev_close_in_window" in w.columns else int(r.n_close)
                n2 = int(w.at[tid, "n_close_2000"]) + int(r.n_close_2000) - int(w.at[tid, "prev_close_in_window"]) if "prev_close_in_window" in w.columns else int(r.n_close_2000)
                p.at[tid, "n_rows"], p.at[tid, "n_close"], p.at[tid, "n_close_2000"] = nr, nc, n2
                for c, fn in (("first_close", min), ("first_row", min), ("last_close", max), ("last_row", max)):
                    a, b = r[c], w.at[tid, c]
                    if pd.isna(a):
                        v = b
                    elif pd.isna(b):
                        v = a
                    else:
                        v = fn(pd.Timestamp(a), pd.Timestamp(b))
                    # last_* inside the window must come from the window only (a vendor-removed last row disappears)
                    if c.startswith("last") and pd.notna(a) and pd.Timestamp(a) >= win_lo:
                        v = b
                    p.at[tid, c] = v
                p.at[tid, "queriedAt"] = pd.Timestamp(queried)
            else:
                p.loc[tid] = w.loc[tid, [c for c in AGG_COLS if c != "tradingItemId"]]
    out = p.reset_index()
    for c in ("first_close", "last_close", "first_row", "last_row"):
        out[c] = pd.to_datetime(out[c]).astype("datetime64[ns]")
    for c in ("n_rows", "n_close", "n_close_2000"):
        out[c] = out[c].astype(np.int64)
    out["queriedAt"] = pd.to_datetime(out.queriedAt)
    return out.sort_values("tradingItemId").reset_index(drop=True)[AGG_COLS]


# ======================================================================================
# incremental gates (G-JUMP on boundaries of affected ids, G-DIV on new steps)
# ======================================================================================
class ObservedSubset(pqa.Observed):
    """prices_qa.Observed for a subset of ids (same attributes / methods, loaded with row-group pruning)."""

    def __init__(self, base: Path, side: Path | None, ids, split: pfm.FactorLookup, div: pfm.FactorLookup):
        t0 = time.time()
        ids_sorted = np.sort(np.asarray(list(ids), dtype=np.int64))
        b = read_rows_filtered(base, ["date", "tradingItemId", "close", "volume"], ids=ids_sorted)
        keep = np.isfinite(b.close.to_numpy(dtype=np.float64))
        b = b[keep]
        self.ids = b.tradingItemId.to_numpy(np.int64)
        self.dates = to_ns(b.date)
        self.close = b.close.to_numpy(dtype=np.float32)
        self.volume = b.volume.to_numpy(dtype=np.float64)
        if side is not None and side.exists():
            s = read_rows_filtered(side, None, ids=ids_sorted)
            ks = row_keys(s.tradingItemId.to_numpy(), s.date.to_numpy())
            kb = row_keys(self.ids, self.dates)
            pos = np.searchsorted(ks, kb)
            pos = np.minimum(pos, max(len(ks) - 1, 0))
            ok = len(ks) > 0 and np.array_equal(ks[pos], kb)
            if not ok:
                raise RuntimeError("side file rows do not align with base_data for the observed subset")
            self.close64 = (s.ciqClose.to_numpy(dtype=np.float64) * s.adjustmentfactor.to_numpy(dtype=np.float64))[pos]
        else:
            self.close64 = self.close.astype(np.float64)
        self.keys = row_keys(self.ids, self.dates)
        if len(self.keys) and not (np.diff(self.keys) > 0).all():
            raise RuntimeError("observed subset not strictly sorted")
        self.sf = split(self.ids, self.dates) if len(self.ids) else np.array([])
        self.df = div(self.ids, self.dates) if len(self.ids) else np.array([])
        same = np.r_[False, self.ids[1:] == self.ids[:-1]] if len(self.ids) else np.array([], dtype=bool)
        self.prev = np.where(same, np.arange(len(self.ids)) - 1, -1)
        self.split, self.div = split, div
        log.info("observed subset: %d rows / %d ids in %.1fs", len(self.ids), len(ids_sorted), time.time() - t0)


def jump_boundaries(obs: pqa.Observed, restrict_ids: set[int] | None, min_date: pd.Timestamp | None) -> pd.DataFrame:
    """Split-factor boundaries (rows whose sf differs from the previous observed row) with the fake-jump rule (prices_qa.fake_jumps)."""
    has_prev = obs.prev >= 0
    i = np.flatnonzero(has_prev)
    if len(i) == 0:
        return pd.DataFrame(columns=["tradingItemId", "date", "prevDate", "r_adj", "r_raw", "r_total", "sf_prev", "sf", "fake"])
    p = obs.prev[i]
    boundary = obs.sf[i] != obs.sf[p]
    ib, pb_ = i[boundary], p[boundary]
    adj = obs.close.astype(np.float64) / obs.sf
    r_adj = adj[ib] / adj[pb_] - 1.0
    r_raw = obs.close[ib].astype(np.float64) / obs.close[pb_] - 1.0
    r_tot = (adj[ib] * obs.df[ib]) / (adj[pb_] * obs.df[pb_]) - 1.0
    fake = pqa.fake_jumps(r_adj, r_raw, "t2")
    d = pd.DataFrame({"tradingItemId": obs.ids[ib], "date": obs.dates[ib], "prevDate": obs.dates[pb_], "r_adj": r_adj, "r_raw": r_raw,
                      "r_total": r_tot, "sf_prev": obs.sf[pb_], "sf": obs.sf[ib], "fake": fake})
    sel = np.ones(len(d), dtype=bool)
    if restrict_ids is not None and min_date is not None:
        sel = d.tradingItemId.isin(list(restrict_ids)).to_numpy() | (d.date.to_numpy() >= np.datetime64(min_date, "ns"))
    elif restrict_ids is not None:
        sel = d.tradingItemId.isin(list(restrict_ids)).to_numpy()
    elif min_date is not None:
        sel = d.date.to_numpy() >= np.datetime64(min_date, "ns")
    return d[sel].reset_index(drop=True)


def load_known_artifacts(paths: list[Path]) -> pd.DataFrame:
    for p in paths:
        if p.exists():
            k = pd.read_csv(p)
            if {"tradingItemId", "date"} <= set(k.columns):
                k["tradingItemId"] = k.tradingItemId.astype(np.int64)
                k["date"] = pd.to_datetime(k.date)
                return k
    return pd.DataFrame(columns=["tradingItemId", "date"])


def new_dividend_steps(prev_dv: pd.DataFrame | None, new_dv: pd.DataFrame, since: str = "2000-01-03") -> pd.DataFrame:
    """Vendor dividend-factor steps (row k -> k+1 of the same id) whose fromdate is not a window start in the stored copy."""
    d = new_dv.copy()
    d["fromdate"] = pd.to_datetime(d.fromdate).dt.normalize()
    d["todate"] = pd.to_datetime(d.todate).dt.normalize()
    d["divadjfactor"] = d.divadjfactor.astype(float)
    d = d.sort_values(["tradingitemid", "fromdate"]).reset_index(drop=True)
    same = d.tradingitemid.to_numpy()[1:] == d.tradingitemid.to_numpy()[:-1]
    k = np.flatnonzero(same)
    steps = pd.DataFrame({"tradingItemId": d.tradingitemid.to_numpy(np.int64)[k + 1], "date": d.fromdate.to_numpy()[k + 1],
                          "f_prev": d.divadjfactor.to_numpy()[k], "f_new": d.divadjfactor.to_numpy()[k + 1],
                          "contiguous": (d.todate.to_numpy()[k] + np.timedelta64(1, "D")) == d.fromdate.to_numpy()[k + 1]})
    steps = steps[steps.date >= np.datetime64(since, "ns")]
    if prev_dv is not None and len(prev_dv):
        pk = set(zip(prev_dv.tradingitemid.astype(np.int64), pd.to_datetime(prev_dv.fromdate).dt.normalize()))
        is_new = [(int(t), pd.Timestamp(x)) not in pk for t, x in zip(steps.tradingItemId, steps.date)]
        steps = steps[np.array(is_new, dtype=bool)]
    return steps.reset_index(drop=True)


# ======================================================================================
# the update context
# ======================================================================================
class Ctx:
    def __init__(self, args, prev: PrevBuild, out: OutBuild):
        self.args = args
        self.prev = prev
        self.out = out
        self.db: DB | None = None
        self.results: dict = {"steps": {}, "gates": {}, "warnings": []}
        self.timings: dict = {}
        self.observed = ts(pd.Timestamp.now(tz=KST).tz_localize(None))       # observation date (run date, KST)
        self.prev_asof = prev.as_of()
        self.asof: pd.Timestamp | None = None
        self.ids: np.ndarray | None = None          # symbol set (prev instruments + new)
        self.new_ids: list[int] = []
        self.calendar: np.ndarray | None = None
        self.pend = Pending()
        self.win_ciq: pa.Table | None = None        # fetched ciqpriceequity window (all ids, raw vendor columns)
        self.win_mi: pd.DataFrame | None = None
        self.win_base: pd.DataFrame | None = None   # transformed window rows (pandas) with final source/qualityFlag
        self.win_side: pd.DataFrame | None = None
        self.prev_win: pd.DataFrame | None = None   # stored rows inside the window
        self.refresh_dates: list[pd.Timestamp] = []
        self.calendar_frame: pd.DataFrame | None = None
        self.flag_map: dict = {}
        self.price_revisions: list[pd.DataFrame] = []
        self.factor_revisions: pd.DataFrame | None = None
        self.master_revisions: list[dict] = []
        self.refetched_ids: set[int] = set()
        self.ident: pd.DataFrame | None = None
        self.cur: pd.DataFrame | None = None
        self.id_mapping: pd.DataFrame | None = None
        self.raw_gate: dict = {}
        self.merge_done = False
        self.build_id = None
        self.failed = None
        # rc2f (D2)
        self.aux_ids: np.ndarray = np.zeros(0, dtype=np.int64)     # auxiliary instruments (master/aux_instruments, lifecycle status aux)
        self.aux_new_ids: list[int] = []
        self.aux_factor_rows: dict = {}                            # new aux ids: factor intervals to append (reapply_aux_additions)
        self.ovr: dict = {k: pd.DataFrame() for k in ("split", "price", "ledger", "lifecycle", "lineage", "aux")}
        self.ovr_files: list[dict] = []
        self.ovr_issues: list[dict] = []
        self.alog = dr.ra.ApplyLog()
        self.prev_alog: pd.DataFrame | None = None
        self.ovr_cmp: dict = {}
        self.price_win: pd.DataFrame = pd.DataFrame()
        self.rc2f: dict = {}                                       # per-step rc2f results (BUILD.json.results.rc2f)
        self.read_rows = read_rows_filtered
        self.kept_factor_tbl: pd.DataFrame | None = None           # P0-1: intervals carried for kept restatements
        self.kept_factor_vendor: pd.DataFrame | None = None

    ledger_stale_after: pd.Timestamp | None = None

    def price_ids(self) -> list[int]:
        """ids whose prices/factors the build maintains: instruments + auxiliary instruments (rc2 aux, rows live in base_data)."""
        return sorted(set(int(i) for i in self.ids) | set(int(i) for i in self.aux_ids))

    def warn(self, msg: str):
        log.warning(msg)
        self.results["warnings"].append(msg)

    def dbc(self) -> DB:
        if self.db is None:
            self.db = DB(application_name="cache_v21_T9_update")
        return self.db

    def time_step(self, name: str, t0: float, **info):
        self.timings[name] = {"seconds": round(time.time() - t0, 1), "peak_rss_gb": round(rss_gb(), 2), **info}
        log.info("== %s done in %.1fs (peak rss %.1f GB)", name, time.time() - t0, rss_gb())

    def save_state(self, status: str):
        info = {"build_id": self.build_id, "status": status, "layout": "cache_v2.1 stage (T1/T2/T2_work/T5/T6/archive)",
                "prev_build": str(self.prev.root), "prev_layout": self.prev.layout, "prev_asOf": str(self.prev_asof.date()),
                "asOf": str(self.asof.date()) if self.asof is not None else None, "observedAt": str(self.observed.date()),
                "steps_requested": self.args.steps, "dry_run": bool(self.args.dry_run), "full_lifecycle": bool(self.args.full_lifecycle),
                "refresh_dates": [str(d.date()) for d in self.refresh_dates], "n_ids": int(len(self.ids)) if self.ids is not None else None,
                "new_ids": self.new_ids, "refetched_ids": sorted(self.refetched_ids), "timings": self.timings, "results": self.results,
                "failed": self.failed, "written_at": now_str(), "peak_rss_gb": round(rss_gb(), 2),
                "aux_ids": int(len(self.aux_ids)), "rc2f": pqa.to_native(self.rc2f)}
        self.out.build_json.write_text(json.dumps(info, indent=1, default=str))
        self.write_apply_log()

    def write_apply_log(self):
        """rc2f/rc2f_apply_log.csv + rc2f_apply_info.json of the day (publish copies them to qa/rc2f)."""
        try:
            lg = self.alog.frame()
            self.out.rc2f.mkdir(parents=True, exist_ok=True)
            lg.to_csv(self.out.rc2f / "rc2f_apply_log.csv", index=False)
            info = {"build_id": self.build_id, "asOf": str(self.asof.date()) if self.asof is not None else None, "overrides_files": self.ovr_files,
                    "counts": {k: int(len(v)) for k, v in self.ovr.items()}, "apply_log": {"rows": int(len(lg)), "status": lg.status.value_counts().to_dict() if len(lg) else {}},
                    "compare_with_prev": self.ovr_cmp, "daily": True, "rc2_rules": False, "at": now_str()}
            (self.out.rc2f / "rc2f_apply_info.json").write_text(json.dumps(pqa.to_native(info), indent=1, default=str))
        except Exception as exc:  # noqa: BLE001
            log.warning("apply log not written: %s", exc)


# ======================================================================================
# step 0: prepare
# ======================================================================================
def prepare(ctx: Ctx):
    t0 = time.time()
    out, prev = ctx.out, ctx.prev
    out.mkdirs()
    point_master_modules(out)
    ctx.build_id = f"v21_{ctx.observed:%Y%m%d}_{dt.datetime.now(dt.timezone.utc):%H%M%S}Z"
    inst = pd.read_parquet(prev.master_file("instruments"), columns=["tradingItemId"])
    ctx.ids = np.sort(inst.tradingItemId.to_numpy(np.int64))
    # carry-forward: master files (steps may overwrite), archive tree, qa lists
    steps = ctx.args.steps
    for name in ("instruments", "current", "classification_history", "primary_history", "id_mapping", "identifier_history",
                 "lifecycle", "lineage", "tradable_exclusions", "lifecycle_dates", "aux_instruments", "lifecycle_aux"):
        copy_file(prev.master_file(name), out.master_file(name))
    for f in (prev.split, prev.dividend):
        copy_file(f, out.t2 / "factors" / f.name)
    # rc2f (D2): auxiliary instruments = master/aux_instruments (priceFetch usd) + lifecycle rows with status aux
    aux = set()
    if prev.master_file("aux_instruments").exists():
        a = pd.read_parquet(prev.master_file("aux_instruments"))
        aux |= set(int(i) for i in a.loc[a.priceFetch.astype(str) == "usd", "tradingItemId"]) if "priceFetch" in a.columns else set(int(i) for i in a.tradingItemId)
    if prev.master_file("lifecycle").exists():
        lc0 = pd.read_parquet(prev.master_file("lifecycle"), columns=["tradingItemId", "status"])
        aux |= set(int(i) for i in lc0.loc[lc0.status == "aux", "tradingItemId"])
    aux -= set(int(i) for i in ctx.ids)
    ctx.aux_ids = np.array(sorted(aux), dtype=np.int64)
    # overrides (price/split/lineage/lifecycle/aux re-applied by the U-steps; ledger overrides -> ledger builder)
    led_ids = []
    if prev.ledger.exists():
        led_ids = pq.read_table(prev.ledger, columns=["tradingItemId"]).column(0).unique().to_pylist()
    ctx.ovr, ctx.ovr_files, ctx.ovr_issues = dr.load_overrides(ctx.args.overrides_glob, ctx.price_ids(), led_ids)
    ctx.price_win = dr.price_windows(ctx.ovr.get("price"))
    (out.rc2f / "overrides").mkdir(parents=True, exist_ok=True)
    for f in ctx.ovr_files:
        pth = Path(f["path"])
        shutil.copy2(pth, out.rc2f / "overrides" / (pth.parent.name + "__" + pth.name))
    ctx.prev_alog = dr.prev_apply_log(prev.root, prev.layout)
    if len(ctx.ovr.get("aux", pd.DataFrame())) and not ctx.args.dry_run:
        prev_led = pd.read_parquet(prev.ledger) if prev.ledger.exists() else None
        ctx.rc2f["aux_additions"] = dr.reapply_aux_additions(ctx, ctx.ovr["aux"], ctx.alog, prev_led)
        if ctx.rc2f["aux_additions"].get("new"):
            ctx.aux_new_ids = list(ctx.rc2f["aux_additions"].get("new_ids", []))
            log.info("aux additions: %s", ctx.rc2f["aux_additions"])
    if ctx.ovr_issues:
        ctx.warn(f"override CSV issues: {len(ctx.ovr_issues)} (first: {ctx.ovr_issues[0]})")
    # previous events -> U4 lifecycle marker source (T6 rewrites the file in U5); previous ledger files -> carried unless the builder runs
    if "U5" in steps or "U4" in steps:
        copy_file(prev.events, out.events)
    for p in sorted(prev.ledger.parent.glob("ca_ledger*")) if prev.ledger.parent.exists() else []:
        if p.is_file():
            copy_file(p, out.t6 / "events" / p.name)
    ctx.rc2f["prepare"] = {"aux_ids": int(len(ctx.aux_ids)), "override_files": len(ctx.ovr_files), "override_rows": {k: int(len(v)) for k, v in ctx.ovr.items()},
                           "prev_apply_log_rows": int(len(ctx.prev_alog)) if ctx.prev_alog is not None else None, "price_windows": int(len(ctx.price_win))}
    if "U2" not in steps and "U3" not in steps:
        copy_file(prev.base_data, out.base_data)
        copy_file(prev.sidecar, out.sidecar)
        link_side_publish_name(out)
    if "U2" not in steps and prev.calendar.exists():
        copy_file(prev.calendar, out.calendar)
    if "U5" not in steps:
        copy_file(prev.returns, out.returns)
    copy_file(prev.raw_splitcache, out.raw_splitcache)
    copy_file(prev.raw_divadj, out.raw_divadj)
    copy_tree(prev.archive, out.archive)
    for p in prev.known_artifacts:
        if p.exists():
            copy_file(p, out.t2_qa / "factor_artifacts.csv")
            break
    for name in ("ciq_agg", "mi_agg", "lineage_overlap"):
        copy_file(prev.t1_work / f"{name}.parquet", out.t1_work / f"{name}.parquet")
    if ctx.args.prev_work:                       # published previous build: work files of the stage it was published from
        for name in ("calendar", "lineage_overlap", "identity", "ciq_agg", "mi_agg", "candidate_negatives"):
            if not (out.t1_work / f"{name}.parquet").exists():
                copy_file(Path(ctx.args.prev_work) / f"{name}.parquet", out.t1_work / f"{name}.parquet")
    for name in ("candidate_negatives",):
        copy_file(prev.t1_work / f"{name}.parquet", out.t1_work / f"{name}.parquet")
    # calendar: previous file + DB extension over the last 60 days (index scans on a few long-lived ids)
    cal_prev = None
    cal_src = next((p for p in (prev.t1_work / "calendar.parquet", out.t1_work / "calendar.parquet") if p.exists()), None)   # out: --prev-work copy
    if cal_src is not None:
        cal_prev = pd.read_parquet(cal_src).date.to_numpy(dtype="datetime64[ns]")
    elif prev.calendar.exists():                     # published build: its close-count calendar (trading days, 2000+) (R-D2 P2-6)
        c = pd.read_parquet(prev.calendar)
        cal_prev = pd.to_datetime(c.loc[c.isTradingDay, "date"]).to_numpy(dtype="datetime64[ns]")
        ctx.warn("previous build has no T1/_work/calendar.parquet: trading calendar from its calendar.parquet (2000+ only; pass --prev-work for the 1968+ list)")
    cur_prev = pd.read_parquet(prev.master_file("current"), columns=["tradingItemId", "symbol"])
    cal_ids = [int(i) for i in cur_prev.loc[cur_prev.symbol.isin(mb.CALENDAR_SYMBOLS), "tradingItemId"]]
    if ctx.args.offline:
        cal_new = np.array([], dtype="datetime64[ns]")
    else:
        since = (ctx.prev_asof - pd.Timedelta(days=60)).date()
        df = ctx.dbc().query(f"select distinct pricingdate from {SCHEMA}.ciqpriceequity where tradingitemid = any(%s) "
                             f"and priceclose is not null and pricingdate >= %s order by 1", (cal_ids, since))
        cal_new = to_ns(df.pricingdate)
    if cal_prev is None:
        pf = pq.ParquetFile(prev.base_data)
        ds = set()
        for rg in range(pf.num_row_groups):
            ds.update(np.unique(pf.read_row_group(rg, columns=["date"])["date"].to_numpy()).tolist())
        cal_prev = np.array(sorted(ds), dtype="datetime64[ns]")
        ctx.warn("previous build has no T1/_work/calendar.parquet: trading calendar rebuilt from base_data dates (2000+ only)")
    ctx.calendar = np.unique(np.concatenate([cal_prev, cal_new]))
    pd.DataFrame({"date": ctx.calendar}).to_parquet(out.t1_work / "calendar.parquet", index=False)
    # symbols side file for the prices_* CLIs (ids only)
    ctx.time_step("prepare", t0, n_ids=int(len(ctx.ids)), aux_ids=int(len(ctx.aux_ids)), prev_layout=prev.layout, calendar_days=int(len(ctx.calendar)))


# ======================================================================================
# U1 master delta
# ======================================================================================
def u1_master(ctx: Ctx):
    t0 = time.time()
    db, out, prev = ctx.dbc(), ctx.out, ctx.prev
    observed = ctx.observed
    res = {}
    # ---- 1. symbol rule -> new candidates
    v1 = set(db.query(pb.SQL_V1_RULE, (pb.COMMON_STOCK, pb.V1_EXCHANGES)).tradingitemid.astype(int))
    ever = set(db.query(pb.SQL_EVER_MAIN, (pb.USD, pb.COMMON_STOCK, pb.MAIN_EXCHG)).tradingitemid.dropna().astype(int))
    rule = v1 | ever
    prev_ids = set(int(i) for i in ctx.ids)
    neg_path = out.t1_work / "candidate_negatives.parquet"
    negatives = pd.read_parquet(neg_path) if neg_path.exists() else None
    if negatives is None and (prev.t1_work / "ciq_agg.parquet").exists():
        agg = pd.read_parquet(prev.t1_work / "ciq_agg.parquet", columns=["tradingItemId", "n_close_2000", "queriedAt"])
        negatives = agg[(agg.n_close_2000 == 0) & ~agg.tradingItemId.isin(list(prev_ids))][["tradingItemId"]].copy()
        negatives["checkedAt"] = pd.to_datetime(agg.queriedAt.max())
        ctx.warn(f"candidate_negatives seeded from the previous build's ciq_agg ({len(negatives)} rule candidates without a 2000+ close)")
    neg_ids = set(int(i) for i in negatives.tradingItemId) if negatives is not None else set()
    to_check = sorted(rule - prev_ids - (set() if ctx.args.full_lifecycle else neg_ids))
    res["rule_v1"], res["rule_ever_main"], res["rule_union"] = len(v1), len(ever), len(rule)
    res["candidates_not_in_set"], res["known_negatives"], res["checked_now"] = len(rule - prev_ids), len(neg_ids), len(to_check)
    new_ids, neg_rows = [], []
    if to_check:
        wait_for_quiet(db, max_wait_s=ctx.args.db_wait_max)
        for chunk in chunked(to_check, 1000):
            a = db.query(ml.CIQ_SQL, (chunk,))
            has = set(a.loc[a.n_close_2000 > 0, "tradingitemid"].astype(int))
            new_ids += sorted(has)
            neg_rows += [i for i in chunk if i not in has]
    # negatives: ids re-checked today get today's date, the others keep their previous checkedAt
    old_checked = dict(zip(negatives.tradingItemId.astype(int), pd.to_datetime(negatives.checkedAt))) if negatives is not None and len(negatives) else {}
    checked_now = set(neg_rows)
    all_neg = sorted((neg_ids | checked_now) - set(new_ids))
    negatives_new = pd.DataFrame({"tradingItemId": np.array(all_neg, dtype=np.int64),
                                  "checkedAt": [pd.Timestamp(observed) if i in checked_now else old_checked.get(i, pd.Timestamp(observed)) for i in all_neg]})
    negatives_new["checkedAt"] = pd.to_datetime(negatives_new.checkedAt).astype("datetime64[ns]")
    write_parquet_atomic(negatives_new, neg_path)
    ctx.new_ids = [int(i) for i in new_ids]
    res["new_ids"] = ctx.new_ids
    ids_all = np.sort(np.array(sorted(prev_ids | set(ctx.new_ids)), dtype=np.int64))
    ctx.ids = ids_all
    # ---- 2. new ids: full price history (once) + aggregates
    if ctx.new_ids:
        wait_for_quiet(db, max_wait_s=ctx.args.db_wait_max)
        parts = [pb.fetch_ciq(db, chunk, pb.START) for chunk in chunked(ctx.new_ids, 1000)]
        t_new = pa.concat_tables(parts)
        base, side = pb.transform_chunk(t_new, str(ctx.asof.date()) if ctx.asof is not None else None)
        ctx.pend.add_full(base, side, ctx.new_ids)
        ciq_agg_new = pd.concat([db.query(ml.CIQ_SQL, (chunk,)) for chunk in chunked(ctx.new_ids, 1000)], ignore_index=True)
        mi_agg_new = pd.concat([db.query(ml.MI_SQL, (chunk,)) for chunk in chunked(ctx.new_ids, 1000)], ignore_index=True)
        for name, a in (("ciq_agg", ciq_agg_new), ("mi_agg", mi_agg_new)):
            a = a.rename(columns={"tradingitemid": "tradingItemId"})
            for c in ("first_close", "last_close", "first_row", "last_row"):
                a[c] = pd.to_datetime(a[c]).astype("datetime64[ns]")
            a["queriedAt"] = pd.Timestamp(now_str())
            missing = [i for i in ctx.new_ids if i not in set(a.tradingItemId.astype(int))]
            if missing:
                a = pd.concat([a, pd.DataFrame({"tradingItemId": missing, "first_close": pd.NaT, "last_close": pd.NaT, "first_row": pd.NaT,
                                                "last_row": pd.NaT, "n_rows": 0, "n_close": 0, "n_close_2000": 0, "queriedAt": pd.Timestamp(now_str())})])
            p = out.t1_work / f"{name}.parquet"
            if p.exists():
                old = pd.read_parquet(p)
                a = pd.concat([old[~old.tradingItemId.isin(a.tradingItemId)], a[AGG_COLS]], ignore_index=True).sort_values("tradingItemId")
            write_parquet_atomic(a[AGG_COLS].reset_index(drop=True), p)
        res["new_ids_rows"] = int(base.num_rows)
        log.info("U1: %d new ids -> %d price rows appended (full history)", len(ctx.new_ids), base.num_rows)
    # ---- 3. identity for all ids (fresh) -> ident frame in the build_cache_v2 column style
    ident = mb.identity_for(db, ids_all.tolist())
    ident = ident.sort_values("tradingitemid").reset_index(drop=True)
    if ident.tradingitemid.duplicated().any():
        raise RuntimeError("identity has duplicate tradingItemIds")
    missing_ident = np.setdiff1d(ids_all, ident.tradingitemid.to_numpy(np.int64))
    if len(missing_ident):
        ctx.warn(f"{len(missing_ident)} ids have no ciqtradingitem row any more (kept with previous identity): {missing_ident[:5].tolist()}")
        prev_ident = pd.read_parquet(prev.t1_work / "identity.parquet") if (prev.t1_work / "identity.parquet").exists() else None
        if prev_ident is not None:
            ident = pd.concat([ident, prev_ident[prev_ident.tradingitemid.isin(missing_ident)]], ignore_index=True).sort_values("tradingitemid")
    prev_inst = pd.read_parquet(prev.master_file("instruments")).set_index("tradingItemId")
    ident["inv1rule"] = ident.tradingitemid.isin(list(v1))
    ident["evermainexchange"] = ident.tradingitemid.isin(list(ever))
    ident["evermaincommonusd"] = ident["evermainexchange"]
    ident["inv1cache"] = ident.tradingitemid.map(prev_inst.inV1Cache).fillna(False).astype(bool)
    name = ident.companyname.fillna("")
    ident["currentprimary"] = (ident.primaryflag == 1) & (ident.primaryflagti == 1) & ~name.str.contains(mb.PRIMARY_NAME_PATTERN, regex=True)
    for c in ("exchangehistory", "gvkey", "iid"):
        ident[c] = None
    ident.to_parquet(out.t1_work / "identity.parquet", **mb.PARQUET_KW)
    ctx.ident = ident
    # ---- 4. id_mapping + identifier_history (full refresh from the small tables)
    for name in ("id_mapping", "identifier_history"):
        (out.master_file(name)).unlink(missing_ok=True)
    for f in ("ciqgvkeyiid_all.parquet", "sec_idhist_tic_exchg_isin.parquet"):
        (out.t1_work / f).unlink(missing_ok=True)
    idm = mh.step_id_mapping(db, ids_all)
    ih = mh.step_identifier_history(db, idm)
    ctx.id_mapping = idm
    for name, new_df, keys in (("id_mapping", idm, ["tradingItemId", "gvkey", "iid", "symbolStartDate", "symbolEndDate"]),
                               ("identifier_history", ih, ["tradingItemId", "gvkey", "iid", "item", "value", "effFrom", "effThru"])):
        if prev.master_file(name).exists():
            old = pd.read_parquet(prev.master_file(name))
            a = old[keys].astype(str).agg("|".join, axis=1)
            b = new_df[keys].astype(str).agg("|".join, axis=1)
            added, removed = int((~b.isin(set(a))).sum()), int((~a.isin(set(b))).sum())
            res[f"{name}_rows_added"], res[f"{name}_rows_removed"] = added, removed
            if added or removed:
                ctx.master_revisions.append({"table": name, "tradingItemId": -1, "field": "rows", "old": str(len(old)), "new": str(len(new_df)),
                                             "observedAt": observed})
    # ---- 5. current (fresh) and its diff against the previous current
    exch = mb.fetch_exchanges(db)
    cur = mb.build_current(ident, exch, idm, observed)
    prev_cur = pd.read_parquet(prev.master_file("current")).set_index("tradingItemId")
    k = cur.set_index("tradingItemId")
    common = k.index.intersection(prev_cur.index)
    n_changed_ids = set()
    for f in CURRENT_FIELDS:
        a = prev_cur.loc[common, f]
        b = k.loc[common, f]
        if str(a.dtype).startswith("datetime") or str(b.dtype).startswith("datetime"):
            a2, b2 = pd.to_datetime(a), pd.to_datetime(b)
            diff = ~((a2.isna() & b2.isna()) | (a2 == b2))
        else:
            sa, sb = a.astype(object).where(a.notna(), None), b.astype(object).where(b.notna(), None)
            diff = pd.Series([not ((x is None and y is None) or (x is not None and y is not None and str(x) == str(y))) for x, y in zip(sa, sb)], index=common)
        for tid in common[diff.to_numpy()]:
            ctx.master_revisions.append({"table": "current", "tradingItemId": int(tid), "field": f, "old": str(prev_cur.at[tid, f]), "new": str(k.at[tid, f]),
                                         "observedAt": observed})
            n_changed_ids.add(int(tid))
    res["current_changed_ids"] = len(n_changed_ids)
    res["current_changes_by_field"] = pd.Series([r["field"] for r in ctx.master_revisions if r["table"] == "current"]).value_counts().to_dict() if ctx.master_revisions else {}
    mb.write_parquet(cur, out.master_file("current"))
    ctx.cur = cur
    # ---- 6. instruments: previous rows refreshed + new rows
    inst = prev_inst.copy()
    ki = ident.set_index("tradingitemid")
    col_map = {"securityId": "securityid", "companyId": "companyid", "companyName": "companyname", "securitySubTypeId": "securitysubtypeid",
               "tiCurrencyId": "ticurrencyid"}
    for f in INSTRUMENT_REFRESH:
        src = ki[col_map[f]].reindex(inst.index)
        old = inst[f]
        if f in ("companyName",):
            diff = (old.astype(object).where(old.notna(), None) != src.astype(object).where(src.notna(), None)) & src.notna()
        else:
            diff = old.astype("Int64") != src.astype("Int64")
            diff &= src.notna()
        for tid in inst.index[diff.fillna(False).to_numpy()]:
            ctx.master_revisions.append({"table": "instruments", "tradingItemId": int(tid), "field": f, "old": str(old.at[tid]), "new": str(src.at[tid]),
                                         "observedAt": observed})
        if f == "companyName":
            inst.loc[diff.fillna(False).to_numpy(), f] = src[diff.fillna(False).to_numpy()]
        else:
            inst.loc[diff.fillna(False).to_numpy(), f] = src[diff.fillna(False).to_numpy()].astype("Int64")
    if ctx.new_ids:
        kn = ki.reindex(ctx.new_ids)
        add = pd.DataFrame({"securityId": kn.securityid.astype("int64").to_numpy(), "companyId": kn.companyid.astype("int64").to_numpy(),
                            "companyName": kn.companyname.astype(object).where(kn.companyname.notna(), None).to_numpy(),
                            "securitySubTypeId": kn.securitysubtypeid.astype("Int64").to_numpy(), "tiCurrencyId": kn.ticurrencyid.astype("Int64").to_numpy(),
                            "inV1Rule": kn.inv1rule.astype(bool).to_numpy(), "everMainExchange": kn.evermainexchange.astype(bool).to_numpy(),
                            "inV1Cache": False, "inProtoV2": False, "addedOn": pd.Timestamp(observed)}, index=pd.Index(ctx.new_ids, name="tradingItemId"))
        inst = pd.concat([inst, add])
    inst = inst.reset_index().sort_values("tradingItemId").reset_index(drop=True)
    inst["securityId"] = inst.securityId.astype("int64")
    inst["companyId"] = inst.companyId.astype("int64")
    inst["securitySubTypeId"] = inst.securitySubTypeId.astype("Int64")
    inst["tiCurrencyId"] = inst.tiCurrencyId.astype("Int64")
    inst["addedOn"] = pd.to_datetime(inst.addedOn).astype("datetime64[ns]")
    mb.write_parquet(inst, out.master_file("instruments"))
    # ---- 7. classification_history / primary_history: observed rows for changed values
    for name, cols in (("classification_history", CLASS_COLS), ("primary_history", PRIM_COLS)):
        hist = pd.read_parquet(prev.master_file(name))
        hist, n_app, n_new = append_history(hist, cur, cols, ids_all, observed)
        res[f"{name}_appended"], res[f"{name}_new_ids"] = n_app, n_new
        mb.write_parquet(hist, out.master_file(name))
    # ---- 8. snapshot + master revisions
    snap = out.archive / "master_snapshots" / f"date={observed:%Y-%m-%d}"
    snap.mkdir(parents=True, exist_ok=True)
    write_parquet_atomic(cur, snap / "current.parquet")
    rev = pd.DataFrame(ctx.master_revisions, columns=REV_MASTER_COLS)
    rev["tradingItemId"] = rev.tradingItemId.astype(np.int64)
    rev["observedAt"] = pd.to_datetime(rev.observedAt).astype("datetime64[ns]")
    write_parquet_atomic(rev, out.revisions_dir(observed) / "master_revisions.parquet")
    res["master_revisions_rows"] = int(len(rev))
    ctx.results["steps"]["U1"] = res
    ctx.time_step("U1", t0, new_ids=len(ctx.new_ids), current_changed_ids=len(n_changed_ids), master_revisions=int(len(rev)))


def append_history(hist: pd.DataFrame, cur: pd.DataFrame, cols: list[str], ids: np.ndarray, observed: pd.Timestamp) -> tuple[pd.DataFrame, int, int]:
    """Close the open interval and append a 'db_current' observed row for ids whose current value tuple differs
    from the open interval's; ids without any row get a 'current_only' row.  Same-day re-run: values updated in place."""
    h = hist.copy()
    k = cur.set_index("tradingItemId")[cols]
    open_rows = h[h.effectiveTo.isna()].sort_values("effectiveFrom").groupby("tradingItemId").tail(1)
    last = open_rows.set_index("tradingItemId")
    rows, n_app, n_new = [], 0, 0

    def same(a, b) -> bool:
        return all((pd.isna(x) and pd.isna(y)) or (not pd.isna(x) and not pd.isna(y) and str(x) == str(y)) for x, y in zip(a, b))

    have = set(h.tradingItemId.unique())
    for tid in ids:
        tid = int(tid)
        if tid not in k.index:
            continue
        vals = k.loc[tid, cols]
        if tid not in have:
            rows.append({"tradingItemId": tid, **{c: vals[c] for c in cols}, "effectiveFrom": observed, "effectiveTo": pd.NaT, "observedAt": observed,
                         "source": "db_current", "historyQuality": "current_only"})
            n_new += 1
            continue
        if tid not in last.index:
            # no open interval (history ended) -> reopen with the current value
            rows.append({"tradingItemId": tid, **{c: vals[c] for c in cols}, "effectiveFrom": observed, "effectiveTo": pd.NaT, "observedAt": observed,
                         "source": "db_current", "historyQuality": "observed"})
            n_app += 1
            continue
        old = last.loc[tid, cols]
        if same(old, vals):
            continue
        idx = h.index[(h.tradingItemId == tid) & h.effectiveTo.isna()]
        if pd.Timestamp(last.at[tid, "effectiveFrom"]) >= observed:
            for c in cols:
                h.loc[idx, c] = vals[c]
            h.loc[idx, "observedAt"] = observed
            continue
        h.loc[idx, "effectiveTo"] = observed - pd.Timedelta(days=1)
        rows.append({"tradingItemId": tid, **{c: vals[c] for c in cols}, "effectiveFrom": observed, "effectiveTo": pd.NaT, "observedAt": observed,
                     "source": "db_current", "historyQuality": "observed"})
        n_app += 1
    if rows:
        add = pd.DataFrame(rows)
        for c in cols:
            if str(h[c].dtype) == "Int64":
                add[c] = pd.to_numeric(add[c], errors="coerce").astype("Int64")
            elif str(h[c].dtype) == "boolean":
                add[c] = add[c].map(lambda x: None if pd.isna(x) else bool(x)).astype("boolean")
        h = pd.concat([h, add], ignore_index=True)
    for c in ("effectiveFrom", "effectiveTo", "observedAt"):
        h[c] = pd.to_datetime(h[c]).astype("datetime64[ns]")
    h["tradingItemId"] = h.tradingItemId.astype(np.int64)
    return h.sort_values(["tradingItemId", "effectiveFrom"]).reset_index(drop=True), n_app, n_new


# ======================================================================================
# U2 raw append
# ======================================================================================
def u2_raw(ctx: Ctx):
    t0 = time.time()
    db, out, prev, args = ctx.dbc(), ctx.out, ctx.prev, ctx.args
    res = {}
    explicit = ts(args.asof) if args.asof and args.asof != "auto" else None
    upto = explicit if explicit is not None else last_closed_session()
    # window start: `refresh_days` trading days before the upper bound on the stored calendar (+ margin)
    cal = ctx.calendar
    i_up = int(np.searchsorted(cal, np.datetime64(upto, "ns"), side="right"))
    i_lo = max(i_up - args.refresh_days - 3, 0)          # 3 extra days: calendar may lack the newest dates
    fetch_from = min(pd.Timestamp(cal[i_lo]), ctx.prev_asof - pd.Timedelta(days=10))
    ids = [int(i) for i in ctx.price_ids() if int(i) not in set(ctx.new_ids)]        # instruments + aux (rc2f: aux rows live in base_data)
    wait_for_quiet(db, max_wait_s=args.db_wait_max)
    t1 = time.time()
    ciq_parts = [pb.fetch_ciq(db, chunk, str(fetch_from.date()), str(upto.date())) for chunk in chunked(ids, 1000)]
    ciq = pa.concat_tables(ciq_parts)
    res["seconds_fetch_ciq"], res["rows_fetched_ciq"] = round(time.time() - t1, 1), ciq.num_rows
    t1 = time.time()
    mi_parts = [pb.fetch_mi(db, chunk, str(fetch_from.date()), str(upto.date())) for chunk in chunked(ids, 1000)]
    mi = pa.concat_tables(mi_parts).to_pandas()
    mi["date"] = pd.to_datetime(mi.pricedate).astype("datetime64[ns]")
    ctx.win_mi = mi                       # U4 updates the miprice aggregates (last close) from these rows
    res["seconds_fetch_mi"], res["rows_fetched_mi"] = round(time.time() - t1, 1), int(len(mi))
    # ---- counts per date -> calendar (publish rule: close count >= 20% of the centered 41-day rolling median), asOf
    cdf = ciq.select(["pricingdate", "priceclose"]).to_pandas()
    cdf["date"] = pd.to_datetime(cdf.pricingdate).astype("datetime64[ns]")
    ciq_close = cdf[cdf.priceclose.notna()].groupby("date").size()
    mi_close = mi[mi.priceclose.notna()].groupby("date").size()
    prev_cal = load_prev_calendar(prev)
    # fetched counts replace the stored counts inside the fetched range (new ids are not in the fetch: only when they exist)
    keep_prev = prev_cal[(prev_cal.date < fetch_from) | (prev_cal.date > upto)]
    n_all = pd.concat([pd.Series(keep_prev.nClose.to_numpy(), index=pd.DatetimeIndex(keep_prev.date)), ciq_close]).sort_index()
    n_all = n_all[~n_all.index.duplicated(keep="last")]
    cal = calendar_from_counts(n_all)
    tdays = pd.DatetimeIndex(cal.loc[cal.isTradingDay, "date"])
    off_cal = cal[(~cal.isTradingDay) & (cal.date >= fetch_from) & (cal.date <= upto)]
    res["off_calendar_dates"] = [{"date": str(pd.Timestamp(r.date).date()), "nClose": int(r.nClose), "rollingMedian": float(r.rollingMedian)} for r in off_cal.itertuples()]
    ctx.calendar_frame = cal
    pick = pick_asof(ciq_close, mi_close, tdays, upto, ctx.prev_asof, explicit)
    res["asof_pick"] = {"mode": pick["mode"], "upto": str(upto.date()), "fetch_from": str(fetch_from.date()),
                        "checks": {k: v for k, v in pick["checks"].items() if pd.Timestamp(k) >= fetch_from}}
    if not pick["ok"] or pick["asOf"] is None:
        ctx.results["steps"]["U2"] = res
        raise UpdateAbort(4, f"asOf close-count threshold failed: {json.dumps(pick['checks'], default=str)}")
    asof = ts(pick["asOf"])
    if pick["mode"] == "auto" and asof < upto:
        msg = f"latest closed session {upto.date()} not complete yet (asOf would be {asof.date()})"
        if not args.allow_stale:
            ctx.results["steps"]["U2"] = res
            raise UpdateAbort(3, msg)
        ctx.warn(msg)
    ctx.asof = asof
    # refresh window = last `refresh_days` calendar trading days <= asOf
    all_td = tdays[tdays <= asof].to_numpy().astype("datetime64[ns]")
    ctx.refresh_dates = [pd.Timestamp(d) for d in all_td[-args.refresh_days:]]
    win_lo, win_hi = ctx.refresh_dates[0], asof
    res["refresh_dates"] = [str(d.date()) for d in ctx.refresh_dates]
    res["asOf"], res["prev_asOf"] = str(asof.date()), str(ctx.prev_asof.date())
    # ---- transform (raw) and restrict to the window; rows on off-calendar dates are not stored (logged as revisions)
    ciq_w = ciq.filter(pc.and_(pc.greater_equal(ciq["pricingdate"], pa.scalar(win_lo.to_pydatetime(), type=pa.timestamp("s"))),
                               pc.less_equal(ciq["pricingdate"], pa.scalar(win_hi.to_pydatetime(), type=pa.timestamp("s")))))
    base_t, side_t = pb.transform_chunk(ciq_w, str(asof.date()))
    new = base_t.to_pandas()
    side = side_t.to_pandas()
    off_dates = set(pd.DatetimeIndex(off_cal.date).to_numpy().astype("datetime64[ns]").tolist())
    off_mask = np.isin(to_ns(new.date), np.array(sorted(off_dates), dtype="datetime64[ns]")) if off_dates else np.zeros(len(new), dtype=bool)
    off_rows = new[off_mask]
    res["off_calendar_rows_dropped"] = int(off_mask.sum())
    if off_mask.any():
        ctx.price_revisions.append(finalize_revisions(pd.DataFrame({"tradingItemId": off_rows.tradingItemId.to_numpy(), "date": off_rows.date.to_numpy(), "field": "row",
                                                                    "old": np.nan, "new": off_rows.close.to_numpy(dtype=np.float64), "observedAt": ctx.observed,
                                                                    "reason": "off_calendar"})))
        new = new[~off_mask].reset_index(drop=True)
        side = side[~off_mask].reset_index(drop=True)
        log.info("U2: %d rows on %d off-calendar dates not stored (%s)", int(off_mask.sum()), len(off_dates), res["off_calendar_dates"])
    mi_w = mi[(mi.date >= win_lo) & (mi.date <= win_hi) & mi.priceclose.notna()]
    mi_map = dict(zip(row_keys(mi_w.tradingitemid.to_numpy(np.int64), mi_w.date.to_numpy()).tolist(), mi_w.priceclose.astype(float)))
    q = quality_flags(new.tradingItemId.to_numpy(), new.date.to_numpy(), new.close.to_numpy(dtype=np.float64), mi_map, asof)
    new["qualityFlag"] = q
    ctx.flag_map = dict(zip(row_keys(new.tradingItemId.to_numpy(), new.date.to_numpy()).tolist(), q.tolist()))
    # G-RAW (window): mi vs ciq closes
    kn = row_keys(new.tradingItemId.to_numpy(), new.date.to_numpy())
    mi_vals = np.array([mi_map.get(int(k), np.nan) for k in kn])
    cl = new.close.to_numpy(dtype=np.float64)
    both = np.isfinite(mi_vals) & np.isfinite(cl)
    with np.errstate(invalid="ignore", divide="ignore"):
        rel = np.abs(cl / mi_vals - 1.0)
    mism = both & (rel > MI_FLAG_TOL)
    mi_only = len(set(mi_map) - set(kn.tolist()))
    ctx.raw_gate = {"window": [str(win_lo.date()), str(win_hi.date())], "ciq_rows": int(len(new)), "ciq_close_rows": int(np.isfinite(cl).sum()),
                    "both_close": int(both.sum()), "mismatch_over_1e-3": int(mism.sum()),
                    "mismatch_rate": float(mism.sum() / max(both.sum(), 1)), "ciq_close_without_mi": int((np.isfinite(cl) & ~np.isfinite(mi_vals)).sum()),
                    "mi_close_without_ciq_row": int(mi_only), "asof_flags": pd.Series(q[new.date.to_numpy() == np.datetime64(asof, "ns")]).value_counts().to_dict(),
                    "flags_window": pd.Series(q).value_counts().to_dict(),
                    "mismatch_examples": [{"tradingItemId": int(new.tradingItemId.iloc[j]), "date": str(pd.Timestamp(new.date.iloc[j]).date()),
                                           "ciq_raw": float(cl[j]), "mi": float(mi_vals[j]), "rel": float(rel[j])} for j in np.flatnonzero(mism)[:20]]}
    # ---- compare with the stored rows inside the window
    prev_win = read_rows_filtered(prev.base_data, None, date_from=win_lo)
    prev_win = prev_win[prev_win.date <= win_hi]
    if ctx.new_ids:
        prev_win = prev_win[~prev_win.tradingItemId.isin(ctx.new_ids)]
    if off_dates:
        prev_off = prev_win[np.isin(to_ns(prev_win.date), np.array(sorted(off_dates), dtype="datetime64[ns]"))]
        if len(prev_off):
            ctx.price_revisions.append(finalize_revisions(pd.DataFrame({"tradingItemId": prev_off.tradingItemId.to_numpy(), "date": prev_off.date.to_numpy(), "field": "row",
                                                                        "old": prev_off.close.to_numpy(dtype=np.float64), "new": np.nan, "observedAt": ctx.observed,
                                                                        "reason": "off_calendar_removed"})))
            res["off_calendar_stored_rows_removed"] = int(len(prev_off))
        prev_win = prev_win[~np.isin(to_ns(prev_win.date), np.array(sorted(off_dates), dtype="datetime64[ns]"))]
    ctx.prev_win = prev_win
    rev, changed, cstats = compare_rows(prev_win, new, ctx.observed, prev_asof=ctx.prev_asof)
    aux_set = set(int(i) for i in ctx.aux_ids)
    if len(rev):
        rem = rev[rev.reason == "vendor_removed"]
        cstats["removed_rows_aux"] = int(rem.tradingItemId.isin(list(aux_set)).sum())
        cstats["removed_rows_instruments"] = int(len(rem) - cstats["removed_rows_aux"])
    else:
        cstats["removed_rows_aux"] = cstats["removed_rows_instruments"] = 0
    cstats["prev_rows_aux"] = int(prev_win.tradingItemId.isin(list(aux_set)).sum())
    cstats["prev_rows_instruments"] = int(len(prev_win) - cstats["prev_rows_aux"])
    res["compare"] = cstats
    # source: 2 where a value changed, else the stored source (1 for brand-new rows)
    kp = row_keys(prev_win.tradingItemId.to_numpy(), prev_win.date.to_numpy())
    pos = np.searchsorted(kp, kn)
    pos_c = np.minimum(pos, max(len(kp) - 1, 0))
    common = (len(kp) > 0) & (pos < len(kp))
    if len(kp):
        common &= kp[pos_c] == kn
    src = np.full(len(new), SOURCE_CIQ, dtype=np.int8)
    if len(kp):
        src[common] = prev_win.source.to_numpy(dtype=np.int8)[pos_c[common]]
    src[changed] = SOURCE_CORRECTED
    new["source"] = src
    # rc2f price overrides on the fresh rows of the window (new rows or changed values) inside an override window: rare (windows are historical)
    fresh = ~common | changed
    new, n_px = dr.apply_price_to_rows(new, ctx.price_win, fresh, ctx.alog)
    res["price_override_rows_applied"] = int(n_px)
    # fuse: too many stored rows vanished from the re-fetch (instruments; aux rows are counted apart and logged as vendor_removed revisions)
    frac_removed = cstats["removed_rows_instruments"] / max(cstats["prev_rows_instruments"], 1)
    res["removed_fraction"] = frac_removed
    res["removed_fraction_aux"] = cstats["removed_rows_aux"] / max(cstats["prev_rows_aux"], 1)
    if frac_removed > REMOVED_ROWS_MAX_FRAC and cstats["removed_rows_instruments"] > 50:
        ctx.results["steps"]["U2"] = res
        raise UpdateAbort(4, f"{cstats['removed_rows_instruments']} stored instrument rows ({frac_removed:.2%}) missing from the re-fetched window: refusing to drop them")
    if cstats["removed_rows_aux"]:
        ctx.warn(f"U2: {cstats['removed_rows_aux']} stored aux rows missing from the re-fetched window (dropped, logged as vendor_removed)")
    ctx.price_revisions.append(rev)
    ctx.win_base, ctx.win_side = new, side
    ctx.pend.add_window(base_from_pandas(new), side_from_pandas(side), win_lo, win_hi)
    # side-file rows of the window (for U3's restatement detector)
    ctx.prev_win_side = read_rows_filtered(prev.sidecar, None, date_from=win_lo) if prev.sidecar.exists() else pd.DataFrame(columns=["tradingItemId", "date", "adjustmentfactor", "ciqClose"])
    (out.t2_work / "asof.json").write_text(json.dumps({"asOf": str(asof.date()), "prev_asOf": str(ctx.prev_asof.date()), "refresh_dates": res["refresh_dates"],
                                                         "close_counts": pick["checks"], "off_calendar_dates": res["off_calendar_dates"]}, indent=1))
    cal_out = cal[cal.date <= asof].reset_index(drop=True)
    write_parquet_atomic(cal_out, out.calendar)
    res["calendar"] = {"dates": int(len(cal_out)), "trading_days": int(cal_out.isTradingDay.sum()), "off_calendar_total": int((~cal_out.isTradingDay).sum())}
    ctx.results["steps"]["U2"] = res
    log.info("U2: asOf %s, window %s..%s, fetched ciq %d / mi %d rows, stored %d, changed rows %d (cells %d), late %d, removed %d",
             asof.date(), win_lo.date(), win_hi.date(), ciq.num_rows, len(mi), cstats["prev_rows"], cstats["changed_rows"], cstats["changed_cells"],
             cstats["late_rows"], cstats["removed_rows"])
    ctx.time_step("U2", t0, rows_fetched=int(ciq.num_rows), changed_rows=cstats["changed_rows"], late_rows=cstats["late_rows"], removed_rows=cstats["removed_rows"])


# ======================================================================================
# U3 factors
# ======================================================================================
def u3_factors(ctx: Ctx):
    t0 = time.time()
    db, out, prev, args = ctx.dbc(), ctx.out, ctx.prev, ctx.args
    res = {}
    ids = ctx.price_ids()                      # instruments + aux (rc2f): every id with rows in base_data needs a covering interval (G-SPLIT lookup)
    snapshot = ctx.observed
    if ctx.asof is None:
        ctx.asof = ctx.prev_asof
    # ---- 1. vendor windows in full
    t1 = time.time()
    sc = pfm.fetch_split_windows(db, ids)
    dv = pfm.fetch_dividend_windows(db, ids)
    res["seconds_fetch_windows"] = round(time.time() - t1, 1)
    res["splitcache_rows"], res["divadj_rows"] = int(len(sc)), int(len(dv))
    prev_sc = pd.read_parquet(prev.raw_splitcache) if prev.raw_splitcache.exists() else None
    prev_dv = pd.read_parquet(prev.raw_divadj) if prev.raw_divadj.exists() else None
    sp0 = pfm.build_intervals(pfm.split_windows(sc), ids, pfm.SRC_SPLIT, snapshot, pre_first="pre")
    dd = pfm.build_intervals(pfm.dividend_windows(dv), ids, pfm.SRC_DIV, snapshot)
    lookup0 = pfm.FactorLookup(sp0)
    # ---- 2. restatement detection -> ids to re-fetch in full
    prev_split = pd.read_parquet(prev.split) if prev.split.exists() else None
    r_adjf = set(int(i) for i in prev_split.loc[prev_split.source == pfm.SRC_ADJF, "tradingItemId"].unique()) if prev_split is not None else set()
    r_sc = splitcache_changed_ids(prev_sc, sc)
    r_side, r_look = set(), set()
    if ctx.win_side is not None and len(ctx.win_side):
        m = adjf_mismatch_ids(ctx.win_side, getattr(ctx, "prev_win_side", pd.DataFrame(columns=["tradingItemId", "date", "adjustmentfactor"])), lookup0)
        r_side, r_look = m["vs_side"], m["vs_lookup"]
    if not prev.sidecar.exists():
        ctx.warn("previous build has no side file (adjf_long.parquet): re-fetching the full adjustmentfactor history for ALL ids")
        r_all = set(ids)
    elif args.full_refetch:
        ctx.warn("--full-refetch: full ciqpriceequity history re-fetched for ALL ids (weekly sweep: restatements outside the daily re-fetch set)")
        r_all = set(ids)
    else:
        r_all = set()
    # D3 (6): aux ids whose rows have no side-table row yet (the published rc2f2 side table was written before the aux merge) -> full fetch once
    r_aux_side = set()
    if not args.full_refetch and len(ctx.aux_ids) and prev.sidecar.exists():
        side_ids = set(pq.read_table(prev.sidecar, columns=["tradingItemId"]).column(0).unique().to_pylist())
        r_aux_side = set(int(i) for i in ctx.aux_ids if int(i) not in side_ids)
        if r_aux_side:
            ctx.warn(f"U3: {len(r_aux_side)} aux ids have no side-table rows in the previous build: full history re-fetched once (side table completed)")
    R = (r_adjf | r_sc | r_side | r_look | r_all | r_aux_side) & set(ids)
    R -= set(ctx.new_ids)
    res["refetch_ids"] = {"prev_adjf_source": len(r_adjf), "splitcache_changed": len(r_sc), "window_adjf_vs_side": len(r_side),
                          "window_adjf_vs_lookup": len(r_look), "aux_without_side_rows": len(r_aux_side), "full_refetch": bool(args.full_refetch), "total": len(R),
                          "splitcache_changed_ids": sorted(r_sc)[:50], "window_adjf_vs_side_ids": sorted(r_side)[:50]}
    ctx.refetched_ids = set(R)
    inv_stats = {"violations": 0}
    if R:
        wait_for_quiet(db, max_wait_s=args.db_wait_max)
        t1 = time.time()
        parts = [pb.fetch_ciq(db, chunk, pb.START, str(ctx.asof.date())) for chunk in chunked(sorted(R), 1000)]
        full = pa.concat_tables(parts)
        res["seconds_refetch_full"], res["rows_refetch_full"] = round(time.time() - t1, 1), int(full.num_rows)
        fb, fs = pb.transform_chunk(full, str(ctx.asof.date()))
        fbd, fsd = fb.to_pandas(), fs.to_pandas()
        stored = read_rows_filtered(prev.base_data, None, ids=np.array(sorted(R), dtype=np.int64))
        stored = stored[stored.date <= ctx.asof]
        # rc2f: rows inside a price override window hold corrected (not vendor) values -> excluded from the invariance check, kept as stored
        in_win = dr.rows_in_windows(stored.tradingItemId.to_numpy(), stored.date.to_numpy(), ctx.price_win)
        viol, inv_stats = raw_invariance(stored[~in_win], fbd, fsd)
        inv_stats["rows_in_price_override_windows_skipped"] = int(in_win.sum())
        res["raw_invariance"] = inv_stats
        accept = set(int(x) for x in (args.accept_raw_change or []))
        acc_keys = np.zeros(0, dtype=np.int64)
        if len(viol):
            viol.to_csv(out.t2_qa / "raw_invariance_violations.csv", index=False)
            with np.errstate(invalid="ignore", divide="ignore"):
                viol["ratio"] = viol.refetched / viol.stored
            by_id = viol.groupby("tradingItemId").agg(cells=("field", "size"), fields=("field", lambda s_: ",".join(sorted(set(s_)))), dateMin=("date", "min"), dateMax=("date", "max"),
                                                      ratioMin=("ratio", "min"), ratioMax=("ratio", "max")).reset_index()
            by_id["policy"] = np.where(by_id.tradingItemId.isin(list(accept)), "accepted(--accept-raw-change)", "kept_stored" if args.raw_change_policy == "keep" else "abort")
            by_id["vwapOnly"] = by_id.fields == "vwap"
            inv_stats["by_id"] = pqa.to_native(by_id.to_dict("records"))
            inv_stats["ids_vwap_only"] = int(by_id.vwapOnly.sum())
            v_keep = viol[~viol.tradingItemId.isin(list(accept))]
            v_acc = viol[viol.tradingItemId.isin(list(accept))]
            if args.raw_change_policy == "abort" and len(v_keep):
                ctx.results["steps"]["U3"] = res
                raise UpdateAbort(5, f"raw not invariant for {v_keep.tradingItemId.nunique()} re-fetched ids ({len(v_keep)} cells, e.g. {v_keep.head(3).to_dict('records')})")
            if len(v_keep):
                # rc2f daily rule: a vendor restatement of the raw history (new split -> restated adjustmentfactor; precision-floor unit artifacts,
                # vwap rounding) is never adopted silently: the stored values stay, the vendor's values are logged (reason vendor_restated_kept,
                # old = stored value kept, new = vendor value) and the ids are listed for review (G-RAWINV; --accept-raw-change <ids> adopts them)
                ctx.warn(f"U3: raw not invariant for {v_keep.tradingItemId.nunique()} re-fetched ids ({len(v_keep)} cells; vwap-only ids {int(by_id[~by_id.tradingItemId.isin(list(accept))].vwapOnly.sum())}): "
                         f"stored values kept, vendor values logged as vendor_restated_kept (T2/qa/raw_invariance_violations.csv)")
                ctx.price_revisions.append(finalize_revisions(pd.DataFrame({"tradingItemId": v_keep.tradingItemId.to_numpy(), "date": v_keep.date.to_numpy(), "field": v_keep.field.to_numpy(),
                                                                            "old": v_keep.stored.to_numpy(dtype=np.float64), "new": v_keep.refetched.to_numpy(dtype=np.float64),
                                                                            "observedAt": ctx.observed, "reason": "vendor_restated_kept"})))
            if len(v_acc):
                ctx.warn(f"U3: vendor restatement adopted for {sorted(set(v_acc.tradingItemId.astype(int)))} ({len(v_acc)} cells, --accept-raw-change)")
                ctx.price_revisions.append(finalize_revisions(pd.DataFrame({"tradingItemId": v_acc.tradingItemId.to_numpy(), "date": v_acc.date.to_numpy(), "field": v_acc.field.to_numpy(),
                                                                            "old": v_acc.stored.to_numpy(dtype=np.float64), "new": v_acc.refetched.to_numpy(dtype=np.float64),
                                                                            "observedAt": ctx.observed, "reason": "vendor_restated_accepted"})))
                acc_keys = np.unique(row_keys(v_acc.tradingItemId.to_numpy(), v_acc.date.to_numpy()))
            ctx.raw_inv_by_id = by_id
            # P0-1 (R-D2): a kept restatement keeps raw AND factors on one basis: the previous build's intervals are carried for the ids whose
            # OHLC raw was kept (vwap-only drift keeps the vendor factors: the raw OHLC is invariant); vendor boundaries after the previous asOf
            # are appended (a new split is not lost); the side table rows of those ids follow the carried intervals
            kept_ohlc = set(int(t) for t, pol, vw in zip(by_id.tradingItemId, by_id.policy, by_id.vwapOnly) if pol == "kept_stored" and not vw)
            if kept_ohlc:
                carried_tbl, vend_k, cinfo = dr.carry_factors_for_kept(prev_split if prev_split is not None else sp0.iloc[0:0], sp0, kept_ohlc, ctx.prev_asof,
                                                                       dr.calendar_from_frame(pd.read_parquet(out.calendar) if out.calendar.exists() else load_prev_calendar(prev)), ctx.observed)
                ctx.kept_factor_tbl, ctx.kept_factor_vendor = carried_tbl, vend_k
                inv_stats["factors_carried"] = cinfo
                if cinfo.get("ids_without_prev_intervals"):
                    ctx.results["steps"]["U3"] = res
                    raise UpdateAbort(5, f"kept restatement without previous intervals to carry: {cinfo['ids_without_prev_intervals']}")
                in_k = np.isin(fsd.tradingItemId.to_numpy(np.int64), np.array(sorted(kept_ohlc), dtype=np.int64))
                if in_k.any():
                    adj_c = dr.side_from_intervals(fsd.tradingItemId.to_numpy()[in_k], fsd.date.to_numpy()[in_k], carried_tbl)
                    a = fsd.adjustmentfactor.to_numpy(dtype=np.float64).copy()
                    a[in_k] = adj_c
                    fsd["adjustmentfactor"] = a
                ctx.warn(f"U3: factors of {len(kept_ohlc)} kept-restatement id(s) carried from the previous build ({cinfo['new_boundaries_added']} new vendor boundaries appended); "
                         f"vendor intervals logged in factor_revisions as vendor_not_adopted")
        # base values: keep the stored values on common rows (bit-stable), take the fetched values for new rows (and for accepted restatements)
        kp = row_keys(stored.tradingItemId.to_numpy(), stored.date.to_numpy())
        kn = row_keys(fbd.tradingItemId.to_numpy(), fbd.date.to_numpy())
        op = np.argsort(kp)
        kp_s = kp[op]
        pos = np.searchsorted(kp_s, kn)
        pos_c = np.minimum(pos, max(len(kp_s) - 1, 0))
        common = (len(kp_s) > 0) & (pos < len(kp_s))
        if len(kp_s):
            common &= kp_s[pos_c] == kn
        take_stored = common.copy()
        if accept:
            take_stored &= ~np.isin(fbd.tradingItemId.to_numpy(np.int64), np.array(sorted(accept), dtype=np.int64))
        for f in PRICE_FIELDS + ["source", "qualityFlag"]:
            vals = fbd[f].to_numpy().copy()
            vals[take_stored] = stored[f].to_numpy()[op][pos_c][take_stored]
            if f in ("source", "qualityFlag") and accept:
                keep_meta = common & ~take_stored
                vals[keep_meta] = stored[f].to_numpy()[op][pos_c][keep_meta]
                if f == "source" and len(acc_keys):
                    vals[np.isin(kn, acc_keys)] = SOURCE_CORRECTED
            if f == "qualityFlag":
                vals[vals == Q_POST_LIFECYCLE] = Q_CONFIRMED      # publish-derived flag (published input): re-applied by publish
            fbd[f] = vals
        if accept and len(acc_keys):
            # P2-2: accepted vendor values inside a price override window get the override again (flag 8/32 must mean 'override applied')
            fbd, n_px_acc = dr.apply_price_to_rows(fbd, ctx.price_win, np.isin(kn, acc_keys), ctx.alog)
            res["price_override_rows_reapplied_on_accepted"] = int(n_px_acc)
        # window flags (fresh mi comparison) apply to window dates of these ids as well
        if ctx.flag_map:
            fl = np.array([ctx.flag_map.get(int(k), -1) for k in kn])
            hit = fl >= 0
            qf = fbd.qualityFlag.to_numpy().copy()
            qf[hit] = fl[hit]
            fbd["qualityFlag"] = qf.astype(np.int8)
            # values inside the window come from the window fetch (same snapshot) -> apply the window's source too
            if ctx.win_base is not None and len(ctx.win_base):
                wk = row_keys(ctx.win_base.tradingItemId.to_numpy(), ctx.win_base.date.to_numpy())
                wsrc = dict(zip(wk.tolist(), ctx.win_base.source.to_numpy().tolist()))
                s2 = fbd.source.to_numpy().copy()
                for j, k in enumerate(kn):
                    if int(k) in wsrc:
                        s2[j] = wsrc[int(k)]
                fbd["source"] = s2.astype(np.int8)
        # rc2f price overrides on the rows the vendor added (not stored before) inside an override window
        fbd, n_px = dr.apply_price_to_rows(fbd, ctx.price_win, ~common, ctx.alog)
        res["price_override_rows_applied_full_refetch"] = int(n_px)
        late = ~common & (to_ns(fbd.date) <= np.datetime64(ctx.prev_asof, "ns"))
        if late.any() or inv_stats["rows_only_in_stored"]:
            obs = ctx.observed
            rows = []
            if late.any():
                rows.append(pd.DataFrame({"tradingItemId": fbd.tradingItemId.to_numpy()[late], "date": fbd.date.to_numpy()[late], "field": "row", "old": np.nan,
                                          "new": fbd.close.to_numpy(dtype=np.float64)[late], "observedAt": obs, "reason": "vendor_refetch_full_added"}))
            gone = ~np.isin(kp, kn)
            if gone.any():
                rows.append(pd.DataFrame({"tradingItemId": stored.tradingItemId.to_numpy()[gone], "date": stored.date.to_numpy()[gone], "field": "row",
                                          "old": stored.close.to_numpy(dtype=np.float64)[gone], "new": np.nan, "observedAt": obs, "reason": "vendor_refetch_full_removed"}))
            ctx.price_revisions.append(finalize_revisions(pd.concat(rows, ignore_index=True)))
        ctx.pend.add_full(base_from_pandas(fbd), side_from_pandas(fsd), sorted(R))
        log.info("U3: re-fetched %d ids (%d rows), raw invariance max rel diff %.2e, violations %d", len(R), full.num_rows, inv_stats["max_rel_diff"], inv_stats["violations"])
    # ---- 3. merge prices (single pass over the previous file)
    if not ctx.merge_done:
        flush_merge(ctx)
    # ---- 4. reconcile the split table against the (refreshed) side file, check, write
    t1 = time.time()
    sp, rec = pfm.reconcile_split(sp0, out.sidecar, snapshot)
    res["reconcile"] = {k: v for k, v in rec.items() if k not in ("rebuilt_ids", "rebuilt_rows_by_id_top")}
    res["reconcile"]["rebuilt_ids_sample"] = rec.get("rebuilt_ids", [])[:20]
    # rc2f: new aux ids (aux_additions of the day) bring their own intervals
    for name, tbl in (("split", None), ("dividend", None)):
        add = ctx.aux_factor_rows.get(name)
        if add is not None and len(add):
            if name == "split":
                sp = pd.concat([sp[~sp.tradingItemId.isin(add.tradingItemId)], add[sp.columns]], ignore_index=True).sort_values(["tradingItemId", "validFrom"]).reset_index(drop=True)
            else:
                dd = pd.concat([dd[~dd.tradingItemId.isin(add.tradingItemId)], add[dd.columns]], ignore_index=True).sort_values(["tradingItemId", "validFrom"]).reset_index(drop=True)
    chk_sp, chk_dd = pfm.check_intervals(sp, ids=ids), pfm.check_intervals(dd, ids=ids)
    res["interval_check"] = {"split": chk_sp["ok"], "dividend": chk_dd["ok"], "split_violations": chk_sp["violations"], "dividend_violations": chk_dd["violations"]}
    if not (chk_sp["ok"] and chk_dd["ok"]):
        ctx.results["steps"]["U3"] = res
        raise UpdateAbort(5, f"factor interval check failed: split {chk_sp['violations']} dividend {chk_dd['violations']}")
    # P0-1: kept restatements -> their intervals are the carried table (raw and factors on one basis); the vendor table of the day keeps them too
    kept_tbl = getattr(ctx, "kept_factor_tbl", None)
    kept_ids_f = set(int(i) for i in kept_tbl.tradingItemId) if kept_tbl is not None and len(kept_tbl) else set()
    if kept_ids_f:
        sp = pd.concat([sp[~sp.tradingItemId.isin(list(kept_ids_f))], kept_tbl[sp.columns]], ignore_index=True).sort_values(["tradingItemId", "validFrom"]).reset_index(drop=True)
    # rc2f split overrides re-applied on the vendor table (kept apart as split_vendor.parquet: G-SPLIT identity, returns_v2_qa --vendor-factors)
    write_parquet_atomic(sp, out.split_vendor)
    cal_frame = pd.read_parquet(out.calendar) if out.calendar.exists() else load_prev_calendar(prev)
    px_by_tid = {}
    for r in ctx.price_win.itertuples(index=False):
        px_by_tid.setdefault(int(r.tradingItemId), []).append(r.overrideId)
    sp, so_info = dr.reapply_split_overrides(sp, ctx.ovr.get("split"), out.base_data, dr.calendar_from_frame(cal_frame), ctx.alog, px_by_tid, carried_ids=kept_ids_f)
    res["split_overrides"] = so_info
    chk_sp2 = pfm.check_intervals(sp, ids=ids)
    if not chk_sp2["ok"]:
        ctx.results["steps"]["U3"] = res
        raise UpdateAbort(5, f"split interval check failed after the rc2f overrides: {chk_sp2['violations']}")
    write_parquet_atomic(sp, out.split)
    write_parquet_atomic(dd, out.dividend)
    # raw vendor snapshots sorted by key so two runs on the same input give the same bytes (R-D2 P2-10)
    write_parquet_atomic(sc.sort_values(["tradingitemid", "splitdate", "nextsplitdate"], kind="mergesort").reset_index(drop=True), out.raw_splitcache)
    write_parquet_atomic(dv.sort_values(["tradingitemid", "fromdate", "todate"], kind="mergesort").reset_index(drop=True), out.raw_divadj)
    res["seconds_reconcile_write"] = round(time.time() - t1, 1)
    res["split_by_source"] = sp.source.value_counts().to_dict()
    res["dividend_by_source"] = dd.source.value_counts().to_dict()
    # ---- 5. factor revisions (diff against the stored tables) + monthly snapshot
    revs = []
    if prev_split is not None:
        revs.append(factor_diff(prev_split, sp, "split", ctx.observed))
    if prev.dividend.exists():
        revs.append(factor_diff(pd.read_parquet(prev.dividend), dd, "dividend", ctx.observed))
    if kept_ids_f and getattr(ctx, "kept_factor_vendor", None) is not None and prev_split is not None:
        vna = factor_diff(prev_split[prev_split.tradingItemId.isin(list(kept_ids_f))], ctx.kept_factor_vendor, "split", ctx.observed)
        vna["kind"] = "vendor_not_adopted:" + vna.kind.astype(str)
        revs.append(vna)
    frev = pd.concat(revs, ignore_index=True) if revs else pd.DataFrame(columns=REV_FACTOR_COLS)
    ctx.factor_revisions = frev
    write_parquet_atomic(frev, out.revisions_dir(ctx.observed) / "factor_revisions.parquet")
    res["factor_revisions"] = {"rows": int(len(frev)), "by_table_kind": kv_str(frev.groupby(["table", "kind"]).size().to_dict()) if len(frev) else {},
                               "ids": int(frev.tradingItemId.nunique()) if len(frev) else 0,
                               "split_ids": sorted(frev.loc[frev.table == "split", "tradingItemId"].unique().tolist())[:50] if len(frev) else []}
    month_dir = out.archive / "factor_snapshots"
    have_month = any(p.name.startswith(f"date={ctx.observed:%Y-%m}") for p in month_dir.glob("date=*")) if month_dir.exists() else False
    if not have_month:
        d = month_dir / f"date={ctx.observed:%Y-%m-%d}"
        d.mkdir(parents=True, exist_ok=True)
        copy_file(out.split, d / "split.parquet")
        copy_file(out.dividend, d / "dividend.parquet")
        res["factor_snapshot_written"] = str(d)
    ctx.results["steps"]["U3"] = res
    ctx.time_step("U3", t0, refetched_ids=len(R), factor_revisions=int(len(frev)), rebuilt_from_adjf=rec.get("ids_rebuilt_from_adjf"))


def link_side_publish_name(out: OutBuild):
    """T2/factors/adjustment_factor_daily.parquet (the name publish.py uses) -> hard link to T2_work/adjf_long.parquet."""
    if not out.sidecar.exists():
        return
    out.side_published_name.parent.mkdir(parents=True, exist_ok=True)
    out.side_published_name.unlink(missing_ok=True)
    try:
        os.link(out.sidecar, out.side_published_name)
    except OSError:
        shutil.copy2(out.sidecar, out.side_published_name)


def flush_merge(ctx: Ctx):
    """Write the new base_data + side file from the previous files and the pending rows; then the price revisions."""
    t0 = time.time()
    out, prev = ctx.out, ctx.prev
    if ctx.pend.empty():
        if not out.base_data.exists():
            copy_file(prev.base_data, out.base_data)
            copy_file(prev.sidecar, out.sidecar)
        link_side_publish_name(out)
        ctx.results["steps"]["merge"] = {"skipped": "no pending rows"}
        ctx.merge_done = True
        return
    ctx.pend.asof = ctx.asof
    info = merge_prices(prev.base_data, prev.sidecar if prev.sidecar.exists() else None, out.base_data, out.sidecar, ctx.pend)
    if not info["side_written"]:
        ctx.warn("side file not written (previous build had none) - U3 rebuilds it from the full re-fetch")
    ctx.results["steps"]["merge"] = info
    rev = finalize_revisions(pd.concat(ctx.price_revisions, ignore_index=True)) if ctx.price_revisions else finalize_revisions(pd.DataFrame())
    write_table_atomic(pa.Table.from_pandas(rev, schema=REV_PRICE_SCHEMA, preserve_index=False), out.revisions_dir(ctx.observed) / "price_revisions.parquet")
    ctx.results["steps"]["merge"]["price_revisions_rows"] = int(len(rev))
    ctx.results["steps"]["merge"]["price_revisions_by_reason"] = rev.reason.value_counts().to_dict() if len(rev) else {}
    ctx.merge_done = True
    log.info("merge: %s; price revisions %d rows", {k: v for k, v in info.items() if k != "bytes"}, len(rev))
    ctx.time_step("merge", t0, rows_out=info["rows_out"], rows_added=info["rows_added"], rows_dropped=info["rows_dropped"])


# ======================================================================================
# U4 lifecycle / lineage / exclusions / master QA
# ======================================================================================
def point_master_modules(out: OutBuild):
    """master_* modules keep their stage paths as module globals; point them at the new build (no file is edited)."""
    mb.STAGE, mb.WORK, mb.MASTER_DIR, mb.QA_DIR = out.t1, out.t1_work, out.master, out.t1_qa
    mb.ensure_dirs()


def u4_lifecycle(ctx: Ctx):
    t0 = time.time()
    db, out, prev, args = ctx.dbc(), ctx.out, ctx.prev, ctx.args
    res = {}
    ids = np.sort(np.asarray(ctx.ids, dtype=np.int64))
    if ctx.asof is None:
        ctx.asof = ctx.prev_asof
    point_master_modules(out)
    queried = pd.Timestamp(now_str())
    # ---- aggregates
    if args.full_lifecycle or not (out.t1_work / "ciq_agg.parquet").exists() or not (out.t1_work / "mi_agg.parquet").exists():
        if not args.full_lifecycle:
            ctx.warn("previous build has no ciq_agg/mi_agg: full aggregation over the price tables (about 16 min per table)")
        for name in ("ciq_agg", "mi_agg"):
            (out.t1_work / f"{name}.parquet").unlink(missing_ok=True)
            shutil.rmtree(out.t1_work / f"{name.split('_')[0]}_agg_parts", ignore_errors=True)
        wait_for_quiet(db, max_wait_s=args.db_wait_max)
        t1 = time.time()
        ml.step_aggregate(ids, workers=2)
        res["seconds_full_aggregation"] = round(time.time() - t1, 1)
        res["aggregation"] = "full"
    else:
        ciq_agg = pd.read_parquet(out.t1_work / "ciq_agg.parquet")
        mi_agg = pd.read_parquet(out.t1_work / "mi_agg.parquet")
        win_lo = ctx.refresh_dates[0] if ctx.refresh_dates else None
        if ctx.win_base is not None and win_lo is not None:
            wb = ctx.win_base
            w = agg_from_rows(wb.tradingItemId, wb.date, np.isfinite(wb.close.to_numpy(dtype=np.float64)), queried).set_index("tradingItemId")
            pw = ctx.prev_win
            pc_ = pd.DataFrame({"tradingItemId": pw.tradingItemId.to_numpy(np.int64), "c": np.isfinite(pw.close.to_numpy(dtype=np.float64))})
            g = pc_.groupby("tradingItemId")
            w["prev_rows_in_window"] = g.size().reindex(w.index).fillna(0).astype(int)
            w["prev_close_in_window"] = g.c.sum().reindex(w.index).fillna(0).astype(int)
            # ids that had stored window rows but no re-fetched rows: their window rows vanished
            gone = set(pc_.tradingItemId.unique()) - set(w.index)
            if gone:
                extra = pd.DataFrame({"tradingItemId": sorted(gone)}).set_index("tradingItemId")
                for c in ("first_close", "last_close", "first_row", "last_row"):
                    extra[c] = pd.NaT
                extra["n_rows"] = 0
                extra["n_close"] = 0
                extra["n_close_2000"] = 0
                extra["queriedAt"] = queried
                extra["prev_rows_in_window"] = g.size().reindex(extra.index).fillna(0).astype(int)
                extra["prev_close_in_window"] = g.c.sum().reindex(extra.index).fillna(0).astype(int)
                w = pd.concat([w, extra])
                # last_* for these ids: recompute from the stored file (rows before the window)
                rows = read_rows_filtered(out.base_data, ["date", "tradingItemId", "close"], ids=np.array(sorted(gone), dtype=np.int64))
                if len(rows):
                    a = agg_from_rows(rows.tradingItemId, rows.date, np.isfinite(rows.close.to_numpy(dtype=np.float64)), queried).set_index("tradingItemId")
                    for tid in a.index:
                        for c in ("last_close", "last_row"):
                            w.at[tid, c] = a.at[tid, c]
            full_agg = None
            if ctx.refetched_ids:
                rows = read_rows_filtered(out.base_data, ["date", "tradingItemId", "close"], ids=np.array(sorted(ctx.refetched_ids), dtype=np.int64))
                full_agg = agg_from_rows(rows.tradingItemId, rows.date, np.isfinite(rows.close.to_numpy(dtype=np.float64)), queried)
                # keep pre-2000 first dates from the stored aggregate (base_data starts in 2000)
                fa = full_agg.set_index("tradingItemId")
                pa_ = ciq_agg.set_index("tradingItemId")
                for tid in fa.index:
                    if tid in pa_.index:
                        for c in ("first_close", "first_row"):
                            a, b = pa_.at[tid, c], fa.at[tid, c]
                            fa.at[tid, c] = min(pd.Timestamp(a), pd.Timestamp(b)) if pd.notna(a) and pd.notna(b) else (a if pd.notna(a) else b)
                        # counts: pre-2000 part unknown here -> shift the stored totals by the change in 2000+ closes
                        delta = int(fa.at[tid, "n_close_2000"]) - int(pa_.at[tid, "n_close_2000"])
                        fa.at[tid, "n_rows"] = int(pa_.at[tid, "n_rows"]) + delta
                        fa.at[tid, "n_close"] = int(pa_.at[tid, "n_close"]) + delta
                full_agg = fa.reset_index()[AGG_COLS]
                w = w[~w.index.isin(full_agg.tradingItemId)]
            ciq_agg = update_agg(ciq_agg, w.reset_index(), win_lo, full_agg, queried)
            # mi: window rows of miprice (all ids)
            mi = ctx.win_mi
            if mi is not None and len(mi):
                mw = mi[(mi.date >= win_lo) & (mi.date <= ctx.asof)]
                wm = agg_from_rows(mw.tradingitemid, mw.date, mw.priceclose.notna().to_numpy(), queried).set_index("tradingItemId")
                pm = mi_agg.set_index("tradingItemId")
                # stored mi counts inside the window are unknown (no mi file): approximate with rows after the stored last_row
                wm["prev_rows_in_window"] = 0
                wm["prev_close_in_window"] = 0
                for tid in wm.index:
                    if tid in pm.index and pd.notna(pm.at[tid, "last_row"]) and pd.Timestamp(pm.at[tid, "last_row"]) >= win_lo:
                        # rows up to the stored last_row were already counted -> count only the newer ones
                        sub = mw[(mw.tradingitemid == tid)]
                        older = sub[sub.date <= pd.Timestamp(pm.at[tid, "last_row"])]
                        wm.at[tid, "prev_rows_in_window"] = int(len(older))
                        wm.at[tid, "prev_close_in_window"] = int(older.priceclose.notna().sum())
                mi_agg = update_agg(mi_agg, wm.reset_index(), win_lo, None, queried)
            res["aggregation"] = "incremental"
        else:
            res["aggregation"] = "carried (no window rows)"
        # ids missing from the aggregates (should only be brand-new ids handled in U1)
        for name, a in (("ciq_agg", ciq_agg), ("mi_agg", mi_agg)):
            miss = np.setdiff1d(ids, a.tradingItemId.to_numpy(np.int64))
            if len(miss):
                ctx.warn(f"{name}: {len(miss)} ids without aggregate row -> zero rows")
                a = pd.concat([a, pd.DataFrame({"tradingItemId": miss, "first_close": pd.NaT, "last_close": pd.NaT, "first_row": pd.NaT, "last_row": pd.NaT,
                                                "n_rows": 0, "n_close": 0, "n_close_2000": 0, "queriedAt": queried})], ignore_index=True)
            a = a[a.tradingItemId.isin(ids)].sort_values("tradingItemId").reset_index(drop=True)
            for c in ("first_close", "last_close", "first_row", "last_row"):
                a[c] = pd.to_datetime(a[c]).astype("datetime64[ns]")
            write_parquet_atomic(a[AGG_COLS], out.t1_work / f"{name}.parquet")
    # ---- events source for lifecycle / lineage
    ev_default = mb.EVENTS_PATH
    if out.events.exists():
        try:
            cols = set(pq.read_schema(out.events).names)
            if {"tradingItemId", "endDate", "actionCode", "successorTradingItemId"} <= cols:
                mb.EVENTS_PATH = out.events
                res["events_source"] = str(out.events)
            else:
                ctx.warn(f"T6 events file lacks lifecycle columns ({sorted(cols)[:8]}...): using {ev_default}")
        except Exception as exc:  # pragma: no cover
            ctx.warn(f"T6 events file unreadable ({exc}): using {ev_default}")
    if not Path(mb.EVENTS_PATH).exists():
        raise UpdateAbort(6, f"events file for lifecycle not found: {mb.EVENTS_PATH}")
    # ---- lifecycle -> lineage -> first-date lift -> exclusions -> QA (T1 modules, module paths pointed at the build)
    cur = ctx.cur if ctx.cur is not None else pd.read_parquet(out.master_file("current"))
    ident = ctx.ident if ctx.ident is not None else (pd.read_parquet(out.t1_work / "identity.parquet") if (out.t1_work / "identity.parquet").exists()
                                                    else pd.read_parquet(prev.t1_work / "identity.parquet"))
    idm = ctx.id_mapping if ctx.id_mapping is not None else pd.read_parquet(out.master_file("id_mapping"))
    prev_lc = pd.read_parquet(prev.master_file("lifecycle")) if prev.master_file("lifecycle").exists() else None
    prev_ln = pd.read_parquet(out.master_file("lineage")) if out.master_file("lineage").exists() else None      # previous build's lineage (v2 + overrides, carried)
    ml.step_lifecycle(ids, cur, ctx.calendar, ctx.asof, ctx.observed)
    for f in ("lineage_full.parquet",):
        (out.t1_work / f).unlink(missing_ok=True)
    out.master_file("lineage").unlink(missing_ok=True)
    mg.step_lineage(db, ident, cur, idm, ctx.calendar)
    ml.apply_lineage_first_dates(ctx.calendar, db)      # db: constant-ratio stitch test (T1 'adjust' step)
    ml.step_exclusions(db, cur, ctx.calendar)
    gates = mq.run_all(ident, cur, idm, ctx.calendar, ctx.asof, {})
    # rc2f (D2): lineage of the day = previous v2 rows carried + new v1 pairs; lineage overrides re-applied (idempotent daily rule)
    v1 = pd.read_parquet(out.master_file("lineage"))
    copy_file(out.master_file("lineage"), out.t1_work / "lineage_v1_daily.parquet")
    lc_now = pd.read_parquet(out.master_file("lifecycle"))
    if out.master_file("lifecycle_aux").exists():
        lca_ = pd.read_parquet(out.master_file("lifecycle_aux"))
        lc_now = pd.concat([lc_now, lca_[lc_now.columns]], ignore_index=True)
    cal_days = ctx.calendar.astype("datetime64[ns]")
    recent_after = ctx.prev_asof - pd.Timedelta(days=args.lineage_recent_days)
    if prev_ln is not None and "version" in prev_ln.columns:
        ln, ln_info = dr.merge_lineage_daily(prev_ln, v1, lc_now, cal_days, recent_after=recent_after, new_ids=ctx.new_ids, excluded_out=out.t1_qa / "lineage_daily_excluded.csv")
    else:
        ln, ln_info = dr.merge_lineage_daily(pd.DataFrame(columns=dr.V2_COLS), v1, lc_now, cal_days)
        ctx.warn("previous build has no lineage v2: daily lineage built from today's v1 candidates only")
    if ln_info.get("new_pairs_excluded_historical"):
        ctx.warn(f"U4 lineage: {ln_info['new_pairs_excluded_historical']} historical candidate pairs not in the snapshot lineage were NOT added (T1/qa/lineage_daily_excluded.csv); {ln_info['new_pairs']} recent pairs added")
    ln = dr.reapply_lineage_overrides(ctx.ovr.get("lineage"), ln, lc_now.set_index("tradingItemId"), cal_days, ctx.alog)
    dr.ra._write_parquet_like(ln, prev.master_file("lineage"), out.master_file("lineage"), extra={"linkVerified": pa.bool_()}) if prev.master_file("lineage").exists() else ln.to_parquet(out.master_file("lineage"), index=False)
    res["lineage_daily"] = {**ln_info, "rows_after_overrides": int(len(ln))}
    ctx.rc2f["U4"] = res["lineage_daily"]
    res["master_gates"] = {"G1_pairs": gates["G1_same_security_both_tradable"]["pairs"], "G1_after_exclusions_left": gates.get("G1_after_exclusions", {}).get("pairs_left"),
                           "G2": gates["G2_interval_overlaps"], "symbol_set_consistent": gates["symbol_set"]["all_files_consistent"],
                           "self_tests": gates["self_test_overlap_checker"]["pass"] and gates["self_test_same_security_checker"]["pass"]}
    summ = json.loads((out.t1_qa / "lifecycle_summary.json").read_text())
    res["lifecycle"] = {"status_counts": summ["status_counts"], "self_test_all_pass": summ["self_test"].get("all_pass"), "n": summ["n"],
                        "note": "pre-override lifecycle (instruments); the rc2f lifecycle overrides, aux rows and the revisions vs the previous build are applied in U5 (finalize)"}
    ctx.results["steps"]["U4"] = res
    ctx.time_step("U4", t0, status=res["lifecycle"]["status_counts"])


# ======================================================================================
# U5 derived: returns_total, marketcap / universe (T5), events (T6)
# ======================================================================================
def find_script(names: list[str]) -> Path | None:
    for n in names:
        p = HERE / n
        if p.exists():
            return p
    return None


def ledger_asof(path: Path) -> pd.Timestamp | None:
    """asOf of a ledger from its builder's sidecar (ca_ledger_build_info.json next to it: asOf / asOfDate / asof / as_of,
    also nested under 'inputs'), else None (= unknown, treated as stale by the U5 check)."""
    side = Path(path).with_name("ca_ledger_build_info.json")
    if side.exists():
        try:
            j = json.loads(side.read_text())
            for d in (j, j.get("inputs", {}) if isinstance(j.get("inputs"), dict) else {}):
                for k in ("asOf", "asOfDate", "asof", "as_of"):
                    if d.get(k):
                        return ts(d[k])
        except Exception:  # noqa: BLE001
            return None
    return None


def ledger_staleness(ledger_asof_: pd.Timestamp | None, build_asof: pd.Timestamp | None, prev_asof: pd.Timestamp | None) -> tuple[bool, pd.Timestamp | None, str]:
    """(stale, stale_after, reason). Unknown ledger asOf (no sidecar key) is stale: the check cannot prove coverage of the
    appended days, so the conservative cut is the previous build's asOf (rework residual P1-a)."""
    if ledger_asof_ is None:
        return True, prev_asof, "ledger asOf unknown (ca_ledger_build_info.json has no asOf key): coverage of the appended days cannot be verified"
    if build_asof is not None and ledger_asof_ < build_asof:
        return True, ledger_asof_, f"ledger asOf {ledger_asof_.date()} is older than the build asOf {build_asof.date()} (events of the appended days unknown)"
    return False, None, ""


def resolve_ledger(ctx: Ctx) -> tuple[Path | None, pd.Timestamp | None]:
    """rc2 returns v2 input -> (ledger path, ledger asOf or None). Order: --ledger, else the new build's
    T6/events/ca_ledger.parquet (written by the ledger builder when it runs in this update), else the previous build's ledger
    copied forward. A carried-forward ledger is stale by construction (asOf = previous build asOf); a ledger whose sidecar
    asOf is older than the update asOf is stale too. Stale -> UpdateAbort unless --allow-stale-ledger, in which case the
    rows after the ledger asOf are flagged retFlag 4 and BUILD.json / manifest carry ledger_stale (rework F5).
    (None, None) only with --returns-legacy."""
    out, prev, args = ctx.out, ctx.prev, ctx.args
    if getattr(args, "returns_legacy", False):
        ctx.warn("returns_total: --returns-legacy -> rc1 vendor-factor computation (not the rc2 cash-style contract)")
        return None, None
    if getattr(args, "ledger", None):
        p = Path(args.ledger)
        if not p.exists():
            raise UpdateAbort(5, f"--ledger {p} not found")
        return p, ledger_asof(p)
    if out.ledger.exists():
        return out.ledger, ledger_asof(out.ledger)
    if prev.ledger.exists():
        copy_file(prev.ledger, out.ledger)
        side = prev.ledger.with_name("ca_ledger_build_info.json")
        if side.exists():
            copy_file(side, out.ledger.with_name("ca_ledger_build_info.json"))
        a = ledger_asof(out.ledger) or ctx.prev_asof
        ctx.warn(f"returns_total: corporate-action ledger carried forward from the previous build ({prev.ledger}, asOf {a.date()}); "
                 f"events after that date are unknown until the ledger builder (R1 ledger_build.py) runs")
        return out.ledger, a
    raise UpdateAbort(5, "returns_total v2 needs events/ca_ledger.parquet (rc2 BRIEF section 3-1): none in --ledger, the new build "
                         "or the previous build; pass --ledger <path> or --returns-legacy")


def resolve_lineage(ctx: Ctx) -> Path | None:
    """Lineage file for first-row links: --lineage, else the new build's master/lineage.parquet (U4 output or carried)."""
    args, out = ctx.args, ctx.out
    if getattr(args, "lineage", None):
        p = Path(args.lineage)
        if not p.exists():
            raise UpdateAbort(5, f"--lineage {p} not found")
        return p
    p = out.master_file("lineage")
    return p if p.exists() else None


def u5_derived(ctx: Ctx):
    t0 = time.time()
    out, prev, args = ctx.out, ctx.prev, ctx.args
    res = {}
    if not out.base_data.exists():
        copy_file(prev.base_data, out.base_data)
    if ctx.asof is None:
        ctx.asof = ctx.prev_asof
    prev_events_df = pd.read_parquet(prev.events) if prev.events.exists() else None
    # ---- T5 universe / T6 events (fresh), then the rc2f finalization of lifecycle / events / dates / lineage (overrides, aux rows)
    res["t5"] = run_t5(ctx)
    t6_res = run_t6(ctx)
    res["t6"] = t6_res
    fresh_ev = pd.read_parquet(out.events) if (out.events.exists() and t6_res.get("ok") and not t6_res.get("carried_from_prev")) else None
    if fresh_ev is None:
        ctx.warn("T6 events not regenerated today: previous events carried (already patched) - lifecycle overrides applied to lifecycle/dates/lineage only")
    t1 = time.time()
    prev_ledger_for_dates = out.ledger if out.ledger.exists() else (prev.ledger if prev.ledger.exists() else None)     # carried copy: payDate of merger legs (refreshed after the builder)
    fin = dr.finalize_master_rc2f(ctx, ctx.ovr, ctx.alog, prev_events_df, fresh_ev, prev_ledger_for_dates, fresh_lifecycle="U4" in args.steps)
    res["finalize"] = fin
    ctx.rc2f["finalize"] = fin
    log.info("U5 finalize (rc2f lifecycle/events/dates/lineage): %s (%.0fs)", {k: v for k, v in fin.items() if k in ("events_merge", "lifecycle", "lineage", "lifecycle_revisions")}, time.time() - t1)
    # ---- U-L: corporate-action ledger, incremental (D1 CLI) on the finalized master/events of the day; its CSV override results feed the apply log
    if getattr(args, "ledger", None):
        res["ledger_incremental"] = {"skipped": f"--ledger {args.ledger}"}
    elif args.ledger_mode == "skip":
        res["ledger_incremental"] = {"skipped": "--ledger-mode skip"}
    else:
        r = dr.run_ledger_incremental(ctx, run_cmd, dr.prev_published_view(ctx), args.overrides_glob)
        res["ledger_incremental"] = r
        if "skipped" not in r and r["rc"] != 0:
            raise UpdateAbort(5, f"ledger builder (incremental) failed rc {r['rc']}, see {out.cmd_log}")
    ctx.rc2f["ledger"] = res["ledger_incremental"]
    res["source_freshness"] = source_freshness(ctx)
    ss = out.t6 / "work" / "source_status.json"
    if ss.exists():
        try:
            res["source_freshness"]["ledger_source_status"] = json.loads(ss.read_text())      # D1: per-source latest event dates of the day's extract
        except Exception:  # noqa: BLE001
            pass
    ctx.rc2f["source_freshness"] = res["source_freshness"]
    ledger, l_asof = resolve_ledger(ctx)
    stale_after = None
    if ledger is not None:
        asof = ctx.asof if ctx.asof is not None else ctx.prev_asof
        stale, stale_after, why = ledger_staleness(l_asof, asof, ctx.prev_asof)
        if stale:
            msg = f"returns_total: {why}"
            if not getattr(args, "allow_stale_ledger", False):
                raise UpdateAbort(5, msg + "; run the ledger builder (sidecar asOf >= build asOf) or pass --allow-stale-ledger "
                                     f"(rows after {stale_after.date() if stale_after is not None else 'the previous asOf'} get retFlag 4)")
            ctx.warn(msg + f"; --allow-stale-ledger: rows after {stale_after.date() if stale_after is not None else 'n/a'} are flagged retFlag 4, BUILD.json/manifest carry ledger_stale")
        ctx.ledger_stale_after = stale_after
    if ledger is not None:
        dr.ledger_csv_override_statuses(Path(ledger).parent, ctx.alog, ctx.ovr.get("ledger"))
        sc = dr.finalize_scope(ctx, ledger, ctx.alog)
        res["finalize_scope"] = sc
        ctx.rc2f["finalize"].update(sc)
    # ---- returns v2
    lineage = resolve_lineage(ctx) if ledger is not None else None
    lifecycle_p = out.master_file("lifecycle") if ledger is not None else None          # rc2f link/settlement-cash rules need lifecycle.lastTradeDate
    info = prm.build_returns(out.t2, ledger=ledger, legacy=ledger is None, lineage=lineage, stale_after=stale_after,
                             links_out=out.t2_qa / "returns_links.csv" if ledger is not None else None,
                             lifecycle=lifecycle_p if (lifecycle_p is not None and lifecycle_p.exists()) else None)
    res["returns_total"] = {k: info[k] for k in ("rows_in", "rows_out", "first_rows_nan", "bytes", "seconds")}
    res["returns_total"].update({"convention": info.get("convention"), "ledger": str(ledger) if ledger else None, "ledger_sha256": info.get("ledger_sha256"),
                                 "ledger_asof": str(l_asof.date()) if l_asof is not None else "unknown", "ledger_stale": stale_after is not None,
                                 "stale_after": info.get("stale_after"), "lineage": str(lineage) if lineage else None, "lineage_sha256": info.get("lineage_sha256"),
                                 "retFlag": info.get("retFlag"), "events": info.get("events"), "links_resolved": info.get("links_resolved")})
    # ---- returns v2 QA gates on the stage (+ rc1 vendor-factor returns of the same base_data for G-VENDOR) and attribution vs the previous build
    if ledger is not None:
        rc1 = None
        if args.returns_qa_vendor:
            t1 = time.time()
            rc1 = out.t2_work / "returns_rc1_style.parquet"
            prm.build_returns(out.t2, out=rc1, legacy=True)
            res["returns_rc1_style_seconds"] = round(time.time() - t1, 1)
        ctx.write_apply_log()
        qa = dr.run_returns_qa(ctx, run_cmd, out.split_vendor if out.split_vendor.exists() else out.split, out.rc2f, rc1)
        res["returns_qa"] = qa
        ctx.rc2f["returns_qa"] = qa
        t1 = time.time()
        lrev_p = out.revisions_dir(ctx.observed) / "lifecycle_revisions.parquet"
        prev_ln = pd.read_parquet(prev.master_file("lineage")) if prev.master_file("lineage").exists() else None
        ledger_rev_p = out.revisions_dir(ctx.observed) / "ca_ledger_revisions.parquet"
        attr = dr.attribute_returns_changes(ctx, prev.ledger if prev.ledger.exists() else None, ledger, ctx.ovr_cmp or dr.compare_apply_logs(ctx.alog.frame(), ctx.prev_alog), ctx.alog,
                                            prev_ln, pd.read_parquet(out.master_file("lineage")), pd.read_parquet(lrev_p) if lrev_p.exists() else None,
                                            pd.concat(ctx.price_revisions, ignore_index=True) if ctx.price_revisions else None, ctx.factor_revisions,
                                            pd.read_parquet(ledger_rev_p) if ledger_rev_p.exists() else None)
        res["returns_attribution"] = attr
        ctx.rc2f["returns_attribution"] = attr
        log.info("U5 returns attribution: %d changed cells, %d unattributed (%.0fs)", attr.get("cells_changed", 0), attr.get("unattributed", 0), time.time() - t1)
    ctx.results["steps"]["U5"] = res
    ctx.time_step("U5", t0, returns_rows=info["rows_out"])


def source_freshness(ctx: Ctx) -> dict:
    """co_acthist (deal terms of mergers / settlements) has had no load since 2026-02-20 (build doc section 7-8): the ledger builder and T6
    see no new co_acthist rows, so recent events depend on the other sources (ciqdividend types 20/31 spin/rights, ciqsplit, distributions
    proto table (static), lifecycle markers securityEndDate / status, lineage). Recorded in BUILD.json; a warning gate when the last
    datadate is older than asOf - --co-acthist-max-lag-days."""
    res = {"co_acthist_max_datadate": None, "days_behind_asof": None, "lag_days_max": ctx.args.co_acthist_max_lag_days,
           "alternative_sources": ["ciqdividend (types 20/31: spin-off / rights)", "ciqsplit / ciqsplitcache", "distributions proto table (static, 2026-09-14)",
                                   "lifecycle markers (ciqsecurity.securityEndDate, tradingItemStatusId 4/5/8/11)", "lineage (rename/relist)", "overrides/rc2f/** (manual terms)"]}
    if ctx.args.offline:
        return res
    try:
        r = ctx.dbc().query(f"select max(datadate) mx from {SCHEMA}.co_acthist")
        mx = pd.Timestamp(r.mx.iloc[0]) if len(r) and pd.notna(r.mx.iloc[0]) else None
        res["co_acthist_max_datadate"] = str(mx.date()) if mx is not None else None
        if mx is not None and ctx.asof is not None:
            res["days_behind_asof"] = int((ctx.asof - mx).days)
            res["stale"] = res["days_behind_asof"] > ctx.args.co_acthist_max_lag_days
            if res["stale"]:
                ctx.warn(f"co_acthist last datadate {mx.date()} is {res['days_behind_asof']} days behind asOf {ctx.asof.date()} (> {ctx.args.co_acthist_max_lag_days}): "
                         "merger/settlement terms of recent events come only from the alternative sources (BUILD.json rc2f.source_freshness)")
    except Exception as exc:  # noqa: BLE001
        res["error"] = f"{type(exc).__name__}: {exc}"
    return res


def run_t5(ctx: Ctx) -> dict:
    """T5 hooks (other agent's modules, called as CLIs): universe_marketcap.py build -> derived/marketcap.parquet,
    universe_build.py run --publish -> universe/membership{,_v1rule}.parquet. Fresh work dir = full re-pull of
    ciqmarketcap (about 5 min); a T5 incremental option is a proposal in the T9 report."""
    out, prev, args = ctx.out, ctx.prev, ctx.args
    py = sys.executable
    ran = {}
    if args.t5_mode == "skip":
        return {"skipped": "--t5-mode skip"}
    mc, un = find_script(T5_MARKETCAP), find_script(T5_UNIVERSE)
    if args.t5_mode == "auto":
        if mc is None:
            ctx.warn("T5 marketcap module not found (stub step): previous derived/marketcap.parquet carried forward if present")
        else:
            out.t5.mkdir(parents=True, exist_ok=True)
            r = run_cmd([py, str(mc), "build", "--t2", str(out.t2), "--master", str(out.master), "--out", str(out.t5), "--work", str(out.t5_work)],
                        out.cmd_log, timeout_s=args.t5_timeout)
            ran["marketcap"] = r
            if r["rc"] != 0 or not out.marketcap.exists():
                ctx.warn(f"T5 marketcap CLI failed (rc {r['rc']}); carrying the previous marketcap file forward")
        if un is None:
            ctx.warn("T5 universe module not found (stub step): previous universe/membership.parquet carried forward if present")
        elif out.marketcap.exists() or prev.marketcap.exists():
            if not out.marketcap.exists():
                copy_file(prev.marketcap, out.marketcap)
            extra = [a for a in (args.t5_universe_args or "").split() if a]
            r = run_cmd([py, str(un), "run", "--name", f"daily_{ctx.observed:%Y%m%d}", "--t2", str(out.t2), "--master", str(out.master), "--t5", str(out.t5),
                         "--work", str(out.t5_work), "--publish"] + extra, out.cmd_log, timeout_s=args.t5_timeout)
            ran["universe"] = r
            if r["rc"] != 0 or not out.universe.exists():
                ctx.warn(f"T5 universe CLI failed (rc {r['rc']}); carrying the previous universe file forward")
        if not args.keep_work:
            shutil.rmtree(out.t5_work / "mcap", ignore_errors=True)
            shutil.rmtree(out.t5_work / "runs", ignore_errors=True)
    for src, dst, label in ((prev.marketcap, out.marketcap, "marketcap"), (prev.universe, out.universe, "universe"),
                            (prev.universe.with_name("membership_v1rule.parquet"), out.universe.with_name("membership_v1rule.parquet"), "universe_v1rule")):
        if not dst.exists() and src.exists():
            copy_file(src, dst)
            ran[f"{label}_carried_from_prev"] = str(src)
            ctx.warn(f"{label}: previous file carried forward (its asOf is NOT {ctx.asof.date() if ctx.asof is not None else 'new'})")
    if (prev.t5 / "qa").exists() and not (out.t5 / "qa").exists():
        copy_tree(prev.t5 / "qa", out.t5 / "qa")
    return ran or {"note": "no T5 outputs"}


def run_t6(ctx: Ctx) -> dict:
    """T6 hook: the events_build.py pipeline (prepare-master -> prepare-close -> run -> finalize [-> gates]) on the new
    build, inputs prepared exactly as T6 did (loader export of the raw close panel, lifecycle cut)."""
    out, prev, args = ctx.out, ctx.prev, ctx.args
    py = sys.executable
    if args.t6_mode == "skip":
        return {"skipped": "--t6-mode skip"}
    ev = find_script(T6_EVENTS)
    res = {}
    if args.t6_mode == "auto" and ev is not None:
        work = out.root / "T6_work"
        (work / "build").mkdir(parents=True, exist_ok=True)
        (out.t6 / "events").mkdir(parents=True, exist_ok=True)
        (out.t6 / "qa").mkdir(parents=True, exist_ok=True)
        master_idx = work / "master_v21_indexed.parquet"
        close_cut = work / "close_raw_lifecycle_cut.parquet"
        built = work / "build" / "events_raw_built.parquet"
        # raw close panel for build_events: in-process loader export without the returns panel (returns v2 are built after the ledger, D2)
        t1 = time.time()
        try:
            from loader import CacheV21
            cv = CacheV21(out.t2, master_root=out.t1, threads=args.threads, validate=False, ids=np.asarray(sorted(int(i) for i in ctx.ids), dtype=np.int64))   # aux rows excluded (P2-8)
            m = cv.export_wide(work / "raw_wide", fields=["close"], basis="raw", returns=False, masks=["tradable"], release=True, cut="lifecycle")
            res["export_close"] = {"rc": 0, "seconds": round(time.time() - t1, 1), "ids": len(m.get("ids", [])) if isinstance(m.get("ids"), (list, np.ndarray)) else m.get("n_ids"),
                                   "cut_cells": m.get("cut", {}).get("cells_removed") if isinstance(m.get("cut"), dict) else None}
            del cv
        except Exception as exc:  # noqa: BLE001
            ctx.warn(f"T6 raw close export failed: {type(exc).__name__}: {exc}")
            res["export_close"] = {"rc": 1, "error": f"{type(exc).__name__}: {exc}"}
            res["ok"] = False
        steps = [
            ("prepare_master", [py, str(ev), "prepare-master", "--t1", str(out.t1), "--out", str(master_idx)]),
            ("prepare_close", [py, str(ev), "prepare-close", "--raw-close", str(work / "raw_wide" / "BaseData" / "close.parquet"), "--lifecycle",
                               str(out.master_file("lifecycle")), "--out", str(close_cut)]),
            ("run", [py, str(ev), "run", "--master", str(master_idx), "--close", str(close_cut), "--override", args.events_override, "--out", str(built),
                     "--log", str(work / "build" / "run_wrapper.log")]),
            ("finalize", [py, str(ev), "finalize", "--built", str(built), "--lineage", str(out.master_file("lineage")), "--lifecycle", str(out.master_file("lifecycle")),
                          "--current", str(out.master_file("current")), "--close", str(close_cut), "--out", str(out.events), "--lineage-csv", str(out.t6 / "qa" / "lineage_rows.csv")]),
        ]
        if args.t6_gates:
            steps.append(("gates", [py, str(ev), "gates", "--events", str(out.events), "--close-cut", str(close_cut), "--base-data", str(out.base_data), "--lifecycle",
                                    str(out.master_file("lifecycle")), "--current", str(out.master_file("current")), "--out-md", str(out.t6 / "qa" / "events_gates.md"),
                                    "--out-dir", str(out.t6 / "qa")]))
        ok = res.get("export_close", {}).get("rc") == 0
        for name, cmd in (steps if ok else []):
            r = run_cmd(cmd, out.cmd_log, timeout_s=args.t5_timeout)
            res[name] = r
            if r["rc"] != 0:
                ctx.warn(f"T6 step {name} failed (rc {r['rc']}), see {out.cmd_log}")
                ok = False
                if name != "gates":
                    break
        res["ok"] = ok
        if not args.keep_work:
            shutil.rmtree(work / "raw_wide", ignore_errors=True)
            close_cut.unlink(missing_ok=True)
        if out.events.exists() and ok:
            try:
                n = pq.ParquetFile(out.events).metadata.num_rows
                res["rows"] = int(n)
            except Exception:
                pass
    elif ev is None:
        ctx.warn("T6 events module not found (stub step): previous events file carried forward if present (end dates keep their basis)")
        res["stub"] = True
    if (not out.events.exists() or not res.get("ok", False)) and prev.events.exists():
        copy_file(prev.events, out.events)
        ctx.warn(f"events: previous file carried forward from {prev.events} (its end-date basis is the previous build)")
        res["carried_from_prev"] = str(prev.events)
    if (prev.t6 / "qa").exists() and not (out.t6 / "qa").exists():
        copy_tree(prev.t6 / "qa", out.t6 / "qa")
    return res


# ======================================================================================
# U6 gates
# ======================================================================================
def u6_gates(ctx: Ctx):
    t0 = time.time()
    out, prev, args = ctx.out, ctx.prev, ctx.args
    G = {}
    sp = pd.read_parquet(out.split)
    dd = pd.read_parquet(out.dividend)
    ids = np.sort(np.asarray(ctx.ids, dtype=np.int64))
    # ---- interval self-check
    cs, cd = pfm.check_intervals(sp, ids=ids), pfm.check_intervals(dd, ids=ids)
    G["intervals"] = {"pass": bool(cs["ok"] and cd["ok"]), "split": cs["violations"], "dividend": cd["violations"], "blocking": True}
    split, div = pfm.FactorLookup(sp), pfm.FactorLookup(dd)
    # ---- G-SPLIT on every row (prices_qa.gate_split needs the side file with the same row groups); reference = the vendor-rebuilt split
    #      table (T2_work/split_vendor.parquet): the published split holds the rc2f overrides, which differ from the vendor factor by design
    sp_vendor = pd.read_parquet(out.split_vendor) if out.split_vendor.exists() else sp
    if out.sidecar.exists():
        r = pqa.gate_split(out.t2, out.t2_work, sp_vendor)
        G["G-SPLIT"] = {"pass": bool(r["pass"]), "rows": r["rows"], "violations": r["violations"], "synthetic_detected": r["synthetic"]["detected_rows"] if r["synthetic"] else None,
                        "examples": r["examples"][:10], "blocking": True}
    else:
        G["G-SPLIT"] = {"pass": False, "error": "no side file", "blocking": True}
    # ---- G-JUMP on boundaries of affected ids (factor changes) and boundaries inside the window
    affected = set()
    if ctx.factor_revisions is not None and len(ctx.factor_revisions):
        affected |= set(int(i) for i in ctx.factor_revisions.loc[ctx.factor_revisions.table == "split", "tradingItemId"].unique())
    affected |= set(ctx.refetched_ids) | set(ctx.new_ids)
    win_ids = set(int(i) for i in ctx.win_base.tradingItemId.unique()) if ctx.win_base is not None else set()
    min_date = ctx.refresh_dates[0] if ctx.refresh_dates else None
    subset_ids = affected | win_ids
    known = load_known_artifacts([out.t2_qa / "factor_artifacts.csv"] + prev.known_artifacts)
    known_keys = set(zip(known.tradingItemId.astype(int), pd.to_datetime(known.date)))
    if subset_ids:
        obs = ObservedSubset(out.base_data, out.sidecar if out.sidecar.exists() else None, subset_ids, split, div)
        jb = jump_boundaries(obs, affected if affected else set(), min_date)
        fakes = jb[jb.fake].copy()
        fakes["known"] = [(int(t), pd.Timestamp(d)) in known_keys for t, d in zip(fakes.tradingItemId, fakes.date)]
        new_fakes = fakes[~fakes.known]
        # synthetic counterexample: shift one boundary of the first affected id by one observed row -> must be detected
        syn = None
        if len(jb):
            tid = int(jb.tradingItemId.iloc[0])
            b = sp[sp.tradingItemId == tid].sort_values("validFrom").reset_index(drop=True)
            if len(b) >= 2:
                j = len(b) - 1
                d0 = pd.Timestamp(b.at[j, "validFrom"])
                rows_id = obs.dates[obs.ids == tid]
                after = rows_id[rows_id > np.datetime64(d0, "ns")]
                if len(after):
                    bad = sp.copy()
                    bi = bad.index[(bad.tradingItemId == tid) & (bad.validFrom == d0)][0]
                    bad.loc[bi, "validFrom"] = pd.Timestamp(after[0])
                    bad.loc[bi - 1, "validTo"] = pd.Timestamp(after[0])
                    lk = pfm.FactorLookup(bad)
                    m = obs.ids == tid
                    sfa = lk(obs.ids[m], obs.dates[m])
                    adja = obs.close[m].astype(np.float64) / sfa
                    ra = adja[1:] / adja[:-1] - 1
                    rr = obs.close[m][1:].astype(np.float64) / obs.close[m][:-1] - 1
                    f = pqa.fake_jumps(ra, rr, "t2") & (sfa[1:] != sfa[:-1])
                    syn = {"perturbed_id": tid, "boundary": str(d0.date()), "detected": int(f.sum()), "ok": bool(f.sum() > 0)}
        G["G-JUMP"] = {"pass": bool(len(new_fakes) <= args.allow_new_fake_jumps), "boundaries_checked": int(len(jb)), "fake_jumps": int(len(fakes)),
                       "known_artifacts": int(fakes.known.sum()), "new_fake_jumps": int(len(new_fakes)),
                       "new_examples": [{"tradingItemId": int(r.tradingItemId), "date": str(pd.Timestamp(r.date).date()), "r_adj": float(r.r_adj), "r_raw": float(r.r_raw),
                                         "r_total": float(r.r_total), "sf_prev": float(r.sf_prev), "sf": float(r.sf)} for r in new_fakes.head(20).itertuples()],
                       "synthetic": syn, "subset_ids": int(len(subset_ids)), "blocking": True}
        if len(new_fakes):
            allk = pd.concat([known[["tradingItemId", "date"]], new_fakes[["tradingItemId", "date"]]], ignore_index=True).drop_duplicates()
            new_fakes.assign(observedAt=ctx.observed).to_csv(out.t2_qa / f"factor_artifacts_new_{ctx.observed:%Y-%m-%d}.csv", index=False)
            allk.to_csv(out.t2_qa / "factor_artifacts.csv", index=False)
    else:
        G["G-JUMP"] = {"pass": True, "boundaries_checked": 0, "note": "no affected ids / window rows", "blocking": True}
        obs = None
    # ---- G-DIV on new dividend steps
    prev_dv = pd.read_parquet(prev.raw_divadj) if prev.raw_divadj.exists() else None
    new_dv = pd.read_parquet(out.raw_divadj) if out.raw_divadj.exists() else None
    if new_dv is not None:
        steps = new_dividend_steps(prev_dv, new_dv)
        if len(steps) and ctx.asof is not None:
            n_future = int((pd.to_datetime(steps.date) > ctx.asof).sum())
            steps = steps[pd.to_datetime(steps.date) <= ctx.asof]          # R-D2 P2-1: a step whose ex-date is after asOf is checked the day it has a close
        else:
            n_future = 0
        if len(steps):
            step_ids = set(int(i) for i in steps.tradingItemId.unique())
            obs_d = ObservedSubset(out.base_data, out.sidecar if out.sidecar.exists() else None, step_ids, split, div)
            divs = pd.concat([ctx.dbc().query(f"""select dividendid, tradingitemid, exdate, divamount, currencyid, dividendtypeid, supplementaltypeid
                                                   from {SCHEMA}.ciqdividend where tradingitemid = any(%s)""", (chunk,)) for chunk in chunked(sorted(step_ids), 1000)],
                             ignore_index=True)
            divs["divamount"] = divs.divamount.astype(float)
            dsum = pqa.DividendSums(divs, split)
            r = pqa.div_step_identity(steps.tradingItemId.to_numpy(np.int64), steps.date.to_numpy().astype("datetime64[ns]"), steps.f_prev.to_numpy(),
                                      steps.f_new.to_numpy(), obs_d, dsum, split)
            r["contiguous"] = steps.contiguous.to_numpy()
            r["absdiff"] = r["diff"].abs()
            base = r[r.has_prev]
            clean = base[(base.n_noncash == 0) & (base.n_nonusd == 0) & base.contiguous]
            mism = clean[clean.absdiff > 1e-6]
            rounding = (mism.f_decimals <= 5) & (mism.absdiff <= 2e-5)
            uncorrected_ok = mism.split_between & (mism.diff_uncorrected.abs() <= 1e-6)
            genuine = mism[~rounding & ~uncorrected_ok]
            syn = pqa.synthetic_div_check(split)
            G["G-DIV"] = {"pass": bool(len(genuine) <= args.allow_new_div_mismatch), "new_steps": int(len(steps)), "with_prev_close": int(len(base)), "tested": int(len(clean)),
                          "matched_1e-6": int((clean.absdiff <= 1e-6).sum()), "mismatched": int(len(mism)), "explained_rounding": int(rounding.sum()),
                          "explained_uncorrected_split_form": int(uncorrected_ok.sum()), "genuine": int(len(genuine)),
                          "excluded": {"noncash_same_window": int((base.n_noncash > 0).sum()), "nonusd": int(((base.n_noncash == 0) & (base.n_nonusd > 0)).sum()),
                                       "gap_steps": int(((base.n_noncash == 0) & (base.n_nonusd == 0) & ~base.contiguous).sum())},
                          "genuine_examples": genuine.head(20)[["tradingItemId", "date", "step", "implied", "diff", "D_usd", "P_prev", "n_cash"]].to_dict("records"),
                          "steps_after_asof_deferred": n_future, "synthetic": syn, "blocking": args.strict_div}
            if len(genuine):
                genuine.to_csv(out.t2_qa / f"gdiv_new_step_mismatches_{ctx.observed:%Y-%m-%d}.csv", index=False)
        else:
            G["G-DIV"] = {"pass": True, "new_steps": 0, "steps_after_asof_deferred": n_future, "blocking": args.strict_div}
    else:
        G["G-DIV"] = {"pass": True, "note": "no raw_divadj file", "blocking": False}
    # ---- G-RAW (window)
    if ctx.raw_gate:
        G["G-RAW"] = {"pass": bool(ctx.raw_gate["mismatch_rate"] <= args.raw_mismatch_max), **ctx.raw_gate, "blocking": True}
    else:
        G["G-RAW"] = {"pass": True, "note": "U2 not run", "blocking": False}
    # ---- publish gate: every base_data id has a lifecycle row; lifecycle ids == instruments
    lc = pd.read_parquet(out.master_file("lifecycle"), columns=["tradingItemId"])
    inst = pd.read_parquet(out.master_file("instruments"), columns=["tradingItemId"])
    pf = pq.ParquetFile(out.base_data)
    bids = set()
    for rg in range(pf.num_row_groups):
        bids.update(np.unique(pf.read_row_group(rg, columns=["tradingItemId"])["tradingItemId"].to_numpy()).tolist())
    lids = set(int(i) for i in lc.tradingItemId)
    iids = set(int(i) for i in inst.tradingItemId)
    aids = set(int(i) for i in ctx.aux_ids)
    G["publish"] = {"pass": bool(not (bids - lids) and lids == (iids | aids)), "base_ids": len(bids), "base_ids_without_lifecycle": len(bids - lids),
                    "lifecycle_ids": len(lids), "instruments_ids": len(iids), "aux_ids": len(aids), "lifecycle_vs_instruments_plus_aux_diff": len(lids ^ (iids | aids)),
                    "base_ids_not_instruments_or_aux": len(bids - iids - aids), "blocking": True}
    # ---- returns sanity (only when U5 rebuilt returns from this base_data; a carried published returns file is lifecycle-masked)
    if out.returns.exists() and "U5" in args.steps:
        n_ret = pq.ParquetFile(out.returns).metadata.num_rows
        n_close = 0
        for rg in range(pf.num_row_groups):
            c = pf.read_row_group(rg, columns=["close"])["close"]
            n_close += c.length() - c.null_count - int(pc.sum(pc.is_nan(c)).as_py() or 0)
        G["returns"] = {"pass": n_ret == n_close, "returns_rows": int(n_ret), "close_notna_rows": int(n_close), "blocking": True}
    # ---- master gates from U4
    u4 = ctx.results["steps"].get("U4", {})
    if u4:
        G["master"] = {"pass": bool(u4["master_gates"]["symbol_set_consistent"] and u4["master_gates"]["self_tests"] and u4["lifecycle"]["self_test_all_pass"]
                                    and all(v == 0 for k, v in u4["master_gates"]["G2"].items() if k in ("classification_history", "primary_history", "id_mapping_nonconflict_rows"))),
                       "detail": u4["master_gates"], "blocking": True}
    # ---- rc2f gates (D2)
    u5 = ctx.results["steps"].get("U5", {})
    for r in ctx.price_win.itertuples(index=False):          # price overrides with no fresh row today: carried (stored rows hold them; G-PRICE-OVR checks)
        if r.overrideId not in ctx.alog.rows:
            ctx.alog.set(r.overrideId, caseId=r.caseId, kind="price", action=r.op, status="carried", reason="stored rows carry the override (no fresh row in the window today)",
                         affected_rows=0, target=str(r.tradingItemId), file="T2/base_data.parquet")
    ctx.ovr_cmp = dr.compare_apply_logs(ctx.alog.frame(), ctx.prev_alog)
    n_issues = len(ctx.ovr_issues)                                   # R-D2 P2-3: an override CSV that does not parse is never ignored
    G["G-OVR"] = {"pass": bool(ctx.ovr_cmp["pass"]) and n_issues == 0, "rows": ctx.ovr_cmp["rows"], "status": ctx.ovr_cmp["status"], "rejected": len(ctx.ovr_cmp["rejected"]),
                  "regressions": len(ctx.ovr_cmp["regressions"]), "status_changed": len(ctx.ovr_cmp["status_changed"]), "new_ids": len(ctx.ovr_cmp["new_ids"]),
                  "missing_from_current": len(ctx.ovr_cmp["missing_from_current"]), "prev_log": ctx.ovr_cmp["prev_available"], "csv_issues": n_issues,
                  "examples": (ctx.ovr_cmp["regressions"] + ctx.ovr_cmp["rejected"] + ctx.ovr_cmp["status_changed"] + ctx.ovr_issues)[:20], "blocking": True}
    G["G-PRICE-OVR"] = {**dr.price_override_integrity(out.base_data, ctx.price_win, read_rows_filtered), "blocking": True}
    G["G-PRICE-OVR"]["examples"] = G["G-PRICE-OVR"].pop("bad_examples", [])
    G["G-PRICE-OVR"].pop("per_override", None)
    qa = u5.get("returns_qa")
    if qa is not None:
        G["G-RETQA"] = {"pass": bool(qa.get("pass")), "gates": qa.get("gates"), "G-VENDOR": qa.get("G-VENDOR"), "G-SPLIT_v2": qa.get("G-SPLIT"), "G-LINK": qa.get("G-LINK"),
                        "G-NAN": qa.get("G-NAN"), "G-ORACLE": qa.get("G-ORACLE"), "retFlag": qa.get("retFlag"), "seconds": qa.get("seconds"), "blocking": True}
    attr = u5.get("returns_attribution")
    if attr is not None:
        G["G-RET-ATTR"] = {"pass": bool(attr.get("pass")), "cells_changed": attr.get("cells_changed"), "unattributed": attr.get("unattributed"), "by_kind": attr.get("by_kind"),
                           "by_attribution": attr.get("by_attribution"), "ledger_rows_changed": attr.get("ledger_rows_changed"), "lineage_pairs_changed": attr.get("lineage_pairs_changed"),
                           "examples": attr.get("unattributed_examples", [])[:20], "blocking": True}
        oc = attr.get("outliers") or {}
        if oc:
            G["G-RET-OUTLIER"] = {"pass": bool(oc.get("pass")), "new_historical_outliers": oc.get("new_historical_outliers"), "max_abs": oc.get("max_abs"),
                                  "new_date_outliers": oc.get("new_date_outliers"), "new_date_abs_gt_max": oc.get("new_date_abs_gt_max"), "outliers_total": oc.get("outliers_total"),
                                  "outliers_prev_total": oc.get("outliers_prev_total"), "examples": oc.get("examples", [])[:20], "new_date_examples": oc.get("new_date_examples", [])[:20], "blocking": True}
    # R-D2 P0-1 (2): raw jump x parentFactor at every split boundary added/changed today
    cal_df_ = pd.read_parquet(out.calendar) if out.calendar.exists() else None
    if cal_df_ is not None:
        fj = dr.factor_boundary_jumps(sp, ctx.factor_revisions, out.base_data, dr.calendar_from_frame(cal_df_), ctx.asof if ctx.asof is not None else ctx.prev_asof)
        G["G-FACTOR-JUMP"] = {**fj, "blocking": True}
    # R-D2 P0-2: events of the day = previous events + delta; the delta and every not-adopted T6 difference are listed
    fin = u5.get("finalize", {})
    em = fin.get("events_merge", {})
    if em:
        n_delta_rows = int(em.get("adopted_changed", 0) + em.get("adopted_new", 0) + em.get("dropped", 0))
        G["G-EVENTS"] = {"pass": n_delta_rows <= args.events_delta_max, "delta_rows_adopted": n_delta_rows, "adopted_changed": em.get("adopted_changed"), "adopted_new": em.get("adopted_new"),
                         "adopted_same": em.get("adopted_same"), "dropped": em.get("dropped"), "not_adopted_t6_differences": em.get("not_adopted"), "delta_ids": fin.get("events_delta", {}).get("delta_n"),
                         "unconditioned_prev": em.get("unconditioned_prev"), "unconditioned_new": em.get("unconditioned_new"), "unconditioned_fresh_t6": em.get("unconditioned_fresh_t6"),
                         "max": args.events_delta_max, "transitions_csv": str(out.t6 / "qa" / "events_transitions.csv"), "blocking": True}
    # R-D2 P0-2 (3): the same distribution leg on the old id and its linked new id within a few bars = double count
    if out.ledger.exists() and out.master_file("lineage").exists():
        dl = dr.double_leg_check(pd.read_parquet(out.ledger, columns=["eventId", "tradingItemId", "exDate", "legType", "childTradingItemId", "childRatio", "source"]),
                                 pd.read_parquet(out.master_file("lineage")), pd.read_parquet(out.master_file("lifecycle"), columns=["tradingItemId", "lastTradeDate"]), ctx.calendar)
        G["G-DOUBLE-LEG"] = {**dl, "blocking": True}
    u3 = ctx.results["steps"].get("U3", {})
    ri = u3.get("raw_invariance", {}) if u3 else {}
    if ri:
        kept = [r for r in ri.get("by_id", []) if r.get("policy") == "kept_stored"]
        G["G-RAWINV"] = {"pass": len(kept) == 0, "violations": ri.get("violations", 0), "ids": len(ri.get("by_id", [])), "kept_ids": len(kept), "vwap_only_ids": ri.get("ids_vwap_only", 0),
                         "accepted_ids": len([r for r in ri.get("by_id", []) if str(r.get("policy", "")).startswith("accepted")]), "rows_in_price_override_windows_skipped": ri.get("rows_in_price_override_windows_skipped"),
                         "examples": [{k: r.get(k) for k in ("tradingItemId", "cells", "fields", "dateMin", "dateMax", "ratioMin", "ratioMax", "policy")} for r in ri.get("by_id", [])[:20]],
                         "blocking": bool(args.strict_raw)}
    sf = u5.get("source_freshness", {})
    if sf:
        G["G-SOURCE"] = {"pass": not sf.get("stale", False), "co_acthist_max_datadate": sf.get("co_acthist_max_datadate"), "days_behind_asof": sf.get("days_behind_asof"),
                         "lag_days_max": sf.get("lag_days_max"), "blocking": False}
    rt = u5.get("returns_total", {})
    if rt:
        G["G-LEDGER"] = {"pass": bool(rt.get("ledger") and (not rt.get("ledger_stale") or args.allow_stale_ledger)), "ledger_asof": rt.get("ledger_asof"), "build_asof": str(ctx.asof.date()) if ctx.asof is not None else None,
                         "stale": rt.get("ledger_stale"), "stale_after": rt.get("stale_after"), "sha256": rt.get("ledger_sha256"), "blocking": True}
    # ---- pre-publish engine export (display rows) + exposure summary
    try:
        view = dr.build_stage_view(ctx)
        from loader import CacheV21
        out.engine.mkdir(parents=True, exist_ok=True)
        cv = CacheV21(view, master_root=view, threads=args.threads)
        m = cv.export_ca_ledger(out.engine_export, ledger_path=out.ledger, events_path=out.events)
        (out.engine / "ca_ledger_cache_manifest.json").write_text(json.dumps(m, indent=1, default=str))
        me = cv.export_events(out.engine / "events_cache.parquet", events_path=out.events)
        (out.engine / "events_cache_manifest.json").write_text(json.dumps(me, indent=1, default=str))
        ctx.rc2f["engine_export_stage"] = {"ca_ledger_rows": m.get("rows"), "marker_rows": m.get("marker_rows", {}).get("exported"), "marker_by_reason": m.get("marker_rows", {}).get("by_reason"),
                                           "value_legs_no_child_id_valued": m.get("value_legs_no_child_id_valued"), "events_cache_rows": me.get("rows"), "events_status": me.get("status")}
    except Exception as exc:  # noqa: BLE001
        ctx.warn(f"pre-publish engine export failed: {type(exc).__name__}: {exc}")
        ctx.rc2f["engine_export_stage"] = {"error": f"{type(exc).__name__}: {exc}"}
    if out.returns.exists() and out.events.exists() and ctx.asof is not None:
        ev_now = pd.read_parquet(out.events)
        ev_prev = pd.read_parquet(prev.events) if prev.events.exists() else None
        ex = dr.exposure_daily(ctx, ev_prev, ev_now, prev.engine_export if prev.engine_export.exists() else None, out.engine_export if out.engine_export.exists() else None, args.book_glob)
        ctx.rc2f["exposure"] = ex
        G["G-EXPO"] = {"pass": bool(ex["pass"]), "warning": ex["warning"], "asof_flag3": ex["asof_flag3"], "asof_flag4": ex["asof_flag4"], "asof_nan": ex["asof_nan"],
                       "book_hits": ex["book"]["hits"], "book_hit_kinds": ex["book"]["hit_kinds"], "new_unconditioned_settlements": ex["unconditioned_settlements"]["new_vs_prev"],
                       "new_display_rows": ex["display_rows"]["new_vs_prev"], "blocking": bool(ex["blocking"])}
        if ex["warning"]:
            ctx.warn(f"exposure: {ex['book']['hits']} live-book hits ({ex['book']['hit_kinds']}) - see qa/exposure_daily.md")
    blocking_fail = [k for k, v in G.items() if v.get("blocking") and not v.get("pass")]
    G["summary"] = {"all_blocking_pass": not blocking_fail, "blocking_failed": blocking_fail,
                    "non_blocking_failed": [k for k, v in G.items() if k != "summary" and not v.get("blocking") and not v.get("pass")]}
    ctx.results["gates"] = pqa.to_native(G)
    write_gates_md(ctx)
    ctx.time_step("U6", t0, blocking_failed=blocking_fail)
    if blocking_fail:
        raise UpdateAbort(7, f"gates failed: {blocking_fail}")


def write_gates_md(ctx: Ctx):
    G = ctx.results["gates"]
    L = [f"# update_daily gates - asOf {ctx.asof.date() if ctx.asof is not None else '?'} (observed {ctx.observed.date()}, build {ctx.build_id})", "",
         "| gate | pass | blocking | key numbers |", "|---|---|---|---|"]
    for k, v in G.items():
        if k == "summary":
            continue
        nums = {kk: vv for kk, vv in v.items() if kk not in ("pass", "blocking", "examples", "new_examples", "genuine_examples", "mismatch_examples", "detail", "synthetic", "asof_flags", "flags_window", "excluded", "gates", "G-VENDOR", "G-SPLIT_v2", "G-LINK", "G-NAN", "G-ORACLE", "retFlag", "by_attribution", "by_kind", "book_hit_kinds", "status")}
        L.append(f"| {k} | {v.get('pass')} | {v.get('blocking')} | {json.dumps(nums, default=str)[:400]} |")
    L += ["", f"summary: {json.dumps(G.get('summary'), default=str)}", ""]
    for k in ("G-JUMP", "G-DIV", "G-RAW", "G-SPLIT", "G-OVR", "G-PRICE-OVR", "G-RET-ATTR", "G-RAWINV"):
        v = G.get(k, {})
        ex = v.get("new_examples") or v.get("genuine_examples") or v.get("mismatch_examples") or v.get("examples")
        if ex:
            L += [f"## {k} examples", "", md_table(ex), ""]
        if v.get("synthetic"):
            L += [f"{k} synthetic counterexample: {json.dumps(v['synthetic'], default=str)}", ""]
    (ctx.out.t2_qa / "update_gates.md").write_text("\n".join(L))


# ======================================================================================
# U7 publish
# ======================================================================================
def u7_publish(ctx: Ctx):
    t0 = time.time()
    out, args = ctx.out, ctx.args
    pub_out = Path(args.publish_out) if args.publish_out else out.root.parent / "published" / ctx.build_id
    ctx.write_apply_log()
    view, rc2 = dr.build_publish_view(ctx, out.rc2f / "rc2f_apply_log.csv", out.rc2f / "rc2f_apply_info.json")
    cmd = [sys.executable, str(HERE / "publish.py"), "--stage", str(view), "--t2", str(out.t2), "--rc2-stage", str(rc2), "--out", str(pub_out), "--build-id", ctx.build_id,
           "--orch", args.orch, "--orch-rc2", args.orch_rc2, "--orch-rc2f", args.orch_rc2f]
    if getattr(args, "returns_legacy", False):
        cmd.append("--returns-legacy")
    else:
        led = Path(args.ledger) if getattr(args, "ledger", None) else out.ledger
        cmd += ["--ledger", str(led)]
        lin = Path(args.lineage) if getattr(args, "lineage", None) else out.master_file("lineage")
        if lin.exists():
            cmd += ["--lineage", str(lin)]
        if getattr(ctx, "ledger_stale_after", None) is not None:
            cmd += ["--ledger-stale-after", f"{ctx.ledger_stale_after:%Y-%m-%d}"]
    if args.no_proto_tables:
        cmd.append("--no-proto-tables")
    r = run_cmd(cmd, out.cmd_log, timeout_s=3600)                 # local build only; the remote sync runs after the anchor gate (R-D2 P1-3)
    res = {"publish_out": str(pub_out), "remote": args.remote or None, **r}
    if r["rc"] == 0 and (pub_out / "manifest.json").exists():
        man = json.loads((pub_out / "manifest.json").read_text())
        res["manifest"] = {"build_id": man.get("build_id"), "asOfDate": man.get("asOfDate"), "files": len(man.get("files", {})),
                           "bytes_total": int(sum(f["bytes"] for f in man.get("files", {}).values()))}
        for name in ("ca_ledger_cache", "events_cache"):
            mp = pub_out / "engine" / f"{name}_manifest.json"
            if mp.exists():
                m = json.loads(mp.read_text())
                res[f"engine/{name}"] = {k: m.get(k) for k in ("rows", "sha256", "marker_rows", "status", "value_legs_no_child_id_valued") if k in m}
        # stale-anchor check of the regenerated engine exports against the published panel (the C++ loader refuses a stale file)
        t1 = time.time()
        anc = dr.anchor_check(pub_out, read_rows_filtered)
        anc["seconds"] = round(time.time() - t1, 1)
        res["anchor_check"] = anc
        ctx.results["gates"]["G-ANCHOR"] = {"pass": bool(anc.get("pass")), **{k: v for k, v in anc.items() if k != "pass"}, "blocking": True}
        ctx.rc2f["publish"] = {"out": str(pub_out), "engine": {k: v for k, v in res.items() if k.startswith("engine/")}, "anchor_check": anc}
    ctx.results["steps"]["U7"] = res
    ctx.time_step("U7", t0, rc=r["rc"])
    if r["rc"] != 0:
        raise UpdateAbort(8, f"publish.py failed (rc {r['rc']}), see {out.cmd_log}")
    if not res.get("anchor_check", {}).get("pass", True):
        raise UpdateAbort(8, f"engine export anchors do not match the published panel: {res['anchor_check']}")
    if args.remote:
        t1 = time.time()
        rs = run_cmd([sys.executable, str(HERE / "publish.py"), "--sync-only", "--out", str(pub_out), "--remote", args.remote], out.cmd_log, timeout_s=3600)
        res["sync"] = {**rs, "seconds": round(time.time() - t1, 1)}
        ctx.results["steps"]["U7"] = res
        if rs["rc"] != 0:
            raise UpdateAbort(8, f"remote sync failed (rc {rs['rc']}), local build stays at {pub_out}; see {out.cmd_log}")


# ======================================================================================
# build comparison (trials / operations): value-level diff of two builds
# ======================================================================================
def compare_long(a_path: Path, b_path: Path, key_cols=("tradingItemId", "date"), ignore=()) -> dict:
    """Two (id, date)-sorted long parquet files -> rows only in A/B, and per-column count of differing common rows
    (NaN == NaN; floats compared bit-exactly after casting to float64)."""
    ta, tb = pq.read_table(a_path), pq.read_table(b_path)
    ka = row_keys(ta[key_cols[0]].to_numpy(), ta[key_cols[1]].to_numpy())
    kb = row_keys(tb[key_cols[0]].to_numpy(), tb[key_cols[1]].to_numpy())
    res = {"rows_a": ta.num_rows, "rows_b": tb.num_rows, "sha256_equal": sha256(a_path) == sha256(b_path)}
    if np.array_equal(ka, kb):
        ia = ib = np.arange(len(ka))
        res["rows_only_a"] = res["rows_only_b"] = 0
    else:
        common = np.intersect1d(ka, kb)
        ia = np.searchsorted(ka, common)
        ib = np.searchsorted(kb, common)
        res["rows_only_a"], res["rows_only_b"] = int(len(ka) - len(common)), int(len(kb) - len(common))
    diff = {}
    for c in ta.column_names:
        if c in key_cols or c in ignore or c not in tb.column_names:
            continue
        x = ta[c].to_numpy(zero_copy_only=False)[ia]
        y = tb[c].to_numpy(zero_copy_only=False)[ib]
        if x.dtype.kind in "fc" or y.dtype.kind in "fc":
            x, y = x.astype(np.float64), y.astype(np.float64)
            d = ~((np.isnan(x) & np.isnan(y)) | (x == y))
        elif x.dtype.kind == "M" or y.dtype.kind == "M":
            x, y = x.astype("datetime64[ns]"), y.astype("datetime64[ns]")
            d = ~((np.isnat(x) & np.isnat(y)) | (x == y))
        else:
            d = x != y
        n = int(d.sum())
        if n:
            j = np.flatnonzero(d)[:3]
            diff[c] = {"rows": n, "examples": [{"tradingItemId": int(ta[key_cols[0]][int(ia[k])].as_py()), "date": str(pd.Timestamp(ta[key_cols[1]][int(ia[k])].as_py()).date()),
                                                "a": str(x[k]), "b": str(y[k])} for k in j]}
    res["columns_differing"] = diff
    res["common_rows_identical"] = not diff
    return res


def compare_frames(a: pd.DataFrame, b: pd.DataFrame, key: list[str], ignore=()) -> dict:
    """Generic keyed comparison (factors, master tables): rows only in A/B and differing common rows per column."""
    ka = a[key].astype(str).agg("|".join, axis=1)
    kb = b[key].astype(str).agg("|".join, axis=1)
    a2 = a.assign(_k=ka).drop_duplicates("_k").set_index("_k")
    b2 = b.assign(_k=kb).drop_duplicates("_k").set_index("_k")
    common = a2.index.intersection(b2.index)
    res = {"rows_a": int(len(a)), "rows_b": int(len(b)), "rows_only_a": int(len(a2) - len(common)), "rows_only_b": int(len(b2) - len(common)),
           "dup_keys_a": int(len(a) - len(a2)), "dup_keys_b": int(len(b) - len(b2))}
    diff = {}
    for c in a.columns:
        if c in key or c in ignore or c not in b.columns:
            continue
        x, y = a2.loc[common, c], b2.loc[common, c]
        if str(x.dtype).startswith("datetime") or str(y.dtype).startswith("datetime"):
            x, y = pd.to_datetime(x), pd.to_datetime(y)
            d = ~((x.isna() & y.isna()) | (x == y))
        elif x.dtype.kind in "fc" or y.dtype.kind in "fc":
            xv, yv = x.to_numpy(dtype=np.float64), y.to_numpy(dtype=np.float64)
            d = pd.Series(~((np.isnan(xv) & np.isnan(yv)) | (xv == yv)), index=common)
        else:
            d = ~((x.isna() & y.isna()) | (x.astype(str) == y.astype(str)))
        n = int(d.sum())
        if n:
            diff[c] = {"rows": n, "examples": [{"key": k, "a": str(a2.at[k, c]), "b": str(b2.at[k, c])} for k in common[d.to_numpy()][:3]]}
    res["columns_differing"] = diff
    res["common_rows_identical"] = not diff
    return res


def compare_builds(a_root: Path, b_root: Path) -> dict:
    A, B = PrevBuild(a_root), PrevBuild(b_root)
    out = {"a": str(A.root), "b": str(B.root)}
    for name, pa_, pb_ in (("base_data", A.base_data, B.base_data), ("side table (adjustment_factor_daily)", A.sidecar, B.sidecar), ("returns_total", A.returns, B.returns)):
        if pa_.exists() and pb_.exists():
            r = compare_long(pa_, pb_)
            r["bytes_a"], r["bytes_b"] = pa_.stat().st_size, pb_.stat().st_size
            out[name] = r
        else:
            out[name] = {"missing": [str(p) for p in (pa_, pb_) if not p.exists()]}
    for name, pa_, pb_ in (("factors/split", A.split, B.split), ("factors/dividend", A.dividend, B.dividend)):
        if pa_.exists() and pb_.exists():
            r = compare_frames(pd.read_parquet(pa_), pd.read_parquet(pb_), ["tradingItemId", "validFrom"], ignore=("snapshotDate",))
            r["sha256_equal"] = sha256(pa_) == sha256(pb_)
            out[name] = r
    keyed = {"instruments": ["tradingItemId"], "current": ["tradingItemId"], "lifecycle": ["tradingItemId"], "lineage": ["oldId", "newId"],
             "tradable_exclusions": ["tradingItemId", "fromDate"], "classification_history": ["tradingItemId", "effectiveFrom"],
             "primary_history": ["tradingItemId", "effectiveFrom"], "id_mapping": ["tradingItemId", "gvkey", "iid", "symbolStartDate"],
             "identifier_history": ["tradingItemId", "gvkey", "iid", "item", "value", "effFrom"]}
    for name, key in keyed.items():
        pa_, pb_ = A.master_file(name), B.master_file(name)
        if pa_.exists() and pb_.exists():
            ign = ("asOf", "observedAt", "evidence") if name in ("current", "lifecycle") else ("observedAt",)
            out[f"master/{name}"] = compare_frames(pd.read_parquet(pa_), pd.read_parquet(pb_), key, ignore=ign)
    for name, pa_, pb_ in (("universe/membership", A.universe, B.universe), ("derived/marketcap", A.marketcap, B.marketcap), ("events", A.events, B.events)):
        if pa_.exists() and pb_.exists():
            out[name] = {"rows_a": pq.ParquetFile(pa_).metadata.num_rows, "rows_b": pq.ParquetFile(pb_).metadata.num_rows, "sha256_equal": sha256(pa_) == sha256(pb_)}
    return out


def compare_md(res: dict) -> str:
    rows = []
    for k, v in res.items():
        if k in ("a", "b"):
            continue
        if "missing" in v:
            rows.append({"file": k, "note": "missing: " + ", ".join(v["missing"])})
            continue
        diff = v.get("columns_differing", {})
        rows.append({"file": k, "rows_a": v.get("rows_a"), "rows_b": v.get("rows_b"), "only_a": v.get("rows_only_a", ""), "only_b": v.get("rows_only_b", ""),
                     "sha256_equal": v.get("sha256_equal", ""), "common_identical": v.get("common_rows_identical", ""),
                     "differing_columns": ", ".join(f"{c}:{d['rows']}" for c, d in diff.items()) if diff else ""})
    return md_table(rows, ["file", "rows_a", "rows_b", "only_a", "only_b", "sha256_equal", "common_identical", "differing_columns"])


def file_manifest(ctx: Ctx) -> dict:
    """Contract files of the new build: bytes, sha256, and whether they are byte-identical to the previous build
    (the 'transfer volume' of the day = bytes of files that changed)."""
    out, prev = ctx.out, ctx.prev
    pairs = [("T2/base_data.parquet", out.base_data, prev.base_data), ("T2_work/adjf_long.parquet (= factors/adjustment_factor_daily.parquet)", out.sidecar, prev.sidecar),
             ("calendar.parquet", out.calendar, prev.calendar),
             ("T2/factors/split.parquet", out.split, prev.split), ("T2/factors/dividend.parquet", out.dividend, prev.dividend),
             ("T2/derived/returns_total.parquet", out.returns, prev.returns), ("T5/derived/marketcap.parquet", out.marketcap, prev.marketcap),
             ("T5/universe/membership.parquet", out.universe, prev.universe), ("T6/events/corporate_actions.parquet", out.events, prev.events)]
    for name in ("instruments", "current", "classification_history", "primary_history", "id_mapping", "identifier_history", "lifecycle", "lineage", "tradable_exclusions",
                 "lifecycle_dates", "aux_instruments", "lifecycle_aux"):
        pairs.append((f"T1/master/{name}.parquet", out.master_file(name), prev.master_file(name)))
    pairs += [("T6/events/ca_ledger.parquet", out.ledger, prev.ledger), ("T6/engine/ca_ledger_cache.parquet", out.engine_export, prev.engine_export),
              ("T6/engine/events_cache.parquet", out.engine / "events_cache.parquet", prev.engine_export.with_name("events_cache.parquet")),
              ("T2_work/split_vendor.parquet", out.split_vendor, prev.t2_work / "split_vendor.parquet")]
    files, changed_bytes, total_bytes = {}, 0, 0
    for rel, p, q in pairs:
        if not p.exists():
            continue
        h = sha256(p)
        same = q.exists() and q.stat().st_size == p.stat().st_size and sha256(q) == h
        files[rel] = {"bytes": p.stat().st_size, "sha256": h[:16], "same_as_prev": bool(same)}
        total_bytes += p.stat().st_size
        if not same:
            changed_bytes += p.stat().st_size
    arch = sum(f.stat().st_size for f in (out.archive / "revisions" / f"date={ctx.observed:%Y-%m-%d}").glob("*")) if (out.archive / "revisions" / f"date={ctx.observed:%Y-%m-%d}").exists() else 0
    # rc2f (D2) contract summary: ledger asOf / sha, override file shas, links verified, display rows, revisions of the day
    rc2f = {"ledger_asof": None, "ledger_sha256": None, "overrides": [{"file": Path(f["path"]).parent.name + "/" + Path(f["path"]).name, "sha256": f["sha256"], "rows": f["rows"]} for f in ctx.ovr_files],
            "overrides_sha256_all": hashlib.sha256("".join(sorted(f["sha256"] for f in ctx.ovr_files)).encode()).hexdigest()[:16] if ctx.ovr_files else None,
            "links_verified": None, "display_rows": None, "revisions": {}}
    if out.ledger.exists():
        rc2f["ledger_sha256"] = files.get("T6/events/ca_ledger.parquet", {}).get("sha256")
        la = ledger_asof(out.ledger)
        rc2f["ledger_asof"] = str(la.date()) if la is not None else None
    if out.master_file("lineage").exists():
        ln = pd.read_parquet(out.master_file("lineage"))
        rc2f["links_verified"] = int(ln.linkVerified.sum()) if "linkVerified" in ln.columns else 0
        rc2f["links_chainable"] = int(ln.chainable.sum()) if "chainable" in ln.columns else None
    mp = out.engine / "ca_ledger_cache_manifest.json"
    if mp.exists():
        m = json.loads(mp.read_text())
        rc2f["display_rows"] = m.get("marker_rows", {}).get("exported")
    rd = out.archive / "revisions" / f"date={ctx.observed:%Y-%m-%d}"
    if rd.exists():
        for f in sorted(rd.glob("*.parquet")):
            try:
                rc2f["revisions"][f.name] = int(pq.ParquetFile(f).metadata.num_rows)
            except Exception:  # noqa: BLE001
                rc2f["revisions"][f.name] = None
    lg = ctx.alog.frame()
    rc2f["apply_log"] = {"rows": int(len(lg)), "status": lg.status.value_counts().to_dict() if len(lg) else {}}
    ctx.rc2f["manifest"] = rc2f
    return {"files": files, "bytes_total": total_bytes, "bytes_changed_vs_prev": changed_bytes, "bytes_revisions_today": arch, "rc2f": rc2f}


# ======================================================================================
# self-test (no DB): every detector must fail on a planted counterexample and pass on clean data
# ======================================================================================
class UpdateAbort(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


def self_test() -> list[dict]:
    res = []
    T = pd.Timestamp

    def rec(name, ok, detail=""):
        res.append({"case": name, "ok": bool(ok), "detail": detail})

    dates = pd.to_datetime(["2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15"])
    prev = pd.DataFrame({"tradingItemId": np.repeat([1, 2], 5), "date": np.tile(dates, 2),
                         "open": np.r_[np.linspace(10, 11, 5), np.linspace(20, 21, 5)].astype(np.float32), "high": 0.0, "low": 0.0,
                         "close": np.r_[np.linspace(10, 11, 5), np.linspace(20, 21, 5)].astype(np.float32), "vwap": 0.0,
                         "volume": np.full(10, 1000.0), "source": np.int8(1), "qualityFlag": np.int8(0)})
    prev["high"] = prev.close * np.float32(1.01)
    prev["low"] = prev.close * np.float32(0.99)
    prev["vwap"] = prev.close
    # (1) identical frames -> no revision; one close x1.01 -> exactly one revision (field close), row flagged changed
    new = prev.copy()
    rev, changed, st = compare_rows(prev, new, T("2026-09-16"))
    rec("compare: identical -> 0 revisions", len(rev) == 0 and changed.sum() == 0, st)
    new2 = prev.copy()
    new2.loc[3, "close"] = np.float32(new2.loc[3, "close"] * 1.01)
    rev, changed, st = compare_rows(prev, new2, T("2026-09-16"))
    rec("compare: planted close change -> 1 revision (close), 1 changed row", len(rev) == 1 and rev.field.iloc[0] == "close" and changed.sum() == 1 and changed[3], rev.to_dict("records"))
    new3 = prev.copy()
    new3.loc[3, "close"] = np.float32(new3.loc[3, "close"] * (1 + 1e-8))
    rev, changed, _ = compare_rows(prev, new3, T("2026-09-16"))
    rec("compare: 1e-8 relative change is below the revision tolerance", len(rev) == 0)
    # (2) vendor removed / late row
    new4 = prev.drop(index=4).reset_index(drop=True)
    late = pd.DataFrame([{**prev.iloc[9].to_dict(), "date": T("2026-09-08"), "tradingItemId": 2}])
    new4 = pd.concat([new4, late], ignore_index=True).sort_values(["tradingItemId", "date"]).reset_index(drop=True)
    rev, changed, st = compare_rows(prev, new4, T("2026-09-16"), prev_asof=T("2026-09-15"))
    rec("compare: removed row + late row detected", st["removed_rows"] == 1 and st["late_rows"] == 1 and set(rev.reason) == {"vendor_removed", "vendor_late_row"}, st)
    new5 = pd.concat([prev, pd.DataFrame([{**prev.iloc[4].to_dict(), "date": T("2026-09-16")}])], ignore_index=True)
    rev, changed, st = compare_rows(prev, new5, T("2026-09-17"), prev_asof=T("2026-09-15"))
    rec("compare: a row on a new date is an append, not a revision", len(rev) == 0 and st["appended_rows"] == 1 and st["late_rows"] == 0, st)
    # (3) quality flags
    ids = np.array([1, 1, 1, 1, 1])
    d = pd.to_datetime(["2026-09-11", "2026-09-14", "2026-09-14", "2026-09-15", "2026-09-15"]).to_numpy()
    ids = np.array([1, 1, 2, 1, 2])
    close = np.array([10.0, 10.0, 20.0, 10.0, np.nan])
    mi = {int(row_keys([1], [d[0]])[0]): 10.0, int(row_keys([1], [d[1]])[0]): 10.05, int(row_keys([1], [d[3]])[0]): 10.0}
    q = quality_flags(ids, d, close, mi, T("2026-09-15"))
    rec("flags: match->0, pre-asOf mismatch->2, pre-asOf no mi->0, asOf match->0, null close->3", q.tolist() == [0, 2, 0, 0, 3], q.tolist())
    q2 = quality_flags(np.array([1, 1]), pd.to_datetime(["2026-09-15", "2026-09-15"]).to_numpy(), np.array([10.0, 10.0]),
                       {int(row_keys([1], [np.datetime64("2026-09-15")])[0]): 10.05}, T("2026-09-15"))
    q3 = quality_flags(np.array([3]), pd.to_datetime(["2026-09-15"]).to_numpy(), np.array([10.0]), {}, T("2026-09-15"))
    rec("flags: asOf mismatch -> 1 (provisional), asOf without mi -> 1", q2.tolist() == [1, 1] and q3.tolist() == [1], (q2.tolist(), q3.tolist()))
    # (4) asOf close-count threshold
    td = pd.DatetimeIndex(dates)
    cc = pd.Series([7000, 7000, 7000, 7000, 6200], index=td)
    p = pick_asof(cc, cc, td, T("2026-09-15"), T("2026-09-10"), None)
    rec("asOf auto: 6200 < 90% x 7000 -> falls back to 09-14", p["asOf"] == T("2026-09-14"), str(p["asOf"]))
    p2 = pick_asof(cc, cc, td, T("2026-09-15"), T("2026-09-10"), T("2026-09-15"))
    rec("asOf explicit 09-15 with 6200 closes -> not ok (abort)", not p2["ok"])
    cc2 = cc.copy()
    cc2.iloc[-1] = 6300
    p3 = pick_asof(cc2, cc2, td, T("2026-09-15"), T("2026-09-10"), None)
    rec("asOf auto: 6300 = 90% x 7000 -> 09-15 accepted", p3["asOf"] == T("2026-09-15"))
    # (5) factor diff
    fp = pd.DataFrame({"tradingItemId": [1, 1, 2], "validFrom": pd.to_datetime(["1900-01-01", "2020-01-01", "1900-01-01"]),
                       "validTo": pd.to_datetime(["2020-01-01", None, None]), "factor": [2.0, 1.0, 1.0], "source": ["synthetic_pre_first", "ciqsplitcache", "synthetic_no_window"]})
    fn = pd.DataFrame({"tradingItemId": [1, 1, 1, 2], "validFrom": pd.to_datetime(["1900-01-01", "2020-01-01", "2026-09-14", "1900-01-01"]),
                       "validTo": pd.to_datetime(["2020-01-01", "2026-09-14", None, None]), "factor": [4.0, 2.0, 1.0, 1.0],
                       "source": ["synthetic_pre_first", "ciqsplitcache", "ciqsplitcache", "synthetic_no_window"]})
    fd = factor_diff(fp, fn, "split", T("2026-09-16"))
    kinds = fd.groupby("kind").size().to_dict()
    rec("factor diff: new split -> 2 changed + 1 added, untouched id silent", kinds == {"added": 1, "changed": 2} and set(fd.tradingItemId) == {1}, kinds)
    rec("factor diff: identical tables -> 0 rows", len(factor_diff(fn, fn, "split", T("2026-09-16"))) == 0)
    # (6) restatement detectors
    sc_prev = pd.DataFrame({"tradingitemid": [1], "splitdate": ["2020-01-01"], "nextsplitdate": ["2079-06-05"], "cumulativesplitfactor": ["1.0"], "latestsplitfactor": ["2.0"]})
    sc_new = pd.DataFrame({"tradingitemid": [1, 1], "splitdate": ["2020-01-01", "2026-09-14"], "nextsplitdate": ["2026-09-14", "2079-06-05"],
                           "cumulativesplitfactor": ["0.5", "1.0"], "latestsplitfactor": ["2.0", "0.5"]})
    rec("splitcache window diff flags the id with a new window; identical -> none",
        splitcache_changed_ids(sc_prev, sc_new) == {1} and splitcache_changed_ids(sc_new, sc_new) == set())
    win_side = pd.DataFrame({"tradingItemId": [1, 1, 2], "date": pd.to_datetime(["2026-09-10", "2026-09-11", "2026-09-10"]), "adjustmentfactor": [0.5, 0.5, 1.0], "ciqClose": [20.0, 20.0, 5.0]})
    prev_side = pd.DataFrame({"tradingItemId": [1, 1, 2], "date": pd.to_datetime(["2026-09-10", "2026-09-11", "2026-09-10"]), "adjustmentfactor": [1.0, 1.0, 1.0], "ciqClose": [10.0, 10.0, 5.0]})
    lk = pfm.FactorLookup(pfm.build_intervals(pfm.split_windows(sc_new), [1, 2], pfm.SRC_SPLIT, T("2026-09-16"), pre_first="pre"))
    m = adjf_mismatch_ids(win_side, prev_side, lk)
    rec("adjf detector: stale side file -> id 1 (not 2); lookup agrees -> none", m["vs_side"] == {1} and m["vs_lookup"] == set(), {k: sorted(v) for k, v in m.items()})
    # (7) raw invariance
    stored = prev[prev.tradingItemId == 1].copy()
    refetched = stored.copy()
    side = pd.DataFrame({"tradingItemId": 1, "date": stored.date, "adjustmentfactor": 0.5, "ciqClose": stored.close.astype(float) * 2})
    v, st = raw_invariance(stored, refetched, side)
    refetched2 = stored.copy()
    refetched2.loc[refetched2.index[2], "close"] = np.float32(refetched2.loc[refetched2.index[2], "close"] * 1.001)
    v2, st2 = raw_invariance(stored, refetched2, side)
    refetched3 = stored.copy()
    refetched3["close"] = (refetched3.close.astype(np.float64) * (1 + 5e-8)).astype(np.float32)
    v3, _ = raw_invariance(stored, refetched3, side)
    rec("raw invariance: identical -> 0, 1e-3 change -> 1 violation, 5e-8 (float32 noise) -> 0", len(v) == 0 and len(v2) == 1 and len(v3) == 0, (st, st2))
    # (8) incremental G-JUMP with a shifted boundary (fixture from prices_returns.self_test)
    dts = pd.to_datetime(["2020-01-03", "2020-01-06", "2020-01-08", "2020-01-09", "2020-01-10"]).to_numpy().astype("datetime64[ns]")
    raw = np.array([100.0, 102.0, 52.0, 53.0, 52.5], dtype=np.float32)
    sc = pd.DataFrame({"tradingitemid": [1], "splitdate": ["2020-01-08"], "nextsplitdate": ["2079-06-05"], "cumulativesplitfactor": [1.0], "latestsplitfactor": [2.0]})
    good = pfm.build_intervals(pfm.split_windows(sc), [1], pfm.SRC_SPLIT, T("2026-09-16"), pre_first="pre")
    bad = good.copy()
    bad.loc[bad.validFrom == T("2020-01-08"), "validFrom"] = T("2020-01-09")
    bad.loc[bad.validTo == T("2020-01-08"), "validTo"] = T("2020-01-09")
    div = pfm.FactorLookup(pfm.build_intervals(pd.DataFrame(columns=["tid", "start", "end", "value"]), [1], pfm.SRC_DIV, T("2026-09-16")))

    class O:  # minimal Observed stand-in
        pass

    def make(split_df):
        o = O()
        o.ids = np.ones(5, dtype=np.int64)
        o.dates = dts
        o.close = raw
        o.close64 = raw.astype(np.float64)
        o.keys = row_keys(o.ids, o.dates)
        o.sf = pfm.FactorLookup(split_df)(o.ids, o.dates)
        o.df = div(o.ids, o.dates)
        o.prev = np.r_[-1, np.arange(4)]
        return o

    jg = jump_boundaries(make(good), None, None)
    jbd = jump_boundaries(make(bad), None, None)
    rec("G-JUMP incremental: correct boundary -> no fake; boundary shifted one day -> fake detected", (~jg.fake).all() and jbd.fake.any(), (jg.fake.tolist(), jbd.fake.tolist()))
    known = pd.DataFrame({"tradingItemId": [1], "date": [T("2020-01-09")]})
    kk = set(zip(known.tradingItemId.astype(int), pd.to_datetime(known.date)))
    fk = jbd[jbd.fake]
    rec("G-JUMP: known-artifact list filters the planted jump", all((int(t), pd.Timestamp(d)) in kk for t, d in zip(fk.tradingItemId, fk.date)))
    # (9) new dividend steps
    dv_prev = pd.DataFrame({"tradingitemid": [5, 5], "fromdate": ["2016-12-12", "2017-06-12"], "todate": ["2017-06-11", None], "divadjfactor": [0.8, 1.0]})
    dv_new = pd.DataFrame({"tradingitemid": [5, 5, 5], "fromdate": ["2016-12-12", "2017-06-12", "2026-09-14"], "todate": ["2017-06-11", "2026-09-13", None], "divadjfactor": [0.79, 0.99, 1.0]})
    ns = new_dividend_steps(dv_prev, dv_new)
    rec("G-DIV: only the new step (2026-09-14) is tested", len(ns) == 1 and ns.date.iloc[0] == T("2026-09-14") and abs(ns.f_prev.iloc[0] - 0.99) < 1e-12, ns.to_dict("records"))
    # (10) merge on a tiny two-group file: window replacement + full-id replacement + sort/unique proof
    import tempfile
    with tempfile.TemporaryDirectory() as tmpd:
        tmpd = Path(tmpd)
        base_prev = base_from_pandas(prev)
        side_prev = side_from_pandas(pd.DataFrame({"tradingItemId": prev.tradingItemId, "date": prev.date, "adjustmentfactor": 1.0, "ciqClose": prev.close.astype(float)}))
        w = pb.RowGroupWriter(tmpd / "base.parquet", pb.BASE_SCHEMA)
        w.write(base_prev.filter(pc.equal(base_prev["tradingItemId"], 1)))
        w.write(base_prev.filter(pc.equal(base_prev["tradingItemId"], 2)))
        w.close()
        ws = pb.RowGroupWriter(tmpd / "side.parquet", pb.ADJF_SCHEMA)
        ws.write(side_prev.filter(pc.equal(side_prev["tradingItemId"], 1)))
        ws.write(side_prev.filter(pc.equal(side_prev["tradingItemId"], 2)))
        ws.close()
        pend = Pending()
        win = prev[prev.date >= T("2026-09-14")].copy()
        win.loc[win.index[0], "close"] = np.float32(99.0)
        win["source"] = np.int8(2)
        newday = pd.DataFrame([{**prev.iloc[4].to_dict(), "date": T("2026-09-16"), "close": np.float32(12.0), "source": np.int8(1)}])
        win = pd.concat([win, newday], ignore_index=True)
        pend.add_window(base_from_pandas(win), side_from_pandas(pd.DataFrame({"tradingItemId": win.tradingItemId, "date": win.date, "adjustmentfactor": 1.0, "ciqClose": win.close.astype(float)})),
                        T("2026-09-14"), T("2026-09-16"))
        full = prev[prev.tradingItemId == 2].copy()
        full = full[full.date != T("2026-09-09")]      # vendor removed the first row of id 2
        pend.add_full(base_from_pandas(full), side_from_pandas(pd.DataFrame({"tradingItemId": full.tradingItemId, "date": full.date, "adjustmentfactor": 0.5, "ciqClose": full.close.astype(float) * 2})), [2])
        info = merge_prices(tmpd / "base.parquet", tmpd / "side.parquet", tmpd / "out.parquet", tmpd / "out_side.parquet", pend)
        got = pq.read_table(tmpd / "out.parquet").to_pandas()
        gs = pq.read_table(tmpd / "out_side.parquet").to_pandas()
        ok = (info["row_groups"] == 2 and len(got) == 5 + 1 + 4 and got[(got.tradingItemId == 1) & (got.date == T("2026-09-14"))].close.iloc[0] == np.float32(99.0)
              and got[(got.tradingItemId == 1) & (got.date == T("2026-09-16"))].close.iloc[0] == np.float32(12.0)
              and (got.tradingItemId == 2).sum() == 4 and (gs[gs.tradingItemId == 2].adjustmentfactor == 0.5).all()
              and (np.diff(row_keys(got.tradingItemId, got.date)) > 0).all() and np.array_equal(row_keys(gs.tradingItemId, gs.date), row_keys(got.tradingItemId, got.date)))
        rec("merge: window replaced (1 corrected, 1 appended), full id replaced (row removed, side refreshed), 2 row groups, sorted & aligned", ok, info)
    # (11) incremental aggregate vs brute force
    rows_all = pd.concat([prev, pd.DataFrame([{**prev.iloc[4].to_dict(), "date": T("2026-09-16"), "close": np.float32(12.0)}])], ignore_index=True)
    prev_agg = agg_from_rows(prev.tradingItemId, prev.date, np.isfinite(prev.close.to_numpy(dtype=np.float64)), T("2026-09-15"))
    win_rows = rows_all[rows_all.date >= T("2026-09-14")]
    w = agg_from_rows(win_rows.tradingItemId, win_rows.date, np.isfinite(win_rows.close.to_numpy(dtype=np.float64)), T("2026-09-16"))
    pw = prev[prev.date >= T("2026-09-14")]
    w["prev_rows_in_window"] = pw.groupby("tradingItemId").size().reindex(w.tradingItemId).fillna(0).astype(int).to_numpy()
    w["prev_close_in_window"] = w["prev_rows_in_window"]
    inc = update_agg(prev_agg, w, T("2026-09-14"), None, T("2026-09-16"))
    brute = agg_from_rows(rows_all.tradingItemId, rows_all.date, np.isfinite(rows_all.close.to_numpy(dtype=np.float64)), T("2026-09-16"))
    same = all((inc[c].to_numpy() == brute[c].to_numpy()).all() for c in ("first_close", "last_close", "first_row", "last_row", "n_rows", "n_close", "n_close_2000"))
    rec("incremental aggregate == brute force after appending one day", same, inc[["tradingItemId", "last_close", "n_rows"]].to_dict("records"))
    # (12) calendar rule (publish.apply_calendar): a holiday with a few OTC prints is off-calendar, a half day is not
    idx = pd.bdate_range("2026-07-01", periods=60)
    n = pd.Series(7000, index=idx, dtype="int64")
    n.loc[idx[30]] = 3          # holiday single prints
    n.loc[idx[31]] = 2500       # thin half day (still >= 20% of ~7000)
    cal = calendar_from_counts(n)
    off = cal.loc[~cal.isTradingDay, "date"].tolist()
    rec("calendar: 3 closes on a holiday -> off-calendar, 2500 on a half day -> trading, all others trading",
        off == [idx[30]] and cal.isTradingDay.sum() == 59 and abs(cal.rollingMedian.iloc[30] - 7000) < 1, (off, int(cal.isTradingDay.sum())))
    # (13) merge with a misaligned side table (published input: base row groups re-cut, side keeps extra off-calendar rows)
    with tempfile.TemporaryDirectory() as tmpd:
        tmpd = Path(tmpd)
        base_prev = base_from_pandas(prev)
        extra = prev.iloc[[0]].copy(); extra["date"] = T("2026-09-07")     # off-calendar row present in the side table only
        side_df = pd.concat([prev, extra], ignore_index=True).sort_values(["tradingItemId", "date"])
        side_prev = side_from_pandas(pd.DataFrame({"tradingItemId": side_df.tradingItemId, "date": side_df.date, "adjustmentfactor": 2.0, "ciqClose": side_df.close.astype(float) / 2}))
        pq.write_table(base_prev, tmpd / "base.parquet", row_group_size=4)          # 3 row groups of 4/4/2 rows
        pq.write_table(side_prev, tmpd / "side.parquet", row_group_size=11)         # 1 row group, 11 rows
        pend = Pending()
        newday = pd.DataFrame([{**prev.iloc[4].to_dict(), "date": T("2026-09-16"), "close": np.float32(12.0)}])
        pend.add_window(base_from_pandas(newday), side_from_pandas(pd.DataFrame({"tradingItemId": [1], "date": [T("2026-09-16")], "adjustmentfactor": [2.0], "ciqClose": [6.0]})),
                        T("2026-09-16"), T("2026-09-16"))
        (tmpd / "T2_work").mkdir()
        info = merge_prices(tmpd / "base.parquet", tmpd / "side.parquet", tmpd / "T2_work" / "out.parquet", tmpd / "T2_work" / "adjf_long.parquet", pend)
        gb = pq.read_table(tmpd / "T2_work" / "out.parquet").to_pandas(); gs = pq.read_table(tmpd / "T2_work" / "adjf_long.parquet").to_pandas()
        ok = (info["side_aligned_by_key"] and info["side_rows_missing_for_base_rows"] == 0 and len(gb) == 11 and len(gs) == 11
              and np.array_equal(row_keys(gs.tradingItemId, gs.date), row_keys(gb.tradingItemId, gb.date)) and (gs.adjustmentfactor == 2.0).all()
              and not (gs.date == T("2026-09-07")).any() and (tmpd / "T2" / "factors" / "adjustment_factor_daily.parquet").exists()
              and (tmpd / "T2" / "factors" / "adjustment_factor_daily.parquet").stat().st_ino == (tmpd / "T2_work" / "adjf_long.parquet").stat().st_ino)
        rec("merge: side table with other row groups + extra off-calendar row aligned by key, publish-name hard link created", ok, info)
    # (14) history append
    hist = pd.DataFrame({"tradingItemId": [1, 2], "sector": ["Energy", "Tech"], "simpleIndustryDescription": ["Oil", "Software"],
                         "effectiveFrom": pd.to_datetime(["2025-02-03", "2025-02-03"]), "effectiveTo": pd.to_datetime([None, None]),
                         "observedAt": pd.to_datetime(["2025-02-03", "2025-02-03"]), "source": "v1_daily_master", "historyQuality": "observed"})
    cur = pd.DataFrame({"tradingItemId": [1, 2, 3], "sector": ["Energy", "Health", "Tech"], "simpleIndustryDescription": ["Oil", "Pharma", "Software"]})
    h2, n_app, n_new = append_history(hist, cur, CLASS_COLS, np.array([1, 2, 3]), T("2026-09-16"))
    ok = (n_app == 1 and n_new == 1 and len(h2) == 4 and h2[(h2.tradingItemId == 2) & h2.effectiveTo.notna()].effectiveTo.iloc[0] == T("2026-09-15")
          and h2[(h2.tradingItemId == 2) & h2.effectiveTo.isna()].sector.iloc[0] == "Health" and len(h2[h2.tradingItemId == 1]) == 1)
    rec("history append: changed id closed at observed-1 + new row, new id current_only, unchanged untouched", ok, (n_app, n_new, len(h2)))
    return res


# ======================================================================================
# main
# ======================================================================================
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prev-build", type=Path, help="previous build: stage root (T1/T2/...) or published build dir")
    ap.add_argument("--out-stage", type=Path, default=None, help="new build directory (created; must not contain BUILD.json; default <work-root>/builds/<build_id>)")
    ap.add_argument("--work-root", type=Path, default=Path("/home/jgo/longshort/daily"), help="durable root for builds/, published/, orch/ (never the session scratchpad)")
    ap.add_argument("--asof", default="auto", help="YYYY-MM-DD (explicit T-1) or 'auto' (latest closed NYSE session that passes the close-count rule)")
    ap.add_argument("--steps", default=",".join(ALL_STEPS), help="comma list of U1..U7 (default all)")
    ap.add_argument("--dry-run", action="store_true", help="fetch + compare + report only (no base_data/factors written, no publish)")
    ap.add_argument("--selftest", action="store_true", help="run the detector counterexample tests (no DB) and exit")
    ap.add_argument("--full-lifecycle", action="store_true", help="re-aggregate ciq/mi last-close dates for all ids (weekly) and re-check rule candidates")
    ap.add_argument("--full-refetch", action="store_true", help="U3: re-fetch the full ciqpriceequity history of ALL ids (weekly sweep; vendor restatements outside the daily re-fetch set become price_revisions / raw-invariance findings)")
    ap.add_argument("--refresh-days", type=int, default=5, help="trading days re-fetched (T-5..T-1)")
    ap.add_argument("--allow-stale", action="store_true", help="auto asOf: proceed when the latest closed session is not complete yet")
    ap.add_argument("--prev-sidecar", type=Path, default=None, help="adjf_long.parquet of the previous build (published layout)")
    ap.add_argument("--publish-out", default=None, help="publish.py --out (default <out-stage>/../published/<build_id>)")
    ap.add_argument("--remote", default=None, help="publish.py --remote (default: no remote)")
    ap.add_argument("--no-remote", action="store_true", help="(default) do not sync to a remote")
    ap.add_argument("--orch", default=None, help="publish.py --orch (BRIEF.md/TASKS.md/reports copied to qa/; default <work-root>/orch)")
    ap.add_argument("--no-proto-tables", action="store_true")
    ap.add_argument("--t5-mode", choices=["auto", "skip", "copy"], default="auto")
    ap.add_argument("--t6-mode", choices=["auto", "skip", "copy"], default="auto")
    ap.add_argument("--t5-timeout", type=int, default=3600, help="seconds per T5/T6 subprocess")
    ap.add_argument("--t6-gates", action="store_true", help="also run events_build.py gates (compares against the prototype events)")
    ap.add_argument("--t5-universe-args", default="", help="extra arguments appended to `universe_build.py run` (e.g. '--tradable line --primary pit')")
    ap.add_argument("--events-override", default="/home/jgo/61/data2/temp_stock/compare_v1_v2/events_v2_altaba/events_override_altaba.csv")
    ap.add_argument("--keep-work", action="store_true", help="keep T5_work/T6_work intermediates (default: deleted after use)")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"), default=None, help="compare two builds (stage or published layout) and exit")
    ap.add_argument("--allow-new-fake-jumps", type=int, default=0)
    ap.add_argument("--allow-new-div-mismatch", type=int, default=0)
    ap.add_argument("--strict-div", action="store_true", help="make G-DIV new-step mismatches blocking")
    ap.add_argument("--raw-mismatch-max", type=float, default=0.01, help="G-RAW window: max fraction of ciq/mi close mismatches (>1e-3)")
    ap.add_argument("--db-wait-max", type=float, default=300.0, help="max seconds to wait for a quiet DB (pg_stat_activity)")
    ap.add_argument("--offline", action="store_true", help="internal: no DB in prepare (tests)")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--ledger", type=Path, default=None, help="rc2 corporate-action ledger (events/ca_ledger.parquet) for returns_total v2; default: new build T6/events, else previous build's")
    ap.add_argument("--returns-legacy", action="store_true", help="rc1 vendor-factor returns_total (no ledger); explicit opt-in only")
    ap.add_argument("--lineage", type=Path, default=None, help="lineage file for returns_total first-row links (default: the new build's master/lineage.parquet)")
    ap.add_argument("--allow-stale-ledger", action="store_true", help="proceed when the ledger asOf is older than the build asOf: rows after it get retFlag 4 and ledger_stale is recorded (default: abort)")
    # rc2f (D2)
    ap.add_argument("--overrides-glob", default=dr.OVERRIDES_GLOB_DEFAULT, help="rc2f override CSVs re-applied every day (default cache_v21/overrides/rc2f/*/*.csv; new subdirs append)")
    ap.add_argument("--ledger-mode", choices=["auto", "skip"], default="auto", help="auto: ledger_build.py --incremental (D1) before returns; skip: --ledger / carry rules only")
    ap.add_argument("--ledger-extra-args", default="", help="extra arguments appended to the ledger builder call")
    ap.add_argument("--ledger-prev", type=Path, default=None, help="ledger builder --prev override (first build from a published build: the rc2f R1 stage, e.g. /home/jgo/longshort/rc2f_L2/build/R1, carries the apply hand-over files); default = previous build's T6 (stage) or root (published)")
    ap.add_argument("--strict-exposure", action="store_true", help="G-EXPO blocking: a live-book holding with a flag 3/4/NaN asOf row, a new settlement without terms or a new display row fails the build (default: warning)")
    ap.add_argument("--book-glob", default=dr.BOOK_GLOB, help="live book position files (latest one is used; /home/base is read-only)")
    ap.add_argument("--no-returns-qa-vendor", dest="returns_qa_vendor", action="store_false", help="skip the rc1 vendor-factor returns (G-VENDOR reference) in the returns QA")
    ap.add_argument("--orch-rc2", default=None, help="publish.py --orch-rc2 (default <orch>/rc2)")
    ap.add_argument("--orch-rc2f", default=None, help="publish.py --orch-rc2f (default <orch>/rc2f)")
    ap.add_argument("--exposure-warn-rc", type=int, default=9, help="exit code of a successful build whose exposure summary has live-book hits (G-EXPO warning; 0 = plain success)")
    ap.add_argument("--co-acthist-max-lag-days", type=int, default=7, help="G-SOURCE warning when co_acthist's last datadate is older than asOf minus this many days")
    ap.add_argument("--prev-work", type=Path, default=None, help="T1/_work of a stage build (calendar.parquet, lineage_overlap.parquet, identity.parquet) when --prev-build is a published build")
    ap.add_argument("--raw-change-policy", choices=["keep", "abort"], default="keep", help="re-fetched (restated) ids whose vendor raw differs from the stored raw: keep = stored values stay, "
                    "vendor values logged as vendor_restated_kept + G-RAWINV (default; blocking with --strict-raw); abort = exit code 5 (T9 rule)")
    ap.add_argument("--accept-raw-change", default=None, help="comma list of ids whose vendor restatement is adopted today (revisions reason vendor_restated_accepted, rows source 2)")
    ap.add_argument("--strict-raw", action="store_true", help="G-RAWINV blocking: any kept restatement fails the build")
    ap.add_argument("--lineage-recent-days", type=int, default=30, help="U4: a new lineage candidate pair is added only when its effectiveDate / an id's lastTradeDate is within this many days before the previous asOf or an id is new (older pairs are listed, not added)")
    ap.add_argument("--mi-lag-tdays", type=int, default=2, help="U5 finalize: an active id whose lastTradeDate came from a miprice lagging the ciq close by <= N trading days keeps the ciq last close (0 = T1 rule only)")
    ap.add_argument("--events-delta-max", type=int, default=100, help="G-EVENTS blocking when more events rows than this are adopted from today's T6 (newly ended / changed / dropped ids)")
    ap.add_argument("--ret-abs-max", type=float, default=50.0, help="G-RET-OUTLIER: any |ret| above this (5,000%%) fails the build; new historical cells beyond 4x / -75%% fail regardless")
    ap.set_defaults(returns_qa_vendor=True)
    args = ap.parse_args(argv)
    args.orch = str(args.orch) if args.orch else str(args.work_root / "orch")
    args.accept_raw_change = [int(x) for x in str(args.accept_raw_change).split(",") if x.strip()] if args.accept_raw_change else []
    args.orch_rc2 = str(args.orch_rc2) if args.orch_rc2 else str(Path(args.orch) / "rc2")
    args.orch_rc2f = str(args.orch_rc2f) if args.orch_rc2f else str(Path(args.orch) / "rc2f")
    pa.set_cpu_count(args.threads)
    pa.set_io_thread_count(max(2, args.threads // 2))

    if args.compare:
        res = compare_builds(Path(args.compare[0]), Path(args.compare[1]))
        print(compare_md(res))
        print(json.dumps(pqa.to_native(res), indent=1, default=str))
        return 0
    if args.selftest:
        logging.basicConfig(level=logging.WARNING)
        res = self_test()
        for r in res:
            print(("OK  " if r["ok"] else "BAD ") + r["case"] + ("" if r["ok"] else f" | {r['detail']}"))
        n_bad = sum(not r["ok"] for r in res)
        print(f"self-test: {len(res) - n_bad}/{len(res)} cases behave as expected")
        return 1 if n_bad else 0

    if not args.prev_build:
        ap.error("--prev-build is required (or --selftest)")
    if (Path(args.prev_build) / "base_data.parquet").exists() and not (Path(args.prev_build) / "T2" / "base_data.parquet").exists():
        # published previous build (first daily build): the ledger builder needs the rc2f apply hand-over files (R-D2 P1-6) and the stage work files help U4
        if args.ledger_mode == "auto" and not args.ledger and not args.ledger_prev and not args.dry_run:
            ap.error("--prev-build is a published build: pass --ledger-prev <rc2f R1 stage with handover/> (e.g. /home/jgo/longshort/rc2f_L2/build/R1) or --ledger / --ledger-mode skip")
    if not args.out_stage:
        args.out_stage = args.work_root / "builds" / f"v21_{dt.datetime.now(KST):%Y%m%d_%H%M%S}"
    if not args.publish_out:
        args.publish_out = str(args.work_root / "published" / args.out_stage.name)
    steps = [s.strip().upper() for s in args.steps.split(",") if s.strip()]
    bad = [s for s in steps if s not in ALL_STEPS]
    if bad:
        ap.error(f"unknown steps {bad}; valid: {ALL_STEPS}")
    args.steps = steps
    out = OutBuild(args.out_stage)
    if out.build_json.exists():
        ap.error(f"{out.root} already holds a build (BUILD.json): choose a new --out-stage")
    out.mkdirs()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(out.log_path)])
    prev = PrevBuild(args.prev_build, args.prev_sidecar)
    ctx = Ctx(args, prev, out)
    t_all = time.time()
    log.info("==== update_daily start: prev=%s (%s, asOf %s) out=%s steps=%s asof=%s dry_run=%s pid=%d", prev.root, prev.layout, ctx.prev_asof.date(),
             out.root, steps, args.asof, args.dry_run, os.getpid())
    rc = 0
    try:
        prepare(ctx)
        ctx.save_state("running")
        if args.asof and args.asof != "auto":
            ctx.asof = ts(args.asof)
        if "U1" in steps:
            u1_master(ctx)
            ctx.save_state("running")
        if "U2" in steps:
            u2_raw(ctx)
            ctx.save_state("running")
        if args.dry_run:
            # U3 fetch/diff without writing the big files
            if "U3" in steps:
                db, ids = ctx.dbc(), ctx.price_ids()
                sc = pfm.fetch_split_windows(db, ids)
                dv = pfm.fetch_dividend_windows(db, ids)
                sp0 = pfm.build_intervals(pfm.split_windows(sc), ids, pfm.SRC_SPLIT, ctx.observed, pre_first="pre")
                dd = pfm.build_intervals(pfm.dividend_windows(dv), ids, pfm.SRC_DIV, ctx.observed)
                prev_sc = pd.read_parquet(prev.raw_splitcache) if prev.raw_splitcache.exists() else None
                r_sc = splitcache_changed_ids(prev_sc, sc)
                frev = pd.concat([factor_diff(pd.read_parquet(prev.split), sp0, "split(before reconcile)", ctx.observed),
                                  factor_diff(pd.read_parquet(prev.dividend), dd, "dividend", ctx.observed)], ignore_index=True)
                ctx.results["steps"]["U3_dry"] = {"splitcache_changed_ids": sorted(r_sc), "factor_diff_rows": int(len(frev)),
                                                  "factor_diff_by_table_kind": kv_str(frev.groupby(["table", "kind"]).size().to_dict()) if len(frev) else {}}
            ctx.results["dry_run"] = True
            ctx.save_state("dry-run")
            (out.qa / f"dryrun_{ctx.observed:%Y-%m-%d}.json").write_text(json.dumps(pqa.to_native(ctx.results), indent=1, default=str))
            log.info("dry-run: nothing written except BUILD.json / qa; %s", json.dumps(pqa.to_native(ctx.results["steps"]), default=str)[:2000])
            return 0
        if "U3" in steps:
            u3_factors(ctx)
            ctx.save_state("running")
        elif "U2" in steps and not ctx.merge_done:
            flush_merge(ctx)
            ctx.save_state("running")
        if "U4" in steps:
            u4_lifecycle(ctx)
            ctx.save_state("running")
        if "U5" in steps:
            u5_derived(ctx)
            ctx.save_state("running")
        if "U6" in steps:
            u6_gates(ctx)
            ctx.save_state("running")
        t_m = time.time()
        ctx.results["manifest"] = file_manifest(ctx)
        ctx.time_step("manifest", t_m, bytes_changed=ctx.results["manifest"]["bytes_changed_vs_prev"])
        ctx.write_apply_log()
        if "U7" in steps:
            if "U6" not in steps:
                ctx.warn("U7 requested without U6: publishing without the gates")
            u7_publish(ctx)
        status = "ok"
    except UpdateAbort as exc:
        rc = exc.code
        ctx.failed = {"code": exc.code, "message": str(exc)}
        log.error("ABORT (code %d): %s -- previous build stays current", exc.code, exc)
        status = "failed"
    except Exception as exc:  # noqa: BLE001
        rc = 1
        ctx.failed = {"code": 1, "message": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()[-4000:]}
        log.exception("FAILED: %s -- previous build stays current", exc)
        status = "failed"
    finally:
        if ctx.db is not None:
            try:
                ctx.db.close()
            except Exception:
                pass
    ctx.timings["total"] = {"seconds": round(time.time() - t_all, 1), "peak_rss_gb": round(rss_gb(), 2)}
    if rc == 0 and status == "ok" and ctx.rc2f.get("exposure", {}).get("warning") and not args.strict_exposure and args.exposure_warn_rc:
        rc = args.exposure_warn_rc
        ctx.results["exposure_warning_rc"] = rc
        log.warning("exposure warning: live-book hits (qa/exposure_daily.md) -> exit code %d (build ok, published)", rc)
    ctx.save_state(status)
    log.info("==== update_daily %s in %.1f min, peak rss %.1f GB -> %s", status, (time.time() - t_all) / 60, rss_gb(), out.build_json)
    return rc


if __name__ == "__main__":
    sys.exit(main())
