"""cache_v21/tests/chain_compare.py - D3: file-by-file comparison of the daily chain end with the full-regeneration references and
attribution of every differing cell (used by chain_reproduce.py; importable, no DB).

Attribution classes (Korean labels in the report):
  refetch_window        재수신 창(T-5..T-1) 정정: cell logged as vendor_refetch / vendor_late_row / vendor_removed in either side's price_revisions
  vendor_restatement    벤더 재기술: vendor_restated_kept/accepted (raw invariance) or a factor/side-table restatement of a re-fetched id
  vendor_restatement_unseen  벤더 재기술(창 밖, 체인 미탐지): full-fetch cell differs from the chain AND the chain's stored value equals the snapshot
                        (vendor changed the row after the snapshot; the id was not in any re-fetch set) - reported, chain blind spot
  vendor_late_load      벤더 지연 적재 / 실제 적재 시점: a source load (xfl_control) landed between the two runs and the cell is in the later
                        run's revisions / source_changes with cls window|late_history|future
  new_id                새 id (U1 rule additions after the start build)
  ledger_revision       원장 revisions (D1): row key present in either side's ca_ledger_revisions (added/removed/changed/rekeyed)
  override              교정: price override window / override id whose apply status differs between the sides
  lifecycle_delta       계보·lifecycle 델타: id in either side's lifecycle_revisions (field), lifecycle_dates rebuilt from it, marker rows
  lineage_delta         계보 델타: pair listed in lineage_daily_excluded.csv / U4 new pairs / lineage-touched ids
  tie_break             rule:ciqsplit_tie_break_smallest_splitid (D1 attributed list vs the published rc2f2/rc2f3 ledger)
  path_dependence       경로 의존: explained only by the chain's intermediate state (a chain-day revision reverted or superseded on a later day)
  construction          구조적(비교 불가 컬럼): full-fetch has no miprice flags / provisional flags / lifecycle tail flags; export estimate columns
  upstream:<class>      derived file cell whose input (price/factor/ledger/lineage/lifecycle) differs with that class
  unattributed          -> FAIL
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
CV = HERE.parent
sys.path.insert(0, str(CV))
import rc2f_recount as rr  # noqa: E402
import update_daily_rc2f as dr  # noqa: E402

KST = dt.timezone(dt.timedelta(hours=9))
PRICE_COLS = ["open", "high", "low", "close", "vwap", "volume"]
LEDGER_KEY = ["tradingItemId", "exDate", "eventType", "legType", "childTradingItemId"]
LEDGER_IGNORE = ("eventId",)                     # eventId carries a sequence -> compared through the key (rekeyed rows are revisions)
FILES = {   # published-layout files compared chain-end vs one-step (and stage-level extras)
    "base_data": "base_data.parquet", "factors/adjustment_factor_daily": "factors/adjustment_factor_daily.parquet",
    "factors/split": "factors/split.parquet", "factors/dividend": "factors/dividend.parquet", "events/ca_ledger": "events/ca_ledger.parquet",
    "derived/returns_total": "derived/returns_total.parquet", "engine/ca_ledger_cache": "engine/ca_ledger_cache.parquet",
    "engine/events_cache": "engine/events_cache.parquet", "master/lifecycle": "master/lifecycle.parquet",
    "master/lifecycle_dates": "master/lifecycle_dates.parquet", "master/lineage": "master/lineage.parquet",
    "universe/membership": "universe/membership.parquet", "events/corporate_actions": "events/corporate_actions.parquet",
    "derived/marketcap": "derived/marketcap.parquet", "calendar": "calendar.parquet", "master/lifecycle_aux": "master/lifecycle_aux.parquet",
    "master/instruments": "master/instruments.parquet", "master/tradable_exclusions": "master/tradable_exclusions.parquet",
}
FACTOR_TOL_DAYS = 10
LEDGER_TOL_DAYS = 10


def sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 24), b""):
            h.update(chunk)
    return h.hexdigest()


def days_of(x) -> np.ndarray:
    return np.asarray(pd.to_datetime(np.asarray(x))).astype("datetime64[D]").astype(np.int64)


def row_keys(ids, dates) -> np.ndarray:
    return (np.asarray(ids, dtype=np.int64) << 20) | days_of(dates)


def key_ids(k: np.ndarray) -> np.ndarray:
    return (np.asarray(k, dtype=np.int64) >> 20).astype(np.int64)


def key_days(k: np.ndarray) -> np.ndarray:
    return (np.asarray(k, dtype=np.int64) & ((1 << 20) - 1)).astype(np.int64)


def day_str(d: int) -> str:
    return str(np.datetime64(int(d), "D"))


# --------------------------------------------------------------------------------------
# build inputs (stage + published) for attribution
# --------------------------------------------------------------------------------------
class BuildInfo:
    def __init__(self, stage: Path, pub: Path | None, label: str):
        self.stage, self.pub, self.label = stage, pub, label
        self.build = json.loads((stage / "BUILD.json").read_text()) if (stage / "BUILD.json").exists() else {}
        self.asof = self.build.get("asOf")
        self.prev_asof = self.build.get("prev_asOf")
        self.refresh_dates = [str(d)[:10] for d in self.build.get("refresh_dates", [])]
        self.refetched_ids = set(int(i) for i in self.build.get("refetched_ids", []) or [])
        self.new_ids = set(int(i) for i in self.build.get("new_ids", []) or [])
        u2 = self.build.get("results", {}).get("steps", {}).get("U2", {})
        self.fetch_from = u2.get("asof_pick", {}).get("fetch_from")
        self.price_rev = self._concat("archive/revisions/date=*/price_revisions.parquet")
        self.factor_rev = self._concat("archive/revisions/date=*/factor_revisions.parquet")
        self.lifecycle_rev = self._concat("archive/revisions/date=*/lifecycle_revisions.parquet")
        self.master_rev = self._concat("archive/revisions/date=*/master_revisions.parquet")
        self.ledger_rev = self._concat("T6/events/ca_ledger_revisions/date=*/revisions.parquet")
        self.ledger_src = self._concat("T6/events/ca_ledger_revisions/date=*/source_changes.parquet")
        p = stage / "T2" / "qa" / "raw_invariance_violations.csv"
        self.rawinv = pd.read_csv(p) if p.exists() and p.stat().st_size > 0 else pd.DataFrame(columns=["tradingItemId", "date", "field"])
        p = stage / "T1" / "qa" / "lineage_daily_excluded.csv"
        self.lineage_excluded = pd.read_csv(p) if p.exists() and p.stat().st_size > 0 else pd.DataFrame(columns=["oldId", "newId"])
        p = stage / "rc2f" / "rc2f_apply_log.csv"
        self.apply_log = pd.read_csv(p, dtype=str, keep_default_na=False) if p.exists() else pd.DataFrame(columns=["overrideId", "kind", "status", "target"])
        self.u4 = self.build.get("rc2f", {}).get("U4", {})
        self.finalize = self.build.get("rc2f", {}).get("finalize", {})
        p = stage / "T6" / "qa" / "events_transitions.csv"
        self.events_transitions = pd.read_csv(p, dtype=str, keep_default_na=False) if p.exists() and p.stat().st_size > 0 else pd.DataFrame(columns=["tradingItemId", "kind", "fields"])
        self.events_adopted = {int(i): k for i, k in zip(self.events_transitions.tradingItemId, self.events_transitions.kind) if str(k).startswith("adopted") or str(k) == "dropped"} if len(self.events_transitions) else {}
        p = stage / "T1" / "qa" / "lifecycle_end_dates_carried.csv"
        self.end_dates_carried = set(int(i) for i in pd.read_csv(p).tradingItemId) if p.exists() and p.stat().st_size > 0 else set()

    def _concat(self, pattern: str) -> pd.DataFrame:
        paths = sorted(glob.glob(str(self.stage / pattern)))
        if "archive/revisions" in pattern:
            own = [p for p in paths if ".run" not in Path(p).parent.name]      # this build's own dir; date=<obs>.run<N> = carried earlier attempts (D2 rework)
            paths = own or paths
        parts = [pd.read_parquet(p) for p in paths]
        parts = [p for p in parts if len(p)]
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()

    def timings(self) -> dict:
        return self.build.get("timings", {})


def load_price_windows(overrides_glob: str, ids) -> pd.DataFrame:
    try:
        ovr, _, _ = dr.load_overrides(overrides_glob, ids, None)
        return dr.price_windows(ovr.get("price"))
    except Exception as exc:  # noqa: BLE001
        print(f"price windows unavailable: {exc}")
        return pd.DataFrame(columns=["tradingItemId", "d0", "d1", "overrideId"])


# --------------------------------------------------------------------------------------
# generic diffs
# --------------------------------------------------------------------------------------
def diff_long(a_path: Path, b_path: Path, cols: list[str] | None = None, ignore=()) -> tuple[dict, pd.DataFrame, np.ndarray, np.ndarray]:
    """(id, date) long files -> (stats, diff cells [tradingItemId, date, column, a, b], keys only in a, keys only in b)."""
    ta, tb = pq.read_table(a_path), pq.read_table(b_path)
    ka = row_keys(ta["tradingItemId"].to_numpy(), ta["date"].to_numpy())
    kb = row_keys(tb["tradingItemId"].to_numpy(), tb["date"].to_numpy())
    sa, sb = np.argsort(ka, kind="stable"), np.argsort(kb, kind="stable")
    ka, kb = ka[sa], kb[sb]
    common, ia, ib = np.intersect1d(ka, kb, assume_unique=True, return_indices=True)
    only_a = np.setdiff1d(ka, common, assume_unique=True)
    only_b = np.setdiff1d(kb, common, assume_unique=True)
    ia, ib = sa[ia], sb[ib]
    stats = {"rows_a": ta.num_rows, "rows_b": tb.num_rows, "rows_only_a": int(len(only_a)), "rows_only_b": int(len(only_b)), "common_rows": int(len(common)),
             "sha256_equal": sha256(a_path) == sha256(b_path)}
    parts = []
    cols = cols or [c for c in ta.column_names if c not in ("tradingItemId", "date")]
    for c in cols:
        if c in ignore or c not in tb.column_names:
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
        if d.any():
            j = np.flatnonzero(d)
            parts.append(pd.DataFrame({"tradingItemId": key_ids(common[j]), "date": key_days(common[j]).astype("datetime64[D]").astype("datetime64[ns]"),
                                       "column": c, "a": x[j].astype(str), "b": y[j].astype(str)}))
        stats.setdefault("cells_by_column", {})[c] = int(d.sum())
    diff = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["tradingItemId", "date", "column", "a", "b"])
    stats["diff_cells"] = int(len(diff))
    return stats, diff, only_a, only_b


def diff_keyed(a: pd.DataFrame, b: pd.DataFrame, key: list[str], ignore=()) -> tuple[dict, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Small keyed tables -> (stats, diff cells [key..., column, a, b], rows only in a, rows only in b)."""
    def k(df):
        return df[key].astype(str).agg("|".join, axis=1)
    a2, b2 = a.copy(), b.copy()
    a2["_k"], b2["_k"] = k(a2), k(b2)
    dup_a, dup_b = int(a2._k.duplicated().sum()), int(b2._k.duplicated().sum())
    a2, b2 = a2.drop_duplicates("_k").set_index("_k"), b2.drop_duplicates("_k").set_index("_k")
    common = a2.index.intersection(b2.index)
    only_a, only_b = a2.loc[a2.index.difference(b2.index)].reset_index(drop=True), b2.loc[b2.index.difference(a2.index)].reset_index(drop=True)
    stats = {"rows_a": int(len(a)), "rows_b": int(len(b)), "rows_only_a": int(len(only_a)), "rows_only_b": int(len(only_b)), "dup_keys_a": dup_a, "dup_keys_b": dup_b,
             "common_rows": int(len(common))}
    parts = []
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
        elif x.dtype == bool or y.dtype == bool:
            d = x.astype(str) != y.astype(str)
        else:
            d = ~((x.isna() & y.isna()) | (x.astype(str) == y.astype(str)))
        n = int(d.sum())
        stats.setdefault("cells_by_column", {})[c] = n
        if n:
            idx = common[d.to_numpy()]
            part = a2.loc[idx, key].reset_index(drop=True)
            part["column"] = c
            part["a"] = a2.loc[idx, c].astype(str).to_numpy()
            part["b"] = b2.loc[idx, c].astype(str).to_numpy()
            parts.append(part)
    diff = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=key + ["column", "a", "b"])
    stats["diff_cells"] = int(len(diff))
    return stats, diff, only_a, only_b


# --------------------------------------------------------------------------------------
# attribution helpers
# --------------------------------------------------------------------------------------
class Attributor:
    """Per-cell lookups built from the revision logs of both sides."""

    def __init__(self, sides: list[BuildInfo], start_ids: set[int], price_win: pd.DataFrame, loads_between: dict, tie_break: pd.DataFrame | None):
        self.sides = sides
        self.start_ids = start_ids
        self.price_win = price_win
        self.loads_between = loads_between          # {"prices": bool, "factors": bool, "dividends": bool, ...} loads landed between the runs
        self.tie_break = tie_break
        # price revisions: key -> set of reasons (field-specific and row-level)
        self.price_reason: dict[tuple[int, int, str], set] = {}
        self.price_row_reason: dict[tuple[int, int], set] = {}
        self.restated_ids: set[int] = set()
        self.refetched_ids: set[int] = set()
        self.new_ids: set[int] = set()
        for s in sides:
            self.refetched_ids |= s.refetched_ids
            self.new_ids |= s.new_ids
            pr = s.price_rev
            if len(pr):
                for r in pr.itertuples(index=False):
                    key = (int(r.tradingItemId), int(days_of([r.date])[0]))
                    if r.field == "row":
                        self.price_row_reason.setdefault(key, set()).add(f"{r.reason}@{s.label}")
                    else:
                        self.price_reason.setdefault((key[0], key[1], r.field), set()).add(f"{r.reason}@{s.label}")
                self.restated_ids |= set(int(i) for i in pr.loc[pr.reason.astype(str).str.startswith("vendor_restated"), "tradingItemId"])
            if len(s.rawinv):
                self.restated_ids |= set(int(i) for i in s.rawinv.tradingItemId)
        # factor revisions: id -> list of (validFrom day, table)
        self.factor_ids: dict[int, list] = {}
        for s in sides:
            fr = s.factor_rev
            if len(fr):
                for r in fr.itertuples(index=False):
                    self.factor_ids.setdefault(int(r.tradingItemId), []).append((int(days_of([r.validFrom])[0]), str(r.table), s.label))
        # ledger revisions: key -> sources
        self.ledger_keys: dict[tuple, set] = {}
        self.ledger_ids: dict[int, list] = {}
        for s in sides:
            lr = s.ledger_rev
            if len(lr):
                for r in lr.itertuples(index=False):
                    child = int(r.childTradingItemId) if pd.notna(r.childTradingItemId) else -1
                    key = (int(r.tradingItemId), int(days_of([r.exDate])[0]), str(r.eventType), str(r.legType), child)
                    self.ledger_keys.setdefault(key, set()).add(f"{r.kind}:{r.source}@{s.label}")
                    self.ledger_ids.setdefault(int(r.tradingItemId), []).append((int(days_of([r.exDate])[0]), f"{r.kind}:{r.source}@{s.label}"))
        # lifecycle revisions: id -> fields
        self.lifecycle_ids: dict[int, set] = {}
        self.master_ids: dict[int, set] = {}
        self.full_lifecycle_labels = {s.label for s in sides if s.build.get("full_lifecycle")}
        for s in sides:
            lr = s.lifecycle_rev
            if len(lr):
                for r in lr.itertuples(index=False):
                    self.lifecycle_ids.setdefault(int(r.tradingItemId), set()).add(f"{r.field}@{s.label}")
            mr = s.master_rev
            if len(mr):
                for r in mr.itertuples(index=False):
                    self.master_ids.setdefault(int(r.tradingItemId), set()).add(f"{r.table}.{r.field}@{s.label}")
        # lineage: excluded pairs / touched ids
        self.lineage_pairs: dict[tuple[int, int], set] = {}
        self.lineage_ids: dict[int, set] = {}
        for s in sides:
            le = s.lineage_excluded
            if len(le):
                for r in le.itertuples(index=False):
                    self.lineage_pairs.setdefault((int(r.oldId), int(r.newId)), set()).add(f"excluded@{s.label}")
                    for i in (int(r.oldId), int(r.newId)):
                        self.lineage_ids.setdefault(i, set()).add(f"excluded_pair@{s.label}")
        # override status differences between the sides (override ids whose apply status differs)
        self.override_status_diff: dict[str, str] = {}
        logs = [s.apply_log for s in sides if len(s.apply_log)]
        if len(logs) >= 2:
            a, b = logs[0].set_index("overrideId").status, logs[-1].set_index("overrideId").status
            for oid in a.index.union(b.index):
                if a.get(oid) != b.get(oid):
                    self.override_status_diff[oid] = f"{a.get(oid)}->{b.get(oid)}"
        self.override_ids_diff: set[int] = set()
        for s in sides:
            lg = s.apply_log
            if len(lg) and self.override_status_diff:
                sub = lg[lg.overrideId.isin(list(self.override_status_diff))]
                for t in sub.target.astype(str):
                    for tok in t.replace(",", " ").replace(":", " ").split():
                        if tok.isdigit():
                            self.override_ids_diff.add(int(tok))
        self.tie_break_events = set(tie_break.eventId) if tie_break is not None and len(tie_break) else set()
        self.events_adopted: dict[int, str] = {}
        for s in sides:
            for i, k in getattr(s, "events_adopted", {}).items():
                self.events_adopted[i] = f"{k}@{s.label}"
        self.first_label = sides[0].label if sides else ""
        self.snapshot_lifecycle = None

    def in_fetch_range(self, day: int) -> bool:
        for sd in self.sides:
            if sd.fetch_from and day >= int(days_of([pd.Timestamp(sd.fetch_from)])[0]):
                return True
        return False

    def any_price_reason(self, tid: int, day: int) -> set:
        r = set(self.price_row_reason.get((tid, day), set()))
        for col in PRICE_COLS:
            r |= self.price_reason.get((tid, day, col), set())
        return r

    # --- lookups
    def in_price_window(self, tid: int, day: int) -> str | None:
        w = self.price_win
        if not len(w):
            return None
        m = (w.tradingItemId.to_numpy() == tid) & (days_of(w.d0) <= day) & (days_of(w.d1) >= day)
        return ",".join(w.overrideId[m].astype(str)) if m.any() else None

    def price_cell(self, tid: int, day: int, col: str) -> tuple[str, str]:
        reasons = self.price_reason.get((tid, day, col), set()) | self.price_row_reason.get((tid, day), set())
        if not reasons and col in ("qualityFlag", "source"):
            reasons = self.any_price_reason(tid, day)          # flag/source of a row follow any revised field of that row
        if reasons:
            rs = ",".join(sorted(reasons))
            if any(x.startswith("vendor_restated") for x in reasons):
                return "vendor_restatement", rs
            if any(x.startswith("vendor_refetch_full") for x in reasons):
                return "vendor_restatement", rs
            labels = {x.split("@", 1)[1] for x in reasons if "@" in x}
            chain_side = any(l.startswith("chain") for l in labels)
            other_side = any(not l.startswith("chain") for l in labels)
            if chain_side and other_side:
                return ("vendor_late_load" if self.loads_between.get("prices") else "refetch_window"), rs + " (both sides re-fetched the row)"
            return "refetch_window", rs + " (re-fetch scope of one side only)"
        if tid in self.new_ids:
            return "new_id", "U1 new id"
        oid = self.in_price_window(tid, day)
        if oid:
            return "override", f"price override window {oid}"
        if tid in self.restated_ids and col in PRICE_COLS:
            return "vendor_restatement", "id in raw_invariance_violations"
        return "unattributed", ""

    def factor_cell(self, tid: int, day: int | None, table: str) -> tuple[str, str]:
        if tid in self.factor_ids:
            hits = [h for h in self.factor_ids[tid] if day is None or abs(h[0] - day) <= FACTOR_TOL_DAYS or table == "dividend"]
            if hits:
                cls = "vendor_late_load" if self.loads_between.get("factors" if table == "split" else "dividends") else "vendor_restatement"
                return cls, ";".join(f"factor_revision {h[1]} {day_str(h[0])}@{h[2]}" for h in hits[:3])
        if tid in self.new_ids:
            return "new_id", "U1 new id"
        if tid in self.override_ids_diff:
            return "override", "override status differs"
        return "unattributed", ""

    def ledger_row(self, key: tuple, eventid: str | None = None) -> tuple[str, str]:
        if key in self.ledger_keys:
            srcs = sorted(self.ledger_keys[key])
            if any("tie_break" in s for s in srcs):
                return "tie_break", ";".join(srcs)
            if any("override:" in s for s in srcs):
                return "override", ";".join(srcs)
            if any("ended_without_terms" in s or "lifecycle" in s for s in srcs):
                return "lifecycle_delta", ";".join(srcs)
            if self.loads_between.get("dividends") and any("ciqdividend" in s for s in srcs):
                return "vendor_late_load", ";".join(srcs)
            return "ledger_revision", ";".join(srcs)
        if eventid and eventid in self.tie_break_events:
            return "tie_break", "D1 attributed_diffs_vs_rc2f2"
        tid, day = key[0], key[1]
        near = [h for h in self.ledger_ids.get(tid, []) if abs(h[0] - day) <= LEDGER_TOL_DAYS]
        if near:
            return "ledger_revision", "same id, exDate within tol: " + ";".join(h[1] for h in near[:3])
        if tid in self.new_ids:
            return "new_id", "U1 new id"
        if tid in self.lifecycle_ids:
            return "lifecycle_delta", ",".join(sorted(self.lifecycle_ids[tid]))[:200]
        return "unattributed", ""

    def lifecycle_id(self, tid: int, field: str | None = None) -> tuple[str, str]:
        if field in ("securityEndDate", "tradingItemStatusId") and tid in self.master_ids:
            return "identity_delta", "U1 identity/current change: " + ",".join(sorted(self.master_ids[tid]))[:160]
        if tid in self.lifecycle_ids:
            f = sorted(self.lifecycle_ids[tid])
            alias = {"firstTradeSource": "firstTradeDate", "firstPriceDate": "firstTradeDate"}.get(field, field)
            if field is None or any(x.startswith(alias) for x in f) or field in ("ciqLastCloseDate", "miLastCloseDate", "lastTradeSource", "endReason", "evidence", "observedAt"):
                return "lifecycle_delta", ",".join(f)[:200]
        if field in ("firstPriceDate", "firstTradeDate", "ciqLastCloseDate", "miLastCloseDate", "lastTradeDate") and self.full_lifecycle_labels:
            rows = [x for (t, d), rs in self.price_row_reason.items() if t == tid for x in rs]
            if rows:
                return "full_aggregation", "full re-aggregation (--full-lifecycle) re-derives first/last close dates from the vendor rows; the incremental aggregate kept the stored dates after " + ",".join(sorted(set(rows)))[:120]
        if tid in self.new_ids:
            return "new_id", "U1 new id"
        if tid in self.lineage_ids:
            return "lineage_delta", ",".join(sorted(self.lineage_ids[tid]))[:200]
        return "unattributed", ""


# --------------------------------------------------------------------------------------
# comparisons
# --------------------------------------------------------------------------------------
def rec(file, ref, stats, diff: pd.DataFrame, note="") -> dict:
    by = diff["cls"].value_counts().to_dict() if len(diff) and "cls" in diff.columns else {}
    unattr = int(sum(v for k, v in by.items() if k == "unattributed" or str(k).endswith(":unattributed")))
    examples = {}
    if len(diff) and "cls" in diff.columns:
        keycols = [c for c in diff.columns if c not in ("cls", "how")]
        for k, g in diff.groupby("cls"):
            examples[k] = [{c: (str(v)[:60]) for c, v in r.items()} for r in g.head(2)[keycols + ["how"]].to_dict("records")]
    return {"file": file, "ref": ref, "rows_a": stats.get("rows_a"), "rows_b": stats.get("rows_b"), "only_a": stats.get("rows_only_a", 0), "only_b": stats.get("rows_only_b", 0),
            "sha256_equal": stats.get("sha256_equal"), "diff_cells": int(len(diff)), "by_class": by, "unattributed": unattr,
            "cells_by_column": stats.get("cells_by_column", {}), "note": note, "examples": examples}


def cmp_base_data(a: Path, b: Path, att: Attributor, out: Path, label: str, ref: str, cols=None, ignore=()) -> tuple[dict, pd.DataFrame]:
    stats, diff, only_a, only_b = diff_long(a, b, cols=cols, ignore=ignore)
    rows = []
    for k in only_a:
        rows.append({"tradingItemId": int(key_ids([k])[0]), "date": day_str(key_days([k])[0]), "column": "row", "a": "present", "b": "absent"})
    for k in only_b:
        rows.append({"tradingItemId": int(key_ids([k])[0]), "date": day_str(key_days([k])[0]), "column": "row", "a": "absent", "b": "present"})
    if rows:
        diff = pd.concat([diff, pd.DataFrame(rows)], ignore_index=True)
    if len(diff):
        cls, how = [], []
        dd = days_of(diff.date)
        for tid, day, col in zip(diff.tradingItemId.astype(int), dd, diff.column):
            if col in ("qualityFlag", "source"):
                c, h = att.price_cell(tid, int(day), col)
                if c == "unattributed":
                    c2, h2 = att.price_cell(tid, int(day), "close")
                    c, h = (c2, h2) if c2 != "unattributed" else (c, h)
                if c == "unattributed" and tid in att.lifecycle_ids:
                    c, h = "lifecycle_delta", "flag 4 tail follows lastTradeDate: " + ",".join(sorted(att.lifecycle_ids[tid]))[:120]
                if c == "unattributed" and col == "qualityFlag" and att.in_fetch_range(int(day)):
                    c, h = "refetch_window", "qualityFlag re-evaluated on a re-fetched row (mi cross-check / provisional rule); values unchanged so no revision is logged"
            else:
                c, h = att.price_cell(tid, int(day), col)
            cls.append(c)
            how.append(h)
        diff["cls"], diff["how"] = cls, how
    diff.to_csv(out / f"diff_{label}.csv", index=False)
    return rec("base_data" if "base_data" in label else label, ref, stats, diff), diff


def cmp_side(a: Path, b: Path, att: Attributor, out: Path, label: str, ref: str, aux_ids: set | None = None) -> tuple[dict, pd.DataFrame]:
    stats, diff, only_a, only_b = diff_long(a, b)
    aux_ids = aux_ids or set()
    rows = [{"tradingItemId": int(key_ids([k])[0]), "date": day_str(key_days([k])[0]), "column": "row", "a": "present", "b": "absent"} for k in only_a]
    rows += [{"tradingItemId": int(key_ids([k])[0]), "date": day_str(key_days([k])[0]), "column": "row", "a": "absent", "b": "present"} for k in only_b]
    if rows:
        diff = pd.concat([diff, pd.DataFrame(rows)], ignore_index=True)
    if len(diff):
        cls, how = [], []
        for tid, d, col, av, bv in zip(diff.tradingItemId.astype(int), days_of(diff.date), diff.column, diff.a, diff.b):
            if tid in aux_ids and ("nan" in (str(av), str(bv))):
                cls.append("construction")
                how.append("aux instrument row: no side table in the snapshot (R3 aux prices); the side row exists only where a run re-fetched the window row")
                continue
            c, h = att.price_cell(tid, int(d), "close" if col == "ciqClose" else col)
            if c == "unattributed" and (tid in att.refetched_ids or tid in att.factor_ids):
                c, h = "vendor_restatement", "id re-fetched for adjustment-factor restatement (U3)"
            if c == "unattributed" and col == "adjustmentfactor":
                c2, h2 = att.factor_cell(tid, int(d), "split")
                if c2 != "unattributed":
                    c, h = "upstream:" + c2, h2
            cls.append(c)
            how.append(h)
        diff["cls"], diff["how"] = cls, how
    diff.to_csv(out / f"diff_{label}.csv", index=False)
    return rec("factors/adjustment_factor_daily", ref, stats, diff), diff


def cmp_factors(a: Path, b: Path, table: str, att: Attributor, out: Path, ref: str) -> tuple[dict, pd.DataFrame]:
    fa, fb = pd.read_parquet(a), pd.read_parquet(b)
    stats, diff, only_a, only_b = diff_keyed(fa, fb, ["tradingItemId", "validFrom"], ignore=("snapshotDate",))
    stats["sha256_equal"] = sha256(a) == sha256(b)
    rows = []
    for side, df in (("a", only_a), ("b", only_b)):
        for r in df.itertuples(index=False):
            rows.append({"tradingItemId": int(r.tradingItemId), "validFrom": str(pd.Timestamp(r.validFrom).date()), "column": "row", "a": "present" if side == "a" else "absent",
                         "b": "present" if side == "b" else "absent"})
    if rows:
        diff = pd.concat([diff, pd.DataFrame(rows)], ignore_index=True)
    if len(diff):
        cls, how = [], []
        for tid, vf in zip(diff.tradingItemId.astype(int), days_of(diff.validFrom)):
            c, h = att.factor_cell(tid, int(vf), table)
            cls.append(c)
            how.append(h)
        diff["cls"], diff["how"] = cls, how
    diff.to_csv(out / f"diff_factors_{table}_{ref}.csv", index=False)
    return rec(f"factors/{table}", ref, stats, diff), diff


def ledger_frame(p: Path) -> pd.DataFrame:
    df = pd.read_parquet(p)
    df["childTradingItemId"] = df.childTradingItemId.fillna(-1).astype(np.int64) if "childTradingItemId" in df.columns else -1
    return df


def cmp_ledger(a: Path, b: Path, att: Attributor, out: Path, ref: str, extra_ignore=()) -> tuple[dict, pd.DataFrame]:
    la, lb = ledger_frame(a), ledger_frame(b)
    key = LEDGER_KEY + ["seq"]
    for df in (la, lb):
        df["seq"] = df.groupby(LEDGER_KEY).cumcount()
    stats, diff, only_a, only_b = diff_keyed(la, lb, key, ignore=LEDGER_IGNORE + tuple(extra_ignore))
    stats["sha256_equal"] = sha256(a) == sha256(b)
    rows = []
    for side, df in (("a", only_a), ("b", only_b)):
        for r in df.itertuples(index=False):
            rows.append({"tradingItemId": int(r.tradingItemId), "exDate": str(pd.Timestamp(r.exDate).date()), "eventType": r.eventType, "legType": r.legType,
                         "childTradingItemId": int(r.childTradingItemId), "seq": int(r.seq), "column": "row", "a": "present" if side == "a" else "absent",
                         "b": "present" if side == "b" else "absent", "eventId": r.eventId, "source": r.source, "status": r.status})
    if rows:
        diff = pd.concat([diff, pd.DataFrame(rows)], ignore_index=True)
    if len(diff):
        cls, how = [], []
        ev_a = la.set_index(la[key].astype(str).agg("|".join, axis=1)).eventId
        for r in diff.itertuples(index=False):
            k = (int(r.tradingItemId), int(days_of([r.exDate])[0]), str(r.eventType), str(r.legType), int(r.childTradingItemId))
            kstr = "|".join(str(x) for x in (r.tradingItemId, r.exDate if isinstance(r.exDate, str) else pd.Timestamp(r.exDate), r.eventType, r.legType, r.childTradingItemId, r.seq))
            eid = getattr(r, "eventId", None) if "eventId" in diff.columns else None
            if eid is None or (isinstance(eid, float) and np.isnan(eid)):
                eid = ev_a.get(kstr)
            c, h = att.ledger_row(k, eid)
            if c == "unattributed" and str(getattr(r, "column", "")) == "evidence" and "splitId=" in str(r.a) and "splitId=" in str(r.b):
                c, h = "tie_break", "evidence splitId differs only (paired split rows)"
            cls.append(c)
            how.append(h)
        diff["cls"], diff["how"] = cls, how
    diff.to_csv(out / f"diff_ledger_{ref}.csv", index=False)
    return rec("events/ca_ledger", ref, stats, diff), diff


def cmp_returns(a: Path, b: Path, att: Attributor, out: Path, ref: str, price_diff: pd.DataFrame, factor_diffs: list[pd.DataFrame], ledger_diff: pd.DataFrame,
                lifecycle_diff: pd.DataFrame, lineage_diff: pd.DataFrame, lineage_new: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    old, new = rr.load_returns(a), rr.load_returns(b)
    diff = rr.returns_diff(old, new)
    stats = {"rows_a": int(len(old["key"])), "rows_b": int(len(new["key"])), "rows_only_a": int((diff.kind == "removed").sum()) if len(diff) else 0,
             "rows_only_b": int((diff.kind == "added").sum()) if len(diff) else 0, "sha256_equal": sha256(a) == sha256(b)}
    if not len(diff):
        stats["diff_cells"] = 0
        diff.to_csv(out / f"diff_returns_{ref}.csv", index=False)
        return rec("derived/returns_total", ref, stats, diff), diff
    obs = rr.ObsDates(old, new)
    # upstream windows
    price_cells: dict[int, list] = {}
    if len(price_diff):
        for r in price_diff.itertuples(index=False):
            price_cells.setdefault(int(r.tradingItemId), []).append((int(days_of([r.date])[0]), r.column, getattr(r, "cls", "")))
    factor_ids: dict[int, list] = {}
    for fd in factor_diffs:
        if len(fd):
            for r in fd.itertuples(index=False):
                factor_ids.setdefault(int(r.tradingItemId), []).append((int(days_of([r.validFrom])[0]), getattr(r, "cls", "")))
    ledger_ids: dict[int, list] = {}
    if len(ledger_diff):
        for r in ledger_diff.itertuples(index=False):
            ledger_ids.setdefault(int(r.tradingItemId), []).append((int(days_of([r.exDate])[0]), getattr(r, "cls", "")))
            child = int(r.childTradingItemId)
            if child > 0:
                ledger_ids.setdefault(child, []).append((int(days_of([r.exDate])[0]), getattr(r, "cls", "")))
    lc_ids = {}
    if len(lifecycle_diff):
        for r in lifecycle_diff.itertuples(index=False):
            lc_ids.setdefault(int(r.tradingItemId), set()).add(getattr(r, "cls", ""))
    ln_ids = {}
    if len(lineage_diff):
        for r in lineage_diff.itertuples(index=False):
            for i in (int(r.oldId), int(r.newId)):
                ln_ids.setdefault(i, set()).add(getattr(r, "cls", ""))
    succ = {}
    for r in lineage_new.itertuples(index=False):
        succ.setdefault(int(r.newId), set()).add(int(r.oldId))
    cls, how = [], []
    for r in diff.itertuples(index=False):
        tid, day = int(r.tradingItemId), int(r.day)
        c, h = "unattributed", ""
        # price: the cell itself or the previous observation of the same id
        hits = [p for p in price_cells.get(tid, []) if p[0] <= day and (day - p[0]) <= 15]
        if hits:
            c, h = "upstream:" + (hits[-1][2] or "price"), f"price cell {day_str(hits[-1][0])} {hits[-1][1]}"
        if c == "unattributed":
            fh = [f for f in factor_ids.get(tid, []) if abs(f[0] - day) <= FACTOR_TOL_DAYS]
            if fh:
                c, h = "upstream:" + (fh[0][1] or "factor"), f"factor boundary {day_str(fh[0][0])}"
        if c == "unattributed":
            lh = [l for l in ledger_ids.get(tid, []) if -1 <= (day - l[0]) <= LEDGER_TOL_DAYS or obs.first_on_or_after(tid, l[0]) == day]
            if lh:
                c, h = "upstream:" + (lh[0][1] or "ledger"), f"ledger row exDate {day_str(lh[0][0])}"
        if c == "unattributed":
            for o in succ.get(tid, ()):
                lh = [l for l in ledger_ids.get(o, []) if -5 <= (day - l[0]) <= LEDGER_TOL_DAYS]
                if lh:
                    c, h = "upstream:" + (lh[0][1] or "ledger"), f"linked old id {o} ledger row exDate {day_str(lh[0][0])} (link row)"
                    break
        if c == "unattributed" and tid in lc_ids:
            c, h = "upstream:lifecycle_delta", ",".join(sorted(lc_ids[tid]))
        if c == "unattributed" and (tid in ln_ids or any(o in ln_ids or o in lc_ids or o in price_cells for o in succ.get(tid, ()))):
            c, h = "upstream:lineage_delta", "linked first row / old id changed"
        if c == "unattributed" and tid in att.new_ids:
            c, h = "new_id", "U1 new id"
        if c == "unattributed":
            for o in succ.get(tid, ()):
                oh = [p for p in price_cells.get(o, []) if p[0] <= day]
                if oh:
                    c, h = "upstream:" + (oh[-1][2] or "price"), f"old id {o} price cell {day_str(oh[-1][0])}"
                    break
        cls.append(c)
        how.append(h)
    diff["cls"], diff["how"] = cls, how
    diff.to_csv(out / f"diff_returns_{ref}.csv", index=False)
    return rec("derived/returns_total", ref, stats, diff), diff


def cmp_export_ledger(a: Path, b: Path, att: Attributor, out: Path, ref: str, ledger_diff: pd.DataFrame, price_diff: pd.DataFrame, factor_diffs: list | None = None) -> tuple[dict, pd.DataFrame]:
    ea, eb = pd.read_parquet(a), pd.read_parquet(b)
    stats, diff, only_a, only_b = diff_keyed(ea, eb, ["eventId"])
    stats["sha256_equal"] = sha256(a) == sha256(b)
    rows = []
    for side, df in (("a", only_a), ("b", only_b)):
        for r in df.itertuples(index=False):
            rows.append({"eventId": r.eventId, "column": "row", "a": "present" if side == "a" else "absent", "b": "present" if side == "b" else "absent",
                         "tradingItemId": r.tradingItemId, "exDate": str(pd.Timestamp(r.exDate).date()), "markerReason": getattr(r, "markerReason", "")})
    if rows:
        diff = pd.concat([diff, pd.DataFrame(rows)], ignore_index=True)
    ld_keys = {}
    if len(ledger_diff):
        for r in ledger_diff.itertuples(index=False):
            ld_keys.setdefault((int(r.tradingItemId), int(days_of([r.exDate])[0])), set()).add(getattr(r, "cls", ""))
    pc = {}
    if len(price_diff):
        for r in price_diff.itertuples(index=False):
            pc.setdefault(int(r.tradingItemId), []).append((int(days_of([r.date])[0]), getattr(r, "cls", "")))
    fids = {}
    for fd_ in (factor_diffs or []):
        if fd_ is not None and len(fd_):
            for r in fd_.itertuples(index=False):
                fids[int(r.tradingItemId)] = getattr(r, "cls", "") or "factor"
    ea_i = ea.set_index("eventId")
    if len(diff):
        cls, how = [], []
        for r in diff.itertuples(index=False):
            eid = r.eventId
            row = ea_i.loc[eid] if eid in ea_i.index else (eb.set_index("eventId").loc[eid] if eid in set(eb.eventId) else None)
            tid = int(row.tradingItemId) if row is not None else int(getattr(r, "tradingItemId", -1))
            ex = int(days_of([row.exDate])[0]) if row is not None else int(days_of([r.exDate])[0])
            c, h = "unattributed", ""
            k = (tid, ex)
            if k in ld_keys:
                c, h = "upstream:" + sorted(ld_keys[k])[0], "ledger row differs"
            else:
                near = [kk for kk in ld_keys if kk[0] == tid and abs(kk[1] - ex) <= LEDGER_TOL_DAYS]
                if near:
                    c, h = "upstream:" + sorted(ld_keys[near[0]])[0], "ledger row (same id, exDate within tol) differs"
            if c == "unattributed":
                child = int(row.childTradingItemId) if row is not None and str(row.childTradingItemId) not in ("", "nan", "None") and str(row.childTradingItemId).lstrip("-").isdigit() else -1
                for i in (tid, child):
                    hits = [p for p in pc.get(i, []) if abs(p[0] - ex) <= 7]
                    if hits:
                        c, h = "upstream:" + (hits[0][1] or "price"), f"anchor close {day_str(hits[0][0])} of {i} differs"
                        break
            if c == "unattributed" and str(getattr(r, "column", "")) in ("markerReason",) or (c == "unattributed" and "coacthist" in str(getattr(r, "markerReason", ""))):
                c2, h2 = att.lifecycle_id(tid)
                if c2 != "unattributed":
                    c, h = c2, h2
            if c == "unattributed":
                c2, h2 = att.ledger_row((tid, ex, str(row.eventType) if row is not None else "", str(row.legType) if row is not None else "", -1), eid)
                if c2 != "unattributed":
                    c, h = c2, h2
            if c == "unattributed" and str(getattr(r, "column", "")).endswith("Cache"):
                child = int(row.childTradingItemId) if row is not None and str(row.childTradingItemId).lstrip("-").isdigit() else -1
                for i in (tid, child):
                    if i in fids:
                        c, h = "upstream:" + fids[i], f"cache-unit conversion uses the split factor of {i}, which differs (factor table)"
                        break
            cls.append(c)
            how.append(h)
        diff["cls"], diff["how"] = cls, how
    diff.to_csv(out / f"diff_export_ledger_{ref}.csv", index=False)
    return rec("engine/ca_ledger_cache", ref, stats, diff), diff


def cmp_events(a: Path, b: Path, name: str, att: Attributor, out: Path, ref: str, lifecycle_diff: pd.DataFrame, price_diff: pd.DataFrame,
               lineage_diff: pd.DataFrame, regen: bool = False) -> tuple[dict, pd.DataFrame]:
    ea, eb = pd.read_parquet(a), pd.read_parquet(b)
    key = ["tradingItemId", "endDate"]
    stats, diff, only_a, only_b = diff_keyed(ea, eb, key)
    stats["sha256_equal"] = sha256(a) == sha256(b)
    rows = []
    for side, df in (("a", only_a), ("b", only_b)):
        for r in df.itertuples(index=False):
            rows.append({"tradingItemId": int(r.tradingItemId), "endDate": str(pd.Timestamp(r.endDate).date()), "column": "row", "a": "present" if side == "a" else "absent",
                         "b": "present" if side == "b" else "absent"})
    if rows:
        diff = pd.concat([diff, pd.DataFrame(rows)], ignore_index=True)
    lc_ids = best_class_by_id(lifecycle_diff)
    ln_ids = {}
    if len(lineage_diff):
        for r in lineage_diff.itertuples(index=False):
            for i in (int(r.oldId), int(r.newId)):
                ln_ids[i] = getattr(r, "cls", "")
    pc = {}
    if len(price_diff):
        for r in price_diff.itertuples(index=False):
            pc.setdefault(int(r.tradingItemId), []).append(int(days_of([r.date])[0]))
    succ_col = "successorTradingItemId"
    ea_i = ea.set_index(ea[key].astype(str).agg("|".join, axis=1))
    if len(diff):
        cls, how = [], []
        for r in diff.itertuples(index=False):
            tid, ed = int(r.tradingItemId), int(days_of([r.endDate])[0])
            c, h = "unattributed", ""
            if tid in att.events_adopted:
                c, h = "transition:events_adopted", f"events delta adopted by the daily merge ({att.events_adopted[tid]}; T6/qa/events_transitions.csv)"
            if c == "unattributed" and str(r.column) in ("reviewFlag", "evidence", "notes", "actionText") and str(r.a) != "nan" and str(r.b) != "nan":
                ta, tb = [x for x in str(r.a).split(";") if x], [x for x in str(r.b).split(";") if x]
                if set(ta) == set(tb) and len(ta) != len(tb):
                    c, h = "override_reapply_accumulation", f"same tags, multiplicity {len(ta)} vs {len(tb)} (rc2f tag appended on every daily re-application; R-D2 P1-2)"
            if c == "unattributed" and tid in lc_ids:
                c, h = "upstream:" + (lc_ids[tid] or "lifecycle_delta"), "lifecycle row of the id differs"
            if c == "unattributed" and tid in ln_ids:
                c, h = "upstream:" + (ln_ids[tid] or "lineage_delta"), "lineage pair of the id differs"
            if c == "unattributed":
                kstr = f"{tid}|{pd.Timestamp(r.endDate)}"
                succ = None
                if kstr in ea_i.index and succ_col in ea_i.columns:
                    v = ea_i.loc[kstr, succ_col]
                    succ = int(v) if pd.notna(v) else None
                for i in (tid, succ):
                    if i is not None and any(abs(d - ed) <= 30 for d in pc.get(i, [])):
                        c, h = "upstream:price", f"anchor close of {i} near endDate differs"
                        break
            if c == "unattributed":
                c2, h2 = att.lifecycle_id(tid)
                if c2 != "unattributed":
                    c, h = c2, h2
            if c == "unattributed" and tid in att.ledger_ids:
                c, h = "upstream:ledger_revision", "ledger rows of the id revised (settlement scope label)"
            if c == "unattributed" and regen:
                c, h = "transition:events_regeneration", "daily T6 regenerates events (successor from lineage relist/rename pairs, rc2f-patched inputs); the snapshot rows came from the rc2-era build (R-D2 P0-2/P1-4 transition list)"
            cls.append(c)
            how.append(h)
        diff["cls"], diff["how"] = cls, how
    diff.to_csv(out / f"diff_{name.replace('/', '_')}_{ref}.csv", index=False)
    return rec(name, ref, stats, diff), diff


EVIDENCE_HASH = re.compile(r"eventsFile=sha256:[0-9a-f]+")


def cmp_lifecycle(a: Path, b: Path, att: Attributor, out: Path, ref: str, name="master/lifecycle", events_ids: set | None = None, snapshot: Path | None = None, regen: bool = False) -> tuple[dict, pd.DataFrame]:
    la, lb = pd.read_parquet(a), pd.read_parquet(b)
    n_hash_only = 0
    if "evidence" in la.columns and "evidence" in lb.columns:
        ea, eb = la.evidence.astype(str), lb.evidence.astype(str)
        la = la.assign(evidence=ea.str.replace(EVIDENCE_HASH, "eventsFile=<sha>", regex=True))
        lb = lb.assign(evidence=eb.str.replace(EVIDENCE_HASH, "eventsFile=<sha>", regex=True))
        ia, ib = la.set_index("tradingItemId").evidence, lb.set_index("tradingItemId").evidence
        common = ia.index.intersection(ib.index)
        raw_a, raw_b = ea.set_axis(la.tradingItemId).reindex(common), eb.set_axis(lb.tradingItemId).reindex(common)
        n_hash_only = int(((raw_a != raw_b) & (ia.reindex(common) == ib.reindex(common))).sum())
    stats, diff, only_a, only_b = diff_keyed(la, lb, ["tradingItemId"], ignore=("observedAt",))
    stats["evidence_cells_differing_only_by_eventsFile_sha"] = n_hash_only
    snap_lc = pd.read_parquet(snapshot / "master" / "lifecycle.parquet").set_index("tradingItemId") if snapshot is not None and (snapshot / "master" / "lifecycle.parquet").exists() else None
    stats["sha256_equal"] = sha256(a) == sha256(b)
    rows = [{"tradingItemId": int(r.tradingItemId), "column": "row", "a": "present", "b": "absent"} for r in only_a.itertuples(index=False)]
    rows += [{"tradingItemId": int(r.tradingItemId), "column": "row", "a": "absent", "b": "present"} for r in only_b.itertuples(index=False)]
    if rows:
        diff = pd.concat([diff, pd.DataFrame(rows)], ignore_index=True)
    if len(diff):
        cls, how = [], []
        for tid, col, av, bv in zip(diff.tradingItemId.astype(int), diff.column, diff.a, diff.b):
            c, h = att.lifecycle_id(tid, col)
            if c == "unattributed" and col in ("evidence",):
                c2, h2 = att.lifecycle_id(tid, None)
                c, h = (c2, h2) if c2 != "unattributed" else (c, h)
            if c == "unattributed" and tid in att.events_adopted and col in ("eventEndDate", "endReason", "status", "lastTradeDate", "lastTradeSource", "evidence", "securityEndDate"):
                c, h = "transition:events_adopted", f"event marker from an adopted events delta ({att.events_adopted[tid]})"
            if c == "unattributed" and events_ids and tid in events_ids and col in ("eventEndDate", "endReason", "status", "lastTradeDate", "lastTradeSource", "evidence"):
                c, h = "events_regeneration", "events row of the id differs between the sides (marker source)"
            # path dependence: the reference equals the snapshot, the chain moved the value on a chain day after the first one
            if snap_lc is not None and col in ("firstTradeDate", "firstTradeSource", "firstPriceDate") and tid in snap_lc.index:
                sv = snap_lc.at[tid, col]
                sv_s = str(pd.Timestamp(sv).date()) if col != "firstTradeSource" and pd.notna(sv) else str(sv)
                labels = [x for x in h.split(",") if x.startswith(col + "@")]
                later = [x for x in labels if x.split("@", 1)[1].startswith("chain") and x.split("@", 1)[1] != att.first_label]
                if str(bv) == sv_s and str(av) != sv_s and later:
                    c, h = "path_dependence", f"reference == snapshot ({sv_s}); chain re-evaluated the lineage first-date lift on {','.join(l.split('@', 1)[1] for l in later)} on top of the previous day's lifecycle (R-D2 P1-1a)"
            cls.append(c)
            how.append(h)
        diff["cls"], diff["how"] = cls, how
        pd_ids = set(diff.loc[(diff.cls == "path_dependence"), "tradingItemId"].astype(int))
        m = diff.tradingItemId.astype(int).isin(pd_ids) & diff.column.isin(["firstTradeSource", "firstPriceDate", "evidence"]) & (diff.cls == "unattributed")
        diff.loc[m, "cls"] = "path_dependence"
        diff.loc[m, "how"] = "follows the firstTradeDate path dependence of the same id"
        if events_ids:
            m2 = diff.tradingItemId.astype(int).isin(events_ids) & (diff.cls == "unattributed") & diff.column.isin(["evidence", "eventEndDate", "endReason", "status", "lastTradeDate"])
            diff.loc[m2, "cls"] = "events_regeneration"
            diff.loc[m2, "how"] = "event marker of the id differs between the sides' marker sources (previous build's events vs snapshot events; D2 P2-7 lag)"
        if regen:
            # snapshot comparison: the daily U4 takes event markers from the build's (rc2f-patched, regenerated) events; the snapshot's T1 lifecycle
            # was computed with the rc1-era marker source -> eventEndDate NaT->date and evidence 'event=' text while lastTradeDate stays
            ltd_ids = set(diff.loc[diff.column == "lastTradeDate", "tradingItemId"].astype(int))
            m3 = (diff.cls == "unattributed") & diff.column.isin(["eventEndDate", "evidence"]) & ~diff.tradingItemId.astype(int).isin(ltd_ids)
            diff.loc[m3, "cls"] = "transition:event_marker"
            diff.loc[m3, "how"] = "daily lifecycle marker source = build events (rc2f-patched); snapshot lifecycle had no event marker for the id; lastTradeDate unchanged"
            m4 = (diff.cls == "unattributed") & diff.column.isin(["eventEndDate", "evidence", "endReason", "status", "lastTradeDate", "lastTradeSource"])
            diff.loc[m4, "cls"] = "transition:event_marker"
            diff.loc[m4, "how"] = "daily lifecycle marker source = build events; marker changes the end date/reason vs the snapshot (R-D2 P1-1/P1-4 transition list)"
    diff.to_csv(out / f"diff_{name.replace('/', '_')}_{ref}.csv", index=False)
    return rec(name, ref, stats, diff), diff


def events_ids_differing(a: Path, b: Path) -> set:
    """ids whose events/corporate_actions rows differ between the sides (structural columns only; used as a marker-source explanation)."""
    cols = ["tradingItemId", "endDate", "settlementDate", "actionCode", "settlementReturn", "cashPerShare", "successorTradingItemId", "successorRatio", "endDateSource", "hasStockLeg", "hasCashLeg", "cacheUnitStatus"]
    ea, eb = pd.read_parquet(a), pd.read_parquet(b)
    cols = [c for c in cols if c in ea.columns and c in eb.columns]
    _, d, oa, ob = diff_keyed(ea[cols], eb[cols], ["tradingItemId", "endDate"])
    ids = set(int(i) for i in d.tradingItemId) if len(d) else set()
    ids |= set(int(i) for i in oa.tradingItemId) | set(int(i) for i in ob.tradingItemId)
    return ids


def cmp_lifecycle_dates(a: Path, b: Path, att: Attributor, out: Path, ref: str, lifecycle_diff: pd.DataFrame, events_diff: pd.DataFrame, regen: bool = False) -> tuple[dict, pd.DataFrame]:
    la, lb = pd.read_parquet(a), pd.read_parquet(b)
    stats, diff, only_a, only_b = diff_keyed(la, lb, ["tradingItemId"])
    stats["sha256_equal"] = sha256(a) == sha256(b)
    rows = [{"tradingItemId": int(r.tradingItemId), "column": "row", "a": "present", "b": "absent"} for r in only_a.itertuples(index=False)]
    rows += [{"tradingItemId": int(r.tradingItemId), "column": "row", "a": "absent", "b": "present"} for r in only_b.itertuples(index=False)]
    if rows:
        diff = pd.concat([diff, pd.DataFrame(rows)], ignore_index=True)
    lc_ids = best_class_by_id(lifecycle_diff)
    ev_ids = best_class_by_id(events_diff)
    if len(diff):
        cls, how = [], []
        for tid, col in zip(diff.tradingItemId.astype(int), diff.column):
            c, h = "unattributed", ""
            if tid in lc_ids:
                c, h = "upstream:" + (lc_ids[tid] or "lifecycle_delta"), "lifecycle row differs"
            elif tid in ev_ids:
                c, h = "upstream:" + (ev_ids[tid] or "events"), "events row differs"
            else:
                c, h = att.lifecycle_id(tid, col)
            if c == "unattributed" and tid in att.events_adopted:
                c, h = "transition:events_adopted", f"dates rebuilt from an adopted events delta ({att.events_adopted[tid]})"
            if c == "unattributed" and tid in att.ledger_ids and col in ("payDate", "payDateSource"):
                c, h = "upstream:ledger_revision", "payDate follows the ledger row of the id"
            if c == "unattributed" and regen and col in ("payDate", "payDateSource"):
                c, h = "transition:lifecycle_dates_payDate", "daily finalize fills payDate from the ledger (D2 section 12-2); the snapshot row had no payDate"
            cls.append(c)
            how.append(h)
        diff["cls"], diff["how"] = cls, how
    diff.to_csv(out / f"diff_master_lifecycle_dates_{ref}.csv", index=False)
    return rec("master/lifecycle_dates", ref, stats, diff), diff


def cmp_lineage(a: Path, b: Path, att: Attributor, out: Path, ref: str, lifecycle_diff: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    la, lb = pd.read_parquet(a), pd.read_parquet(b)
    stats, diff, only_a, only_b = diff_keyed(la, lb, ["oldId", "newId"])
    stats["sha256_equal"] = sha256(a) == sha256(b)
    rows = []
    for side, df in (("a", only_a), ("b", only_b)):
        for r in df.itertuples(index=False):
            rows.append({"oldId": int(r.oldId), "newId": int(r.newId), "column": "row", "a": "present" if side == "a" else "absent", "b": "present" if side == "b" else "absent"})
    if rows:
        diff = pd.concat([diff, pd.DataFrame(rows)], ignore_index=True)
    lc_ids = {int(r.tradingItemId) for r in lifecycle_diff.itertuples(index=False)} if len(lifecycle_diff) else set()
    if len(diff):
        cls, how = [], []
        for o, n, col in zip(diff.oldId.astype(int), diff.newId.astype(int), diff.column):
            c, h = "unattributed", ""
            if (o, n) in att.lineage_pairs:
                c, h = "lineage_delta", ",".join(sorted(att.lineage_pairs[(o, n)]))
            elif o in lc_ids or n in lc_ids:
                c, h = "upstream:lifecycle_delta", "old/new id lifecycle differs (chainGap / chainable recomputed)"
            elif o in att.new_ids or n in att.new_ids:
                c, h = "new_id", "U1 new id"
            elif o in att.lifecycle_ids or n in att.lifecycle_ids:
                c, h = "lifecycle_delta", "old/new id in lifecycle_revisions"
            elif col == "chainGapTradingDays":
                c, h = "lineage_delta", "chainGapTradingDays recomputed daily on the build calendar/lifecycle (D2 section 12-2 rule)"
            cls.append(c)
            how.append(h)
        diff["cls"], diff["how"] = cls, how
    diff.to_csv(out / f"diff_master_lineage_{ref}.csv", index=False)
    return rec("master/lineage", ref, stats, diff), diff


def cmp_membership(a: Path, b: Path, att: Attributor, out: Path, ref: str, price_diff: pd.DataFrame, lifecycle_diff: pd.DataFrame, mc_diff: pd.DataFrame | None = None) -> tuple[dict, pd.DataFrame]:
    ma, mb = pd.read_parquet(a), pd.read_parquet(b)
    stats, diff, only_a, only_b = diff_keyed(ma, mb, ["universe", "tradingItemId", "fromDate"])
    stats["sha256_equal"] = sha256(a) == sha256(b)
    rows = []
    for side, df in (("a", only_a), ("b", only_b)):
        for r in df.itertuples(index=False):
            rows.append({"universe": r.universe, "tradingItemId": int(r.tradingItemId), "fromDate": str(pd.Timestamp(r.fromDate).date()), "column": "row",
                         "a": "present" if side == "a" else "absent", "b": "present" if side == "b" else "absent"})
    if rows:
        diff = pd.concat([diff, pd.DataFrame(rows)], ignore_index=True)
    pids = set(int(i) for i in price_diff.tradingItemId) if len(price_diff) else set()
    lids = set(int(i) for i in lifecycle_diff.tradingItemId) if len(lifecycle_diff) else set()
    mids = {int(r.tradingItemId): getattr(r, "cls", "") for r in mc_diff.itertuples(index=False)} if mc_diff is not None and len(mc_diff) else {}
    if len(diff):
        cls, how = [], []
        for tid in diff.tradingItemId.astype(int):
            if tid in pids:
                c, h = "upstream:price", "price cells of the id differ"
            elif tid in lids:
                c, h = "upstream:lifecycle_delta", "lifecycle of the id differs"
            elif tid in mids:
                c, h = "upstream:" + (mids[tid] or "marketcap"), "marketcap cells of the id differ"
            elif tid in att.new_ids:
                c, h = "new_id", ""
            else:
                c, h = "unattributed", ""
            cls.append(c)
            how.append(h)
        diff["cls"], diff["how"] = cls, how
    diff.to_csv(out / f"diff_universe_membership_{ref}.csv", index=False)
    return rec("universe/membership", ref, stats, diff), diff


CLASS_PRIORITY = ["path_dependence", "transition:events_adopted", "transition:events_regeneration", "transition:event_marker", "events_regeneration", "identity_delta", "full_aggregation", "override_reapply_accumulation", "vendor_late_load", "vendor_restatement", "refetch_window", "lineage_delta", "lifecycle_delta", "new_id", "override", "construction", "tie_break", "ledger_revision"]


def best_class_by_id(diff: pd.DataFrame, col: str = "tradingItemId") -> dict:
    """id -> the most specific attributed class among its differing cells (unattributed only when nothing else)."""
    res = {}
    if diff is None or not len(diff) or "cls" not in diff.columns:
        return res
    for tid, g in diff.groupby(diff[col].astype(int)):
        classes = set(g.cls.astype(str))
        pick = next((c for c in CLASS_PRIORITY if c in classes), None)
        res[int(tid)] = pick or (sorted(classes - {"unattributed"})[0] if classes - {"unattributed"} else "unattributed")
    return res


def cmp_exclusions(a: Path, b: Path, att: Attributor, out: Path, ref: str, lifecycle_diff: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    ea, eb = pd.read_parquet(a), pd.read_parquet(b)
    stats, diff, only_a, only_b = diff_keyed(ea, eb, ["tradingItemId", "fromDate"])
    stats["sha256_equal"] = sha256(a) == sha256(b)
    rows = []
    for side, df in (("a", only_a), ("b", only_b)):
        for r in df.itertuples(index=False):
            rows.append({"tradingItemId": int(r.tradingItemId), "fromDate": str(pd.Timestamp(r.fromDate).date()), "column": "row", "a": "present" if side == "a" else "absent",
                         "b": "present" if side == "b" else "absent", "reason": getattr(r, "reason", "")})
    if rows:
        diff = pd.concat([diff, pd.DataFrame(rows)], ignore_index=True)
    lc = best_class_by_id(lifecycle_diff)
    if len(diff):
        cls, how = [], []
        for tid in diff.tradingItemId.astype(int):
            if tid in lc:
                c, h = "upstream:" + (lc[tid] or "lifecycle_delta"), "lifecycle of the id differs (same-security overlap re-evaluated)"
            else:
                c, h = att.lifecycle_id(tid, None)
            cls.append(c)
            how.append(h)
        diff["cls"], diff["how"] = cls, how
    diff.to_csv(out / f"diff_master_tradable_exclusions_{ref}.csv", index=False)
    return rec("master/tradable_exclusions", ref, stats, diff), diff


def cmp_calendar(a: Path, b: Path, att: Attributor, out: Path, ref: str) -> tuple[dict, pd.DataFrame]:
    ca, cb = pd.read_parquet(a), pd.read_parquet(b)
    stats, diff, only_a, only_b = diff_keyed(ca, cb, ["date"])
    stats["sha256_equal"] = sha256(a) == sha256(b)
    rows = [{"date": str(pd.Timestamp(r.date).date()), "column": "row", "a": "present", "b": "absent"} for r in only_a.itertuples(index=False)]
    rows += [{"date": str(pd.Timestamp(r.date).date()), "column": "row", "a": "absent", "b": "present"} for r in only_b.itertuples(index=False)]
    if rows:
        diff = pd.concat([diff, pd.DataFrame(rows)], ignore_index=True)
    if len(diff):
        fetch_from = min((pd.Timestamp(sd.fetch_from) for sd in att.sides if sd.fetch_from), default=None)
        cls, how = [], []
        for d_, col in zip(pd.to_datetime(diff.date), diff.column):
            if fetch_from is not None and d_ >= fetch_from and col in ("nClose", "rollingMedian", "row"):
                cls.append("refetch_window")
                how.append("close count of a re-fetched date (U2 replaces stored counts inside its fetch range)")
            elif col == "rollingMedian":
                cls.append("refetch_window")
                how.append("centered rolling median window touches re-fetched dates")
            else:
                cls.append("unattributed")
                how.append("")
        diff["cls"], diff["how"] = cls, how
    diff.to_csv(out / f"diff_calendar_{ref}.csv", index=False)
    return rec("calendar", ref, stats, diff), diff


def cmp_sha(a: Path, b: Path, name: str, ref: str) -> dict:
    ra_, rb_ = pq.ParquetFile(a).metadata.num_rows, pq.ParquetFile(b).metadata.num_rows
    eq = sha256(a) == sha256(b)
    return {"file": name, "ref": ref, "rows_a": ra_, "rows_b": rb_, "only_a": "", "only_b": "", "sha256_equal": eq, "diff_cells": 0 if eq else None, "by_class": {},
            "unattributed": 0, "note": "sha only" if eq else "sha differs (rows compared in the keyed diff)" }


def cmp_marketcap(a: Path, b: Path, ref: str, out: Path, price_diff: pd.DataFrame, lb: dict | None = None) -> tuple[dict, pd.DataFrame]:
    stats, diff, only_a, only_b = diff_long(a, b, cols=["marketCap_USD", "sharesOutstanding"])
    rows = [{"tradingItemId": int(key_ids([k])[0]), "date": day_str(key_days([k])[0]), "column": "row", "a": "present", "b": "absent"} for k in only_a[:100000]]
    rows += [{"tradingItemId": int(key_ids([k])[0]), "date": day_str(key_days([k])[0]), "column": "row", "a": "absent", "b": "present"} for k in only_b[:100000]]
    if rows:
        diff = pd.concat([diff, pd.DataFrame(rows)], ignore_index=True)
    pc = {}
    if len(price_diff):
        for r in price_diff.itertuples(index=False):
            pc.setdefault(int(r.tradingItemId), set()).add(int(days_of([r.date])[0]))
    if len(diff):
        cls = []
        mc_load = bool((lb or {}).get("marketcap"))
        for tid, d in zip(diff.tradingItemId.astype(int), days_of(diff.date)):
            cls.append("upstream:price" if int(d) in pc.get(tid, set()) else ("vendor_late_load" if mc_load else "unattributed"))
        diff["cls"] = cls
        diff["how"] = np.where(diff["cls"] == "upstream:price", "close of the day differs", np.where(diff["cls"] == "vendor_late_load", "ciqmarketcap loaded between the two T5 runs (probe)", ""))
    diff.to_csv(out / f"diff_derived_marketcap_{ref}.csv", index=False)
    return rec("derived/marketcap", ref, stats, diff, note="marketCap_USD/sharesOutstanding; T5 re-receives ciqmarketcap in full every run (vendor intraday changes are not logged)"), diff


# --------------------------------------------------------------------------------------
# probes -> loads between runs
# --------------------------------------------------------------------------------------
def load_probe(root: Path, label: str) -> dict:
    p = root / "probes" / f"xfl_control_{label}.json"
    if not p.exists():
        return {}
    d = json.loads(p.read_text())
    return {(r["tablename"], r["loadsubid"]): r for r in d.get("rows", [])} if "rows" in d else {}


def loads_between(root: Path, earlier_labels: list[str], later_labels: list[str]) -> dict:
    """Tables whose xfl_control load state changed between the earliest 'before' probe of one side and the latest 'after' probe of the other."""
    def state(labels):
        st = {}
        for lab in labels:
            for k, r in load_probe(root, lab).items():
                st[k] = max(st.get(k, ""), str(r.get("enddatetime", "")))
        return st
    a, b = state(earlier_labels), state(later_labels)
    changed = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    groups = {"prices": ("ciqPriceEquity", "MIPrice", "ciqPriceEquityDivAdjFactor"), "factors": ("ciqSplit", "ciqSplitCache", "ciqPriceEquityDivAdjFactor"),
              "dividends": ("ciqDividend", "ciqDividendCache", "ciqExchangeRate", "ciqCurrency"), "identity": ("ciqTradingItem", "ciqSecurity", "ciqGvkeyIID"),
              "marketcap": ("ciqMarketCap",)}
    res = {g: any(k[0] in t for k in changed) for g, t in groups.items()}
    res["changed"] = [f"{k[0]}#{k[1]}: {a.get(k)} -> {b.get(k)}" for k in changed]
    return res


# --------------------------------------------------------------------------------------
# semantic sanity checks (value-level; the file-diff classes cannot see a wrong value that both sides share)
# --------------------------------------------------------------------------------------
def extreme_returns(pub: Path, thr: float = 5.0) -> pd.DataFrame:
    t = pq.read_table(pub / "derived/returns_total.parquet", columns=["tradingItemId", "date", "ret", "retFlag"])
    r = t["ret"].to_numpy(zero_copy_only=False)
    m = np.abs(np.nan_to_num(r, nan=0.0)) > thr
    df = t.filter(pa.array(m)).to_pandas()
    return df.sort_values(["tradingItemId", "date"]).reset_index(drop=True)


def duplicate_child_legs(ledger_path: Path, lineage_path: Path, tol_days: int = 3) -> pd.DataFrame:
    """Stock legs with the same (child, ratio) on two different parents within tol_days (a leg moved onto a stitched/linked new id while the
    original stays) - the R-D2 P0-2 signature (GGP 578353660 -> 115485825, 2010-11-09/10)."""
    led = pd.read_parquet(ledger_path, columns=["eventId", "tradingItemId", "exDate", "eventType", "legType", "childTradingItemId", "childRatio", "status", "source", "exportExclude"])
    led = led[(led.legType == "stock") & led.childTradingItemId.notna() & (led.childTradingItemId > 0) & ~led.exportExclude.fillna(False).astype(bool)]
    led = led.assign(ratio=led.childRatio.round(6), day=days_of(led.exDate))
    ln = pd.read_parquet(lineage_path, columns=["oldId", "newId"])
    linked = set(zip(ln.oldId.astype(int), ln.newId.astype(int))) | set(zip(ln.newId.astype(int), ln.oldId.astype(int)))
    rows = []
    for (child, ratio), g in led.groupby(["childTradingItemId", "ratio"]):
        if len(g) < 2:
            continue
        g = g.sort_values("day")
        recs = g.to_dict("records")
        for i in range(len(recs)):
            for j in range(i + 1, len(recs)):
                a, b = recs[i], recs[j]
                if a["tradingItemId"] != b["tradingItemId"] and abs(int(a["day"]) - int(b["day"])) <= tol_days:
                    rows.append({"childTradingItemId": int(child), "ratio": ratio, "parentA": int(a["tradingItemId"]), "exDateA": day_str(a["day"]), "eventIdA": a["eventId"], "statusA": a["status"],
                                 "parentB": int(b["tradingItemId"]), "exDateB": day_str(b["day"]), "eventIdB": b["eventId"], "statusB": b["status"],
                                 "parents_linked": (int(a["tradingItemId"]), int(b["tradingItemId"])) in linked})
    return pd.DataFrame(rows)


def semantic_checks(pub_chain: Path, start: Path, out: Path, label: str) -> dict:
    res = {}
    ex_c, ex_s = extreme_returns(pub_chain), extreme_returns(start)
    kc = set(zip(ex_c.tradingItemId.astype(int), days_of(ex_c.date))) if len(ex_c) else set()
    ks = set(zip(ex_s.tradingItemId.astype(int), days_of(ex_s.date))) if len(ex_s) else set()
    snap_asof = json.loads((start / "manifest.json").read_text()).get("asOfDate")
    snap_day = int(days_of([pd.Timestamp(snap_asof)])[0])
    new = ex_c[[(int(i), int(d)) not in ks for i, d in zip(ex_c.tradingItemId, days_of(ex_c.date))]] if len(ex_c) else ex_c
    new = new.assign(after_snapshot_asof=days_of(new.date) > snap_day) if len(new) else new
    new.sort_values("ret", key=lambda x: -x.abs()).to_csv(out / f"semantic_extreme_returns_new_{label}.csv", index=False)
    res["extreme_returns"] = {"threshold": 5.0, "chain_cells": int(len(ex_c)), "snapshot_cells": int(len(ex_s)), "new_in_chain": int(len(new)),
                              "new_after_snapshot_asof": int(new.after_snapshot_asof.sum()) if len(new) else 0,
                              "new_historical": int((~new.after_snapshot_asof).sum()) if len(new) else 0,
                              "top": [{"tradingItemId": int(r.tradingItemId), "date": str(pd.Timestamp(r.date).date()), "ret": float(r.ret), "retFlag": int(r.retFlag)}
                                      for r in new.sort_values("ret", key=lambda x: -x.abs()).head(5).itertuples(index=False)]}
    dc = duplicate_child_legs(pub_chain / "events/ca_ledger.parquet", pub_chain / "master/lineage.parquet")
    ds = duplicate_child_legs(start / "events/ca_ledger.parquet", start / "master/lineage.parquet")
    key = lambda df: set(zip(df.childTradingItemId, df.ratio, df.parentA, df.exDateA, df.parentB, df.exDateB)) if len(df) else set()
    new_pairs = dc[[k not in key(ds) for k in zip(dc.childTradingItemId, dc.ratio, dc.parentA, dc.exDateA, dc.parentB, dc.exDateB)]] if len(dc) else dc
    dc.to_csv(out / f"semantic_duplicate_child_legs_{label}.csv", index=False)
    res["duplicate_child_legs"] = {"tol_days": 3, "chain_pairs": int(len(dc)), "snapshot_pairs": int(len(ds)), "new_in_chain": int(len(new_pairs)),
                                   "new_linked_parents": int(new_pairs.parents_linked.sum()) if len(new_pairs) else 0,
                                   "examples": new_pairs.head(5).to_dict("records")}
    (out / f"semantic_checks_{label}.json").write_text(json.dumps(res, indent=1, default=str))
    return res


def compare_day0(root: Path, args, start: Path, start_ids: set, price_win: pd.DataFrame, tie_break, out: Path) -> tuple[list[dict], dict]:
    """day0 (same-asOf re-run of the snapshot through the daily path) vs the snapshot itself (the snapshot path's output at that asOf)."""
    asof = json.loads((start / "manifest.json").read_text()).get("asOfDate")
    tag = asof[5:].replace("-", "")
    d0 = BuildInfo(root / f"day0_{tag}", root / f"pub_day0_{tag}", f"day0_{tag}")
    if not (d0.pub / "base_data.parquet").exists():
        return [], {}
    lb = loads_between(root, [f"before_day0_{tag}"], [f"after_day0_{tag}"])
    att = Attributor([d0], start_ids, price_win, {"prices": False, "factors": False, "dividends": False, "marketcap": False}, tie_break)
    pub_c, pub_o, ref = d0.pub, start, f"snapshot_day0_{tag}"
    R = []
    r_bd, d_bd = cmp_base_data(pub_c / "base_data.parquet", pub_o / "base_data.parquet", att, out, f"base_data_{ref}", ref)
    R.append(r_bd)
    fd = []
    for t in ("split", "dividend"):
        r_f, d_f = cmp_factors(pub_c / f"factors/{t}.parquet", pub_o / f"factors/{t}.parquet", t, att, out, ref)
        R.append(r_f)
        fd.append(d_f)
    ev_ids = events_ids_differing(pub_c / "events/corporate_actions.parquet", pub_o / "events/corporate_actions.parquet")
    r_lc, d_lc = cmp_lifecycle(pub_c / "master/lifecycle.parquet", pub_o / "master/lifecycle.parquet", att, out, ref, events_ids=ev_ids, regen=True)
    R.append(r_lc)
    r_ln, d_ln = cmp_lineage(pub_c / "master/lineage.parquet", pub_o / "master/lineage.parquet", att, out, ref, d_lc)
    R.append(r_ln)
    r_ev, d_ev = cmp_events(pub_c / "events/corporate_actions.parquet", pub_o / "events/corporate_actions.parquet", "events/corporate_actions", att, out, ref, d_lc, d_bd, d_ln, regen=True)
    R.append(r_ev)
    r_ld, d_ld = cmp_lifecycle_dates(pub_c / "master/lifecycle_dates.parquet", pub_o / "master/lifecycle_dates.parquet", att, out, ref, d_lc, d_ev, regen=True)
    R.append(r_ld)
    r_lg, d_lg = cmp_ledger(pub_c / "events/ca_ledger.parquet", pub_o / "events/ca_ledger.parquet", att, out, ref)
    R.append(r_lg)
    r_rt, d_rt = cmp_returns(pub_c / "derived/returns_total.parquet", pub_o / "derived/returns_total.parquet", att, out, ref, d_bd, fd, d_lg, d_lc, d_ln,
                             pd.read_parquet(pub_c / "master/lineage.parquet"))
    R.append(r_rt)
    r_ex, _ = cmp_export_ledger(pub_c / "engine/ca_ledger_cache.parquet", pub_o / "engine/ca_ledger_cache.parquet", att, out, ref, d_lg, d_bd, fd)
    R.append(r_ex)
    r_ec, _ = cmp_events(pub_c / "engine/events_cache.parquet", pub_o / "engine/events_cache.parquet", "engine/events_cache", att, out, ref, d_lc, d_bd, d_ln, regen=True)
    R.append(r_ec)
    r_mc, d_mc = cmp_marketcap(pub_c / "derived/marketcap.parquet", pub_o / "derived/marketcap.parquet", ref, out, d_bd, {"marketcap": True})
    r_mc["note"] = "T5 re-receives ciqmarketcap in full: vendor intraday share/marketcap updates since the snapshot are expected (labelled vendor_late_load)"
    R.append(r_mc)
    r_mb, _ = cmp_membership(pub_c / "universe/membership.parquet", pub_o / "universe/membership.parquet", att, out, ref, d_bd, d_lc, d_mc)
    R.append(r_mb)
    # transition lists (R-D2 section 7): events rows / lifecycle firstTradeDate / lineage chainable that the daily path changes at the same asOf
    tr = {}
    if len(d_ev):
        ev_ids = d_ev.tradingItemId.astype(int).unique()
        tr["events_ids_changed"] = int(len(ev_ids))
        d_ev.to_csv(out / f"transition_events_{tag}.csv", index=False)
    if len(d_lc):
        ft = d_lc[d_lc.column == "firstTradeDate"]
        tr["lifecycle_firstTradeDate_ids"] = int(ft.tradingItemId.nunique())
        tr["lifecycle_ids_changed_by_column"] = d_lc.groupby("column").tradingItemId.nunique().to_dict()
        d_lc.to_csv(out / f"transition_lifecycle_{tag}.csv", index=False)
    if len(d_ln):
        tr["lineage_cells_by_column"] = d_ln.groupby("column").size().to_dict()
        d_ln.to_csv(out / f"transition_lineage_{tag}.csv", index=False)
    sem = semantic_checks(pub_c, start, out, f"day0_{tag}")
    if len(d0.events_transitions):
        tr["events_transitions_by_kind"] = d0.events_transitions.kind.value_counts().to_dict()
    tr["lifecycle_end_dates_carried_ids"] = len(d0.end_dates_carried)
    return R, {"day0_asOf": asof, "loads_between_day0_run": lb, "transition": tr, "semantic_day0": sem}


# --------------------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------------------
def run_compare(root: Path, args) -> dict:
    out = root / "compare"
    out.mkdir(parents=True, exist_ok=True)
    days = list(args.days)
    tag = days[-1][5:].replace("-", "")
    chain = [BuildInfo(root / f"b{i + 1}_{d[5:].replace('-', '')}", root / f"pub_{d[5:].replace('-', '')}", f"chain_{d[5:].replace('-', '')}") for i, d in enumerate(days)]
    one = BuildInfo(root / f"one_{tag}", root / f"pub_one_{tag}", f"onestep_{tag}")
    start = Path(args.start)
    start_ids = set(pd.read_parquet(start / "master" / "instruments.parquet", columns=["tradingItemId"]).tradingItemId.astype(int))
    pub_c, pub_o = chain[-1].pub, one.pub
    price_win = load_price_windows(getattr(args, "overrides_glob", dr.OVERRIDES_GLOB_DEFAULT), sorted(start_ids))
    tb_path = Path("/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad/stage_daily/D1/attributed_diffs_vs_rc2f2.csv")
    tie_break = pd.read_csv(tb_path) if tb_path.exists() else None
    chain_labels = [f"before_chain_{d[5:].replace('-', '')}" for d in days] + [f"after_chain_{d[5:].replace('-', '')}" for d in days]
    one_labels = [f"before_onestep_{tag}", f"after_onestep_{tag}"]
    lb = loads_between(root, [f"before_chain_{days[0][5:].replace('-', '')}", f"before_onestep_{tag}"], [f"after_chain_{tag}", f"after_onestep_{tag}"])
    summary = {"at": dt.datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M:%S"), "days": days, "loads_between_chain_and_onestep": lb, "records": []}
    att = Attributor(chain + [one], start_ids, price_win, lb, tie_break)
    R = summary["records"]

    # ---- A. chain end vs one-step (published layout) ----
    ref = "onestep"
    aux_ids = set(int(i) for i in pd.read_parquet(pub_c / "master/lifecycle_aux.parquet", columns=["tradingItemId"]).tradingItemId) if (pub_c / "master/lifecycle_aux.parquet").exists() else set()
    ev_ids = events_ids_differing(pub_c / "events/corporate_actions.parquet", pub_o / "events/corporate_actions.parquet")
    # U4 takes its event markers from the PREVIOUS build's events (T6 runs later in U5): chain end <- previous chain day, one-step <- snapshot
    prev_ev_chain = (chain[-2].stage / "T6" / "events" / "corporate_actions.parquet") if len(chain) > 1 else (start / "events" / "corporate_actions.parquet")
    if prev_ev_chain.exists():
        ev_ids |= events_ids_differing(prev_ev_chain, start / "events" / "corporate_actions.parquet")
    r_bd, d_bd = cmp_base_data(pub_c / "base_data.parquet", pub_o / "base_data.parquet", att, out, f"base_data_{ref}", ref)
    R.append(r_bd)
    r_sd, d_sd = cmp_side(pub_c / "factors/adjustment_factor_daily.parquet", pub_o / "factors/adjustment_factor_daily.parquet", att, out, f"side_{ref}", ref, aux_ids)
    R.append(r_sd)
    fdiffs = []
    for t in ("split", "dividend"):
        r_f, d_f = cmp_factors(pub_c / f"factors/{t}.parquet", pub_o / f"factors/{t}.parquet", t, att, out, ref)
        R.append(r_f)
        fdiffs.append(d_f)
    r_lc, d_lc = cmp_lifecycle(pub_c / "master/lifecycle.parquet", pub_o / "master/lifecycle.parquet", att, out, ref, events_ids=ev_ids, snapshot=start)
    R.append(r_lc)
    r_la, d_la = cmp_lifecycle(pub_c / "master/lifecycle_aux.parquet", pub_o / "master/lifecycle_aux.parquet", att, out, ref, name="master/lifecycle_aux", events_ids=ev_ids)
    R.append(r_la)
    r_ln, d_ln = cmp_lineage(pub_c / "master/lineage.parquet", pub_o / "master/lineage.parquet", att, out, ref, d_lc)
    R.append(r_ln)
    r_ev, d_ev = cmp_events(pub_c / "events/corporate_actions.parquet", pub_o / "events/corporate_actions.parquet", "events/corporate_actions", att, out, ref, d_lc, d_bd, d_ln)
    R.append(r_ev)
    r_ld, d_ld = cmp_lifecycle_dates(pub_c / "master/lifecycle_dates.parquet", pub_o / "master/lifecycle_dates.parquet", att, out, ref, d_lc, d_ev)
    R.append(r_ld)
    r_lg, d_lg = cmp_ledger(pub_c / "events/ca_ledger.parquet", pub_o / "events/ca_ledger.parquet", att, out, ref)
    R.append(r_lg)
    r_rt, d_rt = cmp_returns(pub_c / "derived/returns_total.parquet", pub_o / "derived/returns_total.parquet", att, out, ref, d_bd, fdiffs, d_lg, d_lc, d_ln,
                             pd.read_parquet(pub_c / "master/lineage.parquet"))
    R.append(r_rt)
    r_ex, d_ex = cmp_export_ledger(pub_c / "engine/ca_ledger_cache.parquet", pub_o / "engine/ca_ledger_cache.parquet", att, out, ref, d_lg, d_bd)
    R.append(r_ex)
    r_ec, d_ec = cmp_events(pub_c / "engine/events_cache.parquet", pub_o / "engine/events_cache.parquet", "engine/events_cache", att, out, ref, d_lc, d_bd, d_ln)
    R.append(r_ec)
    r_mc, d_mc = cmp_marketcap(pub_c / "derived/marketcap.parquet", pub_o / "derived/marketcap.parquet", ref, out, d_bd, lb)
    R.append(r_mc)
    r_mb, d_mb = cmp_membership(pub_c / "universe/membership.parquet", pub_o / "universe/membership.parquet", att, out, ref, d_bd, d_lc, d_mc)
    R.append(r_mb)
    R.append(cmp_calendar(pub_c / "calendar.parquet", pub_o / "calendar.parquet", att, out, ref)[0])
    R.append(cmp_sha(pub_c / "master/instruments.parquet", pub_o / "master/instruments.parquet", "master/instruments", ref))
    R.append(cmp_exclusions(pub_c / "master/tradable_exclusions.parquet", pub_o / "master/tradable_exclusions.parquet", att, out, ref, d_lc)[0])

    # ---- B. chain end vs full fetch (raw price columns + side table) ----
    ff = root / "full_fetch"
    if (ff / "base_data_full.parquet").exists():
        ref = "fullfetch"
        ff_ids = set(pd.read_parquet(ff / "ids.parquet").tradingItemId.astype(int))
        pub_ids = set(pq.read_table(pub_c / "base_data.parquet", columns=["tradingItemId"]).column(0).unique().to_pylist())
        summary["fullfetch_ids"] = {"ids_fetched": len(ff_ids), "ids_in_chain_base_data": len(pub_ids), "chain_ids_not_fetched": sorted(pub_ids - ff_ids)[:50],
                                    "n_chain_ids_not_fetched": len(pub_ids - ff_ids)}
        r_ff, d_ff = cmp_fullfetch(pub_c, ff, start, att, out, chain[-1], lb_ff=loads_between(root, ["before_fullfetch"], [f"after_chain_{tag}"]))
        R += r_ff
    # ---- C. chain end ledger vs ledger one-step on the chain end's inputs ----
    lf = root / f"ledger_full_{tag}"
    if (lf / "events" / "ca_ledger.parquet").exists():
        ref = "ledgerfull"
        lf_info = BuildInfoLedger(lf, "ledgerfull")
        att_l = Attributor(chain + [one], start_ids, price_win, loads_between(root, [f"before_chain_{days[0][5:].replace('-', '')}"], ["after_ledgerfull"]), tie_break)
        att_l.ledger_keys = dict(att.ledger_keys)
        for r in lf_info.ledger_rev.itertuples(index=False) if len(lf_info.ledger_rev) else []:
            child = int(r.childTradingItemId) if pd.notna(r.childTradingItemId) else -1
            key = (int(r.tradingItemId), int(days_of([r.exDate])[0]), str(r.eventType), str(r.legType), child)
            att_l.ledger_keys.setdefault(key, set()).add(f"{r.kind}:{r.source}@ledgerfull")
            att_l.ledger_ids.setdefault(int(r.tradingItemId), []).append((int(days_of([r.exDate])[0]), f"{r.kind}:{r.source}@ledgerfull"))
        r_lf, d_lf = cmp_ledger(chain[-1].stage / "T6" / "events" / "ca_ledger.parquet", lf / "events" / "ca_ledger.parquet", att_l, out, ref)
        r_lf["note"] = "chain-end T6 ledger vs ledger_build --incremental --prev <rc2f R1> --asof last day --build <chain end> (one full source re-query)"
        R.append(r_lf)
        summary["ledgerfull_source_changes"] = lf_info.source_summary()
        summary["ledgerfull_sha"] = {"chain_end": sha256(chain[-1].stage / "T6" / "events" / "ca_ledger.parquet"), "ledgerfull": sha256(lf / "events" / "ca_ledger.parquet")}
    # ---- D. chain day-1 ledger revisions vs the published rc2f3 ledger (tie-break attribution) ----
    b1 = chain[0]
    if len(b1.ledger_rev):
        by_src = b1.ledger_rev.source.value_counts().to_dict()
        summary["chain_day1_ledger_revisions_by_source"] = by_src
        summary["tie_break_rows_day1"] = int(sum(v for k, v in by_src.items() if "tie_break" in str(k)))
    # ---- semantic checks on the chain end (value-level; both sides of a comparison can share a wrong value) ----
    summary["semantic_chain_end"] = semantic_checks(pub_c, start, out, f"chain_end_{tag}")
    # ---- day0: same-asOf re-run of the snapshot vs the snapshot ----
    R0, s0 = compare_day0(root, args, start, start_ids, price_win, tie_break, out)
    R += R0
    summary.update(s0)
    # ---- extra one-step days ----
    for d in (getattr(args, "onestep_days", None) or []):
        t2 = d[5:].replace("-", "")
        if not (root / f"pub_one_{t2}" / "base_data.parquet").exists():
            continue
        cb = next((c for c in chain if c.asof == d), None)
        if cb is None:
            continue
        o2 = BuildInfo(root / f"one_{t2}", root / f"pub_one_{t2}", f"onestep_{t2}")
        lb2 = loads_between(root, [f"before_chain_{days[0][5:].replace('-', '')}", f"before_onestep_{t2}"], [f"after_chain_{t2}", f"after_onestep_{t2}"])
        att2 = Attributor([c for c in chain if c.asof <= d] + [o2], start_ids, price_win, lb2, tie_break)
        ref = f"onestep_{t2}"
        rb, db = cmp_base_data(cb.pub / "base_data.parquet", o2.pub / "base_data.parquet", att2, out, f"base_data_{ref}", ref)
        rl, dl = cmp_ledger(cb.pub / "events/ca_ledger.parquet", o2.pub / "events/ca_ledger.parquet", att2, out, ref)
        ev2 = events_ids_differing(cb.pub / "events/corporate_actions.parquet", o2.pub / "events/corporate_actions.parquet")
        ci = [c for c in chain if c.asof <= d]
        if len(ci) > 1:
            ev2 |= events_ids_differing(ci[-2].stage / "T6" / "events" / "corporate_actions.parquet", start / "events" / "corporate_actions.parquet")
        rlc, dlc = cmp_lifecycle(cb.pub / "master/lifecycle.parquet", o2.pub / "master/lifecycle.parquet", att2, out, ref, events_ids=ev2, snapshot=start)
        rln, dln = cmp_lineage(cb.pub / "master/lineage.parquet", o2.pub / "master/lineage.parquet", att2, out, ref, dlc)
        fd2 = []
        for t in ("split", "dividend"):
            rf, df_ = cmp_factors(cb.pub / f"factors/{t}.parquet", o2.pub / f"factors/{t}.parquet", t, att2, out, ref)
            R.append(rf)
            fd2.append(df_)
        rr_, drr = cmp_returns(cb.pub / "derived/returns_total.parquet", o2.pub / "derived/returns_total.parquet", att2, out, ref, db, fd2, dl, dlc, dln,
                               pd.read_parquet(cb.pub / "master/lineage.parquet"))
        rex, _ = cmp_export_ledger(cb.pub / "engine/ca_ledger_cache.parquet", o2.pub / "engine/ca_ledger_cache.parquet", att2, out, ref, dl, db)
        rec_ = cmp_events(cb.pub / "engine/events_cache.parquet", o2.pub / "engine/events_cache.parquet", "engine/events_cache", att2, out, ref, dlc, db, dln)[0]
        R += [rb, rl, rlc, rln, rr_, rex, rec_]
        summary[f"loads_between_chain_and_{ref}"] = lb2

    sem = summary.get("semantic_chain_end", {})
    summary["semantic_fail"] = {"new_historical_extreme_returns": int(sem.get("extreme_returns", {}).get("new_historical", 0)),
                                "new_duplicate_child_legs_linked": int(sem.get("duplicate_child_legs", {}).get("new_linked_parents", 0))}
    summary["semantic_fail"]["fail"] = any(v > 0 for k, v in summary["semantic_fail"].items() if k != "fail")
    summary["chain_events_transitions_by_kind"] = {c.label: (c.events_transitions.kind.value_counts().to_dict() if len(c.events_transitions) else {}) for c in chain}
    summary["chain_firstTradeDate_revisions"] = {c.label: int((c.lifecycle_rev.field == "firstTradeDate").sum()) if len(c.lifecycle_rev) else 0 for c in chain}
    summary["unattributed_total"] = int(sum(r.get("unattributed", 0) or 0 for r in R))
    summary["diff_cells_total"] = int(sum((r.get("diff_cells") or 0) for r in R))
    def _jsonable(o):
        if isinstance(o, dict):
            return {str(k): _jsonable(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [_jsonable(v) for v in o]
        return o
    summary = _jsonable(summary)
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    (out / "compare_table.md").write_text(compare_table_md(R, summary))
    print(compare_table_md(R, summary))
    return summary


class BuildInfoLedger:
    def __init__(self, stage: Path, label: str):
        self.stage, self.label = stage, label
        parts = [pd.read_parquet(p) for p in sorted(glob.glob(str(stage / "events" / "ca_ledger_revisions" / "date=*" / "revisions.parquet")))]
        self.ledger_rev = pd.concat([p for p in parts if len(p)], ignore_index=True) if any(len(p) for p in parts) else pd.DataFrame()
        parts = [pd.read_parquet(p) for p in sorted(glob.glob(str(stage / "events" / "ca_ledger_revisions" / "date=*" / "source_changes.parquet")))]
        self.ledger_src = pd.concat([p for p in parts if len(p)], ignore_index=True) if any(len(p) for p in parts) else pd.DataFrame()

    def source_summary(self) -> dict:
        if not len(self.ledger_src):
            return {}
        return {"rows": int(len(self.ledger_src)), "by_table_kind_cls": {"|".join(map(str, k)): int(v) for k, v in self.ledger_src.groupby(["table", "kind", "cls"]).size().items()}}


def cmp_fullfetch(pub_c: Path, ff: Path, start: Path, att: Attributor, out: Path, chain_end: BuildInfo, lb_ff: dict) -> tuple[list[dict], pd.DataFrame]:
    """Chain-end published base_data vs the full vendor re-pull: raw price columns (float32 exact) + side table adjustmentfactor.
    Structural row differences (rows dropped by the published contract) are classified 'construction' with the rule that drops them."""
    recs = []
    ff_base, ff_side = ff / "base_data_full.parquet", ff / "work" / "adjf_long.parquet"
    if (ff / "base_data_full_extra.parquet").exists():           # new ids fetched after the chain finished -> one combined file (sorted by id chunk)
        ff_base, ff_side = ff / "base_data_full_all.parquet", ff / "work" / "adjf_long_all.parquet"
        for src_a, src_b, dst in ((ff / "base_data_full.parquet", ff / "base_data_full_extra.parquet", ff_base),
                                  (ff / "work" / "adjf_long.parquet", ff / "work_extra" / "adjf_long.parquet", ff_side)):
            if not dst.exists():
                pq.write_table(pa.concat_tables([pq.read_table(src_a), pq.read_table(src_b)]), dst, compression="zstd")
    stats, diff, only_a, only_b = diff_long(pub_c / "base_data.parquet", ff_base, cols=PRICE_COLS)
    lb_prices = lb_ff.get("prices", False)
    # rows only in the chain (a): extend_prices supplements (source != 1) / aux rows fetched by R3 with a different id set; rows only in the full fetch (b):
    # mirror-tail cut (lifecycle override), off-calendar dates, rows after the lifecycle end that the chain's U2 never re-fetched ... classify by rule
    cal = pd.read_parquet(pub_c / "calendar.parquet")
    tdays = set(days_of(cal.loc[cal.isTradingDay, "date"]).tolist())
    lc = pd.concat([pd.read_parquet(pub_c / "master/lifecycle.parquet", columns=["tradingItemId", "lastTradeDate", "firstTradeDate"]),
                    pd.read_parquet(pub_c / "master/lifecycle_aux.parquet", columns=["tradingItemId", "lastTradeDate", "firstTradeDate"])], ignore_index=True)
    last = dict(zip(lc.tradingItemId.astype(int), days_of(lc.lastTradeDate.fillna(pd.Timestamp("2100-01-01")))))
    first = dict(zip(lc.tradingItemId.astype(int), days_of(lc.firstTradeDate.fillna(pd.Timestamp("1900-01-01")))))
    snap_ids = set(pd.read_parquet(start / "master" / "instruments.parquet", columns=["tradingItemId"]).tradingItemId.astype(int))
    src_col = pq.read_table(pub_c / "base_data.parquet", columns=["tradingItemId", "date", "source", "qualityFlag"])
    kk = row_keys(src_col["tradingItemId"].to_numpy(), src_col["date"].to_numpy())
    order = np.argsort(kk, kind="stable")
    kk_s = kk[order]
    src_s = src_col["source"].to_numpy()[order]
    q_s = src_col["qualityFlag"].to_numpy()[order]
    rows = []
    for k in only_a:
        tid, day = int(key_ids([k])[0]), int(key_days([k])[0])
        i = int(np.searchsorted(kk_s, k))
        src, q = (int(src_s[i]), int(q_s[i])) if i < len(kk_s) and kk_s[i] == k else (None, None)
        if src is not None and src != 1:
            c, h = "override", f"extend_prices / R3 supplement row (source {src}, qualityFlag {q})"
        elif q is not None and q & 8:
            c, h = "override", f"price override row not in the vendor table (qualityFlag {q})"
        elif tid in att.new_ids:
            c, h = "new_id", ""
        elif q is not None and (q & 4) and day > last.get(tid, 10 ** 9):
            c, h = "vendor_restatement_unseen", f"row removed by the vendor after the snapshot, outside the re-fetch window (chain keeps it as post-lifecycle tail, qualityFlag {q})"
        else:
            c, h = "unattributed", f"row in chain only (source {src}, qualityFlag {q})"
        rows.append({"tradingItemId": tid, "date": day_str(day), "column": "row", "a": "present", "b": "absent", "cls": c, "how": h})
    for k in only_b:
        tid, day = int(key_ids([k])[0]), int(key_days([k])[0])
        if day not in tdays:
            c, h = "construction", "off-calendar date (publish drops rows on non-trading days)"
        elif day > last.get(tid, 10 ** 9):
            c, h = "construction", "row after lifecycle lastTradeDate (mirror-tail cut / lifecycle override: rows removed by the rc2f contract)"
        elif day < first.get(tid, -1):
            c, h = "construction", "row before lifecycle firstTradeDate (stitched-history rule: rows before the new id's first trade removed)"
        else:
            reasons = att.price_row_reason.get((tid, day), set())
            if reasons:
                c, h = "refetch_window", ",".join(sorted(reasons))
            else:
                c, h = "unattributed", "row in full fetch only"
        rows.append({"tradingItemId": tid, "date": day_str(day), "column": "row", "a": "absent", "b": "present", "cls": c, "how": h})
    if len(diff):
        # cell diffs: override windows, restatements the chain saw (raw invariance), restatements outside any re-fetch (blind spot), window revisions
        snap = pq.read_table(start / "base_data.parquet", columns=["tradingItemId", "date"] + PRICE_COLS)
        ks = row_keys(snap["tradingItemId"].to_numpy(), snap["date"].to_numpy())
        so = np.argsort(ks, kind="stable")
        ks = ks[so]
        snap_cols = {c: snap[c].to_numpy(zero_copy_only=False)[so] for c in PRICE_COLS}
        cls, how = [], []
        dkeys = row_keys(diff.tradingItemId.to_numpy(np.int64), diff.date.to_numpy())
        pos = np.searchsorted(ks, dkeys)
        for j, (tid, day, col, a_val) in enumerate(zip(diff.tradingItemId.astype(int), days_of(diff.date), diff.column, diff.a)):
            c, h = att.price_cell(tid, int(day), col)
            if c == "refetch_window" and lb_prices:
                c, h = "vendor_late_load", h + " (price load landed between the full fetch and the chain run)"
            if c == "unattributed":
                i = int(pos[j])
                in_snap = i < len(ks) and ks[i] == dkeys[j]
                snap_val = float(snap_cols[col][i]) if in_snap else np.nan
                same_as_snap = in_snap and (str(np.float64(snap_val)) == str(a_val) or (np.isnan(snap_val) and a_val == "nan") or abs(np.float64(snap_val) - float(a_val)) <= 1e-9 * max(abs(snap_val), 1e-30))
                if same_as_snap and tid in snap_ids:
                    c, h = "vendor_restatement_unseen", "chain value == snapshot value; vendor restated the row after the snapshot; id not in any re-fetch set"
                elif tid not in snap_ids:
                    c, h = "new_id", "id added after the snapshot (U1) - full history fetched once by U1"
                else:
                    c, h = "unattributed", f"chain value differs from both the snapshot ({snap_val}) and the vendor"
            cls.append(c)
            how.append(h)
        diff["cls"], diff["how"] = cls, how
    if rows:
        diff = pd.concat([diff, pd.DataFrame(rows)], ignore_index=True)
    diff.to_csv(out / "diff_base_data_fullfetch.csv", index=False)
    r = rec("base_data (raw OHLCV+vwap)", "fullfetch", stats, diff, note="qualityFlag/source not compared (ids-file mode has no miprice cross-check, provisional or lifecycle flags)")
    recs.append(r)
    # side table
    stats2, diff2, oa2, ob2 = diff_long(pub_c / "factors/adjustment_factor_daily.parquet", ff_side, cols=["adjustmentfactor", "ciqClose"])
    rows2 = []
    for k in oa2:
        tid, day = int(key_ids([k])[0]), int(key_days([k])[0])
        rows2.append({"tradingItemId": tid, "date": day_str(day), "column": "row", "a": "present", "b": "absent", "cls": "override" if (tid, day) not in att.price_row_reason else "refetch_window", "how": "side row of a supplement/override row"})
    for k in ob2:
        tid, day = int(key_ids([k])[0]), int(key_days([k])[0])
        if day not in tdays:
            c, h = "construction", "off-calendar date"
        elif day > last.get(tid, 10 ** 9) or day < first.get(tid, -1):
            c, h = "construction", "outside lifecycle [firstTradeDate, lastTradeDate]"
        else:
            c, h = "unattributed", "side row in full fetch only"
        rows2.append({"tradingItemId": tid, "date": day_str(day), "column": "row", "a": "absent", "b": "present", "cls": c, "how": h})
    aux_ids = set(int(i) for i in pd.read_parquet(pub_c / "master/lifecycle_aux.parquet", columns=["tradingItemId"]).tradingItemId) if (pub_c / "master/lifecycle_aux.parquet").exists() else set()
    if len(diff2):
        cls, how = [], []
        for tid, day, col, a_val in zip(diff2.tradingItemId.astype(int), days_of(diff2.date), diff2.column, diff2.a):
            if tid in aux_ids and str(a_val) == "nan":
                cls.append("construction")
                how.append("aux instrument row: R3 aux prices carry no adjustment-factor side table (daily merge fills NaN); the full fetch has the vendor value")
                continue
            c, h = att.price_cell(tid, int(day), "close" if col == "ciqClose" else col)
            if c in ("unattributed", "override") and col == "adjustmentfactor":
                if tid in att.refetched_ids or tid in att.factor_ids:
                    c, h = "vendor_restatement", "id re-fetched for adjustment-factor restatement (U3)"
                elif c == "unattributed":
                    c, h = "vendor_restatement_unseen", "adjustmentfactor differs; id not in any U3 re-fetch set (side file kept from the snapshot)"
            if c == "refetch_window" and lb_prices:
                c = "vendor_late_load"
            cls.append(c)
            how.append(h)
        diff2["cls"], diff2["how"] = cls, how
    if rows2:
        diff2 = pd.concat([diff2, pd.DataFrame(rows2)], ignore_index=True)
    diff2.to_csv(out / "diff_side_fullfetch.csv", index=False)
    recs.append(rec("factors/adjustment_factor_daily", "fullfetch", stats2, diff2))
    # price override windows: the chain's daily re-application (apply_price_to_rows) vs the snapshot's batch application (rc2f_apply
    # apply_price_overrides) -> the cells inside every window must be identical (all windows end before the snapshot asOf)
    win = att.price_win
    if len(win):
        ids = sorted(set(int(i) for i in win.tradingItemId))
        def rows_of(path):
            t = pq.read_table(path, columns=["tradingItemId", "date"] + PRICE_COLS + ["qualityFlag"], filters=[("tradingItemId", "in", ids)])
            df = t.to_pandas()
            m = dr.rows_in_windows(df.tradingItemId.to_numpy(), df.date.to_numpy(), win)
            return df[m].sort_values(["tradingItemId", "date"]).reset_index(drop=True)
        wc, ws = rows_of(pub_c / "base_data.parquet"), rows_of(start / "base_data.parquet")
        st3, d3, oa3, ob3 = diff_keyed(wc, ws, ["tradingItemId", "date"])
        if len(d3):
            d3["cls"], d3["how"] = "unattributed", "override-window cell differs between the daily re-application and the snapshot batch application"
        rows3 = [{"tradingItemId": int(r.tradingItemId), "date": str(pd.Timestamp(r.date).date()), "column": "row", "a": "present", "b": "absent", "cls": "unattributed", "how": "window row only in chain"} for r in oa3.itertuples(index=False)]
        rows3 += [{"tradingItemId": int(r.tradingItemId), "date": str(pd.Timestamp(r.date).date()), "column": "row", "a": "absent", "b": "present", "cls": "unattributed", "how": "window row only in snapshot"} for r in ob3.itertuples(index=False)]
        if rows3:
            d3 = pd.concat([d3, pd.DataFrame(rows3)], ignore_index=True)
        d3.to_csv(out / "diff_override_windows_chain_vs_snapshot.csv", index=False)
        recs.append(rec("base_data (price override windows: chain vs rc2f3 snapshot)", "snapshot", st3, d3, note=f"{len(win)} windows / {len(wc)} rows: daily apply_price_to_rows == batch rc2f_apply"))
    return recs, diff


def pivot_md(R: list[dict]) -> str:
    """file x reference -> 'diff cells (classes) / unattributed' (the 파일 x 일자 view: reference = onestep_<date> / onestep(last day) / fullfetch / ledgerfull)."""
    refs = []
    for r in R:
        if r["ref"] not in refs:
            refs.append(r["ref"])
    files = []
    for r in R:
        if r["file"] not in files:
            files.append(r["file"])
    cell = {(r["file"], r["ref"]): r for r in R}
    lines = ["| file | " + " | ".join(refs) + " |", "|---|" + "---|" * len(refs)]
    for f in files:
        row = []
        for ref in refs:
            r = cell.get((f, ref))
            if r is None:
                row.append("")
            elif r.get("diff_cells") == 0 or (r.get("sha256_equal") and not r.get("diff_cells")):
                row.append("0 (sha =)" if r.get("sha256_equal") else "0")
            else:
                by = ",".join(f"{k} {v}" for k, v in sorted(r.get("by_class", {}).items(), key=lambda kv: -kv[1]))
                row.append(f"{r.get('diff_cells')} [{by}] unattr {r.get('unattributed')}")
        lines.append(f"| {f} | " + " | ".join(row) + " |")
    return "\n".join(lines)


def compare_table_md(R: list[dict], summary: dict) -> str:
    lines = ["### file x reference (diff cells [classes] unattributed)", pivot_md(R), "", "### detail",
             "| file | reference | rows chain | rows ref | only chain | only ref | sha equal | diff cells | by class | unattributed |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in R:
        by = ", ".join(f"{k} {v}" for k, v in sorted(r.get("by_class", {}).items(), key=lambda kv: -kv[1]))
        lines.append(f"| {r['file']} | {r['ref']} | {r.get('rows_a')} | {r.get('rows_b')} | {r.get('only_a')} | {r.get('only_b')} | {r.get('sha256_equal')} | {r.get('diff_cells')} | {by} | {r.get('unattributed')} |")
    lines.append("")
    lines.append(f"unattributed total: {summary.get('unattributed_total')} ; diff cells total: {summary.get('diff_cells_total')}")
    sem = summary.get("semantic_chain_end", {})
    if sem:
        e, d = sem.get("extreme_returns", {}), sem.get("duplicate_child_legs", {})
        lines.append("")
        lines.append("### semantic checks (chain end vs snapshot; value-level gate)")
        lines.append(f"- |ret| > {e.get('threshold')}: chain {e.get('chain_cells')} / snapshot {e.get('snapshot_cells')} / new {e.get('new_in_chain')} "
                     f"(after snapshot asOf {e.get('new_after_snapshot_asof')}, historical {e.get('new_historical')}) top: {e.get('top')}")
        lines.append(f"- duplicate child legs (same child+ratio, two parents within {d.get('tol_days')}d): chain {d.get('chain_pairs')} / snapshot {d.get('snapshot_pairs')} / "
                     f"new {d.get('new_in_chain')} (linked parents {d.get('new_linked_parents')}) examples: {d.get('examples')}")
        lines.append(f"- semantic gate: {summary.get('semantic_fail')}")
    lines.append("")
    lines.append("### examples (up to 2 per file x class)")
    for r in R:
        for k, ex in (r.get("examples") or {}).items():
            for e in ex:
                lines.append(f"- {r['file']} [{r['ref']}] {k}: " + "; ".join(f"{c}={v}" for c, v in e.items() if v not in ("", "nan")))
    return "\n".join(lines)


def perf_table(root: Path, days: list[str]) -> str:
    rows = []
    for i, d in enumerate(days):
        t2 = d[5:].replace("-", "")
        rows += perf_rows(root / f"b{i + 1}_{t2}", root / "logs" / f"chain_{t2}.log", f"chain {d}")
    for one in sorted(root.glob("one_*")):
        t2 = one.name.split("_", 1)[1]
        if (one / "BUILD.json").exists():
            b = json.loads((one / "BUILD.json").read_text())
            rows += perf_rows(one, root / "logs" / f"onestep_{t2}.log", f"one-step {b.get('asOf')}" + (" (--full-lifecycle)" if b.get("full_lifecycle") else ""))
    for lab, st in (("fullfetch", root / "state" / "fullfetch.done"), ("ledgerfull", root / "state" / "ledgerfull.done")):
        if st.exists():
            j = json.loads(st.read_text())
            rows.append({"build": lab, "asOf": days[-1], "wall_min": round(j.get("seconds", 0) / 60, 1), "db_wait_min": "", "peak_rss_gb": j.get("children_maxrss_gb"),
                         "steps": ""})
    lines = ["| build | asOf | wall min | DB-wait min (pg_stat_activity) | peak RSS GB | step seconds |", "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['build']} | {r['asOf']} | {r['wall_min']} | {r['db_wait_min']} | {r['peak_rss_gb']} | {r['steps']} |")
    return "\n".join(lines)


def perf_rows(stage: Path, log_path: Path, label: str) -> list[dict]:
    b = json.loads((stage / "BUILD.json").read_text()) if (stage / "BUILD.json").exists() else {}
    t = b.get("timings", {})
    waits = 0
    if log_path.exists():
        for line in open(log_path, errors="replace"):
            if "other active queries" in line and "waiting" in line:
                waits += 20
    steps = " ".join(f"{k}:{int(v.get('seconds', 0))}" for k, v in t.items() if k != "total")
    return [{"build": label, "asOf": b.get("asOf"), "wall_min": round(t.get("total", {}).get("seconds", 0) / 60, 1), "db_wait_min": round(waits / 60, 1),
             "peak_rss_gb": t.get("total", {}).get("peak_rss_gb"), "steps": steps}]


def write_report(root: Path, args) -> None:
    out = root / "compare"
    dst = Path(args.tables_out)
    dst.mkdir(parents=True, exist_ok=True)
    for p in out.glob("*.md"):
        shutil.copy2(p, dst / p.name)
    for p in out.glob("*.json"):
        shutil.copy2(p, dst / p.name)
    for p in out.glob("diff_*.csv"):
        if p.stat().st_size <= 5_000_000:
            shutil.copy2(p, dst / p.name)
    (dst / "chain_perf.md").write_text(perf_table(root, list(args.days)))
    for p in (root / "probes").glob("*.json"):
        shutil.copy2(p, dst / p.name)
    print(f"report tables -> {dst}")
